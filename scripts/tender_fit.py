"""Passung einer Helm-Ausschreibung für Schuberth: Hoch, Mittel oder Gering.

Die KI-Relevanz sagt nur, ob es um ballistische Helme geht (fast alle Treffer bekommen 9 oder 10).
Für die Frage "Lohnt sich das für uns?" zählen drei Dinge:
- Helmtyp: eigene Helm-Beschaffung oder Helm nur als Teil eines Westen-Pakets, Spezialhelm, Dienstleistung
- Auftraggeber: Streitkräfte und Polizei sind Kernkunden, Kommunen und Firmen kaufen meist kleine Mengen
- Umfang: Stückzahl oder Schätzwert

Die KI liefert Helmtyp, Auftraggeber und Menge mit (Felder kind, buyer_type, qty). Fehlen sie, weil die
Meldung noch nicht neu bewertet wurde, schätzen Stichwortlisten in mehreren Sprachen dieselben Merkmale.
"""

import re

LEVELS = (("hoch", 80), ("mittel", 60), ("gering", 0))

KIND_POINTS = {"helm": 40, "paket": 25, "schutz": 25, "spezial": 15, "service": 5}
KIND_LABEL = {"helm": "ballistischer Helm", "paket": "Helm im Paket", "schutz": "Einsatzhelm ohne Beschuss",
              "spezial": "Spezialhelm", "service": "Dienstleistung"}
BUYER_POINTS = {"militaer": 30, "polizei": 25, "zivil": 0, None: 10}
BUYER_LABEL = {"militaer": "Militär", "polizei": "Polizei/Sicherheit", "zivil": "Kommune/Firma"}

# Grobe Umrechnung in Euro, nur um Größenordnungen zu vergleichen
EUR = {"EUR": 1, "UAH": 0.022, "PLN": 0.23, "CZK": 0.04, "GBP": 1.17, "USD": 0.92, "CHF": 1.05, "SEK": 0.087,
       "NOK": 0.085, "DKK": 0.134, "HUF": 0.0025, "RON": 0.2, "BGN": 0.51, "CAD": 0.67, "ISK": 0.0066}


def _rx(words):
    return re.compile("|".join(words), re.I)


SERVICE = _rx([r"reparatur", r"repair", r"ремонт", r"naprawa", r"prüfung von", r"testing", r"badani[ae]", r"wartung",
               r"maintenance", r"entretien", r"dienstleistung"])
SPECIAL = _rx([r"flieger", r"flughelm", r"pilot", r"hubschrauber", r"helicop", r"hélicop", r"aviat", r"hgu-", r"lotnicz",
               r"motorrad", r"motorcycl", r"\bmoto", r"motocykl", r"bomben", r"\beod\b", r"tedax", r"entschärf", r"démin",
               r"sapper", r"сапер", r"пілот"])
RIOT = _rx([r"pare-coups", r"riot", r"aufstand", r"schlag ?schutz", r"maintien de l.ordre", r"orden p[uú]blico",
            r"antidisturb", r"\bpsu\b", r"public order", r"\bmo\b", r"hełm(y|ów)? ochronn\w* z osłoną"])
PACKAGE = _rx([r"weste", r"\bvest", r"kamizel", r"жилет", r"плитоносц", r"plattenträger", r"plate carrier", r"бронеплит",
               r"panzerplatt", r"płyt", r"chaleco", r"gilet", r"körperschutz", r"body armou?r", r"schild", r"shield",
               r"escudo", r"bouclier", r"tarcz", r"щит", r"(?<!ein)kleidung", r"odzież", r"uniform"])
LOT = _rx([r"\blos\b", r"\blot\b", r"\blote\b", r"część", r"лот", r"частина"])

MILITARY = _rx([r"obron", r"оборон", r"verteidigung", r"bundeswehr", r"baainbw", r"armed forces", r"arm[ée]e", r"\bdga\b",
                r"direction générale de l.armement", r"defen[cs]", r"defensa", r"ej[ée]rcito", r"militar", r"military",
                r"wojsk", r"військов", r"збройн", r"гвард", r"омега", r"gynybos", r"krašto apsaug", r"šaulių",
                r"kaitsev", r"kaitseinvest", r"\bnspa\b", r"\bnato\b", r"forsvar", r"puolustus", r"honvéd", r"védelmi",
                r"armád", r"obrany", r"mando", r"marine", r"luftwaffe", r"air force", r"navy", r"armada"])
POLICE = _rx([r"poli[cz]", r"politi", r"\bpolis", r"політ", r"поліц", r"gendarm", r"guardia civil", r"straż graniczn", r"straży graniczn",
              r"border", r"grenz", r"frontex", r"zoll", r"customs", r"justiz", r"justice", r"väzensk", r"prison",
              r"виправн", r"служб\w* безпек", r"ochrony państwa", r"politsei", r"piirivalve", r"carabinier", r"intérieur",
              r"interior", r"vnitra", r"depafi", r"sailmi", r"constabulary", r"ertzaintza", r"seguridad", r"mossos",
              r"security", r"sicherheit", r"guardia", r"sécurité", r"bezpieczeństw", r"pravosuđ", r"inneres", r"innenmin"])
CIVIL = _rx([r"pożarn", r"\bpsp\b", r"feuerwehr", r"\bfire\b", r"pompier", r"bomberos", r"рятув", r"міськ", r"сільськ", r"селищн",
             r"\bрада\b", r"council", r"gmina", r"powiat", r"miasto", r"urząd", r"ayuntamiento", r"commune", r"ville de",
             r"m[ée]tropole", r"stadt", r"gemeinde", r"landkreis", r"товариств", r"акціонерн", r"\bтов\b", r"\bпат\b",
             r"\bат\b", r"компан", r"підприємств", r"gmbh", r"\bs\.a\.", r"sp\. z o\.o", r"\bltd", r"univers", r"коледж",
             r"college", r"банк", r"\bbank", r"енерго", r"газ", r"нафт", r"комунальн", r"zivilschutz", r"civil protection",
             r"medical", r"медичн", r"лікар", r"foundation", r"stiftung"])

QTY = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.  ']\d{3})+|\d+)\s*(?:x\s*)?(?:[\wäöüÄÖÜ\-]+\s+){0,3}?[\wäöüÄÖÜ\-]*?"
                 r"(?:stk|stück|szt|pcs|pieces|units|einheiten|helm|helmet|hełm|casque|casco|šalm|přilb|шолом|шт)", re.I)
# "Los 2", "Typ 1", "Schutzklasse 1" sind keine Stückzahlen
NOT_QTY = re.compile(r"\b(?:los|lot|lote|część|лот|typ|type|тип|tipo|klasse|class|клас|level|stufe|niveau|nivel|kategorie|gen)\s*\d+", re.I)
FRAMEWORK = _rx([r"rahmen", r"framework", r"accord-cadre", r"acuerdo marco", r"umow\w* ramow", r"rámcov", r"raamovereenkomst",
                 r"raamsopimus", r"рамков", r"mehrjährig", r"multi-year", r"pluriannuel"])


def _quantity(*texts):
    best = None
    for text in texts:
        for m in QTY.finditer(NOT_QTY.sub(" ", text or "")):
            n = int(re.sub(r"\D", "", m.group(1)))
            if 0 < n < 1_000_000 and not 1990 <= n <= 2100:  # Jahreszahlen sind keine Stückzahl
                best = max(best or 0, n)
    return best


def _value_eur(it):
    rate = EUR.get((it.get("currency") or "EUR").upper())
    try:
        return round(float(it["estimate"]) * rate) if it.get("estimate") and rate else None
    except (TypeError, ValueError):
        return None


def _kind(text, it):
    if SERVICE.search(text):
        return "service"
    if SPECIAL.search(text):
        return "spezial"
    if RIOT.search(text) and not it.get("ballistic") and not re.search(r"ballist|kugel|schuss|кулез|балісти|kulood", text, re.I):
        return "schutz"
    if PACKAGE.search(text) and not LOT.search(text):
        return "paket"
    return "helm"


def _strip_cpv(title):
    """TED-Titel "Land – Warengruppe – Titel": die Warengruppe (z. B. "Militärhelme") sagt nichts über den Käufer."""
    p = (title or "").split(" – ")
    return " – ".join([p[0]] + p[2:]) if len(p) >= 3 else (title or "")


def _buyer(buyer, text):
    for name, rx in (("militaer", MILITARY), ("polizei", POLICE), ("zivil", CIVIL)):
        if buyer and rx.search(buyer):
            return name
    # Ohne Auftraggeber im Datensatz verrät manchmal der Titel den Kunden
    for name, rx in (("militaer", MILITARY), ("polizei", POLICE)):
        if rx.search(text):
            return name
    return None


def _fmt_eur(v):
    if v >= 1e6:
        return f"≈ {v / 1e6:.1f} Mio. €".replace(".", ",", 1)
    return f"≈ {max(1, round(v / 1000))} Tsd. €"


def assess(it):
    ai = it.get("ai") or {}
    title = it.get("title") or ""
    text = " ".join(filter(None, [_strip_cpv(title), _strip_cpv(ai.get("title_de")), ai.get("facts")]))
    kind = ai.get("kind") if ai.get("kind") in KIND_POINTS else _kind(text, it)
    buyer = ai.get("buyer_type") if ai.get("buyer_type") in BUYER_LABEL else _buyer(it.get("buyer") or "", text)
    qty = ai.get("qty") if isinstance(ai.get("qty"), int) and ai["qty"] > 0 else _quantity(ai.get("facts"), ai.get("title_de"), title)
    value = _value_eur(it)

    tiny = False
    if (qty and qty >= 1000) or (value and value >= 500_000):
        size = 30
    elif (qty and qty >= 200) or (value and value >= 100_000):
        size = 20
    elif (qty and qty >= 50) or (value and value >= 25_000):
        size = 5
    elif qty or value:
        size, tiny = 0, True
    else:
        size = 15  # unbekannt: fehlt oft bei großen Verfahren, deshalb nicht bestrafen
    framework = bool(FRAMEWORK.search(text))
    score = min(100, KIND_POINTS[kind] + BUYER_POINTS[buyer] + size + (10 if framework else 0))
    # Ein paar Helme für eine Gemeinde oder Einheit lohnen kein Angebot, egal wer kauft
    level = "gering" if tiny else next(name for name, floor in LEVELS if score >= floor)

    reasons = [KIND_LABEL[kind]]
    if buyer:
        reasons.append(BUYER_LABEL[buyer])
    if qty:
        reasons.append(f"{qty:,} Stück".replace(",", "."))
    elif value:
        reasons.append(_fmt_eur(value))
    if framework:
        reasons.append("Rahmenvertrag")
    return {"level": level, "score": score, "kind": kind, "buyer": buyer, "reasons": reasons}


def apply(topics, ids=("ausschreibungen",)):
    for t in topics:
        if t["id"] in ids:
            for it in t["items"]:
                it["fit"] = assess(it)
