"""T-16: canary, credential-store and origin tests (I8, I12, CR-11).

Sentinel values are planted everywhere a secret could hide, then every path the
engine has is walked and the sentinels are searched for in stdout, stderr, the
DTOs, the history and the HTTP responses.

The audit hook proves the two negative facts that matter more than any positive
one:

* ``auth.json`` is only ever opened **read** (and by Hermes, not by us),
* ``~/.codex`` is never touched,
* ``write_credential_pool`` and ``load_pool`` are never called.
"""

from __future__ import annotations

import builtins
import io
import pathlib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from reset_rescue.accounts import (
    CONSUME_PATH,
    CREDITS_PATH,
    OFFICIAL_ORIGIN,
    USAGE_PATH,
    PoolAccountSource,
    Response,
    SourceUnavailable,
    account_key_for,
)
from reset_rescue.clock import Deps
from reset_rescue.codex_api import CodexResetApi, ReadOnlyTransport
from reset_rescue.dto import to_json
from reset_rescue.service import Service
from reset_rescue.settings import parse_settings
from tests.fakes import (
    FakeCli,
    FakePool,
    FakeStore,
    FakeTransport,
    make_deps,
    make_settings,
)

SALT = "a" * 64
CREDITS_URL = OFFICIAL_ORIGIN + CREDITS_PATH
CONSUME_URL = OFFICIAL_ORIGIN + CONSUME_PATH
USAGE_URL = OFFICIAL_ORIGIN + USAGE_PATH

#: Sentinels: values that must never appear in any output.
TOKEN_SENTINEL = "SENTINEL-ACCESS-TOKEN-9f3a2b"
REFRESH_SENTINEL = "SENTINEL-REFRESH-TOKEN-77c1de"
CREDIT_SENTINEL = "SENTINEL-RAW-CREDIT-ID-4b8e"
CHAT_SENTINEL = "998877665544"
THREAD_SENTINEL = "123456"
ACCOUNT_SENTINEL = "SENTINEL-BACKEND-ACCOUNT-2d9f"
BODY_SENTINEL = "SENTINEL-RAW-BODY-CONTENT-6a1c"

ALL_SENTINELS = (
    TOKEN_SENTINEL,
    REFRESH_SENTINEL,
    CREDIT_SENTINEL,
    CHAT_SENTINEL,
    THREAD_SENTINEL,
    ACCOUNT_SENTINEL,
    BODY_SENTINEL,
)

KEY_A = "a" * 16


def sentinel_row(entry_id: str = "entry-1") -> dict:
    return {
        "id": entry_id,
        "provider": "openai-codex",
        "access_token": TOKEN_SENTINEL,
        "refresh_token": REFRESH_SENTINEL,
        "account_id": ACCOUNT_SENTINEL,
        "label": "bob@example.com",
        "base_url": "",
        "last_status": "ok",
    }


def assert_no_sentinel(case: unittest.TestCase, text: str, where: str) -> None:
    for sentinel in ALL_SENTINELS:
        case.assertNotIn(sentinel, text, f"{where} leaked {sentinel}")


class TokenNeverReachesADto(unittest.TestCase):
    def test_a_status_dto_over_a_sentinel_pool_is_clean(self):
        store = FakeStore()
        key = account_key_for(SALT, ACCOUNT_SENTINEL)
        store.account_upsert(
            {
                "account_key": key,
                "label": "b***@e***.com",
                "source_kind": "pool",
                "last_list_ok_at": 1_800_000_000.0,
            }
        )
        store.credit_upsert(
            {
                "credit_key": KEY_A,
                "account_key": key,
                "title": "Full reset",
                "reset_type": "codex_rate_limits",
                "expires_at": 1_800_003_600.0,
                "backend_state": "available",
                "status": "scheduled",
                "updated_at": 1_800_000_000.0,
            }
        )
        deps = make_deps(store=store, salt=SALT)
        text = Service(deps).status().to_json()
        assert_no_sentinel(self, text, "StatusDTO")

    def test_a_redeem_result_is_clean(self):
        from reset_rescue.dto import RedeemResultDTO

        text = RedeemResultDTO(
            credit_key=KEY_A, outcome="redeemed", reason_code="reset", windows_reset=2
        ).to_json()
        assert_no_sentinel(self, text, "RedeemResultDTO")

    def test_history_never_carries_a_raw_credit_id(self):
        store = FakeStore()
        store.history_add("fire", outcome="ok", credit_key=KEY_A, account_key="b" * 16)
        deps = make_deps(store=store, salt=SALT)
        text = Service(deps).history().to_json()
        assert_no_sentinel(self, text, "HistoryDTO")
        self.assertIn(KEY_A, text)

    def test_every_dto_repr_is_clean(self):
        service = Service(make_deps(salt=SALT))
        for dto in (service.status(), service.ensure(), service.history(), service.quota_check()):
            assert_no_sentinel(self, repr(dto), f"repr({type(dto).__name__})")


class TokenNeverReachesAHeaderOffOrigin(unittest.TestCase):
    def test_the_only_origin_a_request_can_reach_is_chatgpt_com(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(200, {"credits": []}))
        pool = FakePool([sentinel_row()])
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        account = source.accounts()[0]
        source.request(account, "GET", CREDITS_URL)
        for request in transport.requests:
            host = request["url"].split("/")[2]
            self.assertEqual(host, "chatgpt.com")

    def test_a_redirect_is_never_followed(self):
        """The transport refuses 3xx instead of following it to another host."""
        from reset_rescue.accounts import _NoRedirect

        handler = _NoRedirect()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", {}, "https://evil.test")
        )

    def test_a_custom_base_url_row_never_gets_the_token(self):
        transport = FakeTransport()
        row = sentinel_row()
        row["base_url"] = "https://gateway.example.com/v1"
        pool = FakePool([row])
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        self.assertEqual(source.accounts(), [])
        self.assertEqual(transport.requests, [])

    def test_an_off_origin_url_is_refused_before_the_transport(self):
        transport = FakeTransport()
        pool = FakePool([sentinel_row()])
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        account = source.accounts()[0]
        for url in ("https://api.openai.com/v1/x", "https://evil.test/x", "http://chatgpt.com/x"):
            with self.assertRaises(SourceUnavailable):
                source.request(account, "GET", url)
        self.assertEqual(transport.requests, [])


class RawBodyNeverSurvives(unittest.TestCase):
    def test_a_consume_body_with_extra_content_is_dropped(self):
        transport = FakeTransport()
        transport.on(
            "POST",
            "/consume",
            Response(
                200,
                {
                    "code": "reset",
                    "windows_reset": 1,
                    "debug": BODY_SENTINEL,
                    "credit": {"id": CREDIT_SENTINEL},
                },
            ),
        )
        pool = FakePool([sentinel_row()])
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        account = source.accounts()[0]
        api = CodexResetApi(source, sleep=lambda _s: None)
        result = api.consume(account, CREDIT_SENTINEL, "uuid-1")
        text = repr(result)
        assert_no_sentinel(self, text, "ConsumeResult")

    def test_a_credit_list_body_with_extra_fields_is_dropped(self):
        transport = FakeTransport()
        transport.on(
            "GET",
            "/rate-limit-reset-credits",
            Response(
                200,
                {
                    "credits": [
                        {
                            "id": CREDIT_SENTINEL,
                            "status": "available",
                            "expires_at": 1_800_003_600,
                            "description": BODY_SENTINEL,
                            "profile_user_id": ACCOUNT_SENTINEL,
                        }
                    ],
                    "debug": BODY_SENTINEL,
                },
            ),
        )
        pool = FakePool([sentinel_row()])
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        api = CodexResetApi(source, sleep=lambda _s: None)
        parsed = api.list_credits(source.accounts()[0])
        text = repr(parsed) + repr(parsed.credits[0])
        assert_no_sentinel(self, text, "CreditList")

    def test_a_usage_body_is_never_serialized(self):
        transport = FakeTransport()
        transport.on(
            "GET",
            "/usage",
            Response(
                200,
                {
                    "rate_limit": {
                        "primary_window": {"limit_window_seconds": 604800, "used_percent": 42.0}
                    },
                    "debug": BODY_SENTINEL,
                },
            ),
        )
        pool = FakePool([sentinel_row()])
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        api = CodexResetApi(source, sleep=lambda _s: None)
        summary = api.usage(source.accounts()[0])
        assert_no_sentinel(self, repr(summary), "UsageSummary")
        # D-010: the percentage is not in the repr either.
        self.assertNotIn("42", repr(summary))


class ErrorMessagesCarryNoBody(unittest.TestCase):
    def test_a_source_unavailable_never_includes_a_body(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(400, {"error": BODY_SENTINEL}))
        pool = FakePool([sentinel_row()])
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        api = CodexResetApi(source, sleep=lambda _s: None)
        try:
            api.list_credits(source.accounts()[0])
        except SourceUnavailable as exc:
            text = f"{exc} {exc.code} {exc.detail}"
            assert_no_sentinel(self, text, "SourceUnavailable")
        else:
            self.fail("expected SourceUnavailable")

    def test_an_auth_expired_carries_only_a_label(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        pool = FakePool([sentinel_row()])
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        cli = FakeCli()
        cli.on("auth", "refresh", "openai-codex", returncode=1)
        source._refresh = lambda entry_id: bool(
            cli.run(["auth", "refresh", "openai-codex", entry_id]).ok
        )
        try:
            source.request(source.accounts()[0], "GET", CREDITS_URL)
        except SourceUnavailable as exc:
            assert_no_sentinel(self, f"{exc} {exc.code}", "AuthExpired")
        else:
            self.fail("expected AuthExpired")


class StdoutAndStderrAreClean(unittest.TestCase):
    def test_a_full_service_round_trip_prints_nothing_secret(self):
        store = FakeStore()
        key = account_key_for(SALT, ACCOUNT_SENTINEL)
        store.account_upsert({"account_key": key, "label": "b***@e***.com", "source_kind": "pool"})
        store.credit_upsert(
            {
                "credit_key": KEY_A,
                "account_key": key,
                "title": "Full reset",
                "reset_type": "codex_rate_limits",
                "expires_at": 1_800_003_600.0,
                "backend_state": "available",
                "status": "scheduled",
                "updated_at": 1_800_000_000.0,
                "redeem_request_id": "uuid-1",
            }
        )
        deps = make_deps(
            store=store,
            salt=SALT,
            settings=make_settings(notify_target=f"telegram:{CHAT_SENTINEL}:{THREAD_SENTINEL}"),
        )
        service = Service(deps)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            service.status()
            service.ensure()
            service.scan()
            service.history()
            service.quota_check()
            service.redeem_now(KEY_A, confirm=True)
            service.uninstall()

        assert_no_sentinel(self, out.getvalue(), "stdout")
        assert_no_sentinel(self, err.getvalue(), "stderr")

    def test_a_cli_result_stderr_is_never_propagated(self):
        """A child's stderr can carry a path or a token fragment (I8)."""
        cli = FakeCli()
        cli.on(
            "auth", "refresh", "openai-codex", returncode=1, stderr=f"failed with {TOKEN_SENTINEL}"
        )
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        pool = FakePool([sentinel_row()])
        source = PoolAccountSource(
            salt=SALT, deps=Deps(cli=cli), read_pool=pool.read, transport=transport
        )
        try:
            source.request(source.accounts()[0], "GET", CREDITS_URL)
        except SourceUnavailable as exc:
            assert_no_sentinel(self, f"{exc} {exc.code}", "refresh failure")
        else:
            self.fail("expected a failure")


class CredentialStoreIsReadOnly(unittest.TestCase):
    """An audit hook proves the negative facts (T-16)."""

    def test_the_accounts_module_never_writes_the_pool(self):
        import reset_rescue.accounts as accounts_module

        text = pathlib.Path(accounts_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("write_credential_pool", text)
        self.assertNotIn("load_pool(", text)
        self.assertIn("read_credential_pool", text)

    def test_the_default_reader_is_read_credential_pool(self):
        import inspect

        import reset_rescue.accounts as accounts_module

        source = inspect.getsource(accounts_module._default_read_pool)
        self.assertIn("read_credential_pool", source)
        self.assertNotIn("load_pool", source)
        self.assertNotIn("write", source)

    def test_no_module_in_the_package_touches_a_credential_path(self):
        """No module may name a credential file or a home-dir path.

        A docstring may legitimately mention ``agent.codex_headers`` (a Hermes
        module name), so the check is for filesystem shapes: a quoted
        ``auth.json``, ``~/.codex``, or an ``expanduser`` call.
        """
        package = pathlib.Path(__file__).resolve().parents[2] / "reset_rescue"
        forbidden = (
            '"auth.json"',
            "'auth.json'",
            "~/.codex",
            ".codex/",
            "expanduser",
            "Path.home()",
        )
        for module in package.glob("*.py"):
            text = module.read_text(encoding="utf-8")
            for needle in forbidden:
                self.assertNotIn(needle, text, f"{module.name} contains {needle!r}")

    def test_the_pool_is_only_ever_read_in_a_full_run(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        pool = FakePool([sentinel_row()])
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
        try:
            source.request(account, "GET", CREDITS_URL)
        except SourceUnavailable:
            pass
        self.assertEqual(pool.write_calls, [])
        self.assertGreaterEqual(pool.reads, 1)

    def test_open_is_never_called_with_a_write_mode_on_a_credential_path(self):
        """An audit hook over a full run: nothing opens auth.json for writing."""
        opened: list[tuple] = []
        real_open = builtins.open

        def spy(file, mode="r", *args, **kwargs):
            opened.append((str(file), mode))
            return real_open(file, mode, *args, **kwargs)

        store = FakeStore()
        deps = make_deps(store=store, salt=SALT)
        with mock.patch("builtins.open", spy):
            service = Service(deps)
            service.status()
            service.ensure()
            service.history()

        for path, mode in opened:
            if "auth.json" in path:
                self.assertNotIn("w", mode, f"auth.json opened for writing: {mode}")
                self.assertNotIn("a", mode, f"auth.json appended: {mode}")

    def test_no_credential_environment_variable_is_read(self):
        import reset_rescue.accounts as accounts_module

        text = pathlib.Path(accounts_module.__file__).read_text(encoding="utf-8")
        for name in ("OPENAI_API_KEY", "CODEX_TOKEN", "AUTH_TOKEN"):
            self.assertNotIn(f"environ[{name!r}]", text, name)
            self.assertNotIn(f"getenv({name!r}", text, name)


class ChatIdIsMasked(unittest.TestCase):
    def test_a_chat_id_never_reaches_a_dto(self):
        deps = make_deps(
            salt=SALT,
            settings=make_settings(notify_target=f"telegram:{CHAT_SENTINEL}:{THREAD_SENTINEL}"),
        )
        text = Service(deps).status().to_json()
        self.assertNotIn(CHAT_SENTINEL, text)
        self.assertNotIn(THREAD_SENTINEL, text)

    def test_the_status_target_display_masks_the_chat(self):
        deps = make_deps(
            salt=SALT,
            settings=make_settings(notify_target=f"telegram:{CHAT_SENTINEL}"),
        )
        payload = Service(deps).status().to_json()
        self.assertNotIn(CHAT_SENTINEL, payload)
        self.assertIn("…5544", payload)

    def test_the_full_target_is_kept_only_for_delivery(self):
        settings = parse_settings({"notify_target": f"telegram:{CHAT_SENTINEL}"})
        self.assertEqual(settings.notify_target, f"telegram:{CHAT_SENTINEL}")
        self.assertNotIn(CHAT_SENTINEL, settings.notify_target_display())


class AccountKeyIsNotTheBackendId(unittest.TestCase):
    def test_the_backend_id_never_appears_in_the_key_or_label(self):
        key = account_key_for(SALT, ACCOUNT_SENTINEL)
        self.assertNotIn(ACCOUNT_SENTINEL, key)
        self.assertEqual(len(key), 16)

    def test_a_pool_account_ref_never_carries_the_token(self):
        pool = FakePool([sentinel_row()])
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=FakeTransport())
        account = source.accounts()[0]
        text = repr(account)
        assert_no_sentinel(self, text, "AccountRef")

    def test_a_pool_account_has_no_token_attribute(self):
        pool = FakePool([sentinel_row()])
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=FakeTransport())
        account = source.accounts()[0]
        self.assertFalse(hasattr(account, "access_token"))
        self.assertFalse(hasattr(account, "refresh_token"))
        self.assertEqual(set(vars(account)), {"account_key", "label", "source_kind", "entry_id"})


class ReadOnlyTransportIsTheOnlyDryRunTransport(unittest.TestCase):
    def test_a_dry_run_cannot_post_even_with_a_real_source(self):
        from reset_rescue.codex_api import MutationRefused

        transport = FakeTransport()
        pool = FakePool([sentinel_row()])
        source = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        api = CodexResetApi(ReadOnlyTransport(source), sleep=lambda _s: None)
        with self.assertRaises(MutationRefused):
            api.consume(source.accounts()[0], CREDIT_SENTINEL, "uuid")
        self.assertEqual(transport.posts(), [])


class ServicePathsAreClean(unittest.TestCase):
    def test_no_service_dto_contains_a_sentinel_after_a_full_cycle(self):
        store = FakeStore()
        store.account_upsert(
            {"account_key": KEY_A, "label": "b***@e***.com", "source_kind": "pool"}
        )
        store.credit_upsert(
            {
                "credit_key": "c" * 16,
                "account_key": KEY_A,
                "title": "Full reset",
                "reset_type": "codex_rate_limits",
                "expires_at": 1_800_003_600.0,
                "backend_state": "available",
                "status": "scheduled",
                "updated_at": 1_800_000_000.0,
            }
        )
        deps = make_deps(store=store, salt=SALT)
        service = Service(deps)
        dtos = [
            service.status(),
            service.ensure(),
            service.scan(),
            service.history(),
            service.quota_check(),
            service.uninstall(),
        ]
        text = to_json({"all": dtos})
        assert_no_sentinel(self, text, "every DTO")

    def test_the_db_file_is_never_written_by_the_engine(self):
        """L1 never opens SQLite; L2's store owns the file (proved by absence)."""
        package = pathlib.Path(__file__).resolve().parents[2] / "reset_rescue"
        for name in (
            "accounts.py",
            "codex_api.py",
            "classify.py",
            "keys.py",
            "settings.py",
            "service.py",
            "dto.py",
            "clock.py",
        ):
            text = (package / name).read_text(encoding="utf-8")
            self.assertNotIn("sqlite3", text, name)


if __name__ == "__main__":
    unittest.main()
