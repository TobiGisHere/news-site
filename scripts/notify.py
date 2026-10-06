"""E-Mails zum Helm-Radar: Sofortalarm bei neuen Helm-Ausschreibungen und Wochen-Digest am Montagmorgen.

Läuft im Workflow direkt nach fetch_news.py. Vergleicht die frisch geschriebene site/data/news.json mit dem
Stand der Live-Seite: Was dort noch nicht stand, ist neu. Versendet wird über Resend (RESEND_API_KEY),
Empfänger stehen im Secret NOTIFY_TO (mehrere durch Komma getrennt), damit keine Adressen im öffentlichen
Repository landen. Fehlt eins von beiden, passiert nichts. Mit --dry-run werden die Mails nur als HTML-Dateien
nach /tmp geschrieben.
"""

import datetime as dt
import html
import json
import os
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_news import OUT, load_previous  # noqa: E402

SITE = "https://tobigishere.github.io/news-site/"
SENDER = os.environ.get("NOTIFY_FROM", "Helm-Radar <onboarding@resend.dev>")
BERLIN = ZoneInfo("Europe/Berlin")
DIGEST_WEEKDAY = 0      # Montag
DIGEST_HOUR = 8         # ab 8 Uhr deutscher Zeit; der erste Lauf danach verschickt den Digest
ALERT_MAX_AGE_DAYS = 21  # ältere Funde (z. B. neue Quelle mit Altbestand) lösen keinen Alarm aus
ACCENT = "#c2410c"

FLAG = {"DEU": "DE", "AUT": "AT", "CHE": "CH", "FRA": "FR", "ITA": "IT", "ESP": "ES", "POL": "PL", "CZE": "CZ",
        "SVK": "SK", "NLD": "NL", "BEL": "BE", "DNK": "DK", "SWE": "SE", "FIN": "FI", "NOR": "NO", "EST": "EE",
        "LVA": "LV", "LTU": "LT", "GBR": "GB", "PRT": "PT", "GRC": "GR", "ROU": "RO", "BGR": "BG", "HUN": "HU",
        "HRV": "HR", "SVN": "SI", "IRL": "IE", "LUX": "LU"}


def flag(code):
    iso2 = FLAG.get(code or "", code if code and len(code) == 2 else "")
    return "".join(chr(0x1F1A5 + ord(c)) for c in iso2.upper()) if iso2 else "🇪🇺"


def esc(s):
    return html.escape(str(s or ""))


def title_of(it):
    t = (it.get("ai") or {}).get("title_de") or it["title"]
    parts = t.split(" – ")
    return " – ".join(parts[2:]) if it.get("domain") == "ted.europa.eu" and len(parts) >= 3 else t


def fmt_day(iso):
    return dt.datetime.fromisoformat(iso).astimezone(BERLIN).strftime("%d.%m.%Y") if iso else ""


def parse(iso):
    return dt.datetime.fromisoformat(iso) if iso else None


def tender_rows(items, now):
    rows = []
    for it in items:
        dl = parse(it.get("deadline"))
        due = ""
        if dl:
            days = (dl - now).days
            due = f"Frist {fmt_day(it['deadline'])}" + (f" (noch {days} Tage)" if days >= 0 else " (abgelaufen)")
        facts = (it.get("ai") or {}).get("facts")
        rows.append(f"""<tr><td style="padding:10px 0;border-top:1px solid #e6e3dc;vertical-align:top;font-size:20px;width:34px">{flag(it.get('country'))}</td>
<td style="padding:10px 0;border-top:1px solid #e6e3dc">
<a href="{esc(it['link'])}" style="color:#1d1d1b;font-weight:600;text-decoration:none">{esc(title_of(it))}</a><br>
<span style="color:#6b6a66;font-size:13px">{esc(it.get('buyer') or it.get('source'))}{' · ' + esc(due) if due else ''}</span>
{f'<br><span style="color:{ACCENT};font-size:13px;font-weight:600">{esc(facts)}</span>' if facts else ''}</td></tr>""")
    return f'<table style="width:100%;border-collapse:collapse">{"".join(rows)}</table>'


def news_rows(items):
    rows = []
    for it in items:
        summary = (it.get("ai") or {}).get("summary") or it.get("summary") or ""
        rows.append(f"""<li style="margin:0 0 12px">
<a href="{esc(it['link'])}" style="color:#1d1d1b;font-weight:600;text-decoration:none">{esc(title_of(it))}</a>
<span style="color:#6b6a66;font-size:13px"> · {esc(it.get('source'))}</span>
{f'<br><span style="color:#44433f;font-size:14px">{esc(summary[:260])}</span>' if summary else ''}</li>""")
    return f'<ul style="padding-left:18px;margin:8px 0">{"".join(rows)}</ul>'


def frame(heading, body):
    return f"""<div style="font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;max-width:640px;margin:0 auto;color:#1d1d1b">
<div style="border-top:4px solid {ACCENT};padding:16px 0 4px"><b style="font-size:20px">Helm-Radar</b>
<span style="color:#6b6a66"> · {esc(heading)}</span></div>
{body}
<p style="margin-top:24px"><a href="{SITE}" style="background:{ACCENT};color:#fff;padding:9px 16px;border-radius:8px;text-decoration:none;font-weight:600">Helm-Radar öffnen</a></p>
<p style="color:#9c9a94;font-size:12px">Automatisch erstellt vom Helm-Radar.</p></div>"""


def topic(data, tid):
    return next((t["items"] for t in data.get("topics", []) if t["id"] == tid), [])


def new_tenders(data, prev, now):
    known = {i["link"] for t in prev.get("topics", []) for i in t.get("items", [])} | set(prev.get("ai_cache") or {})
    if not prev.get("topics"):
        return []  # ohne Vergleichsstand lieber kein Alarm als eine Flut
    cutoff = now - dt.timedelta(days=ALERT_MAX_AGE_DAYS)
    return [i for i in topic(data, "ausschreibungen")
            if i["link"] not in known and (parse(i.get("published")) or now) >= cutoff]


def digest(data, now):
    week = now - dt.timedelta(days=7)
    recent = lambda i: (parse(i.get("published")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) >= week  # noqa: E731
    score = lambda i: (i.get("ai") or {}).get("score", 5)  # noqa: E731
    open_tenders = sorted((i for i in topic(data, "ausschreibungen") if parse(i.get("deadline")) and parse(i["deadline"]) > now),
                          key=lambda i: i["deadline"])
    awards = [i for i in topic(data, "zuschlaege") if recent(i)]
    sections = []
    if open_tenders:
        sections.append(f"<h3 style='margin:20px 0 4px'>🪖 Offene Ausschreibungen ({len(open_tenders)})</h3>{tender_rows(open_tenders[:12], now)}")
    if awards:
        lines = "".join(f"<li style='margin:0 0 8px'>{flag(i.get('country'))} <a href='{esc(i['link'])}' style='color:#1d1d1b'>{esc(title_of(i))}</a>"
                        f"<br><span style='color:#6b6a66;font-size:13px'>Gewinner: {esc(', '.join(i.get('winners') or ['nicht angegeben']))}</span></li>"
                        for i in awards[:8])
        sections.append(f"<h3 style='margin:20px 0 4px'>🏆 Neue Zuschläge</h3><ul style='padding-left:18px'>{lines}</ul>")
    groups = [("schuberth", "⭐ Schuberth"), ("konkurrenz", "🏢 Wettbewerber"), ("branche", "🛡️ Branche"),
              ("normen", "📏 Normen"), ("technologie", "🔬 Technik und Forschung"), ("weltpolitik", "🌍 Geopolitik")]
    for tid, label in groups:
        items = sorted((i for i in topic(data, tid) if recent(i)), key=lambda i: (score(i), i.get("published") or ""), reverse=True)[:4]
        if items:
            sections.append(f"<h3 style='margin:20px 0 4px'>{label}</h3>{news_rows(items)}")
    if not sections:
        sections.append("<p>Diese Woche gab es keine neuen Meldungen.</p>")
    intro = (f"<p>Guten Morgen! Das hat sich in der Woche bis {now.astimezone(BERLIN).strftime('%d.%m.')} getan"
             f"{f', {len(open_tenders)} Ausschreibungen sind gerade offen' if open_tenders else ''}.</p>")
    return frame("Wochenüberblick", intro + "".join(sections))


def send(subject, body, to, key, dry_run):
    if dry_run:
        path = Path("/tmp") / f"helm-radar-{subject.split()[0].lower().strip(':')}.html"
        path.write_text(body, encoding="utf-8")
        print(f"[dry-run] {subject} -> {', '.join(to)} ({path})")
        return True
    req = urllib.request.Request("https://api.resend.com/emails", method="POST",
                                 data=json.dumps({"from": SENDER, "to": to, "subject": subject, "html": body}).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "helm-radar"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True
    except Exception as e:  # noqa: BLE001 - eine fehlgeschlagene Mail darf die Seite nicht aufhalten
        detail = e.read().decode(errors="replace")[:200] if hasattr(e, "read") else ""
        print(f"::warning title=Mail fehlgeschlagen::{subject}: {e} {detail}")
        return False


def main():
    dry_run = "--dry-run" in sys.argv
    key = os.environ.get("RESEND_API_KEY")
    to = [a.strip() for a in os.environ.get("NOTIFY_TO", "").split(",") if a.strip()]
    if not dry_run and not (key and to):
        print("Mail: RESEND_API_KEY oder NOTIFY_TO fehlt, nichts versendet")
        return 0
    data = json.loads(OUT.read_text(encoding="utf-8"))
    prev = load_previous()
    now = dt.datetime.now(dt.timezone.utc)
    state = dict(prev.get("notify") or {})
    done = []

    fresh = new_tenders(data, prev, now)
    if fresh:
        subject = (f"Neue Helm-Ausschreibung: {title_of(fresh[0])[:70]}" if len(fresh) == 1
                   else f"{len(fresh)} neue Helm-Ausschreibungen")
        body = frame("Sofortalarm", f"<p>Seit dem letzten Abruf neu auf dem Radar:</p>{tender_rows(fresh[:15], now)}")
        if send(subject, body, to, key, dry_run):
            done.append(f"Alarm ({len(fresh)} neu)")

    local = now.astimezone(BERLIN)
    week_id = f"{local.isocalendar().year}-W{local.isocalendar().week:02d}"
    force_digest = "--digest" in sys.argv
    if force_digest or (local.weekday() == DIGEST_WEEKDAY and local.hour >= DIGEST_HOUR and state.get("digest_week") != week_id):
        if send(f"Helm-Radar: Wochenüberblick KW {local.isocalendar().week}", digest(data, now), to, key, dry_run):
            if not force_digest:
                state["digest_week"] = week_id
            done.append("Wochen-Digest")

    # Zustand mit der Seite veröffentlichen, damit der nächste Lauf weiß, was schon verschickt ist
    data["notify"] = state
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    msg = ", ".join(done) or "nichts Neues"
    print(f"Mail: {msg}")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice title=Mail::{msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
