# Portfolio-Analyse (Flatex)

Wertet den **rohen Flatex-Transaktionsexport** aus und zeigt die
Gesamtentwicklung des Aktien-Portfolios.

## Installation
```bash
pip install -r requirements.txt        # yfinance, pandas, matplotlib
```
Benötigt Python 3.9+ und eine Internetverbindung (Yahoo Finance). **Kein LLM,
kein API-Key** – die Auswertung ist reine Rechnung.

## Ausführen
```bash
# 1) Alles automatisch erkennen (im Ordner mit den CSVs ausführen):
python3 portfolio_analyse.py

# 2) Dateien explizit angeben:
python3 portfolio_analyse.py DEPOT.csv --konto KONTO.csv

# 3) Mit Ausgabeordner / Stichtag:
python3 portfolio_analyse.py -t DEPOT.csv -k KONTO.csv -o report/ --today 2026-06-20
```

| Parameter | Bedeutung |
|-----------|-----------|
| `WERTPAPIER_CSV` (oder `-t`) | Wertpapier-Export (Käufe/Verkäufe/Splits). Ohne Angabe: Auto-Erkennung |
| `-k`, `--konto`, `--cash` | Verrechnungskonto-Export (Dividenden, Ein-/Auszahlungen). Ohne Angabe: Auto-Erkennung |
| `-o`, `--outdir` | Ausgabeordner für Report/CSV/Cache (Standard: aktueller Ordner) |
| `--today` | Stichtag der Bewertung (Standard: heute) |
| `--no-konto` | Kontoexport ignorieren (Dividenden werden dann geschätzt) |

Ohne Argumente findet das Script die Flatex-CSVs selbst (nimmt den vollständigsten
Wertpapier-Export), löst ISINs zu Börsentickern auf (Yahoo-Such-API, gecached in
`tickers.json`) und holt Kurse/Devisen/Splits von Yahoo Finance (gecached in `cache/`).

## Eingabe: roher Flatex-Export
Funktioniert direkt mit dem vollen Flatex-Export (`...Transactions.csv`). Erkannt
werden alle Buchungsarten:
- **Kauf / Verkauf** → echte Cashflows (Basis für alle G/V-Zahlen).
- **Split / Aufteilung / Reverse-Split / Stockdividende / Lagerstellenwechsel /
  Thesaurierung / Storno** → Kapitalmaßnahmen ohne Cash; ändern Stückzahl/ISIN.
  Splits werden hieraus abgeleitet, ISIN-Wechsel (z. B. vor/nach Split)
  automatisch zu einer Position verschmolzen.

## Ergebnisse
- **`portfolio_report.html`** – interaktiver Report (im Browser öffnen):
  hover-fähiger Verlaufsgraph, Stacked-Area-Chart der Depot-Zusammensetzung je
  Aktie über die Zeit, sortierbare Ergebnistabelle, Kennzahlen-Karten,
  „beste Entscheidung / größter Fehler". Braucht Internet (Plotly via CDN).
- `portfolio_entwicklung.png` – statischer Verlaufsgraph + reine G/V-Kurve.
- `ergebnis_je_aktie.png` – statische Ergebnistabelle je Aktie.
- `positionen.csv` – G/V je Aktie (realisiert/unrealisiert/gesamt, Status,
  Rendite %, Verkauf-Timing, Dividenden).
- Konsolen-Zusammenfassung inkl. geldgewichteter Jahresrendite (XIRR).

## Wie gerechnet wird
- **Realisierte G/V** exakt aus den Cashflows (Spalte *Betrag*, FIFO) – splitsicher.
- **Marktwert offener Positionen**: aktuelle Yahoo-Kurse, in EUR umgerechnet über
  tägliche Devisenkurse.
- **Aktiensplits**: Yahoo liefert split-*bereinigte* Kurse. Stückzahlen werden auf
  die heutige Split-Basis gebracht – die Splitfaktoren kommen **aus den
  Flatex-Daten** (maßgeblich; fehlt eine Maßnahme, Fallback auf Yahoo-Splits).
  Beispiel NVIDIA: korrekt 17 statt 8 Stück.
- **Sicherheits-Check**: Weicht der Flatex-Splitfaktor stark vom Yahoo-Faktor ab
  (z. B. fehlerhafte Yahoo-Daten bei BYD), wird die Position automatisch zu
  Einstand bewertet und im Report markiert.

## Sonderfälle (oben im Script unter `SPECIAL`)
- **InVivo Therapeutics** (US46186M5067): delistet/wertlos → Wert 0.
- **SpaceX** (US84615Q1031): privat, kein Börsenkurs → zu Einstand bewertet.
- **BYD**: wird durch den Split-Check automatisch erkannt (Yahoo 18× vs. real 3×)
  und zu Einstand bewertet – kein manueller Eingriff nötig.

## Verrechnungskonto (optional): echte Dividenden + Ein-/Auszahlungen
Liegt zusätzlich der **Kontoumsätze-Export** im Ordner (`;`-getrennt, Spalte
*Zahlungspfl.*), nutzt das Script daraus automatisch:
- **echte Bardividenden** je Aktie (netto, tatsächlich gutgeschrieben) statt der
  Yahoo-Schätzung,
- **Ein-/Auszahlungen**: Überweisungen auf Flatex bzw. auf andere Konten,
- Zinsen und Gebühren.

Keine Doppelzählung: Die Kauf/Verkauf-Cashlegs im Kontoexport werden ignoriert
(stehen schon im Wertpapierexport); die „Dividende/Ausschüttung"-Zeilen im
Wertpapierexport sind **Stockdividenden/Wiederanlagen** (Stückzahl, kein Cash)
und damit etwas anderes als die Bargutschriften im Kontoexport.

Ohne Kontoexport fällt die Dividende auf eine **Brutto-Schätzung** zurück
(Yahoo-Ausschüttungshistorie × gehaltene Stück je Ex-Tag, vor Steuern).

## Aktualisieren
Kurse liegen im Cache. Für frische Kurse `cache/` löschen und neu starten. Neue
Trades einfach exportieren – das Script nimmt automatisch den neuesten/vollsten
Flatex-Export im Ordner.
