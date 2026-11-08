#!/usr/bin/env bash
# Reset Rescue — the release gate, proven against a READ-ONLY real profile.
#
#   scripts/release_gate.sh            # the whole gate, read-only real profile
#   scripts/release_gate.sh --release  # same, release mode (no n/a, no baseline)
#
# Why this wrapper exists (R17)
# -----------------------------
# `check.sh` already unsets HERMES_HOME and moves TMPDIR outside the profile, but
# "we unset it" is a claim about our own code. This wrapper removes the
# possibility instead: the real profile is bind-mounted **read-only** into a
# private mount namespace, so any write into it fails with EROFS — including one
# from a path nobody thought to guard (a tool, a dependency, a future step).
#
# It also removes the network (`--unshare-net`), so a dependency install or a
# stray request fails instead of quietly succeeding on this machine's
# connectivity.
#
# Install metadata, locks and generation leases are seeded into each temporary
# home. There are NO writable exceptions anywhere under the real profile.
#
# Exit codes: 0 the gate passed, 1 the gate failed, 2 the wrapper cannot run.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REAL_HOME="${REAL_HOME:-$HOME/.hermes}"

if ! command -v bwrap > /dev/null 2>&1; then
  echo "release_gate: bubblewrap (bwrap) is required to make the real profile read-only" >&2
  exit 2
fi
if [ ! -d "$REAL_HOME" ]; then
  echo "release_gate: no real profile at $REAL_HOME" >&2
  exit 2
fi

# A writable scratch root outside the profile: the gate's TMPDIR, the e2e home
# and every suite's temp state live here, never in $HOME. Resolve the override
# before mktemp so even a symlink cannot place the scratch in the live profile.
WORK_ROOT="$(realpath -m -- "${RR_GATE_TMP_ROOT:-/var/tmp}")" || exit 2
REAL_PROFILE="$(realpath -e -- "$REAL_HOME")" || exit 2
NATIVE_PROFILE="$(realpath -m -- "$HOME/.hermes")" || exit 2
case "$WORK_ROOT/" in
  "$REAL_PROFILE/"*|"$NATIVE_PROFILE/"*)
    echo "release_gate: refusing scratch inside the real profile: $WORK_ROOT" >&2
    exit 2 ;;
esac
WORK="$(mktemp -d -p "$WORK_ROOT" rr-release-gate.XXXXXX)" || exit 2
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/tmp" "$WORK/scratch" "$WORK/ruff-cache" "$WORK/mypy-cache"

# The gate lints and type-checks the tree, and both tools write a cache beside
# it. Under a read-only bind mount that fails with EROFS for a reason that has
# nothing to do with the code, so the tree is copied into the writable scratch
# and the gate runs on the copy. The copy is what a reviewer's fresh clone is.
TREE="$WORK/tree"
if ! cp -a "$REPO/." "$TREE" 2>/dev/null; then
  echo "release_gate: cannot copy the tree into $TREE" >&2
  exit 2
fi

echo "release_gate: real profile $REAL_HOME is READ-ONLY; network is unshared"
echo "release_gate: scratch $WORK (TMPDIR=$WORK/tmp)"

bwrap \
  --ro-bind / / \
  --dev /dev \
  --proc /proc \
  --ro-bind "$REAL_HOME" "$REAL_HOME" \
  --bind "$WORK" "$WORK" \
  --unshare-net \
  --die-with-parent \
  --chdir "$REPO" \
  --setenv TMPDIR "$WORK/tmp" \
  --setenv TEMP "$WORK/tmp" \
  --setenv TMP "$WORK/tmp" \
  --setenv RR_GATE_TMP_ROOT "$WORK" \
  --setenv RR_TEMP_ROOT "$WORK" \
  --setenv RR_SCRATCH "$WORK/scratch" \
  --setenv RR_E2E_HOME "$WORK/scratch/e2e-home" \
  --setenv RUFF_CACHE_DIR "$WORK/ruff-cache" \
  --setenv MYPY_CACHE_DIR "$WORK/mypy-cache" \
  --setenv PYTHONDONTWRITEBYTECODE 1 \
  --setenv HERMES_DISABLE_LAZY_INSTALLS 1 \
  --setenv RR_ORIGINAL_RUNTIME_WRAPPER "${RUNTIME_WRAPPER:-}" \
  --setenv RUNTIME_WRAPPER "$TREE/tests/e2e/gate_runtime.py" \
  --unsetenv RR_E2E_SEED_STATE \
  --unsetenv HERMES_RUNTIME_DIR \
  --unsetenv HERMES_HOME \
  env -u HERMES_HOME bash "$TREE/scripts/check.sh" "$@"
