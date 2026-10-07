# Copyright (c) 2022, phamos.eu and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from hr_addon.events.employee_checkin import (
	SYNC_FLAG,
	enqueue_workday_sync,
	sync_workday,
)
from hr_addon.hr_addon.doctype.workday.workday import _create_new_attendance


def _make_checkin(time="2026-07-25 09:00:00", employee="HR-EMP-00001", previous_time=None):
	previous = SimpleNamespace(time=previous_time) if previous_time else None
	return SimpleNamespace(
		employee=employee,
		time=time,
		get_doc_before_save=lambda: previous,
	)


class TestEnqueueWorkdaySync(FrappeTestCase):
	def tearDown(self):
		setattr(frappe.flags, SYNC_FLAG, False)

	# enqueue_workday_sync: does nothing while the setting is off.
	@patch("hr_addon.events.employee_checkin.frappe.enqueue")
	@patch(
		"hr_addon.events.employee_checkin.frappe.db.get_single_value",
		return_value=0,
	)
	def test_enqueue_skipped_when_disabled(self, _mock_setting, mock_enqueue):
		enqueue_workday_sync(_make_checkin(), "on_update")
		mock_enqueue.assert_not_called()

	# enqueue_workday_sync: does not re-enter while a sync is already running.
	@patch("hr_addon.events.employee_checkin.frappe.enqueue")
	@patch(
		"hr_addon.events.employee_checkin.frappe.db.get_single_value",
		return_value=1,
	)
	def test_enqueue_skipped_during_sync(self, _mock_setting, mock_enqueue):
		setattr(frappe.flags, SYNC_FLAG, True)
		enqueue_workday_sync(_make_checkin(), "on_update")
		mock_enqueue.assert_not_called()

	# enqueue_workday_sync: queues one deduplicated job for the checkin date.
	@patch("hr_addon.events.employee_checkin.frappe.enqueue")
	@patch(
		"hr_addon.events.employee_checkin.frappe.db.get_single_value",
		return_value=1,
	)
	def test_enqueue_queues_job_for_checkin_date(self, _mock_setting, mock_enqueue):
		enqueue_workday_sync(_make_checkin(), "on_update")
		self.assertEqual(mock_enqueue.call_count, 1)
		kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(kwargs["log_date"], "2026-07-25")
		self.assertEqual(kwargs["employee"], "HR-EMP-00001")
		self.assertTrue(kwargs["deduplicate"])
		self.assertTrue(kwargs["enqueue_after_commit"])

	# enqueue_workday_sync: a corrected timestamp refreshes the old date as well.
	@patch("hr_addon.events.employee_checkin.frappe.enqueue")
	@patch(
		"hr_addon.events.employee_checkin.frappe.db.get_single_value",
		return_value=1,
	)
	def test_enqueue_covers_previous_date_after_correction(self, _mock_setting, mock_enqueue):
		enqueue_workday_sync(
			_make_checkin(previous_time="2026-07-24 09:00:00"), "on_update"
		)
		queued_dates = {call.kwargs["log_date"] for call in mock_enqueue.call_args_list}
		self.assertEqual(queued_dates, {"2026-07-24", "2026-07-25"})


class TestSyncWorkday(FrappeTestCase):
	def tearDown(self):
		setattr(frappe.flags, SYNC_FLAG, False)

	# sync_workday: skips dates without an hours row in Weekly Working Hours.
	@patch("hr_addon.events.employee_checkin.frappe.get_doc")
	@patch(
		"hr_addon.hr_addon.doctype.workday.workday.has_weekly_working_hours_for_date",
		return_value=False,
	)
	@patch("hr_addon.events.employee_checkin.frappe.db.exists", return_value="HR-EMP-00001")
	def test_sync_skips_date_without_weekly_hours(
		self, _mock_exists, _mock_weekly, mock_get_doc
	):
		sync_workday("HR-EMP-00001", "2026-07-25")
		mock_get_doc.assert_not_called()

	# sync_workday: saves the existing Workday and clears the recursion flag afterwards.
	@patch("hr_addon.events.employee_checkin.frappe.db.commit")
	@patch("hr_addon.events.employee_checkin.frappe.get_doc")
	@patch(
		"hr_addon.events.employee_checkin.frappe.db.get_value",
		return_value="WD-2026-00001",
	)
	@patch(
		"hr_addon.hr_addon.doctype.workday.workday.has_weekly_working_hours_for_date",
		return_value=True,
	)
	@patch("hr_addon.events.employee_checkin.frappe.db.exists", return_value="HR-EMP-00001")
	def test_sync_saves_existing_workday(
		self, _mock_exists, _mock_weekly, _mock_get_value, mock_get_doc, _mock_commit
	):
		workday = MagicMock()
		mock_get_doc.return_value = workday
		sync_workday("HR-EMP-00001", "2026-07-25")
		workday.save.assert_called_once()
		self.assertFalse(getattr(frappe.flags, SYNC_FLAG, False))

	# sync_workday: a failing save is logged instead of bubbling into the checkin.
	@patch("hr_addon.events.employee_checkin.frappe.log_error")
	@patch("hr_addon.events.employee_checkin.frappe.db.rollback")
	@patch("hr_addon.events.employee_checkin.frappe.get_doc")
	@patch(
		"hr_addon.events.employee_checkin.frappe.db.get_value",
		return_value="WD-2026-00001",
	)
	@patch(
		"hr_addon.hr_addon.doctype.workday.workday.has_weekly_working_hours_for_date",
		return_value=True,
	)
	@patch("hr_addon.events.employee_checkin.frappe.db.exists", return_value="HR-EMP-00001")
	def test_sync_logs_failure(
		self,
		_mock_exists,
		_mock_weekly,
		_mock_get_value,
		mock_get_doc,
		mock_rollback,
		mock_log_error,
	):
		workday = MagicMock()
		workday.save.side_effect = Exception("boom")
		mock_get_doc.return_value = workday
		sync_workday("HR-EMP-00001", "2026-07-25")
		mock_rollback.assert_called_once_with(save_point="workday_checkin_sync")
		mock_log_error.assert_called_once()
		self.assertFalse(getattr(frappe.flags, SYNC_FLAG, False))


class TestAttendanceIgnoresEmployeePermissions(FrappeTestCase):
	@patch("hr_addon.hr_addon.doctype.workday.workday.frappe.msgprint")
	@patch("hr_addon.hr_addon.doctype.workday.workday.frappe.db.commit")
	@patch("hr_addon.hr_addon.doctype.workday.workday.frappe.db.set_value")
	@patch("hr_addon.hr_addon.doctype.workday.workday.frappe.get_doc")
	def test_new_attendance_is_inserted_without_the_employee_permission(
		self, mock_get_doc, _set_value, _commit, _msgprint
	):
		attendance = MagicMock()
		mock_get_doc.return_value = attendance
		doc = SimpleNamespace(
			name="WD-1",
			employee="HR-EMP-00001",
			company="Movaria",
			log_date="2026-10-01",
			first_checkin=None,
			last_checkout=None,
		)
		_create_new_attendance(doc, 0, "Present", 8, 8)
		attendance.insert.assert_called_once_with(ignore_permissions=True)
		self.assertTrue(attendance.flags.ignore_permissions)
		attendance.submit.assert_called_once()
