import frappe
from erpnext.buying.doctype.supplier_scorecard.supplier_scorecard import daterange
from frappe import _
from frappe.utils import cint, flt, getdate
from hrms.hr.doctype.leave_application.leave_application import (
	LeaveApplication,
	get_leave_balance_on,
	is_lwp,
)
from hrms.hr.utils import get_holiday_dates_for_employee

from hr_addon.hr_addon.leave_days import charged_leave_days, is_zero_hour_day


class HrAddonLeaveApplication(LeaveApplication):
	def validate_balance_leaves(self):
		precision = cint(frappe.db.get_single_value("System Settings", "float_precision")) or 2
		leave_application = None if self.is_new() else self.name

		if self.from_date and self.to_date:
			self.total_leave_days = charged_leave_days(
				self.employee,
				self.leave_type,
				self.from_date,
				self.to_date,
				self.half_day,
				self.half_day_date,
			)

			if self.total_leave_days <= 0:
				frappe.throw(
					_(
						"These days are not working days in the weekly working hours, or they are holidays. No leave is deducted, so there is nothing to request."
					)
				)

			if not is_lwp(self.leave_type):
				leave_balance = get_leave_balance_on(
					self.employee,
					self.leave_type,
					self.from_date,
					self.to_date,
					consider_all_leaves_in_the_allocation_period=True,
					for_consumption=True,
					leave_application=leave_application,
				)
				leave_balance_for_consumption = flt(
					leave_balance.get("leave_balance_for_consumption"), precision
				)
				if self.status != "Rejected" and (
					leave_balance_for_consumption < self.total_leave_days or not leave_balance_for_consumption
				):
					self.show_insufficient_balance_message(leave_balance_for_consumption)

	def validate_leave_overlap(self):
		"""Overlap only on days that deduct leave. A 0-hour day does not block another request."""
		if not self.name:
			self.name = "New Leave Application"

		for existing in frappe.db.sql(
			"""
			select name, leave_type, from_date, to_date, half_day, half_day_date, total_leave_days
			from `tabLeave Application`
			where employee = %(employee)s and docstatus < 2 and status in ('Open', 'Approved')
			and to_date >= %(from_date)s and from_date <= %(to_date)s
			and name != %(name)s
			""",
			{
				"employee": self.employee,
				"from_date": self.from_date,
				"to_date": self.to_date,
				"name": self.name,
			},
			as_dict=True,
		):
			if self._shares_charged_day(existing):
				self.throw_overlap_error(existing)

	def _shares_charged_day(self, other):
		start = max(getdate(self.from_date), getdate(other.from_date))
		end = min(getdate(self.to_date), getdate(other.to_date))
		if start > end:
			return False
		# A day counts when either request would deduct it. Both use the same
		# weekly hours, so one calculation over the shared dates is enough.
		if not charged_leave_days(self.employee, self.leave_type, start, end, 0, None):
			return False
		if (
			cint(self.half_day) == 1
			and cint(other.half_day) == 1
			and getdate(self.half_day_date) == getdate(other.half_day_date)
			and flt(self.total_leave_days) == 0.5
			and flt(other.total_leave_days) == 0.5
		):
			return self.get_total_leaves_on_half_day() >= 1
		return True

	def _leave_type_deducts_from_ot_ledger(self):
		lt = getattr(self, "leave_type", None)
		if not lt:
			return False
		return bool(
			frappe.db.get_value("Leave Type", lt, "custom_is_deducted_from_overtime_ledger")
		)

	def _reconcile_ot_leave_attendance_name(self, date, attendance_name):
		"""Amend/cancel flow cancels Attendance (docstatus 2); core update_attendance only
		looks for docstatus != 2, so submit of amended LA inserts a duplicate row.

		For OT-deduct leave: prefer reviving the cancelled row and cancelling any stray
		submitted duplicate for the same day, then update that single Attendance.
		"""
		if not self._leave_type_deducts_from_ot_ledger():
			return attendance_name

		cancelled_rows = frappe.db.sql(
			"""
			select name from `tabAttendance`
			where employee = %(employee)s and attendance_date = %(d)s and docstatus = 2
				and status in ('On Leave', 'Half Day')
			order by modified desc
			limit 1
			""",
			{"employee": self.employee, "d": date},
		)
		cancelled_name = cancelled_rows[0][0] if cancelled_rows else None

		if cancelled_name and attendance_name and attendance_name != cancelled_name:
			frappe.db.set_value(
				"Attendance",
				cancelled_name,
				"docstatus",
				1,
				update_modified=True,
			)
			try:
				dup = frappe.get_doc("Attendance", attendance_name)
				dup.flags.ignore_permissions = True
				if dup.docstatus == 1:
					dup.cancel()
			except Exception as e:
				frappe.log_error(
					title="Leave amend: cancel duplicate attendance",
					message=f"{attendance_name}: {e}",
				)
			return cancelled_name

		if cancelled_name and not attendance_name:
			frappe.db.set_value(
				"Attendance",
				cancelled_name,
				"docstatus",
				1,
				update_modified=True,
			)
			return cancelled_name

		return attendance_name

	def update_attendance(self):
		if self.status != "Approved":
			return

		holiday_dates = []
		if not frappe.db.get_value("Leave Type", self.leave_type, "include_holiday"):
			holiday_dates = get_holiday_dates_for_employee(self.employee, self.from_date, self.to_date)

		for dt in daterange(getdate(self.from_date), getdate(self.to_date)):
			date = dt.strftime("%Y-%m-%d")
			attendance_name = frappe.db.exists(
				"Attendance",
				dict(
					employee=self.employee,
					attendance_date=date,
					docstatus=("!=", 2),
				),
			)
			if self._leave_type_deducts_from_ot_ledger():
				attendance_name = self._reconcile_ot_leave_attendance_name(date, attendance_name)

			if date in holiday_dates or is_zero_hour_day(self.employee, date):
				if attendance_name:
					attendance = frappe.get_doc("Attendance", attendance_name)
					attendance.flags.ignore_permissions = True
					if attendance.docstatus == 1:
						attendance.cancel()
					frappe.delete_doc("Attendance", attendance_name, force=1)
				continue

			self.create_or_update_attendance(attendance_name, date)

	def cancel_attendance(self):
		"""
		Cancel leave-linked Attendance via Document.cancel() so server hooks run
		(e.g. Overtime Ledger reversal on Attendance on_cancel).

		HRMS core uses frappe.db.set_value(..., docstatus, 2), which bypasses
		Attendance.cancel() and leaves OLE rows active.
		"""
		if self.docstatus != 2:
			return

		attendance_rows = frappe.db.sql(
			"""
			select name from `tabAttendance`
			where employee = %(employee)s
				and attendance_date between %(from_date)s and %(to_date)s
				and docstatus < 2
				and status in ('On Leave', 'Half Day')
			""",
			{
				"employee": self.employee,
				"from_date": self.from_date,
				"to_date": self.to_date,
			},
			as_dict=True,
		)

		for row in attendance_rows:
			doc = frappe.get_doc("Attendance", row.name)
			doc.flags.ignore_permissions = True
			doc.cancel()
