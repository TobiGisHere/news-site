# Helm-Radar: Betriebshandbuch

Stand: 26.10.2026. Dieses Handbuch beschreibt, wie die Seite gebaut ist, wie sie läuft und was im Betrieb zu tun ist.
Es richtet sich an alle, die den Betrieb übernehmen, auch an eine KI. Die Kurzfassung mit den festen Regeln steht in
[`AGENTS.md`](../AGENTS.md).

- Live-Seite: https://tobigishere.github.io/news-site/
- Repository: https://github.com/TobiGisHere/news-site (öffentlich)
- Eigentümer: Tobi (GitHub `TobiGisHere`), arbeitet bei Schuberth. Sprache im Projekt: Deutsch.

## 1. Worum es geht

Helm-Radar ist eine persönliche Marktbeobachtung rund um **ballistische Schutzhelme**. Die Seite sammelt alle paar
Stunden Ausschreibungen, Zuschläge, Wettbewerber-News, Forschung, Patente, Normen und passende Geopolitik, lässt sie
von einer kostenlosen KI (Google Gemini) bewerten und zeigt sie als statische Seite auf GitHub Pages.

Inhaltliche Leitplanken (von Tobi festgelegt):
- Die Seite handelt **nur von Helmen**. Ausschreibungen nur für ballistische Helme, keine Westen, Schutzkleidung,
  Motorrad-, Feuerwehr-, Flieger-, Riot- oder EOD-Helme.
- Wettbewerber- und Branchennews nur mit Helm- oder Ballistikbezug. Schuberth ist **kein** Wettbewerber, sondern hat
  einen eigenen kleinen Bereich „Schuberth“.
- Geopolitik nur, soweit relevant für Schutzhelme, Logistik oder Verteidigung.
- Die Seite soll übersichtlich bleiben: Neue Rubriken kommen als Unterbereich in einen bestehenden Reiter, nicht als
  neuer Hauptreiter.
- **Es darf nichts kosten.** Nur kostenlose Dienste und Schlüssel. Kein kostenpflichtiger API-Key (auch kein
  Anthropic-Key).

## 2. Architektur

```
GitHub Actions (Zeitplan, manuell oder Push auf main)
  └─ scripts/fetch_news.py      ruft alle Quellen aus sources.json ab
       ├─ ai_rate.py            Gemini bewertet neue Meldungen (Score 0–10, deutscher Titel, Zusammenfassung)
       └─ tender_fit.py         Passung jeder Ausschreibung für Schuberth: Hoch / Mittel / Gering
     → site/data/news.json
  └─ scripts/notify.py          Sofortalarm bei neuer Helm-Ausschreibung, Wochen-Digest Montag ab 8 Uhr (Resend)
  └─ feedback-config.js         Web3Forms-Schlüssel für den Feedback-Knopf wird beim Bauen hineingeschrieben
  └─ Deploy auf GitHub Pages    site/ ist die komplette Website (HTML/CSS/JS ohne Build-Schritt)
```

Wichtig: `site/data/news.json` liegt **nicht** im Repository. Jeder Lauf lädt den vorherigen Stand von der Live-Seite
(`load_previous()` in `fetch_news.py`), mischt die neuen Treffer dazu und veröffentlicht das Ergebnis. Der Zustand der
Seite (alte Meldungen, KI-Cache, Mail-Status wie `notify.digest_week`) lebt also in der veröffentlichten news.json.
Ein kaputter Lauf, der eine leere news.json veröffentlicht, würde diesen Zustand verlieren. Darum bricht
`fetch_news.py` mit Fehler ab, wenn keine einzige Quelle erreichbar war.

### Dateien

| Datei | Zweck |
|---|---|
| `sources.json` | Alle Rubriken (`topics`) mit Quellen, Suchbegriffen und Filtern. Hier Quellen ändern. |
| `scripts/fetch_news.py` | Abruf aller Quellentypen, Filter, Entdoppeln, TED-Verfahren zusammenführen, Schreiben der news.json. |
| `scripts/ai_rate.py` | KI-Bewertung. Reihenfolge der Anbieter: `ANTHROPIC_API_KEY` (nicht genutzt, kostet) → `GEMINI_API_KEY` → GitHub Models nur mit `USE_GITHUB_MODELS`. Schwellen in `MIN_SCORE`/`DEFAULT_MIN_SCORE` (darunter ausgeblendet), Prompt in `SYSTEM`. |
| `scripts/tender_fit.py` | Passung Hoch/Mittel/Gering. Produktpassung zur M100 ist das wichtigste Kriterium (50 Punkte), dazu Auftraggeber (25), Umfang (25), Rahmenvertrag (5). Hoch ab 80, Mittel ab 55. |
| `scripts/notify.py` | Mails über Resend. Lokal testen ohne Versand: `python scripts/notify.py --dry-run --digest`. |
| `scripts/probe.py`, `probe_urls.txt` | Entwicklungswerkzeug: lädt Seiten roh herunter (siehe Workflow „Quellen untersuchen“). |
| `site/index.html`, `app.js`, `style.css` | Die Seite. Reiter und Unterbereiche in `SECTIONS`, Wettbewerberprofile in `PROFILES` (Busch, Mehler, Galvion). |
| `site/feedback.js`, `feedback-config.js` | Feedback-Knopf (Web3Forms). Ohne Schlüssel bleibt der Knopf unsichtbar. |
| `site/messen.json` | Messekalender (Reiter Termine). Von Hand gepflegt. |

### Rubriken (sources.json → `topics`)

| id | Anzeige | Bereich auf der Seite |
|---|---|---|
| `ausschreibungen` | Ausschreibungen ballistische Helme | Ausschreibungen › Offen |
| `zuschlaege` | Zuschläge (aus TED-Vergabebekanntmachungen) | Ausschreibungen › Zuschläge |
| `konkurrenz` | Wettbewerber | Markt › Wettbewerber |
| `schuberth` | Schuberth-News | Markt › Schuberth |
| `branche` | Branche, Berichte, Behörden | Markt › Branche |
| `technologie` | Technologieradar (OpenAlex, arXiv, News) | Technik & Normen › Forschung |
| `patente` | Patente (Google Patents, später EPO) | Technik & Normen › Patente |
| `normen` | Normen-Radar | Technik & Normen › Normen |
| `weltpolitik` | Geopolitik, Verteidigung, Logistik | Geopolitik |

Filterfelder pro Rubrik: `keywords` (Treffer muss eines enthalten, bei `filter: "keywords"`), `require_keywords` und
`require_also_keywords` (beide Listen müssen treffen, z. B. Helm **und** ballistisch), `reject_keywords` (Meldung
fliegt raus), `exclude_keywords` (nur diese Wörter zählen nicht als Treffer), `title_only`, `max_items`,
`ai_borderline` (Grenzfälle ohne Ballistik-Begriff bewertet die KI, sichtbar ab Score 7).

Quellentypen: `rss`, `gnews` (Google-News-Suche), `api` (TED, Find a Tender, Vergabeportale, OpenAlex, Google Patents
usw., jeweils eigene Funktion `fetch_*` in `fetch_news.py`), `scrape` (HTML-Seite, Links per `link_pattern` oder
RSS-Autodiscovery), `info` (nur Hinweis im Quellen-Status).

## 3. Workflows (GitHub Actions)

### „News aktualisieren“ (`.github/workflows/update-news.yml`)
- Läuft per Zeitplan stündlich zwischen ca. 6 und 23 Uhr deutscher Zeit (`cron: "17 4-21 * * *"`, UTC), bei jedem
  Push auf `main` und manuell über **Actions → News aktualisieren → Run workflow**.
- Manuell mit Haken „Wochen-Digest sofort als Test verschicken“ geht der Digest sofort raus.
- **GitHub lässt geplante Läufe oft ausfallen** (bei diesem Repo teils nur wenige Läufe pro Tag). Das ist kein Fehler
  im Code. Wenn die Seite veraltet ist, einfach manuell starten.
- Ergebnisse stehen als Anmerkungen (Annotations) am Lauf: „Quellen“ (x/y ok und Anzahl je Rubrik), „KI-Bewertung“,
  „Beispiele …“ (erste Titel je Rubrik), „Nicht erreichbar“ (Quellen mit Fehler).

### „Quellen untersuchen (Entwicklung)“ (`.github/workflows/probe.yml`)
- Nur manuell. Lädt die Adressen aus `scripts/probe_urls.txt` roh herunter und macht einen kompletten Probeabruf
  mit dem Code des gewählten Branches, **ohne** die Live-Seite zu ändern.
- Ergebnis landet im Branch `probe-output` (wird jedes Mal überschrieben), inklusive einer Vorschau-`news.json`.
- Gedacht zum Entwickeln neuer Scraper und zum Testen eines Branches vor dem Merge.

### Manuell per API starten
`POST /repos/TobiGisHere/news-site/actions/workflows/update-news.yml/dispatches` mit `{"ref":"main"}`
(oder `gh workflow run update-news.yml`). Optional `{"inputs":{"test_mail":"true"}}`.

## 4. Secrets

Alle unter **Settings → Secrets and variables → Actions**. Hier stehen nur die Namen. **Werte gehören nie ins Repo,
in Commits, Issues, PRs oder Logs.**

| Secret | Wofür | Kosten | Fehlt es … |
|---|---|---|---|
| `GEMINI_API_KEY` | KI-Bewertung, deutsche Titel, Passungsmerkmale | kostenlos (Google AI Studio) | wird nichts bewertet oder gefiltert, Deutsch-Schalter verschwindet |
| `RESEND_API_KEY` | Mailversand | kostenlos (resend.com) | keine Mails |
| `NOTIFY_TO` | Empfänger der Mails (Tobis Firmenadresse), mehrere durch Komma | – | keine Mails |
| `WEB3FORMS_KEY` | Feedback-Knopf, Mails gehen an die im Web3Forms-Konto hinterlegte Adresse | kostenlos (250/Monat) | Knopf unsichtbar |
| `NEUER_HELM` | **Vertraulich.** Freitext mit Daten eines noch nicht veröffentlichten Schuberth-Helms für die Passung | – | Passung nur nach M100 |
| `SAM_API_KEY` | US-Ausschreibungen (SAM.gov) | kostenlos | Quelle wird übersprungen |
| `OPS_KEY`, `OPS_SECRET` | EPO Open Patent Services (aktuelle Patente), erst mit PR #7 | kostenlos (developers.epo.org) | nur Google Patents (ca. 8 Wochen Verzug) |
| `ANTHROPIC_API_KEY` | wäre kostenpflichtig, **nicht anlegen** | kostet | – |

`GITHUB_TOKEN` stellt GitHub automatisch bereit.

### Vertrauliche Produktdaten (wichtigste Regel)
Schuberth bringt bald einen zweiten ballistischen Helm heraus. Seine Daten sind **vertraulich**. Das Repository und die
Seite sind öffentlich. Deshalb:
- Die Daten stehen ausschließlich im Secret `NEUER_HELM`. `tender_fit.py` liest daraus nur die höchste VPAM-Stufe,
  `ai_rate.py` gibt den Text vertraulich an Gemini.
- Niemals Schutzklasse, Gewicht, Normen, Namen oder Erscheinungstermin dieses Helms in Code, Kommentare, Commits,
  PRs, Issues, Dateien in `site/` oder Ausgaben im Log schreiben. Auf der Seite heißt die Passung dazu nur neutral
  „passt“.
- Öffentlich bekannte Daten zur M100 (NIJ IIIA, VPAM 2, V50 690 m/s, Schnitte, Mandible, Visier, T100C) dürfen im
  Code stehen; sie stammen aus öffentlichen Datenblättern.

## 5. Wiederkehrende Aufgaben

| Wann | Was |
|---|---|
| täglich, kurz | Läuft der Workflow? Letzter Lauf grün, Anzeige „Aktualisiert …“ oben auf der Seite aktuell. Sonst manuell starten. |
| täglich, kurz | Annotation „Nicht erreichbar“ ansehen. Einzelne Ausfälle sind normal; fällt eine Quelle mehrere Tage aus, URL oder Parser prüfen. |
| wöchentlich (Montag) | Kam der Wochen-Digest bei Tobi an? Falls nein: Lauf nach 8 Uhr gab es? Resend-Secrets gesetzt? |
| wöchentlich | Stichproben: Rutschen fachfremde Treffer durch (Westen, Motorradhelme, Kleinkram)? Dann `reject_keywords` in `sources.json` oder Prompt in `ai_rate.py` nachschärfen. Fehlen bekannte Treffer? Dann Suchbegriffe ergänzen. |
| monatlich | Messekalender `site/messen.json` aktualisieren (Felder mit `unsure` prüfen, abgelaufene Messen entfernen, neue Termine eintragen). |
| bei Bedarf | Feedback aus dem Feedback-Knopf (kommt per Mail bei Tobi an) umsetzen. |

## 6. Änderungen machen

1. Von `main` einen Branch abzweigen, Änderung machen.
2. Lokal testen:
   ```
   pip install -r requirements.txt
   python scripts/fetch_news.py          # schreibt site/data/news.json (ohne Secrets: ohne KI, ohne Passung nach NEUER_HELM)
   cd site && python -m http.server 8000 # http://localhost:8000
   ```
   Einige Quellen (Google Patents, manche Portale) blockieren Rechenzentren oder Proxys. Dann den Branch mit dem
   Workflow „Quellen untersuchen“ testen und die Vorschau-news.json aus Branch `probe-output` ansehen.
3. Pull Request gegen `main` öffnen, Beschreibung auf Deutsch, für Tobi verständlich (was ändert sich auf der Seite).
4. Merge nur nach Tobis Okay. Der Merge löst sofort einen Lauf aus und veröffentlicht die Seite.

Stil im Code: Kommentare und Texte auf Deutsch, keine zusätzlichen Abhängigkeiten (nur `feedparser`), Frontend ohne
Framework und ohne Build-Schritt.

## 7. Offene Punkte (Stand 26.10.2026)

- **PR #7 – Patente direkt vom EPA (EPO OPS):** wartet darauf, dass Tobi die kostenlosen Secrets `OPS_KEY` und
  `OPS_SECRET` anlegt. Danach mit „Quellen untersuchen“ auf dem Branch testen, ob EPO-Treffer kommen, dann mergen.
  Hintergrund: Google Patents hinkt etwa 8 Wochen hinterher, ein neues Ulbrichts-Patent wurde dadurch verpasst.
- **PR #5 – Technologieradar erweitern** (Werkstoffe, Fertigung, Polster, Kopf-Nacken, Passform): offen seit 11.10.,
  Status mit Tobi klären.
- **Passwortschutz für die Seite:** Tobi hat ihn angefragt, Optionen liegen vor (u. a. verschlüsselte Daten im
  öffentlichen Pages-Hosting), Entscheidung von Tobi steht aus. Kostenlos bleiben.
- **Aus dem Ausbauplan noch nicht umgesetzt:** gemeinsamer Bieterstatus pro Ausschreibung (beobachten / in Prüfung /
  Angebot in Arbeit / abgegeben / gewonnen / verloren / kein Gebot) und Login. Geplant war Cloudflare Pages + Access
  + D1 (kostenlos), noch nicht begonnen.
- **Quellen, die Schlüssel bräuchten:** Doffin (Norwegen), Hilma (Finnland) – nicht gebaut.
- Alte Branches `tender-upgrade`, `claude/project-thread-6mpw2z`, `claude/project-thread-n5kere` sind gemergt bzw.
  überholt und können weg (nur nach Rückfrage löschen).

## 8. Typische Fehler und was hilft

| Symptom | Ursache | Abhilfe |
|---|---|---|
| Seite seit Stunden nicht aktualisiert | GitHub hat geplante Läufe ausgelassen | Workflow manuell starten. Dauerhaft: externer Auslöser (z. B. kostenloser Cloudflare-Worker-Cron, der die Dispatch-API aufruft). |
| Google News liefert HTTP 503 | zu viele Abrufe | wird schon seriell mit Pause und einer Wiederholung abgerufen; einzelne Ausfälle ignorieren |
| TED liefert 0 Treffer oder 400 | Volltextsuche verträgt nur einfache Paare `FT ~ (a* AND b*)`, keine verschachtelten OR-Gruppen | Abfragen einfach halten, ein Paar pro Abfrage (siehe `TED_BALLISTIC_FT`) |
| Polen: Treffer zur Stadt „Chełm“ | „hełm“ steckt im Ortsnamen | Wortstämme statt Teilstring verwenden |
| NSPA 403 | blockiert Bots | bekannt, nicht behebbar |
| KI-Bewertung: „überlastet“ oder 429 | Gemini-Gratiskontingent ausgeschöpft | `ai_rate.py` wartet und weicht auf leichteres Modell aus; Rest wird im nächsten Lauf bewertet |
| Deutsch-Schalter fehlt, alles unbewertet | `GEMINI_API_KEY` fehlt oder ungültig | Annotation „KI-Bewertung“ lesen, Schlüssel bei Google AI Studio erneuern |
| Keine Mails | `RESEND_API_KEY`/`NOTIFY_TO` fehlen; Resend schickt ohne eigene Domain nur an die Adresse des Resend-Kontos | Secrets prüfen; Schritt „Mails“ im Lauf ansehen (bricht den Lauf absichtlich nicht ab) |
| Feedback-Knopf fehlt | `WEB3FORMS_KEY` fehlt | Secret anlegen |
| Patente veraltet | Google Patents hinkt ~8 Wochen | PR #7 (EPO OPS) fertigstellen |
| Seite leer oder Rubrik leer nach Änderung | Filter zu streng oder Fehler im Parser | Annotation „Quellen“/„Beispiele“ vergleichen, Änderung zurücknehmen oder per Probe-Workflow testen |
| Kanada (CanadaBuys) 403 | braucht Browser-Kennung | wird mit `BROWSER_UA` abgerufen |

## 9. Kontakt und Entscheidungen

Alles, was Geld kostet, die Seite für andere öffnet (Empfänger, Login, Team), Produktdaten von Schuberth berührt oder
neue Hauptbereiche anlegt, entscheidet Tobi. Im Zweifel fragen, bevor gemergt wird.
