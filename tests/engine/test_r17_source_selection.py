"""R17(a) — source selection must never depend on the ambient profile.

The reviewer's counterexample: ``SourceSelection.test_active_sign_in_is_no_longer_a_setting``
called ``select_source`` with no injected pool, so it read the *real*
``agent.credential_pool`` and asserted the result was ``"pool"``. On a clean
profile (an empty pool) the same code correctly returns ``active_sign_in`` and
the test failed — a unit test whose verdict depended on who ran it.

The fix is in the test: it stubs the pool the way its siblings in the same class
already do. These tests keep it that way by running the class with the module's
default reader replaced by one that raises, so a test that reaches for the
profile fails here on any machine instead of passing on a laptop with five
accounts.
"""

from __future__ import annotations

import unittest

import reset_rescue.accounts as accounts_module
from tests.engine.test_t10_accounts import SourceSelection


class _PoisonedAmbientPool:
    """Make the module's default pool reader raise for the length of a block."""

    def __enter__(self):
        self.original = accounts_module._default_read_pool
        accounts_module._default_read_pool = self._raise
        return self

    def __exit__(self, *exc_info) -> None:
        accounts_module._default_read_pool = self.original

    @staticmethod
    def _raise():
        raise AssertionError("source selection read the ambient credential pool")


def _run(case: unittest.TestCase) -> unittest.TestResult:
    result = unittest.TestResult()
    with _PoisonedAmbientPool():
        case.run(result)
    return result


class NoAmbientPoolRead(unittest.TestCase):
    def test_the_reviewers_counterexample_passes_without_an_ambient_pool(self):
        """The exact test the reviewer named, with the ambient reader poisoned."""
        result = _run(SourceSelection("test_active_sign_in_is_no_longer_a_setting"))
        self.assertTrue(
            result.wasSuccessful(),
            "test_active_sign_in_is_no_longer_a_setting depends on the ambient pool: "
            f"{result.failures + result.errors}",
        )

    def test_the_whole_source_selection_class_passes_without_an_ambient_pool(self):
        """My variant: the same class of defect, across every test in the class.

        One ambient-dependent test is the reported symptom; the class is the
        unit that has to stay hermetic, because the next test added there would
        inherit the same trap. Every test of ``SourceSelection`` runs with the
        default reader poisoned, so any of them reaching for the profile fails.
        """
        failures: list[str] = []
        for name in unittest.TestLoader().getTestCaseNames(SourceSelection):
            result = _run(SourceSelection(name))
            if not result.wasSuccessful():
                failures.append(name)
        self.assertEqual(failures, [], f"tests depend on the ambient pool: {failures}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
