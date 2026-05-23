#!/usr/bin/env python3
"""Discord slash-command wrapper for KTX/SRT train reservation.

Use `/예매` to open a Discord modal and reserve matching trains.
This module intentionally keeps Discord I/O thin and
delegates Korail/SRT behavior to app_t.py.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import types
from dataclasses import dataclass
from datetime import date as date_cls, datetime
from pathlib import Path
from typing import Callable, Iterable


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

sys.path.insert(0, str(Path(__file__).parent))

try:
    import app_t  # type: ignore[import]
except ImportError as exc:
    raise SystemExit(f"app_t.py를 불러올 수 없습니다: {exc}") from exc


class FormError(ValueError):
    """User-facing form validation error."""


@dataclass(frozen=True)
class BookingRequest:
    train: str
    dep: str
    arr: str
    date: str
    from_time: str
    to_time: str
    seat_option: str
    try_waiting: bool
    interval: float
    adults: int
    children: int
    seniors: int
    toddlers: int
    targets: str


@dataclass
class ReservationPlan:
    client: object
    trains: list
    display_trains: list[dict]
    train_ids: list[str]


FORM_GUIDE = """`/예매` 입력 안내
각 줄은 `항목: 값` 형식으로 적습니다. 항목 이름은 한글/일부 영문 별칭을 쓸 수 있습니다.

```text
열차: KTX
출발: 서울
도착: 부산
날짜: 20260601
시작: 09:00
종료: 13:00
좌석: 일반우선
```

필수 항목
- `출발`: 출발역 이름. 예: `서울`, `수서`, `동대구`
- `도착`: 도착역 이름. 예: `부산`, `광주송정`, `대전`
- `날짜`: `YYYYMMDD`, `YYYY-MM-DD`, `M/D`, `6월1일` 형식

선택 항목
- `열차`: `KTX` 또는 `SRT`. 생략 시 `KTX`
- `시작`/`종료`: `0900`, `09:00`, `9시`, `9시30분` 형식. 생략 시 `00:00~23:59`
- `좌석`: `일반우선`, `일반만`, `특실우선`, `특실만`. 생략 시 `일반우선`
- 승객은 성인만 지원

별칭
- `출발역`, `dep`, `도착역`, `arr`, `일자`, `시작시간`, `종료시간`, `성인`, `인원` 사용 가능

사용 예시: `/예매`
"""

DEFAULT_BOOKING_VALUES = {
    "train": "KTX",
    "from_time": "0000",
    "to_time": "2359",
    "seat_option": "일반우선",
    "try_waiting": "아니오",
    "targets": "all",
    "interval": "45",
    "adults": "1",
    "children": "0",
    "seniors": "0",
    "toddlers": "0",
}

KEY_ALIASES = {
    "train": "train",
    "열차": "train",
    "종류": "train",
    "출발": "dep",
    "출발역": "dep",
    "dep": "dep",
    "도착": "arr",
    "도착역": "arr",
    "arr": "arr",
    "날짜": "date",
    "일자": "date",
    "date": "date",
    "시작": "from_time",
    "시작시각": "from_time",
    "시작시간": "from_time",
    "from": "from_time",
    "종료": "to_time",
    "종료시각": "to_time",
    "종료시간": "to_time",
    "to": "to_time",
    "좌석": "seat_option",
    "seat": "seat_option",
    "성인": "adults",
    "인원": "adults",
    "adult": "adults",
    "adults": "adults",
}

SEAT_ALIASES = {
    "일반우선": "general-first",
    "일반석우선": "general-first",
    "general-first": "general-first",
    "일반만": "general-only",
    "일반석만": "general-only",
    "general-only": "general-only",
    "특실우선": "special-first",
    "특실": "special-first",
    "special-first": "special-first",
    "특실만": "special-only",
    "special-only": "special-only",
}

TRUE_VALUES = {"y", "yes", "true", "1", "예", "네", "응", "시도", "on"}
FALSE_VALUES = {"n", "no", "false", "0", "아니오", "아니요", "아님", "안함", "off"}


def _clean_key(raw: str) -> str:
    return re.sub(r"[\s_/-]+", "", raw.strip().lower())


def _parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("/예매"):
            line = line.removeprefix("/예매").strip()
            if line.startswith("+"):
                line = line[1:].strip()
        if not line:
            continue
        match = re.match(r"([^:=：]+)\s*[:=：]\s*(.+)$", line)
        if not match:
            raise FormError(f"양식 줄을 해석할 수 없습니다: {raw_line!r}")
        key = KEY_ALIASES.get(_clean_key(match.group(1)))
        if not key:
            raise FormError(f"지원하지 않는 항목입니다: {match.group(1).strip()}")
        values[key] = match.group(2).strip()
    return values


def _split_time_range(raw: str) -> tuple[str, str]:
    text = raw.strip()
    if not text:
        return DEFAULT_BOOKING_VALUES["from_time"], DEFAULT_BOOKING_VALUES["to_time"]
    parts = [part.strip() for part in re.split(r"\s*(?:~|–|-|부터|에서)\s*", text, maxsplit=1) if part.strip()]
    if len(parts) == 1:
        return parts[0], DEFAULT_BOOKING_VALUES["to_time"]
    return parts[0], parts[1]


def build_form_text(values: dict[str, str]) -> str:
    from_time, to_time = _split_time_range(values.get("time_range", ""))
    merged = DEFAULT_BOOKING_VALUES | values | {"from_time": from_time, "to_time": to_time}
    return "\n".join(
        [
            f"열차: {merged['train']}",
            f"출발: {merged['dep']}",
            f"도착: {merged['arr']}",
            f"날짜: {merged['date']}",
            f"시작: {merged['from_time']}",
            f"종료: {merged['to_time']}",
            f"좌석: {merged['seat_option']}",
            f"성인: {merged['adults']}",
        ]
    )


def _parse_positive_int(values: dict[str, str], key: str, default: int) -> int:
    raw = values.get(key, str(default)).strip()
    if not re.fullmatch(r"\d+", raw):
        raise FormError(f"{key} 값은 0 이상의 정수여야 합니다: {raw!r}")
    return int(raw)


def _parse_bool(raw: str) -> bool:
    cleaned = _clean_key(raw)
    if cleaned in TRUE_VALUES:
        return True
    if cleaned in FALSE_VALUES:
        return False
    raise FormError(f"대기 값은 예/아니오로 입력하세요: {raw!r}")


def _normalize_seat(raw: str) -> str:
    key = _clean_key(raw)
    seat = SEAT_ALIASES.get(key)
    if not seat:
        raise FormError("좌석은 일반우선/일반만/특실우선/특실만 중 하나로 입력하세요.")
    return seat


def parse_booking_form(text: str, *, today: date_cls | None = None) -> BookingRequest:
    values = _parse_key_values(text)
    missing = [label for label, key in [("출발", "dep"), ("도착", "arr"), ("날짜", "date")] if not values.get(key)]
    if missing:
        raise FormError(f"필수값 누락: {', '.join(missing)}")

    train = values.get("train", "KTX").strip().lower()
    if train not in {"ktx", "srt"}:
        raise FormError("열차는 KTX 또는 SRT만 지원합니다.")

    parsed_date = app_t._parse_date(values["date"])
    if not parsed_date:
        raise FormError("날짜를 인식할 수 없습니다. 예: 20260601, 2026-06-01, 6/1")
    try:
        travel_date = datetime.strptime(parsed_date, "%Y%m%d").date()
    except ValueError as exc:
        raise FormError("날짜 형식이 올바르지 않습니다.") from exc
    baseline = today or date_cls.today()
    if travel_date < baseline:
        raise FormError(f"오늘 이전 날짜는 검색하지 않습니다: {app_t._fmt_date(parsed_date)}")

    from_time = app_t._parse_time(values.get("from_time", "0000"))
    to_time = app_t._parse_time(values.get("to_time", "2359"))
    if not from_time or not to_time:
        raise FormError("시각을 인식할 수 없습니다. 예: 0900, 09:00, 9시")
    if from_time > to_time:
        from_time, to_time = to_time, from_time

    try:
        interval = float(values.get("interval", "45"))
    except ValueError as exc:
        raise FormError("간격은 숫자로 입력하세요.") from exc
    if interval < 30:
        interval = 45.0

    adults = _parse_positive_int(values, "adults", 1)
    children = _parse_positive_int(values, "children", 0)
    seniors = _parse_positive_int(values, "seniors", 0)
    toddlers = _parse_positive_int(values, "toddlers", 0)
    if adults + children + seniors + toddlers < 1:
        raise FormError("승객은 최소 1명 이상이어야 합니다.")

    return BookingRequest(
        train=train,
        dep=values["dep"].strip(),
        arr=values["arr"].strip(),
        date=parsed_date,
        from_time=from_time,
        to_time=to_time,
        seat_option=_normalize_seat(values.get("seat_option", "일반우선")),
        try_waiting=_parse_bool(values.get("try_waiting", "아니오")),
        interval=interval,
        adults=adults,
        children=children,
        seniors=seniors,
        toddlers=toddlers,
        targets=values.get("targets", "all").strip() or "all",
    )


def _seat_label(info: dict) -> str:
    parts = []
    if info["has_general_seat"]:
        parts.append("일반석")
    if info["has_special_seat"]:
        parts.append("특실")
    return "/".join(parts) if parts else "매진"


def _search_raw_trains(
    request: BookingRequest,
) -> tuple[object, list, Callable[[object, int], dict], Callable[[object], str]]:
    if request.train == "srt":
        if not getattr(app_t, "_SRT_AVAILABLE", False):
            raise FormError("SRT 기능을 사용하려면 SRTrain 패키지를 설치해야 합니다.")
        client = app_t.build_srt_client()
        trains = app_t._search_all_srt_trains(
            client,
            request.dep,
            request.arr,
            request.date,
            request.from_time,
            request.to_time,
        )
        trains.sort(key=lambda train: train.dep_time)
        return client, trains, app_t.normalize_srt_train, app_t.build_srt_train_id

    client = app_t._login_with_retry()
    trains = app_t._search_all_trains(
        client,
        request.dep,
        request.arr,
        request.date,
        request.from_time,
        request.to_time,
    )
    trains.sort(key=lambda train: app_t.normalize_train(train, 0)["dep_time"])
    return client, trains, app_t.normalize_train, app_t.build_train_id


def build_reservation_plan(request: BookingRequest) -> ReservationPlan:
    client, trains, norm_fn, id_fn = _search_raw_trains(request)
    display_trains = [norm_fn(train, idx) for idx, train in enumerate(trains, 1)]
    if not trains:
        return ReservationPlan(client=client, trains=[], display_trains=[], train_ids=[])

    selected_idx = app_t._parse_selection(request.targets, len(trains))
    if not selected_idx:
        raise FormError("대상은 all, 1, 1,3, 1-3 형식으로 입력하세요.")

    train_ids = [id_fn(trains[i - 1]) for i in selected_idx]
    return ReservationPlan(
        client=client,
        trains=trains,
        display_trains=display_trains,
        train_ids=train_ids,
    )


def search_trains(request: BookingRequest) -> list[dict]:
    return build_reservation_plan(request).display_trains


def format_train_table(
    request: BookingRequest,
    trains: Iterable[dict],
    *,
    limit: int = 15,
    footer: str | None = "표 조회만 수행했습니다. 실제 예약/결제는 아직 진행하지 않았습니다.",
) -> str:
    rows = list(trains)
    title = (
        f"{request.train.upper()} {request.dep} -> {request.arr} "
        f"{app_t._fmt_date(request.date)} {app_t._fmt_time(request.from_time)}~{app_t._fmt_time(request.to_time)}"
    )
    if not rows:
        return f"검색 결과가 없습니다.\n`{title}`"

    lines = [title, "", "```text", " #  열차번호  출발   도착   좌석"]
    for info in rows[:limit]:
        lines.append(
            f"{int(info['index']):>2}  {str(info['train_no']):>8}  "
            f"{app_t._fmt_time(str(info['dep_time'])):>5}  "
            f"{app_t._fmt_time(str(info['arr_time'])):>5}  "
            f"{_seat_label(info)}"
        )
    lines.append("```")
    if len(rows) > limit:
        lines.append(f"외 {len(rows) - limit}개 결과는 시간 범위를 좁혀 다시 조회하세요.")
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def _booking_args(request: BookingRequest):
    return types.SimpleNamespace(
        dep=request.dep,
        arr=request.arr,
        date=request.date,
        time=request.from_time,
        seat_option=request.seat_option,
        train_type=request.train,
        try_waiting=request.try_waiting,
        interval=request.interval,
        adults=request.adults,
        children=request.children,
        seniors=request.seniors,
        toddlers=request.toddlers,
        is_srt=request.train == "srt",
    )


def _format_success(res: dict) -> str:
    buy_d = res.get("buy_limit_date", "?")
    buy_t_raw = res.get("buy_limit_time", "")
    buy_t = app_t._fmt_time(buy_t_raw) if len(buy_t_raw) >= 4 else (buy_t_raw or "?")
    buy_date = app_t._fmt_date(buy_d) if len(buy_d) == 8 else buy_d
    dep_t = app_t._fmt_time(res.get("dep_time", ""))
    arr_t = app_t._fmt_time(res.get("arr_time", ""))
    return (
        "예약 성공\n"
        f"예약번호: {res.get('reservation_id', '?')}\n"
        f"열차: {res.get('train_type', 'KTX')} {res.get('train_no', '?')}\n"
        f"구간: {res.get('dep_name', '?')} {dep_t} -> {res.get('arr_name', '?')} {arr_t}\n"
        f"운임: {res.get('price', '?')}원\n"
        f"결제기한: {buy_date} {buy_t}\n"
        "Korail/SRT 앱 또는 웹에서 결제기한 내 결제하세요."
    )


def run_reservation(request: BookingRequest, plan: ReservationPlan) -> dict | None:
    if not plan.train_ids:
        return None

    result: dict | None = None
    original_print_success = app_t._print_success

    def capture_success(res: dict) -> None:
        nonlocal result
        result = dict(res)
        original_print_success(res)

    app_t._print_success = capture_success
    try:
        app_t.snipe(_booking_args(request), plan.train_ids, plan.client)
    finally:
        app_t._print_success = original_print_success
    return result


def _parse_id_allowlist(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        if not part.isdigit():
            raise SystemExit(f"allowlist 값은 Discord snowflake 숫자여야 합니다: {part!r}")
        ids.add(int(part))
    return ids


def _require_discord():
    try:
        # discord.py imports voice modules eagerly, and those import audioop.
        # This bot never uses voice, so avoid native audioop-lts architecture
        # conflicts on universal2 macOS Python installations.
        class _VoiceClientStub:
            warn_nacl = False
            warn_dave = False

        class _VoiceProtocolStub:
            pass

        class _AudioPlayerStub:
            pass

        class _AudioSourceStub:
            pass

        if "discord.player" not in sys.modules:
            player_stub = types.ModuleType("discord.player")
            player_stub.AudioPlayer = _AudioPlayerStub
            player_stub.AudioSource = _AudioSourceStub
            sys.modules["discord.player"] = player_stub
        if "discord.voice_client" not in sys.modules:
            voice_stub = types.ModuleType("discord.voice_client")
            voice_stub.VoiceClient = _VoiceClientStub
            voice_stub.VoiceProtocol = _VoiceProtocolStub
            sys.modules["discord.voice_client"] = voice_stub
        import discord  # type: ignore[import]
        from discord import app_commands  # type: ignore[import]
    except Exception as exc:
        raise SystemExit(
            "discord.py import 실패. 현재 Python 아키텍처/패키지 조합을 확인하세요.\n"
            "권장: 같은 아키텍처의 Python에서 `python3 -m pip install -U discord.py PyNaCl` 재설치\n"
            f"원인: {exc}"
        ) from exc
    return discord, app_commands


def _configure_certifi_ca() -> None:
    try:
        import certifi  # type: ignore[import]
    except Exception as exc:
        raise SystemExit(
            "Discord HTTPS 연결에 필요한 certifi import 실패. "
            "`python3 -m pip install -U certifi` 후 다시 실행하세요.\n"
            f"원인: {exc}"
        ) from exc

    cafile = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", cafile)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", cafile)


def run_bot() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN 환경변수가 필요합니다.")

    _configure_certifi_ca()
    discord, app_commands = _require_discord()
    allowed_channels = _parse_id_allowlist(os.environ.get("DISCORD_ALLOWED_CHANNEL_IDS", ""))
    allowed_users = _parse_id_allowlist(os.environ.get("DISCORD_ALLOWED_USER_IDS", ""))
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if guild_id and not guild_id.isdigit():
        raise SystemExit("DISCORD_GUILD_ID는 Discord 서버 snowflake 숫자여야 합니다.")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    synced_commands = False
    reservation_task: asyncio.Task | None = None
    reservation_lock = asyncio.Lock()

    def is_allowed(interaction) -> bool:
        if allowed_channels and interaction.channel_id not in allowed_channels:
            return False
        if allowed_users and interaction.user.id not in allowed_users:
            return False
        return True

    async def send_channel_message(interaction, message: str) -> None:
        channel = interaction.channel
        if channel and hasattr(channel, "send"):
            await channel.send(message[:1900])
        else:
            await interaction.followup.send(message[:1900], ephemeral=True)

    async def reservation_worker(interaction, request: BookingRequest, plan: ReservationPlan) -> None:
        try:
            async with reservation_lock:
                try:
                    result = await asyncio.to_thread(run_reservation, request, plan)
                    if result:
                        message = f"<@{interaction.user.id}> {_format_success(result)}"
                    else:
                        message = f"<@{interaction.user.id}> 예약 대상 열차가 없습니다."
                except Exception as exc:
                    message = f"<@{interaction.user.id}> 예약 루프 실패: {exc}"
                await send_channel_message(interaction, message)
        except Exception as exc:
            print(f"Discord reservation worker failed: {exc}", flush=True)

    def render_draft(values: dict[str, str]) -> str:
        form_text = build_form_text(values)
        try:
            request = parse_booking_form(form_text)
            warning = "\n경고: 2명 이상은 동시에 빈 좌석이 나와야 해서 예약까지 오래 걸릴 수 있습니다." if request.adults >= 2 else ""
            return (
                "입력값을 확인한 뒤 예약 시작을 누르세요.\n"
                "```text\n"
                f"열차: {request.train.upper()}\n"
                f"구간: {request.dep} -> {request.arr}\n"
                f"날짜: {app_t._fmt_date(request.date)}\n"
                f"시간: {app_t._fmt_time(request.from_time)} ~ {app_t._fmt_time(request.to_time)}\n"
                f"좌석: {request.seat_option}\n"
                f"승객: 성인 {request.adults}명\n"
                f"```{warning}"
            )
        except FormError as exc:
            return f"입력값 오류: {exc}\n처음부터 다시 입력해 주세요."

    class BookingModal(discord.ui.Modal, title="KTX/SRT 예매"):
        train = discord.ui.TextInput(
            label="열차",
            placeholder="KTX 또는 SRT",
            default="KTX",
            max_length=10,
            required=True,
        )
        dep = discord.ui.TextInput(
            label="출발역",
            placeholder="예: 서울",
            max_length=20,
            required=True,
        )
        arr = discord.ui.TextInput(
            label="도착역",
            placeholder="예: 부산",
            max_length=20,
            required=True,
        )
        date = discord.ui.TextInput(
            label="날짜",
            placeholder="예: 20260601, 2026-06-01, 6/1",
            max_length=20,
            required=True,
        )
        time_range = discord.ui.TextInput(
            label="시간 범위",
            placeholder="예: 0900~1300, 9시~13시",
            default="0000~2359",
            max_length=30,
            required=True,
        )

        def __init__(self, user_id: int):
            super().__init__(timeout=300)
            self.user_id = user_id

        async def on_submit(self, interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("이 입력창은 요청한 사용자만 사용할 수 있습니다.", ephemeral=True)
                return
            values = {
                "train": str(self.train.value),
                "dep": str(self.dep.value),
                "arr": str(self.arr.value),
                "date": str(self.date.value),
                "time_range": str(self.time_range.value),
            }
            await interaction.response.send_message(
                render_draft(values),
                view=BookingOptionsView(self.user_id, values),
                ephemeral=True,
            )

    class SeatSelect(discord.ui.Select):
        def __init__(self):
            super().__init__(
                placeholder="좌석 선호",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(label="일반석 우선", value="일반우선"),
                    discord.SelectOption(label="일반석만", value="일반만"),
                    discord.SelectOption(label="특실 우선", value="특실우선"),
                    discord.SelectOption(label="특실만", value="특실만"),
                ],
            )

        async def callback(self, interaction) -> None:
            view = self.view
            if not isinstance(view, BookingOptionsView):
                return
            if interaction.user.id != view.user_id:
                await interaction.response.send_message("이 설정은 요청한 사용자만 바꿀 수 있습니다.", ephemeral=True)
                return
            view.values["seat_option"] = self.values[0]
            await interaction.response.edit_message(content=render_draft(view.values), view=view)

    class AdultCountSelect(discord.ui.Select):
        def __init__(self):
            super().__init__(
                placeholder="성인 인원",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(label=f"성인 {count}명", value=str(count))
                    for count in range(1, 5)
                ],
            )

        async def callback(self, interaction) -> None:
            view = self.view
            if not isinstance(view, BookingOptionsView):
                return
            if interaction.user.id != view.user_id:
                await interaction.response.send_message("이 설정은 요청한 사용자만 바꿀 수 있습니다.", ephemeral=True)
                return
            view.values["adults"] = self.values[0]
            await interaction.response.edit_message(content=render_draft(view.values), view=view)

    class BookingOptionsView(discord.ui.View):
        def __init__(self, user_id: int, values: dict[str, str]):
            super().__init__(timeout=900)
            self.user_id = user_id
            self.values = DEFAULT_BOOKING_VALUES | values
            self.add_item(SeatSelect())
            self.add_item(AdultCountSelect())

        def disable_all(self) -> None:
            for item in self.children:
                item.disabled = True

        @discord.ui.button(label="예약 시작", style=discord.ButtonStyle.primary)
        async def start(self, interaction, button) -> None:
            nonlocal reservation_task
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("이 예약은 요청한 사용자만 시작할 수 있습니다.", ephemeral=True)
                return
            if reservation_task and not reservation_task.done():
                await interaction.response.send_message(
                    "이미 예약 스나이퍼가 실행 중입니다. 완료 후 다시 시도하세요.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                request = parse_booking_form(build_form_text(self.values))
                plan = await asyncio.to_thread(build_reservation_plan, request)
                if not plan.display_trains:
                    message = format_train_table(request, plan.display_trains)
                else:
                    footer = (
                        f"조회된 {len(plan.train_ids)}개 열차 예약 스나이퍼를 시작했습니다. "
                        f"좌석 {request.seat_option}, 성인 {request.adults}명"
                    )
                    if request.adults >= 2:
                        footer += "\n경고: 2명 이상은 동시에 빈 좌석이 나와야 해서 예약까지 오래 걸릴 수 있습니다."
                    message = format_train_table(request, plan.display_trains, footer=footer)
                    reservation_task = asyncio.create_task(reservation_worker(interaction, request, plan))
                    self.disable_all()
                    if interaction.message:
                        try:
                            await interaction.message.edit(view=self)
                        except Exception as exc:
                            print(f"Discord view disable failed: {exc}", flush=True)
            except FormError as exc:
                message = f"입력값 오류: {exc}"
            except Exception as exc:
                message = f"예약 준비 실패: {exc}"
            await interaction.followup.send(message[:1900], ephemeral=True)

    @tree.command(name="예매", description="KTX/SRT 열차표를 모달로 입력해 예약합니다")
    async def reserve(interaction):
        if not is_allowed(interaction):
            await interaction.response.send_message("이 채널 또는 사용자는 봇 사용 권한이 없습니다.", ephemeral=True)
            return
        if reservation_task and not reservation_task.done():
            await interaction.response.send_message(
                "이미 예약 스나이퍼가 실행 중입니다. 완료 후 다시 시도하세요.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(BookingModal(interaction.user.id))

    @client.event
    async def on_ready():
        nonlocal synced_commands
        if not synced_commands:
            try:
                if guild_id:
                    guild = discord.Object(id=int(guild_id))
                    tree.copy_global_to(guild=guild)
                    await asyncio.wait_for(tree.sync(guild=guild), timeout=30)
                else:
                    await asyncio.wait_for(tree.sync(), timeout=30)
                synced_commands = True
                print("Discord slash commands synced", flush=True)
            except asyncio.TimeoutError:
                print("Discord slash command sync timed out; bot remains connected", flush=True)
        print(f"Discord bot ready: {client.user}", flush=True)

    client.run(token)


def main() -> None:
    run_bot()


def _ensure_tmux() -> None:
    """Run the Discord bot in a persistent tmux session by default."""
    if os.environ.get("TMUX"):
        return

    if os.environ.get("APP_DISCORD_NO_TMUX") == "1":
        return

    if not shutil.which("tmux"):
        raise SystemExit("tmux가 없습니다. `brew install tmux` 후 다시 실행하세요.")

    session = os.environ.get("APP_DISCORD_TMUX_SESSION", "ticket-discord")
    script = str(Path(__file__).resolve())
    workdir = str(Path(__file__).resolve().parent)
    log_path = "logs/app_discord.log"

    exists = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
        check=False,
    ).returncode == 0

    if exists:
        print(f"기존 tmux 세션 '{session}'에 연결합니다. detach: Ctrl+B, D")
        os.execvp("tmux", ["tmux", "attach-session", "-t", session])

    Path(workdir, "logs").mkdir(exist_ok=True)
    command = f"cd {shlex_quote(workdir)} && {shlex_quote(sys.executable)} {shlex_quote(script)} 2>&1 | tee -a {shlex_quote(log_path)}"
    print(f"tmux 세션 '{session}' 생성 후 실행합니다. detach: Ctrl+B, D")
    os.execvp("tmux", ["tmux", "new-session", "-s", session, "zsh", "-lc", command])


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    _ensure_tmux()
    main()
