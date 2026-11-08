# S11 — scanner and validator on the early tree

**Verdict: PASS.** No `dangerous` finding; every `caution`/`medium` finding is
understood and explained below. The spike ran on a skeleton that carries the
same shape as the real tree (manifest, `__init__.py`, `desktop/plugin.js`, a
`scripts/` cron wrapper, docs) because the real tree had no `plugin.yaml` yet
when the spike ran — L3 owns that file (request below).

## What ran

```bash
export HERMES_HOME=<HERMES_HOME>/cache/scratch/rr-l5/home-s11   # temp profile, never the real one
hermes plugins validate <skeleton>          # catalog-admission gate
hermes plugins validate <skeleton> --json   # same, machine-readable
python3 .../run_runtime_script.py scripts/scan_repo.py --strict   # scanner over this repo
```

`hermes plugins validate` runs the install-time scanner itself
(`tools.plugin_guard.scan_plugin`), so a `dangerous` verdict here is the exact
verdict a catalog install would get.

## Result on the skeleton

```
✓ manifest — plugin.yaml parses
✓ manifest fields — name, version, description present
✓ requires_hermes — spec '>=0.22.6' parses
✓ config schema — not declared
✓ requires_env — all entries UPPER_SNAKE
✓ oauth — not declared
✓ loadable — entry: __init__.py, desktop/plugin.js
✓ python dependencies — none declared
✓ capability probe — register() ran in isolation
✓ declared tools — matches registrations
✓ declared hooks — matches registrations
✓ declared middleware — matches registrations
✓ built-in tool collisions — no tools to check
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
✓ desktop surface — stays inside the plugin SDK surface
◆ Isolation — runs in the plugin host (plugins.isolation: host)

Validation passed.
```

The skeleton's `scripts/reset_rescue_run.sh` (the cron wrapper template, the
shape §7.1 contingency is about) raised **no** finding: a wrapper that `exec`s
`hermes reset-rescue scan` is not a `curl | sh`, not an obfuscated payload and
not a credential reader. The `dangerous` risk in §12 risk 7 did not
materialise, so the §7.1 explicit-opt-in fallback is **not** needed.

## Earlier run on this repository (before the candidate tree)

```
$ python3 .../run_runtime_script.py scripts/scan_repo.py --strict
scanner: l5-release: safe — 22 finding(s) in execution, exfiltration, persistence
verdict: safe
22 finding(s), 0 unexplained
RESULT: SAFE
```

Twenty-two findings across the merged tree, all `low`/`medium`, none `critical`:

| Pattern | File | Severity | Why it is not a threat |
|---|---|---|---|
| `agent_config_ref` | `AGENTS.md` | low | The repo's own agent instructions; the pattern scores *modification* of agent config, and naming the file is informational. |
| `agent_config_ref` | `docs/plan/architecture.md` | low | Architecture prose cites the repo's agent rules while explaining invariants. |
| `agent_config_ref` | `docs/release/scanner-allowlist.txt` | low | This evidence file names the pattern it explains. |
| `agent_config_ref` | `docs/release/scanner.md` | low | This file names the pattern it explains. |
| `python_os_environ` | `docs/plan/architecture.md` | medium | A fenced code block showing how the cron wrapper passes `HERMES_HOME`; design prose, not runtime code. |
| `python_subprocess` | `docs/plan/architecture.md` | medium | Same code block: the wrapper invokes `hermes reset-rescue fire`. |
| `python_subprocess` | `tests/…` (twelve sites) | medium | Tests that cannot prove their contract in-process: the guard's own allow-list check, the floor probe running `bash`/`git` on a synthetic history, and the run-lock test that needs a real second process. The guard forces each to declare `allow_subprocess` in code. |

The `tests/` row is a **category** row (`python_subprocess|tests/`), not a
per-file list: every lane adds tests, and a per-file list would turn each new
test into a false alarm. Offline suites declare allowed processes at the call
site. Opt-in native ticker/preparation fixtures instead verify temporary CLI
homes and read-only real-home/install mounts before launch; the ticker also
requires loopback-only networking. The ticker uses `socket.if_nameindex()` and
mount flags rather than reading process files, so no process-access exemption
is needed.

Rows outside `docs/` are pinned in
[`scanner-allowlist.txt`](scanner-allowlist.txt) so a *new* finding fails
`--strict` instead of blending in. A row that stops matching also fails: stale
evidence would let a real finding hide behind an old excuse.

## Result on the full candidate tree (2026-11-08)

Candidate built from the integration tip (`18e76b5`) with the pre-publication fixes applied.

```
$ python3 <RUNTIME_WRAPPER> scripts/scan_repo.py --strict
33 finding(s), 0 unexplained
RESULT: SAFE
```

Every finding is marked `explained` in the scanner output. This is the repository scanner, not `hermes plugins validate` at the public pin. Install-time validation at the pinned commit still has to run.

## What this means for the catalog PR

- **Rule 7 (install scanner):** the tree is `safe`, not `caution`. Nothing to
  explain in the PR beyond this file.
- **Rule 17 (no unscannable payloads):** no binary, no archive, no bytecode.
  `scripts/scan_repo.py` reports structural findings too, and none appeared.
- **Rule 13 (risky behaviour disclosed):** the scanner does **not** flag the
  pool read, because the read goes through Hermes' own
  `read_credential_pool` and no credential literal appears in the tree. The
  disclosure obligation is therefore a *reviewer* obligation, not a scanner
  one — the README block (T-44) and the catalog `description` carry it.

## Request for L3 (host lane)

`plugin.yaml` does not exist on `integration` yet, so `hermes plugins validate`
against the repo root stops at `✗ manifest — no plugin.yaml`. Once L3 lands the
manifest, re-run:

```bash
export HERMES_HOME=<HERMES_HOME>/cache/scratch/rr-l5/home-s11
hermes plugins validate .            # expect: Validation passed.
python3 .../run_runtime_script.py scripts/scan_repo.py --strict
```

The two checks that will move for the real manifest are *declared tools/hooks
match registrations* and *config schema*; both are L3's contract, not L5's.

## Reproduce

```bash
SK=<HERMES_HOME>/cache/scratch/rr-l5/skeleton
export HERMES_HOME=<HERMES_HOME>/cache/scratch/rr-l5/home-s11
hermes plugins validate "$SK"
python3 <RUNTIME_WRAPPER> \
    "$PWD/scripts/scan_repo.py" --strict
```

The skeleton is scratch state, not a repo artifact: rebuild it from this file's
description (manifest + empty `register()` + `desktop/plugin.js` + the wrapper)
if it is gone.
