from __future__ import annotations

import ticket_web


class DummyTrain:
    pass


class DummyClient:
    def __init__(self) -> None:
        self.reserve_calls = []

    def reserve(self, train, passengers, option, try_waiting):
        self.reserve_calls.append({
            "train": train,
            "passengers": passengers,
            "option": option,
            "try_waiting": try_waiting,
        })
        return DummyReservation()


class DummyReservation:
    pass


def _reset_session() -> None:
    ticket_web._session["client"] = None
    ticket_web._session["credentials"] = {"ktx": None, "srt": None}
    ticket_web._session["thread"] = None
    ticket_web._session["log_queue"] = None
    ticket_web._session["search"] = None
    ticket_web._session["snipe"] = None
    ticket_web._session["logs"] = []
    ticket_web._session["last_poll"] = None
    ticket_web._session["success"] = None


def test_search_requires_web_login() -> None:
    _reset_session()
    client = ticket_web.app.test_client()

    resp = client.post("/api/search", json={
        "dep": "서울",
        "arr": "부산",
        "date": "20260602",
        "from_time": "090000",
        "to_time": "100000",
        "train_type": "ktx",
    })

    assert resp.status_code == 400
    assert "로그인" in resp.get_json()["error"]


def test_search_uses_payload_login_and_state_does_not_leak_password(monkeypatch) -> None:
    _reset_session()
    captured = {}

    def fake_build_client(user_id: str, password: str):
        captured["user_id"] = user_id
        captured["password"] = password
        return DummyClient()

    def fake_search_all(client, dep, arr, date, from_time, to_time):
        captured["range"] = (dep, arr, date, from_time, to_time)
        return [DummyTrain()]

    def fake_normalize_train(train, index):
        return {
            "train_no": "101",
            "dep_time": "090000",
            "arr_time": "120000",
            "has_general_seat": False,
            "has_special_seat": False,
            "has_waiting_list": False,
        }

    monkeypatch.setattr(ticket_web, "build_client", fake_build_client)
    monkeypatch.setattr(ticket_web, "_search_all_trains", fake_search_all)
    monkeypatch.setattr(ticket_web, "normalize_train", fake_normalize_train)
    monkeypatch.setattr(ticket_web, "build_train_id", lambda train: "ktx:test")

    client = ticket_web.app.test_client()
    resp = client.post("/api/search", json={
        "dep": "서울",
        "arr": "부산",
        "date": "20260602",
        "from_time": "090000",
        "to_time": "100000",
        "train_type": "ktx",
        "login": {"user_id": "member-1", "password": "secret-pw"},
    })

    assert resp.status_code == 200
    assert captured == {
        "user_id": "member-1",
        "password": "secret-pw",
        "range": ("서울", "부산", "20260602", "090000", "100000"),
    }
    state = client.get("/api/state").get_json()
    assert "secret-pw" not in str(state)


def test_snipe_start_rejects_without_prior_login() -> None:
    _reset_session()
    client = ticket_web.app.test_client()

    resp = client.post("/api/snipe/start", json={
        "dep": "서울",
        "arr": "부산",
        "date": "20260602",
        "from_time": "090000",
        "to_time": "100000",
        "train_type": "ktx",
        "train_ids": ["ktx:test"],
    })

    assert resp.status_code == 400
    assert "로그인" in resp.get_json()["error"]


def test_snipe_success_stream_and_state_do_not_leak_password(monkeypatch) -> None:
    _reset_session()
    train = DummyTrain()
    client_obj = DummyClient()

    monkeypatch.setattr(ticket_web, "build_client", lambda user_id, password: client_obj)
    monkeypatch.setattr(ticket_web, "AdultPassenger", lambda: object())
    monkeypatch.setitem(ticket_web.TRAIN_TYPE_MAP, "ktx", object())
    monkeypatch.setitem(ticket_web.RESERVE_OPTION_MAP, "general-first", object())
    monkeypatch.setattr(ticket_web, "_search_all_trains", lambda *args: [train])
    monkeypatch.setattr(ticket_web, "build_train_id", lambda item: "ktx:test")
    monkeypatch.setattr(ticket_web, "find_train_by_id", lambda trains, train_id: train if train_id == "ktx:test" else None)
    monkeypatch.setattr(ticket_web, "normalize_train", lambda item, index: {
        "train_no": "101",
        "dep_time": "090000",
        "arr_time": "120000",
        "has_general_seat": True,
        "has_special_seat": False,
        "has_waiting_list": False,
    })
    monkeypatch.setattr(ticket_web, "normalize_reservation", lambda reservation: {
        "reservation_id": "R123",
        "train_no": "101",
        "train_type": "KTX",
        "dep_name": "서울",
        "arr_name": "부산",
        "dep_time": "090000",
        "arr_time": "120000",
        "price": "59800",
        "buy_limit_date": "20260602",
        "buy_limit_time": "130000",
    })

    client = ticket_web.app.test_client()
    search_resp = client.post("/api/search", json={
        "dep": "서울",
        "arr": "부산",
        "date": "20260602",
        "from_time": "090000",
        "to_time": "100000",
        "train_type": "ktx",
        "login": {"user_id": "member-1", "password": "secret-pw"},
    })
    assert search_resp.status_code == 200

    start_resp = client.post("/api/snipe/start", json={
        "dep": "서울",
        "arr": "부산",
        "date": "20260602",
        "from_time": "090000",
        "to_time": "100000",
        "train_type": "ktx",
        "train_ids": ["ktx:test"],
        "seat_option": "general-first",
        "try_waiting": False,
        "interval": 30,
    })
    assert start_resp.status_code == 200

    thread = ticket_web._session["thread"]
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert client_obj.reserve_calls

    stream_resp = client.get("/api/snipe/stream")
    stream_body = b"".join(stream_resp.response).decode("utf-8")
    assert '"type": "success"' in stream_body
    assert "R123" in stream_body

    state = client.get("/api/state").get_json()
    assert state["success"]["reservation_id"] == "R123"
    assert state["success"]["buy_limit_date"] == "2026-06-02"
    assert state["snipe"]["payload"]["train_ids"] == ["ktx:test"]
    assert "secret-pw" not in str(state)
