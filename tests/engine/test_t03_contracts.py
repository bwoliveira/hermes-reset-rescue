"""T-03: the shared contracts (Clock, Deps, protocols) and the fakes import."""

from __future__ import annotations

import unittest

from reset_rescue.accounts import (
    AccountSource,
    Response,
    account_key_for,
    is_official_base_url,
    mask_email,
    mask_label,
)
from reset_rescue.clock import (
    CliResult,
    Clock,
    SystemClock,
    TransportProtocol,
)
from tests.fakes import (
    FakeCli,
    FakeClock,
    FakeCron,
    FakeNotifier,
    FakePool,
    FakeSource,
    FakeStore,
    FakeTransport,
    FakeWrappers,
    credit_row,
    credits_payload,
    jwt_for,
    make_deps,
    make_settings,
    usage_payload,
)


class ClockContract(unittest.TestCase):
    def test_system_clock_satisfies_the_protocol(self):
        self.assertIsInstance(SystemClock(), Clock)

    def test_fake_clock_is_a_clock_and_sleep_advances(self):
        clock = FakeClock(start=1000.0)
        self.assertIsInstance(clock, Clock)
        self.assertEqual(clock.now(), 1000.0)
        clock.sleep(30)
        self.assertEqual(clock.now(), 1030.0)
        self.assertEqual(clock.sleeps, [30.0])

    def test_deps_now_and_wait_go_through_the_injected_clock(self):
        clock = FakeClock(start=500.0)
        deps = make_deps(clock=clock)
        self.assertEqual(deps.now(), 500.0)
        deps.wait(2)
        self.assertEqual(deps.now(), 502.0)
        self.assertEqual(clock.sleeps, [2.0])


class CliResultContract(unittest.TestCase):
    def test_ok_requires_zero_and_no_timeout_and_no_exec_error(self):
        self.assertTrue(CliResult(returncode=0).ok)
        self.assertFalse(CliResult(returncode=1).ok)
        self.assertFalse(CliResult(returncode=0, timed_out=True).ok)
        self.assertFalse(CliResult(returncode=0, exec_error="spawn_failed").ok)
        self.assertFalse(CliResult(returncode=None).ok)


class ProtocolConformance(unittest.TestCase):
    def test_fake_source_is_an_account_source(self):
        source = FakeSource()
        self.assertIsInstance(source, AccountSource)
        self.assertEqual(source.kind, "pool")

    def test_fake_source_satisfies_the_transport_protocol(self):
        self.assertIsInstance(FakeSource(), TransportProtocol)

    def test_account_source_is_abstract(self):
        with self.assertRaises(TypeError):
            AccountSource()  # type: ignore[abstract]

    def test_response_never_prints_a_body(self):
        response = Response(status=200, body={"secret": "yes"})
        self.assertEqual(repr(response), "Response(status=200)")
        self.assertTrue(response.ok)
        self.assertFalse(Response(status=401).ok)


class FakeTransportContract(unittest.TestCase):
    """T-03 acceptance: logs method and URL, and can fail on an unexpected POST."""

    def test_logs_method_and_url(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(status=200, body={"credits": []}))
        transport(
            "GET",
            "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
            {"Authorization": "Bearer x"},
            None,
            30.0,
        )
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.requests[0]["method"], "GET")
        self.assertIn("/rate-limit-reset-credits", transport.requests[0]["url"])
        self.assertEqual(transport.count("GET", "rate-limit-reset-credits"), 1)

    def test_unexpected_post_is_visible_and_a_test_can_fail_on_it(self):
        def explode(*_args, **_kwargs):
            raise AssertionError("unexpected POST")

        transport = FakeTransport()
        transport.on("POST", "/consume", explode)
        with self.assertRaises(AssertionError):
            transport(
                "POST",
                "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume",
                {},
                b"{}",
                30.0,
            )
        self.assertEqual(len(transport.posts()), 1)

    def test_default_route_answers_when_no_route_matches(self):
        transport = FakeTransport()
        transport.default = Response(status=204)
        self.assertEqual(transport("GET", "https://chatgpt.com/x", {}, None, 5.0).status, 204)


class FakeCliContract(unittest.TestCase):
    def test_records_calls_and_matches_prefixes(self):
        cli = FakeCli()
        cli.on("auth", "refresh", "openai-codex", result=CliResult(returncode=0))
        cli.run(["auth", "refresh", "openai-codex", "entry-1"])
        cli.run(["cron", "list", "--all"])
        self.assertEqual(cli.count("auth", "refresh"), 1)
        self.assertEqual(cli.refreshes(), ["entry-1"])
        self.assertEqual(cli.count("cron", "list"), 1)

    def test_default_result_is_success(self):
        cli = FakeCli()
        self.assertTrue(cli.run(["anything"]).ok)


class FakeCronContract(unittest.TestCase):
    def test_create_list_remove_round_trip(self):
        from reset_rescue.cron_gateway import build_scan_spec

        cron = FakeCron()
        spec = build_scan_spec(interval_hours=12, deliver="local")
        job = cron.create(spec)
        self.assertIsNotNone(job)
        self.assertEqual(cron.names(), ["reset-rescue-scan"])
        self.assertEqual(len(cron.list_jobs()), 1)
        self.assertTrue(cron.remove(job.job_id, owned={job.job_id}))
        self.assertEqual(cron.list_jobs(), [])

    def test_failed_create_returns_none_and_records_the_attempt(self):
        from reset_rescue.cron_gateway import build_scan_spec

        cron = FakeCron()
        cron.fail_create = True
        self.assertIsNone(cron.create(build_scan_spec(interval_hours=12, deliver="local")))
        self.assertEqual(len(cron.created), 1)


class FakePoolContract(unittest.TestCase):
    def test_read_returns_copies_and_write_raises(self):
        pool = FakePool([{"id": "e1", "access_token": "t"}])
        rows = pool.read()
        rows[0]["access_token"] = "changed"
        self.assertEqual(pool.rows[0]["access_token"], "t")
        with self.assertRaises(AssertionError):
            pool.write()

    def test_set_token_updates_the_row(self):
        pool = FakePool([{"id": "e1", "access_token": "old"}])
        pool.set_token("e1", "new")
        self.assertEqual(pool.rows[0]["access_token"], "new")


class PayloadBuilders(unittest.TestCase):
    def test_credit_row_defaults_are_available_and_soon(self):
        row = credit_row("c1", expires_in=600)
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["id"], "c1")

    def test_credits_payload_counts_available_and_redeeming(self):
        payload = credits_payload(
            credit_row("a"), credit_row("b", status="redeeming"), credit_row("c", status="redeemed")
        )
        self.assertEqual(payload["available_count"], 2)
        self.assertEqual(len(payload["credits"]), 3)

    def test_usage_payload_uses_the_window_seconds_hermes_reads(self):
        payload = usage_payload(weekly=42.0, session=7.5)
        self.assertEqual(payload["rate_limit"]["primary_window"]["limit_window_seconds"], 604800)
        self.assertEqual(payload["rate_limit"]["secondary_window"]["limit_window_seconds"], 18000)

    def test_jwt_carries_account_id_and_email(self):
        from reset_rescue.accounts import chatgpt_account_id, jwt_claims

        token = jwt_for("acct-9", "someone@example.com", residency="eu")
        self.assertEqual(chatgpt_account_id(token), "acct-9")
        self.assertEqual(jwt_claims(token)["email"], "someone@example.com")
        self.assertEqual(
            jwt_claims(token)["https://api.openai.com/auth"]["chatgpt_data_residency"], "eu"
        )


class MaskingAndKeys(unittest.TestCase):
    def test_account_key_is_stable_and_salted(self):
        first = account_key_for("salt-a", "acct-1")
        second = account_key_for("salt-a", "acct-1")
        other_salt = account_key_for("salt-b", "acct-1")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other_salt)
        self.assertEqual(len(first), 16)
        self.assertNotIn("acct-1", first)

    def test_email_masking_keeps_only_the_first_letter(self):
        self.assertEqual(mask_email("bob@example.com"), "b***@e***.com")
        self.assertEqual(mask_email("not-an-email"), "")
        self.assertEqual(mask_email(""), "")

    def test_label_masking_never_returns_a_raw_email(self):
        self.assertEqual(mask_label("bob@example.com"), "b***@e***.com")
        self.assertNotIn("@", mask_label("some-long-label-value"))
        self.assertEqual(mask_label("", fallback_key="abcd1234"), "Codex account abcd")
        self.assertEqual(mask_label("b***@e***.com"), "b***@e***.com")

    def test_only_the_official_origin_is_accepted(self):
        self.assertTrue(is_official_base_url(""))
        self.assertTrue(is_official_base_url("https://chatgpt.com"))
        self.assertTrue(is_official_base_url("https://chatgpt.com/backend-api/codex"))
        self.assertFalse(is_official_base_url("https://gateway.example.com/v1"))
        self.assertFalse(is_official_base_url("http://chatgpt.com"))
        self.assertFalse(is_official_base_url("https://chatgpt.com.evil.test"))


class DepsWiring(unittest.TestCase):
    def test_make_deps_wires_every_collaborator(self):
        deps = make_deps()
        for name in (
            "clock",
            "source",
            "store",
            "cli",
            "cron",
            "wrappers",
            "notifier",
            "settings",
            "paths",
        ):
            self.assertIsNotNone(getattr(deps, name), name)
        self.assertTrue(deps.salt)
        self.assertEqual(deps.settings.scan_interval_hours, 12)

    def test_make_deps_with_pool_rows_builds_a_pool_backed_source(self):
        from reset_rescue.accounts import PoolAccountSource

        deps = make_deps(pool_rows=[{"id": "e1", "access_token": jwt_for("acct-1")}])
        refs = deps.source.accounts()
        self.assertEqual(len(refs), 1)
        self.assertIsInstance(deps.source._pool_source, PoolAccountSource)

    def test_settings_defaults_and_overrides(self):
        self.assertEqual(make_settings().lead_minutes, 10)
        self.assertEqual(make_settings(lead_minutes=30).lead_minutes, 30)


class StoreAndNotifierFakes(unittest.TestCase):
    def test_store_meta_and_history(self):
        store = FakeStore()
        self.assertIsNone(store.meta_get("missing"))
        store.meta_set("k", "v")
        self.assertEqual(store.meta_get("k"), "v")
        store.history_add("scan", outcome="ok", credit_key="c" * 16)
        self.assertEqual(store.history_tail()[0]["action"], "scan")

    def test_notifier_enqueues_then_flushes(self):
        notifier = FakeNotifier()
        notifier.enqueue("success", {"credit": "x"}, credit_key="c" * 16, dedupe_key="d")
        self.assertEqual(notifier.kinds(), ["success"])
        delivered = notifier.flush()
        self.assertEqual(len(delivered), 1)
        self.assertTrue(delivered[0]["delivered"])
        self.assertEqual(notifier.enqueued, [])

    def test_wrappers_install_and_remove_owned(self):
        wrappers = FakeWrappers()
        states = wrappers.install()
        self.assertEqual(states["reset_rescue_scan.py"], "ok")
        removed = wrappers.remove_owned()
        self.assertIn("reset_rescue_fire.py", removed)
        self.assertEqual(wrappers.states, {})


if __name__ == "__main__":
    unittest.main()
