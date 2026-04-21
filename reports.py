"""Project / subproject / worker reports with AI-generated narrative summaries.

Imported by both `worklog_tracker.py` (scheduled cron) and `bot.py` (Slack bot).
"""

import hashlib
import json as _json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


DEFAULT_OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")

# In-memory cache: {sha256(normalized inputs) → response string}.
# Survives for the lifetime of the process — cron runs start fresh, bot
# process accumulates hits across commands.
_LLM_CACHE = {}
_LLM_CACHE_STATS = {"hits": 0, "misses": 0}


# ---------------------------------------------------------------------------
# Jira fetchers (epic-aware)
# ---------------------------------------------------------------------------

def jira_get(base_url, email, api_token, path, params=None):
    """GET a Jira REST API path. Returns parsed JSON or None on failure."""
    resp = requests.get(
        f"{base_url}{path}",
        params=params or {},
        auth=(email, api_token),
        headers={"Accept": "application/json"},
    )
    if not resp.ok:
        print(f"Jira GET {path} error {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()


def jira_search(base_url, email, api_token, jql, fields):
    """Run a JQL search, pagination-aware. Returns list of issues."""
    auth = (email, api_token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    all_issues = []
    next_token = None
    while True:
        body = {"jql": jql, "maxResults": 100, "fields": fields}
        if next_token:
            body["nextPageToken"] = next_token
        resp = requests.post(
            f"{base_url}/rest/api/3/search/jql",
            json=body, auth=auth, headers=headers,
        )
        if not resp.ok:
            print(f"Jira search error {resp.status_code}: {resp.text[:200]}")
            return all_issues
        data = resp.json()
        all_issues.extend(data.get("issues", []))
        next_token = data.get("nextPageToken")
        if not next_token:
            break
    return all_issues


def adf_to_plain(adf):
    """Crude Atlassian Document Format → plain text. Good enough for summaries."""
    if not adf:
        return ""
    if isinstance(adf, str):
        return adf
    out = []
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                out.append(node.get("text", ""))
            for child in node.get("content", []) or []:
                walk(child)
            if node.get("type") in {"paragraph", "heading", "listItem"}:
                out.append("\n")
        elif isinstance(node, list):
            for n in node:
                walk(n)
    walk(adf)
    return "".join(out).strip()


def get_project_worklogs(base_url, email, api_token, project_keys, start_date, end_date):
    """Fetch all worklogs in a date window for a list of Jira project keys.

    Returns dict keyed by issue_key → {
        "summary": str,
        "description": str (plain),
        "project_key": str,
        "project_name": str,
        "epic_key": str | None,
        "epic_summary": str | None,
        "worklogs": [{"author_id", "author_name", "started", "seconds", "comment"}],
    }
    """
    project_keys = [p.upper() for p in project_keys]
    keys_jql = ", ".join(f'"{k}"' for k in project_keys)
    jql = (
        f'project IN ({keys_jql}) AND '
        f'worklogDate >= "{start_date}" AND worklogDate <= "{end_date}"'
    )
    issues = jira_search(
        base_url, email, api_token, jql,
        fields=["key", "summary", "project", "parent", "issuetype", "description"],
    )

    out = {}
    for issue in issues:
        f = issue["fields"]
        parent = f.get("parent") or {}
        parent_fields = parent.get("fields") or {}
        parent_type = (parent_fields.get("issuetype") or {}).get("name", "")
        epic_key = parent.get("key") if parent_type == "Epic" else None
        epic_summary = parent_fields.get("summary") if epic_key else None
        out[issue["key"]] = {
            "summary": f.get("summary") or "",
            "description": adf_to_plain(f.get("description")),
            "project_key": (f.get("project") or {}).get("key") or "",
            "project_name": (f.get("project") or {}).get("name") or "",
            "epic_key": epic_key,
            "epic_summary": epic_summary,
            "issuetype": (f.get("issuetype") or {}).get("name") or "",
            "worklogs": [],
        }

    # Fetch worklogs per issue (Jira search doesn't inline them)
    start_dt = date.fromisoformat(start_date)
    end_dt = date.fromisoformat(end_date)
    for key in list(out.keys()):
        wl = jira_get(
            base_url, email, api_token,
            f"/rest/api/3/issue/{key}/worklog",
        ) or {}
        for entry in wl.get("worklogs", []):
            started_day = entry.get("started", "")[:10]
            if not started_day:
                continue
            try:
                d = date.fromisoformat(started_day)
            except ValueError:
                continue
            if d < start_dt or d > end_dt:
                continue
            author = entry.get("author") or {}
            out[key]["worklogs"].append({
                "author_id": author.get("accountId"),
                "author_name": author.get("displayName") or "",
                "started": started_day,
                "seconds": entry.get("timeSpentSeconds", 0),
                "comment": adf_to_plain(entry.get("comment")),
            })

    # Drop issues that had no worklogs in window (JQL matched but worklog day fell outside cached date)
    return {k: v for k, v in out.items() if v["worklogs"]}


def get_all_projects(base_url, email, api_token):
    """List all Jira projects. Returns [{key, name}]."""
    data = jira_get(base_url, email, api_token, "/rest/api/3/project/search", params={"maxResults": 200}) or {}
    return [
        {"key": p["key"], "name": p["name"], "description": adf_to_plain(p.get("description") or "")}
        for p in data.get("values", [])
    ]


def format_hours(seconds):
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


# ---------------------------------------------------------------------------
# Aggregators
# ---------------------------------------------------------------------------

def aggregate_by_epic(issues):
    """Group issue-level data by epic. Returns {epic_key or '(no epic)': {
        "epic_summary", "total_seconds", "issues": {issue_key: {...}}}}."""
    epics = defaultdict(lambda: {"epic_summary": None, "total_seconds": 0, "issues": {}})
    for key, issue in issues.items():
        epic_key = issue["epic_key"] or "(no epic)"
        epics[epic_key]["epic_summary"] = issue["epic_summary"] or "Uncategorized (no Epic parent)"
        issue_seconds = sum(w["seconds"] for w in issue["worklogs"])
        epics[epic_key]["total_seconds"] += issue_seconds
        epics[epic_key]["issues"][key] = {
            "summary": issue["summary"],
            "seconds": issue_seconds,
            "worklogs": issue["worklogs"],
        }
    return dict(epics)


def aggregate_by_worker(issues):
    """Group worklogs by worker. Returns {account_id: {name, total_seconds, issues: {key: seconds}}}."""
    workers = defaultdict(lambda: {"name": "", "total_seconds": 0, "issues": defaultdict(int)})
    for key, issue in issues.items():
        for wl in issue["worklogs"]:
            aid = wl["author_id"]
            if not aid:
                continue
            workers[aid]["name"] = wl["author_name"]
            workers[aid]["total_seconds"] += wl["seconds"]
            workers[aid]["issues"][key] += wl["seconds"]
    # dict-ify inner defaultdict
    for w in workers.values():
        w["issues"] = dict(w["issues"])
    return dict(workers)


def aggregate_by_project(issues):
    """Group by project key. Returns {project_key: {name, total_seconds, issues: {...}}}."""
    projects = defaultdict(lambda: {"name": "", "total_seconds": 0, "issues": {}})
    for key, issue in issues.items():
        pk = issue["project_key"]
        projects[pk]["name"] = issue["project_name"]
        issue_seconds = sum(w["seconds"] for w in issue["worklogs"])
        projects[pk]["total_seconds"] += issue_seconds
        projects[pk]["issues"][key] = issue
    return dict(projects)


# ---------------------------------------------------------------------------
# OpenAI summarizer
# ---------------------------------------------------------------------------

def _cache_key(context_label, items, project_descriptions, model):
    """Stable hash over the inputs that affect model output."""
    sorted_items = sorted(
        items,
        key=lambda it: (
            it.get("ticket") or "",
            it.get("worker") or "",
            it.get("hours") or 0,
            it.get("ticket_summary") or "",
            tuple(it.get("comments") or []),
        ),
    )
    canon = {
        "label": context_label,
        "items": sorted_items,
        "desc": project_descriptions or {},
        "model": model or DEFAULT_OPENAI_MODEL,
    }
    return hashlib.sha256(
        _json.dumps(canon, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def llm_cache_stats():
    return dict(_LLM_CACHE_STATS, size=len(_LLM_CACHE))


def llm_cache_clear():
    _LLM_CACHE.clear()
    _LLM_CACHE_STATS["hits"] = 0
    _LLM_CACHE_STATS["misses"] = 0


def llm_summarize(context_label, items, project_descriptions=None, model=None):
    """Generate a 2-5 sentence narrative summary of a period of work.

    Identical input (tickets + worklogs + description + model unchanged)
    is cached in-memory → no repeat OpenAI call, no token cost.

    Args:
        context_label: e.g. "project LIP week 2026-04-13..2026-04-17"
        items: list of dicts {ticket, ticket_summary, ticket_description,
               worker, hours, comments: [str]}
        project_descriptions: optional {project_key: description} map
        model: override OPENAI_MODEL

    Returns: string summary, or a fallback message on failure.
    """
    if not OPENAI_AVAILABLE:
        return "(AI summary unavailable: openai package not installed.)"
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return "(AI summary unavailable: OPENAI_API_KEY not set.)"

    key = _cache_key(context_label, items, project_descriptions, model)
    if key in _LLM_CACHE:
        _LLM_CACHE_STATS["hits"] += 1
        print(f"  [llm-cache HIT] {context_label}")
        return _LLM_CACHE[key]
    _LLM_CACHE_STATS["misses"] += 1

    # Build compact context
    lines = [f"Context: {context_label}"]
    if project_descriptions:
        for k, v in project_descriptions.items():
            if v:
                lines.append(f"Project {k}: {v[:500]}")
    lines.append("")
    lines.append("Work log entries:")
    for it in items[:200]:  # cap to avoid huge payloads
        hours = it.get("hours", 0)
        parts = [f"- {it.get('ticket', '?')} ({hours}h, {it.get('worker', '?')}): {it.get('ticket_summary', '')}"]
        if it.get("ticket_description"):
            parts.append(f"  Ticket: {it['ticket_description'][:300]}")
        for c in it.get("comments", []):
            if c:
                parts.append(f"  Log: {c[:200]}")
        lines.append("\n".join(parts))

    prompt = "\n".join(lines)

    try:
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model or DEFAULT_OPENAI_MODEL,
            messages=[
                {"role": "system", "content":
                    "Magyar nyelvű heti státusz-összefoglalót írsz mérnöki munkáról, "
                    "DOLGOZÓNKÉNT. Minden dolgozó külön sorba kerüljön, az első szó a "
                    "neve legyen, utána 1-3 mondat a tényleges munkájáról (feature-ök, "
                    "bugfix-ek, dokumentáció, meeting, vizsgálat). Az ugyanazon dolgozó "
                    "átfedő munkáit vond össze. Ne a ticket-listát olvasd fel, "
                    "hanem a tartalomra koncentrálj. Nincs bevezető, nincs kötőjel "
                    "vagy bullet, nincs záró összegzés — csak a dolgozók soronkénti "
                    "listája. Példa:\n"
                    "Kovács Béla implementálta az X API-t és javította a Y bug-ot.\n"
                    "Nagy Anna megírta a Z dokumentációt és kiküldte az ügyfélnek."},
                {"role": "user", "content": prompt},
            ],
        )
        content = resp.choices[0].message.content.strip()
        _LLM_CACHE[key] = content
        return content
    except Exception as e:
        return f"(AI summary failed: {e})"


def build_llm_items(issues):
    """Flatten issues dict into the list format llm_summarize expects."""
    items = []
    for key, issue in issues.items():
        for wl in issue["worklogs"]:
            items.append({
                "ticket": key,
                "ticket_summary": issue["summary"],
                "ticket_description": issue["description"],
                "worker": wl["author_name"],
                "hours": round(wl["seconds"] / 3600, 1),
                "comments": [wl["comment"]] if wl["comment"] else [],
            })
    return items


# ---------------------------------------------------------------------------
# Report builders (Slack message format)
# ---------------------------------------------------------------------------

def build_project_report(project_key, project_name, project_desc,
                         issues, start_date, end_date, include_ai=True):
    """Weekly report for one project: totals, per-epic, per-worker, AI summary."""
    total_seconds = sum(sum(w["seconds"] for w in i["worklogs"]) for i in issues.values())
    by_epic = aggregate_by_epic(issues)
    by_worker = aggregate_by_worker(issues)

    lines = [
        f":clipboard: *Projekt riport — {project_name} ({project_key})*",
        f"Időszak: *{start_date} – {end_date}*",
        f"Összes logolt idő: *{format_hours(total_seconds)}*",
    ]

    if by_epic:
        lines.append("")
        lines.append("*Subprojektek (Epikek):*")
        for epic_key, data in sorted(by_epic.items(), key=lambda x: -x[1]["total_seconds"]):
            label = f"{epic_key}" if epic_key != "(no epic)" else "(Epic nélkül)"
            lines.append(f"  • `{label}` {data['epic_summary']} — *{format_hours(data['total_seconds'])}*")
            for ikey, idata in sorted(data["issues"].items(), key=lambda x: -x[1]["seconds"]):
                lines.append(f"      ◦ `{ikey}` {idata['summary']} — {format_hours(idata['seconds'])}")

    if by_worker:
        lines.append("")
        lines.append("*Dolgozónként:*")
        for aid, w in sorted(by_worker.items(), key=lambda x: -x[1]["total_seconds"]):
            lines.append(f"  • {w['name']} — *{format_hours(w['total_seconds'])}*")

    if include_ai:
        lines.append("")
        lines.append("*Összefoglaló (AI):*")
        summary = llm_summarize(
            f"project {project_key} {start_date}..{end_date}",
            build_llm_items(issues),
            project_descriptions={project_key: project_desc} if project_desc else None,
        )
        lines.append(summary)

    return "\n".join(lines)


def build_subproject_report(epic_key, epic_summary, issues_for_epic,
                            start_date, end_date, include_ai=True):
    """Report for a single Epic/subproject."""
    total_seconds = sum(sum(w["seconds"] for w in i["worklogs"]) for i in issues_for_epic.values())
    by_worker = aggregate_by_worker(issues_for_epic)

    lines = [
        f":dart: *Subprojekt riport — `{epic_key}` {epic_summary}*",
        f"Időszak: *{start_date} – {end_date}*",
        f"Összes logolt idő: *{format_hours(total_seconds)}*",
    ]

    if by_worker:
        lines.append("")
        lines.append("*Dolgozónként:*")
        for aid, w in sorted(by_worker.items(), key=lambda x: -x[1]["total_seconds"]):
            lines.append(f"  • {w['name']} — *{format_hours(w['total_seconds'])}*")
            for ikey, sec in sorted(w["issues"].items(), key=lambda x: -x[1]):
                title = issues_for_epic.get(ikey, {}).get("summary", "")
                lines.append(f"      ◦ `{ikey}` {title} — {format_hours(sec)}")

    if include_ai:
        lines.append("")
        lines.append("*Összefoglaló (AI):*")
        summary = llm_summarize(
            f"epic {epic_key} {start_date}..{end_date}",
            build_llm_items(issues_for_epic),
        )
        lines.append(summary)

    return "\n".join(lines)


def build_company_report(issues, start_date, end_date, project_descriptions=None, include_ai=True):
    """Company-wide weekly report: every project, every worker, per-project AI narrative."""
    total_seconds = sum(sum(w["seconds"] for w in i["worklogs"]) for i in issues.values())
    by_project = aggregate_by_project(issues)
    by_worker = aggregate_by_worker(issues)

    lines = [
        f":office: *Cégszintű heti riport*",
        f"Időszak: *{start_date} – {end_date}*",
        f"Összes logolt idő: *{format_hours(total_seconds)}*",
    ]

    if by_project:
        lines.append("")
        lines.append("*Projektek:*")
        for pk, p in sorted(by_project.items(), key=lambda x: -x[1]["total_seconds"]):
            lines.append(f"  • `{pk}` {p['name']} — *{format_hours(p['total_seconds'])}*")

    if by_worker:
        lines.append("")
        lines.append("*Dolgozónként:*")
        for aid, w in sorted(by_worker.items(), key=lambda x: -x[1]["total_seconds"]):
            lines.append(f"  • {w['name']} — *{format_hours(w['total_seconds'])}*")

    if include_ai and by_project:
        lines.append("")
        lines.append("*Heti narratíva projektenként (AI):*")
        for pk, p in sorted(by_project.items(), key=lambda x: -x[1]["total_seconds"]):
            project_issues = {k: v for k, v in issues.items() if v["project_key"] == pk}
            desc = (project_descriptions or {}).get(pk, "")
            # Same label format as build_project_report so cache hits across
            # PO + Management runs in the same process.
            summary = llm_summarize(
                f"project {pk} {start_date}..{end_date}",
                build_llm_items(project_issues),
                project_descriptions={pk: desc} if desc else None,
            )
            lines.append("")
            lines.append(f"_`{pk}` {p['name']} — {format_hours(p['total_seconds'])}_")
            lines.append(summary)

    return "\n".join(lines)


def build_worker_query_report(worker_name, worker_issues, start_date, end_date):
    """Ad-hoc bot response: one worker, time breakdown. No AI to keep it snappy."""
    total = sum(sum(w["seconds"] for w in i["worklogs"]) for i in worker_issues.values())
    lines = [
        f":bust_in_silhouette: *{worker_name}* — {start_date}..{end_date}",
        f"Összesen: *{format_hours(total)}*",
    ]
    if worker_issues:
        lines.append("")
        lines.append("*Ticketek:*")
        for key, issue in sorted(
            worker_issues.items(),
            key=lambda x: -sum(w["seconds"] for w in x[1]["worklogs"]),
        ):
            seconds = sum(w["seconds"] for w in issue["worklogs"])
            lines.append(f"  • `{key}` {issue['summary']} — *{format_hours(seconds)}*")
            for wl in issue["worklogs"]:
                if wl["comment"]:
                    lines.append(f"      ◦ {wl['started']}: {wl['comment'][:120]}")
    return "\n".join(lines)


def filter_issues_by_worker(issues, account_id):
    """Return a copy of issues dict with worklogs filtered to one worker."""
    out = {}
    for key, issue in issues.items():
        matching = [w for w in issue["worklogs"] if w["author_id"] == account_id]
        if matching:
            new = dict(issue)
            new["worklogs"] = matching
            out[key] = new
    return out


def filter_issues_by_epic(issues, epic_key):
    """Return a copy of issues dict filtered to one Epic."""
    return {k: v for k, v in issues.items() if v["epic_key"] == epic_key}


def week_range(anchor=None):
    """Return (monday_str, friday_str) for the week of `anchor` (default today)."""
    d = anchor or date.today()
    monday = d - timedelta(days=d.weekday())
    friday = monday + timedelta(days=4)
    return monday.isoformat(), friday.isoformat()
