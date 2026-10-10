import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4
from unittest.mock import patch

from fastapi import HTTPException
from pymongo.errors import PyMongoError

import app.main as main


class PunchOutConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            main.client.admin.command("ping")
        except PyMongoError as exc:
            raise unittest.SkipTest(f"MongoDB unavailable for punch-out concurrency test: {exc}") from exc

        cls.database_name = f"attendance_punch_out_test_{uuid4().hex}"
        cls.database = main.client[cls.database_name]
        cls.employees = cls.database.employees
        cls.attendance_logs = cls.database.attendance_logs
        cls.employees.create_index("emp_code", unique=True)
        cls.attendance_logs.create_index(
            [("emp_code", 1), ("date", 1)],
            unique=True,
        )

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "database"):
            main.client.drop_database(cls.database_name)

    def setUp(self):
        self.employees.delete_many({})
        self.attendance_logs.delete_many({})
        self.employees.insert_one(
            {
                "emp_code": "EMP9001",
                "name": "Punch Out Test",
                "department": "QA",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
            }
        )
        self.punch_in = datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc)
        self.punch_out = datetime(2026, 10, 10, 13, 0, tzinfo=timezone.utc)
        self.attendance_logs.insert_one(
            {
                "emp_code": "EMP9001",
                "date": "2026-10-10",
                "status": "PRESENT",
                "punch_in": self.punch_in,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            }
        )

    def test_simultaneous_punch_outs_close_record_once(self):
        worker_count = 8
        barrier = Barrier(worker_count)
        body = main.PunchOutIn(
            emp_code="EMP9001",
            punched_at=main.datetime_to_epoch_ms(self.punch_out),
        )

        def submit_punch_out():
            barrier.wait()
            try:
                return main.punch_out(body)["punch_out"], None
            except HTTPException as exc:
                return None, exc.status_code

        with (
            patch.object(main, "employees", self.employees),
            patch.object(main, "attendance_logs", self.attendance_logs),
            ThreadPoolExecutor(max_workers=worker_count) as executor,
        ):
            results = list(executor.map(lambda _: submit_punch_out(), range(worker_count)))

        successes = [punched_out for punched_out, error in results if error is None]
        conflicts = [error for _, error in results if error is not None]
        self.assertEqual(successes, [main.datetime_to_epoch_ms(self.punch_out)])
        self.assertEqual(conflicts, [409] * (worker_count - 1))

        record = self.attendance_logs.find_one({"emp_code": "EMP9001", "date": "2026-10-10"})
        self.assertEqual(main.datetime_to_epoch_ms(record["punch_out"]), main.datetime_to_epoch_ms(self.punch_out))
        self.assertEqual(record["work_hours"], 9.0)
        self.assertEqual(record["history"], [])

    def test_punch_out_sets_half_day_from_rounded_work_hours(self):
        second_employee = {
            "emp_code": "EMP9002",
            "name": "Half Day Test",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        self.employees.insert_one(second_employee)
        self.attendance_logs.insert_one(
            {
                "emp_code": "EMP9002",
                "date": "2026-10-10",
                "status": "PRESENT",
                "punch_in": self.punch_in,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            }
        )

        with (
            patch.object(main, "employees", self.employees),
            patch.object(main, "attendance_logs", self.attendance_logs),
        ):
            rounded_up = main.punch_out(
                main.PunchOutIn(
                    emp_code="EMP9001",
                    punched_at=main.datetime_to_epoch_ms(
                        self.punch_in + timedelta(seconds=16_182)
                    ),
                )
            )
            below_threshold = main.punch_out(
                main.PunchOutIn(
                    emp_code="EMP9002",
                    punched_at=main.datetime_to_epoch_ms(
                        self.punch_in + timedelta(seconds=16_181)
                    ),
                )
            )

        self.assertEqual(rounded_up["work_hours"], 4.5)
        self.assertFalse(rounded_up["half_day"])
        self.assertEqual(below_threshold["work_hours"], 4.49)
        self.assertTrue(below_threshold["half_day"])


if __name__ == "__main__":
    unittest.main()
