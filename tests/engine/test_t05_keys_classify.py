"""T-05: keys and classification.

The classification table is the I1 contract: ``available``, ``pending``
(including ``redeeming`` without ``redeemed_at``), ``redeemed``, ``unknown``.
"""

from __future__ import annotations

import unittest

from reset_rescue.classify import (
    AVAILABLE,
    CLASSIFICATIONS,
    PENDING,
    REDEEMED,
    UNKNOWN,
    Credit,
    classify,
    coerce_epoch,
    is_actionable,
    is_settled,
    parse_credit,
    parse_credit_list,
)
from reset_rescue.keys import (
    JOB_PREFIX,
    KEY_HEX_LEN,
    REHEARSAL_JOB_PREFIX,
    TEST_JOB_PREFIX,
    account_key,
    credit_key,
    is_test_key,
    is_valid_key,
    is_valid_salt,
    job_name,
    job_suffix,
    new_salt,
)

NOW = 1_800_000_000.0


class ClassificationTable(unittest.TestCase):
    """The ticket's table, one row per backend shape."""

    CASES = (
        # (status, redeem_started_at, redeemed_at, expected)
        ("available", None, None, AVAILABLE),
        ("redeeming", None, None, PENDING),
        ("redeeming", NOW - 30, None, PENDING),
        ("pending", None, None, PENDING),
        ("available", NOW - 30, None, PENDING),  # started, not done (R5)
        ("redeemed", None, NOW - 10, REDEEMED),
        ("available", None, NOW - 10, REDEEMED),  # redeemed_at wins
        ("used", None, None, REDEEMED),
        ("consumed", None, None, REDEEMED),
        ("expired", None, None, UNKNOWN),
        ("weird", None, None, UNKNOWN),
        ("", None, None, UNKNOWN),
    )

    def test_table(self):
        for status, started, redeemed, expected in self.CASES:
            credit = Credit(id="c1", status=status, redeem_started_at=started, redeemed_at=redeemed)
            self.assertEqual(
                credit.classification,
                expected,
                f"status={status} started={started} redeemed={redeemed}",
            )

    def test_redeeming_without_redeemed_at_is_never_redeemed(self):
        credit = Credit(id="c1", status="redeeming")
        self.assertNotEqual(credit.classification, REDEEMED)
        self.assertTrue(credit.actionable)

    def test_a_pending_credit_is_actionable_but_not_available(self):
        credit = Credit(id="c1", status="redeeming")
        self.assertTrue(credit.actionable)
        self.assertFalse(credit.available)
        self.assertFalse(credit.settled)

    def test_only_a_clean_available_row_is_available(self):
        self.assertTrue(Credit(id="c1", status="available").available)
        self.assertFalse(Credit(id="c1", status="available", redeem_started_at=NOW).available)

    def test_classify_accepts_a_row_a_credit_and_a_status(self):
        self.assertEqual(classify({"id": "c1", "status": "redeeming"}), PENDING)
        self.assertEqual(classify(Credit(id="c1", status="redeemed")), REDEEMED)
        self.assertEqual(classify("available"), AVAILABLE)
        self.assertEqual(classify(None), UNKNOWN)
        self.assertEqual(classify({"status": "available"}), UNKNOWN)  # no id

    def test_is_actionable_and_is_settled_helpers(self):
        self.assertTrue(is_actionable({"id": "c", "status": "available"}))
        self.assertTrue(is_actionable({"id": "c", "status": "redeeming"}))
        self.assertFalse(is_actionable({"id": "c", "status": "redeemed"}))
        self.assertTrue(is_settled({"id": "c", "status": "redeemed"}))
        self.assertFalse(is_settled({"id": "c", "status": "available"}))

    def test_every_result_is_a_known_classification(self):
        for status, started, redeemed, _ in self.CASES:
            value = Credit(
                id="c", status=status, redeem_started_at=started, redeemed_at=redeemed
            ).classification
            self.assertIn(value, CLASSIFICATIONS)

    def test_status_matching_is_case_and_space_insensitive(self):
        self.assertEqual(Credit(id="c", status="  AVAILABLE ").classification, AVAILABLE)
        self.assertEqual(Credit(id="c", status="Redeemed").classification, REDEEMED)


class Parsing(unittest.TestCase):
    def test_unknown_keys_are_dropped_with_the_raw_body(self):
        credit = parse_credit(
            {
                "id": "c1",
                "status": "available",
                "expires_at": NOW + 600,
                "profile_image_url": "https://secret.example/x.png",
                "profile_user_id": "user-123",
                "description": "internal text",
                "is_supported_by_plan": True,
            }
        )
        self.assertEqual(credit.id, "c1")
        self.assertFalse(hasattr(credit, "profile_user_id"))
        self.assertFalse(hasattr(credit, "description"))

    def test_a_row_without_an_id_is_dropped(self):
        self.assertIsNone(parse_credit({"status": "available"}))
        self.assertIsNone(parse_credit({"id": "", "status": "available"}))
        self.assertIsNone(parse_credit({"id": 42, "status": "available"}))
        self.assertIsNone(parse_credit("not a mapping"))

    def test_epochs_accept_seconds_milliseconds_and_iso(self):
        self.assertEqual(coerce_epoch(1_800_000_000), 1_800_000_000.0)
        self.assertEqual(coerce_epoch(1_800_000_000_000), 1_800_000_000.0)
        self.assertEqual(coerce_epoch("1800000000"), 1_800_000_000.0)
        self.assertEqual(coerce_epoch("2027-01-15T08:00:00Z"), 1_800_000_000.0)
        self.assertEqual(coerce_epoch("2027-01-15T08:00:00+00:00"), 1_800_000_000.0)

    def test_unparseable_epochs_are_none_not_zero(self):
        for value in (None, "", "soon", [], {}, True, 0, -5, "not a date"):
            self.assertIsNone(coerce_epoch(value), value)

    def test_parse_credit_list_keeps_order_and_count(self):
        payload = {
            "credits": [
                {"id": "a", "status": "available", "expires_at": NOW + 100},
                {"id": "b", "status": "redeeming"},
                {"id": "c", "status": "redeemed", "redeemed_at": NOW - 10},
                {"status": "available"},  # dropped: no id
            ],
            "available_count": 2,
            "history_enabled": True,
            "total_earned_count": 8,
        }
        parsed = parse_credit_list(payload)
        self.assertEqual(len(parsed), 3)
        self.assertEqual([c.id for c in parsed], ["a", "b", "c"])
        self.assertEqual(parsed.available_count, 2)
        self.assertEqual(len(parsed.actionable()), 2)

    def test_a_malformed_payload_is_an_empty_list(self):
        for payload in (None, [], "nope", {}, {"credits": "nope"}, {"credits": [1, 2]}):
            parsed = parse_credit_list(payload)
            self.assertEqual(len(parsed), 0, payload)
            self.assertIsNone(parsed.available_count, payload)

    def test_available_count_must_be_an_int(self):
        parsed = parse_credit_list({"credits": [], "available_count": "2"})
        self.assertIsNone(parsed.available_count)
        parsed = parse_credit_list({"credits": [], "available_count": True})
        self.assertIsNone(parsed.available_count)

    def test_by_id_finds_a_credit(self):
        parsed = parse_credit_list({"credits": [{"id": "x", "status": "available"}]})
        self.assertIsNotNone(parsed.by_id("x"))
        self.assertIsNone(parsed.by_id("y"))


class Expiry(unittest.TestCase):
    def test_expires_in_and_expired(self):
        credit = Credit(id="c", status="available", expires_at=NOW + 600)
        self.assertEqual(credit.expires_in(NOW), 600.0)
        self.assertFalse(credit.expired(NOW))
        self.assertTrue(credit.expired(NOW + 600))
        self.assertTrue(credit.expired(NOW + 540, margin_s=60))

    def test_a_credit_without_an_expiry_is_never_expired(self):
        credit = Credit(id="c", status="available")
        self.assertIsNone(credit.expires_in(NOW))
        self.assertFalse(credit.expired(NOW + 10**9))


class NoRawIdInRepr(unittest.TestCase):
    def test_credit_repr_never_contains_the_raw_id(self):
        credit = Credit(id="raw-credit-id-12345", status="available", expires_at=NOW)
        text = repr(credit)
        self.assertNotIn("raw-credit-id-12345", text)
        self.assertIn("available", text)


class KeyDerivation(unittest.TestCase):
    def test_a_salt_is_64_hex_and_fresh_each_time(self):
        first, second = new_salt(), new_salt()
        self.assertTrue(is_valid_salt(first))
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, second)

    def test_invalid_salts_are_rejected(self):
        for value in ("", "short", "z" * 64, None, 42, "A" * 64):
            self.assertFalse(is_valid_salt(value), value)

    def test_keys_are_stable_for_one_salt(self):
        salt = "a" * 64
        first = credit_key(salt, "acct-1", "credit-1")
        second = credit_key(salt, "acct-1", "credit-1")
        self.assertEqual(first, second)
        self.assertEqual(len(first), KEY_HEX_LEN)
        self.assertTrue(is_valid_key(first))

    def test_keys_differ_per_salt_account_and_credit(self):
        salt = "a" * 64
        base = credit_key(salt, "acct-1", "credit-1")
        self.assertNotEqual(base, credit_key("b" * 64, "acct-1", "credit-1"))
        self.assertNotEqual(base, credit_key(salt, "acct-2", "credit-1"))
        self.assertNotEqual(base, credit_key(salt, "acct-1", "credit-2"))

    def test_the_key_never_contains_the_raw_inputs(self):
        salt = "a" * 64
        key = credit_key(salt, "acct-1", "credit-1")
        for raw in ("acct-1", "credit-1", salt):
            self.assertNotIn(raw, key)

    def test_an_empty_salt_raises(self):
        with self.assertRaises(ValueError):
            credit_key("", "acct-1", "credit-1")

    def test_account_key_matches_the_accounts_module(self):
        from reset_rescue.accounts import account_key_for

        self.assertEqual(account_key("a" * 64, "acct-1"), account_key_for("a" * 64, "acct-1"))

    def test_an_empty_credit_id_cannot_collide_with_an_account_key(self):
        # Plain concatenation would make credit_key(salt, "x", "") equal
        # account_key(salt, "x"); the empty id is refused instead.
        with self.assertRaises(ValueError):
            credit_key("a" * 64, "x", "")


class JobNames(unittest.TestCase):
    def test_job_name_uses_eight_characters_of_the_key(self):
        key = "0123456789abcdef"
        self.assertEqual(job_suffix(key), "01234567")
        self.assertEqual(job_name("primary", key), f"{JOB_PREFIX}primary-01234567")
        self.assertEqual(job_name("backup", key), f"{JOB_PREFIX}backup-01234567")

    def test_namespaced_job_names(self):
        key = "0123456789abcdef"
        self.assertEqual(
            job_name("rehearsal", key, namespace=REHEARSAL_JOB_PREFIX),
            f"{REHEARSAL_JOB_PREFIX}rehearsal-01234567",
        )
        self.assertEqual(
            job_name("scan", key, namespace=TEST_JOB_PREFIX), f"{TEST_JOB_PREFIX}scan-01234567"
        )

    def test_a_malformed_key_raises(self):
        for value in ("short", "g" * 16, "A" * 16, "", None):
            with self.assertRaises(ValueError):
                job_suffix(value)  # type: ignore[arg-type]

    def test_key_validation(self):
        self.assertTrue(is_valid_key("0123456789abcdef"))
        for value in ("0123456789ABCDEF", "0123456789abcde", "0123456789abcdef0", "", None, 42):
            self.assertFalse(is_valid_key(value), value)

    def test_rehearsal_keys_are_a_separate_namespace(self):
        self.assertTrue(is_test_key("rehearsal-0123456789abcdef"))
        self.assertTrue(is_test_key("test-0123456789abcdef"))
        self.assertFalse(is_test_key("0123456789abcdef"))
        self.assertFalse(is_test_key("rehearsal-xyz"))
        self.assertFalse(is_test_key(None))

    def test_a_rehearsal_key_is_never_a_valid_real_key(self):
        self.assertFalse(is_valid_key("rehearsal-0123456789abcdef"))


if __name__ == "__main__":
    unittest.main()
