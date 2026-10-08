# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

"""Leave and home office requests of the logged-in employee, for the HR app calendar.

Requests are the normal Frappe HR documents: a Leave Application, or an
Attendance Request with reason "Work From Home". They are saved as drafts and
approved the usual way. Only drafts can be changed or withdrawn here.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from hr_addon.api.employee_app import _current_employee, _looks_like_illness

KINDS = {"leave": "Leave Application", "home_office": "Attendance Request"}


def _doctype(kind):
	if kind not in KINDS:
		frappe.throw(_("Unknown request type {0}").format(kind))
	return KINDS[kind]


def _kind_of(doctype):
	return next(kind for kind, name in KINDS.items() if name == doctype)


def _own_draft(doctype, name, employee):
	doc = frappe.get_doc(doctype, name)
	if doc.employee != employee:
		frappe.throw(_("You can only change your own requests."), frappe.PermissionError)
	if doc.docstatus != 0 or (doctype == "Leave Application" and doc.status != "Open"):
		frappe.throw(_("This request was already decided. Ask HR to change it."))
	return doc


def request_payload(doc):
	kind = _kind_of(doc.doctype)
	payload = {
		"kind": kind,
		"doctype": doc.doctype,
		"name": doc.name,
		"from_date": str(doc.from_date),
		"to_date": str(doc.to_date),
		"half_day": cint(doc.half_day),
		"half_day_date": str(doc.half_day_date) if doc.half_day_date else None,
		"docstatus": doc.docstatus,
		"editable": doc.docstatus == 0,
	}
	if kind == "leave":
		payload.update(
			{
				"leave_type": doc.leave_type,
				"illness": _looks_like_illness(doc.leave_type),
				"status": doc.status,
				"note": doc.description or "",
				"total_days": flt(doc.total_leave_days),
				"editable": doc.docstatus == 0 and doc.status == "Open",
			}
		)
	else:
		payload.update({"status": "Approved" if doc.docstatus == 1 else "Open", "note": doc.explanation or ""})
	return payload


@frappe.whitelist()
def get_my_request(kind, name):
	employee = _current_employee()
	doc = frappe.get_doc(_doctype(kind), name)
	if doc.employee != employee:
		frappe.throw(_("You can only open your own requests."), frappe.PermissionError)
	return request_payload(doc)


def _waiting_days_by_type(employee, exclude=None):
	"""Leave days in requests that are still waiting for approval, per leave type.

	Counted the same way as a saved application: 0-hour days and holidays are not included,
	even when an older request still stores them in total_leave_days.
	"""
	from hr_addon.hr_addon.leave_days import charged_leave_days

	rows = frappe.get_all(
		"Leave Application",
		filters={"employee": employee, "docstatus": 0, "status": "Open"},
		fields=["name", "leave_type", "from_date", "to_date", "half_day", "half_day_date"],
		ignore_permissions=True,
		limit_page_length=200,
	)
	totals = {}
	for row in rows:
		if exclude and row.name == exclude:
			continue
		days = charged_leave_days(
			employee, row.leave_type, row.from_date, row.to_date, row.half_day, row.half_day_date
		)
		totals[row.leave_type] = totals.get(row.leave_type, 0.0) + days
	return totals


@frappe.whitelist()
def get_request_options(from_date, exclude=None):
	"""Leave types with balance on `from_date`, illness types first.

	`balance` is what Frappe HR reports (allocation minus approved leave).
	`waiting` is days in requests not yet approved, and `available` is what's
	left once those are counted too. `exclude` is the request being edited, so
	its own days are not counted twice.
	"""
	from hrms.hr.doctype.leave_application.leave_application import get_leave_details

	employee = _current_employee()
	try:
		details = get_leave_details(employee, getdate(from_date))
	except Exception:
		details = {}

	waiting = _waiting_days_by_type(employee, exclude)
	options = []
	for leave_type, row in (details.get("leave_allocation") or {}).items():
		balance = flt(row.get("remaining_leaves"))
		pending = flt(waiting.get(leave_type))
		options.append(
			{
				"leave_type": leave_type,
				"illness": _looks_like_illness(leave_type),
				"balance": balance,
				"waiting": pending,
				"available": flt(balance - pending),
				"without_pay": False,
			}
		)
	for leave_type in details.get("lwps") or []:
		if not any(option["leave_type"] == leave_type for option in options):
			options.append(
				{
					"leave_type": leave_type,
					"illness": _looks_like_illness(leave_type),
					"balance": None,
					"waiting": flt(waiting.get(leave_type)),
					"available": None,
					"without_pay": True,
				}
			)
	options.sort(key=lambda row: (not row["illness"], row["without_pay"], row["leave_type"]))
	return {"leave_types": options}


@frappe.whitelist()
def preview_leave_days(leave_type, from_date, to_date=None, half_day=0, half_day_date=None):
	"""How many leave days will be deducted. 0-hour days and holidays count as nothing."""
	from hr_addon.hr_addon.leave_days import charged_leave_days

	employee = _current_employee()
	to_date = to_date or from_date
	if getdate(to_date) < getdate(from_date):
		return {"days": 0}
	days = charged_leave_days(
		employee,
		leave_type,
		getdate(from_date),
		getdate(to_date),
		cint(half_day),
		getdate(half_day_date) if cint(half_day) and half_day_date else None,
	)
	return {"days": flt(days)}


@frappe.whitelist()
def save_my_request(kind, from_date, to_date=None, leave_type=None, half_day=0, half_day_date=None, note=None, name=None):
	employee = _current_employee()
	doctype = _doctype(kind)
	from_date = getdate(from_date)
	to_date = getdate(to_date or from_date)
	half_day = cint(half_day)

	if name:
		doc = _own_draft(doctype, name, employee)
	else:
		doc = frappe.new_doc(doctype)
		doc.employee = employee
		doc.company = frappe.db.get_value("Employee", employee, "company")

	doc.from_date = from_date
	doc.to_date = to_date
	doc.half_day = half_day
	doc.half_day_date = getdate(half_day_date or from_date) if half_day else None

	if kind == "leave":
		if not leave_type:
			frappe.throw(_("Choose a leave type."))
		from hrms.hr.doctype.leave_application.leave_application import get_leave_approver

		doc.leave_type = leave_type
		doc.description = note or ""
		if not doc.leave_approver:
			doc.leave_approver = get_leave_approver(employee)
	else:
		doc.reason = "Work From Home"
		doc.explanation = note or ""

	doc.save()
	return request_payload(doc)


@frappe.whitelist()
def delete_my_request(kind, name):
	"""Withdraw a request that is still waiting for approval.

	Employees have no delete right on Leave Application, so the ownership and
	draft checks above stand in for it.
	"""
	employee = _current_employee()
	doctype = _doctype(kind)
	_own_draft(doctype, name, employee)
	frappe.delete_doc(doctype, name, ignore_permissions=True)
	return {"deleted": name}
