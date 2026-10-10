import asyncio
import json
import unittest
from datetime import datetime, timezone
from urllib.parse import urlencode
from uuid import uuid4
from unittest.mock import Mock, patch

from pymongo.errors import PyMongoError

import app.main as main


class HTTPStatusContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            main.client.admin.command("ping")
        except PyMongoError as exc:
            raise unittest.SkipTest(f"MongoDB unavailable for HTTP tests: {exc}") from exc

        cls.database_name = f"attendance_http_test_{uuid4().hex}"
        cls.database = main.client[cls.database_name]
        cls.employees = cls.database.employees
        cls.attendance_logs = cls.database.attendance_logs
        with (
            patch.object(main, "employees", cls.employees),
            patch.object(main, "attendance_logs", cls.attendance_logs),
        ):
            main.ensure_indexes()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "database"):
            main.client.drop_database(cls.database_name)

    def setUp(self):
        self.employees.delete_many({})
        self.attendance_logs.delete_many({})

    def http_request(self, method, path, body=None, query=None):
        if query:
            path = f"{path}?{urlencode(query)}"
        request_path, _, query_string = path.partition("?")
        encoded_body = b"" if body is None else json.dumps(body).encode()
        sent = []
        received = False
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": request_path,
            "raw_path": request_path.encode(),
            "query_string": query_string.encode(),
            "root_path": "",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": encoded_body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        asyncio.run(main.app(scope, receive, send))
        start = next(message for message in sent if message["type"] == "http.response.start")
        content = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        return start["status"], json.loads(content) if content else None

    def create_employee(self, emp_code="EMP1001"):
        return {
            "emp_code": emp_code,
            "name": "Test Employee",
            "email": "employee@example.com",
            "department": "QA",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }

    def test_health_http_200_and_503(self):
        with patch.object(main, "db", self.database):
            status, body = self.http_request("GET", "/health")
        self.assertEqual((status, body), (200, {"status": "ok"}))

        unavailable = Mock()
        unavailable.command.side_effect = PyMongoError("offline")
        with patch.object(main, "db", unavailable):
            status, body = self.http_request("GET", "/health")
        self.assertEqual(status, 503)
        self.assertEqual(body, {"detail": "MongoDB is unavailable"})

    def test_employee_create_http_success_duplicate_and_validation(self):
        with (
            patch.object(main, "employees", self.employees),
            patch.object(main, "attendance_logs", self.attendance_logs),
        ):
            status, created = self.http_request("POST", "/employees", self.create_employee())
            duplicate_status, duplicate = self.http_request("POST", "/employees", self.create_employee())
            invalid_status, invalid = self.http_request(
                "POST",
                "/employees",
                {**self.create_employee("BAD"), "email": "invalid"},
            )

        self.assertEqual(status, 201)
        self.assertIs(type(created["created_at"]), int)
        self.assertNotIn("_id", created)
        self.assertEqual(duplicate_status, 409)
        self.assertEqual(duplicate["detail"], "emp_code already exists")
        self.assertEqual(invalid_status, 422)
        self.assertIsInstance(invalid["detail"], list)

    def test_attendance_http_not_found_conflicts_and_timestamp_validation(self):
        with (
            patch.object(main, "employees", self.employees),
            patch.object(main, "attendance_logs", self.attendance_logs),
        ):
            missing_in_status, missing_in = self.http_request(
                "POST",
                "/attendance/punch-in",
                {"emp_code": "EMP9999"},
            )
            missing_out_status, missing_out = self.http_request(
                "POST",
                "/attendance/punch-out",
                {"emp_code": "EMP9999"},
            )
            invalid_status, invalid = self.http_request(
                "POST",
                "/attendance/punch-in",
                {"emp_code": "EMP9999", "punched_at": 1_700_000_000},
            )
            self.employees.insert_one(self.create_employee())
            punched_at = main.datetime_to_epoch_ms(
                datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc)
            )
            create_status, created = self.http_request(
                "POST",
                "/attendance/punch-in",
                {"emp_code": "EMP1001", "punched_at": punched_at},
            )
            duplicate_status, duplicate = self.http_request(
                "POST",
                "/attendance/punch-in",
                {"emp_code": "EMP1001", "punched_at": punched_at},
            )
            close_time = main.datetime_to_epoch_ms(
                datetime(2026, 10, 10, 13, 0, tzinfo=timezone.utc)
            )
            close_status, _ = self.http_request(
                "POST",
                "/attendance/punch-out",
                {"emp_code": "EMP1001", "punched_at": close_time},
            )
            conflict_status, conflict = self.http_request(
                "POST",
                "/attendance/punch-out",
                {"emp_code": "EMP1001", "punched_at": close_time},
            )

        self.assertEqual((missing_in_status, missing_in["detail"]), (404, "employee not found"))
        self.assertEqual((missing_out_status, missing_out["detail"]), (404, "employee not found"))
        self.assertEqual(invalid_status, 422)
        self.assertIsInstance(invalid["detail"], list)
        self.assertEqual(create_status, 201)
        self.assertNotIn("_id", created)
        self.assertEqual((duplicate_status, duplicate["detail"]), (409, "already punched in for this date"))
        self.assertEqual(close_status, 200)
        self.assertEqual((conflict_status, conflict["detail"]), (409, "record is already punched out"))

    def test_query_analytics_and_regularization_http_errors(self):
        with (
            patch.object(main, "employees", self.employees),
            patch.object(main, "attendance_logs", self.attendance_logs),
        ):
            self.employees.insert_one(self.create_employee())
            reversed_status, reversed_body = self.http_request(
                "GET",
                "/attendance",
                query={"date_from": "2026-10-02", "date_to": "2026-10-01"},
            )
            page_status, page_body = self.http_request(
                "GET",
                "/employees",
                query={"page_size": 101},
            )
            absent_month_status, absent_month = self.http_request(
                "GET",
                "/analytics/employees/EMP9999/monthly",
                query={"month": "2026-10"},
            )
            absent_department_status, absent_department = self.http_request(
                "GET",
                "/analytics/departments/Unknown/trend",
                query={"from": "2026-10-01", "to": "2026-10-02"},
            )
            missing_record_status, missing_record = self.http_request(
                "PATCH",
                "/attendance/EMP1001/2026-10-01",
                {"reason": "correct record", "regularized_by": "reviewer"},
            )
            no_punch_status, no_punch = self.http_request(
                "POST",
                "/attendance/punch-out",
                {"emp_code": "EMP1001"},
            )
            invalid_patch_status, invalid_patch = self.http_request(
                "PATCH",
                "/attendance/EMP9999/2026-02-30",
                {"reason": "correct record", "regularized_by": "reviewer"},
            )

        self.assertEqual(reversed_status, 422)
        self.assertIn("detail", reversed_body)
        self.assertEqual(page_status, 422)
        self.assertIn("detail", page_body)
        self.assertEqual((absent_month_status, absent_month["detail"]), (404, "employee not found"))
        self.assertEqual((absent_department_status, absent_department["detail"]), (404, "department not found"))
        self.assertEqual((missing_record_status, missing_record["detail"]), (404, "attendance record not found"))
        self.assertEqual((no_punch_status, no_punch["detail"]), (404, "no punch-in found"))
        self.assertEqual(invalid_patch_status, 422)
        self.assertIsInstance(invalid_patch["detail"], list)


if __name__ == "__main__":
    unittest.main()
