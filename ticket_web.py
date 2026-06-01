#!/usr/bin/env python3
"""KTX / SRT 취소표 스나이퍼 — 로컬 전용 웹 앱"""
from __future__ import annotations

import json
import queue
import random
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

# ── ktx_booking / srt_booking import ────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent))
try:
    from ktx_booking import (  # type: ignore[import]
        AdultPassenger,
        KorailError, NeedToLoginError, NoResultsError, SoldOutError,
        RESERVE_OPTION_MAP, TRAIN_TYPE_MAP,
        build_client, build_train_id, find_train_by_id,
        normalize_reservation, normalize_train,
    )
except ImportError as exc:
    sys.exit(f"ktx_booking.py가 없습니다. setup.sh를 먼저 실행하세요.\n오류: {exc}")

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
    SRTAdult = None  # type: ignore[assignment]
    SRTLoginError = SRTNotLoggedInError = SRTResponseError = SRTNetFunnelError = RuntimeError  # type: ignore[misc,assignment]

try:
    from flask import Flask, Response, jsonify, render_template, request, stream_with_context
except ImportError:
    sys.exit("Flask 없음: python3 -m pip install flask")

try:
    from waitress import serve as _serve
except ImportError:
    _serve = None

# ── Flask 앱 ─────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config["JSON_AS_ASCII"] = False

_session: dict = {
    "client":    None,
    "credentials": {"ktx": None, "srt": None},
    "thread":    None,
    "log_queue": None,
    "stop_flag": threading.Event(),
    "search":    None,
    "snipe":     None,
    "logs":      [],
    "last_poll": None,
    "success":   None,
}

_state_lock = threading.Lock()
_MAX_LOG_HISTORY = 1000

# ── 유틸 ─────────────────────────────────────────────────────────────────────

def _fmt_time(t: str) -> str:
    return f"{t[:2]}:{t[2:4]}" if len(t) >= 4 else t

def _fmt_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d

def _seat_label(info: dict) -> str:
    parts = []
    if info["has_general_seat"]:  parts.append("일반석")
    if info["has_special_seat"]:  parts.append("특실")
    if info["has_waiting_list"] and not (info["has_general_seat"] or info["has_special_seat"]):
        parts.append("대기열")
    return " / ".join(parts) if parts else "매진"

def _is_running() -> bool:
    thread = _session.get("thread")
    return bool(thread and thread.is_alive())

def _normalize_login(data: dict, train_type: str) -> tuple[str, str]:
    login = data.get("login") if isinstance(data.get("login"), dict) else data
    user_id = (
        login.get(f"{train_type}_id")
        or login.get("user_id")
        or login.get("id")
        or ""
    ).strip()
    password = (
        login.get(f"{train_type}_password")
        or login.get("password")
        or ""
    ).strip()
    return user_id, password

def _remember_credentials(train_type: str, user_id: str, password: str) -> None:
    with _state_lock:
        _session["credentials"][train_type] = {"id": user_id, "password": password}

def _get_credentials(train_type: str) -> tuple[str | None, str | None]:
    creds = (_session.get("credentials") or {}).get(train_type) or {}
    return creds.get("id"), creds.get("password")

def _clear_credentials() -> None:
    with _state_lock:
        _session["client"] = None
        _session["credentials"] = {"ktx": None, "srt": None}

def _build_client_for(train_type: str, user_id: str | None = None, password: str | None = None):
    if not user_id or not password:
        user_id, password = _get_credentials(train_type)
    if train_type == "srt":
        return build_srt_client(user_id, password)
    return build_client(user_id, password)

def _passenger_count(data: dict) -> int:
    try:
        count = int(data.get("passenger_count", 1))
    except (TypeError, ValueError):
        count = 1
    return min(4, max(1, count))

def _remember_search(params: dict, trains: list[dict]) -> None:
    with _state_lock:
        _session["search"] = {
            "params": params,
            "trains": trains,
            "count": len(trains),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

def _remember_snipe(payload: dict) -> None:
    sanitized = {k: v for k, v in payload.items() if k not in {"login", "password", "user_id", "id", "ktx_id", "ktx_password", "srt_id", "srt_password"}}
    with _state_lock:
        _session["snipe"] = {
            "payload": sanitized,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        _session["logs"] = []
        _session["last_poll"] = None
        _session["success"] = None

def _record_event(msg: dict) -> None:
    with _state_lock:
        if msg.get("type") in ("log", "done", "error"):
            _session["logs"].append(msg)
            if len(_session["logs"]) > _MAX_LOG_HISTORY:
                _session["logs"] = _session["logs"][-_MAX_LOG_HISTORY:]
        elif msg.get("type") == "poll":
            _session["last_poll"] = msg
        elif msg.get("type") == "success":
            _session["success"] = msg.get("reservation")
            _session["logs"].append(msg)
            if len(_session["logs"]) > _MAX_LOG_HISTORY:
                _session["logs"] = _session["logs"][-_MAX_LOG_HISTORY:]

# ── 라우트 ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("ticket_web.html")

def _search_all_trains(client, dep, arr, date, from_time, to_time, passenger_count=1):
    collected: list = []
    seen_ids: set[str] = set()
    cursor = from_time
    passengers = [AdultPassenger() for _ in range(_passenger_count({"passenger_count": passenger_count}))]
    for _ in range(20):
        try:
            batch = client.search_train(
                dep, arr, date, cursor,
                train_type=TRAIN_TYPE_MAP["ktx"],
                passengers=passengers,
                include_no_seats=True,
                include_waiting_list=True,
            )
        except NoResultsError:
            break
        except Exception:
            break
        new_in_range = False
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
    return collected

def _search_all_srt_trains(client, dep, arr, date, from_time, to_time):
    """SRT 페이지네이션 조회 (time_limit 활용)."""
    collected: list = []
    seen_ids: set[str] = set()
    cursor = from_time
    for _ in range(20):
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
    return collected


@app.route("/api/search", methods=["POST"])
def api_search():
    data       = request.get_json() or {}
    dep        = data.get("dep", "").strip()
    arr        = data.get("arr", "").strip()
    date       = data.get("date", "").strip()
    from_time  = data.get("from_time", "000000")
    to_time    = data.get("to_time",   "235900")
    train_type = data.get("train_type", "ktx").lower()
    passenger_count = _passenger_count(data)

    if not (dep and arr and date):
        return jsonify({"error": "출발역, 도착역, 날짜를 모두 입력하세요."}), 400
    if train_type not in {"ktx", "srt"}:
        return jsonify({"error": "열차 종류는 KTX 또는 SRT만 가능합니다."}), 400
    user_id, password = _normalize_login(data, train_type)
    if not user_id or not password:
        user_id, password = _get_credentials(train_type)
    if not user_id or not password:
        return jsonify({"error": "로그인 ID와 비밀번호를 입력하세요."}), 400

    search_params = {
        "dep": dep,
        "arr": arr,
        "date": date,
        "from_time": from_time,
        "to_time": to_time,
        "train_type": train_type,
        "passenger_count": passenger_count,
    }

    if train_type == "srt":
        if not _SRT_AVAILABLE:
            return jsonify({"error": "SRTrain 패키지가 없습니다. python3 -m pip install SRTrain"}), 500
        try:
            client = build_srt_client(user_id, password)
        except SystemExit as exc:
            return jsonify({"error": str(exc)}), 401
        except Exception as exc:
            return jsonify({"error": f"SRT 로그인 실패: {exc}"}), 401
        _remember_credentials("srt", user_id, password)
        _session["client"]     = client
        _session["train_type"] = "srt"
        in_range = _search_all_srt_trains(client, dep, arr, date, from_time, to_time)
        if not in_range:
            return jsonify({"error": f"{dep}→{arr} {_fmt_date(date)} 구간에 SRT 열차가 없습니다."}), 404
        in_range.sort(key=lambda t: t.dep_time)
        result = []
        for i, t in enumerate(in_range, 1):
            info = normalize_srt_train(t, i)
            result.append({
                "index":            i,
                "train_id":         build_srt_train_id(t),
                "train_no":         info["train_no"],
                "dep_time":         _fmt_time(info["dep_time"]),
                "arr_time":         _fmt_time(info["arr_time"]),
                "has_general_seat": info["has_general_seat"],
                "has_special_seat": info["has_special_seat"],
                "has_waiting_list": info["has_waiting_list"],
                "status":           _seat_label(info),
            })
        _remember_search(search_params, result)
        return jsonify({"trains": result, "count": len(result)})

    # ── KTX ──
    try:
        client = build_client(user_id, password)
    except SystemExit as exc:
        return jsonify({"error": str(exc)}), 401
    except Exception as exc:
        return jsonify({"error": f"로그인 실패: {exc}"}), 401
    _remember_credentials("ktx", user_id, password)
    _session["client"]     = client
    _session["train_type"] = "ktx"
    in_range = _search_all_trains(client, dep, arr, date, from_time, to_time, passenger_count)
    if not in_range:
        return jsonify({"error": f"{dep}→{arr} {_fmt_date(date)} 구간에 KTX 열차가 없습니다."}), 404
    in_range.sort(key=lambda t: normalize_train(t, 0)["dep_time"])
    result = []
    for i, t in enumerate(in_range, 1):
        info = normalize_train(t, i)
        result.append({
            "index":            i,
            "train_id":         build_train_id(t),
            "train_no":         info["train_no"],
            "dep_time":         _fmt_time(info["dep_time"]),
            "arr_time":         _fmt_time(info["arr_time"]),
            "has_general_seat": info["has_general_seat"],
            "has_special_seat": info["has_special_seat"],
            "has_waiting_list": info["has_waiting_list"],
            "status":           _seat_label(info),
        })
    _remember_search(search_params, result)
    return jsonify({"trains": result, "count": len(result)})

@app.route("/api/snipe/start", methods=["POST"])
def api_snipe_start():
    old_flag = _session.get("stop_flag")
    if old_flag:
        old_flag.set()
    old_thread = _session.get("thread")
    if old_thread and old_thread.is_alive():
        old_thread.join(timeout=3.0)
    data = request.get_json() or {}
    train_type = data.get("train_type", _session.get("train_type", "ktx")).lower()
    if train_type not in {"ktx", "srt"}:
        return jsonify({"error": "열차 종류는 KTX 또는 SRT만 가능합니다."}), 400
    if train_type == "srt" and not _SRT_AVAILABLE:
        return jsonify({"error": "SRTrain 패키지가 없습니다. python3 -m pip install SRTrain"}), 500
    if not data.get("dep") or not data.get("arr") or not data.get("date") or not data.get("from_time"):
        return jsonify({"error": "출발역, 도착역, 날짜, 시작 시각이 필요합니다."}), 400
    if not all(_get_credentials(train_type)):
        return jsonify({"error": "먼저 웹 화면에서 로그인하고 열차를 조회하세요."}), 400
    if not data.get("train_ids"):
        return jsonify({"error": "열차를 하나 이상 선택하세요."}), 400
    _remember_snipe(data)
    new_stop_flag  = threading.Event()
    new_log_queue: queue.Queue = queue.Queue()
    _session["stop_flag"] = new_stop_flag
    _session["log_queue"] = new_log_queue
    t = threading.Thread(
        target=_snipe_thread,
        args=(data, new_log_queue, new_stop_flag, _session.get("client")),
        daemon=True,
    )
    _session["thread"] = t
    t.start()
    return jsonify({"status": "started"})

@app.route("/api/snipe/stream")
def api_snipe_stream():
    def generate():
        while True:
            q = _session.get("log_queue")
            if q is None:
                time.sleep(0.2)
                continue
            try:
                msg = q.get(timeout=5)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get("type") in ("success", "error", "done"):
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )

@app.route("/api/snipe/stop", methods=["POST"])
def api_snipe_stop():
    sf = _session.get("stop_flag")
    if sf:
        sf.set()
    q = _session.get("log_queue")
    if q:
        q.put({"type": "done"})
    return jsonify({"status": "stopped"})

@app.route("/api/status")
def api_status():
    running = _is_running()
    return jsonify({"running": running})

@app.route("/api/state")
def api_state():
    with _state_lock:
        return jsonify({
            "running":   _is_running(),
            "search":    _session.get("search"),
            "snipe":     _session.get("snipe"),
            "logs":      list(_session.get("logs") or []),
            "last_poll": _session.get("last_poll"),
            "success":   _session.get("success"),
        })

@app.route("/api/credentials", methods=["DELETE"])
def api_clear_credentials():
    _clear_credentials()
    return jsonify({"status": "cleared"})

# ── 스나이프 스레드 ───────────────────────────────────────────────────────────

def _snipe_thread(data, log_queue, stop_flag, client) -> None:
    dep         = data["dep"]
    arr         = data["arr"]
    date        = data["date"]
    from_time   = data["from_time"]
    to_time     = data.get("to_time", "235900")
    train_ids   = data["train_ids"]
    seat_option = data.get("seat_option", "general-first")
    try_waiting = bool(data.get("try_waiting", False))
    interval    = max(30.0, float(data.get("interval", 45)))
    train_type  = data.get("train_type", _session.get("train_type", "ktx")).lower()
    is_srt      = (train_type == "srt")
    passenger_count = _passenger_count(data)

    def push(msg_type, **kwargs):
        msg = {"type": msg_type, **kwargs}
        _record_event(msg)
        log_queue.put(msg)

    def log(msg, level="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        push("log", ts=ts, msg=msg, level=level)

    def sleep_interruptible(base):
        total    = max(5.0, base + base * random.uniform(-0.1, 0.1))
        deadline = time.time() + total
        while time.time() < deadline:
            if stop_flag.is_set():
                return False
            time.sleep(0.3)
        return True

    def login_with_retry(max_attempts=3):
        for attempt in range(1, max_attempts + 1):
            try:
                return _build_client_for(train_type)
            except (NeedToLoginError, SRTLoginError) as exc:
                log(f"로그인 실패 ({attempt}/{max_attempts}): {exc}", "warn")
            except SystemExit as exc:
                log(f"로그인 불가: {exc}", "error")
                return None
            except Exception as exc:
                log(f"로그인 오류: {exc}", "warn")
            if attempt < max_attempts:
                time.sleep(5 * attempt)
        return None

    if client is None:
        log(f"{'SRT' if is_srt else 'Korail'} 로그인 중...")
        client = login_with_retry()
    if client is None:
        push("error", msg="로그인 실패. 웹 화면의 ID / 비밀번호를 확인하세요.")
        return

    log(f"스나이퍼 시작 [{('SRT' if is_srt else 'KTX')}]: {dep} → {arr}  {_fmt_date(date)}")
    log(f"대상 {len(train_ids)}개 열차  |  성인 {passenger_count}명  |  간격 {interval:.0f}초  |  {seat_option}  |  대기 {'ON' if try_waiting else 'OFF'}")

    passengers    = [SRTAdult() for _ in range(passenger_count)] if is_srt else [AdultPassenger() for _ in range(passenger_count)]
    attempt       = 0
    consec_errors = 0

    while not stop_flag.is_set():
        attempt += 1

        # ── 열차 조회 ─────────────────────────────────────────────────────────
        try:
            if is_srt:
                trains = _search_all_srt_trains(client, dep, arr, date, from_time, to_time)
            else:
                trains = _search_all_trains(client, dep, arr, date, from_time, to_time, passenger_count)
            consec_errors = 0
        except NoResultsError:
            log(f"#{attempt:05d}  조회 결과 없음", "warn")
            consec_errors += 1
            backoff = min(300.0, 30.0 * (2 ** max(0, consec_errors - 3))) if consec_errors >= 3 else interval
            if not sleep_interruptible(backoff): break
            continue
        except (NeedToLoginError, SRTNotLoggedInError):
            log(f"#{attempt:05d}  세션 만료 → 재로그인", "warn")
            client = login_with_retry()
            if client is None:
                push("error", msg="재로그인 실패")
                return
            consec_errors += 1
            if not sleep_interruptible(interval): break
            continue
        except SRTNetFunnelError as exc:
            # NetFunnel 오류 = SRT 서버 과부하/차단 → 재로그인 후 긴 대기
            log(f"#{attempt:05d}  NetFunnel 오류 → 재로그인 후 대기", "warn")
            client = login_with_retry()
            consec_errors += 1
            wait = min(300.0, 60.0 * consec_errors)
            log(f"  {wait:.0f}초 대기...", "warn")
            if not sleep_interruptible(wait): break
            continue
        except (KorailError, SRTResponseError) as exc:
            log(f"#{attempt:05d}  오류: {exc}", "warn")
            consec_errors += 1
            if not sleep_interruptible(interval): break
            continue
        except Exception as exc:
            log(f"#{attempt:05d}  예외: {exc}", "warn")
            consec_errors += 1
            if not sleep_interruptible(interval): break
            continue

        # ── 대상 열차 확인 ────────────────────────────────────────────────────
        train_statuses    = []
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
            train_statuses.append({
                "train_no": info["train_no"],
                "dep_time": _fmt_time(info["dep_time"]),
                "status":   label,
                "alert":    has_seat or has_wait,
            })
            if not (has_seat or (try_waiting and has_wait)):
                continue
            log(f"🎯 {info['train_no']} {_fmt_time(info['dep_time'])} — {label}", "success")
            log("예약 시도 중...")

            # ── 예약 시도 ─────────────────────────────────────────────────────
            try:
                if is_srt:
                    if try_waiting and has_wait and not has_seat:
                        raw_res = client.reserve_standby(
                            target, passengers=passengers,
                            special_seat=SRT_SEAT_OPTION_MAP[seat_option],
                        )
                    else:
                        raw_res = client.reserve(
                            target, passengers=passengers,
                            special_seat=SRT_SEAT_OPTION_MAP[seat_option],
                        )
                    res = normalize_srt_reservation(raw_res)
                else:
                    raw_res = client.reserve(
                        target, passengers=passengers,
                        option=RESERVE_OPTION_MAP[seat_option],
                        try_waiting=try_waiting,
                    )
                    res = normalize_reservation(raw_res)

                dep_t     = _fmt_time(res["dep_time"])  if len(res.get("dep_time",""))  >= 4 else res.get("dep_time","?")
                arr_t     = _fmt_time(res["arr_time"])  if len(res.get("arr_time",""))  >= 4 else res.get("arr_time","?")
                buy_d     = res.get("buy_limit_date", "?")
                buy_t_raw = res.get("buy_limit_time", "")
                buy_t     = _fmt_time(buy_t_raw) if len(buy_t_raw) >= 4 else buy_t_raw
                res_payload = {
                    "reservation_id": res["reservation_id"],
                    "train_no":       res["train_no"],
                    "train_type":     res.get("train_type", "SRT" if is_srt else "KTX"),
                    "dep_name":       res["dep_name"],
                    "arr_name":       res["arr_name"],
                    "dep_time":       dep_t,
                    "arr_time":       arr_t,
                    "price":          res.get("price", "?"),
                    "buy_limit_date": _fmt_date(buy_d) if len(buy_d) == 8 else buy_d,
                    "buy_limit_time": buy_t,
                }
                push("success", reservation=res_payload)
                return

            except SoldOutError:
                log("선점 경쟁 패배 — 즉시 재조회", "warn")
                retry_immediately = True
                break
            except SRTResponseError as exc:
                if is_srt_soldout_error(exc):
                    log("선점 경쟁 패배 — 즉시 재조회", "warn")
                    retry_immediately = True
                else:
                    log(f"예약 실패: {exc}", "warn")
                break
            except (NeedToLoginError, SRTNotLoggedInError):
                log("예약 중 세션 만료 → 재로그인", "warn")
                client = login_with_retry()
                if client is None:
                    push("error", msg="재로그인 실패")
                    return
                retry_immediately = True
                break
            except (KorailError, Exception) as exc:
                log(f"예약 실패: {exc}", "warn")
                break

        push("poll", attempt=attempt, trains=train_statuses)
        if retry_immediately:
            time.sleep(0.5)
            continue
        if not sleep_interruptible(interval):
            break

    push("done")

# ── 접속 URL 안내 ─────────────────────────────────────────────────────────────

def _find_free_port(preferred: int = 8188) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])

def _print_urls(port: int) -> str:
    url = f"http://127.0.0.1:{port}"
    print("=" * 52)
    print("  KTX / SRT 취소표 스나이퍼 — 로컬 웹 앱")
    print("=" * 52)
    print(f"  {url}")
    print()
    print("  외부 접속 차단: 127.0.0.1 로만 실행")
    print("  종료: 이 창을 닫거나 Ctrl+C")
    print("=" * 52)
    return url

# ── 서버 실행 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    PORT = _find_free_port(8188)
    URL = _print_urls(PORT)
    threading.Timer(0.8, lambda: webbrowser.open(URL)).start()
    try:
        if _serve:
            _serve(app, host="127.0.0.1", port=PORT, threads=8)
        else:
            app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\n종료합니다.", flush=True)
