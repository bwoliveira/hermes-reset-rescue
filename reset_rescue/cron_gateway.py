"""Owned Hermes cron jobs (architecture A§7.3-7.4, T-13, invariant I10).

The plugin never edits the cron store directly: it shells out to
``hermes cron`` through :class:`reset_rescue.cli_runner.CliRunner` and parses
the human output. Two facts from the installed CLI shape everything here:

* ``hermes cron create`` prints ``Created job: <id>`` but has no JSON mode, so
  the id is **confirmed** by re-listing and matching the unique job name
  (spike S4: the id is a 12-hex string, the name is ours and unique).
* ``hermes cron list --all`` prints one block per job: a two-space indented
  ``<id> [state]`` header followed by ``    Label:      value`` rows. A job
  that completed stays listed (``[completed]``) — that is how GC knows a
  one-shot is safe to remove.

Ownership is enforced here, not by the caller's good intentions: a job is
only ever created, edited or removed when its name starts with the plugin
prefix **and** its id is in the ``owned`` set the store recorded at creation
time (I10). A user job that happens to be named ``reset-rescue-mine`` is
never touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import keys
from .cli_runner import TRUNCATION_MARKER, CliRunner

__all__ = [
    "CronError",
    "CronJob",
    "JobSpec",
    "CronGateway",
    "SCAN_JOB_NAME",
    "SCAN_SCRIPT",
    "QUOTA_JOB_NAME",
    "QUOTA_INTERVAL_MINUTES",
    "SCRIPT_FOR_ROLE",
    "SLOT_SCRIPT_ROLE",
    "ROLES",
    "build_scan_spec",
    "build_slot_spec",
    "quota_spec",
    "slot_job_name",
    "parse_job_list",
    "strip_ansi",
    "MIN_SLOT_LEAD_SECONDS",
]

#: Recurring scan job name and script (A§7.2-7.3).
SCAN_JOB_NAME = "reset-rescue-scan"
SCAN_SCRIPT = "reset-rescue-scan.py"

#: The conditional quota job (D-009): armed only while early redemption is on
#: **and** at least one credit is still unsettled.
QUOTA_JOB_NAME = "reset-rescue-quota"
QUOTA_INTERVAL_MINUTES = 15

#: One wrapper file per role; the role is a constant in the file (I3).
SCRIPT_FOR_ROLE: Mapping[str, str] = {
    "scan": SCAN_SCRIPT,
    "primary": "reset-rescue-fire.py",
    "backup": "reset-rescue-fire-backup.py",
    "remind": "reset-rescue-remind.py",
    "quota": "reset-rescue-quota.py",
}

#: A stored slot role → the wrapper role it runs. The store keeps the two
#: reminders apart (``reminder1``/``reminder2``) because each is its own
#: one-shot; both run the same read-only reminder wrapper.
SLOT_SCRIPT_ROLE: Mapping[str, str] = {
    "primary": "primary",
    "backup": "backup",
    "reminder1": "remind",
    "reminder2": "remind",
}

ROLES = tuple(SCRIPT_FOR_ROLE)

#: A slot closer than this is not worth a job (A§8 constants).
MIN_SLOT_LEAD_SECONDS = 180.0

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HEADER = re.compile(r"^\s{2}([0-9a-f]{8,64})\s+\[([a-z_-]+)\]\s*$")
_ROW = re.compile(r"^\s{4}([A-Za-z][A-Za-z ]{1,20}):\s{2,}(.*)$")
_CREATED = re.compile(r"Created job:\s*([0-9a-f]{8,64})")
_REMOVED = re.compile(r"Removed job:\s*(.+?)\s*\(([0-9a-f]{8,64})\)")

#: Row labels ``cron list`` prints, mapped to the field name used here.
_ROW_FIELDS = {
    "Name": "name",
    "Schedule": "schedule",
    "Repeat": "repeat",
    "Next run": "next_run_at",
    "Overdue": "next_run_at",
    "Deliver": "deliver",
    "Skills": "skills",
    "Script": "script",
    "Monitor": "monitor",
    "Changed": "monitor_changed_at",
    "Mode": "mode",
    "Timezone": "timezone",
    "Grace": "grace",
    "Workdir": "workdir",
    "Python": "interpreter",
    "Last run": "last_run",
    "Dispatch": "dispatch",
    "Execution": "execution",
}


class CronError(RuntimeError):
    """A fixed-code cron failure (never carries CLI text, which can hold ids)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CronJob:
    """One job as ``hermes cron list --all`` reports it."""

    job_id: str
    name: str
    state: str = ""
    schedule: str = ""
    script: str = ""
    deliver: str = ""
    repeat: str = ""
    next_run_at: str = ""
    enabled: bool = True
    extra: Mapping[str, str] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        """True when the scheduler will not run this job again by itself."""
        return self.state in {"completed", "error"} or not self.enabled

    @property
    def paused(self) -> bool:
        return self.state == "paused"

    @property
    def live(self) -> bool:
        """True when the job is armed and will fire without help."""
        return self.enabled and self.state in {"scheduled", "active", ""}

    def repeat_completed(self) -> Optional[int]:
        match = re.match(r"^(\d+)/(\d+)$", self.repeat.strip())
        return int(match.group(1)) if match else None

    def as_dict(self) -> dict[str, Any]:
        """The ``CronGatewayProtocol`` mapping shape (``id``, not ``job_id``)."""
        return {
            "id": self.job_id,
            "job_id": self.job_id,
            "name": self.name,
            "state": self.state,
            "enabled": self.enabled,
            "schedule": self.schedule,
            "script": self.script,
            "deliver": self.deliver,
            "repeat": self.repeat,
            "next_run_at": self.next_run_at,
            "terminal": self.terminal,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True)
class JobSpec:
    """Everything needed to create one owned job, and to detect its drift."""

    name: str
    schedule: str
    script: str
    deliver: str
    failure_deliver: Optional[str] = None
    repeat: Optional[int] = None
    paused: bool = False
    paused_reason: str = ""

    def argv(self) -> list[str]:
        """The exact ``hermes cron create`` argv (A§7.3-7.4)."""
        argv = [
            "cron",
            "create",
            self.schedule,
            "--name",
            self.name,
            "--script",
            self.script,
            "--no-agent",
        ]
        if self.repeat is not None:
            argv += ["--repeat", str(int(self.repeat))]
        argv += ["--deliver", self.deliver]
        if self.failure_deliver:
            argv += ["--failure-deliver", self.failure_deliver]
        if self.paused:
            argv += ["--paused"]
            if self.paused_reason:
                argv += ["--paused-reason", self.paused_reason]
        return argv

    @property
    def is_one_shot(self) -> bool:
        return self.repeat is not None


def build_scan_spec(
    *, interval_hours: int, deliver: str, failure_deliver: Optional[str] = None
) -> JobSpec:
    """The recurring scan job (A§7.3 step 2)."""
    hours = max(1, int(interval_hours))
    return JobSpec(
        name=SCAN_JOB_NAME,
        schedule=f"every {hours}h",
        script=SCAN_SCRIPT,
        deliver=deliver,
        failure_deliver=failure_deliver or deliver,
        repeat=None,
    )


def build_slot_spec(
    *,
    role: str,
    key: str,
    fire_at_iso: str,
    deliver: str,
    failure_deliver: Optional[str] = None,
) -> JobSpec:
    """One per-credit one-shot (simplification §3).

    ``fire_at_iso`` must carry an offset (``2026-11-09T21:30:00-03:00``): the
    CLI reads a bare local time in the profile's zone, and a slot is defined
    against an absolute instant. ``role`` is the **stored** slot role
    (``primary``/``backup``/``reminder1``/``reminder2``), so the name and the
    wrapper both come from one vocabulary; two reminders of the same credit
    never collide on one job name (R13).
    """
    script_role = SLOT_SCRIPT_ROLE.get(role)
    if script_role is None:
        raise CronError("unknown_role")
    return JobSpec(
        name=slot_job_name(role, key),
        schedule=fire_at_iso,
        script=SCRIPT_FOR_ROLE[script_role],
        deliver=deliver,
        failure_deliver=failure_deliver or deliver,
        repeat=1,
    )


def slot_job_name(role: str, key: str) -> str:
    """``reset-rescue-<key8>[-b]`` / ``reset-rescue-remind-<1|2>-<key8>``.

    One rule for all four slot roles: the rescue pair keeps A§7.4's short shape
    (the backup carries ``-b``), and each reminder carries its own index so the
    two of them, the pair and the scan job are distinct names for one credit.
    """
    suffix = job_suffix_for(role)
    if role in ("primary", "backup"):
        tail = f"-{suffix}" if suffix else ""
        return f"{keys.JOB_PREFIX}{keys.job_suffix(key)}{tail}"
    return f"{keys.JOB_PREFIX}remind-{suffix}-{keys.job_suffix(key)}"


def job_suffix_for(role: str) -> str:
    """The role's marker in a job name (``""``/``b``/``1``/``2``)."""
    suffix = {"primary": "", "backup": "b", "reminder1": "1", "reminder2": "2"}.get(role)
    if suffix is None:
        raise CronError("unknown_role")
    return suffix


def quota_spec(*, deliver: str) -> JobSpec:
    """The conditional quota job: every 15 minutes, ``quota-check`` (§3)."""
    return JobSpec(
        name=QUOTA_JOB_NAME,
        schedule=f"every {QUOTA_INTERVAL_MINUTES}m",
        script=SCRIPT_FOR_ROLE["quota"],
        deliver=deliver,
        failure_deliver=deliver,
        repeat=None,
    )


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text or "")


def parse_job_list(text: str) -> list[CronJob]:
    """Parse ``hermes cron list --all`` output into jobs.

    Unknown rows are kept in ``extra`` rather than dropped, so a future CLI
    row cannot silently change a drift verdict. Lines outside a job block
    (banner, footer warnings) are ignored.
    """
    jobs: list[CronJob] = []
    current: Optional[dict[str, Any]] = None
    for raw_line in strip_ansi(text).splitlines():
        header = _HEADER.match(raw_line)
        if header:
            if current is not None:
                jobs.append(_to_job(current))
            current = {"job_id": header.group(1), "state": header.group(2), "extra": {}}
            continue
        if current is None:
            continue
        row = _ROW.match(raw_line)
        if not row:
            continue
        label, value = row.group(1).strip(), row.group(2).strip()
        field_name = _ROW_FIELDS.get(label)
        if field_name is None:
            current["extra"][label] = value
            continue
        current[field_name] = value
    if current is not None:
        jobs.append(_to_job(current))
    return jobs


def _to_job(raw: Mapping[str, Any]) -> CronJob:
    state = str(raw.get("state") or "")
    deliver = str(raw.get("deliver") or "")
    return CronJob(
        job_id=str(raw.get("job_id") or ""),
        name=str(raw.get("name") or ""),
        state=state,
        schedule=str(raw.get("schedule") or ""),
        script=str(raw.get("script") or ""),
        deliver=deliver,
        repeat=str(raw.get("repeat") or ""),
        next_run_at=str(raw.get("next_run_at") or ""),
        enabled=state not in {"paused", "disabled", "completed", "error"},
        extra=dict(raw.get("extra") or {}),
    )


class CronGateway:
    """Create, list and remove the plugin's own cron jobs.

    One API, not two: :meth:`create` / :meth:`ensure` / :meth:`drift` /
    :meth:`remove` all take or return :class:`JobSpec`/:class:`CronJob`, and
    :meth:`list_jobs` is the flows' read shape. There is no argv-level create
    path (R18: the two dialects drifted, so the abandoned one is gone).

    ``owned`` is the set of job ids the store recorded in ``owned_jobs``. It is
    passed per call (never cached) because the store is the source of truth and
    another process may have added a job since this one started.
    """

    def __init__(
        self, cli: "CliRunner | Any", *, prefix: str = keys.JOB_PREFIX, timeout: float = 60.0
    ) -> None:
        self.cli = cli
        self.prefix = prefix
        self.timeout = float(timeout)

    # -- protocol: listing ------------------------------------------------

    def jobs(self, *, include_all: bool = True) -> list[CronJob]:
        """Every job in the profile as :class:`CronJob`; raises :class:`CronError`.

        A listing the runner had to cut short is **refused**, never parsed:
        a truncated view is missing the tail of the profile, and reading it as
        "these are all the jobs" would make the plugin recreate jobs that
        exist and delete ownership it still has (R8).
        """
        argv = ["cron", "list"] + (["--all"] if include_all else [])
        result = self.cli.run(argv, timeout=self.timeout)
        if result.timed_out:
            raise CronError("cron_timeout")
        if result.exec_error:
            raise CronError(result.exec_error)
        if result.returncode != 0:
            raise CronError("cron_list_failed")
        if TRUNCATION_MARKER in result.stdout:
            raise CronError("cron_list_truncated")
        return parse_job_list(result.stdout)

    def list_jobs(self, *, include_all: bool = True) -> list[dict[str, Any]]:
        """One mapping per job, ``id`` included — the flows' read shape.

        Deliberately no ``argv`` key: ``cron list`` does not print the original
        argv, and inventing one would make drift detection recreate the job on
        every ``ensure``. Drift is decided by :meth:`drift`/:meth:`ensure`,
        which compare the fields the CLI prints.
        """
        return [job.as_dict() for job in self.jobs(include_all=include_all)]

    def find(self, name: str, *, jobs: Optional[Sequence[CronJob]] = None) -> Optional[CronJob]:
        """The job with exactly ``name``, or ``None`` (never a prefix match)."""
        for job in self.jobs() if jobs is None else jobs:
            if job.name == name:
                return job
        return None

    # -- ownership ---------------------------------------------------------

    def owns_name(self, name: str) -> bool:
        return bool(name) and name.startswith(self.prefix)

    def owns(self, job: CronJob, owned: Iterable[str]) -> bool:
        """True only for a job this plugin created (name prefix **and** recorded id)."""
        return self.owns_name(job.name) and job.job_id in set(owned)

    # -- create ------------------------------------------------------------

    def create(self, spec: JobSpec, *, timeout: Optional[float] = None) -> Optional[CronJob]:
        """Create ``spec`` and return the job as the CLI now lists it.

        The id printed by ``create`` is the **identity** of what we just made:
        core cron permits duplicate names, so a same-name job that was already
        in the profile is not ours and must never be adopted (R14). The job is
        therefore looked up **by that id**, and a listing that does not show it
        yet (a store write racing the list) yields the announced id itself
        rather than somebody else's job.
        """
        if not self.owns_name(spec.name):
            raise CronError("foreign_name")
        result = self.cli.run(spec.argv(), timeout=timeout or self.timeout)
        if result.timed_out:
            raise CronError("cron_timeout")
        if result.exec_error:
            raise CronError(result.exec_error)
        if result.returncode != 0:
            raise CronError("cron_create_failed")
        announced = _CREATED.search(strip_ansi(result.stdout))
        if announced is None:
            # The CLI prints "Failed to create job: …" and exits 0 in some
            # paths; the absence of the success line is the real signal.
            raise CronError("cron_create_failed")
        announced_id = announced.group(1)
        for job in self.jobs():
            if job.job_id == announced_id:
                return job
        # Created but not visible yet (a store write raced the list): report the
        # announced id so the caller can record and re-verify it later.
        return CronJob(
            job_id=announced_id,
            name=spec.name,
            state="scheduled",
            schedule=spec.schedule,
            script=spec.script,
            deliver=spec.deliver,
        )

    # -- remove ------------------------------------------------------------

    def remove(self, job: CronJob | str, *, owned: Iterable[str]) -> bool:
        """Remove one owned job; ``False`` when it is foreign or still listed.

        A job id that is not in ``owned`` is refused **before** any CLI call:
        the plugin must not be able to delete a job it did not create (I10).
        ``True`` means the scheduler no longer lists the job — the CLI's
        "Removed job" line alone is a claim, the listing is the fact (R13).
        """
        job_id = job.job_id if isinstance(job, CronJob) else str(job)
        if not job_id or job_id not in set(owned):
            return False
        return self._remove_by_id(job_id)

    def _remove_by_id(self, job_id: str) -> bool:
        result = self.cli.run(["cron", "remove", job_id], timeout=self.timeout)
        if result.timed_out or result.exec_error:
            return False
        text = strip_ansi(result.stdout) + strip_ansi(result.stderr)
        if result.returncode != 0 and "not found" not in text.lower():
            return False
        # Verify: a "Removed job" line is the CLI's claim, the list is the fact.
        # A listing that cannot be read proves nothing either way, so the
        # removal is *unverified* — never a success (R13). The caller keeps the
        # ownership row, the credit and the UUID and retries.
        try:
            still_there = any(job.job_id == job_id for job in self.jobs())
        except CronError:
            return False
        return not still_there

    # -- ensure ------------------------------------------------------------

    def drift(self, job: CronJob, spec: JobSpec) -> list[str]:
        """Why ``job`` does not match ``spec`` (empty = no drift).

        Compared: name, script, deliver target, schedule and (for a one-shot)
        that it is still armed. ``failure_deliver`` is not printed by
        ``cron list``; it is re-applied by recreating on any other drift and by
        the next ``ensure`` after a target change.
        """
        reasons: list[str] = []
        if job.name != spec.name:
            reasons.append("name")
        if job.script and job.script != spec.script:
            reasons.append("script")
        if job.deliver and job.deliver != spec.deliver:
            reasons.append("deliver")
        if job.schedule and not _schedule_matches(job.schedule, spec.schedule):
            reasons.append("schedule")
        if job.terminal:
            # A completed/paused/disabled job will not fire again, whether it
            # is a one-shot or the recurring scan: keeping it would report a
            # protection the scheduler no longer provides (R7).
            reasons.append("terminal")
        return reasons

    def ensure(
        self, spec: JobSpec, *, owned: Iterable[str], now: Optional[float] = None
    ) -> tuple[str, Optional[CronJob]]:
        """Create, recreate or keep ``spec``; returns ``(action, job)``.

        Actions: ``unchanged``, ``created``, ``recreated``, ``foreign`` (a job
        with our name exists but is not ours — never touched) and ``failed``.
        A one-shot is only created while it is still worth firing
        (``fire_at > now + MIN_SLOT_LEAD_SECONDS``); otherwise ``too_late``.
        """
        owned_ids = set(owned)
        jobs = self.jobs()
        existing = next((job for job in jobs if job.name == spec.name), None)

        if spec.is_one_shot and now is not None:
            fire_at = _parse_iso(spec.schedule)
            if fire_at is not None and fire_at <= now + MIN_SLOT_LEAD_SECONDS:
                if existing is not None and self.owns(existing, owned_ids):
                    self.remove(existing, owned=owned_ids)
                return "too_late", None

        if existing is not None:
            if not self.owns(existing, owned_ids):
                return "foreign", existing
            if not self.drift(existing, spec):
                return "unchanged", existing
            if not self.remove(existing, owned=owned_ids):
                return "failed", existing
            created = self.create(spec)
            return ("recreated", created) if created is not None else ("failed", None)

        created = self.create(spec)
        return ("created", created) if created is not None else ("failed", None)

    # -- convenience -------------------------------------------------------

    def live_owned(self, name: str, *, owned: Iterable[str]) -> Optional[CronJob]:
        """The owned job with ``name`` when it is still armed, else ``None``."""
        job = self.find(name)
        if job is None or not self.owns(job, owned) or not job.live:
            return None
        return job

    def completed_owned(self, *, owned: Iterable[str]) -> list[CronJob]:
        """Owned one-shots the scheduler has finished (GC candidates, A§7.5)."""
        owned_ids = set(owned)
        try:
            jobs = self.jobs()
        except CronError:
            return []
        return [job for job in jobs if self.owns(job, owned_ids) and job.terminal]


def _schedule_matches(actual: str, wanted: str) -> bool:
    """Compare the CLI's rendered schedule with the one we asked for.

    ``hermes cron create "every 12h"`` renders ``every 720m``; a one-shot
    ISO instant renders ``once at 2026-11-07 05:49``. Both are compared
    semantically, not by string equality, so a cosmetic CLI change does not
    cause a recreate loop on every scan.
    """
    left = " ".join(str(actual).split()).strip().lower()
    right = " ".join(str(wanted).split()).strip().lower()
    if left == right:
        return True
    interval = re.fullmatch(r"every\s+(\d+)\s*([hm])", right)
    if interval:
        amount, unit = int(interval.group(1)), interval.group(2)
        minutes = amount * 60 if unit == "h" else amount
        return left in {f"every {minutes}m", f"every {amount}{unit}"}
    if right.endswith("z") or re.search(r"[+-]\d{2}:\d{2}$", right):
        moment = _parse_iso(wanted)
        if moment is not None:
            return left.startswith("once at ")
    return False


def _parse_iso(value: str) -> Optional[float]:
    from datetime import datetime

    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
