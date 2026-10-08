import frappe
from frappe.utils import getdate

SYNC_FLAG = "in_workday_checkin_sync"


def is_checkin_sync_enabled():
	return bool(
		frappe.db.get_single_value("HR Addon Settings", "update_workday_on_employee_checkin")
	)


def enqueue_workday_sync(doc, method=None):
	"""Refresh the Workday of a checkin's date whenever that checkin changes.

	Attendance created from a Workday writes itself back into the Employee Checkin, so a
	sync triggered from within our own save would recurse. SYNC_FLAG breaks that cycle.
	"""
	if getattr(frappe.flags, SYNC_FLAG, False):
		return

	if not doc.employee or not doc.time:
		return

	if not is_checkin_sync_enabled():
		return

	dates = {getdate(doc.time)}

	# A corrected timestamp can move the checkin to another day; both days need a refresh.
	previous = doc.get_doc_before_save() if method != "on_trash" else None
	if previous and previous.time:
		dates.add(getdate(previous.time))

	for log_date in dates:
		frappe.enqueue(
			"hr_addon.events.employee_checkin.sync_workday",
			queue="short",
			enqueue_after_commit=True,
			job_id=f"workday-checkin-sync::{doc.employee}::{log_date}",
			deduplicate=True,
			employee=doc.employee,
			log_date=str(log_date),
		)


def sync_workday(employee, log_date):
	"""Recalculate one Workday from its Employee Checkins, creating it when missing."""
	from hr_addon.hr_addon.doctype.workday.workday import has_weekly_working_hours_for_date

	log_date = getdate(log_date)

	if not frappe.db.exists("Employee", {"name": employee, "status": "Active"}):
		return

	if not has_weekly_working_hours_for_date(employee, log_date):
		return

	workday_name = frappe.db.get_value(
		"Workday", {"employee": employee, "log_date": log_date}, "name"
	)

	if not workday_name and not has_checkins(employee, log_date):
		return

	setattr(frappe.flags, SYNC_FLAG, True)
	# Desk alerts ("Attendance updated", "Overtime Ledger Entry created") are for
	# the HR form. During an employee check-in they would be returned with the
	# request and the app would show them as errors.
	previous_mute = frappe.flags.mute_messages
	frappe.flags.mute_messages = True
	# A failed workday update must not undo the check-in the employee just saved.
	# Attendance and the overtime ledger commit inside the save, which drops this
	# savepoint. Releasing or rolling it back afterwards must not become the error.
	save_point = "workday_checkin_sync"
	frappe.db.savepoint(save_point)
	try:
		if workday_name:
			workday = frappe.get_doc("Workday", workday_name)
		else:
			workday = frappe.new_doc("Workday")
			workday.employee = employee
			workday.log_date = log_date
			workday.company = frappe.db.get_value("Employee", employee, "company")

		workday.flags.ignore_permissions = True
		workday.save()
	except Exception:
		_end_sync_savepoint(save_point, rollback=True)
		frappe.log_error(
			title="HR Addon: Workday sync from Employee Checkin",
			message=f"Employee {employee}, date {log_date}\n\n{frappe.get_traceback()}",
		)
	else:
		_end_sync_savepoint(save_point, rollback=False)
		frappe.db.commit()
	finally:
		frappe.flags.mute_messages = previous_mute
		setattr(frappe.flags, SYNC_FLAG, False)
		# Drop desk notes such as "No hour variance to record" so the app does not
		# show them as an error and skip refreshing the day.
		frappe.clear_messages()


def _end_sync_savepoint(save_point, rollback):
	"""Release or roll back the sync savepoint.

	A commit inside the workday save already removed it. That is not a failure:
	the check-in and the ledger row are stored.
	"""
	try:
		if rollback:
			frappe.db.rollback(save_point=save_point)
		else:
			frappe.db.release_savepoint(save_point)
	except Exception as exc:
		if "SAVEPOINT" not in str(exc) or "does not exist" not in str(exc):
			raise


def has_checkins(employee, log_date):
	return bool(
		frappe.db.count(
			"Employee Checkin",
			{
				"employee": employee,
				"time": ["between", [f"{log_date} 00:00:00", f"{log_date} 23:59:59"]],
			},
		)
	)
