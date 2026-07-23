#!/usr/bin/env python3
"""
Diagnostics.

Answers one question: why is the bulletin empty?

There are only four possible causes and this tells you which one you have,
instead of leaving you to guess from a blank page.

  1. The crawl never ran            -> no data/latest.json at all
  2. The runner is rate limited     -> HTTP 429 or 403
  3. The queries return nothing     -> HTTP 200, zero items
  4. It worked                      -> items present

Run it locally, or add it as a step in the workflow when something breaks.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest as I

BAR = "-" * 72


def head(text):
    print("\n" + text)
    print(BAR)


def main():
    cfg = I.load_json("config/lexicon.json", default={})
    langs = cfg.get("languages") or {}
    if not langs:
        print("FATAL: config/lexicon.json missing or unreadable")
        return 1

    # ---------------------------------------------------------- 1. archive
    head("1. ARCHIVE STATE")
    latest = I.load_json("data/latest.json", default=None)
    if not latest:
        print("  data/latest.json     MISSING")
        print("  meaning              the crawl has never completed a run")
        print("  fix                  Actions -> update -> Run workflow")
    else:
        items = latest.get("items", [])
        print("  data/latest.json     present")
        print("  generated            %s" % latest.get("generated_utc"))
        print("  items                %d" % len(items))
        if items:
            by_lang = {}
            for i in items:
                by_lang[i["lang"]] = by_lang.get(i["lang"], 0) + 1
            print("  languages present    %s" % dict(sorted(by_lang.items())))
            missing = sorted(set(langs) - set(by_lang))
            if missing:
                print("  languages EMPTY      %s" % missing)

    # ---------------------------------------------------------- 2. reachability
    head("2. SOURCE REACHABILITY  (one probe per source, English)")
    en = langs.get("en", {})
    probe_terms = (en.get("chemical") or {}).get("query", ["chemical weapon"])[:3]

    g_status, g_body = I.http_get(I.build_url(probe_terms, en["gnews"]))
    g_items = []
    if g_body:
        try:
            g_items = I.parse_rss(g_body)
        except Exception as exc:
            print("  google news        parse error: %s" % exc)
    verdict = ("BLOCKED" if g_status in (403, 429)
               else "UNREACHABLE" if g_status == 0
               else "OK" if g_items else "REACHABLE BUT EMPTY")
    print("  google news        HTTP %-4s  items=%-4d  %s"
          % (g_status or "---", len(g_items), verdict))

    d_status, d_body = I.http_get(
        I.build_gdelt_url(probe_terms, en.get("gdelt", "eng")))
    d_items = []
    if d_body:
        try:
            d_items = I.parse_gdelt(d_body)
        except Exception as exc:
            print("  gdelt              parse error: %s" % exc)
    verdict = ("BLOCKED" if d_status in (403, 429)
               else "UNREACHABLE" if d_status == 0
               else "OK" if d_items else "REACHABLE BUT EMPTY")
    print("  gdelt              HTTP %-4s  items=%-4d  %s"
          % (d_status or "---", len(d_items), verdict))

    # ---------------------------------------------------------- 3. per language
    head("3. PER LANGUAGE  (google news, chemical query, first chunk only)")
    dead = []
    for code, lang in langs.items():
        terms = (lang.get("chemical") or {}).get("query", [])[:4]
        if not terms:
            continue
        status, body = I.http_get(I.build_url(terms, lang["gnews"]))
        I.time.sleep(1.0)
        n = 0
        if body:
            try:
                n = len(I.parse_rss(body))
            except Exception:
                n = -1
        mark = ""
        if status in (403, 429):
            mark = "  BLOCKED"
        elif n == 0:
            mark = "  no results"
            dead.append(code)
        elif n < 0:
            mark = "  unparseable"
        print("  %-3s %-12s HTTP %-4s  items=%-4d%s"
              % (code, lang["name"], status or "---", max(n, 0), mark))

    # ---------------------------------------------------------- verdict
    head("VERDICT")
    if g_status in (403, 429) and d_status in (403, 429):
        print("  Both sources are refusing this IP.")
        print("  This runner is rate limited. Options, in order of effort:")
        print("    a. wait and rerun; limits are usually short")
        print("    b. widen the schedule so fewer requests land per hour")
        print("    c. run the crawler somewhere with a residential IP and")
        print("       push data/ from there")
    elif g_status in (403, 429):
        print("  Google News is refusing this IP, GDELT is not.")
        print("  The fallback should carry the bulletin. If it is not, check")
        print("  the sourcelang codes in config/lexicon.json.")
    elif not g_items and not d_items:
        print("  Both sources answered but returned nothing.")
        print("  This is a query problem, not a network problem. Check the")
        print("  'query' arrays in config/lexicon.json.")
    elif dead:
        print("  Working overall. These languages return nothing and their")
        print("  query terms probably need work: %s" % dead)
    else:
        print("  Sources are healthy. If the site is still empty, the crawl")
        print("  step was skipped or its commit did not land.")
        print("  Check: Actions -> update -> latest run -> the Crawl step.")
    print(BAR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
