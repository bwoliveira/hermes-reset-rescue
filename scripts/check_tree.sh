#!/usr/bin/env bash
# Reset Rescue — tree check (T-52, catalog rule 17).
#
# Fails when the *tracked* tree carries anything a reviewer cannot read as
# source: bytecode, binaries, archives, oversized files, or a symlink that
# escapes the repository. Two reviewed exceptions: the catalog screenshots
# (T-65) and the README art, see SCREENSHOT_DIR and ART_DIR below. Untracked files are ignored — `__pycache__` from a
# local test run is not shipped content; only the index says what a pin carries.
#
#   scripts/check_tree.sh            # check the repository this script lives in
#   scripts/check_tree.sh --repo DIR
#   scripts/check_tree.sh --staged   # check what is staged for commit instead
#
# Exit codes: 0 clean, 1 a finding, 2 bad usage / not a git checkout.
set -uo pipefail

REPO=""
MODE="tracked"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    --staged) MODE="staged"; shift ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "check_tree: unknown argument $1" >&2; exit 2 ;;
  esac
done

if [ -z "$REPO" ]; then
  REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

[ -e "$REPO/.git" ] || { echo "check_tree: $REPO is not a git checkout" >&2; exit 2; }
cd "$REPO" || exit 2
git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_tree: $REPO is not a git work tree" >&2; exit 2; }

# Extensions that are never source. Kept explicit: a new binary format must be
# a deliberate addition here, reviewed, not silently accepted.
BINARY_EXTS="png jpg jpeg gif webp ico bmp tiff psd pdf zip tar gz bz2 xz 7z rar
jar war whl egg so dylib dll exe bin dmg iso img deb rpm apk aab ipa
mp3 mp4 mov avi mkv wav flac ogg webm woff woff2 ttf otf eot
pyc pyo pyd class o a lib obj"

# Files that are text but still must not ship: a data blob dressed as source.
DENY_NAMES=".DS_Store Thumbs.db"

MAX_FILE_KB="${MAX_FILE_KB:-512}"

# The one place a binary may live: the README / catalog screenshots (T-65,
# "up to 6 screenshots in the repo"), made by scripts/screenshots.mjs. Only
# `<dir>/<name>.png` directly inside it, only a real PNG (signature checked),
# at most SCREENSHOT_MAX files, and still under MAX_FILE_KB.
SCREENSHOT_DIR="docs/screenshots"
SCREENSHOT_MAX=6
PNG_SIGNATURE="89504e470d0a1a0a"
screenshots=0

# The README art (hero banner, feature cards, how-it-works strip): same rules,
# but PNG, JPEG or WebP, each checked by its signature, at most ART_MAX files.
ART_DIR="docs/art"
ART_MAX=6
JPEG_SIGNATURE="ffd8ff"
art=0

# Hex of the file's first bytes (prints nothing for an unreadable file).
head_hex() { head -c "$2" -- "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n'; }

# Does the file's content match the image format its extension claims?
image_signature_ok() {
  local file="$1" kind="$2" sig
  case "$kind" in
    png) [ "$(head_hex "$file" 8)" = "$PNG_SIGNATURE" ] ;;
    jpg|jpeg) [ "$(head_hex "$file" 3)" = "$JPEG_SIGNATURE" ] ;;
    webp)
      sig="$(head_hex "$file" 12)"
      [ "${sig:0:8}" = "52494646" ] && [ "${sig:16:8}" = "57454250" ] ;;
    *) return 1 ;;
  esac
}

findings=0
report() {
  printf 'FAIL  %s\n' "$*"
  findings=$((findings + 1))
}

case "$MODE" in
  staged)
    list_files() { git diff --cached --name-only --diff-filter=ACMR -z; }
    ;;
  *)
    list_files() { git ls-files -z; }
    ;;
esac

count=0
while IFS= read -r -d '' path; do
  [ -n "$path" ] || continue
  count=$((count + 1))

  # A symlink in the index that resolves outside the repo escapes review.
  if [ -L "$path" ]; then
    target="$(readlink -f -- "$path" 2>/dev/null || true)"
    root="$(cd "$REPO" && pwd -P)"
    case "$target" in
      "$root"/*) : ;;
      *) report "symlink escapes the repository: $path -> ${target:-<broken>}" ;;
    esac
    continue
  fi

  [ -f "$path" ] || continue

  base="$(basename -- "$path")"
  for deny in $DENY_NAMES; do
    [ "$base" = "$deny" ] && report "editor/OS junk tracked: $path"
  done

  ext="${base##*.}"
  allowed_binary=0
  case "$path" in
    "$SCREENSHOT_DIR"/*/*) : ;;
    "$SCREENSHOT_DIR"/*.png)
      sig="$(head -c 8 -- "$path" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
      if [ "$sig" = "$PNG_SIGNATURE" ]; then
        allowed_binary=1
        screenshots=$((screenshots + 1))
        [ "$screenshots" -eq $((SCREENSHOT_MAX + 1)) ] && report "more than $SCREENSHOT_MAX screenshots in $SCREENSHOT_DIR"
      else
        report "not a PNG despite its name: $path"
      fi
      ;;
    "$ART_DIR"/*/*) : ;;
    "$ART_DIR"/*.*)
      kind="$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')"
      case "$kind" in
        png|jpg|jpeg|webp)
          if image_signature_ok "$path" "$kind"; then
            allowed_binary=1
            art=$((art + 1))
            [ "$art" -eq $((ART_MAX + 1)) ] && report "more than $ART_MAX images in $ART_DIR"
          else
            report "not a $kind image despite its name: $path"
          fi
          ;;
      esac
      ;;
  esac
  if [ "$allowed_binary" -eq 0 ] && [ "$ext" != "$base" ]; then
    ext="$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')"
    for bad in $BINARY_EXTS; do
      if [ "$ext" = "$bad" ]; then
        report "binary or generated file tracked: $path"
        break
      fi
    done
  fi

  # Binary content behind a text extension (an ELF named `sneaky.py`). A NUL
  # byte is the reliable signal: `file --mime-encoding` calls a file holding
  # only a newline "binary", and an empty `__init__.py` is legitimate source.
  # (`grep $'\x00'` cannot be used: bash drops the NUL and the pattern matches
  # every file.)
  if [ "$allowed_binary" -eq 0 ] && [ -s "$path" ]; then
    nul_bytes="$(tr -dc '\000' < "$path" 2>/dev/null | wc -c)"
    if [ "${nul_bytes:-0}" -gt 0 ]; then
      report "binary content (NUL byte): $path"
    fi
  fi

  size_kb=$(( ( $(wc -c < "$path" 2>/dev/null || echo 0) + 1023) / 1024 ))
  if [ "$size_kb" -gt "$MAX_FILE_KB" ]; then
    report "file is ${size_kb}KB (limit: ${MAX_FILE_KB}KB): $path"
  fi
done < <(list_files)

echo "check_tree ($MODE): $count file(s) checked, $findings finding(s)"
if [ "$findings" -gt 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: OK"
exit 0
