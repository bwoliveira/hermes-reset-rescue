#!/usr/bin/env bash
# Reset Rescue — `requires_hermes` floor probe (T-02, catalog rule 14).
#
# Read-only. For every surface listed in docs/release/symbols.txt it finds the
# commit that introduced the symbol (`git log -S`, pickaxe) and the earliest
# *stable* tag that contains that commit (`git tag --contains`, filtered to
# `vX.Y.Z`), then reports the newest such tag as the candidate floor.
#
# Never writes to the Hermes install: `git log`/`git tag` only.
#
#   scripts/floor_probe.sh                      # probe /usr/local/lib/hermes-agent
#   scripts/floor_probe.sh --repo /path/to/tree
#   scripts/floor_probe.sh --symbols docs/release/symbols.txt --out docs/release/floor-probe.txt
#   scripts/floor_probe.sh --check 0.22.6       # exit 1 unless the floor equals 0.22.6
#
# Exit codes: 0 probe ok, 1 --check mismatch, 2 bad usage / no repo / no symbol
# resolved. A symbol that cannot be resolved is an error, not a warning: an
# unproven floor is exactly what rule 14 forbids.
set -uo pipefail

REPO_DEFAULT="/usr/local/lib/hermes-agent"
REPO="$REPO_DEFAULT"
SYMBOLS=""
OUT=""
CHECK=""
QUIET=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SYMBOLS_DEFAULT="$ROOT/docs/release/symbols.txt"

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    --symbols) SYMBOLS="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    --check) CHECK="${2:-}"; shift 2 ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "floor_probe: unknown argument $1" >&2; exit 2 ;;
  esac
done

SYMBOLS="${SYMBOLS:-$SYMBOLS_DEFAULT}"

# Every git call is bounded: a whole-tree pickaxe can walk every blob in the
# history and take minutes, so a slow probe must fail loudly, not hang.
GIT_TIMEOUT="${GIT_TIMEOUT:-120}"

git_bounded() {
  timeout "$GIT_TIMEOUT" git -C "$REPO" "$@"
}

[ -d "$REPO/.git" ] || { echo "floor_probe: $REPO is not a git checkout" >&2; exit 2; }
[ -f "$SYMBOLS" ] || { echo "floor_probe: symbol list not found: $SYMBOLS" >&2; exit 2; }

emit() {
  if [ -n "$OUT" ]; then
    printf '%s\n' "$*" >> "$OUT"
  fi
  [ "$QUIET" = 1 ] || printf '%s\n' "$*"
}

if [ -n "$OUT" ]; then
  : > "$OUT"
  emit "# Reset Rescue — requires_hermes floor probe"
  emit "# generated $(date -u '+%Y-%m-%dT%H:%M:%SZ') by scripts/floor_probe.sh"
  emit "# repo: $REPO at $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo '?')"
  emit "#"
fi

newest_tag=""
newest_id=""
newest_commit=""
newest_date=""
errors=0
rows=""

while IFS='|' read -r id symbol path why floor; do
  case "$id" in ''|'#'*) continue ;; esac
  floor="${floor:-yes}"
  if [ -z "${symbol:-}" ]; then
    echo "floor_probe: $id has no symbol" >&2
    errors=$((errors + 1))
    continue
  fi

  # Oldest commit that changed the occurrence count of the symbol.
  if [ -n "${path:-}" ]; then
    line="$(git_bounded log -S "$symbol" --format='%H %cs' -- "$path" 2>/dev/null | tail -1)"
  else
    line="$(git_bounded log -S "$symbol" --format='%H %cs' 2>/dev/null | tail -1)"
  fi
  commit="${line%% *}"
  date="${line##* }"
  if [ -z "$commit" ]; then
    if [ "$floor" = "no" ]; then
      emit "$(printf '%-28s %-12s %-12s %s  %s' "$id" "(n/a)" "-" "-" "$symbol")"
      continue
    fi
    emit "UNRESOLVED  $id  symbol='$symbol' path='${path:-<all>}'"
    errors=$((errors + 1))
    continue
  fi

  # Earliest stable tag containing that commit. Canary/pre-release tags are
  # ignored: the floor must be a release a user can actually install.
  tag="$(git_bounded tag --contains "$commit" 2>/dev/null \
          | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | head -1)"
  if [ -z "$tag" ]; then
    tag="(no stable tag yet)"
    tag_rank="999.999.999"
  else
    tag_rank="${tag#v}"
  fi

  rows+="$id|$symbol|$commit|$date|$tag"$'\n'
  emit "$(printf '%-28s %-12s %s  %s  %s' "$id" "$tag" "${commit:0:12}" "$date" "$symbol")"

  if [ "$tag_rank" != "999.999.999" ] && [ "$floor" != "no" ]; then
    if [ -z "$newest_tag" ]; then
      newest_tag="$tag_rank"; newest_id="$id"; newest_commit="$commit"; newest_date="$date"
    elif [ "$(printf '%s\n%s\n' "$newest_tag" "$tag_rank" | sort -V | tail -1)" = "$tag_rank" ] \
         && [ "$tag_rank" != "$newest_tag" ]; then
      newest_tag="$tag_rank"; newest_id="$id"; newest_commit="$commit"; newest_date="$date"
    fi
  fi
done < "$SYMBOLS"

if [ "$errors" -gt 0 ]; then
  emit ""
  emit "RESULT: FAIL ($errors unresolved symbol(s))"
  exit 2
fi

emit ""
if [ -z "$newest_tag" ]; then
  emit "RESULT: FAIL (no stable tag contains any probed surface)"
  exit 2
fi

emit "candidate floor: >=$newest_tag  (newest surface: $newest_id, $newest_commit ${newest_date})"
emit "RESULT: OK"

if [ -n "$CHECK" ]; then
  want="${CHECK#v}"
  want="${want#>=}"
  if [ "$want" = "$newest_tag" ]; then
    emit "check: floor $CHECK matches the probe"
    exit 0
  fi
  emit "check: floor $CHECK does NOT match the probe ($newest_tag)"
  exit 1
fi

exit 0
