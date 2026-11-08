#!/usr/bin/env bash
# Reset Rescue — bounded `node --test` for the Desktop suite.
#
#   scripts/node_test.sh tests/desktop/a.test.mjs [more files...]
#
# A runaway desktop test once grew one process past the 80 GB host (a failing
# assert.equal on a React-owned element: Node's assert formats it with
# inspect(depth: 1000), which walks the whole fiber tree). The work is
# synchronous, so neither --test-timeout nor the V8 heap flag stops it in time.
# Every desktop run therefore gets three bounds:
#
#   * ulimit -v      per-process address space (RR_NODE_VMEM_KB, default 2 GiB);
#                    the allocation fails and the file fails within seconds
#   * --max-old-space-size=1024 and --test-timeout=60000 for a bloated or
#                    hung async test (the runner passes both to each file)
#   * timeout        the whole run (RR_NODE_TIMEOUT_S, default 600 s)
#
# Set RR_NODE_VMEM_KB=unlimited to drop the address-space cap (not advised).
set -uo pipefail

[ $# -gt 0 ] || { echo "node_test.sh: no test files" >&2; exit 2; }

VMEM_KB="${RR_NODE_VMEM_KB:-2097152}"
TIMEOUT_S="${RR_NODE_TIMEOUT_S:-600}"

if ! ulimit -v "$VMEM_KB" 2> /dev/null; then
  echo "node_test.sh: warning: could not set ulimit -v $VMEM_KB; running without an address-space cap" >&2
fi

cmd=(node --max-old-space-size=1024 --test --test-timeout=60000 "$@")
if command -v timeout > /dev/null 2>&1; then
  exec timeout --kill-after=10 "$TIMEOUT_S" "${cmd[@]}"
fi
echo "node_test.sh: warning: no timeout(1); running without a wall-clock cap" >&2
exec "${cmd[@]}"
