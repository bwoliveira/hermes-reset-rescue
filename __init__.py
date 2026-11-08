"""Reset Rescue — native plugin entry point.

``register(ctx)`` does exactly one thing: it registers the
``hermes reset-rescue`` CLI command. It performs **no** network request, file
write, cron call or thread start (architecture A§4.1, invariant I11): Hermes
runs ``register`` in every process that discovers plugins, including
``hermes plugins doctor`` and ``hermes plugins validate``, which execute it in
a stub context under a temporary ``HERMES_HOME``.

The dashboard half is mounted by Hermes from ``dashboard/manifest.json``; the
Desktop half is loaded from ``desktop/plugin.js``. Neither is touched here.
"""

from __future__ import annotations

from typing import Any

__all__ = ["register"]

#: Command name registered with Hermes; also the CLI's own argv prefix.
COMMAND_NAME = "reset-rescue"


def register(ctx: Any) -> None:
    """Register the plugin's CLI command (inert: no I/O, no jobs, no threads)."""
    from .reset_rescue.cli import handle_cli, setup_cli

    ctx.register_cli_command(
        name=COMMAND_NAME,
        help="Rescue expiring Codex reset credits (status, scan, redeem, uninstall)",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
        description=(
            "Scans every openai-codex account in this profile's Hermes credential "
            "pool for banked rate-limit reset credits, redeems each one minutes "
            "before it expires through Hermes' own cron jobs, confirms by readback "
            "and sends one notice per result. Run `hermes reset-rescue status`."
        ),
    )
