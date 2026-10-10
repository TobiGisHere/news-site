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

import ai_rate

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources.json"
OUT = ROOT / "site" / "data" / "news.json"

USER_AGENT = "Mozilla/5.0 (compatible; MeineNewsSite/1.0; +https://github.com)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TIMEOUT = 25
MAX_ITEMS_PER_TOPIC = 80
MAX_AGE_DAYS = {"ausschreibungen": 365, "zuschlaege": 730, "konkurrenz": 120, "schuberth": 180,
                "branche": 30, "technologie": 365, "normen": 730}
DEFAULT_MAX_AGE_DAYS = 14

# Nur Schutzkopfbedeckungen/Helme (18444…), keine Westen oder Schutzkleidung.
TED_CPV = ["18444000", "18444100", "18444110"]
TED_CPV_PREFIXES = ("18444",)
TED_FULLTEXT = ['"ballistic helmet"', '"ballistischer Schutzhelm"', "Schutzhelm", "Gefechtshelm"]


# ---------------------------------------------------------------- HTTP

def http_get(url, data=None, headers=None, retries=1):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # Kurzzeitige Überlast (zu viele Anfragen, Wartung): einmal nach kurzer Pause wiederholen
        if retries and e.code in (429, 502, 503, 504):
            time.sleep(10)
            return http_get(url, data, headers, retries - 1)
        raise


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


# ---------------------------------------------------------------- Weitere nationale Portale

# Grobe Vorauswahl für Portale ohne Suchfunktion: Helm- und Ballistikbegriffe in vielen Sprachen.
# Die eigentliche Auswahl passiert danach über require_keywords des Themas.
TENDER_PREFILTER = re.compile(
    r"helm|hełm|kask|casque|casco|kiiver|ķiver|šalm|kypär|hjelm|hjälm|шолом|přilb|prilb|kacig|čelad|sisak|"
    r"ballist|balist|kuloodporn|pare-balle|antibala|bullet|body armo|kogelwerend|kuulikindel", re.I)


def _days_ago(n):
    return (dt.date.today() - dt.timedelta(days=n))


def fetch_ezamowienia(topic, source):
    """Polen: nationale Bekanntmachungen (BZP) unterhalb der EU-Schwelle, Suche im Auftragsgegenstand."""
    items, seen = [], set()
    since = _days_ago(source.get("days", 180)).isoformat()
    for q in source.get("queries", ["balistyczn"]):
        url = ("https://ezamowienia.gov.pl/mo-board/api/v1/Board/Search?NoticeType=ContractNotice"
               f"&OrderObject={urllib.parse.quote(q)}&PublicationDateFrom={since}"
               "&SortingColumnName=PublicationDate&SortingDirection=DESC&PageNumber=1&PageSize=100")
        for n in json.loads(http_get(url)):
            key = n.get("objectId") or n.get("noticeNumber")
            if not key or key in seen:
                continue
            seen.add(key)
            it = make_item(topic, source["name"], n.get("orderObject"),
                           f"https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/{n['objectId']}",
                           to_iso(n.get("publicationDate")), " · ".join(x for x in (n.get("cpvCode"), n.get("bzpNumber")) if x),
                           publisher="e-Zamówienia (Polen)",
                           extra={"buyer": n.get("organizationName"), "country": "POL", "domain": "ezamowienia.gov.pl",
                                  "deadline": to_iso(n.get("submittingOffersDate"))})
            if it:
                items.append(it)
    return items


def fetch_boamp(topic, source):
    """Frankreich: BOAMP über die offene Opendatasoft-Schnittstelle."""
    items, seen = [], set()
    since = _days_ago(source.get("days", 180)).isoformat()
    for q in source.get("queries", ["casque"]):
        where = f'search(objet,"{q}") AND dateparution>=date\'{since}\''
        url = ("https://boamp-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/boamp/records?"
               + urllib.parse.urlencode({"where": where, "order_by": "dateparution desc", "limit": 100}))
        for r in json.loads(http_get(url)).get("results", []):
            if r.get("idweb") in seen:
                continue
            seen.add(r.get("idweb"))
            it = make_item(topic, source["name"], r.get("objet"), r.get("url_avis"), to_iso(r.get("dateparution")),
                           " · ".join(x for x in (r.get("nature_libelle"), r.get("type_marche") if isinstance(r.get("type_marche"), str) else None) if x),
                           publisher="BOAMP (Frankreich)",
                           extra={"buyer": r.get("nomacheteur"), "country": "FRA", "domain": "boamp.fr",
                                  "deadline": to_iso(r.get("datelimitereponse"))})
            if it:
                items.append(it)
    return items


def fetch_contracts_finder(topic, source):
    """Großbritannien: Contracts Finder (auch Aufträge unterhalb der Schwelle), Stichwortsuche per POST."""
    items, seen = [], set()
    since = _days_ago(source.get("days", 180)).isoformat() + "T00:00:00"
    for q in source.get("queries", ["helmet"]):
        body = json.dumps({"searchCriteria": {"keyword": q, "publishedFrom": since, "types": ["Contract"]}, "size": 100}).encode()
        raw = http_get("https://www.contractsfinder.service.gov.uk/api/rest/2/search_notices/json", data=body,
                       headers={"Content-Type": "application/json", "Accept": "application/json"})
        for n in json.loads(raw).get("noticeList", []):
            n = n.get("item") or {}
            if n.get("id") in seen:
                continue
            seen.add(n.get("id"))
            it = make_item(topic, source["name"], html.unescape(n.get("title") or ""),
                           f"https://www.contractsfinder.service.gov.uk/Notice/{n.get('id')}",
                           to_iso(n.get("publishedDate")), n.get("description") or "",
                           publisher="Contracts Finder (UK)",
                           extra={"buyer": n.get("organisationName"), "country": "GBR", "domain": "contractsfinder.service.gov.uk",
                                  "deadline": to_iso(n.get("deadlineDate"))})
            if it:
                items.append(it)
    return items


def fetch_nspa(topic, source):
    """NATO NSPA: stündlich aktualisierte XML-Liste aller Geschäftsmöglichkeiten."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(http_get(source["url"]))
    items = []
    for el in root.iter():
        if not el.tag.endswith("Item"):
            continue
        f = {c.tag: (c.text or "").strip() for c in el}
        title = f.get("Title") or f.get("ProductName") or f.get("ProductNameEN")
        if not title or not TENDER_PREFILTER.search(title):
            continue
        kind = {"FBOItem": "Vorankündigung", "RFPItem": "Ausschreibung", "NOIItem": "Absichtserklärung"}.get(el.tag, el.tag)
        it = make_item(topic, source["name"], title, f.get("DetailsPage"), to_iso(f.get("PublicationDate")),
                       " · ".join(x for x in (kind, f.get("CollectiveNumber") or f.get("OpportunityId")) if x),
                       publisher="NSPA (NATO)",
                       extra={"buyer": "NATO Support and Procurement Agency", "domain": "nspa.nato.int",
                              "deadline": to_iso(f.get("RFPClosingDate") or f.get("RFPTentativeDate"))})
        if it:
            items.append(it)
    return items


def fetch_canadabuys(topic, source):
    """Kanada: offene Bundesausschreibungen als CSV (keine Suche, daher lokale Vorauswahl)."""
    import csv
    raw = http_get(source["url"], headers={"User-Agent": BROWSER_UA, "Accept": "text/csv,*/*"}).decode("utf-8-sig", "replace")
    items = []
    for r in csv.DictReader(io.StringIO(raw)):
        title = r.get("title-titre-eng") or r.get("title-titre-fra") or ""
        desc = r.get("gsinDescription-nibsDescription-eng") or ""
        if not TENDER_PREFILTER.search(f"{title} {desc}"):
            continue
        it = make_item(topic, source["name"], title, r.get("noticeURL-URLavis-eng") or r.get("noticeURL-URLavis-fra"),
                       to_iso(r.get("publicationDate-datePublication")), desc, publisher="CanadaBuys (Kanada)",
                       extra={"buyer": r.get("contractingEntityName-nomEntitContractante-eng"), "country": "CAN",
                              "domain": "canadabuys.canada.ca", "deadline": to_iso(r.get("tenderClosingDate-appelOffresDateCloture"))})
        if it:
            items.append(it)
    return items


def fetch_latvia(topic, source, days):
    """Lettland: tägliche JSON-Dateien des Beschaffungsamts IUB (keine Suche, daher lokale Vorauswahl)."""
    items = []
    for n in range(days):
        d = _days_ago(n)
        url = f"https://open.iub.gov.lv/data/notice/{d:%Y}/{d:%m}/{d:%d-%m-%Y}.json"
        try:
            notices = json.loads(http_get(url))
        except urllib.error.HTTPError as e:
            if e.code == 404:  # Wochenende/Feiertag oder noch nicht erzeugt
                continue
            raise
        except ValueError:  # Tagesdatei fehlt, der Server liefert dann eine HTML-Seite
            continue
        for x in notices if isinstance(notices, list) else []:
            title = x.get("name") or ""
            if not TENDER_PREFILTER.search(title):
                continue
            tp = x.get("tenderingProcess") or {}
            link = tp.get("documentsURL") or (x.get("organizationData") or {}).get("websiteURIClient") or f"https://open.iub.gov.lv/#{x.get('identifier')}"
            it = make_item(topic, source["name"], title, link, d.isoformat() + "T00:00:00+00:00",
                           " · ".join(v for v in (x.get("noticeType"), x.get("cpvType")) if v), publisher="IUB (Lettland)",
                           extra={"buyer": (x.get("organizationData") or {}).get("name"), "country": "LVA", "domain": "iub.gov.lv"})
            if it:
                items.append(it)
    return items



# Spanien: ATOM-Syndikation der Plattform (je Datei rund 70-500 Einträge, "next" führt zu älteren Dateien)
ES_NS = {"a": "http://www.w3.org/2005/Atom",
         "cbc": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
         "cac": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
         "pe": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2"}
ES_MAX_FILES = 14


def fetch_spain(topic, source, hours):
    """Spanien (Plataforma de Contratación): alle Änderungen der letzten Stunden, lokal vorgefiltert."""
    from xml.etree import ElementTree as ET
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    url, items = source["url"], []
    for _ in range(ES_MAX_FILES):
        root = ET.fromstring(http_get(url))
        oldest = None
        for e in root.findall("a:entry", ES_NS):
            updated = to_iso(e.findtext("a:updated", "", ES_NS))
            oldest = min(oldest or updated, updated) if updated else oldest
            title = e.findtext("a:title", "", ES_NS)
            cpvs = [c.text or "" for c in e.iter(f"{{{ES_NS['cbc']}}}ItemClassificationCode")]
            if not (any(c.startswith(TED_CPV_PREFIXES) for c in cpvs) or TENDER_PREFILTER.search(title)):
                continue
            state = e.findtext(".//pe:ContractFolderStatusCode", "", ES_NS)
            buyer = e.findtext(".//cac:Party/cac:PartyName/cbc:Name", "", ES_NS)  # erste Partei = Auftraggeber
            deadline = e.findtext(".//cac:TenderSubmissionDeadlinePeriod/cbc:EndDate", "", ES_NS)
            winners = list(dict.fromkeys(w.text.strip() for w in e.findall(".//cac:WinningParty/cac:PartyName/cbc:Name", ES_NS) if w.text))
            link_el = e.find("a:link", ES_NS)
            extra = {"buyer": buyer or None, "country": "ESP", "domain": "contrataciondelestado.es",
                     "deadline": to_iso(deadline) if state in ("PUB", "PRE") else None}
            item_topic = topic
            if state == "ANUL":
                extra.update(status="abgebrochen")
            elif state in ("ADJ", "RES") and winners:
                item_topic = "zuschlaege"
                extra.update(winners=winners[:12])
            it = make_item(item_topic, source["name"], title, link_el.get("href") if link_el is not None else "",
                           updated, e.findtext("a:summary", "", ES_NS), publisher="Plataforma de Contratación", extra=extra)
            if it:
                items.append(it)
        nxt = next((l.get("href") for l in root.findall("a:link", ES_NS) if l.get("rel") == "next"), None)
        if not nxt or (oldest and oldest < cutoff.isoformat()):
            break
        url = nxt
    return items


PROZORRO_SEARCH = "https://prozorro.gov.ua/api/search/tenders"
PROZORRO_CLOSED = {"cancelled": "abgebrochen", "unsuccessful": "abgebrochen"}


def fetch_prozorro(topic, source):
    """Ukraine: Suche der Prozorro-Website (offen, ohne Schlüssel), mehrere Seiten je Suchbegriff."""
    items, seen = [], set()
    oldest = (dt.date.today() - dt.timedelta(days=MAX_AGE_DAYS["ausschreibungen"])).isoformat()
    for q in source.get("queries", ["шолом кулезахисний"]):
        for page in range(1, 4):
            data = http_post_json(PROZORRO_SEARCH, {"text": q, "page": page})
            batch = data.get("data") or []
            for t in batch:
                tid = t.get("tenderID") or ""
                m = re.match(r"UA-(\d{4}-\d{2}-\d{2})-", tid)
                if not m or tid in seen or m.group(1) < oldest:
                    continue
                seen.add(tid)
                pe = t.get("procuringEntity") or {}
                value = t.get("value") or {}
                extra = {"buyer": pe.get("name") or None, "country": "UKR", "domain": "prozorro.gov.ua",
                         "deadline": to_iso((t.get("tenderPeriod") or {}).get("endDate"))
                         if t.get("status") in ("active.enquiries", "active.tendering") else None}
                if value.get("amount"):
                    extra.update(estimate=round(value["amount"]), currency=value.get("currency"))
                if t.get("status") in PROZORRO_CLOSED:
                    extra.update(status=PROZORRO_CLOSED[t["status"]])
                it = make_item(topic, source["name"], t.get("title") or tid, f"https://prozorro.gov.ua/tender/{tid}",
                               m.group(1) + "T00:00:00+00:00", (pe.get("address") or {}).get("locality") or "",
                               publisher="Prozorro (Ukraine)", extra=extra)
                if it:
                    items.append(it)
            if len(batch) < (data.get("per_page") or 20):
                break
    return items


SAM_SEARCH = "https://api.sam.gov/opportunities/v2/search"


def fetch_sam(topic, source):
    """USA: SAM.gov Opportunities (kostenloser Schlüssel in SAM_API_KEY)."""
    key = os.environ["SAM_API_KEY"]
    today = dt.date.today()
    items, seen = [], set()
    for q in source.get("queries", ["helmet"]):
        params = {"api_key": key, "postedFrom": f"{today - dt.timedelta(days=180):%m/%d/%Y}",
                  "postedTo": f"{today:%m/%d/%Y}", "title": q, "limit": 1000}
        data = json.loads(http_get(f"{SAM_SEARCH}?{urllib.parse.urlencode(params)}", headers={"Accept": "application/json"}))
        for o in data.get("opportunitiesData") or []:
            if o.get("noticeId") in seen:
                continue
            seen.add(o.get("noticeId"))
            awarded = (o.get("award") or {}).get("awardee") or {}
            extra = {"buyer": (o.get("fullParentPathName") or "").split(".")[-1] or None, "country": "USA", "domain": "sam.gov",
                     "deadline": to_iso(o.get("responseDeadLine"))}
            item_topic = topic
            if awarded.get("name"):
                item_topic = "zuschlaege"
                extra.update(winners=[awarded["name"]], deadline=None)
            it = make_item(item_topic, source["name"], o.get("title") or o.get("solicitationNumber") or "",
                           o.get("uiLink") or f"https://sam.gov/opp/{o.get('noticeId')}/view",
                           to_iso(o.get("postedDate")), " · ".join(x for x in (o.get("type"), o.get("solicitationNumber")) if x),
                           publisher="SAM.gov", extra=extra)
            if it:
                items.append(it)
    return items

# ---------------------------------------------------------------- Fachliteratur (OpenAlex)

OPENALEX = "https://api.openalex.org/works?search={q}&filter=from_publication_date:{since}&sort=publication_date:desc&per-page=40&mailto=news-site@example.com"


def fetch_openalex(topic, source):
    since = (dt.date.today() - dt.timedelta(days=MAX_AGE_DAYS.get(topic, 365))).isoformat()
    items, seen = [], set()
    for q in source.get("queries", []):
        data = json.loads(http_get(OPENALEX.format(q=urllib.parse.quote(q), since=since)))
        for w in data.get("results", []):
            if w["id"] in seen or not w.get("title"):
                continue
            seen.add(w["id"])
            # Abstract liegt als invertierter Index vor
            inv = w.get("abstract_inverted_index") or {}
            words = sorted(((pos, word) for word, poss in inv.items() for pos in poss))
            abstract = " ".join(word for _, word in words)
            venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
            authors = [a["author"]["display_name"] for a in (w.get("authorships") or [])[:3] if a.get("author")]
            meta = " · ".join(x for x in (venue, ", ".join(authors) + (" u. a." if len(w.get("authorships") or []) > 3 else "")) if x)
            link = w.get("doi") or (w.get("primary_location") or {}).get("landing_page_url") or w["id"]
            it = make_item(topic, source["name"], w["title"], link, to_iso(w.get("publication_date")),
                           f"{meta}. {abstract}" if abstract else meta, publisher=venue or "Fachartikel",
                           extra={"domain": domain_of(link), "kind": "paper"})
            if it:
                items.append(it)
    return items


# ---------------------------------------------------------------- APIs

# TED-Volltextsuche: Die API akzeptiert nur einfache "A AND B"-Ausdrücke mit Platzhaltern,
# verschachtelte ODER-Gruppen führen zu HTTP 400. Deshalb je Begriffspaar eine Abfrage.
TED_BALLISTIC_FT = (
    [f"ballist* AND {h}" for h in ("helm*", "helmet*", "casque*", "casco*", "kask*")]
    + [f"balist* AND {h}" for h in ("casco*", "kask*", "kacig*", "prilb*", "capacete*", "casca")]
    + ["VPAM AND helm*", "VPAM AND helmet*", "kuloodporn* AND kask*", "kogelwerend* AND helm*"]
)


TED_PROCEDURE_TYPES = {"open": "Offenes Verfahren", "restricted": "Nicht offenes Verfahren",
                       "neg-w-call": "Verhandlungsverfahren", "neg-wo-call": "Verhandlungsverfahren ohne Bekanntmachung",
                       "comp-dial": "Wettbewerblicher Dialog", "innovation": "Innovationspartnerschaft",
                       "oth-single": "Sonstiges einstufiges Verfahren", "oth-mult": "Sonstiges mehrstufiges Verfahren"}
TED_NON_AWARD = {"no-rece": "keine Angebote eingegangen", "all-rej": "alle Angebote abgelehnt",
                 "ins-fund": "keine Mittel", "chan-need": "Bedarf geändert", "other": None}
# Abbruch im Titel (z. B. Litauen "NUTRAUKTAS"), falls das Statusfeld fehlt
TED_CANCEL_RE = re.compile(r"\b(nutrauktas|nutraukta|unieważni\w*|zrušen[oáé]|annul[ée]e?|cancel+ed|aufgehoben|keskeytysilmoitus)\b", re.I)
TED_WINDOW_DAYS = 120
TED_MAX_PAGES = 4


def fetch_ted(topic, source, match=None):
    # Zuschläge reichen weiter zurück als offene Ausschreibungen
    since = (dt.date.today() - dt.timedelta(days=MAX_AGE_DAYS["zuschlaege"])).strftime("%Y%m%d")
    cpv = " ".join(TED_CPV)
    base_fields = ["publication-number", "notice-title", "buyer-name", "buyer-country",
                   "publication-date", "deadline-receipt-tender-date-lot", "notice-type", "classification-cpv"]
    # Los-Titel und -Beschreibung, damit der Ballistik-Filter auch den Text sieht (falls die API die Felder kennt)
    # dazu die Zuschlagsfelder (Gewinner, Bieter, Wert), die nur Zuschlagsbekanntmachungen tragen
    extra_fields = ["title-lot", "description-lot", "winner-name", "winner-country", "organisation-name-tenderer",
                    "total-value", "total-value-cur", "contract-conclusion-date",
                    # Verfahrensdetails (eForms): Teilnahmefrist, Schätzwert, Verfahrensart, Laufzeit, Unterlagen,
                    # Verfahrens-ID (verbindet Ausschreibung, Änderungen und Zuschlag) und Zuschlagsstatus je Los
                    "deadline-receipt-request-date-lot", "estimated-value-proc", "estimated-value-cur-proc",
                    "estimated-value-lot", "estimated-value-cur-lot", "procedure-type", "contract-duration-period-lot",
                    "document-url-lot", "buyer-email", "procedure-identifier", "change-notice-version-identifier",
                    "winner-selection-status", "non-award-justification"]

    def run_query(q):
        fields = base_fields + extra_fields
        notices, page, total = [], 1, None
        while page <= TED_MAX_PAGES:
            try:
                data = http_post_json(source["url"], {"query": q, "fields": fields, "limit": 250, "page": page,
                                                      "scope": "ALL", "paginationMode": "PAGE_NUMBER"})
            except urllib.error.HTTPError as e:
                if e.code == 400 and fields != base_fields:
                    fields = base_fields
                    continue
                raise
            batch = data.get("notices", [])
            notices += batch
            total = data.get("totalNoticeCount") or 0
            if not batch or len(notices) >= total:
                break
            page += 1
        return notices, total, fields != base_fields

    def pick_lang(v):
        if isinstance(v, dict):
            for lang in ("deu", "DEU", "eng", "ENG"):
                if v.get(lang):
                    return pick_lang(v[lang])
            return pick_lang(next(iter(v.values()), ""))
        if isinstance(v, list):
            return pick_lang(v[0]) if v else ""
        return str(v or "")

    def all_values(v):
        """Alle Einträge eines (mehrsprachigen) Feldes, ohne Doppelte, Reihenfolge bleibt."""
        if isinstance(v, dict):
            v = next((v[k] for k in ("deu", "eng") if v.get(k)), next(iter(v.values()), []))
        vals = v if isinstance(v, list) else [v] if v else []
        out = []
        for x in vals:
            x = clean_text(str(x), 120)
            if x and x.lower() not in {o.lower() for o in out}:
                out.append(x)
        return out

    def all_text(v):
        if isinstance(v, dict):
            return " ".join(all_text(x) for x in v.values())
        if isinstance(v, list):
            return " ".join(all_text(x) for x in v)
        return str(v or "")

    # 1) Volltextsuche nach ballistischen Helmen in allen EU-Sprachen, egal welcher CPV-Code
    ballistic, b_counts, b_extra = [], [], False
    for ft in TED_BALLISTIC_FT:
        try:
            found, total, b_extra = run_query(f"FT ~ ({ft}) AND publication-date >= {since} SORT BY publication-date DESC")
            ballistic += found
            b_counts.append(str(total))
        except urllib.error.HTTPError as e:
            b_counts.append(f"Fehler {e.code}")
    b_total = "/".join(b_counts)
    # 2) Alle Bekanntmachungen mit Helm-CPV-Codes, in Zeitscheiben, damit keine Abfrage an die 1.000er-Grenze stößt
    by_cpv, c_counts, capped = [], [], []
    start = dt.date.today() - dt.timedelta(days=MAX_AGE_DAYS["zuschlaege"])
    while start <= dt.date.today():
        end = start + dt.timedelta(days=TED_WINDOW_DAYS)
        found, total, _ = run_query(f"classification-cpv IN ({cpv}) AND publication-date >= {start:%Y%m%d} "
                                    f"AND publication-date < {end:%Y%m%d} SORT BY publication-date DESC")
        by_cpv += found
        c_counts.append(str(total))
        if total > len(found):
            capped.append(f"{start:%d.%m.%Y}")
        start = end
    c_total = "/".join(c_counts)
    if capped and os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title=TED::Zeitscheibe ab {', '.join(capped)} hat mehr als {TED_MAX_PAGES * 250} Treffer, bitte TED_WINDOW_DAYS verkleinern")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice title=TED::Ballistik-Volltext: {len(ballistic)} von {b_total}, Helm-CPV: {len(by_cpv)} von {c_total}, Losfelder: {b_extra}")

    items, seen = [], set()
    for is_ballistic, notices in ((True, ballistic), (False, by_cpv)):
        for n in notices:
            pub_no = n.get("publication-number")
            if not pub_no or pub_no in seen:
                continue
            seen.add(pub_no)
            title = pick_lang(n.get("notice-title"))
            cpvs = n.get("classification-cpv") or []
            cpvs = [str(c) for c in (cpvs if isinstance(cpvs, list) else [cpvs])]
            lot_text = clean_text(f"{all_text(n.get('title-lot'))} {all_text(n.get('description-lot'))}", 600)
            deadline = to_iso(pick_lang(n.get("deadline-receipt-tender-date-lot")))
            # Zweistufige Verfahren nennen statt der Angebotsfrist nur die Frist für Teilnahmeanträge
            request_deadline = to_iso(pick_lang(n.get("deadline-receipt-request-date-lot")))
            country = pick_lang(n.get("buyer-country"))
            buyer = pick_lang(n.get("buyer-name"))
            notice_type = str(n.get("notice-type") or "")
            extra = {"deadline": deadline or request_deadline, "deadline_kind": "request" if request_deadline and not deadline else None,
                     "buyer": buyer or None, "country": country or None,
                     "domain": "ted.europa.eu", "ballistic": is_ballistic or None, "notice_type": notice_type or None,
                     "procedure": pick_lang(n.get("procedure-identifier")) or None,
                     "procedure_type": TED_PROCEDURE_TYPES.get(pick_lang(n.get("procedure-type"))) or None,
                     "duration": ted_duration(n.get("contract-duration-period-lot")),
                     "docs": next((u for u in all_values(n.get("document-url-lot")) if u.startswith("http")), None),
                     "contact": pick_lang(n.get("buyer-email")) or None,
                     "replaces": pick_lang(n.get("change-notice-version-identifier")) or None}
            est, est_cur = ted_amount(n.get("estimated-value-proc")), pick_lang(n.get("estimated-value-cur-proc"))
            if est is None:
                lots = [ted_amount(v) for v in (n.get("estimated-value-lot") or [])]
                est = round(sum(v for v in lots if v)) if any(lots) else None
                est_cur = pick_lang(n.get("estimated-value-cur-lot"))
            if est:
                extra.update(estimate=est, currency=est_cur or None)
            # Zuschlagsbekanntmachungen (can-*, veat) gehören in die Zuschlagsauswertung, nicht zu den offenen Verfahren
            is_award = notice_type.startswith("can") or notice_type == "veat"
            status = [str(x) for x in (n.get("winner-selection-status") or [])]
            # Alle Lose ohne Zuschlag geschlossen: Verfahren abgebrochen, kein Zuschlag
            cancelled = bool(is_award and status and all(x.startswith("clos") for x in status)) or bool(TED_CANCEL_RE.search(title))
            if cancelled:
                reasons = [TED_NON_AWARD.get(str(r), "") for r in (n.get("non-award-justification") or [])]
                extra.update(status="abgebrochen", status_reason=next((r for r in reasons if r), None), deadline=None)
            elif is_award:
                value = n.get("total-value")
                value = value[0] if isinstance(value, list) and value else value
                try:
                    value = round(float(value)) if value not in (None, "") else None
                except (TypeError, ValueError):
                    value = None
                winners = all_values(n.get("winner-name"))
                extra.update(deadline=None, winners=winners[:12] or None,
                             winner_countries=all_values(n.get("winner-country"))[:12] or None,
                             bidders=[b for b in all_values(n.get("organisation-name-tenderer")) if b not in winners][:20] or None,
                             value=value, currency=pick_lang(n.get("total-value-cur")) or None,
                             awarded=to_iso(pick_lang(n.get("contract-conclusion-date"))))
            it = make_item(
                "zuschlaege" if is_award and not cancelled else topic, source["name"], title or f"TED {pub_no}",
                f"https://ted.europa.eu/de/notice/-/detail/{pub_no}",
                to_iso(pick_lang(n.get("publication-date"))),
                " · ".join(x for x in (buyer, lot_text) if x),
                publisher="TED",
                extra=extra,
            )
            if it:
                items.append(it)
    return ted_merge_procedures(items)


def ted_amount(v):
    v = v[0] if isinstance(v, list) and v else v
    try:
        return round(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def ted_duration(v):
    """Vertragslaufzeit des ersten Loses, z. B. "48 Monate"."""
    v = v[0] if isinstance(v, list) and v else v
    if not isinstance(v, dict) or not v.get("value"):
        return None
    unit = {"MONTH": "Monate", "YEAR": "Jahre", "DAY": "Tage", "WEEK": "Wochen"}.get(str(v.get("unit")), "")
    return f"{v['value']} {unit}".strip()


def ted_merge_procedures(items):
    """Fasst Bekanntmachungen desselben Verfahrens zusammen.

    - Änderungsbekanntmachungen ersetzen die ältere Fassung (nur die neueste bleibt, mit Hinweis "geändert").
    - Ein Abbruch markiert die Ausschreibung als abgebrochen statt als eigener Eintrag zu erscheinen.
    - Ein Zuschlag wird mit der Ausschreibung verknüpft.
    Verdrängte Einträge bekommen das Thema "verworfen", damit auch ihre alten Stände aus der Vorgänger-Datei
    nicht wieder auftauchen (finalize kennt dieses Thema nicht und lässt sie weg).
    """
    by_proc = {}
    for it in items:
        if it.get("procedure"):
            by_proc.setdefault(it["procedure"], []).append(it)
    for group in by_proc.values():
        tenders = sorted((i for i in group if i["topic"] != "zuschlaege" and i.get("status") != "abgebrochen"),
                         key=lambda i: i["published"] or "", reverse=True)
        cancels = [i for i in group if i.get("status") == "abgebrochen"]
        awards = [i for i in group if i["topic"] == "zuschlaege"]
        if tenders:
            newest = tenders[0]
            if len(tenders) > 1:
                newest["changed"] = len(tenders) - 1
                newest["first_published"] = min(i["published"] or "" for i in tenders) or None
                newest["deadline"] = newest.get("deadline") or next((i["deadline"] for i in tenders if i.get("deadline")), None)
                for old in tenders[1:]:
                    old["topic"] = "verworfen"
            if cancels:
                c = max(cancels, key=lambda i: i["published"] or "")
                newest.update(status="abgebrochen", status_reason=c.get("status_reason"), status_link=c["link"],
                              status_date=c["published"])
                for c in cancels:
                    c["topic"] = "verworfen"
            if awards:
                a = max(awards, key=lambda i: i["published"] or "")
                newest.update(award_link=a["link"], award_date=a["published"], award_winners=a.get("winners"))
                for a in awards:
                    a["tender_link"] = newest["link"]
        elif len(cancels) > 1:
            for c in sorted(cancels, key=lambda i: i["published"] or "", reverse=True)[1:]:
                c["topic"] = "verworfen"
    return items


FAT_MAX_PAGES = 30


def fetch_find_a_tender(topic, source, match, days=30):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    url = source["url"] + "?" + urllib.parse.urlencode({"updatedFrom": since, "stages": "tender", "limit": 100})
    # Die API liefert höchstens 100 Meldungen je Seite; über den "next"-Link weiterblättern
    releases = []
    for _ in range(FAT_MAX_PAGES):
        data = json.loads(http_get(url, headers={"Accept": "application/json"}))
        releases += data.get("releases", [])
        url = (data.get("links") or {}).get("next")
        if not url or not data.get("releases"):
            break
    prefixes = TED_CPV_PREFIXES
    items = []
    for rel in releases:
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
                if s.get("env") and not os.environ.get(s["env"]):
                    continue  # Quelle braucht einen (kostenlosen) Schlüssel, der noch fehlt
                jobs.append({"topic": tid, "name": s["name"], "kind": "api", "url": s["url"], "source": s})
            elif stype == "scrape" and s.get("url"):
                jobs.append({"topic": tid, "name": s["name"], "kind": "scrape", "url": s["url"], "filter": s.get("filter"), "source": s})
            # Zusätzlicher Google-News-Feed (z. B. für jeden Wettbewerber)
            if s.get("gnews_query") and stype != "gnews":
                jobs.append({"topic": tid, "name": f"{s['name']} (Google News)", "kind": "gnews",
                             "url": gnews_url(template, s["gnews_query"]), "filter": None})
    return jobs


TITLE_ONLY = set()  # Themen, deren Stichwortfilter nur auf den Titel schaut (allgemeine Nachrichtenfeeds)


def run_job(job, matchers, previous_links):
    match = matchers.get(job["topic"]) if job.get("filter") == "keywords" else None
    status = {"name": job["name"], "topic": job["topic"], "kind": job["kind"], "url": job["url"], "ok": False, "count": 0}
    try:
        if job["kind"] == "api":
            if "ted.europa.eu" in job["url"]:
                items = fetch_ted(job["topic"], job["source"], matchers.get(job["topic"]))
            elif "find-tender" in job["url"]:
                items = fetch_find_a_tender(job["topic"], job["source"], matchers.get(job["topic"]), 3 if previous_links else 30)
            elif "openalex.org" in job["url"]:
                items = fetch_openalex(job["topic"], job["source"])
            elif "simap.ch" in job["url"]:
                items = fetch_simap(job["topic"], job["source"])
            elif "ezamowienia.gov.pl" in job["url"]:
                items = fetch_ezamowienia(job["topic"], job["source"])
            elif "boamp" in job["url"]:
                items = fetch_boamp(job["topic"], job["source"])
            elif "contractsfinder" in job["url"]:
                items = fetch_contracts_finder(job["topic"], job["source"])
            elif "nspa.nato.int" in job["url"]:
                items = fetch_nspa(job["topic"], job["source"])
            elif "canadabuys" in job["url"]:
                items = fetch_canadabuys(job["topic"], job["source"])
            elif "contrataciondel" in job["url"]:
                items = fetch_spain(job["topic"], job["source"], 26 if previous_links else 48)
            elif "prozorro" in job["url"]:
                items = fetch_prozorro(job["topic"], job["source"])
            elif "sam.gov" in job["url"]:
                items = fetch_sam(job["topic"], job["source"])
            elif "iub.gov.lv" in job["url"]:
                items = fetch_latvia(job["topic"], job["source"], 3 if previous_links else 30)
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
            items = [i for i in items if match(i["title"] if job["topic"] in TITLE_ONLY else f"{i['title']} {i['summary']}")]
        status.update(ok=True, count=len(items), total=total)
        return status, items
    except Exception as e:  # noqa: BLE001 - jede Quelle darf einzeln scheitern
        status["error"] = f"{e.__class__.__name__}: {str(e)[:160]}"
        return status, []


def finalize(cfg, all_items, limit=MAX_ITEMS_PER_TOPIC):
    now = dt.datetime.now(dt.timezone.utc)
    topics_out = []
    for topic in sorted(cfg["topics"], key=lambda t: t.get("priority", 9)):
        tid = topic["id"]
        max_age = dt.timedelta(days=MAX_AGE_DAYS.get(tid, DEFAULT_MAX_AGE_DAYS))
        require = require_matcher(topic.get("require_keywords"), topic.get("exclude_keywords"))
        require_also = require_matcher(topic.get("require_also_keywords"))
        # Themen, deren Quellen alle gefiltert werden: Filter auch auf übernommene alte Meldungen anwenden
        topic_match = keyword_matcher(topic.get("keywords")) if topic.get("filter_all") else None
        reject = require_matcher(topic.get("reject_keywords"))
        # Grenzfälle (Helm ja, "ballistisch" fehlt im Text) prüft die KI, statt sie pauschal auszusortieren
        check_borderline = topic.get("ai_borderline") and ai_rate.available()
        seen_links, seen_titles, items, borderline, dropped = set(), set(), [], [], []
        for it in sorted((i for i in all_items if i["topic"] == tid),
                         key=lambda i: i["published"] or "", reverse=True):
            text = f"{it['title']} {it['summary']}"
            it.pop("borderline", None)
            if require and not require(text):
                continue
            if require_also and not require_also(text) and not it.get("ballistic"):
                dropped.append(it)
                if not check_borderline:
                    continue
                it["borderline"] = True
            if topic_match and not topic_match(it["title"] if topic.get("title_only") else text):
                continue
            if reject and reject(text):
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
            (borderline if it.get("borderline") else items).append(it)
        if dropped and os.environ.get("GITHUB_ACTIONS"):
            recent = [d for d in dropped if (d["published"] or "") >= (now - dt.timedelta(days=180)).isoformat()]
            lines = "%0A".join(f"{(d['published'] or '')[:10]} {d['title'][:110]}".replace("%", "%25") for d in recent[:40])
            print(f"::notice title=Aussortiert {topic.get('short', tid)} ({len(recent)} letzte 180 Tage)::{lines}")
        topics_out.append({"id": tid, "name": topic["name"], "short": topic.get("short", topic["name"]), "priority": topic.get("priority"),
                           "items": items[:limit] + borderline[:limit]})
    return topics_out


def load_previous():
    """Vorherige news.json von der Live-Seite: Meldungen bleiben erhalten, auch wenn eine Quelle sie nicht mehr liefert."""
    url = os.environ.get("PREVIOUS_URL")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not url and "/" in repo:
        owner, name = repo.split("/", 1)
        url = f"https://{owner.lower()}.github.io/{name}/data/news.json"
    if not url:
        return {}
    try:
        return json.loads(http_get(f"{url}?t={int(time.time())}"))
    except Exception as e:  # noqa: BLE001
        print(f"Vorherige Daten nicht geladen: {e}", file=sys.stderr)
        return {}


def main():
    cfg = json.loads(SOURCES.read_text(encoding="utf-8"))
    topic_ids = {t["id"] for t in cfg["topics"]}
    TITLE_ONLY.update(t["id"] for t in cfg["topics"] if t.get("title_only"))
    prev_data = load_previous()
    previous = [i for t in prev_data.get("topics", []) for i in t.get("items", []) if i.get("topic") in topic_ids]
    ai_cache = dict(prev_data.get("ai_cache") or {})
    ai_cache.update({i["link"]: i["ai"] for i in previous if i.get("ai")})
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
                time.sleep(8)
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
    # Neue Meldungen zuerst, damit sie beim Entdoppeln Vorrang vor alten Ständen haben.
    # Alte Stände, die inzwischen in einer anderen Rubrik landen (z. B. Zuschläge), nicht doppelt behalten.
    fresh_topic = {i["link"]: i["topic"] for i in items}
    items += [i for i in previous if fresh_topic.get(i["link"], i["topic"]) == i["topic"]]
    # Mehr Kandidaten behalten, weil die KI-Bewertung noch irrelevante Meldungen aussortiert
    topics = finalize(cfg, items, limit=MAX_ITEMS_PER_TOPIC * 2)
    ai_cache, ai_msg = ai_rate.rate(topics, ai_cache)
    for t in topics:
        t["items"].sort(key=lambda i: i["published"] or "", reverse=True)
        t["items"] = t["items"][:MAX_ITEMS_PER_TOPIC]
    out = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "topics": topics,
        "sources": statuses,
        # Nur ausgeblendete Bewertungen merken; sichtbare Meldungen tragen ihre Bewertung selbst
        "ai_cache": {k: {"score": v["score"], "why": v.get("why")} for k, v in ai_cache.items()
                     if k not in {i["link"] for t in topics for i in t["items"]}},
    }
    print(f"KI-Bewertung: {ai_msg}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(s["ok"] for s in statuses)
    print(f"{ok}/{len(statuses)} Quellen ok, {sum(len(t['items']) for t in out['topics'])} Meldungen -> {OUT}")
    for s in statuses:
        if not s["ok"]:
            print(f"  ✗ {s['name']}: {s.get('error')}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS"):
        # Ergebnis als Anmerkung am Workflow-Lauf, damit es ohne Log-Download sichtbar ist
        print(f"::notice title=KI-Bewertung::{ai_msg}")
        counts = ", ".join(f"{t['short']}: {len(t['items'])}" for t in out["topics"])
        print(f"::notice title=Quellen::{ok}/{len(statuses)} Quellen ok. Meldungen: {counts}")
        for t in out["topics"]:
            sample = "%0A".join(i["title"][:90].replace("%", "%25") for i in t["items"][:10])
            print(f"::notice title=Beispiele {t['short']}::{sample}")
        failed = [f"{s['name']}: {s.get('error')}" for s in statuses if not s["ok"]]
        if failed:
            print("::warning title=Nicht erreichbar::" + "%0A".join(failed))
    # Nur scheitern, wenn gar nichts geklappt hat (z. B. kein Netz)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
