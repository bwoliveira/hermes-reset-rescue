#!/usr/bin/env bash
# Opt-in Linux validation. Never part of check.sh/release_gate.sh.
set -euo pipefail
# Live Hermes locations, overridable per machine. Defaults: the current user's
# ~/.hermes profile and the standard install. Both are mounted read-only.
LIVE_HERMES_HOME="${RR_LIVE_HERMES_HOME:-$HOME/.hermes}"
HERMES_INSTALL_DIR="${RR_HERMES_INSTALL:-/usr/local/lib/hermes-agent}"
export RR_LIVE_HERMES_HOME="$LIVE_HERMES_HOME" RR_HERMES_INSTALL="$HERMES_INSTALL_DIR"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(realpath -m -- "${RR_TICKER_ROOT:-/var/tmp/rr-rt}")"
case "$ROOT/" in /var/tmp/*/) ;; *) echo 'ticker: scratch must resolve below /var/tmp' >&2; exit 2;; esac
export TMPDIR="$ROOT/tmp" TEMP="$ROOT/tmp" TMP="$ROOT/tmp"
mkdir -p "$TMPDIR"
if [ "${1:-}" != --isolated ]; then
  command -v unshare >/dev/null
  exec unshare --mount --net bash "$0" --isolated "$@"
fi
shift
mount --make-rprivate /
for protected in "$LIVE_HERMES_HOME" "$HERMES_INSTALL_DIR"; do
  mount --bind "$protected" "$protected"
  mount -o remount,bind,ro "$protected"
done
# Native disable explicitly admits a dependency selection. The seed must be a
# real, pre-published empty-member generation (see tests/live_ticker/README.md),
# not an edited fingerprint or a stubbed PM response. Keep that input immutable.
export RR_E2E_SEED_STATE="$(realpath -e -- "${RR_TICKER_SEED_STATE:-/var/tmp/rr-rt/disable-seed/home/installs/76f6e7e5145b5f39}")"
case "$RR_E2E_SEED_STATE/" in /var/tmp/*/) ;; *) echo 'ticker: prepared seed must be under /var/tmp' >&2; exit 2;; esac
mount --bind "$RR_E2E_SEED_STATE" "$RR_E2E_SEED_STATE"
mount -o remount,bind,ro "$RR_E2E_SEED_STATE"
ip link set lo up
export RR_TICKER_ISOLATED=1 RR_TICKER_ROOT="$ROOT"
export HERMES_DISABLE_LAZY_INSTALLS=1 HERMES_PROFILE='' PYTHONDONTWRITEBYTECODE=1
export UV_OFFLINE=1 PIP_NO_INDEX=1
WORK="$(mktemp -d -p "$ROOT" ticker-run.XXXXXX)"
trap 'rm -rf -- "$WORK"' EXIT
export HERMES_HOME="$WORK/bootstrap-home"
export RR_TICKER_HOME="$WORK/ticker-home" RR_TEMP_ROOT="$WORK/homes" RR_SCRATCH="$WORK/scratch"
export RR_TICKER_REAL_LAUNCHER="$WORK/real-hermes"
export RR_TICKER_GUARD="$REPO/tests/live_ticker/checked_cli.py"
cp "$HERMES_INSTALL_DIR/.hermes/bin/hermes" "$RR_TICKER_REAL_LAUNCHER"
mkdir -p "$WORK/bin"
printf '#!/bin/sh\nexec /usr/bin/python3 -I -B "$RR_TICKER_GUARD" "$@"\n' > "$WORK/bin/hermes"
chmod +x "$WORK/bin/hermes" "$RR_TICKER_REAL_LAUNCHER"
# Cover absolute recorded launchers too, without changing the installed file.
mount --bind "$WORK/bin/hermes" "$HERMES_INSTALL_DIR/.hermes/bin/hermes"
mount -o remount,bind,ro "$HERMES_INSTALL_DIR/.hermes/bin/hermes"
export PATH="$WORK/bin:$PATH" HERMES_BIN="$WORK/bin/hermes"
export RR_TICKER_CLI_LOG="$ROOT/cli-homes-final.log"
: > "$RR_TICKER_CLI_LOG"
python3 "$REPO/tests/live_ticker/checked_cli.py" --check-only
python3 "$REPO/tests/e2e/seed.py" --seed "$HERMES_HOME"
WRAPPER="${RUNTIME_WRAPPER:?set RUNTIME_WRAPPER to the audited run_runtime_script.py}"
LOG="$ROOT/ticker-final.log"
set +e
python3 "$WRAPPER" "$REPO/tests/live_ticker/run_ticker_e2e.py" "$@" 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e
python3 "$REPO/tests/live_ticker/report.py" "$LOG" "$ROOT/e2e-matrix-final.md" "$rc"
exit "$rc"
