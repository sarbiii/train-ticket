#!/usr/bin/env python3
from __future__ import annotations

import unittest
from datetime import date

import app_discord
from app_discord import FormError, build_form_text, format_train_table, parse_booking_form


class DiscordBookingFormTest(unittest.TestCase):
    def test_parse_full_form(self) -> None:
        request = parse_booking_form(
            """
            열차: KTX
            출발: 서울
            도착: 부산
            날짜: 2026-06-01
            시작: 9시
            종료: 13:30
            좌석: 일반우선
            성인: 2
            """,
            today=date(2026, 5, 23),
        )

        self.assertEqual(request.train, "ktx")
        self.assertEqual(request.dep, "서울")
        self.assertEqual(request.arr, "부산")
        self.assertEqual(request.date, "20260601")
        self.assertEqual(request.from_time, "090000")
        self.assertEqual(request.to_time, "133000")
        self.assertEqual(request.seat_option, "general-first")
        self.assertFalse(request.try_waiting)
        self.assertEqual(request.targets, "all")
        self.assertEqual(request.adults, 2)
        self.assertEqual(request.children, 0)

    def test_missing_required_field(self) -> None:
        with self.assertRaises(FormError):
            parse_booking_form("출발: 서울\n날짜: 20260601", today=date(2026, 5, 23))

    def test_swaps_reversed_time_range(self) -> None:
        request = parse_booking_form(
            "출발: 서울\n도착: 부산\n날짜: 20260601\n시작: 1800\n종료: 0900",
            today=date(2026, 5, 23),
        )

        self.assertEqual(request.from_time, "090000")
        self.assertEqual(request.to_time, "180000")

    def test_accepts_command_prefix_line(self) -> None:
        request = parse_booking_form(
            "/예매 + 출발: 서울\n도착: 부산\n날짜: 20260601",
            today=date(2026, 5, 23),
        )

        self.assertEqual(request.dep, "서울")

    def test_format_table(self) -> None:
        request = parse_booking_form(
            "출발: 서울\n도착: 부산\n날짜: 20260601",
            today=date(2026, 5, 23),
        )
        message = format_train_table(
            request,
            [
                {
                    "index": 1,
                    "train_no": "101",
                    "dep_time": "090000",
                    "arr_time": "113000",
                    "has_general_seat": True,
                    "has_special_seat": False,
                    "has_waiting_list": False,
                }
            ],
        )

        self.assertIn("KTX 서울 -> 부산", message)
        self.assertIn("101", message)
        self.assertIn("일반석", message)

    def test_format_table_can_report_snipe_start(self) -> None:
        request = parse_booking_form(
            "출발: 서울\n도착: 부산\n날짜: 20260601",
            today=date(2026, 5, 23),
        )
        message = format_train_table(
            request,
            [
                {
                    "index": 1,
                    "train_no": "101",
                    "dep_time": "090000",
                    "arr_time": "113000",
                    "has_general_seat": False,
                    "has_special_seat": False,
                    "has_waiting_list": True,
                }
            ],
            footer="조회된 1개 열차 예약 스나이퍼를 시작했습니다.",
        )

        self.assertIn("예약 스나이퍼를 시작", message)
        self.assertNotIn("실제 예약/결제는 아직 진행하지 않았습니다", message)

    def test_build_form_text_hides_fixed_options(self) -> None:
        form_text = build_form_text(
            {
                "train": "KTX",
                "dep": "서울",
                "arr": "부산",
                "date": "20260601",
                "time_range": "0900~1300",
                "seat_option": "일반우선",
                "adults": "3",
            }
        )

        self.assertIn("출발: 서울", form_text)
        self.assertIn("시작: 0900", form_text)
        self.assertIn("성인: 3", form_text)
        self.assertNotIn("대기:", form_text)
        self.assertNotIn("대상:", form_text)
        self.assertNotIn("간격:", form_text)
        self.assertNotIn("어린이:", form_text)

    def test_build_form_text_defaults_to_one_adult(self) -> None:
        request = parse_booking_form(
            build_form_text(
                {
                    "train": "KTX",
                    "dep": "서울",
                    "arr": "부산",
                    "date": "20260601",
                    "time_range": "0900~1300",
                    "seat_option": "일반우선",
                }
            ),
            today=date(2026, 5, 23),
        )

        self.assertEqual(request.adults, 1)
        self.assertEqual(request.children, 0)

    def test_build_reservation_plan_defaults_to_all_targets(self) -> None:
        request = parse_booking_form(
            build_form_text(
                {
                    "train": "KTX",
                    "dep": "서울",
                    "arr": "부산",
                    "date": "20260601",
                    "time_range": "0900~1300",
                    "seat_option": "일반우선",
                }
            ),
            today=date(2026, 5, 23),
        )

        class Train:
            def __init__(self, train_no: str, dep_time: str) -> None:
                self.train_no = train_no
                self.dep_time = dep_time

        trains = [Train("101", "090000"), Train("102", "100000"), Train("103", "110000")]

        original = app_discord._search_raw_trains
        try:
            app_discord._search_raw_trains = lambda req: (
                object(),
                trains,
                lambda train, idx: {
                    "index": idx,
                    "train_no": train.train_no,
                    "dep_time": train.dep_time,
                    "arr_time": "120000",
                    "has_general_seat": False,
                    "has_special_seat": False,
                    "has_waiting_list": False,
                },
                lambda train: f"id-{train.train_no}",
            )
            plan = app_discord.build_reservation_plan(request)
        finally:
            app_discord._search_raw_trains = original

        self.assertEqual(plan.train_ids, ["id-101", "id-102", "id-103"])


if __name__ == "__main__":
    unittest.main()
