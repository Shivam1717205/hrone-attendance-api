import unittest
from uuid import uuid4
from unittest.mock import patch

from pymongo.errors import PyMongoError

import app.main as main


class EmployeeMonthlyAnalyticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            main.client.admin.command("ping")
        except PyMongoError as exc:
            raise unittest.SkipTest(f"MongoDB unavailable for aggregation tests: {exc}") from exc

        cls.database_name = f"attendance_monthly_test_{uuid4().hex}"
        cls.database = main.client[cls.database_name]
        cls.attendance_logs = cls.database.attendance_logs

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "database"):
            main.client.drop_database(cls.database_name)

    def setUp(self):
        self.attendance_logs.delete_many({})

    def monthly_summary(self):
        class EmployeeCollection:
            @staticmethod
            def find_one(query, projection):
                if query != {"emp_code": "EMP1002"}:
                    return None
                if projection != {"_id": 0, "joined_on": 1}:
                    raise AssertionError(f"unexpected employee projection: {projection}")
                return {"joined_on": "2026-01-05"}

        employee_collection = EmployeeCollection()
        with (
            patch.object(main, "employees", employee_collection),
            patch.object(main, "attendance_logs", self.attendance_logs),
        ):
            return main.employee_monthly("EMP1002", month="2026-10")

    def test_monthly_summary_follows_weekday_presence_and_all_record_totals(self):
        self.attendance_logs.insert_many(
            [
                {
                    "emp_code": "EMP1002",
                    "date": "2026-10-05",
                    "status": "PRESENT",
                    "half_day": False,
                    "late_minutes": 12,
                    "overtime_minutes": 20,
                },
                {
                    "emp_code": "EMP1002",
                    "date": "2026-10-06",
                    "status": "WFH",
                    "half_day": True,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                },
                {
                    "emp_code": "EMP1002",
                    "date": "2026-10-10",
                    "status": "PRESENT",
                    "half_day": True,
                    "late_minutes": 380,
                    "overtime_minutes": 25,
                },
                {
                    "emp_code": "EMP1002",
                    "date": "2026-10-07",
                    "status": "LEAVE",
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                },
                {
                    "emp_code": "EMP1002",
                    "date": "2026-10-08",
                    "status": "ABSENT",
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                },
                {
                    "emp_code": "EMP1002",
                    "date": "2026-09-30",
                    "status": "PRESENT",
                    "half_day": False,
                    "late_minutes": 900,
                    "overtime_minutes": 900,
                },
            ]
        )

        result = self.monthly_summary()

        self.assertEqual(result["working_days"], 22)
        self.assertEqual(result["present_days"], 1.5)
        self.assertEqual(result["leave_days"], 1)
        self.assertEqual(result["late_count"], 2)
        self.assertEqual(result["total_late_minutes"], 392)
        self.assertEqual(result["total_overtime_minutes"], 45)
        self.assertEqual(result["attendance_pct"], 6.8182)

    def test_month_without_attendance_records_returns_zero_metrics(self):
        result = self.monthly_summary()

        self.assertEqual(result["working_days"], 22)
        self.assertEqual(result["present_days"], 0)
        self.assertEqual(result["leave_days"], 0)
        self.assertEqual(result["late_count"], 0)
        self.assertEqual(result["total_late_minutes"], 0)
        self.assertEqual(result["total_overtime_minutes"], 0)
        self.assertEqual(result["attendance_pct"], 0)

    def test_saturday_only_presence_matches_the_reported_monthly_result(self):
        self.attendance_logs.insert_one(
            {
                "emp_code": "EMP1002",
                "date": "2026-10-10",
                "status": "PRESENT",
                "half_day": True,
                "late_minutes": 380,
                "overtime_minutes": 0,
            }
        )

        result = self.monthly_summary()

        self.assertEqual(result["working_days"], 22)
        self.assertEqual(result["present_days"], 0)
        self.assertEqual(result["leave_days"], 0)
        self.assertEqual(result["late_count"], 1)
        self.assertEqual(result["total_late_minutes"], 380)
        self.assertEqual(result["total_overtime_minutes"], 0)
        self.assertEqual(result["attendance_pct"], 0)


if __name__ == "__main__":
    unittest.main()
