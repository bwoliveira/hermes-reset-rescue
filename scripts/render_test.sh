#!/usr/bin/env bash
# Reset Rescue — bounded real-browser layout test (tests/desktop/render).
#
#   scripts/render_test.sh
#
# The render test drives a real headless Chromium, so it cannot run under the
# `ulimit -v` cap that node_test.sh sets: Chromium reserves a large virtual
# address space at start. It is bounded here instead by:
#
#   * timeout        the whole run (RR_RENDER_TIMEOUT_S, default 300 s)
#   * a private scratch dir per run, which holds the test's scratch files and
#     Chromium's profile; on exit every process whose command line names that
#     dir is killed, so no Chromium of this run survives. Other Chromium
#     processes on the host are not matched.
#
# Exit codes follow node --test: 0 all pass, 1 a check failed or timed out.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TIMEOUT_S="${RR_RENDER_TIMEOUT_S:-300}"
BASE="${RR_SCRATCH:-${TMPDIR:-/var/tmp}}"

WORK="$(mktemp -d -p "$BASE" rr-render.XXXXXX)" || exit 2
cleanup() {
  pkill -KILL -f -- "$WORK" 2> /dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

cd "$REPO" || exit 2
RR_SCRATCH="$WORK" timeout --kill-after=10 "$TIMEOUT_S" \
  node --max-old-space-size=1024 --test --test-timeout=60000 tests/desktop/render/popup-layout.test.mjs
