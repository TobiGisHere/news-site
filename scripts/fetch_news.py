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
import io
import zipfile
import html
import http.cookiejar
import json
import os
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
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TIMEOUT = 25
MAX_ITEMS_PER_TOPIC = 80
MAX_AGE_DAYS = {"ausschreibungen": 365, "konkurrenz": 120, "branche": 30}
DEFAULT_MAX_AGE_DAYS = 14

# Nur Schutzkopfbedeckungen/Helme (18444…), keine Westen oder Schutzkleidung.
TED_CPV = ["18444000", "18444100", "18444110"]
TED_CPV_PREFIXES = ("18444",)
TED_FULLTEXT = ['"ballistic helmet"', '"ballistischer Schutzhelm"', "Schutzhelm", "Gefechtshelm"]


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


def require_matcher(required, excluded=None):
    """Teilwort-Suche ohne Groß/Klein: "helm" trifft auch "Schutzhelme" oder "Gefechtshelm"."""
    if not required:
        return None
    req = re.compile("|".join(map(re.escape, required)), re.I)
    exc = re.compile("|".join(map(re.escape, excluded)), re.I) if excluded else None

    def check(text):
        if exc:
            text = exc.sub(" ", text or "")
        return bool(req.search(text or ""))
    return check


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

IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)", re.I)


def entry_image(e):
    """Vorschaubild aus media:thumbnail, media:content, Enclosure oder erstem <img>."""
    for key in ("media_thumbnail", "media_content"):
        for m in e.get(key) or []:
            url = m.get("url")
            if url and (key == "media_thumbnail" or "image" in (m.get("type") or "image") or m.get("medium") == "image"):
                return url
    for enc in e.get("enclosures") or []:
        if (enc.get("type") or "").startswith("image") and enc.get("href"):
            return enc["href"]
    for c in [e.get("summary", "")] + [x.get("value", "") for x in e.get("content") or []]:
        m = IMG_RE.search(c or "")
        if m and not m.group(1).startswith("data:"):
            return html.unescape(m.group(1))
    return None


def domain_of(url):
    host = urllib.parse.urlparse(url or "").hostname or ""
    return host[4:] if host.startswith("www.") else host


def parse_feed(raw, topic, source_name, is_gnews=False):
    feed = feedparser.parse(raw)
    if feed.bozo and not feed.entries:
        raise ValueError(f"Kein gültiger Feed ({feed.bozo_exception.__class__.__name__})")
    items = []
    for e in feed.entries:
        title = e.get("title", "")
        publisher = None
        domain = domain_of(e.get("link"))
        if is_gnews:
            src = e.get("source") or {}
            publisher = src.get("title") if isinstance(src, dict) else None
            if publisher and title.endswith(" - " + publisher):
                title = title[: -len(publisher) - 3]
            domain = domain_of(src.get("href")) if isinstance(src, dict) else ""
        summary = "" if is_gnews else (e.get("summary") or e.get("description") or "")
        published = to_iso(e.get("published_parsed") or e.get("updated_parsed")) or to_iso(e.get("published"))
        img = None if is_gnews else entry_image(e)
        if img and img.startswith("//"):
            img = "https:" + img
        it = make_item(topic, source_name, title, e.get("link"), published, summary, publisher,
                       extra={"image": img if img and img.startswith("https://") else None, "domain": domain or None})
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


# ---------------------------------------------------------------- Webseiten ohne Feed

MONTHS = {m: i + 1 for i, names in enumerate([
    ("jan", "januar", "january", "jän"), ("feb", "februar", "february"), ("mar", "mär", "märz", "march", "maerz"),
    ("apr", "april"), ("may", "mai"), ("jun", "juni", "june"), ("jul", "juli", "july"), ("aug", "august"),
    ("sep", "sept", "september"), ("oct", "okt", "oktober", "october"), ("nov", "november"), ("dec", "dez", "dezember", "december"),
]) for m in names}
DATE_PATTERNS = [
    (re.compile(r'datetime="(\d{4}-\d{2}-\d{2})'), lambda m: m.group(1)),
    (re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.\s?(20\d{2})\b"), lambda m: f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"),
    (re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"), lambda m: f"{m.group(1)}-{m.group(2)}-{m.group(3)}"),
    (re.compile(r"\b([A-Za-zäÄ]{3,9})\.? (\d{1,2}),? (20\d{2})\b"),
     lambda m: f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}" if m.group(1).lower() in MONTHS else None),
    (re.compile(r"\b(\d{1,2})\.? ([A-Za-zäÄ]{3,9})\.? (20\d{2})\b"),
     lambda m: f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}" if m.group(2).lower() in MONTHS else None),
]


def find_date(text, mdy=False):
    if mdy:  # US-Format MM.DD.YYYY
        text = re.sub(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b", r"\2.\1.\3", text or "")
    for rx, conv in DATE_PATTERNS:
        for m in rx.finditer(text or ""):
            try:
                d = conv(m)
                if d:
                    dt.date.fromisoformat(d)
                    return d
            except (ValueError, KeyError):
                continue
    return None


class AnchorParser(HTMLParser):
    """Sammelt Links mit Text; Überschriften innerhalb eines Links werden als Titel bevorzugt."""
    HEADINGS = {"h1", "h2", "h3", "h4", "h5"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            a = dict(attrs)
            self.stack.append({"href": a.get("href"), "title": a.get("title") or a.get("aria-label") or "",
                               "text": "", "heading": "", "in_h": False})
        elif tag in self.HEADINGS and self.stack:
            self.stack[-1]["in_h"] = True

    def handle_endtag(self, tag):
        if tag == "a" and self.stack:
            self.anchors.append(self.stack.pop())
        elif tag in self.HEADINGS and self.stack:
            self.stack[-1]["in_h"] = False

    def handle_data(self, data):
        if self.stack:
            cur = self.stack[-1]
            cur["text"] += data + " "
            if cur["in_h"]:
                cur["heading"] += data + " "


def scrape_page(job, previous_links):
    cfg = job["source"]
    page = cfg.get("list_url") or cfg["url"]
    raw = http_get(page).decode("utf-8", "replace")
    pattern = re.compile(cfg["link_pattern"])
    strip = re.compile(cfg["title_strip"], re.I) if cfg.get("title_strip") else None
    parser = AnchorParser()
    parser.feed(raw)
    hrefs_pos = sorted({raw.find(a["href"]) for a in parser.anchors
                        if a["href"] and pattern.search(urllib.parse.urljoin(page, a["href"]))} - {-1})
    mdy = cfg.get("date_order") == "mdy"
    known_source = any(l.get("via") == job["name"] for l in previous_links.values())
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    items, seen = [], set()
    for a in parser.anchors:
        if not a["href"]:
            continue
        url = urllib.parse.urljoin(page, a["href"]).split("#")[0]
        if url in seen or not pattern.search(url):
            continue
        title = clean_text(a["heading"]) or clean_text(a["text"]) or clean_text(a["title"])
        if strip:
            title = clean_text(strip.sub(" ", title))
        if len(title) < 20:
            continue
        seen.add(url)
        # Datum im Linktext, sonst in der Umgebung des Links im HTML suchen
        where = cfg.get("date_position", "auto")
        published = find_date(a["text"], mdy) if where != "none" else None
        if not published and where != "none":
            # Nur im eigenen Abschnitt suchen: zwischen vorherigem und nächstem Artikel-Link
            pos = raw.find(a["href"])
            if pos >= 0:
                nxt = min([p for p in hrefs_pos if p > pos] + [pos + 1200])
                prv = max([p for p in hrefs_pos if p < pos] + [max(0, pos - 800)])
                after = TAG_RE.sub(" ", raw[pos:min(nxt, pos + 1200)])
                before = TAG_RE.sub(" ", raw[max(prv, pos - 800):pos])
                if where == "before":
                    published = find_date(before[-200:], mdy)
                else:
                    published = find_date(after, mdy) or find_date(before, mdy)
        if published and published > (dt.date.today() + dt.timedelta(days=1)).isoformat():
            published = None  # Datum in der Zukunft = vermutlich Veranstaltungstermin
        published = to_iso(published) if published else None
        if not published:
            prev = previous_links.get(url)
            # Neu aufgetauchte Links bekommen den Zeitpunkt des ersten Funds
            published = prev.get("published") if prev else (now if known_source else None)
        it = make_item(job["topic"], job["name"], title, url, published,
                       extra={"domain": domain_of(url), "image": None})
        if it:
            items.append(it)
        if len(items) >= cfg.get("max_items", 12):
            break
    if not items:
        raise ValueError("keine Artikel-Links gefunden (Seitenaufbau geändert?)")
    return items


# ---------------------------------------------------------------- Bekanntmachungsservice (oeffentlichevergabe.de)

OEV_EXPORT = "https://oeffentlichevergabe.de/api/notice-exports?pubDay={day}&format=ocds.zip"


def fetch_oev(topic, source, days):
    """Lädt die täglichen OCDS-Exporte aller deutschen Bekanntmachungen (u. a. DTVP, evergabe-online)."""
    items, loaded = [], 0
    for back in range(days):
        day = (dt.date.today() - dt.timedelta(days=back)).isoformat()
        try:
            raw = http_get(OEV_EXPORT.format(day=day))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                continue
            raise
        if raw[:2] != b"PK":
            continue
        loaded += 1
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for name in z.namelist():
                try:
                    rel = json.loads(z.read(name))["releases"][0]
                except (KeyError, IndexError, ValueError):
                    continue
                if "tender" not in (rel.get("tag") or []):
                    continue
                t = rel.get("tender") or {}
                cpvs = [((i.get("classification") or {}).get("id") or "") for i in t.get("items") or []]
                lots = " ".join(f"{l.get('title', '')} {l.get('description', '')}" for l in t.get("lots") or [])
                text = f"{t.get('description', '')} {lots}"
                # Vorfilter, die endgültige Auswahl passiert über require_keywords des Themas
                if not (any(c.startswith(TED_CPV_PREFIXES) for c in cpvs) or re.search(r"helm|kopfschutz", f"{t.get('title', '')} {text}", re.I)):
                    continue
                docs = [d.get("url") for d in t.get("documents") or [] if d.get("url")]
                buyer = (rel.get("buyer") or {}).get("name")
                link = docs[0] if docs else f"https://oeffentlichevergabe.de/ui/de/search/details?noticeId={rel.get('id')}"
                it = make_item(topic, source["name"], t.get("title") or "Bekanntmachung", link,
                               to_iso(rel.get("date")), text, publisher="Bekanntmachungsservice",
                               extra={"buyer": buyer, "country": "DEU", "domain": domain_of(link)})
                if it:
                    items.append(it)
    if not loaded:
        raise ValueError("keine Tagesexporte verfügbar")
    return items


# ---------------------------------------------------------------- simap.ch (Schweiz)

SIMAP_API = "https://www.simap.ch/api/publications/v2/project/project-search?lang=de&search={q}"
SIMAP_TYPES = {"tender": "Ausschreibung", "award": "Zuschlag", "direct_award": "Freihändiger Zuschlag",
               "advance_notice": "Vorankündigung", "request_for_information": "Marktabklärung",
               "abandonment": "Abbruch"}


def fetch_simap(topic, source):
    # simap verlangt ein Session-Cookie, deshalb eigener Opener mit Cookie-Speicher
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
    opener.open(urllib.request.Request("https://www.simap.ch/de", headers=headers), timeout=TIMEOUT).read()

    def pick(v):
        return (v or {}).get("de") or (v or {}).get("fr") or (v or {}).get("en") or (v or {}).get("it") or "" if isinstance(v, dict) else (v or "")

    items, seen = [], set()
    for q in source.get("queries", ["Helm"]):
        raw = opener.open(urllib.request.Request(SIMAP_API.format(q=urllib.parse.quote(q)), headers=headers), timeout=TIMEOUT).read()
        for p in json.loads(raw).get("projects", []):
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            lots = " · ".join(pick(l.get("lotTitle")) for l in p.get("lots") or [] if pick(l.get("lotTitle")))
            kind = SIMAP_TYPES.get(p.get("pubType"), p.get("pubType") or "")
            it = make_item(topic, source["name"], pick(p.get("title")),
                           f"https://www.simap.ch/de/project-detail/{p['id']}",
                           to_iso(p.get("publicationDate")), " · ".join(x for x in (kind, lots) if x),
                           publisher="simap.ch",
                           extra={"buyer": pick(p.get("procOfficeName")) or None, "country": "CHE", "domain": "simap.ch"})
            if it:
                items.append(it)
    return items


# ---------------------------------------------------------------- APIs

def fetch_ted(topic, source, match=None):
    since = (dt.date.today() - dt.timedelta(days=MAX_AGE_DAYS["ausschreibungen"])).strftime("%Y%m%d")
    cpv = " ".join(TED_CPV)
    queries = [
        f"(classification-cpv IN ({cpv}) OR FT ~ ({' OR '.join(TED_FULLTEXT)})) AND publication-date >= {since} SORT BY publication-date DESC",
        f"classification-cpv IN ({cpv}) AND publication-date >= {since} SORT BY publication-date DESC",
    ]
    fields = ["publication-number", "notice-title", "buyer-name", "buyer-country",
              "publication-date", "deadline-receipt-tender-date-lot", "notice-type", "classification-cpv"]
    last_err = None
    for qi, q in enumerate(queries):
        try:
            data = http_post_json(source["url"], {
                "query": q, "fields": fields, "limit": 250, "page": 1,
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

    # Weitere Seiten nachladen (max. 1000 Treffer)
    notices = data.get("notices", [])
    for page in range(2, 5):
        if len(notices) >= (data.get("totalNoticeCount") or 0):
            break
        more = http_post_json(source["url"], {
            "query": queries[qi], "fields": fields, "limit": 250, "page": page,
            "scope": "ALL", "paginationMode": "PAGE_NUMBER",
        }).get("notices", [])
        if not more:
            break
        notices += more
    data["notices"] = notices

    items = []
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice title=TED::Abfrage {qi + 1}, {len(data.get('notices', []))} Treffer, gesamt {data.get('totalNoticeCount')}")
    for n in data.get("notices", []):
        pub_no = n.get("publication-number")
        if not pub_no:
            continue
        title = pick_lang(n.get("notice-title"))
        cpvs = n.get("classification-cpv") or []
        cpvs = [str(c) for c in (cpvs if isinstance(cpvs, list) else [cpvs])]
        # Volltext-Treffer nur behalten, wenn CPV passt oder der Titel einen Suchbegriff enthält
        if not any(c.startswith(TED_CPV_PREFIXES) for c in cpvs) and not (match and match(title)):
            continue
        deadline = to_iso(pick_lang(n.get("deadline-receipt-tender-date-lot")))
        country = pick_lang(n.get("buyer-country"))
        buyer = pick_lang(n.get("buyer-name"))
        it = make_item(
            topic, source["name"], title or f"TED {pub_no}",
            f"https://ted.europa.eu/de/notice/-/detail/{pub_no}",
            to_iso(pick_lang(n.get("publication-date"))),
            " · ".join(x for x in (buyer, country) if x),
            publisher="TED",
            extra={"deadline": deadline, "buyer": buyer or None, "country": country or None, "domain": "ted.europa.eu"},
        )
        if it:
            items.append(it)
    return items


def fetch_find_a_tender(topic, source, match):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    url = source["url"] + "?" + urllib.parse.urlencode({"updatedFrom": since, "stages": "tender", "limit": 100})
    data = json.loads(http_get(url, headers={"Accept": "application/json"}))
    prefixes = TED_CPV_PREFIXES
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
            extra={"deadline": to_iso((tender.get("tenderPeriod") or {}).get("endDate")), "country": "GBR",
                   "domain": "find-tender.service.gov.uk"},
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
                jobs.append({"topic": tid, "name": s["name"], "kind": "scrape", "url": s["url"], "filter": s.get("filter"), "source": s})
            # Zusätzlicher Google-News-Feed (z. B. für jeden Wettbewerber)
            if s.get("gnews_query") and stype != "gnews":
                jobs.append({"topic": tid, "name": f"{s['name']} (Google News)", "kind": "gnews",
                             "url": gnews_url(template, s["gnews_query"]), "filter": None})
    return jobs


def run_job(job, matchers, previous_links):
    match = matchers.get(job["topic"]) if job.get("filter") == "keywords" else None
    status = {"name": job["name"], "topic": job["topic"], "kind": job["kind"], "url": job["url"], "ok": False, "count": 0}
    try:
        if job["kind"] == "api":
            if "ted.europa.eu" in job["url"]:
                items = fetch_ted(job["topic"], job["source"], matchers.get(job["topic"]))
            elif "find-tender" in job["url"]:
                items = fetch_find_a_tender(job["topic"], job["source"], matchers.get(job["topic"]))
            elif "simap.ch" in job["url"]:
                items = fetch_simap(job["topic"], job["source"])
            elif "oeffentlichevergabe" in job["url"]:
                items = fetch_oev(job["topic"], job["source"], 3 if previous_links else 21)
            else:
                raise ValueError("API nicht unterstützt")
        elif job["kind"] == "scrape" and job["source"].get("link_pattern"):
            items = scrape_page(job, previous_links)
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
        require = require_matcher(topic.get("require_keywords"), topic.get("exclude_keywords"))
        require_also = require_matcher(topic.get("require_also_keywords"))
        seen_links, seen_titles, items = set(), set(), []
        for it in sorted((i for i in all_items if i["topic"] == tid),
                         key=lambda i: i["published"] or "", reverse=True):
            text = f"{it['title']} {it['summary']}"
            if (require and not require(text)) or (require_also and not require_also(text)):
                continue
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


def load_previous():
    """Vorherige news.json von der Live-Seite: Meldungen bleiben erhalten, auch wenn eine Quelle sie nicht mehr liefert."""
    url = os.environ.get("PREVIOUS_URL")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not url and "/" in repo:
        owner, name = repo.split("/", 1)
        url = f"https://{owner.lower()}.github.io/{name}/data/news.json"
    if not url:
        return []
    try:
        data = json.loads(http_get(f"{url}?t={int(time.time())}"))
        return [i for t in data.get("topics", []) for i in t.get("items", [])]
    except Exception as e:  # noqa: BLE001
        print(f"Vorherige Daten nicht geladen: {e}", file=sys.stderr)
        return []


def main():
    cfg = json.loads(SOURCES.read_text(encoding="utf-8"))
    topic_ids = {t["id"] for t in cfg["topics"]}
    previous = [i for i in load_previous() if i.get("topic") in topic_ids]
    previous_links = {i["link"]: i for i in previous}
    matchers = {t["id"]: keyword_matcher(t.get("keywords")) for t in cfg["topics"]}
    jobs = build_jobs(cfg)
    # Google News drosselt bei vielen parallelen Abrufen (HTTP 503), deshalb nacheinander mit Pause
    gnews_jobs = [j for j in jobs if j["kind"] == "gnews"]
    other_jobs = [j for j in jobs if j["kind"] != "gnews"]

    def run_gnews_serial():
        out = []
        for j in gnews_jobs:
            res = run_job(j, matchers, previous_links)
            if not res[0]["ok"] and "503" in res[0].get("error", ""):
                time.sleep(20)
                res = run_job(j, matchers, previous_links)
            out.append(res)
            time.sleep(2)
        return out

    with ThreadPoolExecutor(max_workers=12) as pool:
        gnews_future = pool.submit(run_gnews_serial)
        results = list(pool.map(lambda j: run_job(j, matchers, previous_links), other_jobs))
        results += gnews_future.result()
    statuses = [r[0] for r in results]
    items = [i for r in results for i in r[1]]
    # Neue Meldungen zuerst, damit sie beim Entdoppeln Vorrang vor alten Ständen haben
    items += previous
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
    if os.environ.get("GITHUB_ACTIONS"):
        # Ergebnis als Anmerkung am Workflow-Lauf, damit es ohne Log-Download sichtbar ist
        counts = ", ".join(f"{t['short']}: {len(t['items'])}" for t in out["topics"])
        print(f"::notice title=Quellen::{ok}/{len(statuses)} Quellen ok. Meldungen: {counts}")
        failed = [f"{s['name']}: {s.get('error')}" for s in statuses if not s["ok"]]
        if failed:
            print("::warning title=Nicht erreichbar::" + "%0A".join(failed))
    # Nur scheitern, wenn gar nichts geklappt hat (z. B. kein Netz)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
