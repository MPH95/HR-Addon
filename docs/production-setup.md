# Production setup for the employee app

What HR has to configure so the employee app (hours, workday calendar, vacation, sick, home office, team calendar, project hours) works. The app does not keep its own copy of this data. It reads Frappe HR, ERPNext, and HR Addon.

Two apps have to be deployed together:

- **hr_addon**, including `hr_addon/api/` (hours, check-in corrections, requests, team calendar, project hours).
- **hrms** fork, branch `version-15`, with the built frontend (`yarn build` in `hrms/frontend`).

Project hours also need ERPNext. They are stored on a normal Timesheet, not on a separate document.

## Once per company

### 1. Holiday list

Create a Holiday List for the year and set it as **Company → Default Holiday List**.

Add the public holidays, and add Saturday and Sunday as weekly holidays (Holiday List has "Add Weekly Holidays" for this).

This list is used in three places:

- A day on the list is a **holiday** in the calendar, not a missing workday.
- **Leave applications** skip holidays when counting days. A day is also skipped when Weekly Working Hours gives it 0 hours, or when the employee has weekly hours for that period but no row for that weekday. It is not tied to Saturday and Sunday. Those days do not reduce the balance and do not block another request. A request that falls only on them is refused. The request panel shows the counted days before the employee sends it.
- An **Attendance Request** (home office) refuses to save if neither the employee nor the company has a holiday list.

An employee can have their own Holiday List. If that field is empty, the company default is used.

### 2. Leave types

Create the types employees should be able to request. The app sorts them into Vacation and Sick by the **name** of the leave type. A type counts as sick when its name contains one of: `sick`, `illness`, `krank`, `maladie`, `medical` (for example "Sick Leave" or "Krankmeldung"). Anything else is shown as Vacation.

There is no separate illness document. A sick day is a Leave Application.

Suggested types:

| Type | Purpose |
| --- | --- |
| Privilege Leave (or your vacation type) | Vacation. Needs a Leave Allocation. |
| Sick Leave | Sick days. The name must match the words above. Needs a Leave Allocation. |
| Leave Without Pay | Fallback. It is offered even without an allocation, and it does not reduce a balance. |

Optional caps live on the Leave Type, not in the app:

- **Maximum Leave Allocation Allowed per Leave Period** limits how many days HR can allocate.
- **Maximum Consecutive Leaves Allowed** limits one request. `0` means no limit.

### 3. Leave period and approvers

Create a **Leave Period** for the year (1 Jan–31 Dec, or your fiscal year). Allocations are made against this period.

Who approves a request, in this order:

1. **Employee → Leave Approver**, or
2. the first **Leave Approver** on the employee's Department.

Set **HR Settings → Leave Approver Mandatory in Leave Application** if a request must not be saved without an approver.

The employee saves a draft. The approver approves it in Frappe HR (or from Team Requests in the app). The app does not approve anything by itself.

### 4. HR Addon Settings

| Setting | What to use |
| --- | --- |
| Workday break calculation mechanism | However you already calculate hours. This site uses **Break Hours from Employee Checkins**, so the break is the gap between the morning OUT and the afternoon IN. |
| Update Workday on Employee Checkin | **On.** A normal check-in then rebuilds that day's Workday immediately. Corrections made in the app rebuild it either way. |
| Skip Workdays for Employees without Weekly Hours | **On**, unless every active employee already has Weekly Working Hours. With it off, the scheduler stops on the first employee who has none. |
| Allow Workdays on Holidays | Off, unless people really work on public holidays and those hours should count. |
| Enable Overtime Ledger Feature | On if overtime balances are kept in the Overtime Ledger. |

Do **not** give the Employee role access to Workday or Overtime Ledger. The app reads them through its own methods, limited to the logged-in employee.

### 5. Departments

The team calendar lists every active employee of the company. The department filter only offers departments that are actually set on an employee, so fill in **Employee → Department**. Leave approvers can also be maintained on the Department instead of on every employee.

### 6. Projects and time tracking

Employees book hours from **Project hours** in the app, one month at a time. A day can be booked only after a check-in has produced hours. The stepper moves in half hours. **All**, and the last plus step, take whatever is still open, including a remainder under half an hour.

The first time anyone opens that page, the app creates three things if they are missing. Check them afterwards:

| What appears | What to do with it |
| --- | --- |
| Activity Types **Development** and **Operational** | Leave the names exactly like that. The app only offers these two. Operational is labelled "Operational work". Existing rates are not overwritten. |
| Hidden Timesheet field **Project Hours Month** | Value like `2026-10`. It marks the one sheet for that employee and month. Do not show it to employees or reuse it for other timesheets. |
| Property setters on the Timesheet rate fields | Moves the base costing and billing amounts to permission level 1, so the Employee role cannot see them. Accounts User already has that level. |

**Projects** the employee can pick are **Open** projects of their company. A project they already booked stays on the list for that month even if it is later closed. The list puts the projects they have used most often first.

**Cost** is the ERPNext Activity Cost, not a field in the app:

1. Open **Activity Cost** and add a row per employee and activity.
2. Employee, Activity Type (**Development** or **Operational**), **Costing Rate** (internal hourly cost).
3. Set **Billing Rate** to `0`. These hours are not invoiced.

When the employee saves, the app submits one Timesheet for that month. ERPNext then writes costing rate × hours onto the line and adds it to the project as **Total Costing Amount**. The hours are added to the project's **Actual Time**. If there is no Activity Cost for that person and activity, ERPNext uses the Costing Rate on the Activity Type, which is the same for everyone. Set the per-employee rates before people start booking, or the first hours land on the project at zero.

The note the employee types ("What did you do?") is the **Description** on the Timesheet time log. It is required and limited to 500 characters.

These timesheets are forced to not billable, so a Sales Invoice does not pick them up. They are still submitted. A Salary Structure with **Salary Slip Based on Timesheet** would pull them into payroll. Leave that unchecked on the structures used for these employees.

Do not give the Employee role Submit or Cancel on Timesheet. The app submits for them, and only for their own month. Editing the same month on the desk and in the app at the same time is unsafe: the next save in the app replaces the whole month.

## For each employee

Do these before the person uses the app. A missing row here is the usual reason a calendar looks empty or a request cannot be sent.

1. **Employee** is Active, with Company and Date of Joining.
2. **User ID** points at their User, and that User has the **Employee** role. The app finds the employee through this link. No link means no hours, no calendar, no requests.
3. **Holiday List** on the employee, or rely on the company default from above.
4. **Department**, so they show up in the right team filter and inherit that department's leave approver.
5. **Leave Approver**, if it is not already set on the department.
6. **Weekly Working Hours**, submitted (not draft), with Valid From / Valid To covering the period they work. One row per weekday:
   - Working days: the target hours and the break minutes, for example Monday–Friday, 8 h, 30 min break.
   - Saturday and Sunday: a row with **0 hours**. This is what makes a weekend a free day. Punches on that day become a Workday with target 0, so the hours count as overtime. An empty weekend is not flagged as missing.
   - A weekday with **no row at all** is different: check-ins are saved but never counted. The day page tells the employee to ask HR to add that day with 0 hours.
7. **Leave Allocation** for the current period, one per type they may take (vacation, sick). Without an allocation the app can only offer Leave Without Pay, and the "days left" number does not exist. The balance shown in the app is this allocation minus approved leave. Days still waiting for approval are shown separately ("22 left · 8 waiting") and are not subtracted by Frappe until the request is approved.
8. **Shift Assignment** only if the employee works in shifts. A home office request then also needs the shift filled in.
9. **Activity Cost** for Development and for Operational, with that employee's costing rate and billing rate 0, if they will book project hours. Without it the project gets the hours and a zero cost, unless the Activity Type itself has a costing rate.

Home office has **no quota**. It is an Attendance Request with reason "Work From Home", and the only limit is that someone approves it. Check-ins on a home office day count exactly like office days.

## What the employee does

- Check in and out as usual.
- Open the workday calendar from the hours card on Home. A past working day with no check-in and no leave is marked missing; they add the punches or send vacation or sick.
- Plan ahead: the calendar goes 12 months forward. Future days take vacation, sick and home office, not check-ins.
- Select several days by tapping the first and the last (or dragging with the mouse), then choose Vacation, Sick or Home office. The panel shows how many leave days Frappe will book.
- A request that is still waiting has a dashed border. They can change it or withdraw it until it is approved. After approval only HR can change it.
- The team calendar shows the whole company, including the leave type. It is not anonymised.
- Open **Project hours**, pick a day that has check-in hours, and book a project, an activity, and a short note of what they did. The month summary at the bottom shows the projects and the hours still open.

## Onboarding checklist

- [ ] Holiday list for the year, with public holidays and weekends, set on the company
- [ ] Leave types named so sick types match the words above
- [ ] Leave period for the year
- [ ] Leave approver on the department or on the employee
- [ ] HR Addon Settings: break mechanism, update workday on check-in, skip employees without weekly hours
- [ ] Employee active, user linked, Employee role
- [ ] Weekly Working Hours submitted, including 0 h for Saturday and Sunday
- [ ] Leave allocations for vacation and sick
- [ ] Department filled in
- [ ] Open projects for the company, status Open
- [ ] Activity Types Development and Operational exist (created on the first visit to Project hours; do not rename them)
- [ ] Activity Cost per employee and activity, costing rate set, billing rate 0
- [ ] Salary structures for these employees do not use "Salary Slip Based on Timesheet"
- [ ] Employee opens the app and sees this month's hours, the calendar, and Project hours

## Things that look like bugs but are setup

| What the employee sees | Cause |
| --- | --- |
| Calendar is empty, every day is blank | No submitted Weekly Working Hours for that period. |
| Weekend check-ins do not count | That weekday has no row. Add it with 0 hours. |
| Empty Saturdays show as missing | Saturday has hours greater than 0, or the row is missing and something else created a workday. A 0-hour row is a free day. |
| Only "Leave Without Pay" can be requested | No Leave Allocation for the date. |
| Vacation on a Saturday cannot be sent | Saturday is a 0-hour day, so it is not leave. Request the working days around it; the weekend is covered without being deducted. |
| Home office request cannot be saved | No holiday list on the employee or the company. |
| "No active Employee is linked to your user" | Employee → User ID is empty or the employee is not Active. |
| Hours do not move after a normal check-in | "Update Workday on Employee Checkin" is off. Corrections made in the app still update the day. |
| Project hours says there is nothing to book | That day has no check-in hours yet. The target on the schedule is not enough. |
| A project is missing from the list | It is not Open, or it belongs to another company. |
| Only Development and Operational work can be chosen | Those are the only activity types the app offers. Other ERPNext activity types stay on the desk. |
| Project shows the hours but the cost is zero | No Activity Cost for that employee and activity, and the Activity Type costing rate is 0. |
| A salary slip includes these project hours | The salary structure has "Salary Slip Based on Timesheet". Turn it off. The timesheets stay submitted so the project cost still updates. |
