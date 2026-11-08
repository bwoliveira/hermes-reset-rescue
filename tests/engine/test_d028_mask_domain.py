"""D-028: masking hides the domain too; old-form labels and exclusions keep matching.

New form: ``b***@f***.com.br``. The first character of the local part, then
``***``, then the first character of the first domain label, ``***``, and the
public suffix (the last label, or the last two when the last is a two-letter
country code such as ``.com.br``).

Old form ``b***@gmail.com`` (labels stored by earlier builds) must keep matching
``excluded_accounts`` entries exactly as before, even after the stored label is
re-derived to the new form.
"""

from __future__ import annotations

import unittest

from reset_rescue.accounts import canonical_label, mask_email, mask_label
from reset_rescue.settings import parse_settings

KEY = "c" * 16


class MaskEmailNewForm(unittest.TestCase):
    def test_domain_is_masked_and_suffix_kept(self):
        cases = {
            "jane.doe@example.com.br": "j***@e***.com.br",
            "example@example.com": "e***@e***.com",
            "someone@shop.example.co.uk": "s***@s***.co.uk",
            "jane@example.org": "j***@e***.org",
            "a@mail.corp.example.com": "a***@m***.com",
            "a@corp": "a***@c***",
            "jane@example.br": "j***@e***.br",
            "a@x.co.uk": "a***@x***.co.uk",
        }
        for raw, masked in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(mask_email(raw), masked)

    def test_uppercase_is_lowered(self):
        self.assertEqual(mask_email("Jane.Doe@Example.COM.BR"), "j***@e***.com.br")

    def test_new_form_is_idempotent(self):
        for masked in ("j***@e***.com.br", "e***@e***.com", "a***@c***"):
            with self.subTest(masked=masked):
                self.assertEqual(mask_email(masked), masked)

    def test_non_email_still_empty(self):
        self.assertEqual(mask_email("not-an-email"), "")
        self.assertEqual(mask_email(""), "")


class MaskLabelBothForms(unittest.TestCase):
    def test_new_form_is_kept(self):
        self.assertEqual(mask_label("j***@e***.com.br"), "j***@e***.com.br")

    def test_old_form_moves_to_new_form(self):
        self.assertEqual(mask_label("b***@example.com"), "b***@e***.com")
        self.assertEqual(canonical_label("b***@example.com"), "b***@e***.com")

    def test_codex_fallback_unchanged(self):
        self.assertEqual(mask_label("", fallback_key="abcd1234"), "Codex account abcd")


class ExcludedAccountsOldAndNewForm(unittest.TestCase):
    def test_old_form_entry_keeps_excluding_after_rederivation(self):
        # The stored label is re-derived to the new form on the next scan.
        settings = parse_settings({"excluded_accounts": "b***@example.com"})
        self.assertFalse(settings.account_included(KEY, "b***@e***.com"))

    def test_new_form_entry_excludes(self):
        settings = parse_settings({"excluded_accounts": "b***@e***.com"})
        self.assertFalse(settings.account_included(KEY, "b***@e***.com"))
        self.assertFalse(settings.account_included(KEY, "b***@example.com"))

    def test_other_label_is_not_excluded(self):
        settings = parse_settings({"excluded_accounts": "b***@example.com"})
        self.assertTrue(settings.account_included(KEY, "c***@e***.com"))

    def test_account_key_entry_still_excludes(self):
        settings = parse_settings({"excluded_accounts": KEY})
        self.assertFalse(settings.account_included(KEY, "b***@e***.com"))


if __name__ == "__main__":
    unittest.main()
