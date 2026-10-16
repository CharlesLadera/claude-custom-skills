#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# On Windows, python3 is often a Microsoft Store stub that exits non-zero.
PY=""
for candidate in python3 python; do
    if "$candidate" -c 'import sys' >/dev/null 2>&1; then PY="$candidate"; break; fi
done
[ -n "$PY" ] || { echo "no working python3/python on PATH" >&2; exit 1; }

echo "=== Python unit tests (tests/test_*.py) with $PY ==="
"$PY" -m unittest discover -s tests -v
