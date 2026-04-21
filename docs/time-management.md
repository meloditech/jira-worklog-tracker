# Time Management & Logging — BPData Onboarding

This document describes how BPData tracks work, estimates, and spend. Every team member is expected to follow these processes.

## 1. Roles

| Role | Who | Responsibility |
|---|---|---|
| **Worker** | Engineers, designers, analysts | Log time daily to Jira tickets. |
| **Product Owner (PO)** | Owns one or more projects | Maintain the backlog: ensure Epics have value estimates and tasks have time estimates. Request green-light from Management for new subprojects. |
| **Management** | Leadership | Approve subprojects, monitor company-wide spend, make staffing decisions based on actual vs. estimated time. |

Role assignment is stored in the `USER_MAPPING` env var (see [README](../README.md#3-user-mapping)) and surfaces in the Slack bot commands — a user's role determines which queries they can run.

## 2. Structure

```
Company
└── Project                       (Jira project, e.g. "LIP", "IN")
    └── Subproject                (Jira Epic)
        └── Task / Story / Bug    (Jira issue, assigned to a Worker)
            └── Worklog           (time entry with comment)
```

- **Project** = a Jira project. Each project has one PO.
- **Subproject** = an Epic inside a project. An Epic represents a scoped piece of work with an estimated business value.
- **Task** = a Jira ticket under an Epic. Has an estimated time.
- **Worklog** = an individual time entry on a task, logged by a Worker, with a required comment.

## 3. Estimation

### Task time estimates (Workers + PO)

- Every task under an Epic **must** have a time estimate (`Original estimate` in Jira) before work starts.
- Estimates are in hours or days (Jira will store as seconds).
- Workers refine their own estimates during planning. The PO signs off.

### Epic value estimates (PO)

- Every Epic **must** have an **estimated added value** (expected revenue, cost saving, strategic value in currency) in the Epic description or a custom field.
- Sum of task estimates = estimated cost (in worker-hours × rate).
- Value − Cost = expected ROI. This is the number Management uses to approve the subproject.

### Green-light process

1. PO creates Epic, fills in value estimate.
2. PO creates tasks under the Epic with time estimates.
3. PO asks Management via the Slack bot (`/wl-project <key>`) to review.
4. Management approves → work starts. Approval is tracked by moving the Epic to `In Progress`.

## 4. Logging time

### When

- **Daily**, before you leave for the day.
- The bot runs every weekday morning and DMs anyone whose previous day is under 8h (see [Daily reminder](#6-daily-reminder)).

### How

1. Pick the Jira task you worked on.
2. Click **Log work**.
3. Enter time spent (`3h`, `30m`, `1h 15m`).
4. Set **Date started** if you're logging a past day.
5. **Add a comment** — one sentence describing what you did. This is what the PO and Management read.

### Rules

- **Log to the most specific ticket.** If a subtask exists, log there, not on the parent.
- **Every worklog needs a comment.** Empty comments are rejected in weekly reports. Good examples:
  - "Implemented user search endpoint and wrote tests."
  - "Reviewed PO feedback, reworked homepage copy."
  - Bad: "work", "coding", "meeting" (no context).
- **One worklog per task per day.** Consolidate multiple chunks into one entry.
- **Round to 15 minutes.** `0:45` yes, `0:37` no.
- **Meetings:** log to a dedicated "Meetings & comms" task in the relevant project. If the meeting spans multiple projects, split the time.
- **Learning / admin:** log to the "Overhead" project (HR and training). Max 10% of your weekly time.

### Out of Office (OoO)

- Mark vacation and appointments in **Google Calendar** as "Out of Office" events (Google's native OoO event type).
  - All-day OoO → counted as a full vacation day (8h credited automatically).
  - Timed OoO ≥ 6h covering a working day → counted as vacation for that day.
  - Timed OoO < 6h (e.g., doctor appointment) → **not** auto-credited. You still log 8h total that day.
- The worklog bot reads your calendar and skips the "you're missing hours" warning for vacation days.
- Sick days: create an all-day OoO event titled `Sick`.

## 5. Reports

### Daily (automated — worker DM)

- Every weekday morning, each worker gets a Slack DM summarizing the previous workday:
  - Total logged time.
  - Per-ticket breakdown.
  - In-progress tickets (Epics excluded).
  - Deadline-tracked tickets.
  - Any OoO events on that day.
  - Warning if under 8h (accounting for OoO).

### Weekly (automated)

- **Per Worker (Friday afternoon):** daily breakdown Mon–Fri, per-project split, total vs. 40h target, AI-generated narrative of what they worked on (see §7).
- **Per Project (PO) — Friday afternoon:** every PO gets a DM per project they own:
  - Total hours logged this week.
  - Per-Epic (subproject) split.
  - Per-worker contribution.
  - AI-generated summary combining ticket descriptions and worklog comments.
- **Company-wide (Management) — Friday afternoon:** one DM per Management member:
  - Total company hours.
  - Per-project split (budget vs. actual).
  - Per-worker summary.
  - AI-generated narrative of company focus areas for the week.

### On-demand (Slack bot)

See [`/wl-*` commands](#6-slack-bot). PO and Management can query any time.

## 6. Slack bot

Add the bot to your DMs or any channel. Commands:

| Command | Who | What it does |
|---|---|---|
| `/wl-projects` | Everyone | List every Jira project with its PO (if configured). |
| `/wl-me` | Everyone | Your logged time this week, by ticket. |
| `/wl-me-week [YYYY-MM-DD]` | Everyone | Your weekly summary for a past week (Monday date). |
| `/wl-worker <name>` | Management | Named worker's time this week. |
| `/wl-project <KEY>` | PO (own) / Management | Project totals, per-Epic breakdown, per-worker breakdown, AI summary. |
| `/wl-subproject <EPIC-KEY>` | PO (own) / Management | One Epic: total time, per-worker, per-task, AI summary. |
| `/wl-company` | Management | Company-wide weekly totals + AI summary. |

Permission errors ("Not authorized for this project") mean you're not listed as PO for that project in `USER_MAPPING` and you're not Management. Ask an admin to update it.

### Daily reminder

The bot warns you if yesterday's log is under 8h (adjusted for OoO). To catch up:

1. Add or extend a worklog for the previous day.
2. The next run will pick it up — no need to dismiss the message.

## 7. AI summaries

Weekly reports (worker, project, company) include an AI-generated narrative paragraph. The model (gpt-5-mini) reads:

- The Jira ticket's `description` field.
- All worklog comments on that ticket for the period.
- The project's description (if configured).

It produces a 2–5 sentence plain-English summary of what actually happened, not just which tickets moved.

**Rule of thumb:** the better your worklog comments, the better the summary. "Built login flow" beats "work".

## 8. FAQ

**Q: I forgot to log Monday — what do I do?**
A: Open Jira, find the ticket, click Log work, set "Date started" to Monday. The next daily reminder will see it.

**Q: My task estimate was wrong — do I update it?**
A: Yes. Update `Remaining estimate` in Jira. The actual spent time is tracked separately via worklogs.

**Q: I worked overtime — where do those hours go?**
A: Log them against the task you worked on. The weekly summary shows total vs. 40h; above is fine occasionally.

**Q: A task spans two Epics — which one?**
A: Pick the primary Epic and log there. If it's genuinely 50/50, create separate sub-tasks under each Epic.

**Q: Can I log to an Epic directly?**
A: No. Epics are containers. Log to the child task.

**Q: The bot said I logged 0h but I did log — what gives?**
A: Jira API takes a few minutes to index. Wait 5 min, then ping `/wl-me` to recheck. If it still shows 0, ensure the worklog date matches the actual day.

---

**Questions?** Ping Management on Slack or read the [README](../README.md) for admin/technical details.
