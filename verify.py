#!/usr/bin/env python3
"""환경 및 동작 검증 스크립트"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


PASS = "  ✅"
FAIL = "  ❌"
WARN = "  ⚠ "

errors: list[str] = []
warnings: list[str] = []


def ok(msg: str) -> None:
    print(f"{PASS} {msg}")


def fail(msg: str) -> None:
    print(f"{FAIL} {msg}")
    errors.append(msg)


def warn(msg: str) -> None:
    print(f"{WARN} {msg}")
    warnings.append(msg)


# ── env 로딩 ─────────────────────────────────────────────────────────────────

def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(Path(__file__).parent / ".env")
_load_dotenv(Path.home() / ".config" / "k-skill" / "secrets.env")


# ── 검증 항목 ────────────────────────────────────────────────────────────────

print("=" * 58)
print("  KTX 스나이퍼 환경 검증")
print("=" * 58)

# 1. Python 버전
print("\n[1] Python 버전")
if sys.version_info >= (3, 10):
    ok(f"Python {sys.version.split()[0]}")
else:
    fail(f"Python {sys.version.split()[0]} — 3.10 이상 필요")

# 2. 패키지 설치 여부
print("\n[2] 필수 패키지")
for pkg, import_name in [("korail2", "korail2"), ("pycryptodome", "Crypto")]:
    if importlib.util.find_spec(import_name):
        ok(f"{pkg} 설치됨")
    else:
        fail(f"{pkg} 없음 → pip install {pkg}")

# 3. ktx_booking.py 존재 여부
print("\n[3] ktx_booking.py")
ktx_path = Path(__file__).parent / "ktx_booking.py"
if ktx_path.exists():
    ok(f"ktx_booking.py 발견 ({ktx_path})")
else:
    fail(f"ktx_booking.py 없음 → setup.sh 실행 필요")

# 4. 환경변수 설정 여부
print("\n[4] 환경변수")
ktx_id = os.environ.get("KSKILL_KTX_ID", "")
ktx_pw = os.environ.get("KSKILL_KTX_PASSWORD", "")

if ktx_id:
    masked = "*" * max(0, len(ktx_id) - 4) + ktx_id[-4:]
    ok(f"KSKILL_KTX_ID 설정됨 ({masked})")
else:
    fail("KSKILL_KTX_ID 미설정 — .env 파일에 추가하세요")

if ktx_pw:
    ok(f"KSKILL_KTX_PASSWORD 설정됨 ({'*' * len(ktx_pw)})")
else:
    fail("KSKILL_KTX_PASSWORD 미설정 — .env 파일에 추가하세요")

# 5. 로그인 테스트 (앞 단계 통과한 경우에만)
print("\n[5] Korail 로그인 테스트")
if errors:
    warn("앞 단계 오류로 로그인 테스트 건너뜀")
else:
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from ktx_booking import build_client, NeedToLoginError  # type: ignore[import]
        try:
            client = build_client()
            ok(f"로그인 성공 (이름: {getattr(client, 'name', '?')})")
        except NeedToLoginError as exc:
            fail(f"로그인 실패: {exc} — ID/PW 확인 필요")
        except SystemExit as exc:
            fail(f"로그인 불가: {exc}")
        except Exception as exc:
            fail(f"로그인 오류: {exc}")
    except ImportError as exc:
        fail(f"ktx_booking import 실패: {exc}")

# 6. 열차 조회 테스트 (로그인 성공한 경우에만)
print("\n[6] 열차 조회 테스트 (서울→부산, 오늘 기준)")
if errors:
    warn("앞 단계 오류로 조회 테스트 건너뜀")
else:
    try:
        from ktx_booking import build_client, NoResultsError  # type: ignore[import]
        import datetime

        kst_now = datetime.datetime.now() + datetime.timedelta(hours=9 - datetime.datetime.now().utcoffset().total_seconds() // 3600 if datetime.datetime.now().utcoffset() else 0)
        today = datetime.date.today().strftime("%Y%m%d")
        try:
            client = build_client()
            trains = client.search_train(
                "서울", "부산", today, "000000",
                include_no_seats=True,
            )
            ok(f"조회 성공 — 열차 {len(trains)}편 반환")
        except NoResultsError:
            warn("조회 결과 없음 (오늘 날짜 열차가 모두 출발 완료되었거나 노선 없음) — 정상일 수 있음")
        except Exception as exc:
            fail(f"조회 오류: {exc}")
    except ImportError:
        pass  # 이미 위에서 처리됨

# ── 결과 요약 ────────────────────────────────────────────────────────────────

print("\n" + "=" * 58)
if not errors:
    print("  ✅  모든 검증 통과 — sniper.py 실행 가능합니다!")
    print("\n  다음 단계:")
    print("    1. 열차 조회:")
    print("       python3 ktx_booking.py search 서울 부산 YYYYMMDD HHMMSS \\")
    print("           --include-no-seats --limit 10")
    print("    2. 스나이퍼 실행:")
    print("       python3 sniper.py 서울 부산 YYYYMMDD HHMMSS \\")
    print('           --train-id "ktx:v1:..." --seat-option general-first \\')
    print("           --interval 45")
else:
    print(f"  ❌  오류 {len(errors)}개 — 위 항목을 먼저 수정하세요:")
    for e in errors:
        print(f"       • {e}")
if warnings:
    print(f"\n  ⚠  경고 {len(warnings)}개:")
    for w in warnings:
        print(f"       • {w}")
print("=" * 58)

sys.exit(1 if errors else 0)
