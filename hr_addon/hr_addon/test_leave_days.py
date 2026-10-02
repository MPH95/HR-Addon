# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from hr_addon.hr_addon.leave_days import charged_leave_days


def _hours(hours):
	return SimpleNamespace(hours=hours)


class TestChargedLeaveDays(FrappeTestCase):
	@patch("hr_addon.hr_addon.leave_days._holiday_dates", return_value=set())
	@patch("hr_addon.hr_addon.leave_days.get_employee_default_work_hour")
	def test_zero_hour_days_are_not_deducted(self, mock_hours, _holidays):
		def hours(_employee, day, skip_workday_if_no_weekly_hours=1):
			return _hours(0 if datetime.strptime(str(day), "%Y-%m-%d").weekday() >= 5 else 8)

		mock_hours.side_effect = hours
		# Friday to Monday: Friday and Monday count, the weekend does not.
		self.assertEqual(charged_leave_days("HR-EMP-00002", "Privilege Leave", "2026-10-02", "2026-10-05"), 2)
		# Saturday and Sunday alone deduct nothing, because their row is 0 hours, not because of the weekday name.
		self.assertEqual(charged_leave_days("HR-EMP-00002", "Privilege Leave", "2026-10-03", "2026-10-04"), 0)

	@patch("hr_addon.hr_addon.leave_days._holiday_dates", return_value=set())
	@patch("hr_addon.hr_addon.leave_days._has_weekly_hours", return_value=True)
	@patch("hr_addon.hr_addon.leave_days.get_employee_default_work_hour", return_value=None)
	def test_missing_weekday_is_not_deducted_when_weekly_hours_exist(self, _hours, _covered, _holidays):
		self.assertEqual(charged_leave_days("HR-EMP-00002", "Privilege Leave", "2026-10-07", "2026-10-07"), 0)

	@patch("hr_addon.hr_addon.leave_days._holiday_dates", return_value=set())
	@patch("hr_addon.hr_addon.leave_days._has_weekly_hours", return_value=False)
	@patch("hr_addon.hr_addon.leave_days.get_employee_default_work_hour", return_value=None)
	def test_day_still_counts_when_the_employee_has_no_weekly_hours(self, _hours, _covered, _holidays):
		self.assertEqual(charged_leave_days("HR-EMP-00002", "Privilege Leave", "2026-10-07", "2026-10-07"), 1)

	@patch("hr_addon.hr_addon.leave_days.frappe.db.get_value", return_value=0)
	@patch("hr_addon.hr_addon.leave_days._holiday_dates", return_value={"2026-10-02"})
	@patch("hr_addon.hr_addon.leave_days.get_employee_default_work_hour", return_value=_hours(8))
	def test_holiday_is_not_deducted_when_the_type_excludes_holidays(self, _hours_mock, _holidays, _include):
		self.assertEqual(charged_leave_days("HR-EMP-00002", "Privilege Leave", "2026-10-02", "2026-10-02"), 0)
