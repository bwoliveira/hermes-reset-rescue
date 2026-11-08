"""R4: per-account overrides apply by account key only; the label is a masked hint.

A masked label can collide between two accounts, and a rebuilt database can leave
an override whose key is gone. Neither may select an override (D-030, R4).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from reset_rescue.authorize import authorize_consume
from reset_rescue.settings import parse_settings

KEY_A = "a" * 16
KEY_B = "b" * 16
KEY_NEW = "c" * 16
LABEL = "a***@e***.com"
NOW = 1_800_000_000.0


def _settings_with_a_only_override():
    return parse_settings(
        {"account_overrides": {KEY_A: {"label": LABEL, "auto_redeem_enabled": False}}}
    )


def _primary_due(settings, account_key):
    """Ask the money boundary for an automatic redeem inside the default lead window."""
    return authorize_consume(
        role="primary",
        credit=SimpleNamespace(expires_at=NOW + 300.0),
        settings=settings,
        now=NOW,
        account_key=account_key,
        label=LABEL,
    )


class KeyOnlyOverrides(unittest.TestCase):
    def test_colliding_masked_labels_leave_the_other_account_armed(self):
        # (a) A and B share the masked label. The override is A's, so B keeps the global.
        settings = _settings_with_a_only_override().with_pool([(KEY_A, LABEL), (KEY_B, LABEL)])
        self.assertIsNone(settings.override_entry(KEY_B, LABEL))
        self.assertTrue(settings.for_account(KEY_B, LABEL).auto_redeem_enabled)
        self.assertTrue(_primary_due(settings, KEY_B).allowed)
        self.assertEqual(_primary_due(settings, KEY_A).reason, "auto_redeem_disabled")
        self.assertNotIn("settings_account_override_orphan", settings.warnings)

    def test_an_orphan_override_is_ignored_and_reported_once(self):
        # (b) KEY_A has left the pool. Its label matches B, but B must not inherit it.
        settings = _settings_with_a_only_override().with_pool([(KEY_B, LABEL), (KEY_NEW, LABEL)])
        self.assertIsNone(settings.override_entry(KEY_B, LABEL))
        self.assertIsNone(settings.override_entry(KEY_NEW, LABEL))
        self.assertTrue(settings.for_account(KEY_B, LABEL).auto_redeem_enabled)
        self.assertTrue(_primary_due(settings, KEY_B).allowed)
        self.assertEqual(settings.warnings.count("settings_account_override_orphan"), 1)
        again = settings.with_pool([(KEY_B, LABEL)])
        self.assertEqual(again.warnings.count("settings_account_override_orphan"), 1)

    def test_an_override_whose_key_is_still_in_the_pool_applies(self):
        settings = _settings_with_a_only_override().with_pool([(KEY_A, LABEL)])
        self.assertIsNotNone(settings.override_entry(KEY_A, LABEL))
        self.assertNotIn("settings_account_override_orphan", settings.warnings)

    def test_a_label_match_alone_never_selects_an_override(self):
        # Old fallback case: A's key is gone and a new key carries A's label. Key-only: no match.
        settings = _settings_with_a_only_override()
        self.assertIsNone(settings.override_entry(KEY_NEW, LABEL))
        self.assertIsNone(settings.override_entry(KEY_B, LABEL))
        self.assertTrue(_primary_due(settings, KEY_NEW).allowed)

    def test_without_a_pool_a_label_match_still_selects_nothing(self):
        # Old D-030 fallback case: no pool known. Key-only: no match.
        settings = _settings_with_a_only_override()
        self.assertIsNone(settings.override_entry(KEY_B, LABEL))
        self.assertIsNotNone(settings.override_entry(KEY_A, LABEL))


if __name__ == "__main__":
    unittest.main()
