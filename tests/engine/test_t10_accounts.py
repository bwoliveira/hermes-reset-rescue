"""T-10: PoolAccountSource and ActiveSignInSource.

The pool is read through `read_credential_pool` only (never `load_pool`, never a
write); a 401 runs `hermes auth refresh openai-codex <entry id>` at most once
per account per run, re-reads the pool and retries with the new token; tokens
only ever go to `https://chatgpt.com`; an empty slice selects the active
sign-in source.
"""

from __future__ import annotations

import unittest

from reset_rescue.accounts import (
    CONSUME_PATH,
    CREDITS_PATH,
    ERR_ACCOUNT_MISSING,
    ERR_AUTH_EXPIRED,
    ERR_PROVIDER_NOT_SIGNED_IN,
    ERR_UNSUPPORTED_ENDPOINT,
    OFFICIAL_ORIGIN,
    USAGE_PATH,
    AccountMismatch,
    AccountRef,
    ActiveSignInSource,
    AuthExpired,
    PoolAccountSource,
    Response,
    SourceUnavailable,
    account_key_for,
    build_headers,
    select_source,
)
from reset_rescue.clock import CliResult, Deps
from tests.fakes import FakeCli, FakeClock, FakePool, FakeTransport, jwt_for

SALT = "a" * 64
CREDITS_URL = OFFICIAL_ORIGIN + CREDITS_PATH
USAGE_URL = OFFICIAL_ORIGIN + USAGE_PATH


def pool_row(
    entry_id: str = "entry-1",
    account_id: str = "acct-1",
    token: str | None = None,
    *,
    label: str = "",
    base_url: str = "",
    email: str = "bob@example.com",
) -> dict:
    return {
        "id": entry_id,
        "provider": "openai-codex",
        "access_token": token or jwt_for(account_id, email),
        "refresh_token": "refresh-secret-value",
        "account_id": account_id,
        "label": label,
        "base_url": base_url,
        "last_status": "ok",
        "last_refresh": "2026-11-07T00:00:00Z",
    }


def make_pool_source(rows, *, cli=None, transport=None, clock=None):
    pool = FakePool(rows)
    fake_cli = cli or FakeCli()
    source = PoolAccountSource(
        salt=SALT,
        read_pool=pool.read,
        refresh=lambda entry_id: bool(
            fake_cli.run(["auth", "refresh", "openai-codex", entry_id]).ok
        ),
        transport=transport or FakeTransport(),
    )
    return source, pool, fake_cli


class PoolDiscovery(unittest.TestCase):
    def test_rows_are_read_through_the_injected_reader(self):
        source, pool, _ = make_pool_source([pool_row()])
        refs = source.accounts()
        self.assertEqual(len(refs), 1)
        self.assertEqual(pool.reads, 1)
        self.assertEqual(pool.write_calls, [])

    def test_the_account_key_is_derived_and_the_label_is_masked(self):
        source, _, _ = make_pool_source([pool_row("e1", "acct-1", email="bob@example.com")])
        ref = source.accounts()[0]
        self.assertEqual(ref.account_key, account_key_for(SALT, "acct-1"))
        self.assertEqual(ref.label, "b***@e***.com")
        self.assertNotIn("@", ref.account_key)

    def test_a_row_without_a_token_is_skipped(self):
        source, _, _ = make_pool_source([{"id": "e1", "account_id": "acct-1"}])
        self.assertEqual(source.accounts(), [])

    def test_a_row_without_an_identity_is_skipped_with_a_code(self):
        row = pool_row()
        row["account_id"] = ""
        row["access_token"] = "not-a-jwt"
        source, _, _ = make_pool_source([row])
        self.assertEqual(source.accounts(), [])
        self.assertEqual(source.skipped(), {"entry-1": ERR_ACCOUNT_MISSING})

    def test_the_account_id_falls_back_to_the_jwt_claim(self):
        row = pool_row("e1", "acct-9")
        row["account_id"] = ""
        source, _, _ = make_pool_source([row])
        refs = source.accounts()
        self.assertEqual(refs[0].account_key, account_key_for(SALT, "acct-9"))

    def test_an_unreadable_pool_is_a_fixed_code(self):
        source = PoolAccountSource(salt=SALT, read_pool=lambda: (_ for _ in ()).throw(OSError()))
        with self.assertRaises(SourceUnavailable) as ctx:
            source.accounts()
        self.assertEqual(ctx.exception.code, "pool_unreadable")

    def test_accounts_are_sorted_by_key_for_stable_dtos(self):
        source, _, _ = make_pool_source(
            [
                pool_row("e1", "acct-1"),
                pool_row("e2", "acct-2"),
                pool_row("e3", "acct-3"),
            ]
        )
        keys = [ref.account_key for ref in source.accounts()]
        self.assertEqual(keys, sorted(keys))

    def test_the_source_reports_its_kind_and_coverage_key(self):
        source, _, _ = make_pool_source([pool_row()])
        self.assertEqual(source.kind, "pool")
        self.assertEqual(source.coverage_note(), "coverage.pool")


class TokenDiscipline(unittest.TestCase):
    def test_a_token_is_sent_only_to_chatgpt_com(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(200, {"credits": []}))
        source, _, _ = make_pool_source([pool_row()], transport=transport)
        account = source.accounts()[0]
        source.request(account, "GET", CREDITS_URL)
        self.assertEqual(len(transport.requests), 1)
        self.assertTrue(transport.requests[0]["url"].startswith("https://chatgpt.com/"))

    def test_a_non_official_url_is_refused_before_any_request(self):
        transport = FakeTransport()
        source, _, _ = make_pool_source([pool_row()], transport=transport)
        account = source.accounts()[0]
        for url in (
            "https://gateway.example.com/v1/credits",
            "http://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
            "https://chatgpt.com.evil.test/x",
        ):
            with self.assertRaises(SourceUnavailable) as ctx:
                source.request(account, "GET", url)
            self.assertEqual(ctx.exception.code, ERR_UNSUPPORTED_ENDPOINT)
        self.assertEqual(transport.requests, [])

    def test_the_authorization_header_carries_the_row_token(self):
        token = jwt_for("acct-1")
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(200, {"credits": []}))
        source, _, _ = make_pool_source([pool_row(token=token)], transport=transport)
        source.request(source.accounts()[0], "GET", CREDITS_URL)
        headers = transport.requests[0]["headers"]
        self.assertEqual(headers["Authorization"], f"Bearer {token}")
        self.assertEqual(headers["ChatGPT-Account-ID"], "acct-1")
        self.assertEqual(headers["User-Agent"], "codex-cli")

    def test_the_residency_header_is_forwarded_when_the_jwt_has_it(self):
        token = jwt_for("acct-1", residency="eu")
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(200, {"credits": []}))
        source, _, _ = make_pool_source([pool_row(token=token)], transport=transport)
        source.request(source.accounts()[0], "GET", CREDITS_URL)
        headers = transport.requests[0]["headers"]
        self.assertEqual(headers["x-openai-internal-codex-residency"], "eu")

    def test_a_consume_body_is_json_encoded(self):
        transport = FakeTransport()
        transport.on("POST", "/consume", Response(200, {"code": "reset"}))
        source, _, _ = make_pool_source([pool_row()], transport=transport)
        source.request(
            source.accounts()[0],
            "POST",
            OFFICIAL_ORIGIN + CONSUME_PATH,
            json={"credit_id": "c1", "redeem_request_id": "u1"},
        )
        post = transport.posts()[0]
        self.assertEqual(post["headers"]["Content-Type"], "application/json")
        self.assertIn(b"redeem_request_id", post["body"])

    def test_build_headers_never_invents_an_account_id(self):
        headers = build_headers("not-a-jwt")
        self.assertNotIn("ChatGPT-Account-ID", headers)
        self.assertEqual(headers["Authorization"], "Bearer not-a-jwt")

    def test_an_explicit_account_id_wins_over_the_claim(self):
        headers = build_headers(jwt_for("from-claim"), "from-row")
        self.assertEqual(headers["ChatGPT-Account-ID"], "from-row")


class RefreshOn401(unittest.TestCase):
    def test_a_401_refreshes_once_re_reads_and_retries_with_the_new_token(self):
        old = jwt_for("acct-1", expires_in=-10)
        new = jwt_for("acct-1", expires_in=3600)
        pool = FakePool([pool_row(token=old)])
        cli = FakeCli()
        transport = FakeTransport()
        calls = {"n": 0}

        def handler(method, url, headers, body, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return Response(401)
            return Response(200, {"credits": []})

        transport.on("GET", "/rate-limit-reset-credits", handler)

        def refresh(entry_id):
            pool.set_token(entry_id, new)  # Hermes rewrote auth.json
            return bool(cli.run(["auth", "refresh", "openai-codex", entry_id]).ok)

        source = PoolAccountSource(
            salt=SALT, read_pool=pool.read, refresh=refresh, transport=transport
        )
        account = source.accounts()[0]
        response = source.request(account, "GET", CREDITS_URL)

        self.assertEqual(response.status, 200)
        self.assertEqual(cli.refreshes(), ["entry-1"])
        self.assertEqual(pool.reads, 2)  # discovery + re-read
        self.assertEqual(transport.requests[0]["headers"]["Authorization"], f"Bearer {old}")
        self.assertEqual(transport.requests[1]["headers"]["Authorization"], f"Bearer {new}")

    def test_refresh_happens_at_most_once_per_account_per_run(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        source, _, cli = make_pool_source([pool_row()], transport=transport)
        account = source.accounts()[0]
        for _ in range(3):
            with self.assertRaises(AuthExpired):
                source.request(account, "GET", CREDITS_URL)
        self.assertEqual(len(cli.refreshes()), 1)
        self.assertEqual(cli.refreshes(), ["entry-1"])

    def test_a_failed_refresh_gives_auth_expired(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        cli = FakeCli()
        cli.on("auth", "refresh", "openai-codex", returncode=1, stderr="boom")
        source, _, _ = make_pool_source([pool_row()], cli=cli, transport=transport)
        with self.assertRaises(AuthExpired) as ctx:
            source.request(source.accounts()[0], "GET", CREDITS_URL)
        self.assertEqual(ctx.exception.code, ERR_AUTH_EXPIRED)

    def test_a_refresh_that_returns_no_new_token_is_a_failure(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        pool = FakePool([pool_row()])
        cli = FakeCli()
        source = PoolAccountSource(
            salt=SALT,
            read_pool=pool.read,
            refresh=lambda entry_id: bool(
                cli.run(["auth", "refresh", "openai-codex", entry_id]).ok
            ),
            transport=transport,
        )
        account = source.accounts()[0]
        pool.rows[0]["access_token"] = ""  # refresh produced nothing usable
        with self.assertRaises(AuthExpired):
            source.request(account, "GET", CREDITS_URL)

    def test_a_second_401_after_a_successful_refresh_is_auth_expired(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        source, _, cli = make_pool_source([pool_row()], transport=transport)
        with self.assertRaises(AuthExpired):
            source.request(source.accounts()[0], "GET", CREDITS_URL)
        self.assertEqual(len(cli.refreshes()), 1)

    def test_a_401_on_a_post_also_refreshes(self):
        transport = FakeTransport()
        calls = {"n": 0}

        def handler(method, url, headers, body, timeout):
            calls["n"] += 1
            return Response(401) if calls["n"] == 1 else Response(200, {"code": "reset"})

        transport.on("POST", "/consume", handler)
        source, _, cli = make_pool_source([pool_row()], transport=transport)
        response = source.request(
            source.accounts()[0], "POST", OFFICIAL_ORIGIN + CONSUME_PATH, json={"credit_id": "c"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(cli.refreshes(), ["entry-1"])

    def test_a_non_401_error_is_returned_not_raised(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(403))
        source, _, cli = make_pool_source([pool_row()], transport=transport)
        response = source.request(source.accounts()[0], "GET", CREDITS_URL)
        self.assertEqual(response.status, 403)
        self.assertEqual(cli.refreshes(), [])


class RefreshThroughTheCli(unittest.TestCase):
    """The default refresh path: `hermes auth refresh openai-codex <entry id>`."""

    def test_the_default_refresh_uses_the_cli_runner_argv(self):
        cli = FakeCli()
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        deps = Deps(cli=cli)
        pool = FakePool([pool_row()])
        source = PoolAccountSource(salt=SALT, deps=deps, read_pool=pool.read, transport=transport)
        account = source.accounts()[0]
        with self.assertRaises(AuthExpired):
            source.request(account, "GET", CREDITS_URL)
        self.assertEqual(cli.calls, [["auth", "refresh", "openai-codex", "entry-1"]])

    def test_no_cli_runner_means_no_refresh_and_auth_expired(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        pool = FakePool([pool_row()])
        source = PoolAccountSource(salt=SALT, deps=Deps(), read_pool=pool.read, transport=transport)
        with self.assertRaises(AuthExpired):
            source.request(source.accounts()[0], "GET", CREDITS_URL)

    def test_a_timed_out_refresh_is_not_a_success(self):
        cli = FakeCli()
        cli.on("auth", "refresh", "openai-codex", result=CliResult(returncode=None, timed_out=True))
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        pool = FakePool([pool_row()])
        source = PoolAccountSource(
            salt=SALT, deps=Deps(cli=cli), read_pool=pool.read, transport=transport
        )
        with self.assertRaises(AuthExpired):
            source.request(source.accounts()[0], "GET", CREDITS_URL)


class RemovedAccount(unittest.TestCase):
    def test_an_account_removed_from_the_pool_gives_account_missing(self):
        # Production shape: one source per operation. A fire for a credit whose
        # account was removed since the last scan re-reads the pool, finds no
        # row, and refuses without a POST.
        pool = FakePool([pool_row()])
        first = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=FakeTransport())
        persisted = first.accounts()[0]
        pool.rows.clear()

        transport = FakeTransport()
        second = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        self.assertEqual(second.accounts(), [])
        with self.assertRaises(SourceUnavailable) as ctx:
            second.request(
                persisted, "POST", OFFICIAL_ORIGIN + CONSUME_PATH, json={"credit_id": "c1"}
            )
        self.assertEqual(ctx.exception.code, ERR_ACCOUNT_MISSING)
        self.assertEqual(transport.requests, [])

    def test_a_key_not_discovered_in_this_run_triggers_one_re_read(self):
        pool = FakePool([pool_row()])
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(200, {"credits": []}))
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        ref = AccountRef(
            account_key=account_key_for(SALT, "acct-1"),
            label="b***@e***.com",
            source_kind="pool",
            entry_id="entry-1",
        )
        self.assertEqual(source.request(ref, "GET", CREDITS_URL).status, 200)
        self.assertEqual(pool.reads, 1)

    def test_a_row_that_disappears_mid_run_is_still_served_from_the_run_snapshot(self):
        # Documented behaviour: the snapshot is a per-run cache, so a row
        # removed mid-run keeps working until the next operation. The next
        # operation re-reads and reports account_missing (test above).
        pool = FakePool([pool_row()])
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(200, {"credits": []}))
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        account = source.accounts()[0]
        pool.rows.clear()
        self.assertEqual(source.request(account, "GET", CREDITS_URL).status, 200)


class ActiveSignIn(unittest.TestCase):
    def _source(self, *, response=None, fn=None, clock=None):
        calls: list[tuple] = []

        def request_fn(provider, method, url, *, json=None, timeout=30.0):
            calls.append((provider, method, url, json))
            if fn is not None:
                return fn(provider, method, url, json=json, timeout=timeout)
            return response or Response(
                200, {"account": {"account_id": "acct-1", "email": "bob@example.com"}}
            )

        source = ActiveSignInSource(
            salt=SALT, request_fn=request_fn, deps=Deps(clock=clock or FakeClock())
        )
        return source, calls

    def test_the_single_account_is_derived_from_the_usage_identity(self):
        source, calls = self._source()
        refs = source.accounts()
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].account_key, account_key_for(SALT, "acct-1"))
        self.assertEqual(refs[0].label, "b***@e***.com")
        self.assertEqual(refs[0].source_kind, "active_sign_in")
        self.assertEqual(calls[0][0], "openai-codex")

    def test_identity_is_cached_after_the_first_call(self):
        source, calls = self._source()
        source.accounts()
        source.accounts()
        self.assertEqual(len(calls), 1)

    def test_a_401_retries_once_after_a_delay(self):
        clock = FakeClock()
        calls: list[str] = []

        def request_fn(provider, method, url, *, json=None, timeout=30.0):
            calls.append(url)
            if url.endswith("/usage"):
                return Response(200, {"account": {"account_id": "acct-1"}})
            if len([u for u in calls if u.endswith("/rate-limit-reset-credits")]) == 1:
                return Response(401)  # the first list GET is stale
            return Response(200, {"credits": []})

        source = ActiveSignInSource(salt=SALT, request_fn=request_fn, deps=Deps(clock=clock))
        refs = source.accounts()  # 1 call: identity
        response = source.request(refs[0], "GET", CREDITS_URL)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(calls), 3)  # identity + 401 + retry
        self.assertEqual(clock.sleeps, [2.0])

    def test_a_persistent_401_is_auth_expired(self):
        calls: list[tuple] = []

        def request_fn(provider, method, url, *, json=None, timeout=30.0):
            calls.append((provider, method, url))
            return Response(401)

        source = ActiveSignInSource(salt=SALT, request_fn=request_fn, deps=Deps(clock=FakeClock()))
        account = AccountRef(
            account_key=account_key_for(SALT, "acct-1"),
            label="b***@e***.com",
            source_kind="active_sign_in",
        )
        with self.assertRaises(AuthExpired):
            source.request(account, "GET", CREDITS_URL)

    def test_a_different_account_is_an_account_mismatch(self):
        source, _ = self._source()
        source.accounts()
        other = AccountRef(
            account_key=account_key_for(SALT, "acct-2"),
            label="o***@e***.com",
            source_kind="active_sign_in",
        )
        with self.assertRaises(AccountMismatch):
            source.request(other, "GET", CREDITS_URL)

    def test_a_not_signed_in_error_is_a_fixed_code(self):
        class ProviderNotSignedIn(Exception):
            pass

        def request_fn(provider, method, url, *, json=None, timeout=30.0):
            raise ProviderNotSignedIn("no token")

        source = ActiveSignInSource(salt=SALT, request_fn=request_fn, deps=Deps(clock=FakeClock()))
        with self.assertRaises(SourceUnavailable) as ctx:
            source.accounts()
        self.assertEqual(ctx.exception.code, ERR_PROVIDER_NOT_SIGNED_IN)

    def test_a_non_official_url_is_refused(self):
        source, _ = self._source()
        account = source.accounts()[0]
        with self.assertRaises(SourceUnavailable) as ctx:
            source.request(account, "GET", "https://example.com/x")
        self.assertEqual(ctx.exception.code, ERR_UNSUPPORTED_ENDPOINT)

    def test_a_tuple_response_is_coerced(self):
        def request_fn(provider, method, url, *, json=None, timeout=30.0):
            return 200, {"content-type": "application/json"}, b'{"credits": []}'

        source = ActiveSignInSource(salt=SALT, request_fn=request_fn, deps=Deps(clock=FakeClock()))
        account = source.accounts()[0]
        response = source.request(account, "GET", CREDITS_URL)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, {"credits": []})


class SourceSelection(unittest.TestCase):
    def _deps(self, rows, settings=None):
        from reset_rescue.settings import parse_settings

        pool = FakePool(rows)
        cli = FakeCli()
        deps = Deps(salt=SALT, cli=cli, clock=FakeClock(), settings=settings or parse_settings({}))
        deps.source = None
        return deps, pool

    def test_a_non_empty_pool_selects_the_pool_source(self):
        deps = Deps(salt=SALT, clock=FakeClock())
        import reset_rescue.accounts as accounts_module

        original = accounts_module.PoolAccountSource

        class StubPool(original):
            def __init__(self, **kwargs):
                super().__init__(read_pool=lambda: [pool_row()], **kwargs)

        accounts_module.PoolAccountSource = StubPool
        try:
            source = select_source(deps)
            self.assertEqual(source.kind, "pool")
            self.assertEqual(len(source.accounts()), 1)
        finally:
            accounts_module.PoolAccountSource = original

    def test_an_empty_slice_selects_the_active_sign_in_source(self):
        import reset_rescue.accounts as accounts_module

        original = accounts_module.PoolAccountSource

        class EmptyPool(original):
            def __init__(self, **kwargs):
                super().__init__(read_pool=lambda: [], **kwargs)

        accounts_module.PoolAccountSource = EmptyPool
        try:
            self.assertEqual(
                select_source(Deps(salt=SALT, clock=FakeClock())).kind, "active_sign_in"
            )
        finally:
            accounts_module.PoolAccountSource = original

    def test_active_sign_in_is_no_longer_a_setting(self):
        """D-001: the pool is the only source; ``account_source`` was cut.

        The pool is stubbed (R17): reading the ambient profile made this test
        pass on a machine with accounts and fail on a clean one. The stub uses
        the same module-level substitution as its siblings in this class, so no
        production signature changes for a test.
        """
        import reset_rescue.accounts as accounts_module
        from reset_rescue.settings import SETTING_NAMES

        self.assertNotIn("account_source", SETTING_NAMES)
        original = accounts_module.PoolAccountSource

        class StubPool(original):
            def __init__(self, **kwargs):
                super().__init__(read_pool=lambda: [pool_row()], **kwargs)

        accounts_module.PoolAccountSource = StubPool
        try:
            source = select_source(Deps(salt=SALT, clock=FakeClock()))
            self.assertEqual(source.kind, "pool")
        finally:
            accounts_module.PoolAccountSource = original

    def test_an_unreadable_pool_falls_back_too(self):
        import reset_rescue.accounts as accounts_module

        original = accounts_module.PoolAccountSource

        class BrokenPool(original):
            def __init__(self, **kwargs):
                super().__init__(read_pool=lambda: (_ for _ in ()).throw(OSError()), **kwargs)

        accounts_module.PoolAccountSource = BrokenPool
        try:
            self.assertEqual(
                select_source(Deps(salt=SALT, clock=FakeClock())).kind, "active_sign_in"
            )
        finally:
            accounts_module.PoolAccountSource = original


class NoPoolWrites(unittest.TestCase):
    """T-10's read-only guarantee, asserted against the fake that raises on write."""

    def test_no_path_writes_the_pool(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        pool = FakePool([pool_row()])
        cli = FakeCli()
        source = PoolAccountSource(
            salt=SALT,
            read_pool=pool.read,
            refresh=lambda entry_id: bool(
                cli.run(["auth", "refresh", "openai-codex", entry_id]).ok
            ),
            transport=transport,
        )
        account = source.accounts()[0]
        with self.assertRaises(AuthExpired):
            source.request(account, "GET", CREDITS_URL)
        self.assertEqual(pool.write_calls, [])

    def test_the_module_never_imports_load_pool_or_write_credential_pool(self):
        import pathlib

        import reset_rescue.accounts as accounts_module

        text = pathlib.Path(accounts_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("load_pool(", text)
        self.assertNotIn("write_credential_pool", text)
        self.assertIn("read_credential_pool", text)


if __name__ == "__main__":
    unittest.main()
