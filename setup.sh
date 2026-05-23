#!/usr/bin/env bash
# KTX 스나이퍼 설치 스크립트
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KTX_BOOKING_URL="https://raw.githubusercontent.com/NomaDamas/k-skill/main/scripts/ktx_booking.py"

echo "======================================================"
echo "  KTX 취소표 스나이퍼 — 설치"
echo "======================================================"

# ── 1. Python 버전 확인 ──────────────────────────────────────────────────────
echo ""
echo "[1] Python 버전 확인..."
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "  ❌  Python $PY_VERSION 감지 — 3.10 이상 필요합니다."
    exit 1
fi
echo "  ✅  Python $PY_VERSION"

# ── 2. 패키지 설치 ───────────────────────────────────────────────────────────
echo ""
echo "[2] 필수 패키지 설치 (korail2, pycryptodome, flask, SRTrain)..."
python3 -m pip install --quiet korail2 pycryptodome flask SRTrain
echo "  ✅  패키지 설치 완료"

# ── 3. ktx_booking.py 다운로드 ───────────────────────────────────────────────
echo ""
echo "[3] ktx_booking.py 다운로드..."
TARGET="$SCRIPT_DIR/ktx_booking.py"

if command -v curl &>/dev/null; then
    curl -fsSL "$KTX_BOOKING_URL" -o "$TARGET"
elif command -v wget &>/dev/null; then
    wget -q "$KTX_BOOKING_URL" -O "$TARGET"
else
    echo "  ❌  curl / wget 모두 없습니다. 직접 다운로드하세요:"
    echo "      $KTX_BOOKING_URL"
    exit 1
fi
echo "  ✅  ktx_booking.py 저장됨: $TARGET"

# ── 4. .env 파일 생성 ────────────────────────────────────────────────────────
echo ""
echo "[4] .env 파일 확인..."
ENV_FILE="$SCRIPT_DIR/.env"
ENV_EXAMPLE="$SCRIPT_DIR/.env.example"

if [ ! -f "$ENV_FILE" ]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "  ✅  .env 생성됨 — ID/PW를 직접 편집하세요:"
    echo "      $ENV_FILE"
else
    echo "  ✅  .env 이미 존재 — 덮어쓰지 않음"
fi

# ── 완료 ─────────────────────────────────────────────────────────────────────
echo ""
echo "======================================================"
echo "  설치 완료!"
echo ""
echo "  다음 단계:"
echo "    1. .env 파일 편집:"
echo "       $ENV_FILE"
echo ""
echo "    2. 환경 검증:"
echo "       python3 verify.py"
echo ""
echo "    3. 열차 조회 (train_id 확보):"
echo "       python3 ktx_booking.py search 서울 부산 YYYYMMDD HHMMSS \\"
echo "           --include-no-seats --limit 10"
echo ""
echo "    4. 스나이퍼 실행:"
echo "       python3 sniper.py 서울 부산 YYYYMMDD HHMMSS \\"
echo '           --train-id "ktx:v1:..." --seat-option general-first \\'
echo "           --interval 45"
echo "======================================================"
