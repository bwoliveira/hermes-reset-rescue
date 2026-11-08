"""Version and schema constants for Reset Rescue.

Kept in one module so the manifest, the DTOs and the tests never disagree.
"""

VERSION = "0.1.0"

#: Local SQLite schema version (owned by ``store.py``, mirrored here so the
#: status DTO can report it without importing the store).
SCHEMA_VERSION = 2

#: Version of the wrapper template rendered into ``<HERMES_HOME>/scripts/``.
WRAPPER_TEMPLATE_VERSION = 1

PLUGIN_ID = "reset-rescue"
PROVIDER = "openai-codex"
