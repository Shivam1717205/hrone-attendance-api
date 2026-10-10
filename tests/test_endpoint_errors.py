import unittest
from datetime import date
from unittest.mock import Mock, patch

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

import app.main as main


class EndpointErrorTests(unittest.TestCase):
    def test_health_returns_503_when_mongodb_ping_fails(self):
        with patch.object(main.db, "command", side_effect=main.PyMongoError("offline")):
            with self.assertRaises(HTTPException) as raised:
                main.health()

        self.assertEqual(raised.exception.status_code, 503)

    def test_duplicate_employee_insert_returns_409(self):
        body = main.EmployeeIn.model_validate(
            {
                "emp_code": "EMP1001",
                "name": "Test Employee",
                "email": "employee@example.com",
                "department": "QA",
                "joined_on": "2026-10-01",
            }
        )
        collection = Mock()
        collection.insert_one.side_effect = DuplicateKeyError("duplicate")

        with patch.object(main, "employees", collection):
            with self.assertRaises(HTTPException) as raised:
                main.create_employee(body)

        self.assertEqual(raised.exception.status_code, 409)

    def test_missing_employee_returns_404_for_punch_in_and_punch_out(self):
        collection = Mock()
        collection.find_one.return_value = None
        punch_in_body = main.PunchInIn(emp_code="EMP1001")
        punch_out_body = main.PunchOutIn(emp_code="EMP1001")

        with patch.object(main, "employees", collection):
            for operation, body in (
                (main.punch_in, punch_in_body),
                (main.punch_out, punch_out_body),
            ):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaises(HTTPException) as raised:
                        operation(body)
                    self.assertEqual(raised.exception.status_code, 404)

    def test_missing_employee_and_attendance_return_404_for_regularization(self):
        collection = Mock()
        collection.find_one.return_value = None
        body = main.RegularizeIn(reason="correct record", regularized_by="reviewer")

        with patch.object(main, "employees", collection):
            with self.assertRaises(HTTPException) as raised:
                main.regularize_attendance("EMP1001", date(2026, 10, 1), body)

        self.assertEqual(raised.exception.status_code, 404)

        collection.find_one.return_value = {"emp_code": "EMP1001"}
        attendance_collection = Mock()
        attendance_collection.find_one.return_value = None
        with (
            patch.object(main, "employees", collection),
            patch.object(main, "attendance_logs", attendance_collection),
        ):
            with self.assertRaises(HTTPException) as raised:
                main.regularize_attendance("EMP1001", date(2026, 10, 1), body)

        self.assertEqual(raised.exception.status_code, 404)

    def test_year_zero_month_returns_422_in_analytics_and_explain(self):
        with self.assertRaises(HTTPException) as summary_error:
            main.department_summary(month="0000-01")
        self.assertEqual(summary_error.exception.status_code, 422)

        with self.assertRaises(HTTPException) as explain_error:
            main.explain_endpoint("late_leaderboard", month="0000-01")
        self.assertEqual(explain_error.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
