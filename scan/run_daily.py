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


def make_id(url, title):
    h = hashlib.sha256(f"{url}|{title}".encode()).hexdigest()[:12]
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
    r = requests.get(source["url"], headers={"User-Agent": UA, "Accept": "text/html"}, timeout=TIMEOUT)
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


FETCHERS = {
    "rss": fetch_rss,
    "api": fetch_reliefweb,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "html": fetch_html,
    "linkedin_rss": fetch_linkedin_rss,
}


# ---------------- Scoring ----------------

EXCLUDE_TITLE_PATTERNS = [
    "intern", "internship", "trainee program", "graduate program", "graduate scheme",
    "driver", "receptionist", "secretary", "cleaner", "janitor", "security guard",
    "administrative assistant", "executive assistant", "office assistant",
    "junior analyst", "entry level", "entry-level", "apprentice",
    "data entry", "data clerk",
    "position title",
]


def is_excluded_title(title):
    t = normalize(title)
    if not t or len(t) < 6:
        return True
    for pat in EXCLUDE_TITLE_PATTERNS:
        if pat in t:
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
                if not passes_geo_filter(item, source, sources_cfg):
                    continue
                scores = score_against_archetypes(item, keywords_cfg)
                scores = apply_publisher_boost(item, scores, keywords_cfg)
                top_archetype = max(scores, key=scores.get) if scores else None
                top_score = scores.get(top_archetype, 0) if top_archetype else 0
                breadth_bonus = sum(0.5 for s in scores.values() if s > 0) - 0.5
                breadth_bonus = max(0, breadth_bonus)
                fit = round(top_score + breadth_bonus, 1)
                if fit < 1:
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
    deduped = sorted(by_id.values(), key=lambda x: x["fit"], reverse=True)

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
