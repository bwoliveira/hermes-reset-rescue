"""T-75 + D-014/D-015: the popup's four routes.

``GET /status``, ``POST /scan`` (ensure, then scan), ``POST /redeem`` and
``GET /history``. This suite pins the table, the body and query schemas
(unknown field → 422, never reaching the service), the busy/timeout mapping
and that only allow-listed DTOs leave.

Run: python3 <runtime wrapper> <wt>/tests/run_lane.py tests/api
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from reset_rescue import dto as dto_module
from reset_rescue.accounts import AccountRef, Response
from reset_rescue.service import Service
from reset_rescue.store import LockTimeout
from tests.api.test_t28_routes import API, CREDIT_1, KEY_A, KEY_B, Base, seeded_service
from tests.fakes import FakeSource, FakeStore, make_deps, make_settings, usage_payload

PREFIX = "/api/plugins/reset-rescue"


class Recorder:
    """A service double that records calls and returns real DTOs."""

    def __init__(self, ensure_error: Exception | None = None) -> None:
        self.calls: list = []
        self.inner = seeded_service()
        self.ensure_error = ensure_error

    def status(self):
        self.calls.append(("status", {}))
        return self.inner.status()

    def ensure(self):
        self.calls.append(("ensure", {}))
        if self.ensure_error is not None:
            raise self.ensure_error
        return dto_module.EnsureDTO(armed=True, created=[], updated=[], removed=[], problems=[])

    def scan(self, **kwargs):
        self.calls.append(("scan", kwargs))
        return self.inner.status()

    def redeem_now(self, credit_key, **kwargs):
        self.calls.append(("redeem_now", {"credit_key": credit_key, **kwargs}))
        return dto_module.RedeemResultDTO(
            credit_key=credit_key, outcome="redeemed", reason_code=None, windows_reset=2
        )

    def history(self, **kwargs):
        self.calls.append(("history", kwargs))
        item = dto_module.HistoryItemDTO(
            at="2026-11-08T12:00:00Z",
            action="fire",
            credit_key="c" * 16,
            account_key="a" * 16,
            outcome="redeemed",
            reason_code=None,
        )
        return dto_module.HistoryDTO(items=[item])


class Http(Base):
    def post(self, path: str, body=None, raw: bytes | None = None):
        if raw is not None:
            return self.http.request(
                "POST", f"{PREFIX}{path}", content=raw, headers={"content-type": "application/json"}
            )
        return self.http.post(f"{PREFIX}{path}", json=body)

    def use(self, service) -> None:
        API.set_service_factory(lambda: service)


class RouteTable(Http):
    def test_the_route_table_is_pinned(self):
        """Four popup routes plus the two settings routes D-025 added."""
        self.assertEqual(
            sorted(API.ROUTES),
            [
                ("GET", "/history"),
                ("GET", "/settings"),
                ("GET", "/status"),
                ("GET", "/{path:path}"),  # D-033 stable dispatcher
                ("POST", "/redeem"),
                ("POST", "/scan"),
                ("POST", "/settings"),
                ("POST", "/settings/account"),  # D-030
                ("POST", "/settings/account/reset"),  # D-030
                ("POST", "/settings/account/reset-all"),  # D-033
                ("POST", "/settings/reset"),  # D-033
                ("POST", "/{path:path}"),
            ],
        )
        served = sorted((m, r.path) for r in API.router.routes for m in r.methods)
        self.assertEqual(served, sorted(API.ROUTES))

    def test_cut_routes_are_gone(self):
        self.use(Recorder())
        for path in ("/ensure", "/accounts/include", "/test-notice", "/rehearse"):
            self.assertIn(self.post(path, {}).status_code, (404, 405), path)


class ScanRoute(Http):
    def test_scan_ensures_then_scans_and_returns_the_status_dto(self):
        rec = Recorder()
        self.use(rec)
        response = self.post("/scan", {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(tuple(response.json()), dto_module.StatusDTO.FIELDS)
        self.assertEqual(rec.calls, [("ensure", {}), ("scan", {"dry_run": None})])

    def test_scan_passes_an_explicit_dry_run_and_accepts_no_body(self):
        rec = Recorder()
        self.use(rec)
        self.assertEqual(self.post("/scan", {"dry_run": True}).status_code, 200)
        self.assertEqual(self.post("/scan", raw=b"").status_code, 200)
        self.assertEqual(
            [c for c in rec.calls if c[0] == "scan"],
            [("scan", {"dry_run": True}), ("scan", {"dry_run": None})],
        )

    def test_scan_rejects_unknown_fields_and_non_bool_dry_run(self):
        rec = Recorder()
        self.use(rec)
        for body in ({"credit_key": "x"}, {"dry_run": "yes"}, {"dry_run": 1}, {"profile": "work"}):
            self.assertEqual(self.post("/scan", body).status_code, 422, body)
        self.assertEqual(self.post("/scan", raw=b"{not json").status_code, 422)
        self.assertEqual(rec.calls, [])

    def test_an_ensure_failure_does_not_stop_the_scan(self):
        rec = Recorder(ensure_error=RuntimeError("cron missing"))
        self.use(rec)
        response = self.post("/scan", {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([c[0] for c in rec.calls], ["ensure", "scan"])
        self.assertNotIn("cron missing", response.text)

    def test_a_held_lock_in_ensure_or_scan_is_409(self):
        self.use(Recorder(ensure_error=LockTimeout("run.lock held by pid 1")))
        response = self.post("/scan", {})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {
                "error": "busy",
                "message_key": "errors.busy",
                "detail": "Another operation is running.",
            },
        )

        class Locked(Recorder):
            def scan(self, **_):
                raise LockTimeout("run.lock held by pid 1")

        self.use(Locked())
        response = self.post("/scan", {})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("pid", response.text)

    def test_scan_answers_504_before_the_ui_timeout(self):
        self.assertLess(API.SCAN_TIMEOUT_S, 45.0, "the UI waits 45 s for scan/redeem")
        original = API.SCAN_TIMEOUT_S
        API.SCAN_TIMEOUT_S = 0.2

        class Slow(Recorder):
            def scan(self, **_):
                time.sleep(1.0)
                return self.inner.status()

        try:
            self.use(Slow())
            started = time.monotonic()
            response = self.post("/scan", {})
            self.assertLess(time.monotonic() - started, 0.9)
            self.assertEqual(response.status_code, 504)
        finally:
            API.SCAN_TIMEOUT_S = original

    def test_scan_never_returns_a_non_dto(self):
        class Leaky(Recorder):
            def scan(self, **_):
                return {"access_token": "tok-SECRET"}

        self.use(Leaky())
        response = self.post("/scan", {})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("tok-SECRET", response.text)


class RedeemRoute(Http):
    def test_redeem_passes_the_credit_and_returns_the_result_dto(self):
        rec = Recorder()
        self.use(rec)
        response = self.post("/redeem", {"credit_key": "c" * 16, "confirm": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(tuple(response.json()), dto_module.RedeemResultDTO.FIELDS)
        self.assertEqual(response.json()["outcome"], "redeemed")
        self.assertEqual(rec.calls, [("redeem_now", {"credit_key": "c" * 16, "confirm": True})])

    def test_redeem_requires_a_literal_true_confirm(self):
        rec = Recorder()
        self.use(rec)
        for body in (
            {"credit_key": "c" * 16},
            {"credit_key": "c" * 16, "confirm": False},
            {"credit_key": "c" * 16, "confirm": "true"},
            {"credit_key": "c" * 16, "confirm": 1},
        ):
            self.assertEqual(self.post("/redeem", body).status_code, 422, body)
        self.assertEqual(rec.calls, [])

    def test_redeem_rejects_bad_keys_and_unknown_fields(self):
        rec = Recorder()
        self.use(rec)
        for body in (
            {"confirm": True},
            {"credit_key": "", "confirm": True},
            {"credit_key": 7, "confirm": True},
            {"credit_key": "x" * 65, "confirm": True},
            {"credit_key": "c" * 16, "confirm": True, "credit_id": "raw"},
        ):
            self.assertEqual(self.post("/redeem", body).status_code, 422, body)
        self.assertEqual(self.post("/redeem", raw=b"").status_code, 422)
        self.assertEqual(rec.calls, [])

    def test_redeem_through_the_real_service_refuses_an_unknown_credit(self):
        API.set_service_factory(seeded_service)
        response = self.post("/redeem", {"credit_key": "e" * 16, "confirm": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], "failed")
        self.assertNotIn("raw-credit-id", response.text)

    def test_redeem_busy_is_409(self):
        class Locked(Recorder):
            def redeem_now(self, credit_key, **_):
                raise LockTimeout("run.lock held")

        self.use(Locked())
        response = self.post("/redeem", {"credit_key": "c" * 16, "confirm": True})
        self.assertEqual(response.status_code, 409)


class HistoryRoute(Http):
    def test_history_defaults_to_a_short_list(self):
        rec = Recorder()
        self.use(rec)
        response = self.get("/history")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(tuple(response.json()), dto_module.HistoryDTO.FIELDS)
        self.assertEqual(tuple(response.json()["items"][0]), dto_module.HistoryItemDTO.FIELDS)
        self.assertEqual(rec.calls, [("history", {"limit": API.HISTORY_DEFAULT_LIMIT})])

    def test_history_limit_bounds(self):
        rec = Recorder()
        self.use(rec)
        self.assertEqual(self.get("/history?limit=200").status_code, 200)
        self.assertEqual(self.get("/history?limit=1").status_code, 200)
        for bad in ("0", "201", "-1", "ten", "1.5", ""):
            response = self.get(f"/history?limit={bad}")
            self.assertEqual(response.status_code, 422, bad)
            self.assertEqual(
                response.json(),
                {
                    "error": "invalid_query",
                    "message_key": "errors.invalid_query",
                    "detail": "Invalid query parameters.",
                },
            )
        self.assertEqual([c[1]["limit"] for c in rec.calls], [200, 1])

    def test_history_through_the_real_service(self):
        API.set_service_factory(seeded_service)
        response = self.get("/history?limit=5")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json()["items"], list)


class PopupContract(Base):
    """The five fields the popup reads, proven through real HTTP.

    L4b's popup asks the backend for exactly these names. The engine tests
    prove the values; this class proves they survive the route, the allow-list
    serializer and JSON, which is the only path the Desktop ever sees.
    """

    def _status(self, service) -> dict:
        API.set_service_factory(lambda: service)
        response = self.get("/status")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _service(self, **settings):
        """A real Service over a real store shape, with a scripted usage read."""
        source = FakeSource(
            [
                AccountRef(
                    account_key=KEY_A, label="b***@e***.com", source_kind="pool", entry_id="e1"
                )
            ]
        )
        source.on("GET", "/usage", Response(200, usage_payload(weekly=60.0, session=12.0)))
        store = FakeStore()
        store.account_upsert(
            {
                "account_key": KEY_A,
                "label": "b***@e***.com",
                "source_kind": "pool",
                "last_list_ok_at": 1_800_000_000.0,
                "last_error_code": None,
            }
        )
        store.credit_upsert(
            {
                "credit_key": CREDIT_1,
                "account_key": KEY_A,
                "title": "Full reset",
                "reset_type": "codex_rate_limits",
                "granted_at": 1_799_000_000.0,
                "expires_at": 1_800_003_600.0,
                "backend_state": "available",
                "status": "scheduled",
                "updated_at": 1_800_000_000.0,
            }
        )
        deps = make_deps(
            store=store, source=source, settings=make_settings(**settings) if settings else None
        )
        return Service(deps)

    # -- 1. D-017 quota ----------------------------------------------------
    def test_account_quota_carries_exactly_the_three_names_the_ui_reads(self):
        body = self._status(self._service())
        quota = body["accounts"][0]["quota"]
        self.assertEqual(
            sorted(quota), ["five_hour_remaining_pct", "plan_type", "weekly_remaining_pct"]
        )
        self.assertEqual(quota["five_hour_remaining_pct"], 88)
        self.assertEqual(quota["weekly_remaining_pct"], 40)

    def test_quota_is_null_when_the_backend_did_not_report_it(self):
        source = FakeSource(
            [
                AccountRef(
                    account_key=KEY_A, label="b***@e***.com", source_kind="pool", entry_id="e1"
                )
            ]
        )
        source.on("GET", "/usage", Response(500))
        store = FakeStore()
        store.account_upsert(
            {"account_key": KEY_A, "label": "b***@e***.com", "source_kind": "pool"}
        )
        store.credit_upsert(
            {
                "credit_key": CREDIT_1,
                "account_key": KEY_A,
                "backend_state": "available",
                "status": "scheduled",
                "expires_at": 1_800_003_600.0,
                "updated_at": 1_800_000_000.0,
            }
        )
        deps = make_deps(store=store, source=source, settings=make_settings())
        self.assertIsNone(self._status(Service(deps))["accounts"][0]["quota"])

    # -- 2. D-016 language -------------------------------------------------
    def test_language_auto_reaches_the_desktop_as_auto(self):
        body = self._status(self._service(language="auto"))
        self.assertEqual(body["language"], "auto")

    def test_an_explicit_language_reaches_the_desktop_unchanged(self):
        self.assertEqual(self._status(self._service(language="pt-BR"))["language"], "pt-BR")

    # -- 3. footer threshold ----------------------------------------------
    def test_settings_carry_the_early_rescue_flag_and_threshold(self):
        body = self._status(self._service(early_redeem_enabled=True, early_redeem_remaining_pct=5))
        settings = body["settings"]
        self.assertIs(settings["early_redeem_enabled"], True)
        self.assertEqual(settings["early_redeem_remaining_pct"], 5)

    def test_the_footer_threshold_key_is_the_real_setting_name(self):
        """The popup reads ``early_redeem_remaining_pct``; the old name is gone."""
        body = self._status(self._service())
        self.assertIn("early_redeem_remaining_pct", body["settings"])
        self.assertNotIn("early_redeem_weekly_remaining_pct", body["settings"])

    def test_a_legacy_key_still_configures_the_threshold(self):
        """A config written by an older build keeps working (read-only alias)."""
        from reset_rescue.settings import parse_settings

        service = self._service()
        service.deps.settings = parse_settings({"early_redeem_weekly_remaining_pct": 15})
        body = self._status(service)
        self.assertEqual(body["settings"]["early_redeem_remaining_pct"], 15)

    # -- 4. empty-state account count -------------------------------------
    def test_account_count_is_present_and_counts_every_scanned_account(self):
        store = FakeStore()
        for key in (KEY_A, KEY_B):
            store.account_upsert(
                {"account_key": key, "label": f"{key[0]}***@example.com", "source_kind": "pool"}
            )
        store.credit_upsert(
            {
                "credit_key": CREDIT_1,
                "account_key": KEY_A,
                "backend_state": "available",
                "status": "scheduled",
                "expires_at": 1_800_003_600.0,
                "updated_at": 1_800_000_000.0,
            }
        )
        deps = make_deps(store=store, settings=make_settings())
        body = self._status(Service(deps))
        self.assertEqual(len(body["accounts"]), 2, "every pool account is listed, D-027")
        self.assertEqual(body["account_count"], 2)

    # -- 5. D-013 masking -------------------------------------------------
    def test_labels_are_masked_by_default(self):
        body = self._status(self._service())
        self.assertNotIn("@", body["accounts"][0]["label"].split("***")[0])
        self.assertEqual(body["accounts"][0]["label"], "b***@e***.com")
        self.assertIs(body["settings"]["mask_labels"], True)

    def test_mask_labels_off_sends_the_full_label(self):
        source = FakeSource(
            [
                AccountRef(
                    account_key=KEY_A, label="b***@e***.com", source_kind="pool", entry_id="e1"
                )
            ]
        )
        source.full_label = lambda key: "bob@example.com" if key == KEY_A else ""  # type: ignore[attr-defined]
        store = FakeStore()
        store.account_upsert(
            {"account_key": KEY_A, "label": "b***@e***.com", "source_kind": "pool"}
        )
        store.credit_upsert(
            {
                "credit_key": CREDIT_1,
                "account_key": KEY_A,
                "backend_state": "available",
                "status": "scheduled",
                "expires_at": 1_800_003_600.0,
                "updated_at": 1_800_000_000.0,
            }
        )
        deps = make_deps(store=store, source=source, settings=make_settings(mask_labels=False))
        body = self._status(Service(deps))
        self.assertEqual(body["accounts"][0]["label"], "bob@example.com")
        self.assertIs(body["settings"]["mask_labels"], False)


class DesktopContractParity(unittest.TestCase):
    """The names the popup reads are the names the engine emits.

    A rename on either side is silent: the popup just renders nothing. These
    checks pin the exact identifiers the popup and the route contract share across the two
    files, so a future rename fails here instead of on a user's screen.
    """

    REPO = Path(__file__).resolve().parents[2]

    def _plugin(self) -> str:
        return (self.REPO / "desktop" / "plugin.js").read_text(encoding="utf-8")

    def test_the_quota_line_reads_the_three_dto_names(self):
        source = self._plugin()
        for name in ("plan_type", "five_hour_remaining_pct", "weekly_remaining_pct"):
            self.assertIn(f"quota.{name}", source, name)
            self.assertIn(name, dto_module.QuotaDTO.FIELDS, name)

    def test_the_footer_reads_the_real_threshold_key(self):
        source = self._plugin()
        self.assertIn("settings.early_redeem_remaining_pct", source)
        self.assertIn("settings.early_redeem_enabled", source)
        self.assertIn("early_redeem_remaining_pct", dto_module.SettingsViewDTO.FIELDS)

    def test_the_empty_state_reads_account_count(self):
        self.assertIn("status?.account_count", self._plugin())
        self.assertIn("account_count", dto_module.StatusDTO.FIELDS)

    def test_the_language_switch_reads_the_status_language(self):
        self.assertIn("status?.language", self._plugin())
        self.assertIn("language", dto_module.StatusDTO.FIELDS)

    def test_the_label_is_rendered_verbatim_from_the_dto(self):
        """D-013: masking is the backend's job, so the UI never re-masks."""
        source = self._plugin()
        self.assertIn("account.label", source)
        self.assertNotIn("maskLabel", source)
        self.assertNotIn("mask_email", source)


if __name__ == "__main__":
    unittest.main()
