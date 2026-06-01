"""SRT 예매 유틸리티 — ktx_booking.py 와 동일한 인터페이스 제공."""
from __future__ import annotations

try:
    from SRT import SRT, Adult, Child, Senior, SeatType
    from SRT.errors import (
        SRTError, SRTLoginError, SRTResponseError,
        SRTNotLoggedInError, SRTNetFunnelError,
    )
except ImportError as exc:
    raise ImportError(f"SRTrain 없음. python3 -m pip install SRTrain 후 다시 시도하세요.\n오류: {exc}") from exc

SEAT_OPTION_MAP: dict[str, SeatType] = {
    "general-first": SeatType.GENERAL_FIRST,
    "general-only":  SeatType.GENERAL_ONLY,
    "special-first": SeatType.SPECIAL_FIRST,
    "special-only":  SeatType.SPECIAL_ONLY,
}

# SoldOut 판단용 키워드 (SRTResponseError.msg 에서 검색)
_SOLDOUT_KEYWORDS = ("없습니다", "매진", "좌석이 없", "잔여석이 없", "예약 불가")


def build_srt_client(srt_id: str | None = None, srt_pw: str | None = None) -> SRT:
    srt_id = (srt_id or "").strip()
    srt_pw = (srt_pw or "").strip()
    if not srt_id or not srt_pw:
        raise SystemExit("SRT 로그인이 필요합니다. 웹 화면에서 SRT ID와 비밀번호를 입력하세요.")
    return SRT(srt_id, srt_pw)


def build_srt_train_id(train) -> str:
    """열차 고유 ID (dep_date:dep_time:train_number)."""
    return f"srt:{train.dep_date}:{train.dep_time}:{train.train_number}"


def find_srt_train_by_id(trains: list, train_id: str):
    for t in trains:
        if build_srt_train_id(t) == train_id:
            return t
    return None


def normalize_srt_train(train, idx: int) -> dict:
    return {
        "idx":              idx,
        "train_no":         train.train_number,
        "dep_time":         train.dep_time,          # hhmmss
        "arr_time":         train.arr_time,          # hhmmss
        "dep_name":         train.dep_station_name,
        "arr_name":         train.arr_station_name,
        "has_general_seat": train.general_seat_available(),
        "has_special_seat": train.special_seat_available(),
        "has_waiting_list": train.reserve_standby_available(),
    }


def normalize_srt_reservation(res) -> dict:
    return {
        "reservation_id": res.reservation_number,
        "train_no":       res.train_number,
        "train_type":     "SRT",
        "dep_name":       res.dep_station_name,
        "arr_name":       res.arr_station_name,
        "dep_time":       res.dep_time,      # hhmmss
        "arr_time":       res.arr_time,      # hhmmss
        "price":          str(res.total_cost),
        "buy_limit_date": res.payment_date,  # yyyyMMdd
        "buy_limit_time": res.payment_time,  # hhmmss
    }


def is_srt_soldout_error(exc: SRTResponseError) -> bool:
    msg = str(getattr(exc, "msg", exc)).lower()
    return any(kw in msg for kw in _SOLDOUT_KEYWORDS)
