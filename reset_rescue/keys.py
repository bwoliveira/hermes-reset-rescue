"""Salted keys and masked labels (architecture A§4.5, T-05).

The salt is 32 random bytes created once and kept in ``meta`` by the store
layer; this module only derives keys from it. Raw credit ids and raw ChatGPT
account ids are never stored, logged or serialized — the fire recomputes keys
over a fresh list and matches (I8).
"""

from __future__ import annotations

import hashlib
import re
import secrets

__all__ = [
    "KEY_HEX_LEN",
    "new_salt",
    "is_valid_salt",
    "credit_key",
    "account_key",
    "job_suffix",
    "job_name",
    "is_valid_key",
    "is_test_key",
    "TEST_KEY_PREFIX",
    "REHEARSAL_KEY_PREFIX",
    "TEST_JOB_PREFIX",
    "REHEARSAL_JOB_PREFIX",
    "JOB_PREFIX",
]

#: Length of a derived key in hex characters (64 bits of the digest).
KEY_HEX_LEN = 16

#: Salt is 32 random bytes rendered as 64 hex characters.
SALT_HEX_LEN = 64

#: Job names the plugin owns. Every owned name starts with one of these.
JOB_PREFIX = "reset-rescue-"
TEST_JOB_PREFIX = "reset-rescue-test-"
REHEARSAL_JOB_PREFIX = "reset-rescue-rehearsal-"

#: Key namespaces. A rehearsal key is a real key with a prefix, so it can
#: never collide with a real credit row (I5).
TEST_KEY_PREFIX = "test-"
REHEARSAL_KEY_PREFIX = "rehearsal-"

_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def new_salt() -> str:
    """A fresh 64-hex salt from 32 cryptographic random bytes."""
    return secrets.token_hex(32)


def is_valid_salt(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


def _digest(salt: str, *parts: str) -> str:
    """``sha256(salt ‖ part…)`` — plain concatenation, exactly as A§4.5 says.

    The salt is always 64 hex characters, so the concatenation is prefix-free
    and unambiguous without a separator; using one would make this module's
    keys disagree with ``accounts.account_key_for``.
    """
    if not isinstance(salt, str) or not salt:
        raise ValueError("a non-empty salt is required")
    material = salt + "".join(str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def credit_key(salt: str, account_backend_id: str, credit_id: str) -> str:
    """``sha256(salt ‖ account id ‖ credit id)[:16]`` (A§4.5).

    An empty ``credit_id`` is refused: with plain concatenation it would derive
    the same digest as :func:`account_key` for the same account, putting a
    credit key and an account key in one namespace.
    """
    if not str(credit_id):
        raise ValueError("a credit id is required")
    return _digest(salt, str(account_backend_id), str(credit_id))[:KEY_HEX_LEN]


def account_key(salt: str, backend_account_id: str) -> str:
    """``sha256(salt ‖ account id)[:16]`` — the same derivation as ``accounts``."""
    return _digest(salt, str(backend_account_id))[:KEY_HEX_LEN]


def job_suffix(key: str) -> str:
    """The 8-character suffix used in cron job names (A§4.5)."""
    if not is_valid_key(key):
        raise ValueError("a key must be 16 lowercase hex characters")
    return key[:8]


def job_name(role: str, key: str, *, namespace: str = "") -> str:
    """``reset-rescue-<role>-<key8>`` (with an optional namespace prefix)."""
    prefix = namespace or JOB_PREFIX
    return f"{prefix}{role}-{job_suffix(key)}"


def is_valid_key(value: object) -> bool:
    """A real credit/account key: exactly 16 lowercase hex characters."""
    return isinstance(value, str) and bool(_HEX16.match(value))


def is_test_key(value: object) -> bool:
    """A rehearsal key: ``rehearsal-`` or the legacy ``test-`` plus 16 hex."""
    if not isinstance(value, str):
        return False
    for prefix in (REHEARSAL_KEY_PREFIX, TEST_KEY_PREFIX):
        if value.startswith(prefix):
            return bool(_HEX16.match(value[len(prefix) :]))
    return False
