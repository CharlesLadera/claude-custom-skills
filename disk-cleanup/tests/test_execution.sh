#!/usr/bin/env bash
# disk-cleanup/tests/test_execution.sh
#
# Not a dry run: this really trashes files in a mktemp -d scratch tree via
# /usr/bin/trash, and then really removes its own items back out of
# ~/.Trash. Nothing outside the scratch tree and this script's own Trash
# entries is ever touched. Rehearses SKILL.md step 5 (removal) so the
# destructive path -- which otherwise lives only in prose -- gets
# exercised against a scratch directory before it is trusted, per this
# repo's standing rule for shell snippets.
set -euo pipefail

SCRATCH="$(mktemp -d)"
TRASHED_NAMES=()

cleanup() {
  if [ ${#TRASHED_NAMES[@]} -gt 0 ]; then
    local name
    for name in "${TRASHED_NAMES[@]}"; do
      [ -e "$HOME/.Trash/$name" ] && /bin/rm -rf "$HOME/.Trash/$name"
    done
  fi
  rm -rf "$SCRATCH"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

command -v trash >/dev/null || fail "trash not on PATH"

# --- Main rehearsal: the shapes SKILL.md step 5 will actually hand trash.
# Every path scan.py/classify.py ever produce is absolute (canonicalized
# to the firmlink form, manifest ~-entries expanded against HOME), so a
# leading "-" in a filename is never the first character of the argument
# trash sees. -weird-name proves that shape is handled cleanly.
# Names carry a per-run tag: cleanup permanently removes ~/.Trash/<name>,
# so a name that matched something the user already trashed would delete it.
TAG="dc-test-$$-$RANDOM"
NAMES=("cache with spaces $TAG" "plain-$TAG" "loose-$TAG.bin" "-weird-name-$TAG")
for name in "${NAMES[@]}" "-relative-name-$TAG"; do
  [ -e "$HOME/.Trash/$name" ] && fail "$name already in Trash; refusing to run"
done

mkdir -p "$SCRATCH/${NAMES[0]}/sub" "$SCRATCH/${NAMES[1]}"
echo x > "$SCRATCH/${NAMES[0]}/sub/a.bin"
echo x > "$SCRATCH/${NAMES[1]}/b.bin"
echo x > "$SCRATCH/${NAMES[2]}"
echo x > "$SCRATCH/${NAMES[3]}"

TARGETS=()
for name in "${NAMES[@]}"; do TARGETS+=("$SCRATCH/$name"); done
/usr/bin/trash "${TARGETS[@]}"

for t in "${TARGETS[@]}"; do
  [ -e "$t" ] && fail "$t survived trash"
done

for name in "${NAMES[@]}"; do
  [ -e "$HOME/.Trash/$name" ] || fail "$name did not land in Trash"
  TRASHED_NAMES+=("$name")
done

echo "PASS: trash handles directories, spaces, loose files, and a leading-dash filename (all as absolute paths)"

# --- Finding: does "trash -- <path>" behave like a GNU tool's "--"
# (end of options, rest are literal filenames)? Manifest paths are
# user-supplied, so it matters whether "--" would be a safe defensive
# habit for SKILL.md's command table to adopt. Tested against a BARE
# relative leading-dash argument, since that's the only shape where a
# real ambiguity could ever exist (trash's usage string documents no "--"
# option at all: "trash [-h][--help][-s][--stopOnError][-v][--verbose]
# fileToMoveToTrash...").
mkdir -p "$SCRATCH/ddash"
echo x > "$SCRATCH/ddash/-relative-name-$TAG"
set +e
DDASH_OUT="$(cd "$SCRATCH/ddash" && /usr/bin/trash -- "-relative-name-$TAG" 2>&1)"
DDASH_EXIT=$?
set -e

if [ -e "$SCRATCH/ddash/-relative-name-$TAG" ]; then
  echo "FINDING: trash -- left a bare leading-dash relative path untouched (exit $DDASH_EXIT: $DDASH_OUT)."
else
  TRASHED_NAMES+=("-relative-name-$TAG")
  echo "FINDING: trash -- <bare-relative-dash-name> DID move the file (exit $DDASH_EXIT: $DDASH_OUT)."
  echo "         trash has no real '--' end-of-options marker: it moves the named file but ALSO tries"
  echo "         to move a literal file called '--' (which never exists), so the command still exits"
  echo "         nonzero and prints a file-not-found error even on success. Do not add '--' to SKILL.md's"
  echo "         command table on the theory that it is a safe no-op -- it turns a successful run into an"
  echo "         apparent failure. This skill stays safe without it because every path it ever hands to"
  echo "         trash is absolute, so the bare-relative-dash shape tested here never actually occurs."
fi
