import unittest
from datetime import datetime, timedelta, timezone

import app.main as main


class AttendanceRuleTests(unittest.TestCase):
    def setUp(self):
        self.employee = {"shift_start": "09:30", "shift_end": "18:30"}
        self.day = datetime(2026, 10, 12).date()

    def test_overnight_attendance_date_changes_at_shift_end(self):
        employee = {"shift_start": "22:00", "shift_end": "06:00"}
        before_shift_end = datetime(2026, 10, 11, 0, 30, tzinfo=main.IST)
        at_shift_end = datetime(2026, 10, 11, 6, 0, tzinfo=main.IST)

        self.assertEqual(main.attendance_date(before_shift_end, employee).isoformat(), "2026-10-10")
        self.assertEqual(main.attendance_date(at_shift_end, employee).isoformat(), "2026-10-11")

    def test_late_grace_boundary_and_fractional_second_truncation(self):
        shift_start = main.shift_boundary(self.day, self.employee["shift_start"])

        at_grace_boundary = shift_start + timedelta(seconds=600, microseconds=900_000)
        one_second_late = shift_start + timedelta(seconds=601)

        self.assertEqual(main.compute_late_minutes(at_grace_boundary, self.employee, self.day), 0)
        self.assertEqual(main.compute_late_minutes(one_second_late, self.employee, self.day), 10)

    def test_overtime_is_counted_only_at_thirty_whole_minutes(self):
        shift_end = main.shift_boundary(self.day, self.employee["shift_end"])

        self.assertEqual(
            main.compute_overtime_minutes(shift_end + timedelta(seconds=1_799), self.employee, self.day),
            0,
        )
        self.assertEqual(
            main.compute_overtime_minutes(shift_end + timedelta(seconds=1_800), self.employee, self.day),
            30,
        )

    def test_work_hours_use_half_up_rounding(self):
        punch_in = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(
            main.compute_work_hours(punch_in, punch_in + timedelta(seconds=16_182)),
            4.5,
        )


if __name__ == "__main__":
    unittest.main()
