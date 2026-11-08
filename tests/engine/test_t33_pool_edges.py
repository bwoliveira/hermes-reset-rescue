"""T-33: pool edge cases — duplicates, custom base_url, global fallback, removal.

Four shapes the pool can take that must not break a run:

1. two rows for the same ChatGPT account (two sign-ins) collapse to one account;
2. a row pointing at a custom gateway never receives a request;
3. the global-root fallback (Hermes merges it per provider) is just rows here;
4. an account removed from the pool yields ``account_missing`` and no POST.

Plus the key-stability rule: the same account has the same ``account_key``
whichever source discovered it, so switching to ``active_sign_in`` does not
orphan state rows.
"""

from __future__ import annotations

import unittest

from reset_rescue.accounts import (
    CONSUME_PATH,
    CREDITS_PATH,
    ERR_ACCOUNT_MISSING,
    ERR_UNSUPPORTED_ENDPOINT,
    OFFICIAL_ORIGIN,
    ActiveSignInSource,
    PoolAccountSource,
    Response,
    SourceUnavailable,
    account_key_for,
    select_source,
)
from reset_rescue.clock import Deps
from reset_rescue.settings import parse_settings
from tests.fakes import FakeCli, FakeClock, FakePool, FakeTransport, jwt_for

SALT = "a" * 64
CREDITS_URL = OFFICIAL_ORIGIN + CREDITS_PATH
CONSUME_URL = OFFICIAL_ORIGIN + CONSUME_PATH


def row(
    entry_id: str,
    account_id: str = "acct-1",
    *,
    token: str | None = None,
    base_url: str = "",
    status: str = "ok",
    refreshed: str = "2026-11-07T00:00:00Z",
    label: str = "",
) -> dict:
    return {
        "id": entry_id,
        "provider": "openai-codex",
        "access_token": token or jwt_for(account_id),
        "refresh_token": "r",
        "account_id": account_id,
        "label": label,
        "base_url": base_url,
        "last_status": status,
        "last_refresh": refreshed,
    }


def source_for(rows, *, transport=None, cli=None):
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


class DuplicateSignIns(unittest.TestCase):
    """S2: the same account signed in twice yields two rows, one account."""

    def test_two_rows_for_one_account_collapse_to_one_account(self):
        source, _, _ = source_for(
            [
                row("entry-a", "acct-1", refreshed="2026-11-01T00:00:00Z"),
                row("entry-b", "acct-1", refreshed="2026-11-06T00:00:00Z"),
            ]
        )
        refs = source.accounts()
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].account_key, account_key_for(SALT, "acct-1"))

    def test_the_newest_ok_row_is_the_one_used(self):
        source, _, _ = source_for(
            [
                row("entry-old", "acct-1", refreshed="2026-11-01T00:00:00Z"),
                row("entry-new", "acct-1", refreshed="2026-11-06T00:00:00Z"),
            ]
        )
        self.assertEqual(source.accounts()[0].entry_id, "entry-new")

    def test_a_failing_row_loses_to_an_ok_row_even_if_newer(self):
        source, _, _ = source_for(
            [
                row("entry-ok", "acct-1", status="ok", refreshed="2026-11-01T00:00:00Z"),
                row("entry-bad", "acct-1", status="error", refreshed="2026-11-06T00:00:00Z"),
            ]
        )
        self.assertEqual(source.accounts()[0].entry_id, "entry-ok")

    def test_the_duplicates_are_reported_for_the_popup(self):
        source, _, _ = source_for(
            [
                row("entry-a", "acct-1", refreshed="2026-11-01T00:00:00Z"),
                row("entry-b", "acct-1", refreshed="2026-11-06T00:00:00Z"),
            ]
        )
        key = source.accounts()[0].account_key
        self.assertEqual(source.duplicates(), {key: ["entry-a"]})

    def test_distinct_accounts_stay_distinct(self):
        source, _, _ = source_for([row("e1", "acct-1"), row("e2", "acct-2")])
        self.assertEqual(len(source.accounts()), 2)
        self.assertEqual(source.duplicates(), {})

    def test_a_refresh_of_a_duplicated_account_targets_the_chosen_entry(self):
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        source, _, cli = source_for(
            [
                row("entry-old", "acct-1", refreshed="2026-11-01T00:00:00Z"),
                row("entry-new", "acct-1", refreshed="2026-11-06T00:00:00Z"),
            ],
            transport=transport,
        )
        account = source.accounts()[0]
        with self.assertRaises(SourceUnavailable):
            source.request(account, "GET", CREDITS_URL)
        self.assertEqual(cli.refreshes(), ["entry-new"])


class CustomBaseUrl(unittest.TestCase):
    """A gateway row must never see a request or a token (I12)."""

    def test_a_custom_base_url_row_is_skipped_with_a_code(self):
        source, _, _ = source_for([row("e1", "acct-1", base_url="https://gateway.example.com/v1")])
        self.assertEqual(source.accounts(), [])
        self.assertEqual(source.skipped(), {"e1": ERR_UNSUPPORTED_ENDPOINT})

    def test_a_custom_row_never_receives_a_request(self):
        transport = FakeTransport()
        source, _, _ = source_for(
            [row("e1", "acct-1", base_url="https://gateway.example.com/v1")],
            transport=transport,
        )
        self.assertEqual(source.accounts(), [])
        self.assertEqual(transport.requests, [])

    def test_a_plain_http_row_is_skipped(self):
        source, _, _ = source_for([row("e1", "acct-1", base_url="http://chatgpt.com")])
        self.assertEqual(source.accounts(), [])

    def test_a_lookalike_host_is_skipped(self):
        source, _, _ = source_for([row("e1", "acct-1", base_url="https://chatgpt.com.evil.test")])
        self.assertEqual(source.accounts(), [])

    def test_a_missing_base_url_is_the_official_endpoint(self):
        source, _, _ = source_for([row("e1", "acct-1", base_url="")])
        self.assertEqual(len(source.accounts()), 1)

    def test_the_official_url_in_a_row_is_accepted(self):
        source, _, _ = source_for([row("e1", "acct-1", base_url="https://chatgpt.com")])
        self.assertEqual(len(source.accounts()), 1)

    def test_a_custom_row_does_not_hide_a_good_row_for_the_same_account(self):
        source, _, _ = source_for(
            [
                row("e-bad", "acct-1", base_url="https://gateway.example.com"),
                row("e-good", "acct-1"),
            ]
        )
        refs = source.accounts()
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].entry_id, "e-good")


class GlobalRootFallback(unittest.TestCase):
    """Hermes merges the global root per provider; the source just sees rows."""

    def test_rows_from_a_global_fallback_are_used_normally(self):
        source, _, _ = source_for([row("global-entry", "acct-global")])
        refs = source.accounts()
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].account_key, account_key_for(SALT, "acct-global"))

    def test_a_profile_row_and_a_global_row_for_different_accounts_both_appear(self):
        source, _, _ = source_for([row("p1", "acct-profile"), row("g1", "acct-global")])
        self.assertEqual(len(source.accounts()), 2)

    def test_an_empty_pool_selects_active_sign_in(self):
        import reset_rescue.accounts as accounts_module

        original = accounts_module.PoolAccountSource

        class Empty(original):
            def __init__(self, **kwargs):
                super().__init__(read_pool=lambda: [], **kwargs)

        accounts_module.PoolAccountSource = Empty
        try:
            source = select_source(Deps(salt=SALT, clock=FakeClock()))
            self.assertEqual(source.kind, "active_sign_in")
        finally:
            accounts_module.PoolAccountSource = original


class RemovedAccount(unittest.TestCase):
    def test_a_removed_account_gives_account_missing_and_no_post(self):
        pool = FakePool([row("e1", "acct-1")])
        first = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=FakeTransport())
        persisted = first.accounts()[0]

        pool.rows.clear()
        transport = FakeTransport()
        second = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        second.accounts()
        with self.assertRaises(SourceUnavailable) as ctx:
            second.request(persisted, "POST", CONSUME_URL, json={"credit_id": "c1"})
        self.assertEqual(ctx.exception.code, ERR_ACCOUNT_MISSING)
        self.assertEqual(transport.posts(), [])

    def test_a_removed_account_on_a_get_also_gives_account_missing(self):
        pool = FakePool([row("e1", "acct-1")])
        first = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=FakeTransport())
        persisted = first.accounts()[0]
        pool.rows.clear()
        second = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=FakeTransport())
        second.accounts()
        with self.assertRaises(SourceUnavailable) as ctx:
            second.request(persisted, "GET", CREDITS_URL)
        self.assertEqual(ctx.exception.code, ERR_ACCOUNT_MISSING)

    def test_an_account_that_comes_back_works_again(self):
        pool = FakePool([row("e1", "acct-1")])
        first = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=FakeTransport())
        persisted = first.accounts()[0]
        saved = list(pool.rows)
        pool.rows.clear()
        pool.read()
        pool.rows.extend(saved)

        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(200, {"credits": []}))
        second = PoolAccountSource(salt=SALT, read_pool=pool.read, transport=transport)
        self.assertEqual(second.request(persisted, "GET", CREDITS_URL).status, 200)


class KeyStabilityAcrossSources(unittest.TestCase):
    """Switching to active_sign_in must not orphan the state rows."""

    def test_the_same_account_has_the_same_key_in_both_sources(self):
        pool_source, _, _ = source_for([row("e1", "acct-1")])
        pool_key = pool_source.accounts()[0].account_key

        def request_fn(provider, method, url, *, json=None, timeout=30.0):
            return Response(200, {"account": {"account_id": "acct-1", "email": "bob@example.com"}})

        active = ActiveSignInSource(salt=SALT, request_fn=request_fn, deps=Deps(clock=FakeClock()))
        active_key = active.accounts()[0].account_key
        self.assertEqual(pool_key, active_key)
        self.assertEqual(pool_key, account_key_for(SALT, "acct-1"))

    def test_a_different_salt_changes_every_key(self):
        first, _, _ = source_for([row("e1", "acct-1")])
        pool = FakePool([row("e1", "acct-1")])
        second = PoolAccountSource(salt="b" * 64, read_pool=pool.read, transport=FakeTransport())
        self.assertNotEqual(first.accounts()[0].account_key, second.accounts()[0].account_key)

    def test_the_label_is_masked_in_both_sources(self):
        pool_source, _, _ = source_for([row("e1", "acct-1")])
        self.assertEqual(pool_source.accounts()[0].label, "b***@e***.com")

        def request_fn(provider, method, url, *, json=None, timeout=30.0):
            return Response(200, {"account": {"account_id": "acct-1", "email": "bob@example.com"}})

        active = ActiveSignInSource(salt=SALT, request_fn=request_fn, deps=Deps(clock=FakeClock()))
        self.assertEqual(active.accounts()[0].label, "b***@e***.com")


class ExcludedAccountsFiltering(unittest.TestCase):
    """The exclusion list is a settings concern; the source still reports all."""

    def test_the_source_reports_every_account_and_settings_filters(self):
        source, _, _ = source_for([row("e1", "acct-1"), row("e2", "acct-2")])
        refs = source.accounts()
        self.assertEqual(len(refs), 2)

        skipped = refs[0].account_key
        settings = parse_settings({"excluded_accounts": skipped})
        included = [ref for ref in refs if settings.account_included(ref.account_key)]
        self.assertEqual(len(included), 1)
        self.assertNotEqual(included[0].account_key, skipped)

    def test_an_empty_list_includes_everything(self):
        source, _, _ = source_for([row("e1", "acct-1"), row("e2", "acct-2")])
        settings = parse_settings({})
        self.assertTrue(
            all(settings.account_included(ref.account_key) for ref in source.accounts())
        )


class RowShapes(unittest.TestCase):
    def test_a_non_mapping_row_is_ignored(self):
        source, _, _ = source_for([row("e1", "acct-1"), "junk", None, 42])
        self.assertEqual(len(source.accounts()), 1)

    def test_a_row_with_an_empty_id_is_ignored(self):
        bad = row("e1", "acct-1")
        bad["id"] = ""
        source, _, _ = source_for([bad])
        self.assertEqual(source.accounts(), [])

    def test_an_account_id_from_the_jwt_when_the_row_omits_it(self):
        bare = row("e1", "acct-7")
        bare["account_id"] = ""
        source, _, _ = source_for([bare])
        self.assertEqual(source.accounts()[0].account_key, account_key_for(SALT, "acct-7"))

    def test_a_row_with_neither_id_nor_claim_is_skipped(self):
        bare = {"id": "e1", "access_token": "opaque-token"}
        source, _, _ = source_for([bare])
        self.assertEqual(source.accounts(), [])
        self.assertEqual(source.skipped(), {"e1": ERR_ACCOUNT_MISSING})

    def test_the_pool_is_read_once_per_accounts_call(self):
        source, pool, _ = source_for([row("e1", "acct-1")])
        source.accounts()
        source.accounts()
        self.assertEqual(pool.reads, 2)

    def test_no_row_is_ever_written(self):
        source, pool, _ = source_for([row("e1", "acct-1")])
        transport = FakeTransport()
        transport.on("GET", "/rate-limit-reset-credits", Response(401))
        source._transport = transport
        with self.assertRaises(SourceUnavailable):
            source.request(source.accounts()[0], "GET", CREDITS_URL)
        self.assertEqual(pool.write_calls, [])


if __name__ == "__main__":
    unittest.main()
