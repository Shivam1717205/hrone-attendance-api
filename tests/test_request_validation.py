import unittest
from datetime import date

from fastapi import HTTPException
from pydantic import ValidationError

import app.main as main


class RequestValidationTests(unittest.TestCase):
    def test_employee_create_rejects_invalid_code_email_date_and_equal_shifts(self):
        valid = {
            "emp_code": "EMP1001",
            "name": "Test Employee",
            "email": "employee@example.com",
            "department": "QA",
            "joined_on": "2026-10-01",
        }
        invalid_cases = (
            {"emp_code": "EMP1"},
            {"email": "not-an-email"},
            {"joined_on": "2026-02-30"},
            {"shift_start": "09:30", "shift_end": "09:30"},
        )
        for override in invalid_cases:
            with self.subTest(override=override):
                with self.assertRaises(ValidationError):
                    main.EmployeeIn.model_validate({**valid, **override})

    def test_punch_requests_reject_seconds_floats_strings_and_invalid_status(self):
        invalid_payloads = (
            {"emp_code": "EMP1001", "punched_at": 1_783_310_400},
            {"emp_code": "EMP1001", "punched_at": 1_783_310_400_000.0},
            {"emp_code": "EMP1001", "punched_at": "1783310400000"},
            {"emp_code": "EMP1001", "status": "LEAVE"},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    main.PunchInIn.model_validate(payload)

    def test_regularization_rejects_null_patch_values_and_derived_fields_are_ignored(self):
        with self.assertRaises(ValidationError):
            main.RegularizeIn.model_validate(
                {
                    "status": None,
                    "reason": "correct status",
                    "regularized_by": "reviewer",
                }
            )

        request = main.RegularizeIn.model_validate(
            {
                "status": "PRESENT",
                "reason": "correct status",
                "regularized_by": "reviewer",
                "work_hours": 99,
                "late_minutes": 999,
            }
        )
        self.assertNotIn("work_hours", request.model_dump())
        self.assertNotIn("late_minutes", request.model_dump())

    def test_attendance_filter_rejects_reversed_date_range_with_422(self):
        with self.assertRaises(HTTPException) as raised:
            main.attendance_filter(
                emp_code=None,
                date_from=date(2026, 10, 2),
                date_to=date(2026, 10, 1),
                status=None,
            )
        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
