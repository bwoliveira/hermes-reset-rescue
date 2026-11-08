#!/usr/bin/env bash
# Reset Rescue — the finish gate (D-019).
#
#   scripts/check.sh              # every step (build mode: baselines allowed)
#   scripts/check.sh --release    # release mode: baselines must be empty,
#                                 #   format is required, nothing is n/a
#   scripts/check.sh --list       # name the steps and exit
#   scripts/check.sh --no-e2e     # skip the end-to-end suite (slowest)
#   scripts/check.sh --fix        # let ruff apply its safe fixes first
#
# One line per step, then a summary. Exits 0 only when every step passed.
#
# Steps
#   1. unit        — the offline Python suites (scripts/test.sh --python-only)
#   2. desktop     — node --test on tests/desktop/*.test.mjs (scripts/test.sh --desktop-only)
#   2b. render     — real-browser popup layout (scripts/render_test.sh, headless Chromium)
#   3. js          — node --check on every desktop/*.js and the SDK static checks
#   4. lint        — ruff check (PEP 8 + pyflakes + import order + bugbear)
#   5. format      — ruff format --check
#   6. types       — mypy over reset_rescue/ (strict-ish, Hermes imports ignored)
#   7. integration — temp-HERMES_HOME discovery, `hermes plugins validate`/`doctor`
#   8. e2e         — the real `hermes` CLI + the real transport against a loopback fake
#   9. tree        — no binaries, bytecode, archives or escaping symlinks
#
# Tool resolution: ruff and mypy are NOT installed into Hermes' interpreter and
# must never be. They live in a PM side environment created with
# `hermes pm environment rr-l5-dev --requirement ...`; this script asks PM for
# that interpreter's path and falls back to PATH. `--fix` is the only mode that
# writes to the tree, and it only ever runs `ruff check --fix`.
#
# Baseline: the other lanes' modules carry pre-existing findings this lane does
# not own. `docs/release/lint-baseline.txt` records them per file so the gate
# fails on NEW findings only; the counts are printed every run so they cannot be
# forgotten. Regenerate deliberately with `scripts/check.sh --write-baseline`.
#
# Skips are not passes. A step that cannot run (a missing tool, a missing
# manifest) is recorded as CANNOT and fails the gate: a gate that shrugs at a
# step it could not execute is not a gate. The only non-passing outcome that
# does not fail is `n/a`, which is reserved for the two cases documented at
# their call sites (`format` while other lanes' files still land, and an absent
# optional input). `--no-<step>` drops a step deliberately and is visible in the
# command line.
set -uo pipefail
unset HERMES_HOME

# Without the Hermes runtime every step that imports hermes_cli fails at once. Stop
# before the temp root is created, with the instruction (exit 2). --list and --help
# need no runtime.
case " $* " in
  *" --list "*|*" -h "*|*" --help "*) ;;
  *)
    if [ -z "${RUNTIME_WRAPPER:-}" ] || [ ! -f "${RUNTIME_WRAPPER:-}" ]; then
      echo 'Reset Rescue tests require the Hermes runtime; run: RUNTIME_WRAPPER=/path/to/run_runtime_script.py bash scripts/check.sh' >&2
      exit 2
    fi
    python3 "$RUNTIME_WRAPPER" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run_tests.py" --check-runtime || exit 2
    ;;
esac

# **HERMES_HOME was unset before preflight for the whole gate.** An inherited value
# (from a lane's e2e run, or a shell that sourced one) would point the offline
# guard at that scratch home instead of the real profile, and make the
# PM-environment lookup below read the wrong root. Each step builds its own home.

# **TMPDIR is moved outside the real profile for the whole gate.** In this
# environment TMPDIR is <HERMES_HOME>/cache/scratch, so `tempfile.mkdtemp()`,
# `mktemp` and Node's `os.tmpdir()` all create directories INSIDE the real
# profile — which Hermes then resolves to ~/.hermes. A step that only guarded its
# HERMES_HOME still wrote real state (or, against a read-only profile, died with
# EROFS for the wrong reason). Every child inherits this value: the unit suites,
# node --test, the desktop harness, the e2e and the tools.
#
# `RR_GATE_TMP_ROOT` names the parent: `/var/tmp` normally, and the wrapper's own
# writable scratch when the whole filesystem is read-only (scripts/release_gate.sh
# bind-mounts the real profile read-only, which also freezes /var/tmp).
GATE_TMP_ROOT="${RR_GATE_TMP_ROOT:-/var/tmp}"
RR_GATE_TMP="$(mktemp -d -p "$GATE_TMP_ROOT" rr-gate.XXXXXX)" || {
  echo "check.sh: cannot create a temp root outside the real profile ($GATE_TMP_ROOT)" >&2
  exit 2
}
export TMPDIR="$RR_GATE_TMP"
export TEMP="$RR_GATE_TMP"
export TMP="$RR_GATE_TMP"
trap 'rm -rf "$RR_GATE_TMP"' EXIT

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASELINE="$REPO/docs/release/lint-baseline.txt"
TYPES_BASELINE="$REPO/docs/release/types-baseline.txt"
# The types baseline is expected to stay empty; the note says why, and is
# rewritten by every green run so the file keeps its explanation without
# carrying entries (and without being deleted: see `baseline_header`).
TYPES_NOTE="Empty: every module is mypy-clean under \`--explicit-package-bases\`. A new
error anywhere fails the gate, which is the point of the file being empty."
# Optional audited wrapper; empty means the Hermes interpreter or python3 (see RUNNER).
RUNTIME_WRAPPER="${RUNTIME_WRAPPER:-}"
PM_ENV="${RR_PM_ENV:-rr-l5-dev}"

RUN_UNIT=1
RUN_DESKTOP=1
RUN_RENDER=1
RUN_JS=1
RUN_LINT=1
RUN_FORMAT=1
RUN_TYPES=1
RUN_INTEGRATION=1
RUN_E2E=1
RUN_TREE=1
FIX=0
WRITE_BASELINE=0
RELEASE=0

for arg in "$@"; do
  case "$arg" in
    --list)
      printf '%s\n' unit desktop render js lint format types integration e2e tree
      exit 0 ;;
    --no-e2e) RUN_E2E=0 ;;
    --no-unit) RUN_UNIT=0 ;;
    --no-desktop) RUN_DESKTOP=0 ;;
    --no-render) RUN_RENDER=0 ;;
    --no-js) RUN_JS=0 ;;
    --no-lint) RUN_LINT=0 ;;
    --no-format) RUN_FORMAT=0 ;;
    --no-types) RUN_TYPES=0 ;;
    --no-integration) RUN_INTEGRATION=0 ;;
    --no-tree) RUN_TREE=0 ;;
    --fix) FIX=1 ;;
    --release) RELEASE=1 ;;
    --write-baseline) WRITE_BASELINE=1 ;;
    -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "check.sh: unknown option $arg" >&2; exit 2 ;;
  esac
done

# --------------------------------------------------------------------- report
PASS=0
FAIL=0
SKIP=0
NA=0
FAILED_STEPS=()
SUMMARY=()

# `record <step> <status> <detail>`
#
# Three outcomes, and the difference between the last two is the point:
#   ok    — the step ran and passed.
#   na    — the step is **documented not applicable** to this tree (see N/A
#           below). It does not count as a pass and does not fail the gate; the
#           summary prints it separately so it can never be mistaken for green.
#   skip  — the step could not run (a missing tool, a missing manifest). That is
#           a **failure**: a gate that shrugs at a step it could not execute is
#           not a gate. Use `--no-<step>` to drop a step deliberately, which is
#           visible on the command line in the log.
record() {
  local step="$1" status="$2" detail="${3:-}"
  case "$status" in
    ok)   PASS=$((PASS + 1)); SUMMARY+=("$(printf '  %-12s ok      %s' "$step" "$detail")") ;;
    na)   NA=$((NA + 1)); SUMMARY+=("$(printf '  %-12s n/a     %s' "$step" "$detail")") ;;
    fail) FAIL=$((FAIL + 1)); FAILED_STEPS+=("$step")
          SUMMARY+=("$(printf '  %-12s FAIL    %s' "$step" "$detail")") ;;
    skip) FAIL=$((FAIL + 1)); FAILED_STEPS+=("$step")
          SUMMARY+=("$(printf '  %-12s CANNOT  %s' "$step" "$detail")") ;;
  esac
}

banner() { printf '\n=== %s ===\n' "$1"; }

# `baseline_growth <current "file count" lines> <reference baseline>`
#
# A baseline may shrink (a lane fixed its files) but must never grow **because
# of this branch**. Growth is measured against the baseline `origin/integration`
# ships, and only for files this branch actually changed: a finding in a file
# nobody touched here was already there, and blaming this branch for it would
# make the guard cry wolf until every lane's baseline caught up.
#
# Two things it catches that the per-file comparison cannot:
#   * a file that got *worse* (count higher than integration's);
#   * a baseline regenerated carelessly, which silently re-blesses new findings.
baseline_growth() {
  local current="$1" reference="$2" grown=""
  [ -f "$reference" ] || return 0
  # `git diff` and `awk` read stdin; without the redirect they would eat the
  # loop's own input and the loop would silently stop after the first row.
  while read -r path count; do
    [ -n "$path" ] || continue
    case "$path" in \#*) continue ;; esac
    if git -C "$REPO" rev-parse --git-dir > /dev/null 2>&1 \
       && git -C "$REPO" rev-parse --verify -q origin/integration > /dev/null 2>&1 \
       && git -C "$REPO" diff --quiet origin/integration -- "$path" < /dev/null 2>/dev/null; then
      continue  # untouched by this branch: its findings are integration's
    fi
    local before
    before="$(awk -v f="$path" '$1 == f {print $2}' "$reference" < /dev/null 2>/dev/null | head -1)"
    before="${before:-0}"
    if [ "$count" -gt "$before" ]; then
      grown="$grown $path($count>$before)"
    fi
  done < "$current"
  printf '%s' "$grown"
}

# `baseline_header <path> <title>`
#
# A green run must never *delete* a tracked baseline. `rm -f` on a tracked file
# leaves the worktree dirty after every clean run, so the next `git status`
# reports a deletion nobody made and a lane's commit picks it up by accident.
# The file is rewritten with its header and no entries instead: "empty" is the
# state the release gate asks for, and an empty file is not the same thing as an
# absent one (the growth guard also reads the file, not the index).
baseline_header() {
  local path="$1" title="$2" note="${3:-}"
  {
    echo "# Reset Rescue — $title"
    echo "# Format: <file> <count>. Regenerate: scripts/check.sh --write-baseline"
    echo "# A file whose count drops is fine; a NEW file or a higher count fails."
    if [ -n "$note" ]; then
      echo "#"
      printf '%s\n' "$note" | while IFS= read -r line; do echo "# $line"; done
    fi
  } > "$path"
}

# The committed baseline, materialised once so growth is measured against what
# integration actually ships rather than against the file this run may rewrite.
BASELINE_REF=""
TYPES_BASELINE_REF=""
materialise_baselines() {
  local work="$1"
  BASELINE_REF="$work/lint-baseline.txt"
  TYPES_BASELINE_REF="$work/types-baseline.txt"
  # No origin/integration ref (a public export or a fresh clone): the baseline is
  # empty, so every finding counts as growth. Release mode already requires zero.
  if git -C "$REPO" rev-parse --verify --quiet origin/integration > /dev/null 2>&1; then
    git -C "$REPO" show origin/integration:docs/release/lint-baseline.txt > "$BASELINE_REF" 2>/dev/null || : > "$BASELINE_REF"
    git -C "$REPO" show origin/integration:docs/release/types-baseline.txt > "$TYPES_BASELINE_REF" 2>/dev/null || : > "$TYPES_BASELINE_REF"
  else
    : > "$BASELINE_REF"
    : > "$TYPES_BASELINE_REF"
  fi
}

# ---------------------------------------------------------------- tool lookup
# `hermes pm environment NAME` builds/reuses the environment but refuses to run
# without `--extra`/`--requirement`, so the *recorded* generation is read from
# `$HERMES_HOME/environments/NAME/active.json` and its interpreter used directly.
# That is the same venv PM would print, without re-resolving dependencies on
# every gate run.
pm_python() {
  local root="${HERMES_HOME:-$HOME/.hermes}"
  local active="$root/environments/$PM_ENV/active.json"
  local generation python
  [ -f "$active" ] || return 0
  generation="$(sed -n 's/.*"generation"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$active" | head -1)"
  [ -n "$generation" ] || return 0
  python="$root/environments/$PM_ENV/$generation/venv/bin/python"
  [ -x "$python" ] && printf '%s' "$python"
}

TOOL_PY=""
resolve_tools() {
  TOOL_PY="$(pm_python)"
  # The Python suites run through the same interpreter: the optional wrapper,
  # else this PM interpreter, else python3 (scripts/test.sh resolves it the same way).
  RUNNER=(python3)
  if [ -n "$RUNTIME_WRAPPER" ] && [ -f "$RUNTIME_WRAPPER" ]; then
    RUNNER=(python3 "$RUNTIME_WRAPPER")
  elif [ -n "$TOOL_PY" ]; then
    RUNNER=("$TOOL_PY")
  fi
  if [ -z "$TOOL_PY" ]; then
    echo "check.sh: no PM environment '$PM_ENV'; falling back to PATH for ruff/mypy" >&2
    echo "check.sh: create it with: hermes pm environment $PM_ENV \\" >&2
    echo "check.sh:   --requirement 'ruff>=0.6,<0.15' --requirement 'mypy>=1.11,<2'" >&2
  fi
}

tool() {  # tool <name> -> path or empty
  local name="$1"
  if [ -n "$TOOL_PY" ]; then
    local candidate
    candidate="$(dirname "$TOOL_PY")/$name"
    [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
  fi
  command -v "$name" 2>/dev/null || true
}

# ------------------------------------------------------------------ 1. unit
if [ "$RUN_UNIT" = 1 ]; then
  banner "unit tests (offline Python suites)"
  out="$(cd "$REPO" && bash scripts/test.sh --python-only 2>&1)"
  rc=$?
  tail="$(printf '%s\n' "$out" | grep -E '^total: ' | tail -1)"
  if [ $rc -eq 0 ]; then
    record unit ok "${tail:-passed}"
  else
    printf '%s\n' "$out" | tail -40
    record unit fail "${tail:-exit $rc}"
  fi
fi

# --------------------------------------------------------------- 2. desktop
if [ "$RUN_DESKTOP" = 1 ]; then
  banner "desktop tests (node --test)"
  if ! command -v node > /dev/null 2>&1; then
    record desktop skip "node not installed"
  else
    out="$(cd "$REPO" && bash scripts/test.sh --desktop-only 2>&1)"
    rc=$?
    tail="$(printf '%s\n' "$out" | grep -E '^ℹ (tests|pass|fail) ' | tr '\n' ' ' | sed 's/ *$//')"
    if [ $rc -eq 0 ]; then
      record desktop ok "${tail:-passed}"
    else
      printf '%s\n' "$out" | tail -40
      record desktop fail "${tail:-exit $rc}"
    fi
  fi
fi

# ------------------------------------------------------------- 2b. render
# Real-browser layout test (headless Chromium, tests/desktop/render). Its own
# step, because it starts Chromium, which the node --test cap in
# node_test.sh would stop. scripts/render_test.sh bounds it and cleans up.
if [ "$RUN_RENDER" = 1 ]; then
  banner "render tests (real-browser popup layout)"
  if ! command -v node > /dev/null 2>&1; then
    record render skip "node not installed"
  elif [ ! -x /usr/bin/chromium ] && [ -z "${RR_CHROMIUM:-}" ]; then
    record render skip "chromium not installed (set RR_CHROMIUM)"
  else
    out="$(cd "$REPO" && bash scripts/render_test.sh 2>&1)"
    rc=$?
    tail="$(printf '%s\n' "$out" | grep -E '^ℹ (tests|pass|fail) ' | tr '\n' ' ' | sed 's/ *$//')"
    if [ $rc -eq 0 ]; then
      record render ok "${tail:-passed}"
    else
      printf '%s\n' "$out" | tail -40
      record render fail "${tail:-exit $rc}"
    fi
  fi
fi

# -------------------------------------------------------------------- 3. js
if [ "$RUN_JS" = 1 ]; then
  banner "JS syntax + SDK static checks"
  if ! command -v node > /dev/null 2>&1; then
    record js skip "node not installed"
  else
    js_fail=0
    js_files=0
    js_detail=""
    for file in "$REPO"/desktop/*.js; do
      [ -f "$file" ] || continue
      js_files=$((js_files + 1))
      if ! node --check "$file" 2>/dev/null; then
        # A plain `.js` with ESM syntax needs the module parser explicitly.
        if node --input-type=module --check < "$file" 2>/dev/null; then
          continue
        fi
        js_fail=$((js_fail + 1))
        js_detail="$js_detail $(basename "$file")"
      fi
    done
    # The SDK static checks live in the desktop suite's own harness; run the
    # static + strings modules directly so this step stands alone.
    static_out="$(cd "$REPO" && bash scripts/node_test.sh tests/desktop/static.test.mjs \
      tests/desktop/strings.test.mjs 2>&1)"
    if [ $? -ne 0 ]; then
      js_fail=$((js_fail + 1))
      printf '%s\n' "$static_out" | tail -25
      js_detail="$js_detail static-checks"
    fi
    if [ "$js_files" -eq 0 ]; then
      record js na "no desktop/*.js in this tree"
    elif [ "$js_fail" -eq 0 ]; then
      record js ok "$js_files file(s) parse; static + string checks pass"
    else
      record js fail "failed:$js_detail"
    fi
  fi
fi

# ------------------------------------------------------------------ 4. lint
if [ "$RUN_LINT" = 1 ] || [ "$RUN_FORMAT" = 1 ] || [ "$RUN_TYPES" = 1 ]; then
  resolve_tools
  BASELINE_WORK="$(mktemp -d "${TMPDIR:-/tmp}/rr-baseline.XXXXXX")"
  materialise_baselines "$BASELINE_WORK"
  trap 'rm -rf "$BASELINE_WORK"' EXIT
fi

if [ "$RUN_LINT" = 1 ]; then
  banner "lint (ruff)"
  ruff_bin="$(tool ruff)"
  if [ -z "$ruff_bin" ]; then
    record lint skip "ruff not found (create the $PM_ENV PM environment)"
  else
    if [ "$FIX" = 1 ]; then
      ( cd "$REPO" && "$ruff_bin" check --fix . > /dev/null 2>&1 )
    fi
    lint_out="$(cd "$REPO" && "$ruff_bin" check --output-format concise . 2>&1)"
    lint_rc=$?
    findings="$(printf '%s\n' "$lint_out" | grep -cE '^[^ ]+:[0-9]+:[0-9]+: [A-Z]+[0-9]+ ' || true)"
    if [ "$WRITE_BASELINE" = 1 ]; then
      baseline_header "$BASELINE" "pre-existing lint findings this lane does not own."
      printf '%s\n' "$lint_out" \
        | grep -E '^[^ ]+:[0-9]+:[0-9]+: [A-Z]+[0-9]+ ' \
        | cut -d: -f1 | sort | uniq -c | awk '{print $2" "$1}' | sort >> "$BASELINE"
      record lint ok "baseline written: $findings finding(s) across $(wc -l < "$BASELINE") line(s)"
    elif [ "$lint_rc" -eq 0 ]; then
      baseline_header "$BASELINE" "pre-existing lint findings this lane does not own."
      record lint ok "0 findings"
    elif [ "$RELEASE" = 1 ]; then
      # Release mode: a baseline is a build-time crutch, not a shipping state.
      # Every finding must be gone, and the baseline file must be empty.
      printf '%s\n' "$lint_out" | head -60
      record lint fail "$findings finding(s): release mode requires zero (baseline is not allowed)"
    else
      # Compare per-file counts against the baseline: only a new file or a
      # higher count is a regression.
      current="$(printf '%s\n' "$lint_out" \
        | grep -E '^[^ ]+:[0-9]+:[0-9]+: [A-Z]+[0-9]+ ' \
        | cut -d: -f1 | sort | uniq -c | awk '{print $2" "$1}' | sort)"
      regressions=""
      while read -r file count; do
        [ -n "$file" ] || continue
        case "$file" in \#*) continue ;; esac
        allowed="$(awk -v f="$file" '$1 == f {print $2}' "$BASELINE" 2>/dev/null | head -1)"
        allowed="${allowed:-0}"
        if [ "$count" -gt "$allowed" ]; then
          regressions="$regressions $file($count>$allowed)"
        fi
      done <<< "$current"
      grown="$(baseline_growth "$current" "$BASELINE_REF")"
      if [ -n "$grown" ]; then
        printf '%s\n' "$lint_out" | head -60
        record lint fail "baseline grew vs origin/integration:$grown"
      elif [ -z "$regressions" ]; then
        record lint ok "$findings pre-existing finding(s), none new (baseline)"
      else
        printf '%s\n' "$lint_out" | head -60
        record lint fail "new findings:$regressions"
      fi
    fi
  fi
fi

# ---------------------------------------------------------------- 5. format
if [ "$RUN_FORMAT" = 1 ]; then
  banner "format (ruff format --check)"
  ruff_bin="$(tool ruff)"
  if [ -z "$ruff_bin" ]; then
    record format skip "ruff not found (create the $PM_ENV PM environment)"
  else
    fmt_out="$(cd "$REPO" && "$ruff_bin" format --check . 2>&1)"
    if [ $? -eq 0 ]; then
      record format ok "already formatted"
    else
      unformatted="$(printf '%s\n' "$fmt_out" | grep -c '^Would reformat' || true)"
      if [ "$RELEASE" = 1 ]; then
        # Release mode: every file must be formatted. No n/a, no tolerance.
        printf '%s\n' "$fmt_out" | head -40
        record format fail "$unformatted file(s) would reformat: release mode requires a formatted tree"
      else
        # Build mode: other lanes' files are still landing and a repo-wide
        # reformat now would bury their diffs. Reported, deliberately not fatal.
        record format na "$unformatted file(s) would reformat: informational while other lanes' files land (D-019 asks for a format check, not a repo-wide reformat mid-build)"
      fi
    fi
  fi
fi

# ----------------------------------------------------------------- 6. types
if [ "$RUN_TYPES" = 1 ]; then
  banner "types (mypy)"
  mypy_bin="$(tool mypy)"
  if [ -z "$mypy_bin" ]; then
    record types skip "mypy not found (create the $PM_ENV PM environment)"
  else
    types_out="$(cd "$REPO" && "$mypy_bin" --no-error-summary --explicit-package-bases reset_rescue/ 2>&1)"
    types_rc=$?
    errors="$(printf '%s\n' "$types_out" | grep -c 'error:' || true)"
    if [ "$WRITE_BASELINE" = 1 ]; then
      baseline_header "$TYPES_BASELINE" "pre-existing mypy errors this lane does not own." \
        "$TYPES_NOTE"
      printf '%s\n' "$types_out" \
        | grep 'error:' | cut -d: -f1 | sort | uniq -c | awk '{print $2" "$1}' | sort >> "$TYPES_BASELINE"
      record types ok "baseline written: $errors error(s)"
    elif [ "$types_rc" -eq 0 ]; then
      baseline_header "$TYPES_BASELINE" "pre-existing mypy errors this lane does not own." \
        "$TYPES_NOTE"
      record types ok "0 errors"
    elif [ "$RELEASE" = 1 ]; then
      printf '%s\n' "$types_out" | head -40
      record types fail "$errors error(s) (see output): release mode requires zero (baseline is not allowed)"
    else
      current="$(printf '%s\n' "$types_out" \
        | grep 'error:' | cut -d: -f1 | sort | uniq -c | awk '{print $2" "$1}' | sort)"
      regressions=""
      while read -r file count; do
        [ -n "$file" ] || continue
        case "$file" in \#*) continue ;; esac
        allowed="$(awk -v f="$file" '$1 == f {print $2}' "$TYPES_BASELINE" 2>/dev/null | head -1)"
        allowed="${allowed:-0}"
        if [ "$count" -gt "$allowed" ]; then
          regressions="$regressions $file($count>$allowed)"
        fi
      done <<< "$current"
      grown="$(baseline_growth "$current" "$TYPES_BASELINE_REF")"
      if [ -n "$grown" ]; then
        printf '%s\n' "$types_out" | head -40
        record types fail "baseline grew vs origin/integration:$grown"
      elif [ -z "$regressions" ]; then
        record types ok "$errors pre-existing error(s), none new (baseline)"
      else
        printf '%s\n' "$types_out" | head -40
        record types fail "new errors:$regressions"
      fi
    fi
  fi
fi

# ----------------------------------------------------------- 7. integration
if [ "$RUN_INTEGRATION" = 1 ]; then
  banner "integration (temp HERMES_HOME: discovery, validate, doctor)"
  out="$(cd "$REPO" && "${RUNNER[@]}" "$REPO/scripts/run_tests.py" integration 2>&1)"
  rc=$?
  tail="$(printf '%s\n' "$out" | grep -E '^total: ' | tail -1)"
  if [ $rc -eq 0 ]; then
    record integration ok "${tail:-passed}"
  else
    printf '%s\n' "$out" | tail -40
    record integration fail "${tail:-exit $rc}"
  fi
fi

# ------------------------------------------------------------------- 8. e2e
if [ "$RUN_E2E" = 1 ]; then
  banner "end-to-end (real hermes CLI, loopback fake backend, network-isolated)"
  e2e_runner="$REPO/tests/e2e/run_e2e.py"
  if [ ! -f "$e2e_runner" ]; then
    record e2e na "tests/e2e/run_e2e.py not present"
  else
    # R17: the e2e claims to be hermetic, so it is run inside a network
    # namespace with only loopback. `unshare -rn` gives the run its own netns
    # and maps root, so no route to the internet exists at all — a dependency
    # install or a stray request fails instead of quietly succeeding on this
    # machine's connectivity. `ip link set lo up` is required because a fresh
    # netns starts with loopback DOWN, and the fake backend listens on it.
    #
    # The profile itself is created outside $HOME (tests/support/isolation.py):
    # Hermes resolves a HERMES_HOME nested under ~/.hermes back to ~/.hermes, so
    # a "temp profile" under the scratch root WAS the real profile. The runner
    # asserts this before writing anything and aborts otherwise.
    #
    # Without `unshare` (macOS, a restricted container) the run happens on the
    # host and the offline guard is the only barrier; that is recorded as a
    # documented n/a, which --release refuses, so a release candidate is always
    # proven under isolation.
    e2e_prefix=()
    if command -v unshare > /dev/null 2>&1 && unshare -rn true 2>/dev/null; then
      e2e_prefix=(unshare -rn sh -c 'ip link set lo up 2>/dev/null; exec "$@"' sh)
    else
      record e2e na "no usable 'unshare -rn': the e2e cannot be proven network-isolated here"
    fi
    if [ ${#e2e_prefix[@]} -eq 0 ]; then
      :  # already recorded n/a above; do not run an unproven e2e
    else
      # The seed is built from the *installed* Hermes' committed dependency
      # state, never from the network. When it cannot be built the e2e must fail
      # loudly here rather than fall back to an online sync: a gate that cannot
      # prepare its own inputs has not proven anything.
      if ! python3 "$REPO/tests/e2e/seed.py" --check > /dev/null 2>&1; then
        record e2e fail "no offline dependency seed: the installed Hermes has no committed state to copy"
      else
        out="$(cd "$REPO" && "${e2e_prefix[@]}" "${RUNNER[@]}" "$e2e_runner" 2>&1)"
        rc=$?
        tail="$(printf '%s\n' "$out" | grep -E '^E2E (OK|FAIL|SKIP)' | tail -1)"
        if [ $rc -eq 0 ]; then
          record e2e ok "${tail:-passed}"
        elif [ $rc -eq 2 ]; then
          # Exit 2 is "this environment cannot run it" (no manifest yet, no
          # openssl, or a step recorded as not applicable by the runner). A skip,
          # not a pass: --release refuses it.
          printf '%s\n' "$out" | grep -E '^E2E (SKIP|NOT APPLICABLE)' | tail -3
          record e2e na "${tail:-environment cannot run it}"
        else
          printf '%s\n' "$out" | tail -50
          record e2e fail "${tail:-exit $rc}"
        fi
      fi
    fi
  fi
fi

# ------------------------------------------------------------------ 9. tree
if [ "$RUN_TREE" = 1 ]; then
  banner "tree check (catalog rule 17)"
  out="$(cd "$REPO" && bash scripts/check_tree.sh 2>&1)"
  rc=$?
  tail="$(printf '%s\n' "$out" | grep -E '^check_tree' | tail -1)"
  if [ $rc -eq 0 ]; then
    record tree ok "${tail:-clean}"
  else
    printf '%s\n' "$out" | tail -20
    record tree fail "${tail:-exit $rc}"
  fi
fi

# ------------------------------------------------------------------ summary
printf '\n=== summary ===\n'
for line in "${SUMMARY[@]}"; do printf '%s\n' "$line"; done
printf '\n%d passed, %d failed, %d not applicable\n' "$PASS" "$FAIL" "$NA"

# Release mode allows no n/a at all: "not applicable" is a build-time concession,
# and a release candidate either proves every step or does not ship.
if [ "$RELEASE" = 1 ] && [ "$NA" -gt 0 ]; then
  echo "check.sh: FAIL (release mode: $NA step(s) not applicable; every step must run)"
  exit 1
fi
if [ "$FAIL" -eq 0 ]; then
  if [ "$RELEASE" = 1 ]; then
    echo "check.sh: OK (release mode: zero baselined findings, formatted tree)"
  else
    echo "check.sh: OK"
  fi
  exit 0
fi
echo "check.sh: FAIL (${FAILED_STEPS[*]})"
exit 1
