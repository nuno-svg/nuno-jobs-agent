#!/usr/bin/env python3
"""
nuno-jobs-agent — daily scan
Aggregates job postings from 20+ sources, scores against 4 archetypes,
emits docs/jobs.json for the dashboard to render.

Designed to be ROBUST: any single source failing must not break the run.
Every source is wrapped in try/except with structured logging.
"""

import json
import os
import re
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import feedparser
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = ROOT / "scan"
DOCS_DIR = ROOT / "docs"
DOCS_DIR.mkdir(exist_ok=True)

OUT_FILE = DOCS_DIR / "jobs.json"
LOG_FILE = DOCS_DIR / "last_run.json"

UA = "nuno-jobs-agent/1.0 (+https://github.com/nuno-svg/nuno-jobs-agent)"
TIMEOUT = 30


def log(msg, source=None, level="info"):
    """Structured log line for GitHub Actions output."""
    prefix = f"[{source}] " if source else ""
    print(f"{level.upper()}: {prefix}{msg}", flush=True)


def load_config():
    with open(SCAN_DIR / "sources.json") as f:
        sources = json.load(f)
    with open(SCAN_DIR / "archetype_keywords.json") as f:
        keywords = json.load(f)
    return sources, keywords


def _normalize_url_for_dedup(url):
    """Strip query parameters and fragments so the same job posted with different
    tracking codes hashes to the same id. Also handles URL variations like trailing
    slashes and casing in the path. Returns lower-case path only."""
    if not url:
        return ""
    try:
        # Strip after ? and # (query + fragment); keep scheme+host+path
        u = re.split(r"[?#]", url, maxsplit=1)[0]
        return u.rstrip("/").lower()
    except Exception:
        return str(url).lower()


def make_id(url, title):
    norm_url = _normalize_url_for_dedup(url)
    norm_title = normalize(title)
    h = hashlib.sha256(f"{norm_url}|{norm_title}".encode()).hexdigest()[:12]
    return h


def normalize(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip().lower()


# ---------------- Source fetchers ----------------

def fetch_rss(source):
    """Generic RSS / Atom feed parser."""
    url = source["url"]
    feed = feedparser.parse(url, agent=UA)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"feed parse failed: {feed.bozo_exception}")
    items = []
    for e in feed.entries[:100]:
        items.append({
            "title": e.get("title", ""),
            "url": e.get("link", ""),
            "description": e.get("summary", "") or e.get("description", ""),
            "published": e.get("published", "") or e.get("updated", ""),
        })
    return items


def fetch_reliefweb(source):
    """ReliefWeb API — requires appname secret."""
    appname = os.environ.get(source["needs_secret"], "").strip()
    if not appname:
        raise RuntimeError(f"missing env var {source['needs_secret']}")
    url = source["url"].format(appname=appname)
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    items = []
    for d in data.get("data", []):
        f = d.get("fields", {})
        title = f.get("title", "")
        body = f.get("body", "") or f.get("body-html", "")
        url = (f.get("url_alias") or "").strip()
        if not url and d.get("id"):
            url = f"https://reliefweb.int/job/{d['id']}"
        items.append({
            "title": title,
            "url": url,
            "description": BeautifulSoup(body, "html.parser").get_text(" ", strip=True)[:2000],
            "published": f.get("date", {}).get("created", ""),
        })
    return items


def fetch_greenhouse(source):
    """Greenhouse public board JSON API."""
    board = source["board"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    data = r.json()
    items = []
    for j in data.get("jobs", []):
        items.append({
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "description": BeautifulSoup(j.get("content", ""), "html.parser").get_text(" ", strip=True)[:2000],
            "published": j.get("updated_at", ""),
            "location": (j.get("location") or {}).get("name", ""),
        })
    return items


def fetch_lever(source):
    """Lever public board JSON API."""
    board = source["board"]
    url = f"https://api.lever.co/v0/postings/{board}?mode=json"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    data = r.json()
    items = []
    for j in data:
        cats = j.get("categories", {}) or {}
        items.append({
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "description": BeautifulSoup(j.get("descriptionPlain", ""), "html.parser").get_text(" ", strip=True)[:2000],
            "published": str(j.get("createdAt", "")),
            "location": cats.get("location", ""),
        })
    return items


def fetch_html(source):
    """Generic HTML scrape — best effort. Many job sites use JS-rendered content
    that this won't catch; sources marked robust=false are expected to sometimes
    return empty without that being an error."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    r = requests.get(source["url"], headers=headers, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    selector = source.get("selector", "a")
    rows = soup.select(selector)
    items = []
    for row in rows[:50]:
        link = row if row.name == "a" else row.find("a")
        if not link or not link.get("href"):
            continue
        href = link["href"]
        if not href.startswith("http"):
            base = urlparse(source["url"])
            href = f"{base.scheme}://{base.netloc}{href if href.startswith('/') else '/' + href}"
        title = link.get_text(" ", strip=True)[:200]
        if not title or len(title) < 5:
            continue
        items.append({
            "title": title,
            "url": href,
            "description": row.get_text(" ", strip=True)[:1000],
            "published": "",
        })
    return items


def fetch_linkedin_rss(source):
    """LinkedIn search RSS — best effort, frequently blocked with 999."""
    r = requests.get(source["url"], headers={"User-Agent": UA}, timeout=TIMEOUT)
    if r.status_code == 999:
        raise RuntimeError("LinkedIn blocked request (999)")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries[:50]:
        items.append({
            "title": e.get("title", ""),
            "url": e.get("link", ""),
            "description": e.get("summary", ""),
            "published": e.get("published", ""),
        })
    return items


def fetch_ashby(source):
    """Ashby HQ public job board JSON API.

    Used by many impact-finance and dev-tech organisations that migrated off
    Greenhouse/Lever in 2024-2026 (e.g. Lendable, FINCA).
    """
    board = source["board"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    data = r.json()
    items = []
    for j in data.get("jobs", []):
        loc = j.get("location", "") or ""
        # Some Ashby responses use a list of secondary locations
        sec = j.get("secondaryLocations", []) or []
        if sec and isinstance(sec, list):
            loc_parts = [loc] + [s if isinstance(s, str) else s.get("location", "") for s in sec]
            loc = " / ".join([x for x in loc_parts if x])
        items.append({
            "title": j.get("title", ""),
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
            "description": BeautifulSoup(j.get("descriptionHtml", "") or j.get("descriptionPlain", ""),
                                          "html.parser").get_text(" ", strip=True)[:2000],
            "published": str(j.get("publishedAt") or j.get("updatedAt") or ""),
            "location": loc,
        })
    return items


def fetch_smartrecruiters(source):
    """SmartRecruiters public postings API.

    Used by some big European employers (e.g. Wise). Pagination via offset.
    We pull up to 200 to keep latency low.
    """
    company = source["company"]
    items = []
    offset = 0
    page_size = 100
    while offset < 200:
        url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit={page_size}&offset={offset}"
        r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=TIMEOUT)
        if r.status_code != 200:
            if offset == 0:
                raise RuntimeError(f"HTTP {r.status_code}")
            break
        data = r.json()
        content = data.get("content", [])
        if not content:
            break
        for j in content:
            loc_obj = j.get("location", {}) or {}
            loc = " / ".join([x for x in [loc_obj.get("city"), loc_obj.get("country")] if x])
            items.append({
                "title": j.get("name", ""),
                "url": (j.get("ref") or "").replace("postings/", "https://jobs.smartrecruiters.com/") or
                       f"https://jobs.smartrecruiters.com/{company}/{j.get('id', '')}",
                "description": "",  # SmartRecruiters list API doesn't return full description
                "published": j.get("releasedDate") or j.get("createdOn") or "",
                "location": loc,
            })
        offset += page_size
        if len(content) < page_size:
            break
    return items


def fetch_apify(source):
    """Generic Apify actor runner. Triggers a synchronous run and reads the dataset.

    Source config requires:
      actor_id: e.g. 'curious_coder/linkedin-jobs-scraper'
      input:    JSON dict passed as actor input
      field_map (optional): mapping {our_field: actor_output_field} to normalise
                            field names across different actors. Defaults below
                            cover the most common LinkedIn/Indeed output shapes.
    """
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        raise RuntimeError("missing env var APIFY_TOKEN")
    actor_id = source["actor_id"].replace("/", "~")
    actor_input = source.get("input", {})
    # run-sync-get-dataset-items: triggers actor, waits for completion, returns items
    url = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"
    params = {"token": token, "timeout": 180}
    r = requests.post(url, params=params, json=actor_input, timeout=240)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"non-JSON response: {r.text[:200]}")
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected response shape: {str(data)[:200]}")
    fmap = source.get("field_map", {})
    items = []
    for d in data[:200]:
        # Best-effort field mapping: try the mapped field, then common alternatives
        title = d.get(fmap.get("title", "title")) or d.get("jobTitle") or d.get("position") or ""
        url_field = d.get(fmap.get("url", "url")) or d.get("jobUrl") or d.get("applyUrl") or d.get("link") or ""
        desc = d.get(fmap.get("description", "description")) or d.get("jobDescription") or d.get("descriptionText") or d.get("summary") or ""
        if isinstance(desc, str) and "<" in desc:
            desc = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
        loc = d.get(fmap.get("location", "location")) or ""
        pub = d.get(fmap.get("published", "postedAt")) or d.get("publishedAt") or d.get("date") or ""
        company = d.get("companyName") or d.get("company") or ""
        if company:
            desc = f"{company} | {desc}"
        items.append({
            "title": str(title)[:300],
            "url": str(url_field),
            "description": str(desc)[:2000],
            "published": str(pub),
            "location": str(loc),
        })
    return items


FETCHERS = {
    "rss": fetch_rss,
    "api": fetch_reliefweb,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "html": fetch_html,
    "linkedin_rss": fetch_linkedin_rss,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "apify": fetch_apify,
}


# ---------------- Scoring ----------------

EXCLUDE_TITLE_PATTERNS = [
    # Pure junior / admin
    "intern", "internship", "trainee program", "graduate program", "graduate scheme",
    "driver", "receptionist", "secretary", "cleaner", "janitor", "security guard",
    "administrative assistant", "executive assistant", "office assistant",
    "junior analyst", "entry level", "entry-level", "apprentice",
    "data entry", "data clerk",
    "position title",
    # Standalone Analyst / Assistant titles — never senior
    # Use leading word-boundary patterns: title starts with these or has them as discrete sections
    "assistant ",           # "Assistant Analyst", "Assistant Manager", etc.
    "junior ",
    # Mid-level deal/PE/M&A execution roles
    "investment associate", "investment analyst",
    "m&a associate", "m&a analyst", "m&a manager",
    "corporate development associate", "corporate development analyst", "corporate development manager",
    "transaction services associate", "transaction services analyst",
    "private equity associate", "private equity analyst",
    "deal associate", "deal analyst",
    "buy-side analyst", "sell-side analyst",
    "investment manager",  # mid-level in most Euro PE firms
    # EBRD-style banker titles below the threshold we want
    "associate banker", "principal banker",  # both mid-level at EBRD
    # Non-finance roles that keep slipping through on keyword overlap
    "infrastructure quality engineer", "quality engineer",
    "health & safety", "health and safety",
    "azure", "kubernetes", "devops", "data engineer", "software engineer",
    "frontend", "backend", "full stack", "full-stack",
    "qa automation", "qa engineer", "test engineer", "sol. architect",
    "solution architect", "solutions architect",
    "people lead", "people partner", "talent", "recruiter",
    "marketing", "sales lead", "growth lead",
    # Specific role types that keep appearing and never fit
    "package responsible buyer", "contracts analyst", "contracts engineer",
    "document controller", "document control",
    "credit analyst", "credit manager", "senior credit",
    "account manager",  # almost always sales, not finance
    "business analyst",  # too broad, rarely senior finance
    # Generic Analyst title alone (when title literally is just "Analyst" or short variants)
    # Handled via the dedicated check below, not in this list
]

# Titles where the entire title is just a junior generic word
EXCLUDE_EXACT_TITLES = {"analyst", "associate", "manager", "consultant", "specialist"}


# --------------- Language filter ---------------
# Exclude job postings predominantly in languages Nuno cannot work in.
# Strategy: count distinctive high-frequency function words from each excluded language.
# If the ratio of foreign-language tokens exceeds threshold, exclude the posting.
# We only check title + first 400 chars of description to keep it fast.

_LANG_SIGNALS = {
    "de": ["die", "der", "das", "und", "sie", "für", "mit", "auf", "auch", "eine", "einen",
           "ist", "sind", "haben", "werden", "ihre", "ihren", "sowie", "suchen", "wir",
           "verantwortung", "kenntnisse", "erfahrung", "aufgaben", "profil", "anforderungen",
           "deutsch", "deutschkenntnisse", "unternehmen", "stellenangebot"],
    "nl": ["de", "het", "een", "van", "voor", "met", "zijn", "worden", "deze", "ons",
           "onze", "naar", "zoeken", "wij", "dutch", "nederland", "nederlandse",
           "werkzaamheden", "functie", "profiel", "kandidaat", "vacature"],
    "it": ["della", "delle", "degli", "nella", "nelle", "degli", "sono", "anche", "essere",
           "viene", "avere", "nostro", "nostra", "cerchiamo", "requisiti", "italiano",
           "italiano fluente", "sede", "azienda", "ruolo", "responsabilità"],
    "ro": ["pentru", "este", "sunt", "care", "sau", "din", "unui", "unei", "coordoneaza",
           "responsabilitati", "candidatului", "recruteaza", "pozitia", "profilul"],
    "ru": ["для", "что", "это", "как", "или", "при", "по", "со", "на", "от", "из",
           "который", "которые", "знание", "русский", "опыт"],
    "fr": ["vous", "nous", "pour", "les", "des", "une", "dans", "sur", "avec", "par",
           "votre", "notre", "qui", "que", "est", "sont", "avoir", "être", "ses", "ces",
           "recherche", "poste", "candidat", "expérience", "compétences", "missions",
           "français", "maîtrise", "rejoindre", "diplômé", "rejoindre", "entreprise"],
}

# Minimum number of signal words to trigger exclusion (avoids false positives on short texts)
_LANG_THRESHOLD = 3


def is_excluded_language(title: str, description: str) -> bool:
    """Return True if the posting appears to be predominantly in an excluded language."""
    # Combine title (weighted ×3) + first 400 chars of description
    text = (normalize(title) + " ") * 3 + normalize(description[:400])
    words = set(re.findall(r"\b\w+\b", text))
    for lang, signals in _LANG_SIGNALS.items():
        hits = sum(1 for s in signals if s in text)
        if hits >= _LANG_THRESHOLD:
            return True
    return False


def is_excluded_title(title):
    t = normalize(title)
    if not t or len(t) < 6:
        return True
    # Exact-title exclusion (e.g. title is literally just "Analyst")
    if t in EXCLUDE_EXACT_TITLES:
        return True

    # EBRD-style "Analyst, Department" pattern — title starts with "analyst," or "analyst "
    # These are entry/mid level at EBRD regardless of what department follows
    if t.startswith("analyst,") or t.startswith("analyst ") or t == "analyst":
        return True
    # Same for "associate" alone or with department qualifier
    if t.startswith("associate,") or t == "associate":
        return True

    # Hard exclusions (these always exclude, regardless of senior-signal override).
    # "Associate Banker" and "Principal Banker" at EBRD are mid-level positions
    # even though "principal" normally signals seniority.
    HARD_EXCLUDE = ["associate banker", "principal banker"]
    for pat in HARD_EXCLUDE:
        if pat in t:
            return True

    # Senior signals that override soft exclusions (e.g. "Head of M&A Associates")
    senior_signals = ["head of", "director of", "chief", "vp ", "vice president",
                      "managing director", "partner", "global head", "group head"]
    has_senior_signal = any(s in t for s in senior_signals)

    # Substring exclusion: pattern must appear as a contiguous substring
    for pat in EXCLUDE_TITLE_PATTERNS:
        if pat in t:
            if has_senior_signal:
                return False
            return True
    return False


def score_against_archetypes(item, keywords_cfg):
    """Returns {archetype_key: score} plus the strongest match."""
    text = " ".join([
        normalize(item.get("title", "")),
        normalize(item.get("description", "")),
        normalize(item.get("location", "")),
    ])
    scores = {}
    for ak, cfg in keywords_cfg.items():
        if ak.startswith("_"):
            continue
        hits = 0
        for kw in cfg.get("keywords", []):
            if normalize(kw) in text:
                hits += 1
        scores[ak] = hits * cfg.get("weight", 1.0)
    return scores


def apply_publisher_boost(item, scores, keywords_cfg):
    """Add publisher-level boost if URL domain matches a known firm."""
    boosts = keywords_cfg.get("_publisher_boosts", {})
    url_lower = (item.get("url") or "").lower()
    desc_lower = normalize(item.get("description", ""))
    for domain_or_name, cfg in boosts.items():
        if domain_or_name.startswith("_"):
            continue
        if domain_or_name in url_lower or domain_or_name in desc_lower:
            ak_short = cfg["archetype"]
            ak_full = next((k for k in scores if k.startswith(ak_short + "_")), None)
            if ak_full:
                scores[ak_full] = scores[ak_full] + cfg["boost"]
    return scores


def passes_geo_filter(item, source, sources_cfg):
    """If geo_filter is on for this source, check the item against allow/deny lists.
    Allow is permissive: an item with no geo info passes.
    Deny is strict: any deny match rejects."""
    if not source.get("geo_filter"):
        return True
    text = " ".join([
        normalize(item.get("title", "")),
        normalize(item.get("description", "")),
        normalize(item.get("location", "")),
    ])
    for deny in sources_cfg.get("geo_deny_keywords", []):
        if normalize(deny) in text:
            return False
    # If there's any explicit geo, at least one allow keyword must match.
    # If there's no geo signal at all, default to allow (avoid false negatives).
    has_any_geo = any(loc in text for loc in [
        "europe", "africa", "asia", "america", "remote", "based in", "location:",
        "headquarter", "office in"
    ])
    if not has_any_geo:
        return True
    for allow in sources_cfg.get("geo_allow_keywords", []):
        if normalize(allow) in text:
            return True
    return False


# ---------------- Main ----------------

def main():
    started = time.time()
    sources_cfg, keywords_cfg = load_config()
    sources = sources_cfg["sources"]

    all_items = []
    source_stats = []

    for source in sources:
        name = source["name"]
        stype = source["type"]
        if source.get("disabled"):
            log(f"skipped (disabled)", source=name)
            continue
        fetcher = FETCHERS.get(stype)
        if not fetcher:
            log(f"no fetcher for type {stype}", source=name, level="warn")
            continue
        t0 = time.time()
        try:
            raw = fetcher(source)
            elapsed = time.time() - t0
            log(f"fetched {len(raw)} items in {elapsed:.1f}s", source=name)
            kept = 0
            for item in raw:
                if not item.get("title") or not item.get("url"):
                    continue
                if is_excluded_title(item["title"]):
                    continue
                if is_excluded_language(item.get("title", ""), item.get("description", "")):
                    continue
                if not passes_geo_filter(item, source, sources_cfg):
                    continue
                scores = score_against_archetypes(item, keywords_cfg)
                scores = apply_publisher_boost(item, scores, keywords_cfg)
                top_archetype = max(scores, key=scores.get) if scores else None
                top_score = scores.get(top_archetype, 0) if top_archetype else 0
                breadth_bonus = sum(0.5 for s in scores.values() if s > 0) - 0.5
                breadth_bonus = max(0, breadth_bonus)
                fit = round(top_score + breadth_bonus, 1)
                if fit < 2:
                    continue
                all_items.append({
                    "id": make_id(item["url"], item["title"]),
                    "title": item["title"][:300],
                    "url": item["url"],
                    "description": item.get("description", "")[:500],
                    "published": item.get("published", ""),
                    "location": item.get("location", ""),
                    "source": name,
                    "archetype": top_archetype,
                    "archetype_label": keywords_cfg.get(top_archetype, {}).get("label", top_archetype),
                    "fit": fit,
                    "scores": {k: round(v, 1) for k, v in scores.items()},
                })
                kept += 1
            source_stats.append({"name": name, "raw": len(raw), "kept": kept, "elapsed_s": round(elapsed, 1), "ok": True})
        except Exception as e:
            elapsed = time.time() - t0
            level = "warn" if not source.get("robust") else "error"
            log(f"FAILED after {elapsed:.1f}s: {e}", source=name, level=level)
            source_stats.append({"name": name, "raw": 0, "kept": 0, "elapsed_s": round(elapsed, 1), "ok": False, "error": str(e)[:200]})

    # Dedup by id, keep highest-fit copy
    by_id = {}
    for it in all_items:
        existing = by_id.get(it["id"])
        if not existing or it["fit"] > existing["fit"]:
            by_id[it["id"]] = it

    # Secondary dedup: same title appearing multiple times within the same source
    # (LinkedIn aggregator queries often return the same job under multiple URLs)
    by_title_source = {}
    for it in by_id.values():
        key = (normalize(it["title"]), it["source"])
        existing = by_title_source.get(key)
        if not existing or it["fit"] > existing["fit"]:
            by_title_source[key] = it
    deduped = sorted(by_title_source.values(), key=lambda x: x["fit"], reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(deduped),
        "items": deduped,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    run_log = {
        "generated_at": payload["generated_at"],
        "elapsed_total_s": round(time.time() - started, 1),
        "sources_total": len(sources),
        "sources_ok": sum(1 for s in source_stats if s["ok"]),
        "sources_failed": sum(1 for s in source_stats if not s["ok"]),
        "items_total_raw": sum(s["raw"] for s in source_stats),
        "items_kept": len(deduped),
        "sources": source_stats,
    }
    with open(LOG_FILE, "w") as f:
        json.dump(run_log, f, indent=2, ensure_ascii=False)

    log(f"DONE: {len(deduped)} items kept across {run_log['sources_ok']}/{run_log['sources_total']} sources in {run_log['elapsed_total_s']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
