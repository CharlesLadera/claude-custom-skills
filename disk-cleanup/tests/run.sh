#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# On Windows, `python3` is usually the Microsoft Store alias, which prints an
# install prompt and exits non-zero instead of running anything.
PY=python3
if ! "$PY" -c "" >/dev/null 2>&1; then
  PY=python
fi

echo "=== Python unit tests (tests/test_*.py) ==="
"$PY" -m unittest discover -s tests -v

echo
echo "=== Shell rehearsal: tests/test_execution.sh ==="
if [ -x /usr/bin/trash ]; then
  echo "(really trashes files in a mktemp scratch tree via /usr/bin/trash, then"
  echo " removes its own items back out of ~/.Trash - see the script's own header)"
  bash tests/test_execution.sh
else
  echo "SKIPPED: /usr/bin/trash not found. This rehearses macOS removal only;"
  echo "the skill does not remove files itself on other OSes."
fi
