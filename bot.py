"""Slack bot — Socket Mode + slash commands.

Role-gated queries:
  /wl-me                         — your weekly log (everyone)
  /wl-me-week [YYYY-MM-DD]       — your weekly log for a past week anchor (everyone)
  /wl-worker <name>              — another worker's log (management)
  /wl-project <KEY>              — project totals + AI summary (PO of project, or management)
  /wl-subproject <EPIC-KEY>      — single Epic report (PO of parent project, or management)
  /wl-company                    — company-wide weekly report (management)

Run: `python bot.py`
Needs: SLACK_APP_TOKEN (xapp-...), SLACK_BOT_TOKEN (xoxb-...) plus the Jira/OpenAI
       env vars used by reports.py.
"""

import os
import ssl
import sys
from datetime import date

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import certifi

# Point every SSL lib (urllib, requests without certifi, websocket-client used by
# Socket Mode) at the bundled CA store. Required on macOS Python where the
# system cert store isn't wired up by default.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

import reports
from worklog_tracker import (
    normalize_user_mapping,
    find_user_by_slack,
    find_po_for_project,
    format_hours,
)


# -- env + mapping ----------------------------------------------------------

def env(name, required=True):
    v = os.environ.get(name)
    if required and not v:
        print(f"Error: {name} not set")
        sys.exit(1)
    return v


JIRA_BASE_URL = env("JIRA_BASE_URL").rstrip("/")
JIRA_EMAIL = env("JIRA_EMAIL")
JIRA_API_TOKEN = env("JIRA_API_TOKEN")
SLACK_BOT_TOKEN = env("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = env("SLACK_APP_TOKEN")
USER_MAPPING_JSON = env("USER_MAPPING")

import json
USER_MAPPING = normalize_user_mapping(json.loads(USER_MAPPING_JSON))

BLACKLIST = [p.strip() for p in os.environ.get("PROJECT_BLACKLIST", "").split(",") if p.strip()]


_ssl_ctx = ssl.create_default_context(cafile=certifi.where())
_web_client = WebClient(token=SLACK_BOT_TOKEN, ssl=_ssl_ctx)
app = App(token=SLACK_BOT_TOKEN, client=_web_client)


# -- request logging -------------------------------------------------------

def log_request(command):
    """Print a one-line audit log for every slash command invocation."""
    from datetime import datetime
    ts = datetime.now().isoformat(timespec="seconds")
    cmd = command.get("command", "?")
    args = (command.get("text") or "").strip()
    slack_id = command.get("user_id", "?")
    slack_name = command.get("user_name", "?")
    jid, entry = actor(slack_id)
    if entry:
        roles = "+".join(entry.get("roles", [])) or "worker"
        who = f"{slack_id} ({slack_name}, jira={jid[:12]}…, roles={roles})"
    else:
        who = f"{slack_id} ({slack_name}, UNMAPPED)"
    args_repr = f" {args}" if args else ""
    print(f"[{ts}] {who} → {cmd}{args_repr}")


@app.middleware
def audit_middleware(body, next):
    """Run before every listener. Logs slash commands; lets events pass through."""
    if body.get("command"):
        try:
            log_request(body)
        except Exception as e:
            print(f"[audit] log error: {e}")
    next()


# -- permission helpers -----------------------------------------------------

def actor(slack_user_id):
    """Return (jira_id, entry) for the invoking Slack user. Both None if unknown."""
    return find_user_by_slack(USER_MAPPING, slack_user_id)


def is_management(entry):
    return entry and "management" in entry.get("roles", [])


def is_po_of(entry, project_key):
    return entry and "product_owner" in entry.get("roles", []) and project_key.upper() in entry["projects"]


def deny(respond, reason):
    respond(f":no_entry: {reason}")


# -- helpers ----------------------------------------------------------------

def parse_week_anchor(text):
    """Text → date. Empty or invalid → today."""
    text = (text or "").strip()
    if not text:
        return date.today()
    if text.lower() in {"last", "last-week", "last_week", "múlt", "múltheti"}:
        from datetime import timedelta
        return date.today() - timedelta(days=7)
    try:
        return date.fromisoformat(text)
    except ValueError:
        return date.today()


def split_arg_and_week(text):
    """For commands like `/wl-project IN last` → ('IN', last-week-anchor).
    Trailing token matching 'last' or ISO date is peeled off as week anchor.
    """
    from datetime import timedelta
    text = (text or "").strip()
    if not text:
        return "", date.today()
    tokens = text.split()
    if len(tokens) >= 2:
        last = tokens[-1].lower()
        if last in {"last", "last-week", "last_week", "múlt", "múltheti"}:
            return " ".join(tokens[:-1]), date.today() - timedelta(days=7)
        try:
            anchor = date.fromisoformat(tokens[-1])
            return " ".join(tokens[:-1]), anchor
        except ValueError:
            pass
    return text, date.today()


def post_long(respond, text):
    """Slack has a ~40KB message limit; chunk if needed."""
    MAX = 3500
    if len(text) <= MAX:
        respond(text)
        return
    chunks = []
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > MAX:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    for c in chunks:
        respond(c)


# -- DM free-form message handler ------------------------------------------

@app.event("message")
def handle_dm(event, say):
    """Nudge users toward slash commands — free-form NLP not implemented yet."""
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype"):
        return
    say(HELP_TEXT)


@app.event("app_mention")
def handle_mention(event, say):
    say(HELP_TEXT)


# -- /wl-help ---------------------------------------------------------------

HELP_TEXT = (
    ":book: *Worklog bot commands*\n"
    "All week-anchored commands accept an optional trailing token:\n"
    "• `last` — previous week (Mon–Fri)\n"
    "• `YYYY-MM-DD` — any date in the target week (Monday anchor)\n"
    "• (nothing) — current week\n"
    "\n"
    "*Everyone*\n"
    "• `/wl-help` — this message\n"
    "• `/wl-projects` — list every Jira project with its PO\n"
    "• `/wl-me` — your logged time this week\n"
    "• `/wl-me-week [last|YYYY-MM-DD]` — your logged time for a chosen week\n"
    "\n"
    "*Product Owner (your projects) + Management (all)*\n"
    "• `/wl-project <KEY> [last|YYYY-MM-DD]` — project report + AI narrative\n"
    "• `/wl-subproject <EPIC-KEY> [last|YYYY-MM-DD]` — single Epic report\n"
    "\n"
    "*Management only*\n"
    "• `/wl-worker <name> [last|YYYY-MM-DD]` — another worker's log\n"
    "• `/wl-company [last|YYYY-MM-DD]` — company-wide report with per-project AI narratives\n"
    "\n"
    "_Examples:_\n"
    "• `/wl-company last` — last week's company report\n"
    "• `/wl-project IN 2026-04-13` — IN project, week of 2026-04-13\n"
    "• `/wl-worker Bence last` — Bence's past week"
)


@app.command("/wl-help")
def cmd_help(ack, respond, command):
    ack()
    respond(HELP_TEXT)


# -- /wl-projects -----------------------------------------------------------

@app.command("/wl-projects")
def cmd_projects(ack, respond, command):
    ack()
    jid, entry = actor(command["user_id"])
    if not entry:
        return deny(respond, "You are not in USER_MAPPING.")

    projects = reports.get_all_projects(JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)
    if not projects:
        return respond("No projects returned from Jira.")

    # Build a {project_key: [po_slack_names]} map via user_mapping
    po_by_project = {}
    for _jid, e in USER_MAPPING.items():
        if "product_owner" in e.get("roles", []):
            for p in e.get("projects", []):
                po_by_project.setdefault(p, []).append(e["slack"])

    lines = [f":open_file_folder: *Projects ({len(projects)})*"]
    for p in sorted(projects, key=lambda x: x["key"]):
        key = p["key"]
        name = p["name"]
        blacklisted = " :no_entry:" if key in BLACKLIST else ""
        po = po_by_project.get(key)
        po_str = f" — PO: {', '.join(f'<@{s}>' for s in po)}" if po else ""
        lines.append(f"  • `{key}` {name}{blacklisted}{po_str}")

    if BLACKLIST:
        lines.append("")
        lines.append(f":no_entry: = in `PROJECT_BLACKLIST` ({', '.join(BLACKLIST)}) — excluded from reports.")

    post_long(respond, "\n".join(lines))


# -- /wl-me -----------------------------------------------------------------

@app.command("/wl-me")
def cmd_me(ack, respond, command):
    ack()
    jid, entry = actor(command["user_id"])
    if not entry:
        return deny(respond, "You are not in USER_MAPPING. Ask an admin to add you.")
    week_start, week_end = reports.week_range()
    issues = reports.get_project_worklogs(
        JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN,
        _all_non_blacklisted_project_keys(), week_start, week_end,
    )
    my_issues = reports.filter_issues_by_worker(issues, jid)
    name = _display_name(command) or jid
    post_long(respond, reports.build_worker_query_report(name, my_issues, week_start, week_end))


# -- /wl-me-week -----------------------------------------------------------

@app.command("/wl-me-week")
def cmd_me_week(ack, respond, command):
    ack()
    jid, entry = actor(command["user_id"])
    if not entry:
        return deny(respond, "You are not in USER_MAPPING.")
    anchor = parse_week_anchor(command.get("text"))
    week_start, week_end = reports.week_range(anchor)
    issues = reports.get_project_worklogs(
        JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN,
        _all_non_blacklisted_project_keys(), week_start, week_end,
    )
    my_issues = reports.filter_issues_by_worker(issues, jid)
    name = _display_name(command) or jid
    post_long(respond, reports.build_worker_query_report(name, my_issues, week_start, week_end))


# -- /wl-worker -------------------------------------------------------------

@app.command("/wl-worker")
def cmd_worker(ack, respond, command):
    ack()
    _, entry = actor(command["user_id"])
    if not is_management(entry):
        return deny(respond, "Only Management can query other workers.")
    arg, anchor = split_arg_and_week(command.get("text"))
    needle = arg.lower()
    if not needle:
        return respond("Usage: `/wl-worker <display name or partial> [last|YYYY-MM-DD]`")
    # Match display name in USER_MAPPING against Jira user lookup
    from worklog_tracker import get_user_email  # reuse existing helper
    target_jid = None
    for jid, e in USER_MAPPING.items():
        data = reports.jira_get(
            JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN,
            "/rest/api/3/user", params={"accountId": jid},
        ) or {}
        display = (data.get("displayName") or "").lower()
        if needle in display:
            target_jid = jid
            target_name = data.get("displayName") or jid
            break
    if not target_jid:
        return respond(f"No worker found matching `{needle}`.")
    week_start, week_end = reports.week_range(anchor)
    issues = reports.get_project_worklogs(
        JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN,
        _all_non_blacklisted_project_keys(), week_start, week_end,
    )
    worker_issues = reports.filter_issues_by_worker(issues, target_jid)
    post_long(respond, reports.build_worker_query_report(target_name, worker_issues, week_start, week_end))


# -- /wl-project ------------------------------------------------------------

@app.command("/wl-project")
def cmd_project(ack, respond, command):
    ack()
    _, entry = actor(command["user_id"])
    arg, anchor = split_arg_and_week(command.get("text"))
    project_key = arg.upper()
    if not project_key:
        return respond("Usage: `/wl-project <PROJECT_KEY> [last|YYYY-MM-DD]`")
    if not (is_management(entry) or is_po_of(entry, project_key)):
        return deny(respond, f"Not authorized for project {project_key}.")

    week_start, week_end = reports.week_range(anchor)
    issues = reports.get_project_worklogs(
        JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN,
        [project_key], week_start, week_end,
    )
    # Project name + description via one-off lookup
    proj = reports.jira_get(
        JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN,
        f"/rest/api/3/project/{project_key}",
    ) or {}
    name = proj.get("name", project_key)
    desc = reports.adf_to_plain(proj.get("description") or "")
    post_long(respond, reports.build_project_report(project_key, name, desc, issues, week_start, week_end))


# -- /wl-subproject ---------------------------------------------------------

@app.command("/wl-subproject")
def cmd_subproject(ack, respond, command):
    ack()
    _, entry = actor(command["user_id"])
    arg, anchor = split_arg_and_week(command.get("text"))
    epic_key = arg.upper()
    if not epic_key:
        return respond("Usage: `/wl-subproject <EPIC_KEY> [last|YYYY-MM-DD]`")
    parent_project = epic_key.split("-")[0] if "-" in epic_key else ""
    if not (is_management(entry) or is_po_of(entry, parent_project)):
        return deny(respond, f"Not authorized for project {parent_project}.")

    week_start, week_end = reports.week_range(anchor)
    issues = reports.get_project_worklogs(
        JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN,
        [parent_project], week_start, week_end,
    )
    epic_issues = reports.filter_issues_by_epic(issues, epic_key)
    if not epic_issues:
        return respond(f"No worklogs on `{epic_key}`'s tasks this week.")
    epic_summary = next(iter(epic_issues.values())).get("epic_summary") or ""
    post_long(respond, reports.build_subproject_report(epic_key, epic_summary, epic_issues, week_start, week_end))


# -- /wl-company ------------------------------------------------------------

@app.command("/wl-company")
def cmd_company(ack, respond, command):
    ack()
    _, entry = actor(command["user_id"])
    if not is_management(entry):
        return deny(respond, "Only Management can run the company-wide report.")
    anchor = parse_week_anchor(command.get("text"))
    week_start, week_end = reports.week_range(anchor)
    all_projects = reports.get_all_projects(JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)
    keys = [p["key"] for p in all_projects if p["key"] not in BLACKLIST]
    desc_map = {p["key"]: p["description"] for p in all_projects}
    issues = reports.get_project_worklogs(
        JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, keys, week_start, week_end,
    )
    post_long(respond, reports.build_company_report(
        issues, week_start, week_end, project_descriptions=desc_map,
    ))


# -- helpers used by commands ----------------------------------------------

def _all_non_blacklisted_project_keys():
    projects = reports.get_all_projects(JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)
    return [p["key"] for p in projects if p["key"] not in BLACKLIST]


def _display_name(command):
    return command.get("user_name")


# -- entrypoint -------------------------------------------------------------

def main():
    print("Starting Slack bot in Socket Mode...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
