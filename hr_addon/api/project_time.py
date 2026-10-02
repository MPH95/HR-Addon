# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

"""Monthly project hours for the employee app.

Hours are stored on an ERPNext Timesheet so project costing stays in the
standard project. The employee picks a project, an activity, and hours.
Start and end times are filled in here, in half-hour steps, only because a
Timesheet line requires them. Lines are never billable, and rates are never
returned to the employee.
"""

import calendar
import json
from datetime import datetime, time, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate

from hr_addon.api.employee_app import (
	_current_employee,
	_is_holiday,
	_today_worked_hours,
	_workday_rows,
	schedule_kind,
)
from hr_addon.hr_addon.doctype.workday.workday import get_employee_default_work_hour

MONTH_FIELD = "custom_project_hours_month"
# Stored activity type, and the label shown in the app.
ACTIVITIES = (
	("Development", "Development"),
	("Operational", "Operational work"),
)
DAY_START = time(8, 0)
MAX_DAY_HOURS = 24

# These amounts sit on the standard form at permission level 0. Employees can
# open a Timesheet, so move them to level 1, which the Employee role does not have.
RATE_FIELDS = (
	("Timesheet", "base_total_costing_amount"),
	("Timesheet", "base_total_billable_amount"),
	("Timesheet", "base_total_billed_amount"),
	("Timesheet Detail", "base_billing_rate"),
	("Timesheet Detail", "base_billing_amount"),
	("Timesheet Detail", "base_costing_rate"),
	("Timesheet Detail", "base_costing_amount"),
)


def keep_project_hours_internal(doc, method=None):
	"""Project-hours sheets track cost only. They are not invoiced."""
	if not doc.get(MONTH_FIELD):
		return
	for row in doc.time_logs or []:
		row.is_billable = 0


@frappe.whitelist()
def get_my_project_time(year, month):
	employee = _current_employee()
	start, end = _month_bounds(year, month)
	_ensure_setup()
	return _month_payload(employee, start, end)


@frappe.whitelist()
def save_my_project_time(year, month, entries):
	employee = _current_employee()
	start, end = _month_bounds(year, month)
	_ensure_setup()
	parsed = _parse_entries(entries)
	_replace_month(employee, start, end, parsed)
	return _month_payload(employee, start, end)


def _month_payload(employee, start, end):
	company = frappe.db.get_value("Employee", employee, "company")
	stored = _stored_entries(employee, start)
	projects = _projects(company, employee, {row["project"] for row in stored})
	booked = flt(sum(row["hours"] for row in stored), 2)
	return {
		"year": start.year,
		"month": start.month,
		"worked_hours": _worked_hours(employee, start, end),
		"booked_hours": booked,
		"projects": projects,
		"activities": [{"name": name, "label": _(label)} for name, label in ACTIVITIES],
		"entries": stored,
		"days": _day_rows(employee, start, end),
	}


def _day_rows(employee, start, end):
	"""Target and worked hours for every day in the month. Booked hours stay on the entries."""
	workdays = {getdate(row.log_date): row for row in _workday_rows(employee, start, end)}
	today = getdate()
	days = []
	current = getdate(start)
	while current <= getdate(end):
		target, worked = _target_and_worked(employee, current, workdays.get(current), today)
		days.append(
			{
				"date": str(current),
				"target_hours": flt(target, 2),
				"worked_hours": flt(worked, 2),
			}
		)
		current = add_days(current, 1)
	return days


def _target_and_worked(employee, day, row, today):
	if row:
		target = flt(row.target_hours)
		worked = flt(row.actual_working_hours)
	else:
		default = get_employee_default_work_hour(employee, day, skip_workday_if_no_weekly_hours=1)
		target = flt(default.hours) if default and schedule_kind(default) == "work" else 0
		if default and _is_holiday(employee, day) and default.set_target_hours_to_zero_when_date_is_holiday:
			target = 0
		worked = 0
	if day == today:
		live, _open = _today_worked_hours(employee, today)
		worked = flt(live)
	return target, worked


def _worked_hours(employee, start, end):
	rows = _workday_rows(employee, start, end)
	total = sum(flt(row.actual_working_hours) for row in rows)
	today = getdate()
	if start <= today <= end:
		stored_today = sum(
			flt(row.actual_working_hours) for row in rows if getdate(row.log_date) == today
		)
		live, _open = _today_worked_hours(employee, today)
		total = total - stored_today + flt(live)
	return flt(total, 2)


def _stored_entries(employee, start):
	names = frappe.get_all(
		"Timesheet",
		filters={
			"employee": employee,
			MONTH_FIELD: _month_key(start),
			"docstatus": 1,
		},
		pluck="name",
		ignore_permissions=True,
	)
	if not names:
		return []
	rows = frappe.get_all(
		"Timesheet Detail",
		filters={"parent": names[0], "parenttype": "Timesheet"},
		fields=["from_time", "hours", "project", "project_name", "activity_type", "description"],
		order_by="from_time asc",
		ignore_permissions=True,
	)
	entries = []
	for row in rows:
		entries.append(
			{
				"date": str(getdate(row.from_time)),
				"project": row.project,
				"project_name": row.project_name or row.project,
				"activity_type": row.activity_type,
				"hours": flt(row.hours, 2),
				"description": (row.description or "").strip(),
			}
		)
	return entries


def _projects(company, employee, extra_names):
	rows = frappe.get_all(
		"Project",
		filters={"company": company, "status": "Open"},
		fields=["name", "project_name"],
		order_by="project_name asc",
		limit_page_length=500,
		ignore_permissions=True,
	)
	known = {row.name for row in rows}
	for name in extra_names:
		if not name or name in known:
			continue
		project_name = frappe.db.get_value("Project", name, "project_name")
		if project_name:
			rows.append({"name": name, "project_name": project_name})
	history = _project_history(employee)
	projects = [{"name": row.name, "project_name": row.project_name or row.name} for row in rows]

	def rank(project):
		stats = history.get(project["name"])
		if not stats:
			return (1, 0, 0, project["project_name"].lower())
		return (0, -cint(stats.uses), -flt(stats.hours), project["project_name"].lower())

	projects.sort(key=rank)
	return projects


def _project_history(employee):
	"""How often this employee has booked each project on a submitted timesheet."""
	rows = frappe.db.sql(
		"""
		SELECT td.project AS project, COUNT(*) AS uses, SUM(td.hours) AS hours
		FROM `tabTimesheet Detail` td
		INNER JOIN `tabTimesheet` t ON t.name = td.parent
		WHERE t.employee = %s
			AND t.docstatus = 1
			AND IFNULL(td.project, '') != ''
		GROUP BY td.project
		""",
		employee,
		as_dict=True,
	)
	return {row.project: row for row in rows}


def _replace_month(employee, start, end, entries):
	logs = _timesheet_rows(employee, start, end, entries)
	key = _month_key(start)
	previous = frappe.get_all(
		"Timesheet",
		filters={"employee": employee, MONTH_FIELD: key, "docstatus": ["<", 2]},
		pluck="name",
		ignore_permissions=True,
	)
	drafts = []
	submitted = []
	for name in previous:
		status = frappe.db.get_value("Timesheet", name, "docstatus")
		if status == 0:
			drafts.append(name)
		elif status == 1:
			submitted.append(name)
	for name in drafts:
		frappe.delete_doc("Timesheet", name, ignore_permissions=True, force=True)
	# Cancel the current sheet before inserting the replacement, or the new
	# lines overlap the ones that are still submitted.
	for name in submitted:
		previous_doc = frappe.get_doc("Timesheet", name)
		previous_doc.flags.ignore_permissions = True
		previous_doc.cancel()

	if logs:
		doc = frappe.new_doc("Timesheet")
		doc.naming_series = "TS-.YYYY.-"
		doc.company = frappe.db.get_value("Employee", employee, "company")
		doc.employee = employee
		doc.user = frappe.db.get_value("Employee", employee, "user_id")
		doc.set(MONTH_FIELD, key)
		for log in logs:
			doc.append("time_logs", log)
		doc.insert(ignore_permissions=True)
		doc.submit()

	for name in submitted:
		frappe.delete_doc("Timesheet", name, ignore_permissions=True, force=True)


def _timesheet_rows(employee, start, end, entries):
	company = frappe.db.get_value("Employee", employee, "company")
	open_projects = {
		row.name
		for row in frappe.get_all(
			"Project",
			filters={"company": company, "status": "Open"},
			fields=["name"],
			limit_page_length=500,
			ignore_permissions=True,
		)
	}
	allowed_activities = {name for name, _label in ACTIVITIES}
	merged = {}
	for entry in entries:
		day = getdate(entry.get("date"))
		if day < start or day > end:
			frappe.throw(_("Each entry has to be inside the selected month."))
		project = (entry.get("project") or "").strip()
		activity = (entry.get("activity_type") or "").strip()
		hours = _booked_hours(entry.get("hours"))
		if hours <= 0:
			continue
		if project not in open_projects:
			frappe.throw(_("Project {0} is not an open project you can book.").format(project))
		if activity not in allowed_activities:
			frappe.throw(_("Choose Development or Operational work."))
		key = (day, project, activity)
		note = _description(entry.get("description"))
		current = merged.get(key)
		if not current:
			merged[key] = {"hours": hours, "description": note}
		else:
			current["hours"] = flt(current["hours"] + hours, 2)
			if note:
				current["description"] = note
		merged[key]["hours"] = _booked_hours(merged[key]["hours"])

	by_day = {}
	for (day, project, activity), row in merged.items():
		by_day.setdefault(day, []).append((project, activity, row["hours"], row["description"]))

	logs = []
	for day in sorted(by_day):
		cursor = datetime.combine(day, DAY_START)
		day_hours = 0
		for project, activity, hours, description in sorted(by_day[day]):
			day_hours += hours
			if day_hours > MAX_DAY_HOURS:
				frappe.throw(_("A day cannot contain more than {0} hours.").format(MAX_DAY_HOURS))
			# Seconds, so a remainder such as 0.33 h is not rounded to the next minute.
			seconds = int(round(hours * 3600))
			end_at = cursor + timedelta(seconds=seconds)
			logs.append(
				{
					"activity_type": activity,
					"from_time": cursor,
					"to_time": end_at,
					"hours": flt(seconds / 3600, 2),
					"project": project,
					"description": description,
					"is_billable": 0,
					"billing_hours": 0,
				}
			)
			cursor = end_at
	return logs


def _description(value):
	"""What the employee did. Required on every booking."""
	text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
	if not text:
		frappe.throw(_("Describe what you did."))
	if len(text) > 500:
		frappe.throw(_("Keep the description under 500 characters."))
	return text


def _booked_hours(value):
	"""Hours on a booking, kept to two decimals. The stepper uses half hours; All may send the remainder."""
	hours = flt(value)
	if hours < 0:
		frappe.throw(_("Hours cannot be negative."))
	return flt(hours, 2)


def _parse_entries(entries):
	if not entries:
		return []
	if isinstance(entries, str):
		entries = json.loads(entries)
	if not isinstance(entries, list):
		frappe.throw(_("The hours could not be read."))
	return entries


def _month_bounds(year, month):
	year = cint(year)
	month = cint(month)
	if month < 1 or month > 12 or year < 2000 or year > 2100:
		frappe.throw(_("Choose a month."))
	last = calendar.monthrange(year, month)[1]
	return getdate(f"{year}-{month:02d}-01"), getdate(f"{year}-{month:02d}-{last:02d}")


def _month_key(start):
	return f"{start.year:04d}-{start.month:02d}"


def _ensure_setup():
	_ensure_month_field()
	_ensure_activities()
	_hide_rate_fields()


def _ensure_month_field():
	if frappe.db.exists("Custom Field", {"dt": "Timesheet", "fieldname": MONTH_FIELD}):
		return
	frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": "Timesheet",
			"fieldname": MONTH_FIELD,
			"label": "Project Hours Month",
			"fieldtype": "Data",
			"insert_after": "end_date",
			"hidden": 1,
			"read_only": 1,
			"no_copy": 1,
			"module": "HR Addon",
		}
	).insert(ignore_permissions=True)
	frappe.clear_cache(doctype="Timesheet")


def _ensure_activities():
	for name, _label in ACTIVITIES:
		if frappe.db.exists("Activity Type", name):
			continue
		frappe.get_doc(
			{
				"doctype": "Activity Type",
				"activity_type": name,
				"costing_rate": 0,
				"billing_rate": 0,
			}
		).insert(ignore_permissions=True)


def _hide_rate_fields():
	for doctype, fieldname in RATE_FIELDS:
		exists = frappe.db.exists(
			"Property Setter",
			{"doc_type": doctype, "field_name": fieldname, "property": "permlevel"},
		)
		if exists:
			continue
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		make_property_setter(
			doctype,
			fieldname,
			"permlevel",
			"1",
			"Int",
			validate_fields_for_doctype=False,
		)
