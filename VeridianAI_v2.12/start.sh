#!/bin/bash
echo ""
echo "  +=========================================+"
echo "  |          VERIDIAN   AI   v2.12          |"
echo "  +=========================================+"
echo ""

# Self-locate so it runs from anywhere
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON=""
for cmd in python3 python py; do
  if command -v $cmd &>/dev/null; then PYTHON=$cmd; break; fi
done

if [ -z "$PYTHON" ]; then
  echo "[ERROR] Python not found. Install Python 3.10+"
  exit 1
fi

echo "Using: $PYTHON"
$PYTHON start.py "$@"
