"""KI-Bewertung der Meldungen.

Jede neue Meldung bekommt eine Relevanz von 0 bis 10 für den Markt ballistischer Schutzhelme,
eine deutsche Kurzfassung und eine Begründung. Bereits bewertete Links kommen aus dem Cache,
damit pro Lauf nur neue Meldungen bewertet werden.

Reihenfolge: ANTHROPIC_API_KEY (Claude, kostenpflichtig), GEMINI_API_KEY (Google, kostenloses Kontingent),
sonst GitHub Models über den GITHUB_TOKEN des Workflows. Ohne Zugang passiert nichts.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = os.environ.get("AI_MODEL", "claude-haiku-4-5")
GITHUB_URL = "https://models.github.ai/inference/chat/completions"
GITHUB_MODEL = os.environ.get("GITHUB_AI_MODEL", "openai/gpt-4o-mini")
BATCH = 15
MAX_NEW_PER_RUN = 300
# GitHub Models erlaubt nur wenige Anfragen pro Tag und Minute: pro Lauf begrenzen, Rest folgt im nächsten Lauf
GITHUB_MAX_BATCHES = 8
GITHUB_PAUSE = 5
# Google Gemini: kostenloser Schlüssel aus Google AI Studio, OpenAI-kompatible Schnittstelle
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
# Bei Überlastung (HTTP 503) oder unbekanntem Modell (404) wird das nächste Modell probiert
GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-flash-latest", "gemini-flash-lite-latest",
                             "gemini-2.5-flash", "gemini-2.5-flash-lite"] if m]
GEMINI_MAX_BATCHES = 20
# Meldungen unter dieser Relevanz werden ausgeblendet (Ausschreibungen etwas großzügiger)
MIN_SCORE = {"ausschreibungen": 3, "zuschlaege": 3, "schuberth": 3}
DEFAULT_MIN_SCORE = 4

SYSTEM = """Du bewertest Meldungen für eine persönliche Marktbeobachtung rund um ballistische Schutzhelme \
(Hersteller- und Vertriebssicht: Militär, Polizei, Spezialeinheiten). Der Leser will nur sehen, was für dieses Geschäft zählt.

Relevanz 0 bis 10 je Rubrik:
- ausschreibungen: 9-10 = Beschaffung ballistischer Helme/Gefechtshelme/Polizei-Schutzhelme (auch als Los); \
5-7 = Schutzausrüstung, bei der Helme wahrscheinlich dazugehören; 0-2 = nur Westen/Platten, Feuerwehr-, Bau-, Fahrrad-, Reithelme oder kein Bezug.
- zuschlaege: Zuschlagsbekanntmachungen (wer hat gewonnen). 9-10 = Zuschlag für ballistische Helme/Gefechtshelme/Polizei-Schutzhelme; \
5-7 = Schutzausrüstung mit wahrscheinlichem Helmanteil; 0-2 = ohne Helmbezug.
- schuberth: Meldungen über Schuberth, den eigenen Arbeitgeber des Lesers. Hoch = Behörden-, Polizei- und Militärhelme, Aufträge, \
Unternehmensnachrichten, Standort, Personal; niedrig = reine Motorradhelm-Tests und Motorsport.
- normen: Prüfnormen und Standards für ballistischen Schutz (VPAM, HVN 2009, NIJ 0106/0123, EN 14458, STANAG 2920/AEP-2920, \
Technische Richtlinien der Polizei). Hoch = neue oder geänderte Norm, Entwurf, Zertifizierung, Prüfverfahren für Helme; \
mittel = Normen für Schutzwesten; niedrig = Normen ohne ballistischen Bezug.
- konkurrenz: Neuigkeiten von Helmherstellern (Ulbrichts, Mehler, Busch, Galvion, Gentex/Ops-Core, Team Wendy, Revision, MSA u. a.). \
Hoch = Helme, Helmaufträge, neue Helmprodukte, Übernahmen; niedrig = Themen ohne Helmbezug.
- branche: Branche ballistischer Schutz. Hoch = Helme, Normen (VPAM, NIJ, STANAG), Aufträge, Messen; niedrig = allgemeine Wirtschaft.
- technologie: Hoch = Forschung/Technik zu ballistischem Schutz, Helmmaterialien (UHMWPE, Aramid, Verbundwerkstoffe), \
Behind-Armor Blunt Trauma, Blast/Schädel-Hirn-Trauma durch Beschuss, Helmprüfung; mittel = allgemeine Kopfschutz-Biomechanik; \
0-2 = Sport-Gehirnerschütterungen ohne Schutzbezug, Archäologie, Medizin ohne Schutzbezug.
- weltpolitik: Hoch = Geopolitik mit Folgen für Verteidigungsbeschaffung, Nachfrage nach Schutzausrüstung, Rüstungsbudgets, \
Lieferketten/Logistik, Exportregeln; niedrig = Innenpolitik und Allgemeines.

Für jede Meldung: score (ganze Zahl), title_de (Titel auf Deutsch übersetzt; ist er schon deutsch, unverändert übernehmen; Eigennamen, Produktnamen und Normen nicht übersetzen), summary (1-2 sachliche Sätze auf Deutsch, was drinsteht und warum es zählt; \
keine Floskeln, nichts erfinden), why (max. 8 Wörter Begründung), facts (nur bei Ausschreibungen und Zuschlägen: Menge, Wert oder Helmtyp, \
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
                        "title_de": {"type": "string"},
                        "summary": {"type": "string"},
                        "why": {"type": "string"},
                        "facts": {"type": "string"},
                    },
                    "required": ["id", "score", "title_de", "summary", "why"],
                },
            }
        },
        "required": ["results"],
    },
}


def _lines(batch):
    return "\n".join(json.dumps({
        "id": n, "rubrik": it["topic"], "titel": it["title"], "quelle": it.get("source"),
        "auftraggeber": it.get("buyer"), "gewinner": it.get("winners"), "text": (it.get("summary") or "")[:600],
    }, ensure_ascii=False) for n, it in enumerate(batch))


def _parse(results, batch):
    out = {}
    for res in results or []:
        i = res.get("id")
        if isinstance(i, int) and 0 <= i < len(batch):
            try:
                score = max(0, min(10, int(res.get("score", 0))))
            except (TypeError, ValueError):
                continue
            out[batch[i]["link"]] = {
                "score": score,
                "title_de": (res.get("title_de") or "").strip()[:300] or None,
                "summary": (res.get("summary") or "").strip()[:400],
                "why": (res.get("why") or "").strip()[:80],
                "facts": (res.get("facts") or "").strip()[:120] or None,
            }
    return out


def _call_anthropic(batch, key):
    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 6000,
        "system": SYSTEM,
        "tools": [TOOL],
        "tool_choice": {"type": "tool", "name": "bewertung"},
        "messages": [{"role": "user", "content": "Bewerte diese Meldungen (eine JSON-Zeile je Meldung):\n" + _lines(batch)}],
    }).encode()
    req = urllib.request.Request(ANTHROPIC_URL, data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    block = next(b for b in data["content"] if b["type"] == "tool_use")
    return _parse(block["input"].get("results"), batch)


class _KeepPost(urllib.request.HTTPRedirectHandler):
    """GitHub Models leitet um; urllib würde dabei aus POST ein GET machen."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return urllib.request.Request(newurl, data=req.data, headers=dict(req.header_items()), method="POST")


_github_opener = urllib.request.build_opener(_KeepPost)


# Zugangswege zu GitHub Models; der erste, der echtes JSON liefert, wird für den Rest des Laufs behalten
GITHUB_ENDPOINTS = [
    ("https://models.github.ai/inference/chat/completions", GITHUB_MODEL, True),
    ("https://models.github.ai/inference/chat/completions", GITHUB_MODEL, False),
    ("https://models.inference.ai.azure.com/chat/completions", GITHUB_MODEL.split("/")[-1], False),
]
_working = []


def _github_request(url, model, gh_headers, batch, token, max_tokens=4000):
    body = json.dumps({
        "model": model,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM + "\n\nAntworte ausschließlich mit JSON der Form "
             '{"results": [{"id": 0, "score": 7, "title_de": "...", "summary": "...", "why": "...", "facts": ""}]}, '
             "genau ein Eintrag je Meldung."},
            {"role": "user", "content": "Bewerte diese Meldungen (eine JSON-Zeile je Meldung):\n" + _lines(batch)},
        ],
    }).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if gh_headers:
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with _github_opener.open(req, timeout=120) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"] or ""
        # Manche Modelle setzen das JSON in ```json-Blöcke
        start, end = content.find("{"), content.rfind("}")
        return _parse(json.loads(content[start:end + 1]).get("results"), batch)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise ValueError(f"{e.__class__.__name__} bei {url} ({model}): {raw[:160]!r}") from None


def _call_github(batch, token):
    """GitHub Models: kostenlos mit dem GITHUB_TOKEN des Workflows (Tageslimit, daher sparsam)."""
    if _working:
        return _github_request(*_working[0], batch, token)
    problems = []
    for ep in GITHUB_ENDPOINTS:
        try:
            res = _github_request(*ep, batch, token)
            _working.append(ep)
            return res
        except urllib.error.HTTPError as e:
            problems.append(f"HTTP {e.code} bei {ep[0]} ({ep[1]}): {e.read().decode('utf-8', 'replace')[:160]}")
        except Exception as e:  # noqa: BLE001
            problems.append(str(e)[:220])
    raise RuntimeError(" | ".join(problems))


def rate(topics, cache):
    """Bewertet neue Meldungen, blendet irrelevante aus und gibt den neuen Cache zurück."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    github_token = os.environ.get("GITHUB_TOKEN")
    all_items = [it for t in topics for it in t["items"]]
    # GitHub Models antwortet von den Runnern derzeit nur mit "OK"; nur auf ausdrücklichen Wunsch probieren
    if not os.environ.get("USE_GITHUB_MODELS"):
        github_token = None
    if not anthropic_key and not gemini_key and not github_token:
        # Ohne Zugang vorhandene Bewertungen weiter anzeigen, aber nichts ausblenden
        for it in all_items:
            if it["link"] in cache:
                it["ai"] = cache[it["link"]]
        return cache, "kein KI-Zugang"

    def needs(it):
        c = cache.get(it["link"])
        # Ältere Bewertungen ohne Übersetzung nachholen; ausgeblendete (nur Score) nicht
        return c is None or ("summary" in c and "title_de" not in c)
    todo = [it for it in all_items if needs(it)][:MAX_NEW_PER_RUN]
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    errors = []

    if anthropic_key:
        provider = "Claude"

        def run(b):
            try:
                return _call_anthropic(b, anthropic_key)
            except Exception as e:  # noqa: BLE001 - nicht bewertete Meldungen bleiben sichtbar
                errors.append(f"{e.__class__.__name__}: {str(e)[:200]}")
                return {}

        with ThreadPoolExecutor(max_workers=4) as pool:
            for res in pool.map(run, batches):
                cache.update(res)
    else:
        if gemini_key:
            provider, limit = "Gemini", GEMINI_MAX_BATCHES
            models = list(dict.fromkeys(GEMINI_MODELS))

            def call(b):
                last = None
                for attempt in range(len(models) * 2):
                    try:
                        res = _github_request(GEMINI_URL, models[0], False, b, gemini_key, 8000)
                        if not _working:
                            _working.append((GEMINI_URL, models[0], False))
                        return res
                    except urllib.error.HTTPError as e:
                        last = e
                        if e.code in (500, 502, 503, 504) and attempt % 2 == 0:
                            time.sleep(8)          # kurz warten, dann gleiches Modell noch einmal
                        elif e.code in (404, 500, 502, 503, 504) and len(models) > 1:
                            models.pop(0)          # nächstes Modell
                        else:
                            raise
                raise last
        else:
            provider, limit = "GitHub Models", GITHUB_MAX_BATCHES
            call = lambda b: _call_github(b, github_token)  # noqa: E731
        batches = batches[:limit]
        for n, b in enumerate(batches):
            if n:
                time.sleep(GITHUB_PAUSE)
            try:
                cache.update(call(b))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:200]
                errors.append(f"HTTP {e.code}: {detail}")
                if e.code in (401, 403, 429):  # Limit erreicht oder kein Zugriff: im nächsten Lauf weiter
                    break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{e.__class__.__name__}: {str(e)[:700]}")
                if not _working:  # kein Zugangsweg funktioniert: Limit nicht weiter verbrauchen
                    break
    rated = sum(1 for it in todo if it["link"] in cache and not needs(it))

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
    msg = f"{provider}: {rated} neu bewertet, {len(todo) - rated} offen, {dropped} ausgeblendet"
    if errors:
        msg += f", {len(errors)} Fehler: {errors[0][:900]}"
        print("KI-Fehler: " + " | ".join(errors), file=sys.stderr)
    if _working:
        msg += f" (Zugang: {_working[0][1]})"
    return cache, msg
