import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bson.int64 import Int64
from fastapi import HTTPException

import app.main as main


class TimestampConversionTests(unittest.TestCase):
    def test_datetime_to_epoch_milliseconds_accepts_datetime_and_integer_types(self):
        instant = datetime(2026, 7, 6, 4, 0, 0, 900000, tzinfo=timezone.utc)
        epoch_ms = 1_783_310_400_900

        self.assertEqual(main.datetime_to_epoch_ms(instant), epoch_ms - 900)
        self.assertEqual(main.datetime_to_epoch_ms(Int64(epoch_ms)), epoch_ms - 900)

    def test_datetime_to_epoch_milliseconds_rejects_invalid_integer_like_values(self):
        for value in (True, 1.5, "1783314000900", 99_999_999_999):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException) as raised:
                    main.datetime_to_epoch_ms(value)
                self.assertEqual(raised.exception.status_code, 422)

    def test_attendance_list_serializes_bson_datetimes_and_integer_milliseconds(self):
        instant = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        epoch_ms = 1_783_310_400_000
        documents = [
            {
                "emp_code": "EMP0001",
                "date": "2026-07-06",
                "status": "PRESENT",
                "punch_in": instant,
                "punch_out": None,
                "history": [],
            },
            {
                "emp_code": "EMP0002",
                "date": "2026-07-06",
                "status": "PRESENT",
                "punch_in": Int64(epoch_ms),
                "punch_out": None,
                "history": [],
            },
        ]

        class Cursor:
            def sort(self, fields):
                self.fields = fields
                return self

            def skip(self, count):
                self.skip_count = count
                return self

            def limit(self, count):
                self.limit_count = count
                return self

            def __iter__(self):
                return iter(documents)

        class AttendanceCollection:
            def count_documents(self, query):
                self.query = query
                return len(documents)

            def find(self, query):
                self.query = query
                return Cursor()

        collection = AttendanceCollection()
        with patch.object(main, "attendance_logs", collection):
            response = main.list_attendance(
                emp_code=None,
                date_from=None,
                date_to=None,
                status=None,
                page=1,
                page_size=20,
            )

        self.assertEqual(response["items"][0]["punch_in"], epoch_ms)
        self.assertEqual(response["items"][1]["punch_in"], epoch_ms)
        self.assertIs(type(response["items"][0]["punch_in"]), int)
        self.assertIs(type(response["items"][1]["punch_in"]), int)

    def test_attendance_filter_uses_ist_calendar_date_strings(self):
        query = main.attendance_filter(
            emp_code="EMP0001",
            date_from=datetime(2026, 7, 6).date(),
            date_to=datetime(2026, 7, 7).date(),
            status="PRESENT",
        )

        self.assertEqual(
            query,
            {
                "emp_code": "EMP0001",
                "date": {"$gte": "2026-07-06", "$lte": "2026-07-07"},
                "status": "PRESENT",
            },
        )


if __name__ == "__main__":
    unittest.main()
