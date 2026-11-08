"""One end-to-end run: real CLI, real transport, real cron jobs, temp profile.

What this proves that no other suite does
-----------------------------------------
Every other suite builds ``Deps`` by hand or drives one module. This one runs
the *shipped* path in a throw-away ``HERMES_HOME``:

1. ``hermes plugins enable`` + the plugin copied into ``<home>/plugins/`` — the
   real discovery path, not an import from the checkout.
2. ``hermes reset-rescue ensure`` — installs the five wrapper files into
   ``<home>/scripts/`` and creates the real cron jobs through ``hermes cron``.
3. ``hermes reset-rescue scan`` — reads the pool from ``auth.json``, calls
   ``GET /rate-limit-reset-credits`` over the real ``urllib`` transport (routed
   to a loopback fake by a CONNECT proxy), and creates the primary/backup
   one-shots for a credit expiring inside the lead window.
4. the **wrapper file itself** is executed (``python <home>/scripts/reset-rescue-fire.py``)
   — the exact artifact a cron tick runs, not a Python call to ``Fire.run``.
5. the fake answers ``POST /consume``; the engine reads back, sees the credit
   gone and records ``redeemed``.
6. ``hermes reset-rescue history`` shows the entry, and the success notice is
   rendered into the outbox — captured, never sent, because the resolved target
   in a profile with no messaging channel is ``local``.

The network guard (``scripts/offline_guard.py``) is installed for the whole run
and allows only loopback; the fake backend asserts on the requests it received,
so a test that "passes" without a POST cannot happen.

Run directly (``scripts/check.sh`` does this):

    python3 <runtime-wrapper> tests/e2e/run_e2e.py [--keep]

Exit codes: 0 pass, 1 a check failed, 2 the environment cannot run it (no
``hermes`` launcher, no ``openssl``, no plugin manifest yet).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

REPO = Path(os.environ.get("RR_E2E_REPO") or Path(__file__).resolve().parents[2]).resolve()
E2E_DIR = Path(__file__).resolve().parent
#: The guard always comes from *this* worktree: `RR_E2E_REPO` may point at
#: another lane's checkout to prove the plugin against a manifest that has not
#: merged yet, and that checkout's scripts/ is not ours to depend on.
OWN_REPO = Path(__file__).resolve().parents[2]

for entry in (str(REPO / "scripts"), str(REPO), str(E2E_DIR), str(OWN_REPO / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from tests.support.isolation import (  # noqa: E402  (path set above)
    IsolationError,
    assert_isolated,
)

#: The e2e's profile: created outside ``$HOME`` and verified against Hermes' own
#: resolver (R17). It used to be ``~/.hermes/cache/scratch/rr-l5/e2e-home``,
#: which ``hermes_constants._hermes_root_of`` collapses back to ``~/.hermes`` —
#: so the run enabled the plugin in the REAL profile and read its already-current
#: dependency state. That is why it passed here and failed in the reviewer's
#: fresh isolation at the first ``plugins enable``.
E2E_HOME = Path(os.environ.get("RR_E2E_HOME") or "/var/tmp/rr-e2e/e2e-home")

# Only directories created by this process may ever be recursively removed.
_OWNED_RUNS: dict[Path, tuple[int, int]] = {}

import offline_guard  # noqa: E402  (path set above)
import seed  # noqa: E402  (path set above)
from fake_backend import (  # noqa: E402  (path set above)
    CONSUME_PATH,
    CREDITS_PATH,
    FakeBackend,
    copy_plugin,
    credit_row,
    credits_payload,
    jwt_for,
    pool_row,
    write_auth,
)

from reset_rescue import messages  # noqa: E402  (path set above)

#: The plugin's own state file inside the temp profile.
STATE_DB = Path("plugin-data") / "reset-rescue" / "data.db"

#: One ChatGPT workspace shared by two users (R17(b)). The pool of a real
#: profile looks like this: several members of the same workspace, each with a
#: token whose ``sub`` differs and whose ``chatgpt_account_id`` is identical.
WORKSPACE = "ws-e2e-shared"

#: How long a single ``hermes`` invocation may take. The launcher imports the
#: whole runtime; 3 minutes is generous and still catches a hang.
CLI_TIMEOUT_S = 180.0


class CheckFailed(AssertionError):
    """One expectation of the run did not hold."""


def check(condition: Any, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def section(title: str) -> None:
    print(f"\n-- {title}")


class Runner:
    """Runs ``hermes`` and the wrapper files against one temp profile."""

    def __init__(self, home: Path, env: dict[str, str]) -> None:
        self.home = home
        self.env = env
        #: The environment for ``hermes`` itself (no proxy, no fake CA). The
        #: plugin's own runs keep ``env``: they must reach the fake backend.
        self.admin_env = _admin_env(home)

    def hermes(
        self, *args: str, timeout: float = CLI_TIMEOUT_S, network: bool = False
    ) -> subprocess.CompletedProcess:
        """Run the real launcher against the temp profile.

        ``network=False`` (the default) uses :attr:`admin_env`: no proxy and no
        fake CA, because Hermes' own machinery (PM, the loader, cron) must never
        be pointed at the loopback fake. ``network=True`` is for the one command
        that genuinely talks to the backend — ``reset-rescue scan`` — and uses
        :attr:`env`, where ``HTTPS_PROXY``/``SSL_CERT_FILE`` route
        ``chatgpt.com`` to the fake.
        """
        env = self.env if network else self.admin_env
        self._assert_home(env)
        return subprocess.run(
            ["hermes", *args],
            capture_output=True,
            text=True,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )

    def python(
        self, script: Path, *args: str, timeout: float = CLI_TIMEOUT_S
    ) -> subprocess.CompletedProcess:
        self._assert_home(self.env)
        return subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True,
            text=True,
            env=self.env,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )

    def _assert_home(self, env: dict[str, str]) -> None:
        selected = Path(env.get("HERMES_HOME", ""))
        if not selected.is_absolute() or selected.resolve() != self.home.resolve():
            raise IsolationError("child HERMES_HOME does not match the prepared profile")
        _safe_root(selected)
        assert_isolated(selected)

    def status(self) -> dict[str, Any]:
        done = self.hermes("reset-rescue", "status", "--json")
        check(done.returncode == 0, f"status failed: {done.stdout} {done.stderr}")
        return json.loads(done.stdout)

    def state(self) -> sqlite3.Connection:
        path = self.home / STATE_DB
        check(path.is_file(), f"the plugin wrote no state database at {path}")
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def state_rw(self) -> sqlite3.Connection:
        """A writable connection, for the one thing a test must change: timing.

        The read-only default is deliberate — an assertion helper should not be
        able to corrupt the state it is checking. Only :func:`_make_primary_due`
        opens a write connection, and only to move a slot's ``fire_at``.
        """
        path = self.home / STATE_DB
        check(path.is_file(), f"the plugin wrote no state database at {path}")
        return sqlite3.connect(str(path))


def _env_for(home: Path, backend: FakeBackend) -> dict[str, str]:
    """The child environment for the plugin's own runs (scan, fire, history).

    ``backend.env()`` routes ``https://chatgpt.com`` to the loopback fake through
    ``HTTPS_PROXY`` and a throw-away ``SSL_CERT_FILE``. That is exactly right for
    the plugin's transport and exactly wrong for anything else the ``hermes``
    launcher does: ``SSL_CERT_FILE`` replaces the trust store for *every* process
    in the tree, so PM's ``uv`` cannot verify ``files.pythonhosted.org`` and the
    plugin enable fails with ``invalid peer certificate: CaUsedAsEndEntity``
    (R17). The enable therefore runs under :func:`_admin_env`, which keeps the
    profile but not the interception.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(_admin_env(home))
    # Last, so the interception wins for the plugin's own backend calls.
    env.update(backend.env())
    return env


def _admin_env(home: Path) -> dict[str, str]:
    """The environment for ``hermes`` commands that must not be intercepted.

    The caller's environment (so ``PATH`` and the launcher resolve exactly as
    they do for a user), with the profile selected and the fake backend's
    interception removed. Used for ``plugins enable``/``list``, ``cron`` and
    ``ensure`` — everything that talks to Hermes itself rather than to the fake
    backend. ``HERMES_DISABLE_LAZY_INSTALLS=1`` is the documented bridge var the
    Docker image and the hermetic harness set: it turns off lazy dependency
    installs, so enabling a plugin with ``python_dependencies: []`` never syncs
    an environment.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(
        {
            "HERMES_HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            # Keep the child from inheriting a real profile's cron/gateway env.
            "HERMES_PROFILE": "",
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "HTTPS_PROXY": "",
            "https_proxy": "",
            "HTTP_PROXY": "",
            "http_proxy": "",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "SSL_CERT_FILE": "",
        }
    )
    return env


def _safe_root(path: Path) -> Path:
    """Validate before mkdir, seeding or cleanup; never trust an env override."""
    resolved = path.resolve()
    scratch = Path("/var/tmp").resolve()
    if not path.is_absolute() or resolved == scratch or not resolved.is_relative_to(scratch):
        raise IsolationError(f"unsafe e2e root {path}: require a directory below /var/tmp")
    if any(resolved.is_relative_to(repo.resolve()) for repo in (REPO, OWN_REPO)):
        raise IsolationError(f"unsafe e2e root {path}: repository paths are refused")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise IsolationError(f"unsafe e2e root {path}: symlinked paths are refused")
    return resolved


def _cleanup_home(home: Path) -> None:
    """Remove only this process's unique directory, never the configured root."""
    root = home.parent
    expected = _OWNED_RUNS.get(root)
    if expected is None:
        raise IsolationError(f"refusing cleanup of unowned e2e directory {root}")
    _safe_root(root)
    stat = root.stat()
    if (stat.st_dev, stat.st_ino) != expected:
        raise IsolationError(f"refusing cleanup of replaced e2e directory {root}")
    shutil.rmtree(root)
    del _OWNED_RUNS[root]


def _prepare_home() -> Path:
    """A fresh profile for this run, verified not to be the real one (R17).

    The profile also receives the install's committed dependency state, built
    from local files by :mod:`seed`. Without it the first ``hermes`` launch wants
    to build one (``hermes_cli/venv_sync._prepare_borrowed_launch``) and needs
    the network; a real user's profile already has it. A seed that cannot be
    built is a **failure** here, never a silent fallback: the run would otherwise
    test a different environment than the one being shipped.
    """
    base = _safe_root(E2E_HOME)
    assert_isolated(base)
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=str(base)))
    stat = root.stat()
    _OWNED_RUNS[root] = (stat.st_dev, stat.st_ino)
    home = root / "home"
    try:
        assert_isolated(home)
        (home / "plugins").mkdir(parents=True)
        copy_plugin(REPO, home)
        source = os.environ.get("RR_E2E_SEED_STATE")
        seeded = seed.seed_profile(home, source_state=Path(source) if source else None)
        if seeded is None:
            raise CheckFailed(
                "the installed Hermes has no committed dependency state to seed the "
                "profile from (run `hermes pm install` on the install first); the e2e "
                "will not fall back to building one over the network"
            )
    except BaseException:
        _cleanup_home(home)
        raise
    return home


def _enable(runner: Runner) -> None:
    """Enable the plugin through the documented config path — offline (R17).

    ``hermes plugins enable`` is **not** an offline operation for a plugin whose
    install already carries a dependency environment: ``plugins_admission.py:66-77``
    calls ``sync_venv(explicit=True)``, and ``pm/install.py:935-938`` exempts an
    explicit sync from the lazy-install policy, so admission re-resolves the
    whole recorded extras union against the network. ``HERMES_DISABLE_LAZY_INSTALLS``
    cannot stop it — that is exactly the reviewer's failure.

    ``plugins.enabled`` in ``config.yaml`` is the documented selection PM reads
    (``pm/plugins_state.read_home_selection``) and publishes without an admission
    sync; ``hermes config set`` is the supported CLI for it. The run then proves
    the *loader* picks the plugin up, which is what the e2e is about. A refusal
    is a **failure**, never a skip.
    """
    section("enable the plugin through the documented config selection (offline)")
    done = runner.hermes("config", "set", "plugins.enabled", '["reset-rescue"]')
    check(
        done.returncode == 0,
        f"`hermes config set plugins.enabled` failed ({done.returncode}):\n"
        f"{done.stdout}\n{done.stderr}",
    )
    listed = runner.hermes("plugins", "list", "--plain", "--no-bundled")
    check(
        "reset-rescue" in listed.stdout,
        "the plugin is not listed after enable:\n"
        f"  enable rc={done.returncode}\n"
        f"  enable stdout={done.stdout!r}\n"
        f"  enable stderr={done.stderr!r}\n"
        f"  list rc={listed.returncode}\n"
        f"  list stdout={listed.stdout!r}\n"
        f"  list stderr={listed.stderr!r}",
    )


def _ensure(runner: Runner) -> set[str]:
    section("ensure: wrappers + owned cron jobs")
    done = runner.hermes("reset-rescue", "ensure")
    check(
        done.returncode == 0, f"`ensure` failed ({done.returncode}):\n{done.stdout}\n{done.stderr}"
    )
    scripts = runner.home / "scripts"
    installed = (
        {path.name for path in scripts.glob("reset-rescue-*.py")} if scripts.is_dir() else set()
    )
    expected = {
        "reset-rescue-scan.py",
        "reset-rescue-fire.py",
        "reset-rescue-fire-backup.py",
        "reset-rescue-remind.py",
        "reset-rescue-quota.py",
    }
    check(expected <= installed, f"missing wrapper files: {sorted(expected - installed)}")
    jobs = runner.hermes("cron", "list", "--all")
    check(
        "reset-rescue-scan" in jobs.stdout,
        f"the scan job was not created:\n{jobs.stdout}\n{jobs.stderr}",
    )
    return installed


def _scan(runner: Runner) -> dict[str, Any]:
    section("scan: pool read + GET credits + schedule one-shots")
    done = runner.hermes("reset-rescue", "scan", "--json", network=True)
    check(done.returncode == 0, f"`scan` failed ({done.returncode}):\n{done.stdout}\n{done.stderr}")
    try:
        status = json.loads(done.stdout)
    except ValueError as exc:
        raise CheckFailed(f"`scan --json` did not print JSON: {exc}\n{done.stdout}") from None
    check(status.get("accounts"), f"the scan found no account:\n{done.stdout}")
    check(
        status.get("problem_code") in (None, ""),
        f"the scan reported a problem: {status.get('problem_code')}",
    )
    return status


def _slots(runner: Runner) -> list[tuple[str, str, Optional[str]]]:
    conn = runner.state()
    try:
        return conn.execute("SELECT credit_key, role, job_id FROM slots ORDER BY role").fetchall()
    finally:
        conn.close()


def _make_primary_due(runner: Runner, backend: FakeBackend) -> str:
    """Rewind ONE primary slot, and its credit's expiry, so a spend is legal.

    The real scan schedules each account's primary ``lead_minutes`` before
    expiry, which is in the future by design. A cron tick cannot be waited for
    in a test, so one slot's own ``fire_at`` is moved into the past — the same
    column the scheduler would have written, and the only thing standing
    between "planned" and "due".

    Only one row is moved on purpose (R17(b)): the profile holds two users, and
    a blanket ``UPDATE ... WHERE role='primary'`` would make both due at once,
    so the run would spend twice and the "exactly one POST" assertion could
    never hold.

    The credit's own expiry is moved with it (R2, lane FA): the money boundary
    authorizes an automatic POST from the **fresh** expiry inside
    ``[expiry - lead, expiry)``, so a slot that is due while the credit still
    has an hour left is exactly the spend the gate must refuse. A test that
    wants one spend has to put that credit in its window.
    """
    section("make one primary slot due (rewind fire_at and the credit's expiry)")
    conn = runner.state_rw()
    try:
        rows = conn.execute(
            "SELECT s.credit_key,s.fire_at FROM slots s JOIN credits c USING(credit_key) "
            "WHERE s.role='primary' AND c.status='scheduled' ORDER BY s.credit_key"
        ).fetchall()
        check(rows, "the scan created no primary slot")
        credit_key = str(rows[0][0])
        conn.execute(
            "UPDATE slots SET fire_at=? WHERE credit_key=? AND role='primary'",
            (time.time() - 1.0, credit_key),
        )
        conn.execute(
            "UPDATE credits SET expires_at=? WHERE credit_key=?",
            (time.time() + 120.0, credit_key),
        )
        conn.commit()
    finally:
        conn.close()
    # The backend must serve the same fresh expiry the state now records, or the
    # money boundary re-reads the old one and refuses the POST (R2). The fake
    # answers per bearer token (R17(b)), so every list is rewound, not only the
    # global one.
    fresh = time.time() + 120.0
    for rows in (backend.credits, *backend.credits_by_token.values()):
        for row in rows.get("credits", []):
            row["expires_at"] = fresh
    return credit_key


def _run_wrapper(runner: Runner) -> None:
    section("run the wrapper file a cron tick would run")
    wrapper = runner.home / "scripts" / "reset-rescue-fire.py"
    check(wrapper.is_file(), f"no fire wrapper at {wrapper}")
    done = runner.python(wrapper)
    check(
        done.returncode == 0,
        f"the fire wrapper failed ({done.returncode}):\n{done.stdout}\n{done.stderr}",
    )


def _assert_backend_calls(backend: FakeBackend, workspace: str, runner: Runner) -> None:
    section("the backend saw the real requests")
    gets = backend.paths("GET")
    posts = backend.posts()
    check(CREDITS_PATH in gets, f"no GET of the credit list reached the backend: {backend.paths()}")
    check(
        len(posts) == 1, f"expected exactly one POST consume, saw {len(posts)}: {backend.paths()}"
    )
    post = posts[0]
    check(post.path == CONSUME_PATH, f"the POST went to {post.path}")
    check(post.has_bearer, "the POST carried no bearer token")
    check(
        isinstance(post.body.get("redeem_request_id"), str) and post.body["redeem_request_id"],
        f"the POST carried no redeem_request_id: {sorted(post.body)}",
    )
    # The readback is what confirms success (I1): a GET after the POST.
    after = backend.paths("GET")[backend.paths("POST").__len__() :]
    check(after, "no readback GET followed the consume POST")
    # R3/R17(b): every request must name the *workspace*, never the local
    # ``workspace:user`` identity the engine keys accounts by. Two users share
    # this workspace, so an identity sent here reads ``<workspace>:<user>`` and
    # the backend rejects it — the exact case the old single-user, subject-less
    # fixture could not see.
    account_ids = {
        str(request.headers.get("chatgpt-account-id"))
        for request in backend.requests
        if request.headers.get("chatgpt-account-id")
    }
    check(
        account_ids == {workspace},
        f"ChatGPT-Account-ID must be the workspace {workspace!r}, saw {sorted(account_ids)}",
    )


def _assert_state(runner: Runner, credit_key: str) -> None:
    section("state: the credit is redeemed and the slot ran")
    conn = runner.state()
    try:
        row = conn.execute(
            "SELECT status, backend_state FROM credits WHERE credit_key=?", (credit_key,)
        ).fetchone()
        check(row is not None, f"no credit row for {credit_key}")
        check(row[0] == "redeemed", f"credit status is {row[0]!r}, expected 'redeemed'")
        slot = conn.execute(
            "SELECT ran_at FROM slots WHERE credit_key=? AND role='primary'", (credit_key,)
        ).fetchone()
        check(
            slot is not None and slot[0] is not None,
            "the primary slot was not marked as run (a re-run would double-spend)",
        )
    finally:
        conn.close()


def _assert_history(runner: Runner) -> None:
    section("history: the redemption is recorded and readable")
    done = runner.hermes("reset-rescue", "history", "--json")
    check(
        done.returncode == 0, f"`history` failed ({done.returncode}):\n{done.stdout}\n{done.stderr}"
    )
    try:
        payload = json.loads(done.stdout)
    except ValueError as exc:
        raise CheckFailed(f"`history --json` did not print JSON: {exc}\n{done.stdout}") from None
    items = payload.get("items") or []
    check(items, f"the history is empty:\n{done.stdout}")
    outcomes = {str(item.get("outcome")) for item in items}
    check("redeemed" in outcomes, f"no 'redeemed' entry in the history: {outcomes}")
    reasons = {str(item.get("reason_code")) for item in items}
    check("reset" in reasons, f"no 'reset' reason in the history: {reasons}")


def _assert_notice(runner: Runner) -> None:
    section("notice: rendered into the outbox, never sent")
    conn = runner.state()
    try:
        rows = conn.execute("SELECT kind, message_key, state, params_json FROM outbox").fetchall()
    finally:
        conn.close()
    check(rows, "the outbox is empty: no notice was rendered")
    kinds = {str(row[0]) for row in rows}
    check("success" in kinds, f"no success notice in the outbox: {kinds}")
    success = next(row for row in rows if row[0] == "success")
    # The target resolves to `local` in a profile with no messaging channel, so
    # the row is queued (or skipped) — never `sent`.
    check(
        str(success[2]) in ("queued", "skipped"),
        f"the notice state is {success[2]!r}: a test must never send",
    )
    params = json.loads(success[3])
    # D-013: notice parameters come **only** from the catalog in
    # `messages.py`, not from the flow. The success notice carries the account
    # label and the two times; `reason_code`/`windows_reset` are deliberately
    # gone, so asserting them here would pin a shape the code no longer has.
    catalog_keys = set(messages.PARAMS["success"])
    check(
        set(params) <= catalog_keys,
        f"the notice carries keys outside the catalog: {sorted(set(params) - catalog_keys)}",
    )
    check("label" in params, f"the notice lost its label: {sorted(params)}")
    check(
        params.get("redeemed_at") and params.get("expires_at"),
        f"the notice lost a time: {sorted(params)}",
    )
    # The rendered text is what the user actually sees: prove it says "reset"
    # rather than asserting an internal code that no longer exists.
    rendered = messages.render("success", params, language="en")
    check(rendered.strip(), "the success notice renders empty")
    check(
        "reset" in rendered.lower(),
        f"the rendered notice does not mention the reset: {rendered!r}",
    )


def _assert_two_users_one_workspace(status: dict[str, Any]) -> None:
    section("pool: two users of one workspace stay two accounts (R17(b))")
    accounts = status.get("accounts") or []
    check(
        len(accounts) == 2,
        f"two users sharing one workspace must be two accounts, saw {len(accounts)}",
    )
    keys = {str(account.get("account_key") or "") for account in accounts}
    check(len(keys) == 2, f"the two accounts collapsed to one key: {sorted(keys)}")
    labels = sorted(str(account.get("label") or "") for account in accounts)
    check(labels == sorted(["b***@e***.com", "a***@e***.com"]), f"labels: {labels}")


def _assert_masked(status: dict[str, Any]) -> None:
    section("privacy: the label in the DTO is masked (D-013 default on)")
    accounts = status.get("accounts") or []
    check(accounts, "no account in the status")
    label = str(accounts[0].get("label") or "")
    check(label, "the account has no label")
    check("*" in label, f"the label is not masked by default: {label!r}")
    check("@" in label, f"the masked label lost its domain: {label!r}")


def _assert_no_secret_leak(runner: Runner, token: str) -> None:
    section("privacy: no token in the state file, the status or the history")
    status = runner.hermes("reset-rescue", "status", "--json")
    history = runner.hermes("reset-rescue", "history", "--json")
    for name, done in (("status", status), ("history", history)):
        blob = done.stdout + done.stderr
        check(token not in blob, f"the access token appears in `{name}` output")
    db = runner.home / STATE_DB
    raw = db.read_bytes()
    check(token.encode() not in raw, "the access token appears in the state database")
    check(b"credit-e2e" not in raw, "the raw credit id appears in the state database")


def main(argv: list[str]) -> int:
    keep = "--keep" in argv
    if shutil.which("hermes") is None:
        print("E2E SKIP: no `hermes` launcher on PATH")
        return 2
    if shutil.which("openssl") is None:
        print("E2E SKIP: no `openssl` to mint the loopback certificate")
        return 2
    if not (REPO / "plugin.yaml").is_file():
        print("E2E SKIP: plugin.yaml is not on this branch yet (L3 owns it)")
        return 2

    try:
        home = _prepare_home()
    except (IsolationError, CheckFailed, seed.SeedError) as exc:
        # Not a skip: the profile could not be prepared, so nothing about the
        # shipped path was proven. `check.sh` records this as a failure.
        print(f"E2E FAIL: cannot prepare an isolated profile: {exc}")
        return 1

    run_root = home.parent
    offline_guard.install(real_home=str(home), allowed_roots=(str(REPO), str(run_root)))
    # `hermes` is a real subprocess, and `openssl` mints the loopback certificate;
    # the guard must let exactly those through and nothing else.
    allowed = offline_guard.allow_subprocess("hermes", "openssl", "python3", "python")
    allowed.__enter__()

    failures: list[str] = []
    started = time.monotonic()
    runner = Runner(home, {})
    with offline_guard.allow_loopback("chatgpt.com"):
        backend = FakeBackend(run_root)
        backend.__enter__()
        try:
            runner = Runner(home, _env_for(home, backend))
            # R17(b): a realistic pool — two users sharing one workspace. The
            # first version of this fixture minted a JWT with no ``sub``, so the
            # engine's local identity collapsed to the bare workspace id and an
            # engine that sent the identity as ``ChatGPT-Account-ID`` still
            # looked correct (that is how R3 hid). Each user now carries a
            # subject, its own token and its own credit; the workspace id is
            # shared, so the header assertion has something to prove.
            token = jwt_for(WORKSPACE, "bob@example.com", user_id="user-jane")
            other_token = jwt_for(WORKSPACE, "ana@example.com", user_id="user-ana")
            write_auth(
                home,
                [
                    pool_row("e2e0000000000001", token, WORKSPACE, "bob@example.com"),
                    pool_row("e2e0000000000002", other_token, WORKSPACE, "ana@example.com"),
                ],
            )
            backend.credits = credits_payload(
                credit_row("credit-e2e-1", expires_in=3_600.0),
                credit_row("credit-e2e-2", expires_in=3_600.0),
            )
            # Each user's own credit list (R17(b)): the real backend answers per
            # token, so the fixture must too or both accounts would see the same
            # credit id and the second readback would look like a vanished one.
            backend.credits_by_token = {
                token: credits_payload(credit_row("credit-e2e-1", expires_in=3_600.0)),
                other_token: credits_payload(credit_row("credit-e2e-2", expires_in=3_600.0)),
            }

            _enable(runner)
            _ensure(runner)
            status = _scan(runner)
            _assert_two_users_one_workspace(status)
            _assert_masked(status)
            credit_key = _make_primary_due(runner, backend)
            _run_wrapper(runner)
            _assert_backend_calls(backend, WORKSPACE, runner)
            _assert_state(runner, credit_key)
            _assert_history(runner)
            _assert_notice(runner)
            _assert_no_secret_leak(runner, token)
        except CheckFailed as exc:
            failures.append(str(exc))
            print(f"\nFAILED: {exc}")
        except subprocess.TimeoutExpired as exc:
            failures.append(f"a command timed out after {exc.timeout}s: {exc.cmd}")
            print(f"\nFAILED: timeout {exc.cmd}")
        finally:
            backend.__exit__(None, None, None)
    allowed.__exit__(None, None, None)

    elapsed = time.monotonic() - started
    if not keep:
        _cleanup_home(home)
    if failures:
        print(f"\nE2E FAIL ({len(failures)} check(s), {elapsed:.1f}s)")
        return 1
    print(f"\nE2E OK (10 checks, {elapsed:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
