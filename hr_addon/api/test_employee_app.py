# Copyright (c) 2026, phamos.eu and Contributors
# See license.txt

from datetime import datetime, time
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import getdate

from hr_addon.api.employee_app import _validated_log, suggest_missing_punch, summarize_hours


class TestEmployeeAppHours(FrappeTestCase):
	def test_summarize_hours_splits_month_and_year(self):
		rows = [
			SimpleNamespace(
				log_date="2026-01-15",
				actual_working_hours=8,
				target_hours=8,
				status="Present",
			),
			SimpleNamespace(
				log_date="2026-09-01",
				actual_working_hours=7.5,
				target_hours=8,
				status="Present",
			),
			SimpleNamespace(
				log_date="2026-09-02",
				actual_working_hours=0,
				target_hours=8,
				status="Not Workday",
			),
			SimpleNamespace(
				log_date="2026-12-01",
				actual_working_hours=8,
				target_hours=8,
				status="Present",
			),
		]

		summary = summarize_hours(rows, getdate("2026-09-28"))

		self.assertEqual(summary["month_actual"], 7.5)
		self.assertEqual(summary["month_target"], 8)
		self.assertEqual(summary["month_balance"], -0.5)
		self.assertEqual(summary["year_actual"], 15.5)
		self.assertEqual(summary["year_target"], 16)

	def test_future_checkin_is_rejected(self):
		with patch("hr_addon.api.employee_app.now_datetime") as mock_now:
			mock_now.return_value = frappe.utils.get_datetime("2026-09-28 12:00:00")
			with self.assertRaises(frappe.ValidationError):
				_validated_log("IN", "2026-09-28 18:00:00")

	def test_log_type_must_be_in_or_out(self):
		with self.assertRaises(frappe.ValidationError):
			_validated_log("BREAK", "2026-09-28 08:00:00")


def _punch(name, log_type, stamp):
	return {"name": name, "log_type": log_type, "time": stamp}


class TestSuggestMissingPunch(FrappeTestCase):
	NOW = datetime(2026, 9, 28, 22, 0, 0)

	def _suggest(self, checkins, date="2026-09-23", target=8, break_minutes=30, mechanism=None, usual="08:00", now=None):
		hour, minute = usual.split(":")
		return suggest_missing_punch(
			checkins,
			date=getdate(date),
			target_hours=target,
			break_minutes=break_minutes,
			mechanism=mechanism or "Break Hours from Employee Checkins",
			usual_start=time(int(hour), int(minute)),
			now=now or self.NOW,
		)

	def test_open_checkin_suggests_checkout_at_target(self):
		result = self._suggest([_punch("a", "IN", "2026-09-23 08:05:00")])

		self.assertEqual(result["action"], "add")
		self.assertEqual(result["log_type"], "OUT")
		self.assertEqual(result["time"], datetime(2026, 9, 23, 16, 5))
		self.assertIn("16:05", result["reason"])

	def test_scheduled_break_is_added_to_the_checkout(self):
		result = self._suggest(
			[_punch("a", "IN", "2026-09-23 08:00:00")],
			mechanism="Break Hours from Weekly Working Hours",
		)

		self.assertEqual(result["time"], datetime(2026, 9, 23, 16, 30))

	def test_return_from_lunch_suggests_the_remaining_hours(self):
		result = self._suggest(
			[
				_punch("a", "IN", "2026-09-23 08:00:00"),
				_punch("b", "OUT", "2026-09-23 12:00:00"),
				_punch("c", "IN", "2026-09-23 12:30:00"),
			]
		)

		self.assertEqual(result["action"], "add")
		self.assertEqual(result["log_type"], "OUT")
		self.assertEqual(result["time"], datetime(2026, 9, 23, 16, 30))

	def test_two_close_checkins_become_a_checkout(self):
		result = self._suggest(
			[
				_punch("a", "IN", "2026-09-23 08:00:00"),
				_punch("b", "IN", "2026-09-23 08:04:00"),
			]
		)

		self.assertEqual(result["action"], "correct")
		self.assertEqual(result["name"], "b")
		self.assertEqual(result["log_type"], "OUT")
		self.assertEqual(result["time"], datetime(2026, 9, 23, 16, 0))

	def test_second_checkin_near_the_end_keeps_its_time(self):
		result = self._suggest(
			[
				_punch("a", "IN", "2026-09-23 08:00:00"),
				_punch("b", "IN", "2026-09-23 16:10:00"),
			]
		)

		self.assertEqual(result["action"], "correct")
		self.assertEqual(result["log_type"], "OUT")
		self.assertEqual(result["time"], datetime(2026, 9, 23, 16, 10))

	def test_two_checkins_with_a_gap_insert_a_checkout_before_the_second(self):
		result = self._suggest(
			[
				_punch("a", "IN", "2026-09-23 08:00:00"),
				_punch("b", "IN", "2026-09-23 12:30:00"),
			]
		)

		self.assertEqual(result["action"], "add")
		self.assertEqual(result["log_type"], "OUT")
		self.assertEqual(result["time"], datetime(2026, 9, 23, 12, 0))

	def test_duplicate_checkin_before_a_later_punch_is_removed(self):
		result = self._suggest(
			[
				_punch("a", "IN", "2026-09-23 08:00:00"),
				_punch("b", "IN", "2026-09-23 08:02:00"),
				_punch("c", "OUT", "2026-09-23 16:30:00"),
			]
		)

		self.assertEqual(result["action"], "remove")
		self.assertEqual(result["name"], "b")

	def test_day_starting_with_checkout_suggests_the_usual_start(self):
		result = self._suggest([_punch("a", "OUT", "2026-09-23 16:30:00")], usual="08:15")

		self.assertEqual(result["action"], "add")
		self.assertEqual(result["log_type"], "IN")
		self.assertEqual(result["time"], datetime(2026, 9, 23, 8, 15))

	def test_two_checkouts_suggest_the_return_checkin(self):
		result = self._suggest(
			[
				_punch("a", "IN", "2026-09-23 08:00:00"),
				_punch("b", "OUT", "2026-09-23 12:00:00"),
				_punch("c", "OUT", "2026-09-23 16:30:00"),
			]
		)

		self.assertEqual(result["action"], "add")
		self.assertEqual(result["log_type"], "IN")
		self.assertEqual(result["time"], datetime(2026, 9, 23, 12, 30))

	def test_duplicate_checkout_is_removed(self):
		result = self._suggest(
			[
				_punch("a", "IN", "2026-09-23 08:00:00"),
				_punch("b", "OUT", "2026-09-23 16:30:00"),
				_punch("c", "OUT", "2026-09-23 16:34:00"),
			]
		)

		self.assertEqual(result["action"], "remove")
		self.assertEqual(result["name"], "c")

	def test_checkout_a_few_minutes_after_checkin_is_moved_to_the_target(self):
		result = self._suggest(
			[
				_punch("a", "IN", "2026-09-23 08:00:00"),
				_punch("b", "OUT", "2026-09-23 08:03:00"),
			]
		)

		self.assertEqual(result["action"], "correct")
		self.assertEqual(result["name"], "b")
		self.assertEqual(result["time"], datetime(2026, 9, 23, 16, 0))

	def test_extra_checkin_right_after_checkout_is_removed(self):
		result = self._suggest(
			[
				_punch("a", "IN", "2026-09-23 08:00:00"),
				_punch("b", "OUT", "2026-09-23 16:30:00"),
				_punch("c", "IN", "2026-09-23 16:35:00"),
			]
		)

		self.assertEqual(result["action"], "remove")
		self.assertEqual(result["name"], "c")

	def test_clean_day_has_no_suggestion(self):
		result = self._suggest(
			[
				_punch("a", "IN", "2026-09-23 08:00:00"),
				_punch("b", "OUT", "2026-09-23 16:30:00"),
			]
		)

		self.assertIsNone(result)

	def test_empty_day_suggests_the_usual_start(self):
		result = self._suggest([], usual="08:15")

		self.assertEqual(result["action"], "add")
		self.assertEqual(result["log_type"], "IN")
		self.assertEqual(result["time"], datetime(2026, 9, 23, 8, 15))

	def test_empty_day_without_a_target_is_left_alone(self):
		self.assertIsNone(self._suggest([], target=0))

	def test_future_checkout_is_not_suggested(self):
		result = self._suggest(
			[_punch("a", "IN", "2026-09-23 08:05:00")],
			now=datetime(2026, 9, 23, 10, 0, 0),
		)

		self.assertIsNone(result)

	def test_late_checkin_checkout_stays_on_the_same_day(self):
		result = self._suggest([_punch("a", "IN", "2026-09-23 18:08:00")])

		self.assertEqual(result["time"], datetime(2026, 9, 23, 23, 59))
		self.assertIn("23:59", result["reason"])
