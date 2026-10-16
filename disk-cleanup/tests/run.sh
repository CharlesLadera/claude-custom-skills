#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Python unit tests (tests/test_*.py) ==="
python3 -m unittest discover -s tests -v

echo
echo "=== Shell rehearsal: tests/test_execution.sh ==="
echo "(really trashes files in a mktemp scratch tree via /usr/bin/trash, then"
echo " removes its own items back out of ~/.Trash - see the script's own header)"
bash tests/test_execution.sh
