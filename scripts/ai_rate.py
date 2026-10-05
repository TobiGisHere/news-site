"""KI-Bewertung der Meldungen mit Claude.

Jede neue Meldung bekommt eine Relevanz von 0 bis 10 für den Markt ballistischer Schutzhelme,
eine deutsche Kurzfassung und eine Begründung. Bereits bewertete Links kommen aus dem Cache,
damit pro Lauf nur neue Meldungen Kosten verursachen. Ohne ANTHROPIC_API_KEY passiert nichts.
"""

import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("AI_MODEL", "claude-haiku-4-5")
BATCH = 20
MAX_NEW_PER_RUN = 300
# Meldungen unter dieser Relevanz werden ausgeblendet (Ausschreibungen etwas großzügiger)
MIN_SCORE = {"ausschreibungen": 3}
DEFAULT_MIN_SCORE = 4

SYSTEM = """Du bewertest Meldungen für eine persönliche Marktbeobachtung rund um ballistische Schutzhelme \
(Hersteller- und Vertriebssicht: Militär, Polizei, Spezialeinheiten). Der Leser will nur sehen, was für dieses Geschäft zählt.

Relevanz 0 bis 10 je Rubrik:
- ausschreibungen: 9-10 = Beschaffung ballistischer Helme/Gefechtshelme/Polizei-Schutzhelme (auch als Los); \
5-7 = Schutzausrüstung, bei der Helme wahrscheinlich dazugehören; 0-2 = nur Westen/Platten, Feuerwehr-, Bau-, Fahrrad-, Reithelme oder kein Bezug.
- konkurrenz: Neuigkeiten von Helmherstellern (Ulbrichts, Mehler/Busch, Galvion, Gentex/Ops-Core, Team Wendy, Revision, MSA u. a.). \
Hoch = Helme, Helmaufträge, neue Helmprodukte, Übernahmen; niedrig = Themen ohne Helmbezug.
- branche: Branche ballistischer Schutz. Hoch = Helme, Normen (VPAM, NIJ, STANAG), Aufträge, Messen; niedrig = allgemeine Wirtschaft.
- technologie: Hoch = Forschung/Technik zu ballistischem Schutz, Helmmaterialien (UHMWPE, Aramid, Verbundwerkstoffe), \
Behind-Armor Blunt Trauma, Blast/Schädel-Hirn-Trauma durch Beschuss, Helmprüfung; mittel = allgemeine Kopfschutz-Biomechanik; \
0-2 = Sport-Gehirnerschütterungen ohne Schutzbezug, Archäologie, Medizin ohne Schutzbezug.
- weltpolitik: Hoch = Geopolitik mit Folgen für Verteidigungsbeschaffung, Nachfrage nach Schutzausrüstung, Rüstungsbudgets, \
Lieferketten/Logistik, Exportregeln; niedrig = Innenpolitik und Allgemeines.

Für jede Meldung: score (ganze Zahl), summary (1-2 sachliche Sätze auf Deutsch, was drinsteht und warum es zählt; \
keine Floskeln, nichts erfinden), why (max. 8 Wörter Begründung), facts (nur bei Ausschreibungen: Menge, Wert oder Helmtyp, \
falls genannt, sonst leer)."""

TOOL = {
    "name": "bewertung",
    "description": "Bewertungen aller Meldungen zurückgeben",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "score": {"type": "integer", "minimum": 0, "maximum": 10},
                        "summary": {"type": "string"},
                        "why": {"type": "string"},
                        "facts": {"type": "string"},
                    },
                    "required": ["id", "score", "summary", "why"],
                },
            }
        },
        "required": ["results"],
    },
}


def _call(batch, key):
    lines = []
    for n, it in enumerate(batch):
        lines.append(json.dumps({
            "id": n, "rubrik": it["topic"], "titel": it["title"], "quelle": it.get("source"),
            "auftraggeber": it.get("buyer"), "text": (it.get("summary") or "")[:700],
        }, ensure_ascii=False))
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 6000,
        "system": SYSTEM,
        "tools": [TOOL],
        "tool_choice": {"type": "tool", "name": "bewertung"},
        "messages": [{"role": "user", "content": "Bewerte diese Meldungen (eine JSON-Zeile je Meldung):\n" + "\n".join(lines)}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    block = next(b for b in data["content"] if b["type"] == "tool_use")
    out = {}
    for res in block["input"].get("results", []):
        i = res.get("id")
        if isinstance(i, int) and 0 <= i < len(batch):
            out[batch[i]["link"]] = {
                "score": max(0, min(10, int(res.get("score", 0)))),
                "summary": (res.get("summary") or "").strip()[:400],
                "why": (res.get("why") or "").strip()[:80],
                "facts": (res.get("facts") or "").strip()[:120] or None,
            }
    return out


def rate(topics, cache):
    """Bewertet neue Meldungen, blendet irrelevante aus und gibt den neuen Cache zurück."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    all_items = [it for t in topics for it in t["items"]]
    if not key:
        # Ohne Schlüssel vorhandene Bewertungen weiter anzeigen, aber nichts ausblenden
        for it in all_items:
            if it["link"] in cache:
                it["ai"] = cache[it["link"]]
        return cache, "kein API-Schlüssel"
    todo = [it for it in all_items if it["link"] not in cache][:MAX_NEW_PER_RUN]
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    errors = []

    def run(b):
        try:
            return _call(b, key)
        except Exception as e:  # noqa: BLE001 - nicht bewertete Meldungen bleiben sichtbar
            errors.append(f"{e.__class__.__name__}: {str(e)[:200]}")
            return {}

    with ThreadPoolExecutor(max_workers=4) as pool:
        for res in pool.map(run, batches):
            cache.update(res)

    dropped = 0
    for t in topics:
        keep = []
        for it in t["items"]:
            ai = cache.get(it["link"])
            if ai:
                it["ai"] = ai
                if ai["score"] < MIN_SCORE.get(t["id"], DEFAULT_MIN_SCORE):
                    dropped += 1
                    continue
            keep.append(it)
        t["items"] = keep
    # Cache auf aktuelle Kandidaten begrenzen, damit die Datei nicht endlos wächst
    current = {it["link"] for it in all_items}
    cache = {k: v for k, v in cache.items() if k in current}
    msg = f"{len(todo)} neu bewertet, {dropped} ausgeblendet"
    if errors:
        msg += f", {len(errors)} Fehler: {errors[0]}"
        print("KI-Fehler: " + " | ".join(errors), file=sys.stderr)
    return cache, msg
