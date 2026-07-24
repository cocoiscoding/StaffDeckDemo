#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN=""
for candidate in "$ROOT_DIR/backend/.venv/bin/python" "${PYTHON:-}" python3 python; do
  [[ -n "$candidate" ]] || continue
  if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.11 or newer is required." >&2
  exit 1
fi
exec "$PYTHON_BIN" "$ROOT_DIR/scripts/dev.py" down "$@"
