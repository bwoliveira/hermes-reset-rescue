"""T-06: `codex_api` over `AccountSource` — endpoints, read-only transport, retry."""

from __future__ import annotations

import unittest

from reset_rescue.accounts import (
    AccountRef,
    Response,
    SourceUnavailable,
    account_key_for,
)
from reset_rescue.classify import AVAILABLE, PENDING, REDEEMED
from reset_rescue.codex_api import (
    ALLOWED_URLS,
    CONSUME_URL,
    CREDITS_URL,
    USAGE_URL,
    CodexResetApi,
    ConsumeResult,
    MutationRefused,
    ReadOnlyTransport,
    UsageSummary,
    parse_consume,
    parse_usage,
)
from tests.fakes import FakeSource, credit_row, credits_payload, usage_payload

SALT = "a" * 64


def make_account(key_seed: str = "acct-1") -> AccountRef:
    return AccountRef(
        account_key=account_key_for(SALT, key_seed),
        label="b***@e***.com",
        source_kind="pool",
        entry_id="entry-1",
    )


class EndpointSurface(unittest.TestCase):
    def test_only_the_three_documented_urls_exist(self):
        self.assertEqual(
            ALLOWED_URLS,
            {
                "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
                "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume",
                "https://chatgpt.com/backend-api/wham/usage",
            },
        )
        self.assertEqual(
            CREDITS_URL, "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
        )
        self.assertEqual(CONSUME_URL, CREDITS_URL + "/consume")
        self.assertEqual(USAGE_URL, "https://chatgpt.com/backend-api/wham/usage")

    def test_list_get_then_usage_get_then_consume_post_are_the_only_calls(self):
        source = FakeSource([make_account()])
        source.on(
            "GET", "/rate-limit-reset-credits", Response(200, credits_payload(credit_row("c1")))
        )
        source.on("GET", "/usage", Response(200, usage_payload(weekly=10.0)))
        source.on("POST", "/consume", Response(200, {"code": "reset", "windows_reset": 2}))
        api = CodexResetApi(source, sleep=lambda _s: None)
        account = source.accounts()[0]

        api.list_credits(account)
        api.usage(account)
        api.consume(account, "c1", "req-uuid")

        for request in source.requests:
            self.assertIn(request["url"], ALLOWED_URLS)
        self.assertEqual([r["method"] for r in source.requests], ["GET", "GET", "POST"])

    def test_an_unknown_url_is_refused_before_it_reaches_the_source(self):
        source = FakeSource([make_account()])
        api = CodexResetApi(source, sleep=lambda _s: None)
        with self.assertRaises(SourceUnavailable) as ctx:
            api._request(source.accounts()[0], "GET", "https://chatgpt.com/backend-api/wham/other")
        self.assertEqual(ctx.exception.code, "unsupported_endpoint")
        self.assertEqual(source.requests, [])


class ListCredits(unittest.TestCase):
    def test_parses_the_list_and_drops_the_raw_body(self):
        source = FakeSource([make_account()])
        source.on(
            "GET",
            "/rate-limit-reset-credits",
            Response(
                200,
                {
                    "credits": [
                        {
                            "id": "c1",
                            "status": "available",
                            "expires_at": 1_800_000_600,
                            "profile_user_id": "user-1",
                            "description": "internal",
                        },
                        {"id": "c2", "status": "redeeming"},
                    ],
                    "available_count": 2,
                    "history_enabled": True,
                },
            ),
        )
        api = CodexResetApi(source, sleep=lambda _s: None)
        parsed = api.list_credits(source.accounts()[0])
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed.credits[0].classification, AVAILABLE)
        self.assertEqual(parsed.credits[1].classification, PENDING)
        self.assertFalse(hasattr(parsed.credits[0], "profile_user_id"))

    def test_a_non_200_raises_a_fixed_code(self):
        for status, code in (
            (401, "auth_expired"),
            (403, "forbidden"),
            (404, "not_found"),
            (400, "http_error"),
        ):
            source = FakeSource([make_account()])
            source.on("GET", "/rate-limit-reset-credits", Response(status))
            api = CodexResetApi(source, sleep=lambda _s: None)
            with self.assertRaises(SourceUnavailable) as ctx:
                api.list_credits(source.accounts()[0])
            self.assertEqual(ctx.exception.code, code, status)

    def test_a_200_with_a_malformed_body_is_an_empty_list(self):
        source = FakeSource([make_account()])
        source.on("GET", "/rate-limit-reset-credits", Response(200, {"unexpected": True}))
        api = CodexResetApi(source, sleep=lambda _s: None)
        self.assertEqual(len(api.list_credits(source.accounts()[0])), 0)


class TransientRetry(unittest.TestCase):
    def test_a_5xx_gets_exactly_one_retry_then_succeeds(self):
        source = FakeSource([make_account()])
        calls = {"n": 0}

        def handler(*_args):
            calls["n"] += 1
            if calls["n"] == 1:
                return Response(503)
            return Response(200, credits_payload(credit_row("c1")))

        source.on("GET", "/rate-limit-reset-credits", handler)
        slept: list[float] = []
        api = CodexResetApi(source, sleep=slept.append)
        parsed = api.list_credits(source.accounts()[0])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(slept, [2.0])

    def test_two_5xx_do_not_retry_a_third_time(self):
        source = FakeSource([make_account()])
        source.on("GET", "/rate-limit-reset-credits", Response(503))
        slept: list[float] = []
        api = CodexResetApi(source, sleep=slept.append)
        with self.assertRaises(SourceUnavailable) as ctx:
            api.list_credits(source.accounts()[0])
        self.assertEqual(ctx.exception.code, "transient")
        self.assertEqual(source.count("GET", "rate-limit-reset-credits"), 2)
        self.assertEqual(len(slept), 1)

    def test_a_4xx_is_not_retried(self):
        source = FakeSource([make_account()])
        source.on("GET", "/rate-limit-reset-credits", Response(400))
        api = CodexResetApi(source, sleep=lambda _s: None)
        with self.assertRaises(SourceUnavailable):
            api.list_credits(source.accounts()[0])
        self.assertEqual(source.count("GET", "rate-limit-reset-credits"), 1)

    def test_a_401_is_not_retried_here(self):
        # The source owns the refresh (A§4.3); a 401 must not be retried by the API.
        source = FakeSource([make_account()])
        source.on("GET", "/rate-limit-reset-credits", Response(401))
        api = CodexResetApi(source, sleep=lambda _s: None)
        with self.assertRaises(SourceUnavailable) as ctx:
            api.list_credits(source.accounts()[0])
        self.assertEqual(ctx.exception.code, "auth_expired")
        self.assertEqual(source.count("GET", "rate-limit-reset-credits"), 1)

    def test_a_timeout_exception_is_mapped_to_a_fixed_code(self):
        source = FakeSource([make_account()])
        source.on("GET", "/rate-limit-reset-credits", TimeoutError())
        api = CodexResetApi(source, sleep=lambda _s: None)
        with self.assertRaises(SourceUnavailable) as ctx:
            api.list_credits(source.accounts()[0])
        self.assertEqual(ctx.exception.code, "timeout")

    def test_a_connection_error_is_mapped_to_network(self):
        source = FakeSource([make_account()])
        source.on("GET", "/rate-limit-reset-credits", ConnectionResetError())
        api = CodexResetApi(source, sleep=lambda _s: None)
        with self.assertRaises(SourceUnavailable) as ctx:
            api.list_credits(source.accounts()[0])
        self.assertEqual(ctx.exception.code, "network")


class ConsumeParsing(unittest.TestCase):
    def test_the_documented_success_shape(self):
        result = parse_consume(Response(200, {"code": "reset", "windows_reset": 2}))
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "reset")
        self.assertEqual(result.windows_reset, 2)
        self.assertTrue(result.reset)

    def test_windows_reset_without_a_code_is_a_reset(self):
        result = parse_consume(Response(200, {"windows_reset": 1}))
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "reset")

    def test_already_redeemed_and_nothing_to_reset_are_recognized(self):
        self.assertEqual(
            parse_consume(Response(200, {"code": "already_redeemed"})).reason, "already_redeemed"
        )
        nothing = parse_consume(Response(200, {"code": "nothing_to_reset"}))
        self.assertEqual(nothing.reason, "nothing_to_reset")
        self.assertFalse(nothing.ok)

    def test_409_is_already_redeemed_and_403_is_ineligible(self):
        self.assertEqual(parse_consume(Response(409)).reason, "already_redeemed")
        self.assertTrue(parse_consume(Response(409)).ok)
        self.assertEqual(parse_consume(Response(403)).reason, "rate_limit_reset_ineligible")
        self.assertEqual(parse_consume(Response(404)).reason, "no_credit")

    def test_an_empty_200_is_unknown_never_success(self):
        result = parse_consume(Response(200, {}))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "unknown_response")

    def test_an_unrecognized_body_is_unknown_and_keeps_no_content(self):
        result = parse_consume(Response(200, {"detail": "some internal text", "x": 1}))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "unknown_response")
        self.assertNotIn("internal", repr(result))

    def test_a_5xx_is_http_error(self):
        self.assertEqual(parse_consume(Response(500)).reason, "http_error")

    def test_windows_reset_must_be_an_int(self):
        self.assertEqual(
            parse_consume(Response(200, {"code": "reset", "windows_reset": "2"})).windows_reset, 0
        )
        self.assertEqual(
            parse_consume(Response(200, {"code": "reset", "windows_reset": True})).windows_reset, 0
        )


class ConsumeCall(unittest.TestCase):
    def test_the_body_carries_the_uuid_and_the_credit_id_only(self):
        source = FakeSource([make_account()])
        source.on("POST", "/consume", Response(200, {"code": "reset", "windows_reset": 1}))
        api = CodexResetApi(source, sleep=lambda _s: None)
        api.consume(source.accounts()[0], "credit-abc", "uuid-123")
        post = source.posts()[0]
        self.assertEqual(post["json"], {"redeem_request_id": "uuid-123", "credit_id": "credit-abc"})
        self.assertEqual(post["url"], CONSUME_URL)

    def test_a_post_is_never_retried_by_the_api(self):
        source = FakeSource([make_account()])
        source.on("POST", "/consume", Response(503))
        api = CodexResetApi(source, sleep=lambda _s: None)
        result = api.consume(source.accounts()[0], "c1", "uuid")
        self.assertEqual(result.reason, "http_error")
        self.assertEqual(source.count("POST", "/consume"), 1)

    def test_missing_arguments_are_a_programming_error(self):
        source = FakeSource([make_account()])
        api = CodexResetApi(source, sleep=lambda _s: None)
        account = source.accounts()[0]
        with self.assertRaises(ValueError):
            api.consume(account, "", "uuid")
        with self.assertRaises(ValueError):
            api.consume(account, "c1", "")
        self.assertEqual(source.posts(), [])


class UsageParsing(unittest.TestCase):
    def test_weekly_and_five_hour_windows_are_read_by_seconds(self):
        summary = parse_usage(usage_payload(weekly=42.5, session=7.0))
        self.assertEqual(summary.weekly_pct, 42.5)
        self.assertEqual(summary.five_hour_pct, 7.0)
        self.assertTrue(summary.known)

    def test_windows_are_matched_by_seconds_not_by_position(self):
        payload = {
            "rate_limit": {
                "primary_window": {"limit_window_seconds": 18000, "used_percent": 11.0},
                "secondary_window": {"limit_window_seconds": 604800, "used_percent": 22.0},
            }
        }
        summary = parse_usage(payload)
        self.assertEqual(summary.five_hour_pct, 11.0)
        self.assertEqual(summary.weekly_pct, 22.0)

    def test_an_unknown_window_is_ignored(self):
        summary = parse_usage(
            {"rate_limit": {"primary_window": {"limit_window_seconds": 3600, "used_percent": 5.0}}}
        )
        self.assertFalse(summary.known)

    def test_a_malformed_payload_is_an_empty_summary(self):
        for payload in (
            None,
            {},
            {"rate_limit": None},
            {"rate_limit": []},
            {"rate_limit": {"primary_window": "x"}},
            {
                "rate_limit": {
                    "primary_window": {"limit_window_seconds": "604800", "used_percent": 5}
                }
            },
        ):
            self.assertFalse(parse_usage(payload).known, payload)

    def test_a_bool_percentage_is_rejected(self):
        self.assertFalse(
            parse_usage(
                {
                    "rate_limit": {
                        "primary_window": {"limit_window_seconds": 604800, "used_percent": True}
                    }
                }
            ).known
        )

    def test_as_dict_shape(self):
        self.assertEqual(
            UsageSummary(weekly_pct=1.0, five_hour_pct=None).as_dict(),
            {"weekly_pct": 1.0, "five_hour_pct": None},
        )

    def test_usage_never_raises_on_a_transport_failure(self):
        source = FakeSource([make_account()])
        source.on("GET", "/usage", SourceUnavailable("network"))
        api = CodexResetApi(source, sleep=lambda _s: None)
        self.assertFalse(api.usage(source.accounts()[0]).known)

    def test_usage_on_a_non_200_is_an_empty_summary(self):
        source = FakeSource([make_account()])
        source.on("GET", "/usage", Response(500))
        api = CodexResetApi(source, sleep=lambda _s: None)
        self.assertFalse(api.usage(source.accounts()[0]).known)


class ReadOnlyTransportContract(unittest.TestCase):
    def test_a_get_passes_through(self):
        source = FakeSource([make_account()])
        source.on(
            "GET", "/rate-limit-reset-credits", Response(200, credits_payload(credit_row("c1")))
        )
        transport = ReadOnlyTransport(source)
        api = CodexResetApi(transport, sleep=lambda _s: None)
        self.assertEqual(len(api.list_credits(source.accounts()[0])), 1)

    def test_any_non_get_raises_mutation_refused_before_reaching_the_source(self):
        source = FakeSource([make_account()])
        transport = ReadOnlyTransport(source)
        api = CodexResetApi(transport, sleep=lambda _s: None)
        with self.assertRaises(MutationRefused):
            api.consume(source.accounts()[0], "c1", "uuid")
        self.assertEqual(source.posts(), [])
        self.assertEqual(transport.attempts, [("POST", CONSUME_URL)])

    def test_put_and_delete_are_refused_too(self):
        source = FakeSource([make_account()])
        transport = ReadOnlyTransport(source)
        for method in ("PUT", "DELETE", "PATCH", "post"):
            with self.assertRaises(MutationRefused):
                transport.request(source.accounts()[0], method, CREDITS_URL)

    def test_the_refusal_never_echoes_a_body(self):
        source = FakeSource([make_account()])
        transport = ReadOnlyTransport(source)
        with self.assertRaises(MutationRefused) as ctx:
            transport.request(
                source.accounts()[0], "POST", CONSUME_URL, json={"credit_id": "raw-credit-id"}
            )
        self.assertNotIn("raw-credit-id", str(ctx.exception))

    def test_accounts_and_coverage_pass_through(self):
        source = FakeSource([make_account()])
        transport = ReadOnlyTransport(source)
        self.assertEqual(len(transport.accounts()), 1)
        self.assertEqual(transport.coverage_note(), "coverage.pool")


class NoRawBodyIsKept(unittest.TestCase):
    def test_parsed_dataclasses_hold_only_documented_fields(self):
        credit = __import__("reset_rescue.classify", fromlist=["parse_credit"]).parse_credit(
            {
                "id": "c1",
                "status": "available",
                "expires_at": 1_800_000_000,
                "reset_type": "codex_rate_limits",
                "title": "Full reset",
                "granted_at": 1_799_000_000,
                "redeem_started_at": None,
                "redeemed_at": None,
                "description": "internal",
                "profile_user_id": "u",
            }
        )
        self.assertEqual(
            set(vars(credit)),
            {
                "id",
                "status",
                "expires_at",
                "granted_at",
                "redeem_started_at",
                "redeemed_at",
                "reset_type",
                "title",
            },
        )

    def test_a_consume_result_holds_no_body(self):
        result = ConsumeResult(ok=True, reason="reset", windows_reset=2, status=200)
        self.assertEqual(set(vars(result)), {"ok", "reason", "windows_reset", "status"})

    def test_statuses_of_parsed_credits_are_classified(self):
        classify = __import__("reset_rescue.classify", fromlist=["parse_credit_list"])
        parsed = classify.parse_credit_list(
            credits_payload(credit_row("a"), credit_row("b", status="redeemed"))
        )
        self.assertEqual([c.classification for c in parsed], [AVAILABLE, REDEEMED])


if __name__ == "__main__":
    unittest.main()
