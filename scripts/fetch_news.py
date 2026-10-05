#!/usr/bin/env python3
"""Ruft alle Quellen aus sources.json ab und schreibt site/data/news.json.

Unterstützte Quellentypen:
  rss    - RSS/Atom-Feed direkt
  gnews  - Google-News-Suchfeed (Feld "query" oder "gnews_query")
  api    - TED (EU) und Find a Tender (UK)
  scrape - Webseite: es wird nach einem verlinkten RSS-Feed gesucht (Autodiscovery).
           Seiten ohne Feed werden im Quellen-Status als "kein Feed" geführt.
Zusätzlich bekommt jede Quelle mit "gnews_query" einen eigenen Google-News-Feed.
"""

import datetime as dt
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

import feedparser

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources.json"
OUT = ROOT / "site" / "data" / "news.json"

USER_AGENT = "Mozilla/5.0 (compatible; MeineNewsSite/1.0; +https://github.com)"
TIMEOUT = 25
MAX_ITEMS_PER_TOPIC = 80
MAX_AGE_DAYS = {"ausschreibungen": 120, "konkurrenz": 120, "branche": 30}
DEFAULT_MAX_AGE_DAYS = 14

TED_CPV = ["35815100", "18444110", "18444100", "35113400"]
TED_FULLTEXT = ['"ballistic helmet"', '"ballistischer Schutzhelm"', "Schutzhelm", "VPAM", '"bullet resistant"']


# ---------------------------------------------------------------- HTTP

def http_get(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def http_post_json(url, payload):
    body = json.dumps(payload).encode()
    raw = http_get(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"})
    return json.loads(raw)


# ---------------------------------------------------------------- Hilfsfunktionen

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(s, limit=320):
    s = html.unescape(TAG_RE.sub(" ", s or ""))
    s = WS_RE.sub(" ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + " …"


def to_iso(value):
    """Wandelt struct_time, ISO-Strings oder TED-Datumsangaben in ISO-8601 (UTC) um."""
    if not value:
        return None
    if isinstance(value, time.struct_time):
        return dt.datetime(*value[:6], tzinfo=dt.timezone.utc).isoformat()
    if isinstance(value, str):
        v = value.strip()
        m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?))?", v)
        if m:
            t = m.group(2) or "00:00:00"
            if len(t) == 5:
                t += ":00"
            return f"{m.group(1)}T{t}+00:00"
    return None


def keyword_matcher(keywords):
    """Kurze Abkürzungen in Großbuchstaben (KI, AI, LLM) exakt, alles andere ohne Groß/Klein am Wortanfang."""
    if not keywords:
        return None
    exact = [k for k in keywords if k.isupper() and len(k) <= 4]
    loose = [k for k in keywords if k not in exact]
    parts = []
    if exact:
        parts.append(r"\b(?:" + "|".join(map(re.escape, exact)) + r")\b")
    if loose:
        parts.append(r"(?i:\b(?:" + "|".join(map(re.escape, loose)) + r"))")
    rx = re.compile("|".join(parts))
    return lambda text: bool(rx.search(text or ""))


def make_item(topic, source, title, link, published=None, summary="", publisher=None, extra=None):
    title = clean_text(title, 300)
    if not title or not link:
        return None
    item = {
        "id": hashlib.sha1(link.encode()).hexdigest()[:12],
        "topic": topic,
        "title": title,
        "link": link,
        "source": publisher or source,
        "via": source,
        "published": published,
        "summary": clean_text(summary),
    }
    if extra:
        item.update(extra)
    return item


# ---------------------------------------------------------------- Feeds

def parse_feed(raw, topic, source_name, is_gnews=False):
    feed = feedparser.parse(raw)
    if feed.bozo and not feed.entries:
        raise ValueError(f"Kein gültiger Feed ({feed.bozo_exception.__class__.__name__})")
    items = []
    for e in feed.entries:
        title = e.get("title", "")
        publisher = None
        if is_gnews:
            src = e.get("source") or {}
            publisher = src.get("title") if isinstance(src, dict) else None
            if publisher and title.endswith(" - " + publisher):
                title = title[: -len(publisher) - 3]
        summary = "" if is_gnews else (e.get("summary") or e.get("description") or "")
        published = to_iso(e.get("published_parsed") or e.get("updated_parsed")) or to_iso(e.get("published"))
        it = make_item(topic, source_name, title, e.get("link"), published, summary, publisher)
        if it:
            items.append(it)
    return items


def gnews_url(template, query):
    return template.replace("{query}", urllib.parse.quote(query))


class FeedLinkFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = dict(attrs)
        if (a.get("rel") or "").lower() == "alternate" and "xml" in (a.get("type") or "") and a.get("href"):
            self.links.append(a["href"])


def discover_feed(page_url):
    raw = http_get(page_url)
    finder = FeedLinkFinder()
    finder.feed(raw.decode("utf-8", "replace"))
    # Kommentar-Feeds überspringen
    links = [l for l in finder.links if "comment" not in l.lower()]
    return urllib.parse.urljoin(page_url, links[0]) if links else None


# ---------------------------------------------------------------- APIs

def fetch_ted(topic, source):
    since = (dt.date.today() - dt.timedelta(days=MAX_AGE_DAYS["ausschreibungen"])).strftime("%Y%m%d")
    cpv = " ".join(TED_CPV)
    queries = [
        f"(classification-cpv IN ({cpv}) OR FT ~ ({' OR '.join(TED_FULLTEXT)})) AND publication-date >= {since} SORT BY publication-date DESC",
        f"classification-cpv IN ({cpv}) AND publication-date >= {since} SORT BY publication-date DESC",
    ]
    fields = ["publication-number", "notice-title", "buyer-name", "buyer-country",
              "publication-date", "deadline-receipt-tender-date-lot", "notice-type"]
    last_err = None
    for q in queries:
        try:
            data = http_post_json(source["url"], {
                "query": q, "fields": fields, "limit": 100, "page": 1,
                "scope": "ALL", "paginationMode": "PAGE_NUMBER",
            })
            break
        except urllib.error.HTTPError as e:
            last_err = e
    else:
        raise last_err

    def pick_lang(v):
        if isinstance(v, dict):
            for lang in ("deu", "DEU", "eng", "ENG"):
                if v.get(lang):
                    return pick_lang(v[lang])
            return pick_lang(next(iter(v.values()), ""))
        if isinstance(v, list):
            return pick_lang(v[0]) if v else ""
        return str(v or "")

    items = []
    for n in data.get("notices", []):
        pub_no = n.get("publication-number")
        if not pub_no:
            continue
        deadline = to_iso(pick_lang(n.get("deadline-receipt-tender-date-lot")))
        country = pick_lang(n.get("buyer-country"))
        buyer = pick_lang(n.get("buyer-name"))
        it = make_item(
            topic, source["name"], pick_lang(n.get("notice-title")) or f"TED {pub_no}",
            f"https://ted.europa.eu/de/notice/-/detail/{pub_no}",
            to_iso(pick_lang(n.get("publication-date"))),
            " · ".join(x for x in (buyer, country) if x),
            publisher="TED",
            extra={"deadline": deadline, "buyer": buyer or None},
        )
        if it:
            items.append(it)
    return items


def fetch_find_a_tender(topic, source, match):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    url = source["url"] + "?" + urllib.parse.urlencode({"updatedFrom": since, "stages": "tender", "limit": 100})
    data = json.loads(http_get(url, headers={"Accept": "application/json"}))
    prefixes = tuple(c[:6] for c in TED_CPV)
    items = []
    for rel in data.get("releases", []):
        tender = rel.get("tender") or {}
        cpvs = [((tender.get("classification") or {}).get("id") or "")]
        cpvs += [((i.get("classification") or {}).get("id") or "") for i in tender.get("items", [])]
        text = f"{tender.get('title', '')} {tender.get('description', '')}"
        if not (any(c.startswith(prefixes) for c in cpvs) or (match and match(text))):
            continue
        notice_id = rel.get("id", "")
        it = make_item(
            topic, source["name"], tender.get("title") or notice_id,
            f"https://www.find-tender.service.gov.uk/Notice/{notice_id}",
            to_iso(rel.get("date")), tender.get("description", ""),
            publisher="Find a Tender",
            extra={"deadline": to_iso((tender.get("tenderPeriod") or {}).get("endDate"))},
        )
        if it:
            items.append(it)
    return items


# ---------------------------------------------------------------- Ablauf

def build_jobs(cfg):
    template = cfg["gnews_template"]
    jobs = []
    for topic in cfg["topics"]:
        tid = topic["id"]
        for s in topic["sources"]:
            stype = s.get("type")
            if stype == "rss" and s.get("url"):
                jobs.append({"topic": tid, "name": s["name"], "kind": "rss", "url": s["url"], "filter": s.get("filter")})
            elif stype == "gnews" and (s.get("query") or s.get("gnews_query")):
                q = s.get("query") or s.get("gnews_query")
                jobs.append({"topic": tid, "name": s["name"], "kind": "gnews", "url": gnews_url(template, q), "filter": s.get("filter")})
            elif stype == "api":
                jobs.append({"topic": tid, "name": s["name"], "kind": "api", "url": s["url"], "source": s})
            elif stype == "scrape" and s.get("url"):
                jobs.append({"topic": tid, "name": s["name"], "kind": "scrape", "url": s["url"], "filter": s.get("filter")})
            # Zusätzlicher Google-News-Feed (z. B. für jeden Wettbewerber)
            if s.get("gnews_query") and stype != "gnews":
                jobs.append({"topic": tid, "name": f"{s['name']} (Google News)", "kind": "gnews",
                             "url": gnews_url(template, s["gnews_query"]), "filter": None})
    return jobs


def run_job(job, matchers):
    match = matchers.get(job["topic"]) if job.get("filter") == "keywords" else None
    status = {"name": job["name"], "topic": job["topic"], "kind": job["kind"], "url": job["url"], "ok": False, "count": 0}
    try:
        if job["kind"] == "api":
            if "ted.europa.eu" in job["url"]:
                items = fetch_ted(job["topic"], job["source"])
            elif "find-tender" in job["url"]:
                items = fetch_find_a_tender(job["topic"], job["source"], matchers.get(job["topic"]))
            else:
                raise ValueError("API nicht unterstützt")
        elif job["kind"] == "scrape":
            feed_url = discover_feed(job["url"])
            if not feed_url:
                status["error"] = "kein RSS-Feed auf der Seite, eigener Abruf folgt später"
                return status, []
            status["feed"] = feed_url
            items = parse_feed(http_get(feed_url), job["topic"], job["name"])
        else:
            items = parse_feed(http_get(job["url"]), job["topic"], job["name"], is_gnews=job["kind"] == "gnews")
        total = len(items)
        if match:
            items = [i for i in items if match(f"{i['title']} {i['summary']}")]
        status.update(ok=True, count=len(items), total=total)
        return status, items
    except Exception as e:  # noqa: BLE001 - jede Quelle darf einzeln scheitern
        status["error"] = f"{e.__class__.__name__}: {str(e)[:160]}"
        return status, []


def finalize(cfg, all_items):
    now = dt.datetime.now(dt.timezone.utc)
    topics_out = []
    for topic in sorted(cfg["topics"], key=lambda t: t.get("priority", 9)):
        tid = topic["id"]
        max_age = dt.timedelta(days=MAX_AGE_DAYS.get(tid, DEFAULT_MAX_AGE_DAYS))
        seen_links, seen_titles, items = set(), set(), []
        for it in sorted((i for i in all_items if i["topic"] == tid),
                         key=lambda i: i["published"] or "", reverse=True):
            norm = re.sub(r"\W+", "", it["title"].lower())[:90]
            if it["link"] in seen_links or norm in seen_titles:
                continue
            if it["published"]:
                pub = dt.datetime.fromisoformat(it["published"])
                if now - pub > max_age:
                    continue
                if pub > now + dt.timedelta(hours=2):  # Zukunftsdaten kappen
                    it["published"] = now.isoformat()
            seen_links.add(it["link"])
            seen_titles.add(norm)
            items.append(it)
        topics_out.append({"id": tid, "name": topic["name"], "short": topic.get("short", topic["name"]), "priority": topic.get("priority"),
                           "items": items[:MAX_ITEMS_PER_TOPIC]})
    return topics_out


def main():
    cfg = json.loads(SOURCES.read_text(encoding="utf-8"))
    matchers = {t["id"]: keyword_matcher(t.get("keywords")) for t in cfg["topics"]}
    jobs = build_jobs(cfg)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda j: run_job(j, matchers), jobs))
    statuses = [r[0] for r in results]
    items = [i for r in results for i in r[1]]
    out = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "topics": finalize(cfg, items),
        "sources": statuses,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(s["ok"] for s in statuses)
    print(f"{ok}/{len(statuses)} Quellen ok, {sum(len(t['items']) for t in out['topics'])} Meldungen -> {OUT}")
    for s in statuses:
        if not s["ok"]:
            print(f"  ✗ {s['name']}: {s.get('error')}", file=sys.stderr)
    # Nur scheitern, wenn gar nichts geklappt hat (z. B. kein Netz)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
