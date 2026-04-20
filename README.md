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

- **Jira account ID:** `GET /rest/api/3/user/search?query=name` vagy user profil URL
- **Slack user ID:** Slack desktop → profil → "..." → Copy member ID

### 4. Környezeti változók / GitHub Secrets

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
