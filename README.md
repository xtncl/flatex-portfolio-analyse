# Portfolio-Analyse (Flatex)

Wertet den **rohen Flatex-Transaktionsexport** aus und zeigt die
Gesamtentwicklung des Aktien-Portfolios – wahlweise als lokaler Web-Server
(mit automatischer Kurs-Aktualisierung) oder als einmaliger CLI-Report.

**Kein LLM, kein API-Key** – die Auswertung ist reine Rechnung auf Basis
von Yahoo Finance (Kurse, Devisen, Splits).

---

## Web-Server (empfohlen)

### Per Docker — kein Clone, kein Build nötig

```bash
# docker-compose.yml herunterladen
curl -O https://raw.githubusercontent.com/xtncl/flatex-portfolio-analyse/main/docker-compose.yml

# Server starten (Image wird automatisch von GHCR geladen)
docker compose up -d
```

Dann im Browser öffnen: **http://localhost:8080**

Das Image wird bei jedem Push auf `main` automatisch gebaut und auf
`ghcr.io/xtncl/flatex-portfolio-analyse:latest` veröffentlicht.

### Alternativ: lokal ohne Docker

```bash
pip install -r requirements.txt
python3 server.py          # läuft auf http://localhost:8080
```

### Ablauf

1. **Depot-Export** (und optional Konto-Export) per Drag & Drop hochladen
2. **Analysieren** klicken – Fortschrittslog läuft live im Browser
3. Interaktiven Report anschauen

Der Server speichert alles in einer SQLite-Datenbank (`/app/data/portfolio.db`).
Beim Neustart des Containers sind Daten und letzter Report sofort wieder da.
Kurse werden **automatisch alle 60 Sekunden** im Hintergrund aktualisiert; der
Report lädt sich selbst neu sobald neue Daten vorliegen. Über den
**↺ Aktualisieren**-Button im Report kann man einen Refresh auch manuell
anstoßen.

Mehrere CSV-Dateien pro Kategorie sind möglich (z. B. mehrere Depot-Exporte
aus verschiedenen Zeiträumen). Duplikate werden per SHA-256-Hash erkannt und
nicht doppelt eingetragen.

---

## CLI (Einzel-Report)

```bash
pip install -r requirements.txt

# Alles automatisch erkennen (im Ordner mit den CSVs ausführen):
python3 portfolio_analyse.py

# Dateien explizit angeben:
python3 portfolio_analyse.py DEPOT.csv --konto KONTO.csv

# Mit Ausgabeordner / Stichtag:
python3 portfolio_analyse.py -t DEPOT.csv -k KONTO.csv -o report/ --today 2026-06-20
```

| Parameter | Bedeutung |
|-----------|-----------|
| `WERTPAPIER_CSV` (oder `-t`) | Wertpapier-Export (Käufe/Verkäufe/Splits). Ohne Angabe: Auto-Erkennung |
| `-k`, `--konto`, `--cash` | Verrechnungskonto-Export (Dividenden, Ein-/Auszahlungen). Ohne Angabe: Auto-Erkennung |
| `-o`, `--outdir` | Ausgabeordner für Report/CSV/Cache (Standard: aktueller Ordner) |
| `--today` | Stichtag der Bewertung (Standard: heute) |
| `--no-konto` | Kontoexport ignorieren (Dividenden werden dann geschätzt) |

### Ausgabe-Dateien

- **`portfolio_report.html`** – interaktiver Report (im Browser öffnen):
  hover-fähiger Verlaufsgraph, Stacked-Area-Chart, sortierbare Ergebnistabelle,
  Kennzahlen-Karten, „beste Entscheidung / größter Fehler". Braucht Internet (Plotly via CDN).
- `portfolio_entwicklung.png` – statischer Verlaufsgraph + G/V-Kurve.
- `ergebnis_je_aktie.png` – statische Ergebnistabelle je Aktie.
- `positionen.csv` – G/V je Aktie (realisiert/unrealisiert/gesamt, Rendite %, Timing, Dividenden).
- Konsolen-Zusammenfassung inkl. geldgewichteter Jahresrendite (XIRR).

---

## Eingabe: roher Flatex-Export

Funktioniert direkt mit dem vollen Flatex-Export (`...Transactions.csv`). Erkannt
werden alle Buchungsarten:
- **Kauf / Verkauf** → echte Cashflows (Basis für alle G/V-Zahlen).
- **Split / Aufteilung / Reverse-Split / Stockdividende / Lagerstellenwechsel /
  Thesaurierung / Storno** → Kapitalmaßnahmen ohne Cash; ändern Stückzahl/ISIN.
  Splits werden hieraus abgeleitet, ISIN-Wechsel (z. B. vor/nach Split)
  automatisch zu einer Position verschmolzen.

## Wie gerechnet wird

- **Realisierte G/V** exakt aus den Cashflows (Spalte *Betrag*, FIFO) – splitsicher.
- **Marktwert offener Positionen**: aktuelle Yahoo-Kurse, in EUR umgerechnet über
  tägliche Devisenkurse.
- **Aktiensplits**: Yahoo liefert split-*bereinigte* Kurse. Stückzahlen werden auf
  die heutige Split-Basis gebracht – die Splitfaktoren kommen **aus den
  Flatex-Daten** (maßgeblich; fehlt eine Maßnahme, Fallback auf Yahoo-Splits).
- **Sicherheits-Check**: Weicht der aus den Buchungen abgeleitete Splitfaktor stark
  vom Yahoo-Faktor ab (fehlerhafte Kursdaten), wird die Position automatisch zu
  Einstand bewertet und im Report markiert.

## Verrechnungskonto (optional): echte Dividenden + Ein-/Auszahlungen

Liegt zusätzlich der **Kontoumsätze-Export** im Ordner (`;`-getrennt, Spalte
*Zahlungspfl.*), nutzt das Script daraus automatisch:
- **echte Bardividenden** je Aktie (netto, tatsächlich gutgeschrieben) statt der
  Yahoo-Schätzung,
- **Ein-/Auszahlungen**: Überweisungen auf Flatex bzw. auf andere Konten,
- Zinsen und Gebühren.

Ohne Kontoexport fällt die Dividende auf eine **Brutto-Schätzung** zurück
(Yahoo-Ausschüttungshistorie × gehaltene Stück je Ex-Tag, vor Steuern).

## Sonderbewertung einzelner Titel (optional, `overrides.json`)

Für Titel ohne verlässlichen Börsenkurs (delistet, privat, falsch aufgelöst):
```json
{
  "<ISIN>": "zero",
  "<ISIN>": "cost"
}
```
`zero` = Marktwert 0, `cost` = Bewertung zu Einstand. Die Datei bleibt lokal
(per `.gitignore` ausgeschlossen). Alternativ per `--overrides PFAD`.
