#!/usr/bin/env python3
"""
KAF BULLETIN ingest.

Crawls Google News RSS once per language per domain, using the lexicon in
config/lexicon.json. Headlines are stored exactly as published, in their own
script and language. Nothing is translated, nothing is rewritten, nothing is
scored for accuracy. The source is the only claim the bulletin makes.

Standard library only. No pip install step in CI means nothing to break when
an upstream package changes.
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# KAF_BULLETIN_ROOT lets the test suite point the whole pipeline at a scratch
# directory. Without it a test run would write fixture headlines straight into
# the real archive, and CI would commit them.
ROOT = os.environ.get(
    "KAF_BULLETIN_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
GNEWS = "https://news.google.com/rss/search"
UA = ("Mozilla/5.0 (compatible; kaf-bulletin/1.0; "
      "+https://github.com/ss-shiri/kaf-bulletin)")


# ----------------------------------------------------------------- io

def path(*parts):
    return os.path.join(ROOT, *parts)


def load_json(rel, default=None):
    try:
        with open(path(rel), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default if default is not None else {}


def save_json(rel, obj):
    p = path(rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, p)


# ----------------------------------------------------------------- time

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def days_ago_str(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def rfc822_to_iso(value):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(
            timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, IndexError):
        return None


# ----------------------------------------------------------------- text

def clean(text, limit=500):
    """Collapse whitespace and strip control characters.

    Deliberately does NOT strip or normalise non-Latin script. The headline
    is stored exactly as the publisher wrote it, in its own writing system.
    """
    if not text:
        return ""
    s = html.unescape(str(text))
    s = " ".join(s.split())
    s = "".join(ch for ch in s if ch == "\t" or ord(ch) >= 32)
    return s[:limit].strip()


def sid(*parts):
    joined = "|".join(str(p or "").strip().lower() for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def strip_source_suffix(title, source_name):
    """Google News appends ' - Source' to every headline. Remove it so the
    stored title is the publisher's actual headline, not a decorated one."""
    if not source_name:
        return title
    for sep in (" - ", " | ", " \u2013 ", " \u00b7 "):
        suffix = sep + source_name
        if title.endswith(suffix):
            return title[: -len(suffix)].strip()
    return title


def match_terms(text, terms):
    """Case-insensitive containment. Works for scripts without word
    boundaries (Chinese, Japanese, Korean) where a regex \\b would fail."""
    low = (text or "").lower()
    return [t for t in terms if t.lower() in low]


# ----------------------------------------------------------------- net

def http_get(url, retries=3, timeout=45):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
                "Accept-Language": "*",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(2.5 * (attempt + 1))
    print("    ! fetch failed (%s)" % last)
    return None


def build_url(terms, gnews):
    query = " OR ".join('"%s"' % t if " " in t else t for t in terms)
    return "%s?%s" % (GNEWS, urllib.parse.urlencode({
        "q": query,
        "hl": gnews["hl"],
        "gl": gnews["gl"],
        "ceid": gnews["ceid"],
    }))


# ----------------------------------------------------------------- parse

def _tag(node):
    return node.tag.split("}", 1)[-1] if "}" in node.tag else node.tag


def parse_rss(payload):
    """Google News RSS to a list of dicts. Pure function, unit testable."""
    root = ET.fromstring(payload)
    out = []
    for item in root.iter():
        if _tag(item) != "item":
            continue
        title = link = pub = src_name = src_url = ""
        for child in item:
            name = _tag(child)
            text = "".join(child.itertext())
            if name == "title" and not title:
                title = text
            elif name == "link" and not link:
                link = (child.get("href") or text).strip()
            elif name == "pubDate" and not pub:
                pub = text
            elif name == "source":
                src_name = text.strip()
                src_url = (child.get("url") or "").strip()
        title = clean(title)
        if not title or not link:
            continue
        out.append({
            "title": strip_source_suffix(title, src_name),
            "url": link.strip(),
            "published": rfc822_to_iso(pub),
            "source_name": clean(src_name, 120) or "unknown",
            "source_url": src_url,
        })
    return out


def chunks(seq, size):
    return [seq[i:i + size] for i in range(0, len(seq), size)]


# ----------------------------------------------------------------- archive

def merge_into_day(records):
    """Append to today's file. Append only, never rewrite an existing row."""
    rel = "data/%s.json" % today_str()
    existing = load_json(rel, default=[])
    if not isinstance(existing, list):
        existing = []
    have = {r.get("id") for r in existing}
    fresh = [r for r in records if r.get("id") not in have]
    if fresh:
        save_json(rel, existing + fresh)
    return len(fresh)


def rebuild_latest(window_days, cap):
    cutoff = days_ago_str(window_days)
    rows = []
    folder = path("data")
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".json") or name in ("latest.json", "index.json"):
                continue
            if name[:-5] < cutoff:
                continue
            part = load_json("data/%s" % name, default=[])
            if isinstance(part, list):
                rows.extend(part)
    rows.sort(key=lambda r: r.get("first_seen_utc", ""), reverse=True)
    rows = rows[:cap]

    by_lang, by_domain = {}, {}
    for r in rows:
        by_lang[r["lang"]] = by_lang.get(r["lang"], 0) + 1
        for d in r.get("domains", []):
            by_domain[d] = by_domain.get(d, 0) + 1

    save_json("data/latest.json", {
        "generated_utc": now_iso(),
        "window_days": window_days,
        "total": len(rows),
        "by_language": by_lang,
        "by_domain": by_domain,
        "items": rows,
    })
    return len(rows), by_lang, by_domain


def rebuild_index():
    days = []
    folder = path("data")
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".json") or name in ("latest.json", "index.json"):
                continue
            part = load_json("data/%s" % name, default=[])
            days.append({"date": name[:-5],
                         "count": len(part) if isinstance(part, list) else 0})
    save_json("data/index.json", {
        "generated_utc": now_iso(),
        "days": days,
        "total_archived": sum(d["count"] for d in days),
    })


def load_seen():
    return load_json("state/seen.json", default={})


def save_seen(seen, retention_days):
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    save_json("state/seen.json", pruned)
    return len(pruned)


# ----------------------------------------------------------------- main

def main():
    cfg = load_json("config/lexicon.json", default={})
    langs = cfg.get("languages") or {}
    st = cfg.get("settings") or {}
    if not langs:
        print("no languages configured")
        return 1

    chunk_size = int(st.get("chunk_size", 8))
    delay = float(st.get("request_delay_seconds", 1.2))
    window = int(st.get("latest_window_days", 10))
    cap = int(st.get("max_items_in_latest", 1200))
    retention = int(st.get("seen_retention_days", 120))

    collected = {}

    for code, lang in langs.items():
        chem_pool = (lang.get("chemical") or {}).get("match") or []
        bio_pool = (lang.get("biological") or {}).get("match") or []

        for domain in ("chemical", "biological"):
            spec = lang.get(domain) or {}
            query_terms = spec.get("query") or []
            if not query_terms:
                continue

            got = 0
            for group in chunks(query_terms, chunk_size):
                payload = http_get(build_url(group, lang["gnews"]))
                time.sleep(delay)          # be a polite crawler
                if payload is None:
                    continue
                try:
                    items = parse_rss(payload)
                except ET.ParseError:
                    print("    ! unparseable XML for %s/%s" % (code, domain))
                    continue

                for it in items:
                    rid = sid(it["title"], it["source_name"])
                    if rid in collected:
                        continue

                    # Label by what the headline actually contains, not by
                    # which query happened to return it. Using the query's
                    # domain would tag nearly every item with both labels
                    # wherever the two queries overlap, which makes the
                    # category filter meaningless.
                    chem_hits = match_terms(it["title"], chem_pool)
                    bio_hits = match_terms(it["title"], bio_pool)
                    domains = []
                    if chem_hits:
                        domains.append("chemical")
                    if bio_hits:
                        domains.append("biological")

                    # Nothing matched in the headline: the term is probably in
                    # the body. Fall back to the query that surfaced it and
                    # mark the label unconfirmed rather than dropping the item.
                    confirmed = bool(domains)
                    if not confirmed:
                        domains = [domain]

                    collected[rid] = {
                        "id": rid,
                        "title": it["title"],
                        "url": it["url"],
                        "source_name": it["source_name"],
                        "source_url": it["source_url"],
                        "lang": code,
                        "lang_name": lang["name"],
                        "lang_native": lang["native"],
                        "dir": lang["dir"],
                        "domains": domains,
                        "label_confirmed": confirmed,
                        "matched": (chem_hits + bio_hits)[:6],
                        "published_utc": it["published"],
                    }
                    got += 1

            print("  %-3s %-11s %4d" % (code, domain, got))

    if not collected:
        print("collected nothing this run")
        return 0

    seen = load_seen()
    stamp = now_iso()
    fresh = []
    for rid, rec in collected.items():
        if rid in seen:
            continue
        seen[rid] = stamp
        # Set once, on the run that first observed this headline. Never
        # rewritten, so the archive records when a story surfaced, not when
        # the file was last touched.
        rec["first_seen_utc"] = stamp
        fresh.append(rec)

    written = merge_into_day(fresh)
    kept = save_seen(seen, retention)
    total, by_lang, by_domain = rebuild_latest(window, cap)
    rebuild_index()

    print("\ncollected=%d  new=%d  written=%d  seen_index=%d"
          % (len(collected), len(fresh), written, kept))
    print("latest=%d  languages=%s" % (total, dict(sorted(by_lang.items()))))
    print("domains=%s" % by_domain)
    return 0


if __name__ == "__main__":
    sys.exit(main())
