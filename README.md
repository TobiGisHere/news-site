# Meine News

Persönliche News-Seite mit Themen-Kacheln: Ausschreibungen, Wettbewerber, Branche, KI, Weltpolitik.

## Aufbau
- `sources.json`: alle Quellen und Suchbegriffe (hier ändern, um Quellen hinzuzufügen oder zu entfernen).
- `scripts/fetch_news.py`: ruft alle Quellen ab und schreibt `site/data/news.json`.
- `site/`: die Website selbst (HTML, CSS, JavaScript, keine Abhängigkeiten).
- `.github/workflows/update-news.yml`: ruft die Quellen alle 2 Stunden (6 bis 23 Uhr) ab und veröffentlicht die Seite auf GitHub Pages.

## Einmalig einrichten
1. Repository auf GitHub anlegen und diesen Ordner hochladen.
2. In GitHub unter **Settings → Pages → Build and deployment → Source** „GitHub Actions“ wählen.
3. Unter **Actions → News aktualisieren → Run workflow** den ersten Lauf starten.

## Lokal testen
```
pip install -r requirements.txt
python scripts/fetch_news.py
cd site && python -m http.server 8000
```

## Quellentypen
- `rss`: Feed direkt. `filter: "keywords"` lässt nur Meldungen mit den Suchbegriffen des Themas durch.
- `gnews`: Google-News-Suche (`query`). Jede Quelle mit `gnews_query` bekommt zusätzlich einen eigenen Google-News-Feed.
- `api`: TED (EU) und Find a Tender (UK), gefiltert nach CPV-Codes und Suchbegriffen.
- `scrape`: Die Seite wird nach einem verlinkten RSS-Feed durchsucht. Seiten ohne Feed erscheinen im Quellen-Status und bekommen später einen eigenen Abruf.
