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
   - `users:read` - user listázás
   - `users:read.email` - email lookup (Google Calendar OoO fallback-hez, és `--list-ooo U...` feloldáshoz)
3. Install to Workspace → másold ki a "Bot User OAuth Token"-t (xoxb-...)

### 3. User Mapping

JSON formátumú mapping a Jira account ID-k és Slack user ID-k között. Két shape támogatott:

**Egyszerű (legacy) forma:**

```json
{"jira_account_id_1": "SLACK_USER_ID_1", "jira_account_id_2": "SLACK_USER_ID_2"}
```

**Kiterjesztett forma (ajánlott, ha Google Calendar OoO detection is kell):**

```json
{
  "jira_account_id_1": {"slack": "U07KUE09ULA", "email": "bence.bial@bpdata.com"},
  "jira_account_id_2": {"slack": "U0123ABC", "email": "adam.nemes@bpdata.com"}
}
```

Az `email` mező opcionális, de **erősen ajánlott** a Google Calendar OoO funkcióhoz:

- A Jira alapértelmezetten **rejti** az `emailAddress`-t privacy setting miatt.
- A Slack `users.info` lookup működne, de `users:read.email` scope-ot igényel.
- Az `email` mezővel a script **közvetlenül** tudja ki melyik Google calendar-t olvassa — nincs reliance external lookupra.

Domain auto-normalizálás: a régi `@meloditech.com` címek automatikusan `@bpdata.com`-ra cserélődnek (lásd lentebb).

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

A script a Jira user `emailAddress` mezőjét használja (Jira REST API `/user?accountId=`). Ennek meg kell egyeznie a Google Workspace email-lel.

**Domain rebrand (meloditech.com → bpdata.com):** a cég névváltása miatt Jira/Slack-ben még maradhat `@meloditech.com` email, miközben a Google Workspace `@bpdata.com`-on van. A script ezt automatikusan kezeli: a `canonicalize_email()` helper minden `@meloditech.com` címet `@bpdata.com`-ra cserél mielőtt impersonationt vagy email-összehasonlítást csinálna. Ha újabb alias-t kell felvenni, szerkeszd az `EMAIL_DOMAIN_ALIASES` dict-et a `worklog_tracker.py` tetején.

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

### Automatikus futtatás — Render Cron Jobs

Az ütemezett futás **Render Cron Job**-okon keresztül történik (korábban GitHub Actions volt). A GitHub workflow megmarad, de csak manuális indításra (Actions → Jira Worklog Tracker → Run workflow).

#### Render setup (manual UI)

**Előfeltétel:** Render account (ingyenes is elég) + a repo (GitHub) hozzáférhető.

**1. Új Cron Job létrehozása — napi check**

1. https://dashboard.render.com/ → **New +** → **Cron Job**.
2. Connect repo: válaszd ki a `jira-worklog-tracker` repo-t. Branch: `main` (vagy aminél telepíteni szeretnél).
3. Beállítások:
   - **Name:** `jira-worklog-daily`
   - **Region:** Frankfurt (legközelebbi)
   - **Runtime:** `Python`
   - **Build Command:** `pip install -r requirements.txt`
   - **Schedule:** `45 6 * * 1-5`  *(UTC! = 08:45 CEST / 07:45 CET, hétfő–péntek)*
   - **Command:** `python worklog_tracker.py`
4. **Environment Variables** — add hozzá az alábbiakat (mindegyik "Secret" típusú a Render-ben):
   - `JIRA_BASE_URL`
   - `JIRA_EMAIL`
   - `JIRA_API_TOKEN`
   - `SLACK_BOT_TOKEN`
   - `USER_MAPPING`
   - `PROJECT_BLACKLIST` *(opcionális)*
   - `GOOGLE_SERVICE_ACCOUNT_JSON` *(opcionális, OoO detection)*

   A `.env` fájlban lévő értékeket másold ide 1:1-ben (egy sorban a JSON-öket is).
5. **Create Cron Job**. A Render legyártja a service-t, futtatja a build-et és várja a következő cron trigger-t.

**2. Második Cron Job — heti összesítő**

Ismételd meg a fenti lépéseket az alábbi eltérésekkel:

- **Name:** `jira-worklog-weekly`
- **Schedule:** `45 14 * * 5`  *(péntek 14:45 UTC = 16:45 CEST / 15:45 CET)*
- **Command:** `python worklog_tracker.py --weekly-summary`
- **Env vars:** ugyanazok mint a daily.

Tipp: ha nem akarod mindegyik env var-t kétszer begépelni, használj Render **Environment Group**-ot:
1. Dashboard → **Env Groups** → **New Environment Group** → add hozzá mind a 7 változót.
2. A Cron Job-oknál Settings → Environment → **Link Environment Group**. Mindkét job ugyanazt a group-ot használja — egy helyen tartod karban.

#### Első indítás / tesztelés

- Minden Cron Job-nál a "Trigger Run" gomb elérhető a dashboard-on → azonnali one-shot futtatás.
- A logok a **Logs** tab alatt élőben látszanak. A `Python` runtime nem hagy semmi state-et két futtatás között.

#### Időzónák

Render cron kifejezés **UTC**-ben van. Jelenlegi setup:
- `45 6 * * 1-5` → hétfő–péntek 08:45 CEST / 07:45 CET (off-peak, gyors indulás)
- `45 14 * * 5` → péntek 16:45 CEST / 15:45 CET

DST átállásakor egy órát csúszik — ha szigorú lokális idő kell, rendszeresen ellenőrizd március/október végén.

### Lokális teszt

A projekt `uv`-t használ Python env + dependency management-re. Telepítés:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# vagy Homebrew: brew install uv
```

Bővebben: https://docs.astral.sh/uv/getting-started/installation/

**Env + dependency install:**

```bash
uv venv --python 3.12           # .venv létrehozás Python 3.12-vel
source .venv/bin/activate       # aktiválás (zsh/bash)
uv pip install -r requirements.txt
```

**Env változók — `.env` fájl (ajánlott lokálisan):**

A script induláskor automatikusan beolvas egy `.env` fájlt a projekt gyökeréből (`python-dotenv` segítségével). A `.env` **nem felülírja** azokat az env változókat, amik már be vannak állítva — így produkcióban (GitHub Actions secrets) ugyanaz a kód gond nélkül fut.

Hozz létre egy `.env` fájlt a repo gyökerében (már `.gitignore`-ban van):

```bash
# .env
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=you@company.com
JIRA_API_TOKEN=your_token
SLACK_BOT_TOKEN=xoxb-your-token
USER_MAPPING={"jira_id": "slack_id"}
PROJECT_BLACKLIST=PROJ1,PROJ2
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
```

Tippek:
- A JSON értékeket **idézőjel nélkül** írd egy sorba (a `python-dotenv` a `=` utáni teljes sort értékként veszi).
- Ha a JSON-on belül `#` szerepel, tedd idézőjelbe az egészet: `USER_MAPPING='{"foo":"bar"}'`.
- Produkcióban ne használj `.env`-et — a GitHub Actions workflow a repo Secrets-ből hozza az értékeket (lásd fent).

**Script futtatás:**

```bash
python worklog_tracker.py --dry-run
```

Alternatíva activation nélkül: `uv run python worklog_tracker.py --dry-run` (az `.venv`-et automatikusan használja, ha van).

Ha nem akarsz `.env` fájlt, exportálhatsz manuálisan is:

```bash
export JIRA_BASE_URL="https://yourcompany.atlassian.net"
# ... stb.
python worklog_tracker.py --dry-run
```

### Paraméterek
- `--dry-run` — Csak kiírja az üzeneteket, nem küld Slack DM-et
- `--date YYYY-MM-DD` — Adott napot ellenőriz (alapértelmezett: előző munkanap)
- `--today` — A mai napot ellenőrzi (debug; a nap még nem ért véget, csak a részletes státusz kedvéért)
- `--weekly-summary` — Heti összesítőt küld (hétfőtől péntekig, projekt bontásban)
- `--users EMAIL_OR_NAME [...]` — Csak a megadott user-eknek küld üzenetet. Az érték automatikusan osztályozva:
  - `@` jelet tartalmaz → Jira `emailAddress` **exact** match (case-insensitive).
  - Nincs `@` → Jira `displayName` **exact** match (case-insensitive).
  - Szóköz- vagy vesszővel elválasztva. Pl. `--users "Bence Bial" foo@melodi.com "Nemes Ádám"`
- `--list-users` — Kiírja a `USER_MAPPING` bejegyzéseket a Jira által visszaadott display name-mel és email-lel (debug).
- `--list-ooo EMAIL_OR_SLACK_ID` — Diagnosztika: kilistázza egy user Google Calendar OoO eseményeit egy időablakban. Elfogadja: email-t (`foo@bpdata.com`) vagy Slack ID-t (`U07KUE09ULA`). Slack ID-nél Slack `users.info` lookupol (scopes: `users:read`, `users:read.email`).
- `--days N` — `--list-ooo`-hoz az időablak napokban (default: 30, a mai napra centrálva).

### Heti összesítő
Pénteken 17:00-kor automatikusan fut a heti összesítő, ami minden felhasználónak elküldi:
- Napi bontás (hétfőtől péntekig)
- Projekt szerinti időbontás
- Összesített heti óraszám vs. 40h

### Célzott futtatás email vagy név alapján (`--users`)

Ha csak konkrét user-eknek akarsz üzenetet küldeni (pl. teszteléskor vagy ad-hoc emlékeztetőként), használd a `--users` flaget. Az értékeket a script az `@` jel alapján osztályozza:

- **Van `@` az értékben** → email match. A script a Jira user API-ban (`/rest/api/3/user?accountId=...`) lookup-olja az `emailAddress`-et, és **exact** (case-insensitive) összehasonlítást végez. *Figyelem:* a Jira alapértelmezetten **rejti** az email-t privacy setting miatt — ilyenkor a match nem fog sikerülni. Használd inkább a név alapján való szűrést.
- **Nincs `@` az értékben** → név match. A Jira `displayName` mezőn **exact** (case-insensitive) egyezés. Pl. `--users "Bence Bial"` — ékezetekkel és szóközökkel együtt pontosan úgy, ahogy a Jira megjeleníti.

A `USER_MAPPING` kulcsai Jira account ID-k, az értékek Slack user ID-k — az email/név a Jira felől resolve-olódik minden futtatáskor.

**Példák:**

```bash
# Név szerint (displayName exact) — dry run
python worklog_tracker.py --dry-run --users "Bence Bial"

# Több user, vessző vagy szóköz elválasztó
python worklog_tracker.py --users "Bence Bial","Nemes Ádám"
python worklog_tracker.py --users "Bence Bial" "Nemes Ádám"

# Email-lel (ha a Jira kiadja az emailAddress-t)
python worklog_tracker.py --users foo@melodi.com

# Vegyesen: email + név
python worklog_tracker.py --users foo@melodi.com "Bence Bial"

# Konkrét dátum + user
python worklog_tracker.py --date 2026-04-17 --users "Bence Bial"

# Heti összesítő egy user-nek
python worklog_tracker.py --weekly-summary --users "Bence Bial"
```

Ha egy megadott érték nem talál USER_MAPPING bejegyzést, warning-ot ír. Ha egyik érték sem match-el, a script `exit 1`-gyel kilép.

**Debug — ki szerepel a mapping-ben és mit ad vissza a Jira?**

```bash
python worklog_tracker.py --list-users
```

Kiírja a Jira Account ID → Slack ID → displayName → emailAddress táblát. Ha az emailAddress oszlopban `(hidden by Jira privacy)` áll, akkor csak név szerint tudsz szűrni.

**GitHub Actions manuális indítás egy user-re:** jelenleg a workflow nem támogatja inputként (lokálisan vagy ad-hoc CLI-ként használd).
