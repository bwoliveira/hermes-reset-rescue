#!/usr/bin/env bash
# Reset Rescue — offline test entry point (T-48).
#
#   scripts/test.sh                 # every Python suite + the Desktop suite
#   scripts/test.sh engine host     # a subset of the Python suites
#   scripts/test.sh --list          # show which suites exist
#   scripts/test.sh --python-only   # skip tests/desktop
#   scripts/test.sh --desktop-only  # skip the Python suites
#
# Python suites run under the Hermes runtime through the audited wrapper, never
# with a bare interpreter: the plugin imports `hermes_cli`/`agent` modules and
# only the runtime's own Python has them. The wrapper also installs the offline
# guard (scripts/offline_guard.py): any socket, unexpected subprocess or write
# into the real Hermes profile fails the run.
#
# Desktop tests run with `node --test`, no network installs.
#
# **HERMES_HOME is unset for the whole run.** An inherited value (a lane's e2e
# run exports one, and a shell that sourced it keeps it) would make the offline
# guard protect that scratch home instead of the real profile, so
# `test_offline_guard.RealProfileWriteBlocked` would pass or fail depending on
# who ran it last. The suites always build their own temp homes.
set -uo pipefail
unset HERMES_HOME

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The audited wrapper is required for Python suites; --list and --help stay portable.
RUNTIME_WRAPPER="${RUNTIME_WRAPPER:-}"
PY_SUITES=()
RUN_PYTHON=1
RUN_DESKTOP=1
LIST_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --python-only) RUN_DESKTOP=0 ;;
    --desktop-only) RUN_PYTHON=0 ;;
    --list) LIST_ONLY=1 ;;
    -h|--help)
      sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*)
      echo "test.sh: unknown option $arg" >&2
      exit 2 ;;
    *) PY_SUITES+=("$arg") ;;
  esac
done

# Without the Hermes runtime the suites fail with a wall of ImportErrors. Stop once,
# with the instruction, before any suite runs (exit 2). Listing needs no runtime.
if [ "$LIST_ONLY" = 0 ] && { [ -z "$RUNTIME_WRAPPER" ] || [ ! -f "$RUNTIME_WRAPPER" ]; }; then
  echo 'Reset Rescue tests require the Hermes runtime; run: RUNTIME_WRAPPER=/path/to/run_runtime_script.py bash scripts/check.sh' >&2
  exit 2
fi

if [ "$LIST_ONLY" = 1 ]; then
  for suite in engine flows host api integration; do
    [ -d "$REPO/tests/$suite" ] && echo "$suite"
  done
  exit 0
fi

# Python suites always use the audited wrapper, never a guessed interpreter.
RUNNER=(python3 "$RUNTIME_WRAPPER")
RUNNER_LABEL="Hermes runtime via RUNTIME_WRAPPER"
"${RUNNER[@]}" "$REPO/scripts/run_tests.py" --check-runtime || exit 2

status=0
ran_something=0

if [ "$RUN_PYTHON" = 1 ]; then
  ran_something=1
  echo "### Python suites ($RUNNER_LABEL)"
  "${RUNNER[@]}" "$REPO/scripts/run_tests.py" "${PY_SUITES[@]+"${PY_SUITES[@]}"}"
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "### Python suites FAILED (exit $rc)"
    status=1
  fi
fi

if [ "$RUN_DESKTOP" = 1 ]; then
  # The suite files are `.test.mjs` (ES modules). A `*.test.js` glob matches
  # nothing here, and `node --test <dir>` on Node 26 tries to load the directory
  # as a single CJS entry and fails — so the files are named explicitly.
  desktop_tests=()
  for candidate in "$REPO"/tests/desktop/*.test.mjs "$REPO"/tests/desktop/*.test.js; do
    [ -f "$candidate" ] && desktop_tests+=("$candidate")
  done
  if [ ${#desktop_tests[@]} -gt 0 ]; then
    if ! command -v node > /dev/null 2>&1; then
      echo "### Desktop suite SKIPPED: node not found" >&2
      status=1
    else
      ran_something=1
      echo
      echo "### Desktop suite (node --test, ${#desktop_tests[@]} file(s))"
      # Bounded: address space, heap, per-test and whole-run time (node_test.sh).
      ( cd "$REPO" && bash scripts/node_test.sh "${desktop_tests[@]}" )
      rc=$?
      if [ $rc -ne 0 ]; then
        echo "### Desktop suite FAILED (exit $rc)"
        status=1
      fi
    fi
  fi
fi

if [ "$ran_something" = 0 ]; then
  echo "test.sh: nothing to run (no suite directories yet)" >&2
  exit 2
fi

echo
if [ "$status" -eq 0 ]; then
  echo "test.sh: OK"
else
  echo "test.sh: FAIL"
fi
exit "$status"
