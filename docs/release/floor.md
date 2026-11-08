# `requires_hermes` floor

**Floor: `>=0.22.6`.** Mirrored in `plugin.yaml` (`requires_hermes`) and in the
catalog entry (`docs/release/reset-rescue.yaml`). Catalog rule 14: the floor is
the oldest released Hermes that carries every surface the plugin uses — never
the version installed on the maintainer's machine (0.23.0).

## How this file was produced

```bash
scripts/floor_probe.sh --out docs/release/floor-probe.txt
```

The probe is read-only: `git log -S <symbol>` finds the commit that introduced
each surface (pickaxe), `git tag --contains <commit>` gives every tag holding
it, and only stable `vX.Y.Z` tags count — a canary tag is not something a user
can install. `docs/release/symbols.txt` is the input; it lists one surface per
line with the path that carries it (a whole-tree pickaxe walks every blob in
the history and takes minutes).

Raw output: [`floor-probe.txt`](floor-probe.txt).

## Result (2026-11-07, install at `90d6b98fd8`)

| Surface | Introduced | Earliest stable tag |
|---|---|---|
| `credentialed_provider_request` (active-sign-in fallback, `requires_auth`) | `4070f011a2bf` 2026-10-10 | **v0.21.7** |
| `desktop_half_copy` — unified-package Desktop half on the serving host | `f9e923f190b1` 2026-10-27 | **v0.22.6** |
| `STATUSBAR_AREAS` (Desktop status-bar area) | `ea0e4299ec5b` 2026-10-05 | v0.21.6 |
| `usePluginI18n` (Desktop i18n bundles) | `bba18c6008c3` 2026-07-19 | v0.21.6 |
| `plugin_data_dir` / `plugin_db` | `8f2ddc9676ff` 2026-08-17 | v0.21.6 |
| `register_cli_command` | `dd8a42bf7d46` 2026-04-05 | v0.21.6 |
| Desktop `config_schema` rendering | `bd6dcd4bd5f0` 2026-08-12 | v0.21.6 |
| cron `no_agent` | `3db6b9cc871c` 2026-05-04 | v0.21.6 |
| cron `--failure-deliver` | `c9491e6a7d9c` 2026-09-02 | v0.21.6 |
| cron scripts-dir guard | `5a8fbbf16ce7` 2026-09-02 | v0.21.6 |
| `hermes send` | `29b1bd0e20e5` 2026-05-16 | v0.21.6 |
| `read_credential_pool` | `8d59881a6246` 2026-03-31 | v0.21.6 |
| `try_refresh_matching` (`hermes auth refresh <provider> <id>`) | `34837597d298` 2026-07-17 | v0.21.6 |
| `Created job:` output the plugin parses | `c3ea62079679` 2026-03-14 | v0.21.6 (informational) |
| `save_plugin_settings` (dashboard `POST /settings` writer) | `5c0e73eff1` 2026-09-22 | v0.21.6 |
| `"dict": "json"` in `config_schema` (dict field type) | `5c0e73eff1` 2026-09-22 | v0.21.6 |
| `_mount_plugin_api_routes` (plugin dashboard router mount) | `2497ec5126` 2026-09-02 | v0.21.6 |
| `plugin_api.json` validator allow-list | `0502fe0597` 2026-11-05 | none yet (canary only); informational |

Rows marked *informational* are probed and printed but cannot raise the floor:
`plugin_api.json` only affects what `hermes plugins validate` warns about on the
catalog side, and the `Created job:` line is a CLI output format, not an API.
`plugin_api_allowlist` is therefore settled as `no`: no released tag ships
`hermes_cli/data/plugin_api.json` yet (checked at `v0.22.6` and `v0.23.0`), and
the plugin never reads it at runtime. It is a canary-only validator list.

Result refreshed 2026-11-07 against install `0ea6ea941a`; the earlier table
above (install `90d6b98fd8`) gained the three rows for the Settings-tab surfaces.

**Why 0.22.6 and not 0.21.7:** `desktop_half_copy` is the newest surface. A
unified package ships its Desktop half as `desktop/plugin.js`; Hermes copies it
to `<HERMES_HOME>/desktop-plugins/<name>/` on the serving host, and that copy
mechanism first appears in v0.22.6. On an older Hermes the Python half would
load and the status-bar chip would silently never appear — the exact failure
mode rule 14 exists to prevent.

## Status of the floor

The floor is derived from source history (the pickaxe probe above). It is not a runtime test. Runtime testing of the plugin so far used Hermes 0.23.0. Installation on v0.22.6 itself has not been run, so `hermes plugins doctor` and the popup on that tag are unverified. If that run fails, the floor rises to the oldest stable tag where doctor passes. It never falls back to the version installed on the development machine.

## Re-running

```bash
scripts/floor_probe.sh                                   # probe, print
scripts/floor_probe.sh --out docs/release/floor-probe.txt  # refresh this evidence
scripts/floor_probe.sh --check 0.22.6                    # exit 1 unless it still matches
```

`--check` is what CI runs once the floor is frozen: a symbol list that drifts
without the floor moving is a rule-14 finding, not a warning.
