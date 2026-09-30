# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

"""Employee-facing hours and check-in corrections for the HR app.

Workday documents stay limited to HR roles. These methods always use the
Employee linked to the logged-in user.
"""

import calendar
from datetime import datetime, time, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, getdate, now_datetime

from hr_addon.hr_addon.doctype.workday.workday import get_employee_default_work_hour

ATTENTION_STATUSES = {"Missing Checkin", "Absent", "Missing"}
# A second punch this close to the previous one is a double tap, not a new stretch of work.
CLOSE_GAP = timedelta(minutes=20)
# A punch this close to the expected end of the day is treated as the checkout.
LOOKS_LIKE_END = timedelta(minutes=90)
# On-site mechanisms credit first-in to last-out, minus the break that applies.
ON_SITE_BREAK_MECHANISMS = {
	"Break Hours from Weekly Working Hours if Shorter breaks",
	"Break Hours from Minimum Break Rule",
}


def _current_employee():
	if frappe.session.user in (None, "Guest"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	employee = frappe.db.get_value(
		"Employee",
		{"user_id": frappe.session.user, "status": "Active"},
		"name",
	)
	if not employee:
		frappe.throw(_("No active Employee is linked to your user."), frappe.PermissionError)
	return employee


def _is_holiday(employee, log_date):
	holiday_list = frappe.get_cached_value("Employee", employee, "holiday_list")
	if not holiday_list:
		return False
	return bool(
		frappe.db.exists(
			"Holiday",
			{"parent": holiday_list, "parenttype": "Holiday List", "holiday_date": log_date},
		)
	)


def _month_bounds(year, month):
	year = int(year)
	month = int(month)
	if month < 1 or month > 12:
		frappe.throw(_("Month must be between 1 and 12."))
	start = getdate(f"{year}-{month:02d}-01")
	end = getdate(f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}")
	return start, end


def summarize_hours(rows, today):
	"""Sum actual and target hours for the month of `today` and the calendar year."""
	today = getdate(today)
	month_actual = month_target = year_actual = year_target = 0.0

	for row in rows:
		log_date = getdate(row.log_date)
		if log_date.year != today.year or log_date > today:
			continue

		actual = flt(row.actual_working_hours)
		target = flt(row.target_hours)
		if row.status == "Not Workday":
			target = 0

		year_actual += actual
		year_target += target
		if log_date.month == today.month:
			month_actual += actual
			month_target += target

	return {
		"month_actual": flt(month_actual, 2),
		"month_target": flt(month_target, 2),
		"month_balance": flt(month_actual - month_target, 2),
		"year_actual": flt(year_actual, 2),
		"year_target": flt(year_target, 2),
		"year_balance": flt(year_actual - year_target, 2),
	}


def _workday_rows(employee, start, end):
	return frappe.get_all(
		"Workday",
		filters={
			"employee": employee,
			"log_date": ["between", [start, end]],
		},
		fields=[
			"name",
			"log_date",
			"status",
			"target_hours",
			"actual_working_hours",
			"hours_worked",
			"first_checkin",
			"last_checkout",
		],
		order_by="log_date asc",
		limit_page_length=400,
		ignore_permissions=True,
	)


@frappe.whitelist()
def get_my_hours_summary():
	employee = _current_employee()
	today = getdate()
	year_start = getdate(f"{today.year}-01-01")
	rows = _workday_rows(employee, year_start, today)
	summary = summarize_hours(rows, today)
	summary["month"] = today.month
	summary["year"] = today.year
	summary["missing_days"] = _missing_day_count(employee, getdate(f"{today.year}-{today.month:02d}-01"), today)
	today_view = _days_for_range(employee, today, today)
	summary["today_target"] = flt(today_view[0]["target_hours"], 2) if today_view else 0
	summary["today_actual"], summary["today_open"] = _today_worked_hours(employee, today)
	summary["overtime_enabled"] = bool(
		cint(frappe.db.get_single_value("HR Addon Settings", "enable_overtime_ledger_feature"))
	)
	summary["overtime_balance"] = _overtime_balance(employee) if summary["overtime_enabled"] else None
	return summary


def _today_worked_hours(employee, today):
	"""Hours credited so far today, using the same break rules as a workday.

	An open check-in is closed at the current time so the minimum break for
	that on-site duration is included. Nothing is saved.
	"""
	from hr_addon.hr_addon.doctype.workday.workday import get_employee_checkin

	default = get_employee_default_work_hour(employee, today, skip_workday_if_no_weekly_hours=1)
	return preview_actual_hours(get_employee_checkin(employee, today), default, now_datetime())


def preview_actual_hours(checkins, work_hour, at=None):
	"""Credited hours for these punches. An open check-in is closed at `at`."""
	from hr_addon.hr_addon.doctype.workday.workday import get_workday

	rows = []
	for row in checkins or []:
		rows.append(
			frappe._dict(
				time=row.get("time") if isinstance(row, dict) else row.time,
				log_type=row.get("log_type") if isinstance(row, dict) else row.log_type,
				attendance=(row.get("attendance") if isinstance(row, dict) else getattr(row, "attendance", ""))
				or "",
			)
		)
	if not rows:
		return 0, False

	open_shift = len(rows) % 2 == 1
	if open_shift:
		at = get_datetime(at or now_datetime()).replace(microsecond=0)
		last = get_datetime(rows[-1].time)
		if at < last:
			at = last
		rows.append(frappe._dict(time=at, log_type="OUT", attendance=rows[0].attendance or ""))

	if not work_hour:
		work_hour = frappe._dict(hours=0, break_minutes=0, no_break_hours=0)
	result = get_workday(rows, work_hour, cint(getattr(work_hour, "no_break_hours", 0)))
	return flt(result.get("actual_working_hours"), 2), open_shift


def _overtime_balance(employee):
	"""Latest overtime ledger balance. None when the feature is switched off."""
	if not cint(frappe.db.get_single_value("HR Addon Settings", "enable_overtime_ledger_feature")):
		return None
	row = frappe.get_all(
		"Overtime Ledger Entry",
		filters={"employee": employee, "is_cancelled": 0},
		fields=["balance_after"],
		order_by="posting_datetime desc, creation desc",
		limit=1,
		ignore_permissions=True,
	)
	if not row:
		return 0
	return flt(row[0].balance_after, 2)


def _missing_day_count(employee, start, today):
	return sum(1 for day in _days_for_range(employee, start, today) if day["needs_attention"])


def describe_empty_day(current, today, is_workday, is_holiday, leave_docstatus=None):
	"""Status for a date that has no Workday document yet.

	A past weekday with weekly hours, no holiday and no leave is Missing:
	the employee should have a check-in or a leave application.
	"""
	current = getdate(current)
	today = getdate(today)
	if not is_workday:
		return "Off", False
	if is_holiday:
		return "Holiday", False
	if leave_docstatus == 1:
		return "On Leave", False
	if leave_docstatus == 0:
		return "Pending Leave", False
	if current < today:
		return "Missing", True
	return "Open", False


def _looks_like_illness(leave_type):
	text = (leave_type or "").lower()
	return any(word in text for word in ("sick", "illness", "krank", "maladie", "medical"))


def _leave_rows(employee, start, end):
	return frappe.get_all(
		"Leave Application",
		filters={
			"employee": employee,
			"docstatus": ["<", 2],
			"status": ["not in", ["Rejected", "Cancelled"]],
			"from_date": ["<=", end],
			"to_date": [">=", start],
		},
		fields=[
			"name",
			"leave_type",
			"from_date",
			"to_date",
			"status",
			"docstatus",
			"half_day",
			"half_day_date",
		],
		ignore_permissions=True,
		limit_page_length=200,
	)


def _leave_on(rows, log_date):
	log_date = getdate(log_date)
	for row in rows:
		if getdate(row.from_date) <= log_date <= getdate(row.to_date):
			return row
	return None


def _leave_payload(row):
	if not row:
		return None
	return {
		"name": row.name,
		"leave_type": row.leave_type,
		"status": row.status,
		"docstatus": row.docstatus,
		"illness": _looks_like_illness(row.leave_type),
	}


def _home_office_rows(employee, start, end):
	"""Work From Home attendance requests. docstatus 0 is waiting for approval.

	The request is the source of truth: HR Addon resets the Attendance status to
	Present whenever the Workday is rebuilt.
	"""
	return frappe.get_all(
		"Attendance Request",
		filters={
			"employee": employee,
			"reason": "Work From Home",
			"docstatus": ["<", 2],
			"from_date": ["<=", end],
			"to_date": [">=", start],
		},
		fields=["name", "from_date", "to_date", "docstatus", "half_day", "half_day_date"],
		ignore_permissions=True,
		limit_page_length=200,
	)


def _home_office_payload(row, log_date):
	if not row:
		return None
	half_day = bool(row.half_day and row.half_day_date and getdate(row.half_day_date) == getdate(log_date))
	return {"name": row.name, "docstatus": row.docstatus, "half_day": half_day}


def _blank_day(current, status="Off"):
	return {
		"date": str(current),
		"workday": None,
		"status": status,
		"target_hours": 0,
		"actual_working_hours": 0,
		"hours_worked": 0,
		"first_checkin": "",
		"last_checkout": "",
		"needs_attention": False,
		"leave": None,
		"home_office": None,
		"schedule": "none",
	}


def schedule_kind(default):
	"""How Weekly Working Hours treat a weekday.

	work: a row with hours, so the day is expected.
	free: a row with 0 hours (usually Saturday and Sunday). Punches still become
	a Workday with 0 target, so they count as overtime.
	none: no row. HR Addon creates no Workday, so punches are not counted.
	"""
	if not default:
		return "none"
	return "work" if flt(default.hours) > 0 else "free"


def _days_for_range(employee, start, end):
	today = getdate()
	joining_date, relieving_date = frappe.get_cached_value(
		"Employee", employee, ["date_of_joining", "relieving_date"]
	)
	workdays = {getdate(row.log_date): row for row in _workday_rows(employee, start, end)}
	leaves = _leave_rows(employee, start, end)
	home_office = _home_office_rows(employee, start, end)
	days = []
	current = getdate(start)
	last = getdate(end)

	while current <= last:
		outside_employment = (joining_date and current < getdate(joining_date)) or (
			relieving_date and current > getdate(relieving_date)
		)
		if outside_employment:
			days.append(_blank_day(current))
			current = add_days(current, 1)
			continue

		default = get_employee_default_work_hour(employee, current, skip_workday_if_no_weekly_hours=1)
		holiday = _is_holiday(employee, current)
		leave = _leave_on(leaves, current)
		row = workdays.get(current)
		schedule = schedule_kind(default)
		expected = schedule == "work"
		target = flt(default.hours, 2) if default else 0
		if holiday and default and default.set_target_hours_to_zero_when_date_is_holiday:
			target = 0

		if row:
			status = row.status or ""
			item = {
				"date": str(current),
				"workday": row.name,
				"status": status,
				"target_hours": flt(row.target_hours, 2),
				"actual_working_hours": flt(row.actual_working_hours, 2),
				"hours_worked": flt(row.hours_worked, 2),
				"first_checkin": str(row.first_checkin or ""),
				"last_checkout": str(row.last_checkout or ""),
			}
			item["needs_attention"] = current < today and status in ATTENTION_STATUSES
			if holiday and status in ("Holiday", "Not Workday"):
				item["needs_attention"] = False
			# A leave application resolves a day that never got a punch, even
			# when the Workday document was never rebuilt.
			if leave and not item["first_checkin"] and status in ("Missing", "Absent", ""):
				item["status"], item["needs_attention"] = describe_empty_day(
					current, today, expected, holiday, leave.docstatus
				)
		else:
			status, needs_attention = describe_empty_day(
				current, today, expected, holiday, leave.docstatus if leave else None
			)
			item = {
				"date": str(current),
				"workday": None,
				"status": status,
				"target_hours": 0 if status in ("Off", "Holiday") else target,
				"actual_working_hours": 0,
				"hours_worked": 0,
				"first_checkin": "",
				"last_checkout": "",
				"needs_attention": needs_attention,
			}

		item["schedule"] = schedule
		item["leave"] = _leave_payload(leave)
		# A request spanning a weekend only marks the days that were worked or expected.
		home = _leave_on(home_office, current)
		worked = bool(item["first_checkin"])
		item["home_office"] = (
			_home_office_payload(home, current) if home and (expected or worked) and not holiday else None
		)
		days.append(item)
		current = add_days(current, 1)

	return days


@frappe.whitelist()
def get_my_workdays(year=None, month=None):
	employee = _current_employee()
	today = getdate()
	if not year or not month:
		year, month = today.year, today.month
	start, end = _month_bounds(year, month)
	days = _days_for_range(employee, start, end)
	return {
		"year": int(year),
		"month": int(month),
		"days": days,
		"missing_days": sum(1 for day in days if day["needs_attention"]),
	}


def suggest_missing_punch(
	checkins, date, target_hours, break_minutes, mechanism, usual_start, now, no_break_hours=False
):
	"""Propose one punch that repairs a broken check-in sequence.

	Workdays pair punches in order (IN, OUT, IN, OUT). The proposal is the
	single next step: add the missing punch, correct a mistyped one, or drop
	a duplicate. Times stay on the same calendar day, because a workday only
	reads that day's check-ins. A time still in the future is not proposed.
	"""
	date = getdate(date)
	now = get_datetime(now).replace(microsecond=0)
	target_hours = flt(target_hours)
	break_minutes = cint(break_minutes or 0)
	no_break_hours = bool(no_break_hours)
	punches = _normalize_punches(checkins)

	if not punches:
		if target_hours <= 0:
			return None
		when = datetime.combine(date, usual_start)
		return _accept(
			_proposal(
				"add",
				"IN",
				when,
				_("Nothing was recorded. A check-in at {0} matches your usual start.").format(
					when.strftime("%H:%M")
				),
			),
			punches,
			date,
			now,
		)

	mismatch = _first_mismatch(punches)
	if mismatch is not None:
		return _suggestion_for_mismatch(
			punches,
			mismatch,
			date,
			target_hours,
			break_minutes,
			mechanism,
			usual_start,
			now,
			no_break_hours,
		)

	if len(punches) % 2 == 1:
		return _suggestion_for_open_checkin(
			punches, date, target_hours, break_minutes, mechanism, now, no_break_hours
		)

	if len(punches) == 2 and punches[1]["time"] - punches[0]["time"] <= CLOSE_GAP:
		return _checkout_at_target(
			punches,
			punches[1],
			punches[0]["time"],
			date,
			target_hours,
			break_minutes,
			mechanism,
			now,
			_(
				"Check-out is only a few minutes after check-in. {0} matches your {1} h target."
			),
			0,
			no_break_hours,
		)

	return None


def _suggestion_for_mismatch(
	punches, index, date, target_hours, break_minutes, mechanism, usual_start, now, no_break_hours=False
):
	punch = punches[index]
	if index == 0:
		when = _checkin_before(
			date, punch["time"], target_hours, break_minutes, mechanism, usual_start, no_break_hours
		)
		return _accept(
			_proposal(
				"add",
				"IN",
				when,
				_("The day starts with a check-out. A check-in at {0} comes before it.").format(
					when.strftime("%H:%M")
				),
			),
			punches,
			date,
			now,
			earlier_than=punch["time"],
		)

	previous = punches[index - 1]
	gap = punch["time"] - previous["time"]

	if punch["log_type"] == "IN":
		if gap <= CLOSE_GAP and index + 1 < len(punches):
			return {
				"action": "remove",
				"name": punch["name"],
				"log_type": None,
				"time": None,
				"reason": _(
					"Two check-ins a few minutes apart, and a later punch already follows them. "
					"The second check-in looks like a duplicate."
				),
			}
		if gap <= CLOSE_GAP:
			return _checkout_at_target(
				punches,
				punch,
				previous["time"],
				date,
				target_hours,
				break_minutes,
				mechanism,
				now,
				_(
					"Two check-ins a few minutes apart. The later one is changed into a check-out at {0}, "
					"which covers your {1} h target."
				),
				index - 1,
				no_break_hours,
			)

		expected = _expected_out(
			previous["time"], punches, index - 1, target_hours, break_minutes, mechanism
		)
		if gap >= timedelta(hours=2) and abs(punch["time"] - expected) <= LOOKS_LIKE_END:
			return _accept(
				_proposal(
					"correct",
					"OUT",
					punch["time"],
					_("This punch is marked IN, but {0} looks like the check-out.").format(
						punch["time"].strftime("%H:%M")
					),
					name=punch["name"],
				),
				punches,
				date,
				now,
				later_than=previous["time"],
			)

		when = _just_before(punches, previous["time"], punch["time"], break_minutes, mechanism)
		return _accept(
			_proposal(
				"add",
				"OUT",
				when,
				_(
					"Two check-ins with no check-out between them. A check-out at {0}, "
					"just before the next check-in, closes the first part of the day."
				).format(when.strftime("%H:%M")),
			),
			punches,
			date,
			now,
			later_than=previous["time"],
			earlier_than=punch["time"],
		)

	if gap <= CLOSE_GAP:
		return {
			"action": "remove",
			"name": punch["name"],
			"log_type": None,
			"time": None,
			"reason": _("Two check-outs a few minutes apart. The later one looks like a duplicate."),
		}

	when = _return_checkin(punches, index, target_hours, break_minutes, mechanism, no_break_hours)
	return _accept(
		_proposal(
			"add",
			"IN",
			when,
			_(
				"Two check-outs with no check-in between them. "
				"A check-in at {0} starts the second part of the day."
			).format(when.strftime("%H:%M")),
		),
		punches,
		date,
		now,
		later_than=previous["time"],
		earlier_than=punch["time"],
	)


def _suggestion_for_open_checkin(
	punches, date, target_hours, break_minutes, mechanism, now, no_break_hours=False
):
	last = punches[-1]
	if (
		len(punches) >= 2
		and punches[-2]["log_type"] == "OUT"
		and last["time"] - punches[-2]["time"] <= CLOSE_GAP
	):
		return {
			"action": "remove",
			"name": last["name"],
			"log_type": None,
			"time": None,
			"reason": _(
				"You checked in again right after checking out. That check-in looks like an extra tap."
			),
		}

	open_index = len(punches) - 1
	expected = _expected_out(
		last["time"], punches, open_index, target_hours, break_minutes, mechanism, no_break_hours
	)
	completed = _completed_hours(punches, open_index)
	remaining = max(flt(target_hours) - _credited_hours(punches, open_index, break_minutes, mechanism, no_break_hours), 0)
	if completed <= 0:
		reason = _(
			"You checked in at {0} and never checked out. A check-out at {1} matches your {2} h target."
		).format(last["time"].strftime("%H:%M"), expected.strftime("%H:%M"), _hours_label(target_hours))
	elif remaining <= 0:
		reason = _(
			"You checked in again at {0} after the day was already covered. A check-out at {1} closes it."
		).format(last["time"].strftime("%H:%M"), expected.strftime("%H:%M"))
	else:
		reason = _(
			"You checked in at {0} and never checked out. A check-out at {1} covers the remaining {2} h."
		).format(last["time"].strftime("%H:%M"), expected.strftime("%H:%M"), _hours_label(remaining))

	return _accept(
		_proposal("add", "OUT", expected, reason),
		punches,
		date,
		now,
		later_than=last["time"],
	)


def _checkout_at_target(
	punches,
	punch,
	open_time,
	date,
	target_hours,
	break_minutes,
	mechanism,
	now,
	reason_template,
	open_index=0,
	no_break_hours=False,
):
	expected = _expected_out(
		open_time, punches, open_index, target_hours, break_minutes, mechanism, no_break_hours
	)
	reason = reason_template.format(expected.strftime("%H:%M"), _hours_label(target_hours))
	return _accept(
		_proposal("correct", "OUT", expected, reason, name=punch["name"]),
		punches,
		date,
		now,
		later_than=open_time,
	)


def _proposal(action, log_type, when, reason, name=None):
	return {
		"action": action,
		"name": name,
		"log_type": log_type,
		"time": get_datetime(when).replace(microsecond=0),
		"reason": reason,
	}


def _accept(suggestion, punches, date, now, later_than=None, earlier_than=None):
	if not suggestion:
		return None
	when = _place(date, suggestion["time"], later_than=later_than, earlier_than=earlier_than)
	if when is None or when > now:
		return None
	when = _free_minute(when, punches, suggestion["log_type"], suggestion.get("name"), date, now)
	if when is None or when > now:
		return None
	suggestion["time"] = when
	suggestion["reason"] = _reason_with_time(suggestion["reason"], when)
	return suggestion


def _reason_with_time(reason, when):
	"""Keep the sentence on the punch time that was actually placed.

	The proposed time is always the last clock time in the sentence. Placement
	can still move it (same-day limit, or one minute off an existing punch).
	"""
	import re

	placed = when.strftime("%H:%M")
	matches = list(re.finditer(r"\d{2}:\d{2}", reason))
	if not matches or matches[-1].group(0) == placed:
		return reason
	last = matches[-1]
	return reason[: last.start()] + placed + reason[last.end() :]


def _place(date, when, later_than=None, earlier_than=None):
	when = get_datetime(when).replace(second=0, microsecond=0)
	start = datetime.combine(date, time(0, 0))
	end = datetime.combine(date, time(23, 59))
	if later_than is not None and when <= get_datetime(later_than):
		when = get_datetime(later_than).replace(second=0, microsecond=0) + timedelta(minutes=1)
	if earlier_than is not None and when >= get_datetime(earlier_than):
		when = get_datetime(earlier_than).replace(second=0, microsecond=0) - timedelta(minutes=1)
	if when < start:
		when = start
	if when > end:
		when = end
	if later_than is not None and when <= get_datetime(later_than):
		return None
	if earlier_than is not None and when >= get_datetime(earlier_than):
		return None
	return when


def _free_minute(when, punches, log_type, own_name, date, now):
	for _unused in range(5):
		clash = any(
			punch["name"] != own_name
			and punch["log_type"] == log_type
			and punch["time"].replace(second=0, microsecond=0) == when
			for punch in punches
		)
		if not clash:
			return when
		when = when + timedelta(minutes=1)
		if getdate(when) != date or when > now:
			return None
	return None


def _normalize_punches(checkins):
	rows = []
	for row in checkins or []:
		log_type = (row.get("log_type") or "").strip().upper()
		rows.append(
			{
				"name": row.get("name"),
				"log_type": log_type,
				"time": get_datetime(row.get("time")).replace(microsecond=0),
			}
		)
	rows.sort(key=lambda row: (row["time"], row["name"] or ""))
	for index, row in enumerate(rows):
		if row["log_type"] not in ("IN", "OUT"):
			row["log_type"] = "IN" if index % 2 == 0 else "OUT"
	return rows


def _first_mismatch(punches):
	for index, punch in enumerate(punches):
		expected = "IN" if index % 2 == 0 else "OUT"
		if punch["log_type"] != expected:
			return index
	return None


def _span_hours(target_hours, break_minutes, mechanism, no_break_hours=False):
	return _open_segment_minutes(target_hours, break_minutes, mechanism, 0, 0, 0, no_break_hours) / 60


def _expected_out(open_time, punches, open_index, target_hours, break_minutes, mechanism, no_break_hours=False):
	"""Checkout that makes the credited hours reach the target.

	The break is the one get_workday would subtract: the real gaps, the
	weekly break, or the mandatory minimum for the on-site duration.
	"""
	open_time = get_datetime(open_time)
	completed = _completed_hours(punches, open_index)
	breaks = _breaks_before(punches, open_index)
	minutes = _open_segment_minutes(
		target_hours,
		break_minutes,
		mechanism,
		completed,
		breaks,
		completed + breaks,
		no_break_hours,
	)
	if minutes <= 0:
		return open_time + timedelta(minutes=1)
	return open_time + timedelta(minutes=minutes)


def _completed_hours(punches, before_index):
	hours = 0.0
	index = 0
	while index + 1 < before_index:
		hours += (punches[index + 1]["time"] - punches[index]["time"]).total_seconds() / 3600
		index += 2
	return hours


def _breaks_before(punches, before_index):
	"""Gaps between a check-out and the next check-in, up to and including `before_index`."""
	hours = 0.0
	index = 1
	while index + 1 <= before_index:
		hours += (punches[index + 1]["time"] - punches[index]["time"]).total_seconds() / 3600
		index += 2
	return hours


def _checkin_before(date, out_time, target_hours, break_minutes, mechanism, usual_start, no_break_hours=False):
	usual = datetime.combine(date, usual_start)
	if usual < get_datetime(out_time):
		return usual
	return get_datetime(out_time) - timedelta(
		minutes=_open_segment_minutes(target_hours, break_minutes, mechanism, 0, 0, 0, no_break_hours)
	)


def _just_before(punches, previous_time, next_time, break_minutes, mechanism):
	gap = _break_gap(punches, next_time, break_minutes, mechanism)
	when = get_datetime(next_time) - gap
	if when <= get_datetime(previous_time):
		when = get_datetime(previous_time) + timedelta(minutes=1)
	if when >= get_datetime(next_time):
		when = get_datetime(next_time) - timedelta(minutes=1)
	return when


def _return_checkin(punches, index, target_hours, break_minutes, mechanism, no_break_hours=False):
	"""Check-in between two check-outs so the closed day credits the target."""
	previous = punches[index - 1]["time"]
	later = punches[index]["time"]
	earliest = previous + timedelta(minutes=1)
	latest = later - timedelta(minutes=1)
	if earliest > latest:
		return latest

	best = None
	minute = latest
	while minute >= earliest:
		if _actual_with_return(punches, index, minute, break_minutes, mechanism, no_break_hours) + 1e-6 >= flt(
			target_hours
		):
			best = minute
			break
		minute -= timedelta(minutes=1)
	return best or earliest


def _break_gap(punches, next_time, break_minutes, mechanism):
	"""Minutes to leave before the next punch, matching the break that would be deducted."""
	scheduled = cint(break_minutes or 0)
	if mechanism == "Break Hours from Minimum Break Rule" and punches:
		on_site = (get_datetime(next_time) - punches[0]["time"]).total_seconds() / 3600
		minutes = int(round(_mandatory_break_hours(on_site) * 60))
		return timedelta(minutes=max(minutes, 1))
	return timedelta(minutes=scheduled or 30)


def _open_segment_minutes(
	target_hours, break_minutes, mechanism, completed, breaks_taken, prior_on_site, no_break_hours=False
):
	"""Minutes still to work so credited hours reach the target."""
	target = flt(target_hours)

	def credited(minutes):
		segment = minutes / 60
		return _actual_hours(
			completed + segment,
			prior_on_site + segment,
			breaks_taken,
			break_minutes,
			mechanism,
			no_break_hours,
		)

	if credited(0) >= target - 1e-6:
		return 0
	lo, hi = 0, 16 * 60
	best = hi
	while lo <= hi:
		mid = (lo + hi) // 2
		if credited(mid) >= target - 1e-6:
			best = mid
			hi = mid - 1
		else:
			lo = mid + 1
	return best


def _credited_hours(punches, open_index, break_minutes, mechanism, no_break_hours):
	completed = _completed_hours(punches, open_index)
	breaks = _breaks_before(punches, open_index)
	return _actual_hours(completed, completed + breaks, breaks, break_minutes, mechanism, no_break_hours)


def _actual_with_return(punches, index, checkin_time, break_minutes, mechanism, no_break_hours):
	previous = punches[index - 1]["time"]
	later = punches[index]["time"]
	first = punches[0]["time"]
	completed = _completed_hours(punches, index)
	earlier_breaks = _breaks_before(punches, index - 1)
	gap = max((checkin_time - previous).total_seconds() / 3600, 0)
	hours_worked = completed + max((later - checkin_time).total_seconds() / 3600, 0)
	on_site = max((later - first).total_seconds() / 3600, 0)
	return _actual_hours(
		hours_worked, on_site, earlier_breaks + gap, break_minutes, mechanism, no_break_hours
	)


def _actual_hours(hours_worked, on_site, breaks_taken, break_minutes, mechanism, no_break_hours=False):
	"""Same credited hours as calculate_actual_working_hours for a closed day."""
	hours_worked = flt(hours_worked)
	on_site = flt(on_site)
	if no_break_hours and hours_worked < 6:
		return hours_worked
	resolved = _resolved_break(on_site, breaks_taken, break_minutes, mechanism)
	if mechanism in ON_SITE_BREAK_MECHANISMS and on_site > 0:
		return flt(on_site - resolved)
	return flt(hours_worked - resolved)


def _resolved_break(on_site, breaks_taken, break_minutes, mechanism):
	"""Break hours get_workday stores for this mechanism."""
	breaks_taken = flt(breaks_taken)
	scheduled = flt(break_minutes) / 60
	if mechanism == "Break Hours from Weekly Working Hours":
		return scheduled
	if mechanism == "Break Hours from Weekly Working Hours if Shorter breaks":
		return scheduled if breaks_taken <= scheduled else breaks_taken
	if mechanism == "Break Hours from Minimum Break Rule":
		mandatory = _mandatory_break_hours(on_site)
		return mandatory if breaks_taken <= mandatory else breaks_taken
	return breaks_taken


def _mandatory_break_hours(on_site_hours):
	from hr_addon.hr_addon.doctype.workday.workday import get_mandatory_break_hours_from_settings

	return flt(get_mandatory_break_hours_from_settings(on_site_hours))


def _as_timedelta(hours):
	return timedelta(seconds=int(round(flt(hours) * 3600)))


def _hours_label(hours):
	return f"{flt(hours, 2):.2f}"


def _usual_start_time(employee, log_date):
	"""Median minute of this employee's first check-in, preferring the same weekday."""
	log_date = getdate(log_date)
	rows = frappe.get_all(
		"Employee Checkin",
		filters={"employee": employee, "log_type": "IN", "time": ["<", f"{log_date} 00:00:00"]},
		fields=["time"],
		order_by="time desc",
		limit_page_length=60,
		ignore_permissions=True,
	)
	first_by_day = {}
	for row in rows:
		day = getdate(row.time)
		current = first_by_day.get(day)
		stamp = get_datetime(row.time)
		if current is None or stamp < current:
			first_by_day[day] = stamp
	if not first_by_day:
		return time(8, 0)

	same_weekday = [stamp for day, stamp in first_by_day.items() if day.weekday() == log_date.weekday()]
	pool = same_weekday or list(first_by_day.values())
	minutes = sorted(stamp.hour * 60 + stamp.minute for stamp in pool)
	mid = minutes[len(minutes) // 2]
	return time(mid // 60, mid % 60)


def _typical_day_hours(default):
	if default and default.name:
		hours = frappe.get_all(
			"Daily Hours Detail",
			filters={"parent": default.name, "parenttype": "Weekly Working Hours", "hours": [">", 0]},
			pluck="hours",
			ignore_permissions=True,
		)
		if hours:
			return max(flt(value) for value in hours)
	return 8


def _suggestion_for_day(employee, log_date, checkins):
	default = get_employee_default_work_hour(employee, log_date, skip_workday_if_no_weekly_hours=1)
	target = flt(default.hours) if default else 0
	break_minutes = cint(default.break_minutes or 0) if default else 0
	free_day = target <= 0
	if free_day:
		if not checkins:
			return None
		# A free day has no target. Size an open check-in by a normal working day.
		target = _typical_day_hours(default)
		break_minutes = 0

	suggestion = suggest_missing_punch(
		checkins,
		date=log_date,
		target_hours=target,
		break_minutes=break_minutes,
		mechanism=frappe.db.get_single_value(
			"HR Addon Settings", "workday_break_calculation_mechanism"
		),
		usual_start=_usual_start_time(employee, log_date),
		now=now_datetime(),
		no_break_hours=bool(default and default.no_break_hours),
	)
	if suggestion and suggestion.get("time"):
		suggestion["time"] = get_datetime(suggestion["time"]).strftime("%Y-%m-%d %H:%M:%S")
	if suggestion and free_day:
		suggestion["reason"] = suggestion["reason"].replace(
			_("h target"), _("h of a normal working day")
		)
	return suggestion


@frappe.whitelist()
def get_my_day(date):
	employee = _current_employee()
	log_date = getdate(date)
	days = _days_for_range(employee, log_date, log_date)
	day = days[0] if days else {
		"date": str(log_date),
		"workday": None,
		"status": "Open",
		"target_hours": 0,
		"actual_working_hours": 0,
		"hours_worked": 0,
		"first_checkin": "",
		"last_checkout": "",
		"needs_attention": False,
		"schedule": "none",
	}
	from hr_addon.hr_addon.doctype.workday.workday import get_employee_checkin

	checkins = []
	for checkin in get_employee_checkin(employee, log_date):
		checkins.append(
			{
				"name": checkin.name,
				"log_type": checkin.log_type or "",
				"time": str(checkin.time),
				"attendance": checkin.attendance or "",
			}
		)
	day["checkins"] = checkins
	day["suggestion"] = _suggestion_for_day(employee, log_date, checkins)
	day["completely_missing"] = day.get("status") == "Missing" and not checkins
	return day


def _validated_log(log_type, time):
	log_type = (log_type or "").strip().upper()
	if log_type not in ("IN", "OUT"):
		frappe.throw(_("Log Type must be IN or OUT."))

	timestamp = get_datetime(time).replace(microsecond=0)
	if timestamp > now_datetime().replace(microsecond=0):
		frappe.throw(_("Check-in time cannot be in the future."))
	return log_type, timestamp


def _fill_coordinates(doc, employee):
	"""Reuse the last known coordinates when geolocation is required.

	A forgotten punch is entered after the fact, so the phone is not at the
	workplace. Live check-in from the home screen still sends a fresh location.
	"""
	if doc.latitude or doc.longitude:
		return
	if not frappe.db.get_single_value("HR Settings", "allow_geolocation_tracking"):
		return
	last = frappe.db.get_value(
		"Employee Checkin",
		{"employee": employee, "latitude": ["is", "set"]},
		["latitude", "longitude"],
		order_by="time desc",
		as_dict=True,
	)
	if not last:
		return
	doc.latitude = last.latitude
	doc.longitude = last.longitude


@frappe.whitelist()
def save_my_checkin(log_type, time, name=None):
	"""Create or correct the current employee's check-in, then rebuild that Workday.

	The Time field on Employee Checkin is permlevel 1, so an Employee cannot
	change it through the standard API. This method writes it only for the
	logged-in employee. A linked Attendance is cleared before a time change so
	Frappe's own lock does not block the correction; the Workday sync links it again.
	"""
	employee = _current_employee()
	log_type, timestamp = _validated_log(log_type, time)
	joining_date = frappe.db.get_value("Employee", employee, "date_of_joining")
	if joining_date and getdate(timestamp) < getdate(joining_date):
		frappe.throw(_("Check-in time is before your joining date."))

	if name:
		doc = frappe.get_doc("Employee Checkin", name)
		if doc.employee != employee:
			frappe.throw(_("Not permitted"), frappe.PermissionError)
		time_changed = get_datetime(doc.time).replace(microsecond=0) != timestamp
		if doc.attendance and time_changed:
			doc.db_set("attendance", None, update_modified=False)
			doc.attendance = None
		previous_date = getdate(doc.time)
		doc.log_type = log_type
		doc.time = timestamp
		_fill_coordinates(doc, employee)
		doc.flags.ignore_permissions = True
		doc.save()
	else:
		previous_date = None
		doc = frappe.get_doc(
			{
				"doctype": "Employee Checkin",
				"employee": employee,
				"log_type": log_type,
				"time": timestamp,
			}
		)
		_fill_coordinates(doc, employee)
		doc.flags.ignore_permissions = True
		doc.insert()

	from hr_addon.events.employee_checkin import sync_workday

	sync_workday(employee, getdate(timestamp))
	if previous_date and previous_date != getdate(timestamp):
		sync_workday(employee, previous_date)

	return {
		"name": doc.name,
		"log_type": doc.log_type,
		"time": str(doc.time),
	}


@frappe.whitelist()
def delete_my_checkin(name):
	"""Remove one of the current employee's punches and rebuild that workday.

	Used when a second tap a few minutes later is a duplicate. The workday's
	own link to the punch is cleared first, otherwise Frappe blocks the delete.
	"""
	from hr_addon.events.employee_checkin import SYNC_FLAG, sync_workday

	employee = _current_employee()
	if not name or not frappe.db.exists("Employee Checkin", name):
		frappe.throw(_("Check-in not found."))

	doc = frappe.get_doc("Employee Checkin", name)
	if doc.employee != employee:
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	log_date = getdate(doc.time)
	setattr(frappe.flags, SYNC_FLAG, True)
	try:
		if doc.attendance:
			doc.db_set("attendance", None, update_modified=False)
		frappe.db.delete("Employee Checkins", {"employee_checkin": doc.name})
		doc.flags.ignore_permissions = True
		doc.delete(ignore_permissions=True)
		sync_workday(employee, log_date)
	finally:
		setattr(frappe.flags, SYNC_FLAG, False)

	return {"deleted": name}
