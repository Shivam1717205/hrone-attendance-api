import unittest
from datetime import date
from uuid import uuid4
from unittest.mock import patch

from pymongo.errors import PyMongoError

import app.main as main


class AnalyticsAggregationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            main.client.admin.command("ping")
        except PyMongoError as exc:
            raise unittest.SkipTest(f"MongoDB unavailable for analytics tests: {exc}") from exc

        cls.database_name = f"attendance_analytics_test_{uuid4().hex}"
        cls.database = main.client[cls.database_name]
        cls.employees = cls.database.employees
        cls.attendance_logs = cls.database.attendance_logs

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "database"):
            main.client.drop_database(cls.database_name)

    def setUp(self):
        self.employees.delete_many({})
        self.attendance_logs.delete_many({})

    def insert_employee(self, emp_code, department="Alpha", joined_on="2026-10-01"):
        self.employees.insert_one(
            {
                "emp_code": emp_code,
                "name": emp_code,
                "email": f"{emp_code.lower()}@example.com",
                "department": department,
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": joined_on,
            }
        )

    def insert_log(self, emp_code, day, status, late=0, hours=None, half_day=False):
        self.attendance_logs.insert_one(
            {
                "emp_code": emp_code,
                "date": day,
                "status": status,
                "late_minutes": late,
                "overtime_minutes": 0,
                "work_hours": hours,
                "half_day": half_day,
            }
        )

    def test_department_summary_counts_joiners_and_averages_records(self):
        self.insert_employee("EMP1001")
        self.insert_employee("EMP1002", joined_on="2026-10-31")
        self.insert_employee("EMP1003", joined_on="2026-11-01")
        self.insert_employee("EMP2001", department="Beta")
        self.insert_log("EMP1001", "2026-10-05", "PRESENT", late=10, hours=8.0)
        self.insert_log("EMP1001", "2026-10-06", "WFH", hours=4.0, half_day=True)
        self.insert_log("EMP1001", "2026-10-07", "LEAVE")
        self.insert_log("EMP1001", "2026-10-08", "ON_DUTY", late=5, hours=6.0)

        with (
            patch.object(main, "employees", self.employees),
            patch.object(main, "attendance_logs", self.attendance_logs),
        ):
            result = main.department_summary(month="2026-10")

        self.assertEqual([row["department"] for row in result["items"]], ["Alpha", "Beta"])
        alpha = result["items"][0]
        self.assertEqual(alpha["headcount"], 2)
        self.assertEqual(alpha["present_days"], 2.5)
        self.assertEqual(alpha["avg_work_hours"], 6.0)
        self.assertEqual(alpha["late_count"], 2)
        self.assertEqual(alpha["total_late_minutes"], 15)
        self.assertEqual(alpha["leave_count"], 1)
        self.assertEqual(alpha["on_duty_count"], 1)
        self.assertEqual(result["items"][1]["headcount"], 1)

    def test_leaderboard_competition_ties_are_kept_at_limit_with_department_ranking(self):
        for code in ("EMP1001", "EMP1002", "EMP1003", "EMP1004"):
            self.insert_employee(code)
        self.insert_employee("EMP2001", department="Beta")
        for day, minutes in (
            ("2026-10-05", 50),
            ("2026-10-06", 50),
        ):
            self.insert_log("EMP1001", day, "PRESENT", late=minutes)
        self.insert_log("EMP1002", "2026-10-05", "PRESENT", late=80)
        self.insert_log("EMP1003", "2026-10-05", "PRESENT", late=80)
        self.insert_log("EMP1004", "2026-10-05", "PRESENT", late=70)
        self.insert_log("EMP2001", "2026-10-05", "PRESENT", late=999)

        with patch.object(main, "attendance_logs", self.attendance_logs):
            result = main.late_leaderboard(month="2026-10", limit=2, department="Alpha")

        self.assertEqual(
            [
                (row["rank"], row["emp_code"], row["total_late_minutes"])
                for row in result["items"]
            ],
            [
                (1, "EMP1001", 100),
                (2, "EMP1002", 80),
                (2, "EMP1003", 80),
            ],
        )

    def test_department_trend_fills_gaps_counts_join_dates_and_windows_rates(self):
        self.insert_employee("EMP1001", joined_on="2026-10-01")
        self.insert_employee("EMP1002", joined_on="2026-10-07")
        self.insert_employee("EMP1003", joined_on="2026-10-10")
        self.insert_log("EMP1001", "2026-10-05", "PRESENT")
        self.insert_log("EMP1001", "2026-10-08", "WFH", half_day=True)
        self.insert_log("EMP1001", "2026-10-10", "PRESENT", late=12, half_day=True)
        self.insert_log("EMP1001", "2026-10-12", "PRESENT")
        self.insert_log("EMP1002", "2026-10-12", "ON_DUTY")

        with patch.object(main, "employees", self.employees):
            result = main.department_trend(
                "Alpha",
                from_date=date(2026, 10, 5),
                to_date=date(2026, 10, 12),
            )

        rows = result["items"]
        self.assertEqual(
            [row["date"] for row in rows],
            [
                "2026-10-05",
                "2026-10-06",
                "2026-10-07",
                "2026-10-08",
                "2026-10-09",
                "2026-10-10",
                "2026-10-11",
                "2026-10-12",
            ],
        )
        self.assertEqual([rows[i]["headcount"] for i in (0, 2, 5, 7)], [1, 2, 3, 3])
        self.assertEqual(rows[0]["attendance_rate"], 1.0)
        self.assertEqual(rows[1]["attendance_rate"], 0.0)
        self.assertEqual(rows[3]["attendance_rate"], 0.25)
        self.assertIsNone(rows[5]["attendance_rate"])
        self.assertEqual(rows[5]["present_count"], 0.5)
        self.assertEqual(rows[5]["late_count"], 1)
        self.assertIsNone(rows[6]["attendance_rate"])
        self.assertEqual(rows[7]["attendance_rate"], 0.6667)
        self.assertEqual(rows[7]["moving_avg_7d"], 0.1833)


if __name__ == "__main__":
    unittest.main()
