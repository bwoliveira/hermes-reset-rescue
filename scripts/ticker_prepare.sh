#!/usr/bin/env bash
# One-time dependency preparation, with network (PyPI), never a ticker scenario.
set -euo pipefail
# Live Hermes locations, overridable per machine. Defaults: the current user's
# ~/.hermes profile and the standard install. Both are mounted read-only.
LIVE_HERMES_HOME="${RR_LIVE_HERMES_HOME:-$HOME/.hermes}"
HERMES_INSTALL_DIR="${RR_HERMES_INSTALL:-/usr/local/lib/hermes-agent}"
export RR_LIVE_HERMES_HOME="$LIVE_HERMES_HOME" RR_HERMES_INSTALL="$HERMES_INSTALL_DIR"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(realpath -m -- "${RR_TICKER_PREP_ROOT:-/var/tmp/rr-rt/disable-seed}")"
case "$ROOT/" in /var/tmp/*/) ;; *) echo 'preparation must be below /var/tmp' >&2; exit 2;; esac
export RR_TICKER_PREP_ROOT="$ROOT"
export TMPDIR=/var/tmp/rr-rt/tmp TEMP=/var/tmp/rr-rt/tmp TMP=/var/tmp/rr-rt/tmp
export PYTHONDONTWRITEBYTECODE=1 HERMES_DISABLE_LAZY_INSTALLS=1 HERMES_PROFILE=''
mkdir -p "$TMPDIR"
if [ "${1:-}" != --isolated ]; then
  exec unshare --mount bash "$0" --isolated "$@"
fi
shift
mount --make-rprivate /
for protected in "$LIVE_HERMES_HOME" "$HERMES_INSTALL_DIR"; do
  mount --bind "$protected" "$protected"
  mount -o remount,bind,ro "$protected"
done
python3 "$REPO/tests/live_ticker/prepare_seed.py" "$@"
