#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "======================================================"
echo "  KTX / SRT 취소표 스나이퍼 — 로컬 설치"
echo "======================================================"

python3 -m pip install -r requirements.txt

echo ""
echo "설치 완료"
echo "실행: python3 ticket_web.py"
echo "macOS 빌드: ./build_macos.sh"
echo "Windows 빌드: PowerShell에서 .\\build_windows.ps1"
