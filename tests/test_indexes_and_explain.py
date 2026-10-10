import json
import unittest
from datetime import date
from uuid import uuid4
from unittest.mock import patch

from pymongo.errors import PyMongoError

import app.main as main


class IndexAndExplainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            main.client.admin.command("ping")
        except PyMongoError as exc:
            raise unittest.SkipTest(f"MongoDB unavailable for index/explain tests: {exc}") from exc

        cls.database_name = f"attendance_explain_test_{uuid4().hex}"
        cls.database = main.client[cls.database_name]
        cls.employees = cls.database.employees
        cls.attendance_logs = cls.database.attendance_logs
        with (
            patch.object(main, "employees", cls.employees),
            patch.object(main, "attendance_logs", cls.attendance_logs),
        ):
            main.ensure_indexes()
            main.ensure_indexes()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "database"):
            main.client.drop_database(cls.database_name)

    def test_startup_indexes_are_idempotent_and_have_expected_keys(self):
        employee_indexes = {
            index["name"]: index
            for index in self.employees.list_indexes()
        }
        attendance_indexes = {
            index["name"]: index
            for index in self.attendance_logs.list_indexes()
        }

        self.assertTrue(employee_indexes["uq_employee_code"]["unique"])
        self.assertEqual(
            list(employee_indexes["ix_employee_department_joined"]["key"].items()),
            [("department", 1), ("joined_on", 1)],
        )
        self.assertTrue(attendance_indexes["uq_attendance_employee_date"]["unique"])
        self.assertEqual(
            list(attendance_indexes["ix_attendance_employee_date"]["key"].items()),
            [("emp_code", 1), ("date", -1)],
        )

    def test_all_explain_pipelines_use_indexes_without_collection_scans_at_scale(self):
        employee_count = 4_000
        self.employees.insert_many(
            [
                {
                    "emp_code": f"EMP{index:04d}",
                    "name": f"Employee {index}",
                    "department": "Engineering",
                    "joined_on": "2026-01-01",
                }
                for index in range(employee_count)
            ],
            ordered=False,
        )

        batch = []
        for index in range(employee_count):
            emp_code = f"EMP{index:04d}"
            for day in range(1, 26):
                batch.append(
                    {
                        "emp_code": emp_code,
                        "date": f"2026-10-{day:02d}",
                        "status": "PRESENT",
                        "late_minutes": (index + day) % 60,
                        "overtime_minutes": 0,
                        "work_hours": 8.0,
                        "half_day": False,
                    }
                )
                if len(batch) == 5_000:
                    self.attendance_logs.insert_many(batch, ordered=False)
                    batch.clear()
        if batch:
            self.attendance_logs.insert_many(batch, ordered=False)

        calls = (
            (
                "attendance_list",
                {
                    "emp_code": "EMP0001",
                    "date_from": date(2026, 10, 1),
                    "date_to": date(2026, 10, 25),
                    "page": 1,
                    "page_size": 20,
                },
            ),
            ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-10"}),
            (
                "department_summary",
                {"month": "2026-10", "department": "Engineering"},
            ),
            (
                "late_leaderboard",
                {"month": "2026-10", "limit": 10, "department": "Engineering"},
            ),
            (
                "department_trend",
                {
                    "department": "Engineering",
                    "from_date": date(2026, 10, 5),
                    "to_date": date(2026, 10, 11),
                },
            ),
        )

        with patch.object(main, "db", self.database):
            for endpoint, parameters in calls:
                with self.subTest(endpoint=endpoint):
                    result = main.explain_endpoint(endpoint, **parameters)
                    explained = json.dumps(result["explain"])
                    self.assertIn("executionStats", explained)
                    self.assertIn("IXSCAN", explained)
                    self.assertNotIn("COLLSCAN", explained)


if __name__ == "__main__":
    unittest.main()
