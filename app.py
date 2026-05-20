#!/usr/bin/env python3
"""KTX 취소표 스나이퍼 — 웹 UI 서버"""
from __future__ import annotations

import json
import os
import queue
import random
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

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

# ── ktx_booking import ───────────────────────────────────────────────────────

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
    from flask import Flask, Response, jsonify, render_template, request, stream_with_context
except ImportError:
    sys.exit("Flask 없음: python3 -m pip install flask")

# ── Flask 앱 ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# ── 전역 세션 (단일 사용자 도구) ─────────────────────────────────────────────

_session: dict = {
    "client": None,          # PatchedKorail 로그인 인스턴스
    "thread": None,          # 스나이프 스레드
    "log_queue": None,       # 로그 큐
    "stop_flag": threading.Event(),
}


# ── 유틸 ─────────────────────────────────────────────────────────────────────

def _fmt_time(t: str) -> str:
    return f"{t[:2]}:{t[2:4]}" if len(t) >= 4 else t


def _fmt_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d


def _seat_label(info: dict) -> str:
    parts = []
    if info["has_general_seat"]:
        parts.append("일반석")
    if info["has_special_seat"]:
        parts.append("특실")
    if info["has_waiting_list"] and not (info["has_general_seat"] or info["has_special_seat"]):
        parts.append("대기열")
    return " / ".join(parts) if parts else "매진"


# ── 라우트 ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


def _search_all_trains(client, dep: str, arr: str, date: str,
                        from_time: str, to_time: str) -> list:
    """Korail API 1회 호출당 ~10개 제한을 우회하는 페이지네이션 조회.
    마지막 열차 출발시각 +1분으로 재조회를 반복해 to_time까지 수집."""
    collected: list = []
    seen_ids: set[str] = set()
    cursor = from_time

    for _ in range(20):          # 안전장치: 최대 20회 반복 (하루 최대 ~200편)
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

        new_in_range = False
        last_dep = cursor
        for t in batch:
            info   = normalize_train(t, 0)
            dep_t  = info["dep_time"]          # HHMMSS
            tid    = build_train_id(t)
            if dep_t > to_time:
                continue
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            collected.append(t)
            new_in_range = True
            if dep_t > last_dep:
                last_dep = dep_t

        # 새로 추가된 열차가 없거나 범위를 벗어났으면 종료
        if not new_in_range:
            break

        # 마지막 출발시각 +1분을 다음 cursor로
        h, m = int(last_dep[:2]), int(last_dep[2:4])
        m += 1
        if m >= 60:
            m, h = 0, h + 1
        cursor = f"{h:02d}{m:02d}00"
        if cursor > to_time:
            break

        time.sleep(0.8)          # Korail anti-bot 보호

    return collected


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json() or {}
    dep       = data.get("dep", "").strip()
    arr       = data.get("arr", "").strip()
    date      = data.get("date", "").strip()
    from_time = data.get("from_time", "000000")
    to_time   = data.get("to_time",   "235900")

    if not (dep and arr and date):
        return jsonify({"error": "출발역, 도착역, 날짜를 모두 입력하세요."}), 400

    # 로그인
    try:
        client = build_client()
    except SystemExit as exc:
        return jsonify({"error": str(exc)}), 401
    except Exception as exc:
        return jsonify({"error": f"로그인 실패: {exc}"}), 401

    _session["client"] = client

    # 페이지네이션 조회
    in_range = _search_all_trains(client, dep, arr, date, from_time, to_time)
    if not in_range:
        return jsonify({"error": f"{dep}→{arr} {_fmt_date(date)} {_fmt_time(from_time)}~{_fmt_time(to_time)} 구간에 KTX 열차가 없습니다."}), 404

    # 출발 시각 순 정렬
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

    return jsonify({"trains": result, "count": len(result)})


@app.route("/api/snipe/start", methods=["POST"])
def api_snipe_start():
    # 기존 스레드에 stop 신호 전송 후 최대 3초 대기
    old_flag: threading.Event | None = _session.get("stop_flag")
    if old_flag:
        old_flag.set()
    old_thread: threading.Thread | None = _session.get("thread")
    if old_thread and old_thread.is_alive():
        old_thread.join(timeout=3.0)
        # 3초 안에 안 죽으면 그냥 진행 (구 스레드는 자신의 stop_flag를 갖고 있어 결국 종료됨)

    data = request.get_json() or {}
    if not data.get("train_ids"):
        return jsonify({"error": "열차를 하나 이상 선택하세요."}), 400

    # 이번 실행 전용 새 객체 생성 (구 스레드의 참조와 완전히 분리)
    new_stop_flag = threading.Event()
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
            q: queue.Queue | None = _session.get("log_queue")
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
    sf: threading.Event | None = _session.get("stop_flag")
    if sf:
        sf.set()
    q: queue.Queue | None = _session.get("log_queue")
    if q:
        q.put({"type": "done"})
    return jsonify({"status": "stopped"})


@app.route("/api/status")
def api_status():
    running = bool(_session["thread"] and _session["thread"].is_alive())
    return jsonify({"running": running})


# ── 스나이프 스레드 ───────────────────────────────────────────────────────────

def _snipe_thread(
    data: dict,
    log_queue: queue.Queue,
    stop_flag: threading.Event,
    client,
) -> None:
    dep        = data["dep"]
    arr        = data["arr"]
    date       = data["date"]
    from_time  = data["from_time"]
    train_ids  = data["train_ids"]
    seat_option = data.get("seat_option", "general-first")
    try_waiting = bool(data.get("try_waiting", False))
    interval    = max(30.0, float(data.get("interval", 45)))

    def push(msg_type: str, **kwargs) -> None:
        log_queue.put({"type": msg_type, **kwargs})

    def log(msg: str, level: str = "info") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        push("log", ts=ts, msg=msg, level=level)

    def sleep_interruptible(base: float) -> bool:
        """슬립 도중 stop_flag 감지. 중단 요청 시 False 반환."""
        total = max(5.0, base + base * random.uniform(-0.1, 0.1))
        deadline = time.time() + total
        while time.time() < deadline:
            if stop_flag.is_set():
                return False
            time.sleep(0.3)
        return True

    def login_with_retry(max_attempts: int = 3):
        for attempt in range(1, max_attempts + 1):
            try:
                return build_client()
            except NeedToLoginError as exc:
                log(f"로그인 실패 ({attempt}/{max_attempts}): {exc}", "warn")
            except SystemExit as exc:
                log(f"로그인 불가: {exc}", "error")
                return None
            except Exception as exc:
                log(f"로그인 오류: {exc}", "warn")
            if attempt < max_attempts:
                time.sleep(5 * attempt)
        return None

    # 로그인
    if client is None:
        log("Korail 로그인 중...")
        client = login_with_retry()
    if client is None:
        push("error", msg="로그인 실패. .env의 KSKILL_KTX_ID / KSKILL_KTX_PASSWORD를 확인하세요.")
        return

    log(f"스나이퍼 시작: {dep} → {arr}  {_fmt_date(date)}")
    log(f"대상 {len(train_ids)}개 열차  |  간격 {interval:.0f}초  |  {seat_option}  |  대기 {'ON' if try_waiting else 'OFF'}")

    passengers = [AdultPassenger()]
    attempt = 0
    consec_errors = 0

    while not stop_flag.is_set():
        attempt += 1

        # ── 열차 조회 ─────────────────────────────────────────────────────────
        try:
            trains = client.search_train(
                dep, arr, date, from_time,
                train_type=TRAIN_TYPE_MAP["ktx"],
                passengers=passengers,
                include_no_seats=True,
                include_waiting_list=True,
            )
            consec_errors = 0

        except NoResultsError:
            log(f"#{attempt:05d}  조회 결과 없음 (날짜가 지났거나 노선 없음)", "warn")
            consec_errors += 1
            backoff = min(300.0, 30.0 * (2 ** max(0, consec_errors - 3))) if consec_errors >= 3 else interval
            if not sleep_interruptible(backoff):
                break
            continue

        except NeedToLoginError:
            log(f"#{attempt:05d}  세션 만료 → 재로그인", "warn")
            client = login_with_retry()
            if client is None:
                push("error", msg="재로그인 실패")
                return
            consec_errors += 1
            if not sleep_interruptible(interval):
                break
            continue

        except KorailError as exc:
            log(f"#{attempt:05d}  KorailError: {exc}", "warn")
            consec_errors += 1
            if not sleep_interruptible(interval):
                break
            continue

        except Exception as exc:
            log(f"#{attempt:05d}  예외: {exc}", "warn")
            consec_errors += 1
            if not sleep_interruptible(interval):
                break
            continue

        # ── 각 대상 열차 확인 ─────────────────────────────────────────────────
        train_statuses = []
        retry_immediately = False

        for train_id in train_ids:
            target = find_train_by_id(trains, train_id)
            if target is None:
                continue

            info     = normalize_train(target, 0)
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

            try:
                reservation = client.reserve(
                    target,
                    passengers=passengers,
                    option=RESERVE_OPTION_MAP[seat_option],
                    try_waiting=try_waiting,
                )
                res   = normalize_reservation(reservation)
                dep_t = _fmt_time(res["dep_time"]) if len(res.get("dep_time", "")) >= 4 else res.get("dep_time", "?")
                arr_t = _fmt_time(res["arr_time"]) if len(res.get("arr_time", "")) >= 4 else res.get("arr_time", "?")
                buy_d = res.get("buy_limit_date", "?")
                buy_t_raw = res.get("buy_limit_time", "")
                buy_t = _fmt_time(buy_t_raw) if len(buy_t_raw) >= 4 else buy_t_raw

                push("success", reservation={
                    "reservation_id": res["reservation_id"],
                    "train_no":       res["train_no"],
                    "train_type":     res.get("train_type", "KTX"),
                    "dep_name":       res["dep_name"],
                    "arr_name":       res["arr_name"],
                    "dep_time":       dep_t,
                    "arr_time":       arr_t,
                    "price":          res.get("price", "?"),
                    "buy_limit_date": _fmt_date(buy_d) if len(buy_d) == 8 else buy_d,
                    "buy_limit_time": buy_t,
                })
                return  # ✅ 성공 종료

            except SoldOutError:
                log("선점 경쟁 패배 — 즉시 재조회", "warn")
                retry_immediately = True
                break

            except NeedToLoginError:
                log("예약 중 세션 만료 → 재로그인", "warn")
                client = login_with_retry()
                if client is None:
                    push("error", msg="재로그인 실패")
                    return
                retry_immediately = True
                break

            except KorailError as exc:
                log(f"예약 실패: {exc}", "warn")
                break

        # 폴링 상태 브로드캐스트
        push("poll", attempt=attempt, trains=train_statuses)

        if retry_immediately:
            time.sleep(0.5)
            continue

        if not sleep_interruptible(interval):
            break

    push("done")


# ── 서버 실행 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import socket

    host = "127.0.0.1"
    port = 8080
    for p in range(8080, 8090):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, p)) != 0:
                port = p
                break

    url = f"http://{host}:{port}"
    print(f"서버 시작: {url}")
    print("종료하려면 Ctrl+C")

    def _open_browser():
        time.sleep(0.8)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host=host, port=port, debug=False, threaded=True)
