#!/usr/bin/env python3
"""KTX 취소표 스나이퍼 — 터미널(tmux) 전용"""
from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import date as date_cls, datetime
from pathlib import Path
from types import SimpleNamespace

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

# ── ktx_booking / srt_booking import ────────────────────────────────────────

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
    sys.exit(f"ktx_booking.py 없음. setup.sh를 먼저 실행하세요.\n오류: {exc}")

try:
    from srt_booking import (  # type: ignore[import]
        build_srt_client, build_srt_train_id, find_srt_train_by_id,
        normalize_srt_train, normalize_srt_reservation,
        is_srt_soldout_error, SEAT_OPTION_MAP as SRT_SEAT_OPTION_MAP,
    )
    from SRT import Adult as SRTAdult
    from SRT.errors import SRTLoginError, SRTNotLoggedInError, SRTResponseError, SRTNetFunnelError
    _SRT_AVAILABLE = True
except ImportError:
    _SRT_AVAILABLE = False

# ── ANSI 컬러 ────────────────────────────────────────────────────────────────

R  = "\033[0m"       # reset
B  = "\033[1m"       # bold
DIM= "\033[2m"       # dim
RED    = "\033[31m"
GRN    = "\033[32m"
YEL    = "\033[33m"
BLU    = "\033[34m"
CYN    = "\033[36m"
WHT    = "\033[97m"
GRAY   = "\033[90m"

def c(color: str, text: str) -> str:
    return f"{color}{text}{R}"

# ── 로그 ─────────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "info") -> None:
    ts   = datetime.now().strftime("%H:%M:%S")
    icon = {
        "info":    c(GRAY,  f"[{ts}]"),
        "warn":    c(YEL,   f"[{ts}]"),
        "success": c(GRN,   f"[{ts}]"),
        "error":   c(RED,   f"[{ts}]"),
    }.get(level, c(GRAY, f"[{ts}]"))

    color = {"warn": YEL, "success": GRN, "error": RED}.get(level, R)
    print(f"{icon} {color}{msg}{R}", flush=True)

def _discord_notify(res: dict) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return
    dep_t = _fmt_time(res.get("dep_time", ""))
    arr_t = _fmt_time(res.get("arr_time", ""))
    buy_d = res.get("buy_limit_date", "?")
    buy_t = _fmt_time(res.get("buy_limit_time", "")) if len(res.get("buy_limit_time", "")) >= 4 else res.get("buy_limit_time", "?")
    buy_date = _fmt_date(buy_d) if len(buy_d) == 8 else buy_d
    user_id = os.environ.get("DISCORD_USER_ID", "").strip()
    mention = f"<@{user_id}>" if user_id else "@here"
    payload = {
        "content": mention,
        "embeds": [{
            "title": "🎉 KTX 예약 성공!",
            "color": 0x22c55e,
            "fields": [
                {"name": "예약번호", "value": res.get("reservation_id", "?"), "inline": True},
                {"name": "열차",     "value": f"{res.get('train_type','KTX')} {res.get('train_no','?')}", "inline": True},
                {"name": "구간",     "value": f"{res.get('dep_name','?')} {dep_t} → {res.get('arr_name','?')} {arr_t}", "inline": False},
                {"name": "운임",     "value": f"{res.get('price','?')}원", "inline": True},
                {"name": "⚠ 결제기한", "value": f"{buy_date} {buy_t}", "inline": True},
            ],
            "footer": {"text": "Korail 앱 또는 letskorail.com 에서 결제기한 내 결제하세요"},
        }]
    }
    try:
        import requests as _req
        resp = _req.post(url, json=payload, timeout=10)
        if resp.status_code not in (200, 204):
            log(f"Discord 알림 실패 (HTTP {resp.status_code})", "warn")
    except Exception as exc:
        log(f"Discord 알림 전송 오류: {exc}", "warn")

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
    sys.stdout.write("\a\a\a"); sys.stdout.flush()

# ── 날짜 / 시각 파서 ──────────────────────────────────────────────────────────

def _parse_date(raw: str) -> str | None:
    s = raw.strip().replace(" ", "")
    if re.fullmatch(r"\d{8}", s):
        return s
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
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

def _parse_time(raw: str) -> str | None:
    s = raw.strip().replace(" ", "")
    m = re.search(r"(\d{1,2})시(?:\s*(\d{1,2})분)?", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}{mi:02d}00"
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}{mi:02d}00"
    if re.fullmatch(r"\d{4}", s):
        h, mi = int(s[:2]), int(s[2:])
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{s}00"
    if re.fullmatch(r"\d{3}", s):
        h, mi = int(s[0]), int(s[1:])
        if 0 <= h <= 9 and 0 <= mi <= 59:
            return f"0{s}00"
    if re.fullmatch(r"\d{1,2}", s):
        h = int(s)
        if 0 <= h <= 23:
            return f"{h:02d}0000"
    return None

def _fmt_time(t: str) -> str:
    return f"{t[:2]}:{t[2:4]}" if len(t) >= 4 else t

def _fmt_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d

# ── 페이지네이션 조회 ─────────────────────────────────────────────────────────

def _search_all_trains(client, dep: str, arr: str, date: str,
                        from_time: str, to_time: str) -> list:
    """Korail API 1회 호출당 ~10개 제한 우회 — to_time까지 반복 조회."""
    collected: list = []
    seen_ids: set[str] = set()
    cursor = from_time
    page = 0

    for _ in range(20):
        page += 1
        print(f"  {c(GRAY, f'({page}회 조회 중...)')}", end="\r", flush=True)
        try:
            batch = client.search_train(
                dep, arr, date, cursor,
                train_type=TRAIN_TYPE_MAP["ktx"],
                passengers=[AdultPassenger()],
                include_no_seats=True,
                include_waiting_list=True,
            )
        except NoResultsError:
            break
        except Exception:
            break

        added = False
        last_dep = cursor
        for t in batch:
            info  = normalize_train(t, 0)
            dep_t = info["dep_time"]
            tid   = build_train_id(t)
            if dep_t > to_time:
                continue
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            collected.append(t)
            added = True
            if dep_t > last_dep:
                last_dep = dep_t

        if not added:
            break

        h, m = int(last_dep[:2]), int(last_dep[2:4])
        m += 1
        if m >= 60:
            m, h = 0, h + 1
        cursor = f"{h:02d}{m:02d}00"
        if cursor > to_time:
            break

        time.sleep(0.8)

    print(" " * 30, end="\r")  # 진행 메시지 지우기
    return collected

# ── 입력 헬퍼 ────────────────────────────────────────────────────────────────

def _ask(prompt: str, default: str | None = None) -> str:
    hint = f" {c(GRAY, f'[{default}]')}" if default is not None else ""
    try:
        val = input(f"  {c(CYN, '▶')} {prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else (default or "")

def _parse_selection(raw: str, max_n: int) -> list[int] | None:
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
        parts.append(c(GRN, "일반석 ✓"))
    if info["has_special_seat"]:
        parts.append(c(GRN, "특실 ✓"))
    if info["has_waiting_list"] and not (info["has_general_seat"] or info["has_special_seat"]):
        parts.append(c(YEL, "대기열"))
    return " | ".join(parts) if parts else c(GRAY, "매진")

def _print_train_table(trains: list) -> None:
    _print_train_table_generic(trains, normalize_train)

def _print_train_table_generic(trains: list, norm_fn) -> None:
    header = f"  {c(B, '  #')}  {c(B,'열차번호')}  {c(B,'출발')}    {c(B,'도착')}    {c(B,'좌석 상태')}"
    print(header)
    print(f"  {c(GRAY, '─' * 50)}")
    for i, t in enumerate(trains, 1):
        info   = norm_fn(t, i)
        dep    = _fmt_time(info["dep_time"])
        arr    = _fmt_time(info["arr_time"])
        label  = _seat_label(info)
        num_c  = c(WHT, f"{i:>3}")
        no_c   = c(B,   f"{info['train_no']:>8}")
        print(f"  {num_c}  {no_c}  {dep} → {arr}  {label}")

# ── 로그인 ────────────────────────────────────────────────────────────────────

def _login_with_retry(max_attempts: int = 3):
    for attempt in range(1, max_attempts + 1):
        try:
            return build_client()
        except NeedToLoginError as exc:
            log(f"로그인 실패 ({attempt}/{max_attempts}): {exc}", "warn")
        except SystemExit:
            raise
        except Exception as exc:
            log(f"로그인 오류 ({attempt}/{max_attempts}): {exc}", "warn")
        if attempt < max_attempts:
            time.sleep(5 * attempt)
    sys.exit(c(RED, "로그인 재시도 초과 — KSKILL_KTX_ID / KSKILL_KTX_PASSWORD 를 확인하세요."))

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

# ── 슬립 ─────────────────────────────────────────────────────────────────────

def _sleep(base: float) -> None:
    time.sleep(max(5.0, base + base * random.uniform(-0.1, 0.1)))

def _backoff(n_errors: int, base: float) -> None:
    if n_errors >= 3:
        wait = min(300.0, 30.0 * (2 ** (n_errors - 3)))
        log(f"연속 오류 {n_errors}회 → backoff {wait:.0f}초", "warn")
        time.sleep(wait)
    else:
        _sleep(base)

# ── 성공 출력 ────────────────────────────────────────────────────────────────

def _print_success(res: dict) -> None:
    buy_d    = res.get("buy_limit_date", "?")
    buy_t_r  = res.get("buy_limit_time", "")
    buy_t    = _fmt_time(buy_t_r) if len(buy_t_r) >= 4 else buy_t_r
    dep_t    = _fmt_time(res["dep_time"]) if len(res.get("dep_time","")) >= 4 else res.get("dep_time","?")
    arr_t    = _fmt_time(res["arr_time"]) if len(res.get("arr_time","")) >= 4 else res.get("arr_time","?")
    buy_date = _fmt_date(buy_d) if len(buy_d) == 8 else buy_d

    sep = c(GRN, "═" * 52)
    print()
    print(sep)
    print(c(GRN+B, "  ✅  KTX 예약 성공!"))
    print(sep)
    print(f"  {c(GRAY,'예약번호')}  {c(WHT+B, res['reservation_id'])}")
    print(f"  {c(GRAY,'열차    ')}  {res.get('train_type','KTX')} {res['train_no']}")
    print(f"  {c(GRAY,'구간    ')}  {res['dep_name']} {dep_t}  →  {res['arr_name']} {arr_t}")
    print(f"  {c(GRAY,'운임    ')}  {res.get('price','?')}원")
    print(f"  {c(GRAY,'결제기한')}  {c(RED+B, f'{buy_date} {buy_t}')} 까지")
    print(sep)
    print(c(YEL, "  ⚠  Korail 앱 또는 www.letskorail.com 에서 결제 기한 내에 결제하세요!"))
    print(sep)
    print(flush=True)
    _bell()
    _macos_notify(
        "KTX 예약 성공 🎉",
        f"예약번호 {res['reservation_id']} | {res['dep_name']}→{res['arr_name']} {dep_t} | 결제기한 {buy_date} {buy_t}",
    )
    _discord_notify(res)

# ── 스나이핑 루프 ─────────────────────────────────────────────────────────────

def snipe(args, train_ids: list[str], client=None) -> None:
    if client is None:
        client = _login_with_retry()
        log("로그인 성공")

    passengers = _build_passengers(args)
    log(f"스나이퍼 시작  {c(B, args.dep)} → {c(B, args.arr)}  {c(B, _fmt_date(args.date))}")
    log(f"대상 {c(B,str(len(train_ids)))}개 열차  |  간격 {args.interval:.0f}초  |  {args.seat_option}  |  대기 {'ON' if args.try_waiting else 'OFF'}")
    print(c(GRAY, "  " + "─" * 52), flush=True)

    is_srt        = getattr(args, "is_srt", False)
    attempt       = 0
    consec_errors = 0

    while True:
        attempt += 1

        # ── 열차 조회 ─────────────────────────────────────────────────────────
        try:
            if is_srt:
                trains = client.search_train(
                    args.dep, args.arr, args.date, args.time,
                    available_only=False,
                )
            else:
                trains = client.search_train(
                    args.dep, args.arr, args.date, args.time,
                    train_type=TRAIN_TYPE_MAP[getattr(args, "train_type", "ktx")],
                    passengers=passengers,
                    include_no_seats=True,
                    include_waiting_list=True,
                )
            consec_errors = 0

        except NoResultsError:
            log(f"#{attempt:05d}  조회 결과 없음", "warn")
            consec_errors += 1
            _backoff(consec_errors, args.interval)
            continue
        except (NeedToLoginError, SRTNotLoggedInError) if _SRT_AVAILABLE else (NeedToLoginError,):
            log(f"#{attempt:05d}  세션 만료 → 재로그인", "warn")
            if is_srt:
                try:
                    client = build_srt_client()
                except Exception:
                    pass
            else:
                client = _login_with_retry()
            consec_errors += 1
            _sleep(args.interval)
            continue
        except Exception as exc:
            if _SRT_AVAILABLE and isinstance(exc, SRTNetFunnelError):
                log(f"#{attempt:05d}  NetFunnel 오류 → 재로그인 후 대기", "warn")
                try:
                    client = build_srt_client()
                except Exception:
                    pass
                consec_errors += 1
                wait = min(300.0, 60.0 * consec_errors)
                log(f"  {wait:.0f}초 대기...", "warn")
                time.sleep(wait)
            else:
                log(f"#{attempt:05d}  오류: {exc}", "warn")
                consec_errors += 1
                _backoff(consec_errors, args.interval)
            continue

        retry_immediately = False

        for train_id in train_ids:
            if is_srt:
                target = find_srt_train_by_id(trains, train_id)
                if target is None:
                    continue
                info = normalize_srt_train(target, 0)
            else:
                target = find_train_by_id(trains, train_id)
                if target is None:
                    continue
                info = normalize_train(target, 0)

            has_seat = info["has_general_seat"] or info["has_special_seat"]
            has_wait = info["has_waiting_list"]
            label    = _seat_label(info)
            t_str    = f"{info['train_no']} {_fmt_time(info['dep_time'])}"

            if has_seat or has_wait:
                log(f"#{attempt:05d}  🎯 {c(B,t_str)}  [{label}]", "success")
            else:
                log(f"#{attempt:05d}     {t_str}  [{label}]")

            if not (has_seat or (args.try_waiting and has_wait)):
                continue

            log("  → 예약 시도...")
            try:
                if is_srt:
                    if args.try_waiting and has_wait and not has_seat:
                        raw_res = client.reserve_standby(
                            target, passengers=passengers,
                            special_seat=SRT_SEAT_OPTION_MAP[args.seat_option],
                        )
                    else:
                        raw_res = client.reserve(
                            target, passengers=passengers,
                            special_seat=SRT_SEAT_OPTION_MAP[args.seat_option],
                        )
                    res = normalize_srt_reservation(raw_res)
                else:
                    raw_res = client.reserve(
                        target, passengers=passengers,
                        option=RESERVE_OPTION_MAP[args.seat_option],
                        try_waiting=args.try_waiting,
                    )
                    res = normalize_reservation(raw_res)

                _print_success(res)
                return

            except SoldOutError:
                log("  → 선점 경쟁 패배 (즉시 재조회)", "warn")
                retry_immediately = True
                break
            except Exception as exc:
                if _SRT_AVAILABLE and isinstance(exc, SRTResponseError) and is_srt_soldout_error(exc):
                    log("  → 선점 경쟁 패배 (즉시 재조회)", "warn")
                    retry_immediately = True
                elif _SRT_AVAILABLE and isinstance(exc, SRTNotLoggedInError):
                    log("  → 세션 만료 → 재로그인", "warn")
                    try:
                        client = build_srt_client()
                    except Exception:
                        pass
                    retry_immediately = True
                elif isinstance(exc, NeedToLoginError):
                    log("  → 세션 만료 → 재로그인", "warn")
                    client = _login_with_retry()
                    retry_immediately = True
                else:
                    log(f"  → 예약 실패: {exc}", "warn")
                break

        if retry_immediately:
            time.sleep(0.5)
            continue

        _sleep(args.interval)

# ── 인터랙티브 세션 ───────────────────────────────────────────────────────────

def _search_all_srt_trains(client, dep, arr, date, from_time, to_time):
    """SRT 페이지네이션 조회."""
    collected: list = []
    seen_ids: set[str] = set()
    cursor = from_time
    page   = 0
    for _ in range(20):
        page += 1
        print(f"  {c(GRAY, f'({page}회 조회 중...)')}", end="\r", flush=True)
        try:
            batch = client.search_train(dep, arr, date, cursor,
                                        time_limit=to_time, available_only=False)
        except Exception:
            break
        if not batch:
            break
        new_in_range = False
        last_dep = cursor
        for t in batch:
            dep_t = t.dep_time
            tid   = build_srt_train_id(t)
            if dep_t > to_time:
                continue
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            collected.append(t)
            new_in_range = True
            if dep_t > last_dep:
                last_dep = dep_t
        if not new_in_range:
            break
        h, m = int(last_dep[:2]), int(last_dep[2:4])
        m += 1
        if m >= 60:
            m, h = 0, h + 1
        cursor = f"{h:02d}{m:02d}00"
        if cursor > to_time:
            break
        time.sleep(0.8)
    print(" " * 30, end="\r")
    return collected


def _run_session() -> None:
    """1회 전체 플로우: 입력 → 조회 → 선택 → 스나이핑"""
    print()
    print(c(BLU+B, "═" * 52))
    print(c(BLU+B, "  🚄  KTX / SRT 취소표 스나이퍼"))
    print(c(BLU+B, "═" * 52))
    print()

    # 열차 종류 선택
    print(f"  {c(B, '열차 종류:')}")
    print(f"    {c(CYN,'1')}) KTX")
    if _SRT_AVAILABLE:
        print(f"    {c(CYN,'2')}) SRT")
    else:
        print(f"    {c(GRAY,'2')}) SRT  {c(RED,'(SRTrain 미설치 — pip install SRTrain)')}")
    raw      = _ask("선택", default="1")
    is_srt   = (raw.strip() == "2" and _SRT_AVAILABLE)
    tag      = "SRT" if is_srt else "KTX"
    dep_hint = "수서" if is_srt else "서울"
    print()

    # 출발 / 도착
    dep = ""
    while not dep:
        dep = _ask(f"출발역  (예: {dep_hint})").strip()

    arr = ""
    while not arr:
        arr = _ask("도착역  (예: 부산)").strip()

    # 날짜
    date_str = ""
    while not date_str:
        raw = _ask("날짜    (예: 20260601 / 6월1일 / 6/1)")
        date_str = _parse_date(raw) or ""
        if not date_str:
            print(c(RED, "    날짜를 인식할 수 없습니다."))
            continue
        try:
            d = datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            print(c(RED, "    날짜 형식 오류입니다."))
            date_str = ""
            continue
        if d < date_cls.today():
            print(c(YEL, f"    ⚠  오늘 이전 날짜입니다: {_fmt_date(date_str)}"))
            date_str = ""
            continue
        dow = ["월","화","수","목","금","토","일"][d.weekday()]
        print(f"    {c(GRAY, f'→ {d.year}년 {d.month}월 {d.day}일 ({dow})')}")

    # 시간대
    from_time = ""
    while not from_time:
        raw = _ask("시작 시각  (예: 0900 / 9시 / 9:00)", default="0000")
        from_time = _parse_time(raw or "0000") or ""
        if not from_time:
            print(c(RED, "    시각을 인식할 수 없습니다."))

    to_time = ""
    while not to_time:
        raw = _ask("종료 시각  (예: 1300 / 13시 / 23:59)", default="2359")
        to_time = _parse_time(raw or "2359") or ""
        if not to_time:
            print(c(RED, "    시각을 인식할 수 없습니다."))

    if from_time > to_time:
        print(c(YEL, "    ⚠  시작 > 종료 — 교환합니다."))
        from_time, to_time = to_time, from_time

    print()
    print(f"  {c(GRAY, f'[{tag}] 조회 중... {dep} → {arr}  {_fmt_date(date_str)}  {_fmt_time(from_time)} ~ {_fmt_time(to_time)}')}")

    # 로그인
    if is_srt:
        try:
            client = build_srt_client()
        except SystemExit as exc:
            print(c(RED, f"  {exc}"))
            return
    else:
        client = _login_with_retry()

    # 페이지네이션 조회
    if is_srt:
        trains = _search_all_srt_trains(client, dep, arr, date_str, from_time, to_time)
    else:
        trains = _search_all_trains(client, dep, arr, date_str, from_time, to_time)

    if not trains:
        print(c(RED, f"  해당 구간/날짜/시간대에 {tag} 열차가 없습니다."))
        return

    if is_srt:
        trains.sort(key=lambda t: t.dep_time)
        # normalize for table display
        norm_fn  = normalize_srt_train
        id_fn    = build_srt_train_id
    else:
        trains.sort(key=lambda t: normalize_train(t, 0)["dep_time"])
        norm_fn = normalize_train
        id_fn   = build_train_id

    print(f"  {c(GRN, f'총 {len(trains)}개 열차 발견')}")
    print()
    _print_train_table_generic(trains, norm_fn)
    print()

    # 열차 선택
    selected_idx: list[int] = []
    while not selected_idx:
        raw = _ask("스나이핑할 열차  (예: 1  / 1,3  / 1-3  / all)")
        selected_idx = _parse_selection(raw, len(trains)) or []
        if not selected_idx:
            print(c(RED, "    올바른 번호를 입력하세요."))

    selected_trains = [trains[i - 1] for i in selected_idx]
    train_ids = [id_fn(t) for t in selected_trains]
    print(f"  {c(CYN, f'→ {len(train_ids)}개 열차 선택됨')}")
    print()

    # 좌석 선호
    seat_map = [
        ("1", f"일반석 우선  {c(GRAY,'(general-first)')}",  "general-first"),
        ("2", f"일반석만    {c(GRAY,'(general-only)')}",    "general-only"),
        ("3", f"특실 우선   {c(GRAY,'(special-first)')}",   "special-first"),
        ("4", f"특실만      {c(GRAY,'(special-only)')}",    "special-only"),
    ]
    print(f"  {c(B, '좌석 선호:')}")
    for k, label, _ in seat_map:
        print(f"    {c(CYN, k)}) {label}")
    raw = _ask("선택", default="1")
    seat_option = next((v for k, _, v in seat_map if raw.strip() == k), "general-first")

    raw = _ask("예약대기도 시도?  (y/N)", default="N")
    try_waiting = raw.lower() in ("y", "yes", "예")

    raw = _ask("폴링 간격 초  (기본 45, 최소 30)", default="45")
    try:
        interval = float(raw)
    except ValueError:
        interval = 45.0
    if interval < 30:
        print(c(YEL, "  ⚠  30초 미만은 차단 위험 → 45초로 설정합니다."))
        interval = 45.0

    args = SimpleNamespace(
        dep=dep, arr=arr,
        date=date_str, time=from_time,
        seat_option=seat_option,
        train_type="srt" if is_srt else "ktx",
        try_waiting=try_waiting, interval=interval,
        adults=1, children=0, seniors=0, toddlers=0,
        is_srt=is_srt,
    )
    print()

    snipe(args, train_ids, client)
    sys.exit(0)  # 예매/예매대기 완료 → 즉시 종료

# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    while True:
        try:
            _run_session()
        except KeyboardInterrupt:
            print(f"\n{c(GRAY, '[중단] Ctrl+C')}")
            sys.exit(0)

        print()
        raw = _ask("다시 검색하시겠습니까?  (y/N)", default="N")
        if raw.lower() not in ("y", "yes", "예"):
            print(c(GRAY, "종료합니다."))
            break
        print()

def _ensure_tmux() -> None:
    """tmux 밖에서 실행 시 자동으로 tmux 세션 'ktx'를 만들어 재실행."""
    if os.environ.get("TMUX"):
        return  # 이미 tmux 안 — 그대로 진행

    if not shutil.which("tmux"):
        print("tmux가 없습니다. brew install tmux 후 다시 실행하세요.")
        sys.exit(1)

    session  = "ktx"
    script   = str(Path(__file__).resolve())
    py       = sys.executable

    exists = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    ).returncode == 0

    if exists:
        # 이미 돌고 있는 세션 — 그대로 attach
        print(f"기존 tmux 세션 '{session}'에 연결합니다 (Ctrl+B, D 로 detach)...")
        os.execvp("tmux", ["tmux", "attach-session", "-t", session])
    else:
        # 새 세션 생성 + 이 스크립트 실행
        print(f"tmux 세션 '{session}' 생성 후 실행합니다 (Ctrl+B, D 로 detach)...")
        os.execvp("tmux", ["tmux", "new-session", "-s", session, py, script])
    # os.execvp 이후 코드는 실행되지 않음


if __name__ == "__main__":
    _ensure_tmux()
    # tmux 안에서 실행 중임을 알림
    print(c(GRAY, "  tmux 세션 'ktx'  |  detach: Ctrl+B → D  |  재연결: tmux attach -t ktx"))
    main()
