# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

"""Leave days that actually count.

A day is not deducted, and does not block another request, when Weekly Working
Hours says it is not a working day: the weekday row has 0 hours, or the
employee has weekly hours for that date but no row for that weekday. Nothing
here looks at Saturday or Sunday by name. A holiday is skipped too, unless the
leave type includes holidays.
"""

import frappe
from frappe.utils import add_days, cint, flt, getdate

from hr_addon.api.employee_app import schedule_kind
from hr_addon.hr_addon.doctype.workday.workday import get_employee_default_work_hour


def _dates(from_date, to_date):
	current = getdate(from_date)
	last = getdate(to_date)
	while current <= last:
		yield current
		current = add_days(current, 1)


def _has_weekly_hours(employee, day):
	day = getdate(day)
	return bool(
		frappe.db.exists(
			"Weekly Working Hours",
			{
				"employee": employee,
				"docstatus": 1,
				"valid_from": ["<=", day],
				"valid_to": [">=", day],
			},
		)
	)


def is_zero_hour_day(employee, day):
	"""True when this date is not a working day in Weekly Working Hours.

	A 0-hour row counts, and so does a missing weekday when the employee does
	have weekly hours covering the date. With no weekly hours at all the day
	still counts, so leave can be requested before that setup exists.
	"""
	default = get_employee_default_work_hour(employee, day, skip_workday_if_no_weekly_hours=1)
	kind = schedule_kind(default)
	if kind == "free":
		return True
	if kind == "work":
		return False
	return _has_weekly_hours(employee, day)


def _holiday_dates(employee, from_date, to_date):
	from hrms.hr.utils import get_holidays_for_employee

	rows = get_holidays_for_employee(employee, from_date, to_date, raise_exception=False) or []
	return {str(getdate(row.holiday_date)) for row in rows}


def charged_leave_days(employee, leave_type, from_date, to_date, half_day=0, half_day_date=None):
	"""Leave days to deduct. Holidays (unless the type includes them) and non-working days count as 0."""
	if not employee or not from_date or not to_date or getdate(to_date) < getdate(from_date):
		return 0.0

	include_holiday = False
	if leave_type:
		include_holiday = bool(frappe.db.get_value("Leave Type", leave_type, "include_holiday"))
	holidays = set() if include_holiday else _holiday_dates(employee, from_date, to_date)
	half = getdate(half_day_date) if cint(half_day) and half_day_date else None

	total = 0.0
	for day in _dates(from_date, to_date):
		if str(day) in holidays or is_zero_hour_day(employee, day):
			continue
		total += 0.5 if half and day == half else 1.0
	return flt(total)
