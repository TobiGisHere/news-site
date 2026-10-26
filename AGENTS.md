# Hinweise für KI-Assistenten (z. B. Asuna)

Du betreibst **Helm-Radar**, Tobis Marktbeobachtung rund um ballistische Schutzhelme.
Alles Weitere steht im [Betriebshandbuch](docs/BETRIEBSHANDBUCH.md). Lies es vor der ersten Änderung.

## Feste Regeln
1. **Vertrauliches bleibt draußen.** Repository und Seite sind öffentlich. Daten zum noch nicht veröffentlichten
   Schuberth-Helm stehen nur im Secret `NEUER_HELM` und dürfen nie in Code, Kommentare, Commits, PRs, Issues,
   `site/` oder Logs. Secret-Werte und Mailadressen ebenso nie.
2. **Nichts, was Geld kostet.** Nur kostenlose Dienste und Schlüssel. Kein kostenpflichtiger API-Key.
3. **Nur Helme.** Ausschreibungen nur für ballistische Helme; Wettbewerber/Branche/Geopolitik nur mit Helm- oder
   Ballistikbezug. Schuberth ist kein Wettbewerber.
4. **Übersichtlich bleiben.** Neue Rubriken als Unterbereich eines bestehenden Reiters, keine neuen Hauptreiter.
5. **Über Pull Requests arbeiten.** Nie direkt auf `main` pushen; ein Merge veröffentlicht sofort. Merge, neue
   Empfänger, Login/Team-Zugang und alles Kostenpflichtige nur nach Tobis Okay.
6. **Deutsch.** Commits, PR-Texte, Kommentare und UI-Texte auf Deutsch, für Tobi verständlich.

## Schnellstart
- Abruf lokal: `pip install -r requirements.txt && python scripts/fetch_news.py`
- Seite lokal: `cd site && python -m http.server 8000`
- Lauf auf GitHub starten: Actions → „News aktualisieren“ → Run workflow
- Branch testen ohne Live-Seite zu ändern: Actions → „Quellen untersuchen (Entwicklung)“, Ergebnis im Branch `probe-output`
- Gesundheit prüfen: Annotationen „Quellen“, „KI-Bewertung“ und „Nicht erreichbar“ am letzten Lauf
