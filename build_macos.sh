#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_CMD=(python3)
if arch -arm64 python3 -c 'import platform; raise SystemExit(0 if platform.machine() == "arm64" else 1)' >/dev/null 2>&1; then
  PYTHON_CMD=(arch -arm64 python3)
fi

"${PYTHON_CMD[@]}" -m pip install -r requirements.txt
"${PYTHON_CMD[@]}" -m PyInstaller --clean --noconfirm ticket_web.spec

echo "macOS 실행 파일: dist/ticket-sniper"
