"""Reset Rescue — engine package.

Public entry points:

``service``
    The only entry for the CLI and the HTTP routes.
``settings``
    Coerced, bounded settings.
``accounts``
    ``AccountSource`` implementations (read-only pool, active sign-in).
``codex_api``
    The three Codex endpoints over an ``AccountSource``.
``classify`` / ``keys``
    Credit classification and salted keys.
``store``
    SQLite state, retention and the run lock (A§6).
``planner`` / ``fire``
    The scan and one-shot flows (A§8.2-8.3).
``dto``
    Allow-listed serializers for everything that leaves the engine.

Importing this package must never touch the network, the filesystem or
``~/.hermes``: registration is inert (I11).
"""

from __future__ import annotations

from .version import PLUGIN_ID, PROVIDER, SCHEMA_VERSION, VERSION

__all__ = ["VERSION", "SCHEMA_VERSION", "PLUGIN_ID", "PROVIDER"]
