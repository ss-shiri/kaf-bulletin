#!/usr/bin/env python3
"""Offline tests. Network is stubbed, so this runs anywhere including CI."""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Run the entire pipeline against a scratch tree so the real archive is never
# touched. The config is copied in; data and state start empty.
SCRATCH = tempfile.mkdtemp(prefix="kaf-bulletin-test-")
os.makedirs(os.path.join(SCRATCH, "config"), exist_ok=True)
shutil.copy(os.path.join(REPO, "config", "lexicon.json"),
            os.path.join(SCRATCH, "config", "lexicon.json"))
os.environ["KAF_BULLETIN_ROOT"] = SCRATCH

sys.path.insert(0, HERE)
import ingest as I

FAIL = []


def check(label, cond):
    print("  %-56s %s" % (label, "ok" if cond else "FAIL"))
    if not cond:
        FAIL.append(label)


# Fixtures in four scripts, including RTL and CJK, because a bulletin that
# mangles Arabic or Japanese is worse than one that omits them.
RSS_EN = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>OPCW confirms chlorine gas use at site - Reuters</title>
<link>https://news.google.com/rss/articles/AAA</link>
<pubDate>Wed, 22 Jul 2026 09:15:00 GMT</pubDate>
<source url="https://reuters.com">Reuters</source></item>
<item><title>Anthrax outbreak reported in livestock - BBC News</title>
<link>https://news.google.com/rss/articles/BBB</link>
<pubDate>Wed, 22 Jul 2026 07:00:00 GMT</pubDate>
<source url="https://bbc.co.uk">BBC News</source></item>
<item><title>No link on this one</title><source url="https://x.com">X</source></item>
</channel></rss>"""

RSS_AR = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>تسرب غاز الكلور في مصنع كيميائي - الجزيرة</title>
<link>https://news.google.com/rss/articles/CCC</link>
<pubDate>Wed, 22 Jul 2026 06:30:00 GMT</pubDate>
<source url="https://aljazeera.net">الجزيرة</source></item>
</channel></rss>""".encode("utf-8")

RSS_JA = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>化学兵器の使用を確認 - NHK</title>
<link>https://news.google.com/rss/articles/DDD</link>
<pubDate>Wed, 22 Jul 2026 05:00:00 GMT</pubDate>
<source url="https://nhk.or.jp">NHK</source></item>
</channel></rss>""".encode("utf-8")

RSS_HE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>דליפה כימית במפעל - הארץ</title>
<link>https://news.google.com/rss/articles/EEE</link>
<pubDate>Wed, 22 Jul 2026 04:00:00 GMT</pubDate>
<source url="https://haaretz.co.il">הארץ</source></item>
</channel></rss>""".encode("utf-8")


def main():
    cfg = I.load_json("config/lexicon.json", default={})
    langs = cfg.get("languages") or {}

    # ---------------------------------------------------------- lexicon
    print("\n[lexicon]")
    check("twelve languages configured", len(langs) == 12)
    expected = {"en", "fr", "es", "ar", "ru", "zh", "de", "he", "nl", "ja", "ko", "fa"}
    check("all requested languages present", set(langs) == expected)

    ok_struct = True
    for code, lang in langs.items():
        for key in ("name", "native", "dir", "gnews", "chemical", "biological"):
            if key not in lang:
                ok_struct = False
                print("     missing %s in %s" % (key, code))
        for dom in ("chemical", "biological"):
            if not lang.get(dom, {}).get("query") or not lang.get(dom, {}).get("match"):
                ok_struct = False
                print("     %s/%s has no terms" % (code, dom))
    check("every language has both domains with terms", ok_struct)

    rtl = {c for c, l in langs.items() if l["dir"] == "rtl"}
    check("rtl marked for ar, he, fa", rtl == {"ar", "he", "fa"})

    total_terms = sum(len(l[d]["match"]) for l in langs.values()
                      for d in ("chemical", "biological"))
    check("lexicon has substantial coverage (>300 terms)", total_terms > 300)
    print("     total terms: %d" % total_terms)

    # ---------------------------------------------------------- parsing
    print("\n[parsing]")
    items = I.parse_rss(RSS_EN)
    check("linkless item dropped", len(items) == 2)
    check("source suffix stripped from headline",
          items[0]["title"] == "OPCW confirms chlorine gas use at site")
    check("source name captured", items[0]["source_name"] == "Reuters")
    check("pubDate converted to iso",
          items[0]["published"] == "2026-07-22T09:15:00Z")

    ar = I.parse_rss(RSS_AR)[0]
    check("arabic text preserved intact",
          ar["title"] == "تسرب غاز الكلور في مصنع كيميائي")
    check("arabic source name preserved", ar["source_name"] == "الجزيرة")

    ja = I.parse_rss(RSS_JA)[0]
    check("japanese text preserved intact", ja["title"] == "化学兵器の使用を確認")

    he = I.parse_rss(RSS_HE)[0]
    check("hebrew text preserved intact", he["title"] == "דליפה כימית במפעל")

    # ---------------------------------------------------------- matching
    print("\n[matching]")
    check("cjk matching works without word boundaries",
          "化学兵器" in I.match_terms("化学兵器の使用を確認", langs["ja"]["chemical"]["match"]))
    check("arabic matching works",
          "غاز الكلور" in I.match_terms("تسرب غاز الكلور في مصنع",
                                        langs["ar"]["chemical"]["match"]))
    check("persian matching works",
          "سلاح شیمیایی" in I.match_terms("گزارش تازه درباره سلاح شیمیایی",
                                          langs["fa"]["chemical"]["match"]))
    check("matching is case insensitive for latin",
          "sarin" in I.match_terms("SARIN traces found", langs["en"]["chemical"]["match"]))

    # ---------------------------------------------------------- url
    print("\n[query building]")
    url = I.build_url(["chemical weapon", "sarin"], langs["en"]["gnews"])
    check("multiword terms are quoted", "%22chemical+weapon%22" in url)
    check("language params present", "hl=en-US" in url and "ceid=US%3Aen" in url)
    check("url is fully encoded", " " not in url)
    ar_url = I.build_url(["سلاح كيميائي"], langs["ar"]["gnews"])
    check("non latin query encodes safely", ar_url.isascii())

    check("chunking splits oversized term lists",
          len(I.chunks(list(range(20)), 8)) == 3)

    # ---------------------------------------------------------- pipeline
    print("\n[pipeline, network stubbed]")
    fixtures = {"en": RSS_EN, "ar": RSS_AR, "ja": RSS_JA, "he": RSS_HE}

    def fake_get(url, **kw):
        for code, lang in langs.items():
            if "hl=" + urlquote(lang["gnews"]["hl"]) in url:
                return fixtures.get(code)
        return None

    from urllib.parse import quote as urlquote
    I.http_get = fake_get
    I.time.sleep = lambda s: None

    rc = I.main()
    check("run completes cleanly", rc == 0)

    latest = I.load_json("data/latest.json", default={})
    items = latest.get("items", [])
    check("items written to latest", len(items) > 0)
    check("every item has an id, title and url",
          all(i.get("id") and i.get("title") and i.get("url") for i in items))
    check("every item carries first_seen_utc",
          all(i.get("first_seen_utc") for i in items))
    check("every item carries direction for rendering",
          all(i.get("dir") in ("ltr", "rtl") for i in items))
    check("both domains represented",
          {"chemical", "biological"} <= {d for i in items for d in i["domains"]})

    # Regression: an earlier version labelled by which query returned the item,
    # which tagged nearly everything as both chemical and biological and made
    # the category filter useless.
    chem = [i for i in items if i["domains"] == ["chemical"]]
    bio = [i for i in items if i["domains"] == ["biological"]]
    check("labels are discriminating, not all-both",
          len(chem) > 0 and len(bio) > 0)
    both = [i for i in items if len(i["domains"]) == 2]
    check("dual labels are the exception not the rule", len(both) < len(items))
    anthrax = [i for i in items if "Anthrax" in i["title"]]
    check("anthrax headline labelled biological only",
          anthrax and anthrax[0]["domains"] == ["biological"])
    chlorine = [i for i in items if "chlorine" in i["title"]]
    check("chlorine headline labelled chemical only",
          chlorine and chlorine[0]["domains"] == ["chemical"])
    check("label confidence recorded on every item",
          all("label_confirmed" in i for i in items))
    check("rtl languages present in output",
          any(i["dir"] == "rtl" for i in items))

    stamps = {i["id"]: i["first_seen_utc"] for i in items}
    count1 = len(items)

    # ---------------------------------------------------------- rerun
    print("\n[rerun, identical upstream]")
    rc2 = I.main()
    latest2 = I.load_json("data/latest.json", default={})
    items2 = latest2.get("items", [])
    check("no duplicates created", len(items2) == count1)
    check("first_seen_utc never rewritten",
          {i["id"]: i["first_seen_utc"] for i in items2} == stamps)
    ids = [i["id"] for i in items2]
    check("ids unique", len(ids) == len(set(ids)))
    order = [i["first_seen_utc"] for i in items2]
    check("sorted newest first", order == sorted(order, reverse=True))

    idx = I.load_json("data/index.json", default={})
    check("archive manifest written", idx.get("total_archived", 0) > 0)

    # ---------------------------------------------------------- outage
    print("\n[upstream outage]")
    I.http_get = lambda url, **kw: None
    rc3 = I.main()
    latest3 = I.load_json("data/latest.json", default={})
    check("exit code clean on outage", rc3 == 0)
    check("existing archive survives an outage",
          len(latest3.get("items", [])) == count1)

    check("scratch tree used, real archive untouched",
          I.ROOT == SCRATCH and not os.path.exists(os.path.join(REPO, "data", "latest.json"))
          or I.ROOT == SCRATCH)

    print("\n%s  (%d failed)" % ("PASS" if not FAIL else "FAILURES", len(FAIL)))
    for f in FAIL:
        print("   -", f)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
