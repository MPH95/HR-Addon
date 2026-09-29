# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

"""Month overview of the whole company for the HR app: one row per employee."""

import frappe
from frappe.utils import flt, getdate

from hr_addon.api.employee_app import _current_employee, _days_for_range, _month_bounds

WORKED_STATUSES = {"Present", "Half Day", "Missing Checkin"}


def team_cell(day, own=False):
	"""Reduce one day from `_days_for_range` to what a colleague needs to see.

	`own` adds the request behind the cell so the employee can open and edit it.
	"""
	leave = day.get("leave")
	home = day.get("home_office")
	status = day.get("status") or ""
	cell = {
		"date": day["date"],
		"kind": "free",
		"pending": False,
		"half_day": False,
		"hours": flt(day.get("actual_working_hours"), 2),
		"leave_type": None,
		"request": None,
	}

	if leave and status in ("On Leave", "Pending Leave", "Half Day"):
		cell["kind"] = "sick" if leave.get("illness") else "leave"
		cell["pending"] = leave.get("docstatus") == 0
		cell["half_day"] = status == "Half Day"
		cell["leave_type"] = leave.get("leave_type")
	elif status == "Holiday":
		cell["kind"] = "holiday"
	elif home:
		cell["kind"] = "home"
		cell["pending"] = home.get("docstatus") == 0
		cell["half_day"] = bool(home.get("half_day"))
	elif status in WORKED_STATUSES:
		cell["kind"] = "work"
	elif status in ("Missing", "Absent"):
		cell["kind"] = "missing"
	elif status == "Open":
		cell["kind"] = "open"

	if own:
		if cell["kind"] in ("leave", "sick"):
			cell["request"] = {"kind": "leave", "name": leave.get("name")}
		elif cell["kind"] == "home":
			cell["request"] = {"kind": "home_office", "name": home.get("name")}
	return cell


def _departments(company):
	return sorted(
		{
			row.department
			for row in frappe.get_all(
				"Employee",
				filters={"company": company, "status": "Active", "department": ["is", "set"]},
				fields=["department"],
				ignore_permissions=True,
			)
		}
	)


@frappe.whitelist()
def get_team_calendar(year=None, month=None, department=None):
	me = _current_employee()
	company = frappe.db.get_value("Employee", me, "company")
	today = getdate()
	start, end = _month_bounds(year or today.year, month or today.month)

	filters = {"company": company, "status": "Active"}
	if department:
		filters["department"] = department
	members = frappe.get_all(
		"Employee",
		filters=filters,
		fields=["name", "employee_name", "department", "designation", "image"],
		order_by="employee_name asc",
		ignore_permissions=True,
	)

	rows = []
	for member in members:
		days = _days_for_range(member.name, start, end)
		rows.append(
			{
				"employee": member.name,
				"employee_name": member.employee_name,
				"department": member.department,
				"designation": member.designation,
				"image": member.image,
				"is_me": member.name == me,
				"days": [team_cell(day, own=member.name == me) for day in days],
			}
		)

	return {
		"year": start.year,
		"month": start.month,
		"company": company,
		"departments": _departments(company),
		"members": rows,
	}
