# nuno-jobs-agent

Daily job-opportunity scanner for senior finance / blended-finance / development-finance roles, scored against 4 archetypes.

**Live dashboard:** https://nuno-svg.github.io/nuno-jobs-agent/

## What it does

Every day at 06:30 UTC, a GitHub Actions workflow runs `scan/run_daily.py`. The script fetches job postings from ~15 sources, filters them by geography (Europe + Lusophone Africa) and seniority, scores each posting against the 4 archetypes defined in `scan/archetype_keywords.json`, and writes the results to `docs/jobs.json`. The static dashboard in `docs/index.html` reads that JSON and renders an interactive table with filters by archetype, status, source, and free-text search. Status (Reviewing / Applied / Dismissed) is saved in browser `localStorage`.

## Setup (one-time, after pushing the repo)

1. **Add the ReliefWeb secret.** Go to repo Settings → Secrets and variables → Actions → New repository secret. Name: `RELIEFWEB_APPNAME`. Value: the approved appname from your ReliefWeb registration email. *(If you don't have one, request at https://apidoc.reliefweb.int/ — takes ~24h.)*

2. **Enable GitHub Pages.** Settings → Pages → Source: Deploy from a branch → Branch: `main`, Folder: `/docs`.

3. **Run the workflow once manually.** Actions tab → `daily-jobs-scan` → Run workflow. After it completes, the dashboard at `https://nuno-svg.github.io/nuno-jobs-agent/` will be populated.

## Sources

| Source | Type | Notes |
|---|---|---|
| ReliefWeb | API (needs appname) | Most productive — 200+ jobs per scan, filtered to Mid-career + Senior |
| AfDB careers | HTML scrape | Low yield (JS-rendered) — best-effort |
| EBRD careers | HTML scrape | 25-ish vacancies per scan, reliable |
| World Bank | HTML scrape | Low yield (JS-rendered) — best-effort |
| Greenhouse boards | JSON API | Instiglio · Acumen · One Acre Fund · Social Finance · RTI · Code for America · Omidyar · Monzo · N26 |
| LinkedIn search RSS | RSS | Frequently blocked (HTTP 999) — included as best-effort |

## Tuning

* Add or remove sources: edit `scan/sources.json`.
* Adjust keyword weights or add new keywords per archetype: edit `scan/archetype_keywords.json`.
* Exclude noisy titles (e.g. "Driver", "Receptionist"): add to `EXCLUDE_TITLE_PATTERNS` in `scan/run_daily.py`.
* Change schedule: edit the cron in `.github/workflows/daily-scan.yml`.

After any change, commit and push — the next scheduled run uses the new config. To re-scan immediately, trigger the workflow manually.

## Logs

Each run also writes `docs/last_run.json` with per-source stats (raw items, kept items, elapsed seconds, ok/error). Useful for spotting silently broken sources.
