"""A scan tells the user once when an account cannot be read (A§8.2 step 3).

The bug this file pins: ``_scan_locked`` wrote the new error code with
``set_account_error`` **before** calling ``_account_error``, whose whole job is
to compare the code it is given with the one already stored. The comparison
therefore always saw the value it had just written, so a scan could never queue
the ``account_error`` notice — the account failed silently, every scan, and the
user only found out when a credit expired unrescued.

The rule the tests hold the scan to:

* **none → error** queues one notice (the transition the user must hear);
* **the same category again** queues nothing — a 12 h scan must not repeat a
  sentence the user already read;
* **a different category** queues one more notice (network → auth is news: the
  first says "it will retry", the second asks for a sign-in);
* **recovery** clears the stored code and is silent (the catalog has no
  recovery kind), and the next failure is a fresh transition that notifies
  again.

Every scenario drives the real ``Planner.scan`` over the real ``Store``; only
the backend, the clock and the notifier are fakes (``tests/flows/support.py``).
"""

from __future__ import annotations

import unittest

from reset_rescue.accounts import AuthExpired, SourceUnavailable
from tests.flows.support import HOUR, Harness

#: One scan interval: a second failure has to be a *new* scan, not a retry.
SCAN_INTERVAL = 12 * HOUR


class AccountErrorNoticeCase(unittest.TestCase):
    """One account whose list GET fails, scanned repeatedly."""

    def setUp(self) -> None:
        self.h = Harness(self)
        self.account = self.h.add_account("b***@e***.com")
        self.h.add_credit(self.account, expires_in=30 * HOUR)
        # The first scan arms the credit and queues its reminder notice; the
        # failure scenarios below start from an account the plugin knows.
        self.assertEqual(self.h.scan().exit_code, 0)
        self.h.notifier.flushed.clear()

    # -- helpers -----------------------------------------------------------
    def fail_with(self, failure) -> None:
        self.h.backend.fail_list[self.account.account_key] = failure

    def recover(self) -> None:
        self.h.backend.fail_list.pop(self.account.account_key, None)

    def scan(self):
        """One scan, one interval after the previous one.

        ``flushed`` is the harness's running record of every notice delivered
        since the test began, so it is cleared first: each assertion below is
        about *this* scan's notices, not the ones the setup already sent.
        """
        self.h.advance(SCAN_INTERVAL)
        self.h.notifier.flushed.clear()
        return self.h.scan()

    def account_error_notices(self) -> list[dict]:
        return [row for row in self.h.notices("account_error")]

    def stored_code(self):
        row = self.h.store.get_account(self.account.account_key) or {}
        return row.get("last_error_code")

    # -- the four transitions ---------------------------------------------
    def test_the_first_failure_queues_exactly_one_notice(self):
        """none → error: the user is told the account cannot be read."""
        self.fail_with(ConnectionError("down"))
        result = self.scan()
        self.assertEqual(result.exit_code, 1, "a failing account fails the scan")
        notices = self.account_error_notices()
        self.assertEqual(len(notices), 1, notices)
        self.assertEqual(notices[0]["params"].get("label"), self.account.label)
        self.assertEqual(self.stored_code(), "network")

    def test_a_repeated_identical_failure_is_silent(self):
        """The same error twice in a row is one sentence, not one per scan."""
        self.fail_with(ConnectionError("down"))
        self.scan()
        self.assertEqual(len(self.account_error_notices()), 1)

        second = self.scan()
        self.assertEqual(second.exit_code, 1)
        self.assertEqual(
            self.account_error_notices(), [], "the same failure was announced a second time"
        )

        third = self.scan()
        self.assertEqual(third.exit_code, 1)
        self.assertEqual(self.account_error_notices(), [])

    def test_a_different_code_of_the_same_category_is_silent(self):
        """``network`` → ``timeout`` reads the same, so it says nothing new."""
        self.fail_with(ConnectionError("down"))
        self.scan()
        self.assertEqual(len(self.account_error_notices()), 1)

        self.fail_with(TimeoutError("slow"))
        self.scan()
        self.assertEqual(self.account_error_notices(), [])
        # The stored code still follows the backend, so the popup is honest.
        self.assertEqual(self.stored_code(), "timeout")

    def test_a_change_of_category_queues_a_second_notice(self):
        """network → auth is a different sentence and a different action."""
        self.fail_with(ConnectionError("down"))
        self.scan()
        self.assertEqual(len(self.account_error_notices()), 1)

        self.fail_with(AuthExpired(self.account.label))
        self.scan()
        notices = self.account_error_notices()
        self.assertEqual(len(notices), 1, "the category change was not announced")
        self.assertEqual(self.stored_code(), "auth_expired")

    def test_a_rate_limited_failure_is_its_own_category(self):
        """``transient`` is the rate-limited sentence, not the network one."""
        self.fail_with(ConnectionError("down"))
        self.scan()
        self.fail_with(SourceUnavailable("transient"))
        self.scan()
        self.assertEqual(len(self.account_error_notices()), 1)
        self.assertEqual(self.stored_code(), "transient")

    # -- recovery ----------------------------------------------------------
    def test_a_recovery_clears_the_error_silently(self):
        """A readable list clears the code; nothing is sent for "it works"."""
        self.fail_with(ConnectionError("down"))
        self.scan()
        self.assertEqual(self.stored_code(), "network")
        self.h.notifier.flushed.clear()

        self.recover()
        result = self.scan()
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(self.stored_code(), "the error code survived recovery")
        self.assertEqual(
            self.h.notices("account_error"), [], "recovery sent a notice the catalog does not have"
        )

    def test_a_failure_after_a_recovery_notifies_again(self):
        """Recovery is a transition too: the next failure is news again."""
        self.fail_with(ConnectionError("down"))
        self.scan()
        self.assertEqual(len(self.account_error_notices()), 1)

        self.recover()
        self.scan()
        self.h.notifier.flushed.clear()

        self.fail_with(ConnectionError("down"))
        self.scan()
        self.assertEqual(
            len(self.account_error_notices()), 1, "a failure after recovery was swallowed"
        )

    # -- the rest of the scan still works ----------------------------------


class MixedAccountsCase(unittest.TestCase):
    """One unreadable account never hides another account's credit."""

    def setUp(self) -> None:
        self.h = Harness(self)
        self.bad = self.h.add_account("x***@e***.com")
        self.good = self.h.add_account("g***@e***.com")
        self.h.add_credit(self.bad, "credit-bad", expires_in=30 * HOUR)
        self.h.add_credit(self.good, "credit-good", expires_in=30 * HOUR)
        self.h.backend.fail_list[self.bad.account_key] = ConnectionError("down")

    def test_the_failing_account_does_not_stop_the_healthy_one(self):
        result = self.h.scan()
        self.assertEqual(result.exit_code, 1)
        self.assertIsNotNone(self.h.slot(self.good, "primary", "credit-good"))
        self.assertIsNone(
            self.h.slot(self.bad, "primary", "credit-bad"),
            "a credit of the failing account was scheduled blind",
        )
        notices = self.h.notices("account_error")
        self.assertEqual(len(notices), 1, notices)
        self.assertEqual(notices[0]["params"].get("label"), self.bad.label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
