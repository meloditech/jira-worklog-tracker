# Jira Worklog Tracker

Napi és heti Slack értesítő bot Jira worklogokról.

- **Reggelente (hétfő-péntek):** mindenki kap egy DM-et az *előző munkanap* logolt óráiról (ticket bontás + in-progress taszkok + határidők). Ha nincs meg a 8 óra → figyelmeztetés.
- **Péntek délután:** heti összesítő DM (napi bontás + projekt szerinti óraszám).

## Működés

1. Lekéri a Jira API-n (`/rest/api/3/search/jql`) az adott nap / hét worklogjait az összes projekten (blacklist támogatással)
2. Összesíti személyenként, projektenként
3. Slack DM-et küld a `USER_MAPPING` szerint

## Setup

### 1. Jira API Token
1. https://id.atlassian.com/manage-profile/security/api-tokens → **Create API token**
2. Használj **scope nélküli** tokent (a scope-os csak limitált endpointokat enged, ami miatt üres válaszokat kapsz)

### 2. Slack App
1. https://api.slack.com/apps → **Create New App** → From scratch
2. OAuth & Permissions → Bot Token Scopes:
   - `chat:write`
   - `im:write`
3. Install to Workspace → másold ki a Bot User OAuth Token-t (`xoxb-...`)
4. A bot csak olyan emberhez küldhet DM-et, akik a workspace tagjai

### 3. User Mapping

Jira account ID → Slack user ID mapping JSON-ban:

```json
{
  "712020:jira-account-id": "U07SLACK_ID",
  "63b6-account-id": "U04V_SLACK_ID"
}
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
- **Jira account ID:** `GET /rest/api/3/user/search?query=name` vagy user profil URL
- **Slack user ID:** Slack desktop → profil → "..." → Copy member ID

### 4. Környezeti változók / GitHub Secrets

| Secret | Leírás | Példa |
|--------|--------|-------|
| `JIRA_BASE_URL` | Jira instance URL | `https://yourcompany.atlassian.net` |
| `JIRA_EMAIL` | Jira user email | `you@company.com` |
| `JIRA_API_TOKEN` | Jira API token | `ATATT3x...` |
| `SLACK_BOT_TOKEN` | Slack bot token | `xoxb-...` |
| `USER_MAPPING` | JSON mapping | `{"abc123": "U0123ABC"}` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service account JSON (opcionális, OoO detection) | `{"type":"service_account",...}` |
| Név | Leírás |
|-----|--------|
| `JIRA_BASE_URL` | pl. `https://yourcompany.atlassian.net` |
| `JIRA_EMAIL` | Jira user email |
| `JIRA_API_TOKEN` | a fenti token |
| `SLACK_BOT_TOKEN` | `xoxb-...` |
| `USER_MAPPING` | JSON mapping (fenti formátum) |
| `PROJECT_BLACKLIST` | opcionális, vesszővel elválasztott project key-ek (pl. `JD,INT`) |

## Futtatás

### Lokálisan (teszthez)

```bash
pip install -r requirements.txt

# .env fájlból betöltés
set -a && source .env && set +a

# Dry run (nem küld üzenetet)
python worklog_tracker.py --dry-run

# Éles napi futás (előző munkanap)
python worklog_tracker.py

# Konkrét nap
python worklog_tracker.py --date 2026-03-17

# Heti összesítő (az aktuális hét hétfő-péntek)
python worklog_tracker.py --weekly-summary

# Heti összesítő dry-run
python worklog_tracker.py --weekly-summary --dry-run
```

### GitHub Actions (production)

1. Fork/clone a repo-t
2. Settings → Secrets and variables → Actions → add a fenti secret-eket
3. A `.github/workflows/worklog_tracker.yml` automatikusan fut a schedule szerint
4. Manuálisan is indítható: Actions → Jira Worklog Tracker → Run workflow (workflow_dispatch)

## Ütemezés

A GitHub Actions cron ütemezésében a **kerek órákon (`:00`) nagy torlódás van** → a job-ok 40+ perc késéssel indulhatnak. Off-peak percekre (`:15`, `:45`) érdemes időzíteni, akkor csak 5-15 perc a tipikus késés.

Az aktuális beállítás (UTC-ben):

| Mikor | Cron (UTC) | Tipikus érkezés (CET télen / CEST nyáron) |
|-------|------------|-------------------------------------------|
| Napi (H-P) | `45 6 * * 1-5` | ~7:55 CET / **~8:55 CEST** |
| Heti (P) | `45 14 * * 5` | ~16:00 CET / **~17:00 CEST** |

### Saját időpont beállítása

1. Vedd a kívánt helyi időt (pl. 9:00 CEST)
2. Vond le a helyi offsetet (CET = UTC+1, CEST = UTC+2) → 7:00 UTC
3. Vonj le további ~10 percet a GitHub Actions tipikus késése miatt → 6:50 UTC
4. Használj **off-peak** percet (15, 45, stb. — kerüld a `:00`-t) → `45 6 * * 1-5`

Fontos: a cron UTC-ben fix, tehát **télen 1 órával korábbra csúszik** a helyi érkezési idő (pl. nyári 8:55 CEST → téli 7:55 CET). Ha ez zavaró, külön cron-t lehet rakni CET/CEST ablakokra vagy DST-aware ütemezővel kell megoldani (pl. [schedule.yml workflow trigger with TZ](https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions#onschedule) csak UTC-t támogat).

## Üzenet formátum

**Napi (8+ óra):**
```
✅ Szia! Az előző munkanapra (2026-03-10) összesen 9h van logolva. Szép munka!

Logolt munkák:
  • KGD-827 Auchan Shadowing — 2h
  • EMS-4 pályázat leírása — 3h

In Progress feladatok:
  • AI-32 DMS Other Consultations
```

**Napi (< 8 óra):**
```
❗Szia! Az előző munkanapra (2026-03-10) összesen 6h van logolva.
2h hiányzik a 8 órából.

[Logolt munkák / In Progress / Határidős feladatok blokkok]

Kérlek pótold a hiányzó órákat! 🙏
```

**Heti (pénteken):**
```
📊 Heti összesítő (2026-03-09 – 2026-03-13)
Összesen logolt idő: 38h / 40h

Napi bontás:
  ✅ Hétfő (2026-03-09): 8h
  ✅ Kedd (2026-03-10): 9h
  ❌ Szerda (2026-03-11): 6h
  ✅ Csütörtök (2026-03-12): 8h
  ✅ Péntek (2026-03-13): 7h

Projektek szerinti bontás:
  • KGD: 18h
  • AI: 12h
  • EMS: 8h

⚠️ 2h hiányzik a heti 40 órából.
```

## CLI paraméterek

| Flag | Leírás |
|------|--------|
| `--dry-run` | Csak kiírja az üzeneteket, nem küld Slack DM-et |
| `--date YYYY-MM-DD` | Adott napot ellenőriz (napi módban: alapértelmezett az előző munkanap; heti módban: ez a nap határozza meg, melyik hetet nézze) |
| `--weekly-summary` | Heti összesítőt küld a napi ellenőrzés helyett |
