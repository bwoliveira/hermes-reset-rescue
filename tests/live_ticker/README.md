# Native ticker validation

This is opt-in and not a release-gate step. It needs Linux mount/network
namespace privileges (usually root), the installed Hermes launcher and its
committed dependency state. No real profile is used for CLI commands.

## One-time dependency preparation

```sh
bash scripts/ticker_prepare.sh
bash scripts/ticker_prepare.sh --check
```

Preparation uses a private mount namespace with the real home/install read-only,
a verified `/var/tmp/rr-rt/disable-seed/home`, private tool locks and copied uv
cache. It runs real `hermes plugins disable reset-rescue` to publish an authentic
core-only dependency generation. It may access PyPI/GitHub to realize native PM
dependencies; it does not scan accounts, start a gateway or contact Codex.
Existing preparation directories are refused, never deleted. A different output
root can be selected with `RR_TICKER_PREP_ROOT`.

Why prepare: native disable is an explicit PM admission transaction, so
`HERMES_DISABLE_LAZY_INSTALLS=1` does not suppress a dependency rebuild. A cold
seed made from an unrelated installed plugin union fails in a loopback-only
namespace. The prerequisite is genuine committed state, not a patched PM,
hand-edited fingerprint, fake dependency response or a mocked disable.

## Run the validation

```sh
RUNTIME_WRAPPER=/path/to/run_runtime_script.py bash scripts/ticker_check.sh
```

The script takes about four minutes, unshares mount **and** network namespaces,
protects the real home/install and prepared seed with read-only mounts, and
verifies the resolved temp home before every CLI invocation (including absolute
launchers recorded by wrappers). It cleans only its unique throwaway directory.
Override `RR_TICKER_SEED_STATE` to use another genuinely prepared install-state
directory; `RR_TICKER_ROOT` selects the output root, always below `/var/tmp`.

The actual `InProcessCronScheduler` claims and dispatches the generated wrappers;
ledger source must be `builtin`. There is no CLI scan or direct wrapper fire.
Only temporary persisted due times, fake credit expiries, GC deadlines and quota
cooldowns are accelerated. Primary cleanup follows the native backup no-op and
a native scan after the I7 timeout + margin; it is not immediate self-deletion.
Local success notices are enqueued in the outbox and then marked `skipped` by
the real local notifier, never sent. Disabled cron definitions remain but both
primary and scan lose command dispatch and make zero HTTP calls. Uninstall
removes only owned jobs/wrappers and retains the foreign-prefix canary.

Evidence goes to `/var/tmp/rr-rt/ticker-final.log`, `cli-homes-final.log` and
`e2e-matrix-final.md`. Failed runs emit FAIL for scenarios they did not complete.
The release gate retains its original fast end-to-end check and ten-row summary.
