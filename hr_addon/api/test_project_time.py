# Copyright (c) 2026, phamos.eu and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import getdate

from hr_addon.api.project_time import (
	_booked_hours,
	get_my_project_time,
	save_my_project_time,
)


def _keys(value):
	found = set()
	if isinstance(value, dict):
		found.update(value)
		for item in value.values():
			found |= _keys(item)
	elif isinstance(value, list):
		for item in value:
			found |= _keys(item)
	return found


def _values(value):
	found = []
	if isinstance(value, dict):
		for item in value.values():
			found.extend(_values(item))
	elif isinstance(value, list):
		for item in value:
			found.extend(_values(item))
	else:
		found.append(value)
	return found


class TestBookedHours(FrappeTestCase):
	def test_half_hours_and_a_remainder_are_kept(self):
		self.assertEqual(_booked_hours(1.5), 1.5)
		self.assertEqual(_booked_hours("0.5"), 0.5)
		self.assertEqual(_booked_hours(0.33), 0.33)
		self.assertEqual(_booked_hours(0), 0)

	def test_negative_hours_are_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			_booked_hours(-1)


class TestProjectTimeBooking(FrappeTestCase):
	def setUp(self):
		company = frappe.db.get_value("Company", {}, "name")
		self.employee = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": f"_T Hours {frappe.generate_hash(length=6)}",
				"company": company,
				"date_of_joining": "2026-01-01",
				"date_of_birth": "1990-01-01",
				"gender": "Male",
				"status": "Active",
			}
		).insert(ignore_permissions=True)
		self.project = frappe.get_doc(
			{
				"doctype": "Project",
				"project_name": f"_T Internal {frappe.generate_hash(length=6)}",
				"company": company,
				"status": "Open",
			}
		).insert(ignore_permissions=True)
		if not frappe.db.exists("Activity Type", "Development"):
			frappe.get_doc(
				{
					"doctype": "Activity Type",
					"activity_type": "Development",
					"costing_rate": 0,
					"billing_rate": 0,
				}
			).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Activity Cost",
				"activity_type": "Development",
				"employee": self.employee.name,
				"costing_rate": 95,
				"billing_rate": 180,
			}
		).insert(ignore_permissions=True)

	def test_booking_stores_cost_without_showing_it(self):
		with self._as_employee():
			result = save_my_project_time(
				2026,
				10,
				[
					{
						"date": "2026-10-05",
						"project": self.project.name,
						"activity_type": "Development",
						"hours": 1.5,
						"description": "  API review  ",
					},
					{
						"date": "2026-10-05",
						"project": self.project.name,
						"activity_type": "Operational",
						"hours": 0.5,
						"description": "Support",
					},
				],
			)

		self.assertEqual(result["booked_hours"], 2)
		development_entry = next(row for row in result["entries"] if row["activity_type"] == "Development")
		self.assertEqual(development_entry["description"], "API review")
		self.assertEqual(len(result["entries"]), 2)
		self.assertEqual(len(result["days"]), 31)
		self.assertEqual(result["days"][4]["date"], "2026-10-05")
		self.assertIn("worked_hours", result["days"][4])
		self.assertIn("target_hours", result["days"][4])
		self.assertFalse(_keys(result) & {"costing_rate", "billing_rate", "costing_amount", "billing_amount"})
		self.assertFalse(set(_values(result)) & {95, 95.0, 142.5})

		timesheet = frappe.get_doc(
			"Timesheet",
			{"employee": self.employee.name, "custom_project_hours_month": "2026-10", "docstatus": 1},
		)
		self.assertEqual(timesheet.docstatus, 1)
		rows = sorted(timesheet.time_logs, key=lambda row: row.from_time)
		self.assertEqual([row.hours for row in rows], [1.5, 0.5])
		self.assertEqual(rows[0].to_time, rows[1].from_time)
		self.assertTrue(all(row.is_billable == 0 and row.billing_amount == 0 for row in rows))
		development = next(row for row in rows if row.activity_type == "Development")
		self.assertEqual(development.description, "API review")
		self.assertEqual(development.costing_rate, 95)
		self.assertEqual(development.costing_amount, 142.5)
		self.assertEqual(getdate(rows[0].from_time), getdate("2026-10-05"))

	def test_a_later_save_replaces_the_month(self):
		with self._as_employee():
			save_my_project_time(
				2026,
				10,
				[
					{
						"date": "2026-10-05",
						"project": self.project.name,
						"activity_type": "Development",
						"hours": 2,
						"description": "Work",
					}
				],
			)
			result = save_my_project_time(
				2026,
				10,
				[
					{
						"date": "2026-10-05",
						"project": self.project.name,
						"activity_type": "Development",
						"hours": 1.5,
						"description": "Work",
					}
				],
			)
			loaded = get_my_project_time(2026, 10)

		self.assertEqual(result["entries"][0]["hours"], 1.5)
		self.assertEqual(loaded["booked_hours"], 1.5)
		self.assertEqual(
			frappe.db.count(
				"Timesheet",
				{"employee": self.employee.name, "custom_project_hours_month": "2026-10", "docstatus": 1},
			),
			1,
		)

	def test_projects_used_more_often_come_first(self):
		other = frappe.get_doc(
			{
				"doctype": "Project",
				"project_name": f"_T Rare {frappe.generate_hash(length=6)}",
				"company": self.project.company,
				"status": "Open",
			}
		).insert(ignore_permissions=True)
		with self._as_employee():
			save_my_project_time(
				2026,
				10,
				[
					{
						"date": "2026-10-05",
						"project": self.project.name,
						"activity_type": "Development",
						"hours": 1,
						"description": "Work",
					},
					{
						"date": "2026-10-06",
						"project": self.project.name,
						"activity_type": "Development",
						"hours": 1,
						"description": "Work",
					},
					{
						"date": "2026-10-07",
						"project": self.project.name,
						"activity_type": "Operational",
						"hours": 0.5,
						"description": "Work",
					},
					{
						"date": "2026-10-08",
						"project": other.name,
						"activity_type": "Development",
						"hours": 4,
						"description": "Work",
					},
				],
			)
			names = [row["name"] for row in get_my_project_time(2026, 11)["projects"]]

		self.assertLess(names.index(self.project.name), names.index(other.name))

	def test_a_remainder_is_stored_exactly(self):
		with self._as_employee():
			result = save_my_project_time(
				2026,
				10,
				[
					{
						"date": "2026-10-05",
						"project": self.project.name,
						"activity_type": "Development",
						"hours": 1.33,
						"description": "Work",
					}
				],
			)
		self.assertEqual(result["entries"][0]["hours"], 1.33)
		timesheet = frappe.get_doc(
			"Timesheet",
			{"employee": self.employee.name, "custom_project_hours_month": "2026-10", "docstatus": 1},
		)
		self.assertEqual(timesheet.time_logs[0].hours, 1.33)

	def test_a_missing_description_is_rejected(self):
		with self._as_employee():
			with self.assertRaises(frappe.ValidationError):
				save_my_project_time(
					2026,
					10,
					[
						{
							"date": "2026-10-05",
							"project": self.project.name,
							"activity_type": "Development",
							"hours": 1,
						}
					],
				)

	def test_a_long_description_is_rejected(self):
		with self._as_employee():
			with self.assertRaises(frappe.ValidationError):
				save_my_project_time(
					2026,
					10,
					[
						{
							"date": "2026-10-05",
							"project": self.project.name,
							"activity_type": "Development",
							"hours": 1,
							"description": "x" * 501,
						}
					],
				)

	def test_more_than_a_day_is_rejected(self):
		with self._as_employee():
			with self.assertRaises(frappe.ValidationError):
				save_my_project_time(
					2026,
					10,
					[
						{
							"date": "2026-10-05",
							"project": self.project.name,
							"activity_type": "Development",
							"hours": 20,
							"description": "Work",
						},
						{
							"date": "2026-10-05",
							"project": self.project.name,
							"activity_type": "Operational",
							"hours": 5,
							"description": "Work",
						},
					],
				)

	def _as_employee(self):
		return patch(
			"hr_addon.api.project_time._current_employee",
			return_value=self.employee.name,
		)
