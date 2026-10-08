# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

from frappe.tests.utils import FrappeTestCase

from hr_addon.api.team_calendar import team_cell


def _day(status, **extra):
	return {"date": "2026-09-14", "status": status, "actual_working_hours": 0, **extra}


class TestTeamCell(FrappeTestCase):
	def test_worked_day(self):
		cell = team_cell(_day("Present", actual_working_hours=8.5))
		self.assertEqual(cell["kind"], "work")
		self.assertEqual(cell["hours"], 8.5)

	def test_illness_is_its_own_kind(self):
		leave = {"illness": True, "docstatus": 1, "leave_type": "Sick Leave"}
		cell = team_cell(_day("On Leave", leave=leave))
		self.assertEqual((cell["kind"], cell["pending"], cell["leave_type"]), ("sick", False, "Sick Leave"))

	def test_pending_vacation(self):
		leave = {"illness": False, "docstatus": 0, "leave_type": "Privilege Leave"}
		cell = team_cell(_day("Pending Leave", leave=leave))
		self.assertEqual((cell["kind"], cell["pending"]), ("leave", True))

	def test_home_office_wins_over_worked(self):
		cell = team_cell(_day("Present", home_office={"docstatus": 1, "half_day": False}))
		self.assertEqual(cell["kind"], "home")

	def test_holiday_wins_over_home_office(self):
		cell = team_cell(_day("Holiday", home_office={"docstatus": 1}))
		self.assertEqual(cell["kind"], "holiday")

	def test_own_cells_carry_the_request(self):
		leave = {"illness": False, "docstatus": 0, "leave_type": "Privilege Leave", "name": "HR-LAP-1"}
		self.assertIsNone(team_cell(_day("Pending Leave", leave=leave))["request"])
		own = team_cell(_day("Pending Leave", leave=leave), own=True)
		self.assertEqual(own["request"], {"kind": "leave", "name": "HR-LAP-1"})

	def test_missing_open_and_free(self):
		self.assertEqual(team_cell(_day("Missing"))["kind"], "missing")
		self.assertEqual(team_cell(_day("Open"))["kind"], "open")
		self.assertEqual(team_cell(_day("Off"))["kind"], "free")
		self.assertEqual(team_cell(_day("Not Workday"))["kind"], "free")
