# Jira Worklog Tracker

Napi bot, ami ellenőrzi a Jira worklogokat és Slack DM-ben értesíti azokat, akik nem logoltak 8 órát.

## Hogyan működik

1. Lekéri a Jira API-n keresztül az aznapi worklogokat (összes project)
2. Összesíti személyenként: ki melyik ticketekre hány órát logolt
3. Akinek nincs meg a 8 óra, annak privát Slack üzenetet küld az összesítéssel

## Setup

### 1. Jira API Token

1. Menj ide: https://id.atlassian.com/manage-profile/security/api-tokens
2. "Create API token" → másold ki

### 2. Slack App

1. Menj ide: https://api.slack.com/apps → "Create New App" → "From scratch"
2. OAuth & Permissions → Bot Token Scopes:
   - `chat:write` - üzenet küldés
   - `im:write` - DM channel nyitás
3. Install to Workspace → másold ki a "Bot User OAuth Token"-t (xoxb-...)

### 3. User Mapping

JSON formátumú mapping a Jira account ID-k és Slack user ID-k között:

```json
{"jira_account_id_1": "SLACK_USER_ID_1", "jira_account_id_2": "SLACK_USER_ID_2"}
```

**Jira Account ID megtalálása:**
- Jira user profil URL-ben: `https://yoursite.atlassian.net/jira/people/ACCOUNT_ID`
- Vagy API: `GET /rest/api/3/user/search?query=username`

**Slack User ID megtalálása:**
- Slack desktop → user profil → "..." menü → "Copy member ID"

### 4. Google Calendar OoO (opcionális)

Ha beállítod, a script lekéri minden user Google Calendar OoO eseményeit és beleszámolja a 8 órába:

- **Egész napos OoO esemény** = szabadság → a nap 8 órája automatikusan be van számolva, nem küld warning-ot.
- **Órás OoO esemény** (pl. orvos) = munkanap közbeni szünet → csak megjelenik az üzenetben, külön nem kompenzálja a 8 órát.

#### A GOOGLE_SERVICE_ACCOUNT_JSON beszerzése

Domain-wide delegation-t használunk: egy service account impersonál minden user-t a Workspace-ben és csak read-only a calendar. Egyszeri admin setup.

**Előfeltétel:** Google Workspace admin hozzáférés a domainhez (pl. `melodi.com`).

**1. Google Cloud projekt + Calendar API engedélyezés**

1. Nyisd meg: https://console.cloud.google.com/
2. Felső sáv → projekt választó → "New Project".
   - Név: pl. `jira-worklog-tracker`
   - Create.
3. Váltsd át az új projektre.
4. Bal menü → "APIs & Services" → "Library".
5. Keresd meg: "Google Calendar API" → Enable.

**2. Service account létrehozása**

1. Bal menü → "IAM & Admin" → "Service Accounts" → "Create service account".
2. Name: pl. `jira-worklog-calendar-reader`. Create and Continue.
3. "Grant this service account access to project" — üresen lehet hagyni (nem kell project role). Continue → Done.
4. A listából nyisd meg az új service account-ot.
5. Fülek: **Details** → jegyezd fel az "Unique ID" mezőt (= `client_id`, ez kell a domain-wide delegation-hoz).
6. Fül: **Keys** → "Add Key" → "Create new key" → JSON → Create.
   - Letöltődik egy `*.json` fájl. **Ez a `GOOGLE_SERVICE_ACCOUNT_JSON` titka.**
   - Ne commitold semmibe. Ez lesz a GitHub Secret értéke — a teljes fájl tartalma (nyitó `{` és záró `}` is).

**3. Domain-wide delegation engedélyezés (Workspace admin)**

1. Nyisd meg: https://admin.google.com (Workspace super admin-ként).
2. Security → Access and data control → API controls.
3. "Manage Domain Wide Delegation" → "Add new".
4. **Client ID:** a service account Unique ID-ja (lásd 2.5. lépés).
5. **OAuth scopes:** `https://www.googleapis.com/auth/calendar.readonly`
6. Authorize.

Ezután a service account bármely user calendarját olvashatja a domainben (csak read-only).

**4. Email matching**

A script a Jira user `emailAddress` mezőjét használja (Jira REST API `/user?accountId=`). Ennek meg kell egyeznie a Google Workspace email-lel. Ha eltér valaki (pl. alias), annál a user-nél az OoO lookup fail-el és nincs szabadság-beszámítás — a régi 8h warning logika érvényesül rá.

**5. GitHub Secret hozzáadása**

- Repo → Settings → Secrets and variables → Actions → New repository secret.
- Name: `GOOGLE_SERVICE_ACCOUNT_JSON`
- Secret: a letöltött JSON fájl **teljes tartalma** (copy-paste a fájlból).

**Lokális teszt:**

```bash
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat path/to/service-account.json)"
python worklog_tracker.py --dry-run
```

### 5. GitHub Secrets

A repo Settings → Secrets and variables → Actions → New repository secret:

| Secret | Leírás | Példa |
|--------|--------|-------|
| `JIRA_BASE_URL` | Jira instance URL | `https://yourcompany.atlassian.net` |
| `JIRA_EMAIL` | Jira user email | `you@company.com` |
| `JIRA_API_TOKEN` | Jira API token | `ATATT3x...` |
| `SLACK_BOT_TOKEN` | Slack bot token | `xoxb-...` |
| `USER_MAPPING` | JSON mapping | `{"abc123": "U0123ABC"}` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service account JSON (opcionális, OoO detection) | `{"type":"service_account",...}` |

## Futtatás

### Automatikus (GitHub Actions)
Hétfőtől péntekig 17:00 CET-kor automatikusan fut. Kézzel is indítható: Actions → Jira Worklog Tracker → Run workflow.

### Lokális teszt
```bash
export JIRA_BASE_URL="https://yourcompany.atlassian.net"
export JIRA_EMAIL="you@company.com"
export JIRA_API_TOKEN="your_token"
export SLACK_BOT_TOKEN="xoxb-your-token"
export USER_MAPPING='{"jira_id": "slack_id"}'

pip install -r requirements.txt
python worklog_tracker.py --dry-run
```

### Paraméterek
- `--dry-run` — Csak kiírja az üzeneteket, nem küld Slack DM-et
- `--date YYYY-MM-DD` — Adott napot ellenőriz (alapértelmezett: előző munkanap)
- `--weekly-summary` — Heti összesítőt küld (hétfőtől péntekig, projekt bontásban)

### Heti összesítő
Pénteken 17:00-kor automatikusan fut a heti összesítő, ami minden felhasználónak elküldi:
- Napi bontás (hétfőtől péntekig)
- Projekt szerinti időbontás
- Összesített heti óraszám vs. 40h
