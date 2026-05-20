#!/usr/bin/env python3
"""KTX 취소표 스나이핑 봇 — 인터랙티브 / CLI 모드"""
from __future__ import annotations

import argparse
import os
import random
import re
import subprocess
import sys
import time
from datetime import date as date_cls, datetime
from pathlib import Path
from types import SimpleNamespace


# ── env 로딩 (우선순위: 환경변수 > .env > ~/.config/k-skill/secrets.env) ──────

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


# ── ktx_booking.py import ────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent))
try:
    from ktx_booking import (  # type: ignore[import]
        AdultPassenger, ChildPassenger, SeniorPassenger, ToddlerPassenger,
        KorailError, NeedToLoginError, NoResultsError, SoldOutError,
        RESERVE_OPTION_MAP, TRAIN_TYPE_MAP,
        build_client, build_train_id, find_train_by_id,
        normalize_reservation, normalize_train,
    )
except ImportError as exc:
    sys.exit(
        f"ktx_booking.py를 찾을 수 없습니다.\n"
        f"  먼저 setup.sh를 실행하세요: bash setup.sh\n"
        f"  오류: {exc}"
    )


# ── 로그 / 알림 ───────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _macos_notify(title: str, body: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{body}" with title "{title}" sound name "Glass"'],
            check=False, timeout=5, capture_output=True,
        )
    except Exception:
        pass


def _bell() -> None:
    sys.stdout.write("\a\a\a")
    sys.stdout.flush()


# ── 날짜 파서 ────────────────────────────────────────────────────────────────

def _parse_date(raw: str) -> str | None:
    s = raw.strip().replace(" ", "")

    # YYYYMMDD
    if re.fullmatch(r"\d{8}", s):
        return s

    # YYYY-MM-DD / YYYY/MM/DD
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"

    # 한국어: 6월1일, 12월 25일
    m = re.search(r"(\d{1,2})월\s*(\d{1,2})일?", s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        today = date_cls.today()
        try:
            cand = date_cls(today.year, month, day)
            if cand < today:
                cand = date_cls(today.year + 1, month, day)
            return cand.strftime("%Y%m%d")
        except ValueError:
            return None

    # MM/DD 또는 M/D
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        today = date_cls.today()
        try:
            cand = date_cls(today.year, month, day)
            if cand < today:
                cand = date_cls(today.year + 1, month, day)
            return cand.strftime("%Y%m%d")
        except ValueError:
            return None

    return None


# ── 시각 파서 ────────────────────────────────────────────────────────────────

def _parse_time(raw: str) -> str | None:
    """사용자 입력 → HHMMSS (6자리 문자열)"""
    s = raw.strip().replace(" ", "")

    # 한국어: 9시30분, 09시, 9시
    m = re.search(r"(\d{1,2})시(?:\s*(\d{1,2})분)?", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}{mi:02d}00"

    # HH:MM
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}{mi:02d}00"

    # HHMM (4자리)
    if re.fullmatch(r"\d{4}", s):
        h, mi = int(s[:2]), int(s[2:])
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{s}00"

    # HMM (3자리: 900 → 09:00)
    if re.fullmatch(r"\d{3}", s):
        h, mi = int(s[0]), int(s[1:])
        if 0 <= h <= 9 and 0 <= mi <= 59:
            return f"0{s}00"

    # H 또는 HH (시각만)
    if re.fullmatch(r"\d{1,2}", s):
        h = int(s)
        if 0 <= h <= 23:
            return f"{h:02d}0000"

    return None


def _fmt_time(t: str) -> str:
    """HHMMSS → HH:MM"""
    return f"{t[:2]}:{t[2:4]}"


def _fmt_date(d: str) -> str:
    """YYYYMMDD → YYYY-MM-DD"""
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


# ── 입력 헬퍼 ────────────────────────────────────────────────────────────────

def _ask(prompt: str, default: str | None = None) -> str:
    hint = f" [{default}]" if default is not None else ""
    try:
        val = input(f"  {prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else (default or "")


def _parse_selection(raw: str, max_n: int) -> list[int] | None:
    """'1', '1,3', '1-3', 'all' → 인덱스 리스트 (1-based)"""
    s = raw.strip().lower()
    if s in ("all", "전체", "*"):
        return list(range(1, max_n + 1))
    indices: set[int] = set()
    for part in re.split(r"[,\s]+", s):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            indices.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif re.fullmatch(r"\d+", part):
            indices.add(int(part))
        else:
            return None
    valid = sorted(i for i in indices if 1 <= i <= max_n)
    return valid if valid else None


# ── 열차 테이블 출력 ──────────────────────────────────────────────────────────

def _seat_label(info: dict) -> str:
    parts = []
    if info["has_general_seat"]:
        parts.append("일반석 ✓")
    if info["has_special_seat"]:
        parts.append("특실 ✓")
    if info["has_waiting_list"] and not (info["has_general_seat"] or info["has_special_seat"]):
        parts.append("대기열 가능")
    return " | ".join(parts) if parts else "매진"


def _print_train_table(trains: list) -> None:
    print(f"  {'#':>3}  {'열차번호':>8}  {'출발':>5}  {'도착':>5}  좌석상태")
    print("  " + "─" * 50)
    for i, t in enumerate(trains, 1):
        info = normalize_train(t, i)
        dep = _fmt_time(info["dep_time"])
        arr = _fmt_time(info["arr_time"])
        label = _seat_label(info)
        print(f"  {i:>3}  {info['train_no']:>8}  {dep:>5} → {arr:<5}  {label}")


# ── 로그인 ────────────────────────────────────────────────────────────────────

def _login_with_retry(max_attempts: int = 3):
    for attempt in range(1, max_attempts + 1):
        try:
            return build_client()
        except NeedToLoginError as exc:
            log(f"로그인 실패 ({attempt}/{max_attempts}): {exc}")
        except SystemExit:
            raise
        except Exception as exc:
            log(f"로그인 오류 ({attempt}/{max_attempts}): {exc}")
        if attempt < max_attempts:
            time.sleep(5 * attempt)
    sys.exit(
        "로그인 재시도 초과\n"
        "  .env의 KSKILL_KTX_ID / KSKILL_KTX_PASSWORD를 확인하세요.\n"
        "  python3 verify.py 로 진단할 수 있습니다."
    )


# ── 승객 ─────────────────────────────────────────────────────────────────────

def _build_passengers(args) -> list:
    passengers = []
    if getattr(args, "adults", 1):
        passengers.append(AdultPassenger(args.adults))
    if getattr(args, "children", 0):
        passengers.append(ChildPassenger(args.children))
    if getattr(args, "seniors", 0):
        passengers.append(SeniorPassenger(args.seniors))
    if getattr(args, "toddlers", 0):
        passengers.append(ToddlerPassenger(args.toddlers))
    if not passengers:
        passengers.append(AdultPassenger())
    return passengers


# ── 성공 알림 ────────────────────────────────────────────────────────────────

def _notify_success(res: dict) -> None:
    sep = "=" * 58
    buy_date = res.get("buy_limit_date", "?")
    buy_time_raw = res.get("buy_limit_time", "")
    buy_time = _fmt_time(buy_time_raw) if len(buy_time_raw) >= 4 else buy_time_raw
    dep_time = _fmt_time(res["dep_time"]) if len(res.get("dep_time", "")) >= 4 else res.get("dep_time", "?")
    arr_time = _fmt_time(res["arr_time"]) if len(res.get("arr_time", "")) >= 4 else res.get("arr_time", "?")

    print(sep, flush=True)
    print("  ✅  KTX 예약 성공!")
    print(f"  예약번호  : {res['reservation_id']}")
    print(f"  열차      : {res.get('train_type', '')} {res['train_no']}")
    print(f"  구간      : {res['dep_name']} {dep_time} → {res['arr_name']} {arr_time}")
    print(f"  운임      : {res.get('price', '?')}원")
    print(f"  결제 기한 : {_fmt_date(buy_date)} {buy_time} 까지")
    print()
    print("  ⚠  Korail 앱/웹에서 결제 기한 내에 결제를 완료하세요!")
    print(sep, flush=True)
    _bell()
    _macos_notify(
        "KTX 예약 성공 🎉",
        f"예약번호 {res['reservation_id']} | {res['dep_name']}→{res['arr_name']} {dep_time} | 결제기한 {_fmt_date(buy_date)} {buy_time}",
    )


# ── 슬립 ─────────────────────────────────────────────────────────────────────

def _sleep(base: float) -> None:
    time.sleep(max(5.0, base + base * random.uniform(-0.1, 0.1)))


def _backoff(n_errors: int, base: float) -> None:
    if n_errors >= 3:
        wait = min(300.0, 30.0 * (2 ** (n_errors - 3)))
        log(f"  연속 오류 {n_errors}회 → backoff {wait:.0f}초")
        time.sleep(wait)
    else:
        _sleep(base)


# ── 스나이핑 루프 ─────────────────────────────────────────────────────────────

def snipe(args, train_ids: list[str], client=None) -> None:
    if client is None:
        client = _login_with_retry()
        log("로그인 성공")

    passengers = _build_passengers(args)

    log(f"스나이퍼 시작: {args.dep} → {args.arr}  {_fmt_date(args.date)}")
    log(f"대상 {len(train_ids)}개 열차 | 간격 {args.interval}초 | {args.seat_option} | 대기 {'ON' if args.try_waiting else 'OFF'}")
    log("─" * 58)

    attempt = 0
    consec_errors = 0

    while True:
        attempt += 1

        # ── 1. 열차 조회 ──────────────────────────────────────────────────────
        try:
            trains = client.search_train(
                args.dep, args.arr, args.date, args.time,
                train_type=TRAIN_TYPE_MAP[getattr(args, "train_type", "ktx")],
                passengers=passengers,
                include_no_seats=True,
                include_waiting_list=True,
            )
            consec_errors = 0

        except NoResultsError:
            log(f"#{attempt:05d}  조회 결과 없음 (날짜 지났거나 해당 노선 열차 없음)")
            consec_errors += 1
            _backoff(consec_errors, args.interval)
            continue

        except NeedToLoginError:
            log(f"#{attempt:05d}  세션 만료 → 재로그인")
            client = _login_with_retry()
            consec_errors += 1
            _sleep(args.interval)
            continue

        except KorailError as exc:
            log(f"#{attempt:05d}  KorailError: {exc}")
            consec_errors += 1
            _backoff(consec_errors, args.interval)
            continue

        except Exception as exc:
            log(f"#{attempt:05d}  예외: {exc}")
            consec_errors += 1
            _backoff(consec_errors, args.interval)
            continue

        # ── 2. 각 대상 열차 확인 ─────────────────────────────────────────────
        retry_immediately = False

        for train_id in train_ids:
            target = find_train_by_id(trains, train_id)
            if target is None:
                continue

            info = normalize_train(target, 0)
            has_seat = info["has_general_seat"] or info["has_special_seat"]
            has_wait = info["has_waiting_list"]
            label = _seat_label(info)
            t_str = f"{info['train_no']} {_fmt_time(info['dep_time'])}"

            if has_seat or has_wait:
                log(f"#{attempt:05d}  🎯 {t_str}  [{label}]")
            else:
                log(f"#{attempt:05d}     {t_str}  [{label}]")

            if not (has_seat or (args.try_waiting and has_wait)):
                continue

            # ── 3. 예약 시도 ─────────────────────────────────────────────────
            log(f"  → 예약 시도...")
            try:
                reservation = client.reserve(
                    target,
                    passengers=passengers,
                    option=RESERVE_OPTION_MAP[args.seat_option],
                    try_waiting=args.try_waiting,
                )
                _notify_success(normalize_reservation(reservation))
                return  # ✅ 성공 종료

            except SoldOutError:
                log("  → 선점 경쟁 패배 (즉시 재조회)")
                retry_immediately = True
                break

            except NeedToLoginError:
                log("  → 예약 중 세션 만료 → 재로그인")
                client = _login_with_retry()
                retry_immediately = True
                break

            except KorailError as exc:
                log(f"  → 예약 실패: {exc}")
                break

        if retry_immediately:
            time.sleep(0.5)
            continue

        _sleep(args.interval)


# ── 인터랙티브 세션 ───────────────────────────────────────────────────────────

def _interactive_session() -> tuple:
    print()
    print("=" * 58)
    print("  KTX 취소표 스나이퍼")
    print("=" * 58)
    print()

    # 출발 / 도착
    dep = ""
    while not dep:
        dep = _ask("출발역  (예: 서울)").strip()
    arr = ""
    while not arr:
        arr = _ask("도착역  (예: 부산)").strip()

    # 날짜
    date_str = ""
    while not date_str:
        raw = _ask("날짜    (예: 20260601 / 6월1일 / 6/1)")
        date_str = _parse_date(raw) or ""
        if not date_str:
            print("    날짜를 인식할 수 없습니다.")
            continue
        try:
            d = datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            print("    날짜 형식 오류입니다.")
            date_str = ""
            continue
        if d < date_cls.today():
            print(f"    ⚠  오늘 이전 날짜입니다: {_fmt_date(date_str)}")
            date_str = ""
            continue
        print(f"    → {d.year}년 {d.month}월 {d.day}일 ({['월','화','수','목','금','토','일'][d.weekday()]})")

    # 시간대
    from_time = ""
    while not from_time:
        raw = _ask("시작 시각  (예: 0900 / 9시 / 9:00, 기본 00:00)", default="0000")
        from_time = _parse_time(raw or "0000") or ""
        if not from_time:
            print("    시각을 인식할 수 없습니다.")

    to_time = ""
    while not to_time:
        raw = _ask("종료 시각  (예: 1300 / 13시 / 23:59, 기본 23:59)", default="2359")
        to_time = _parse_time(raw or "2359") or ""
        if not to_time:
            print("    시각을 인식할 수 없습니다.")

    if from_time > to_time:
        print(f"    ⚠  시작({_fmt_time(from_time)}) > 종료({_fmt_time(to_time)}) — 범위를 교환합니다.")
        from_time, to_time = to_time, from_time

    print()
    print(f"  조회 중... {dep} → {arr}  {_fmt_date(date_str)}  "
          f"{_fmt_time(from_time)} ~ {_fmt_time(to_time)}")

    # 로그인 & 열차 조회
    client = _login_with_retry()

    try:
        trains = client.search_train(
            dep, arr, date_str, from_time,
            train_type=TRAIN_TYPE_MAP["ktx"],
            include_no_seats=True,
            include_waiting_list=True,
        )
    except NoResultsError:
        sys.exit(f"\n  해당 구간/날짜에 KTX 열차가 없습니다: {dep}→{arr}  {_fmt_date(date_str)}")
    except Exception as exc:
        sys.exit(f"\n  열차 조회 실패: {exc}")

    # 종료 시각 필터
    in_range = [t for t in trains if normalize_train(t, 0)["dep_time"] <= to_time]
    if not in_range:
        sys.exit(
            f"\n  {_fmt_time(from_time)}~{_fmt_time(to_time)} 범위에 열차가 없습니다.\n"
            f"  시간 범위를 늘리거나 날짜를 확인하세요."
        )

    print()
    _print_train_table(in_range)
    print()

    # 열차 선택
    selected_idx: list[int] = []
    while not selected_idx:
        raw = _ask("스나이핑할 열차 번호  (예: 1  /  1,3  /  1-3  /  all)")
        selected_idx = _parse_selection(raw, len(in_range)) or []
        if not selected_idx:
            print("    올바른 번호를 입력하세요.")

    selected_trains = [in_range[i - 1] for i in selected_idx]
    train_ids = [build_train_id(t) for t in selected_trains]
    print(f"\n  → {len(train_ids)}개 열차 선택됨")

    # 좌석 선호
    print()
    seat_map = [
        ("1", "일반석 우선 (general-first)",  "general-first"),
        ("2", "일반석만   (general-only)",    "general-only"),
        ("3", "특실 우선  (special-first)",   "special-first"),
        ("4", "특실만     (special-only)",    "special-only"),
    ]
    print("  좌석 선호:")
    for k, label, _ in seat_map:
        print(f"    {k}) {label}")
    raw = _ask("선택", default="1")
    seat_option = next((v for k, _, v in seat_map if raw.strip() == k), "general-first")

    # 예약 대기
    raw = _ask("좌석 없으면 예약대기도 시도?  (y/N)", default="N")
    try_waiting = raw.lower() in ("y", "yes", "예")

    # 폴링 간격
    raw = _ask("폴링 간격 초  (기본 45, 최소 30)", default="45")
    try:
        interval = float(raw)
    except ValueError:
        interval = 45.0
    if interval < 30:
        print("  ⚠  30초 미만은 차단 위험 → 45초로 설정합니다.")
        interval = 45.0

    args = SimpleNamespace(
        dep=dep, arr=arr,
        date=date_str, time=from_time,
        seat_option=seat_option, train_type="ktx",
        try_waiting=try_waiting, interval=interval,
        adults=1, children=0, seniors=0, toddlers=0,
    )
    print()
    return args, train_ids, client


# ── CLI 파서 ─────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KTX 취소표 스나이핑 봇",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
인터랙티브 모드 (권장):
  python3 sniper.py

CLI 모드:
  python3 sniper.py 서울 부산 20260601 090000 \\
      --train-id "ktx:v1:..." --seat-option general-first --interval 45
""",
    )
    parser.add_argument("dep",  nargs="?", help="출발역")
    parser.add_argument("arr",  nargs="?", help="도착역")
    parser.add_argument("date", nargs="?", help="출발일 YYYYMMDD")
    parser.add_argument("time", nargs="?", help="희망 출발 시각 HHMMSS")
    parser.add_argument(
        "--train-id", action="append", dest="train_ids", metavar="ID",
        help="대상 train_id (여러 번 지정 가능, CLI 모드 전용)",
    )
    parser.add_argument("--seat-option", choices=sorted(RESERVE_OPTION_MAP), default="general-first")
    parser.add_argument("--train-type",  choices=sorted(TRAIN_TYPE_MAP), default="ktx")
    parser.add_argument("--interval",    type=float, default=45.0)
    parser.add_argument("--try-waiting", action="store_true")
    parser.add_argument("--adults",   type=int, default=1)
    parser.add_argument("--children", type=int, default=0)
    parser.add_argument("--seniors",  type=int, default=0)
    parser.add_argument("--toddlers", type=int, default=0)
    return parser


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # 인자 없으면 인터랙티브 모드
    if not args.dep:
        try:
            cfg, train_ids, client = _interactive_session()
        except KeyboardInterrupt:
            print("\n[중단]")
            sys.exit(0)
        try:
            snipe(cfg, train_ids, client)
        except KeyboardInterrupt:
            print("\n[중단] Ctrl+C — 스나이퍼 종료")
            sys.exit(0)
        return

    # CLI 모드 검증
    missing = [n for n, v in [("dep", args.dep), ("arr", args.arr),
                               ("date", args.date), ("time", args.time)] if not v]
    if missing:
        parser.error(f"CLI 모드는 dep/arr/date/time 모두 필요합니다. 누락: {missing}")

    if not args.train_ids:
        parser.error("CLI 모드는 --train-id 가 필요합니다. 인자 없이 실행하면 인터랙티브 모드로 진입합니다.")

    for tid in args.train_ids:
        if not tid.startswith("ktx:v1:"):
            parser.error(f"잘못된 train_id: {tid!r} — 'ktx:v1:' 로 시작해야 합니다.")

    if args.interval < 30:
        log(f"⚠  --interval {args.interval}초 — 최소 30초 권장 (anti-bot 차단 위험)")

    cfg = SimpleNamespace(
        dep=args.dep, arr=args.arr,
        date=args.date, time=args.time,
        seat_option=args.seat_option, train_type=args.train_type,
        try_waiting=args.try_waiting, interval=args.interval,
        adults=args.adults, children=args.children,
        seniors=args.seniors, toddlers=args.toddlers,
    )
    try:
        snipe(cfg, args.train_ids)
    except KeyboardInterrupt:
        print("\n[중단] Ctrl+C — 스나이퍼 종료")
        sys.exit(0)


if __name__ == "__main__":
    main()
