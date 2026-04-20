#!/usr/bin/env python3
"""
Jira Worklog Tracker
Checks daily logged hours per person and sends Slack DM if < 8 hours.
"""

import argparse
import json
import os
import ssl
import sys
from datetime import date, datetime, timedelta, timezone

import certifi
import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as google_build
    from googleapiclient.errors import HttpError as GoogleHttpError
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def get_env(name):
    value = os.environ.get(name)
    if not value:
        print(f"Error: {name} environment variable is not set")
        sys.exit(1)
    return value


def get_jira_worklogs(base_url, email, api_token, date_str, blacklisted_projects=None):
    """Fetch all worklogs for the given date across all projects."""
    auth = (email, api_token)
    headers = {"Accept": "application/json"}

    # Search for issues with worklogs on the target date
    jql = f'worklogDate = "{date_str}"'
    if blacklisted_projects:
        excluded = ", ".join(f'"{p}"' for p in blacklisted_projects)
        jql += f" AND project NOT IN ({excluded})"
    max_results = 100
    all_issues = []
    next_page_token = None

    while True:
        body = {
            "jql": jql,
            "maxResults": max_results,
            "fields": ["key", "summary"],
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        response = requests.post(
            f"{base_url}/rest/api/3/search/jql",
            json=body,
            auth=auth,
            headers={**headers, "Content-Type": "application/json"},
        )
        if not response.ok:
            print(f"Jira search error {response.status_code}: {response.text}")
        response.raise_for_status()
        data = response.json()
        all_issues.extend(data["issues"])

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    print(f"Found {len(all_issues)} issues with worklogs on {date_str}")

    # Collect worklogs per person
    # Structure: {account_id: {"name": str, "tickets": {key: {"summary": str, "seconds": int}}, "total_seconds": int}}
    people = {}

    for issue in all_issues:
        issue_key = issue["key"]
        issue_summary = issue["fields"]["summary"]

        # Get worklogs for this issue
        wl_response = requests.get(
            f"{base_url}/rest/api/3/issue/{issue_key}/worklog",
            auth=auth,
            headers=headers,
        )
        wl_response.raise_for_status()
        worklogs = wl_response.json().get("worklogs", [])

        for wl in worklogs:
            # Filter for target date
            started = wl["started"][:10]  # "2024-01-15T..." -> "2024-01-15"
            if started != date_str:
                continue

            account_id = wl["author"]["accountId"]
            display_name = wl["author"]["displayName"]
            time_spent_seconds = wl["timeSpentSeconds"]

            if account_id not in people:
                people[account_id] = {
                    "name": display_name,
                    "tickets": {},
                    "total_seconds": 0,
                }

            if issue_key not in people[account_id]["tickets"]:
                people[account_id]["tickets"][issue_key] = {
                    "summary": issue_summary,
                    "seconds": 0,
                }

            people[account_id]["tickets"][issue_key]["seconds"] += time_spent_seconds
            people[account_id]["total_seconds"] += time_spent_seconds

    return people


def get_active_issues(base_url, email, api_token, account_id, blacklisted_projects=None):
    """Fetch issues assigned to a user that are In Progress or To Do."""
    auth = (email, api_token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    jql = f'assignee = "{account_id}" AND statusCategory IN ("To Do", "In Progress")'
    if blacklisted_projects:
        excluded = ", ".join(f'"{p}"' for p in blacklisted_projects)
        jql += f" AND project NOT IN ({excluded})"
    jql += " ORDER BY status ASC"
    all_issues = []
    next_page_token = None

    while True:
        body = {
            "jql": jql,
            "maxResults": 50,
            "fields": ["key", "summary", "status", "duedate", "issuetype"],
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        response = requests.post(
            f"{base_url}/rest/api/3/search/jql",
            json=body,
            auth=auth,
            headers=headers,
        )
        if not response.ok:
            print(f"Jira active issues error {response.status_code}: {response.text}")
            return []
        data = response.json()
        all_issues.extend(data["issues"])

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return all_issues


def get_user_email(base_url, email, api_token, account_id):
    """Look up a Jira user's email address by account ID."""
    auth = (email, api_token)
    headers = {"Accept": "application/json"}
    response = requests.get(
        f"{base_url}/rest/api/3/user",
        params={"accountId": account_id},
        auth=auth,
        headers=headers,
    )
    if not response.ok:
        print(f"Jira user lookup error for {account_id}: {response.status_code} {response.text}")
        return None
    return response.json().get("emailAddress")


def build_calendar_service(service_account_info, user_email):
    """Build a Google Calendar API client impersonating the given user."""
    creds = service_account.Credentials.from_service_account_info(
        service_account_info, scopes=GOOGLE_CALENDAR_SCOPES
    ).with_subject(user_email)
    return google_build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_ooo_events(service_account_info, user_email, start_date, end_date):
    """
    Fetch OoO events from Google Calendar for a date range (inclusive).

    Returns dict with:
      - vacation_seconds: total full-day OoO seconds (8h per day, clipped to range)
      - vacation_days: list of date strings that were full-day OoO
      - partial_events: list of {date, summary, seconds} for timed OoO events
    """
    result = {"vacation_seconds": 0, "vacation_days": [], "partial_events": []}
    if not GOOGLE_AVAILABLE or not service_account_info or not user_email:
        return result

    try:
        service = build_calendar_service(service_account_info, user_email)
    except Exception as e:
        print(f"  Google auth failed for {user_email}: {e}")
        return result

    start_dt = date.fromisoformat(start_date)
    end_dt = date.fromisoformat(end_date)
    # Calendar API window: [start 00:00, end+1 00:00) in UTC-ish. Use wide RFC3339 with Z.
    time_min = datetime.combine(start_dt, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    time_max = datetime.combine(end_dt + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).isoformat()

    try:
        page_token = None
        events = []
        while True:
            resp = service.events().list(
                calendarId=user_email,
                eventTypes=["outOfOffice"],
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                pageToken=page_token,
            ).execute()
            events.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except GoogleHttpError as e:
        print(f"  Calendar fetch failed for {user_email}: {e}")
        return result
    except Exception as e:
        print(f"  Calendar fetch error for {user_email}: {e}")
        return result

    vacation_days_set = set()
    for ev in events:
        summary = ev.get("summary", "OoO")
        start = ev.get("start", {})
        end = ev.get("end", {})

        if "date" in start:
            # All-day event → vacation. Iterate each day in [start, end).
            ev_start = date.fromisoformat(start["date"])
            ev_end = date.fromisoformat(end["date"])  # exclusive
            cur = max(ev_start, start_dt)
            last = min(ev_end - timedelta(days=1), end_dt)
            while cur <= last:
                # Weekday only — weekends don't count as vacation for 8h requirement
                if cur.weekday() < 5:
                    vacation_days_set.add(cur.isoformat())
                cur += timedelta(days=1)
        elif "dateTime" in start:
            # Timed event → partial OoO (appointment)
            ev_start = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
            ev_end = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00"))
            seconds = int((ev_end - ev_start).total_seconds())
            if seconds <= 0:
                continue
            result["partial_events"].append({
                "date": ev_start.date().isoformat(),
                "summary": summary,
                "seconds": seconds,
            })

    result["vacation_days"] = sorted(vacation_days_set)
    result["vacation_seconds"] = len(vacation_days_set) * 8 * 3600
    return result


def format_hours(seconds):
    """Format seconds as hours and minutes."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


def build_slack_message(person_data, date_str, active_issues=None, ooo_info=None):
    """Build a Slack message with worklog summary."""
    total = person_data["total_seconds"]
    vacation_seconds = 0
    partial_today = []
    if ooo_info:
        is_vacation_day = date_str in ooo_info.get("vacation_days", [])
        if is_vacation_day:
            vacation_seconds = 8 * 3600
        partial_today = [p for p in ooo_info.get("partial_events", []) if p["date"] == date_str]

    effective = total + vacation_seconds
    is_ok = effective >= 8 * 3600

    if vacation_seconds > 0 and total == 0:
        lines = [
            f":palm_tree: Szia! Az előző munkanap (*{date_str}*) szabadság volt. Jó pihenést! :sunny:",
        ]
    elif is_ok:
        if vacation_seconds > 0:
            lines = [
                f":white_check_mark: Szia! Az előző munkanapra (*{date_str}*) *{format_hours(total)}* van logolva + *{format_hours(vacation_seconds)}* szabadság. Rendben!",
            ]
        else:
            lines = [
                f":white_check_mark: Szia! Az előző munkanapra (*{date_str}*) összesen *{format_hours(total)}* van logolva a Jirában. Szép munka!",
            ]
    else:
        missing_seconds = (8 * 3600) - effective
        lines = [
            f":exclamation: Szia! Az előző munkanapra (*{date_str}*) összesen *{format_hours(total)}* van logolva a Jirában.",
            f"*{format_hours(missing_seconds)}* hiányzik a 8 órából.",
        ]

    if partial_today:
        lines.append("")
        lines.append("*OoO események (munkanapba beszámítva):*")
        for ev in partial_today:
            lines.append(f"  • {ev['summary']} — {format_hours(ev['seconds'])}")

    if person_data["tickets"]:
        lines.append("")
        lines.append("*Logolt munkák:*")
        for ticket_key, ticket_data in person_data["tickets"].items():
            lines.append(
                f"  • `{ticket_key}` {ticket_data['summary']} — {format_hours(ticket_data['seconds'])}"
            )

    if active_issues:
        in_progress = []
        with_deadline = []
        today = date.today()

        for issue in active_issues:
            key = issue["key"]
            summary = issue["fields"]["summary"]
            category = issue["fields"]["status"]["statusCategory"]["name"]
            duedate = issue["fields"].get("duedate")

            if category == "In Progress" and issue["fields"]["issuetype"]["name"] != "Epic":
                in_progress.append(f"  • `{key}` {summary}")

            if duedate:
                due = date.fromisoformat(duedate)
                days_left = (due - today).days
                if days_left < 0:
                    days_str = f":rotating_light: *{abs(days_left)} napja lejárt!*"
                elif days_left == 0:
                    days_str = ":warning: *ma!*"
                else:
                    days_str = f"{days_left} nap múlva"
                with_deadline.append(f"  • `{key}` {summary} — határidő: {duedate} ({days_str})")

        if in_progress:
            lines.append("")
            lines.append("*In Progress feladatok:*")
            lines.extend(in_progress)
        if with_deadline:
            lines.append("")
            lines.append("*Határidős feladatok:*")
            lines.extend(with_deadline)

    if not is_ok:
        lines.append("")
        lines.append("Kérlek pótold a hiányzó órákat! :pray:")

    return "\n".join(lines)


def send_slack_dm(slack_client, slack_user_id, message, dry_run=False):
    """Send a direct message to a Slack user."""
    if dry_run:
        print(f"  [DRY RUN] Would send DM to Slack user {slack_user_id}")
        print(f"  Message:\n{message}\n")
        return

    try:
        # Open a DM channel
        response = slack_client.conversations_open(users=[slack_user_id])
        channel_id = response["channel"]["id"]

        # Send message
        slack_client.chat_postMessage(channel=channel_id, text=message)
        print(f"  Sent DM to Slack user {slack_user_id}")
    except SlackApiError as e:
        print(f"  Error sending DM to {slack_user_id}: {e.response['error']}")


def get_weekly_worklogs(base_url, email, api_token, week_start, week_end, blacklisted_projects=None):
    """Fetch worklogs for a full week (Mon-Fri). Returns per-person and per-project stats."""
    auth = (email, api_token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    jql = f'worklogDate >= "{week_start}" AND worklogDate <= "{week_end}"'
    if blacklisted_projects:
        excluded = ", ".join(f'"{p}"' for p in blacklisted_projects)
        jql += f" AND project NOT IN ({excluded})"

    all_issues = []
    next_page_token = None

    while True:
        body = {"jql": jql, "maxResults": 100, "fields": ["key", "summary", "project"]}
        if next_page_token:
            body["nextPageToken"] = next_page_token
        response = requests.post(
            f"{base_url}/rest/api/3/search/jql", json=body, auth=auth, headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        all_issues.extend(data["issues"])
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    print(f"Found {len(all_issues)} issues with worklogs in week {week_start} - {week_end}")

    # {account_id: {"name": str, "total_seconds": int, "daily": {date_str: seconds}, "projects": {project_name: seconds}}}
    people = {}
    # {project_name: {"total_seconds": int, "tickets": {key: {"summary": str, "seconds": int}}}}
    projects = {}

    start_dt = date.fromisoformat(week_start)
    end_dt = date.fromisoformat(week_end)

    for issue in all_issues:
        issue_key = issue["key"]
        issue_summary = issue["fields"]["summary"]
        project_name = issue["fields"]["project"]["name"]

        wl_response = requests.get(
            f"{base_url}/rest/api/3/issue/{issue_key}/worklog",
            auth=(email, api_token),
            headers={"Accept": "application/json"},
        )
        wl_response.raise_for_status()
        worklogs = wl_response.json().get("worklogs", [])

        for wl in worklogs:
            started_date = wl["started"][:10]
            wl_date = date.fromisoformat(started_date)
            if wl_date < start_dt or wl_date > end_dt:
                continue

            account_id = wl["author"]["accountId"]
            display_name = wl["author"]["displayName"]
            seconds = wl["timeSpentSeconds"]

            if account_id not in people:
                people[account_id] = {"name": display_name, "total_seconds": 0, "daily": {}, "projects": {}}
            people[account_id]["total_seconds"] += seconds
            people[account_id]["daily"][started_date] = people[account_id]["daily"].get(started_date, 0) + seconds
            people[account_id]["projects"][project_name] = people[account_id]["projects"].get(project_name, 0) + seconds

            if project_name not in projects:
                projects[project_name] = {"total_seconds": 0, "tickets": {}}
            projects[project_name]["total_seconds"] += seconds
            if issue_key not in projects[project_name]["tickets"]:
                projects[project_name]["tickets"][issue_key] = {"summary": issue_summary, "seconds": 0}
            projects[project_name]["tickets"][issue_key]["seconds"] += seconds

    return people, projects


def build_weekly_summary_message(person_data, week_start, week_end, ooo_info=None):
    """Build weekly summary Slack message for one person."""
    total = person_data["total_seconds"]
    expected = 5 * 8 * 3600  # 40h

    vacation_days = set(ooo_info.get("vacation_days", [])) if ooo_info else set()
    vacation_seconds = len(vacation_days) * 8 * 3600
    partial_events = ooo_info.get("partial_events", []) if ooo_info else []
    partial_by_day = {}
    for ev in partial_events:
        partial_by_day.setdefault(ev["date"], 0)
        partial_by_day[ev["date"]] += ev["seconds"]

    lines = [
        f":bar_chart: *Heti összesítő ({week_start} – {week_end})*",
        f"Összesen logolt idő: *{format_hours(total)}* / {format_hours(expected)}",
    ]
    if vacation_seconds:
        lines.append(f"Szabadság: *{format_hours(vacation_seconds)}* ({len(vacation_days)} nap)")

    # Daily breakdown
    lines.append("")
    lines.append("*Napi bontás:*")
    current = date.fromisoformat(week_start)
    end = date.fromisoformat(week_end)
    day_names = ["Hétfő", "Kedd", "Szerda", "Csütörtök", "Péntek"]
    while current <= end:
        ds = current.strftime("%Y-%m-%d")
        day_seconds = person_data["daily"].get(ds, 0)
        day_name = day_names[current.weekday()] if current.weekday() < 5 else current.strftime("%A")
        is_vacation = ds in vacation_days
        if is_vacation:
            icon = ":palm_tree:"
            suffix = " (szabadság)"
        else:
            icon = ":white_check_mark:" if day_seconds >= 8 * 3600 else ":x:"
            suffix = ""
            if ds in partial_by_day:
                suffix = f" (+{format_hours(partial_by_day[ds])} OoO)"
        lines.append(f"  {icon} {day_name} ({ds}): *{format_hours(day_seconds)}*{suffix}")
        current += timedelta(days=1)

    # Per-project breakdown
    if person_data["projects"]:
        lines.append("")
        lines.append("*Projektek szerinti bontás:*")
        sorted_projects = sorted(person_data["projects"].items(), key=lambda x: x[1], reverse=True)
        for proj_name, proj_seconds in sorted_projects:
            lines.append(f"  • {proj_name}: *{format_hours(proj_seconds)}*")

    effective = total + vacation_seconds
    if effective < expected:
        missing = expected - effective
        lines.append("")
        lines.append(f":warning: *{format_hours(missing)}* hiányzik a heti 40 órából.")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Jira Worklog Tracker")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print messages instead of sending Slack DMs",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Date to check (YYYY-MM-DD format, defaults to today)",
    )
    parser.add_argument(
        "--weekly-summary",
        action="store_true",
        help="Send weekly summary instead of daily check",
    )
    args = parser.parse_args()

    # Config from environment
    jira_base_url = get_env("JIRA_BASE_URL").rstrip("/")
    jira_email = get_env("JIRA_EMAIL")
    jira_api_token = get_env("JIRA_API_TOKEN")
    slack_bot_token = get_env("SLACK_BOT_TOKEN")
    user_mapping_json = get_env("USER_MAPPING")

    try:
        user_mapping = json.loads(user_mapping_json)
    except json.JSONDecodeError as e:
        print(f"Error parsing USER_MAPPING JSON: {e}")
        sys.exit(1)

    # Optional: Google service account JSON for OoO event detection
    google_sa_info = None
    google_sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if google_sa_json:
        if not GOOGLE_AVAILABLE:
            print("Warning: GOOGLE_SERVICE_ACCOUNT_JSON set but google libs not installed.")
        else:
            try:
                google_sa_info = json.loads(google_sa_json)
            except json.JSONDecodeError as e:
                print(f"Error parsing GOOGLE_SERVICE_ACCOUNT_JSON: {e}")
                sys.exit(1)

    # Target date (default: previous workday — Mon→Fri, otherwise yesterday)
    if args.date:
        date_str = args.date
    else:
        today = date.today()
        weekday = today.weekday()  # 0=Mon, 6=Sun
        if weekday == 0:  # Monday → check Friday
            target = today - timedelta(days=3)
        elif weekday == 6:  # Sunday → check Friday
            target = today - timedelta(days=2)
        else:
            target = today - timedelta(days=1)
        date_str = target.strftime("%Y-%m-%d")

    # Project blacklist (optional, comma-separated)
    blacklist_str = os.environ.get("PROJECT_BLACKLIST", "")
    blacklisted_projects = [p.strip() for p in blacklist_str.split(",") if p.strip()] if blacklist_str else []
    if blacklisted_projects:
        print(f"Blacklisted projects: {', '.join(blacklisted_projects)}")

    # Initialize Slack client
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    slack_client = WebClient(token=slack_bot_token, ssl=ssl_context)

    if args.weekly_summary:
        # Weekly summary mode — calculate Mon-Fri of the current week
        today = date.today()
        if args.date:
            today = date.fromisoformat(args.date)
        # Find Monday of this week
        monday = today - timedelta(days=today.weekday())
        friday = monday + timedelta(days=4)
        week_start = monday.strftime("%Y-%m-%d")
        week_end = friday.strftime("%Y-%m-%d")

        print(f"Generating weekly summary for {week_start} - {week_end}...")

        people, projects = get_weekly_worklogs(
            jira_base_url, jira_email, jira_api_token, week_start, week_end, blacklisted_projects
        )

        print(f"\nWeekly totals:")
        for account_id, data in people.items():
            print(f"  {data['name']}: {format_hours(data['total_seconds'])}")

        for jira_id, slack_id in user_mapping.items():
            person = people.get(jira_id)

            ooo_info = None
            if google_sa_info:
                user_email = get_user_email(jira_base_url, jira_email, jira_api_token, jira_id)
                if user_email:
                    ooo_info = get_ooo_events(google_sa_info, user_email, week_start, week_end)

            if person:
                name = person["name"]
                message = build_weekly_summary_message(person, week_start, week_end, ooo_info)
            else:
                name = jira_id
                empty_data = {"total_seconds": 0, "daily": {}, "projects": {}}
                message = build_weekly_summary_message(empty_data, week_start, week_end, ooo_info)

            print(f"\n{name}: {format_hours(person['total_seconds'] if person else 0)}")
            send_slack_dm(slack_client, slack_id, message, dry_run=args.dry_run)

        print(f"\nWeekly summary sent to {len(user_mapping)} people.")
        return

    print(f"Checking worklogs for {date_str}...")

    # Fetch worklogs from Jira
    people = get_jira_worklogs(jira_base_url, jira_email, jira_api_token, date_str, blacklisted_projects)

    print(f"\nFound {len(people)} people with worklogs:")
    for account_id, data in people.items():
        print(f"  {data['name']}: {format_hours(data['total_seconds'])}")

    # Check each mapped user
    notified = 0
    skipped = 0

    for jira_id, slack_id in user_mapping.items():
        person = people.get(jira_id)
        total_seconds = person["total_seconds"] if person else 0

        active_issues = get_active_issues(jira_base_url, jira_email, jira_api_token, jira_id, blacklisted_projects)

        ooo_info = None
        if google_sa_info:
            user_email = get_user_email(jira_base_url, jira_email, jira_api_token, jira_id)
            if user_email:
                ooo_info = get_ooo_events(google_sa_info, user_email, date_str, date_str)

        vacation_seconds = 0
        if ooo_info and date_str in ooo_info.get("vacation_days", []):
            vacation_seconds = 8 * 3600
        is_ok = (total_seconds + vacation_seconds) >= 8 * 3600

        if person:
            name = person["name"]
            message = build_slack_message(person, date_str, active_issues, ooo_info)
        else:
            name = jira_id
            person_data = {"total_seconds": 0, "tickets": {}}
            message = build_slack_message(person_data, date_str, active_issues, ooo_info)

        status = "OK" if is_ok else "UNDER 8h"
        vac_note = f" (+{format_hours(vacation_seconds)} vacation)" if vacation_seconds else ""
        print(f"\n{name}: {format_hours(total_seconds)}{vac_note} - {status}")
        send_slack_dm(slack_client, slack_id, message, dry_run=args.dry_run)

        if is_ok:
            skipped += 1
        else:
            notified += 1

    print(f"\nDone! Under 8h: {notified}, OK: {skipped}")


if __name__ == "__main__":
    main()
