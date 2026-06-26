#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Portfolio-Auswertung aus dem ROHEN Flatex-Transaktionsexport.

Einfach starten - keine Konfiguration, kein LLM:
    python3 portfolio_analyse.py
Das Script sucht selbst die Flatex-CSV im Ordner, loest ISINs zu Tickern auf
(Yahoo-Such-API, gecached) und holt Kurse/Devisen/Splits von Yahoo Finance.

Erzeugt:
  - portfolio_report.html     : interaktiver Report (Charts, Tabelle)
  - portfolio_entwicklung.png : statischer Verlaufsgraph
  - ergebnis_je_aktie.png     : statische Ergebnistabelle
  - positionen.csv            : Gewinn/Verlust je Position
  - Konsolen-Zusammenfassung

Verarbeitet den vollen Export inkl. Splits, ISIN-Wechseln, Stockdividenden,
Thesaurierungen und Storni:
  * Nur "Kauf"/"Verkauf" sind echte Cashflows; alle anderen Buchungen sind
    Kapitalmassnahmen (kein Cash, aendern aber Stueckzahl/ISIN).
  * Aktiensplits werden AUS DEN FLATEX-DATEN abgeleitet (massgeblich) und auf
    die heutige Split-Basis umgerechnet, passend zu Yahoos split-bereinigten
    Kursen. Bei Abweichung Flatex<->Yahoo wird die Position zur Sicherheit zu
    Einstand bewertet.
  * ISINs, die per Kapitalmassnahme zusammenhaengen (z.B. vor/nach Split),
    werden zu einer Position verschmolzen.
"""
import csv, json, os, time, sys, glob, re, argparse
from datetime import datetime
from collections import defaultdict
import pandas as pd

# ---------------------------------------------------------------- Konfiguration
TODAY    = pd.Timestamp.today().normalize()   # Stichtag (per --today ueberschreibbar)
CACHE    = "cache"
OUT_PNG  = "portfolio_entwicklung.png"
OUT_CSV  = "positionen.csv"
OUT_HTML = "portfolio_report.html"
BENCH_TICKER = "IWRD.L"          # MSCI World ETF fuer Vergleichslinie
SPLIT_TOL = 0.05                 # Toleranz Flatex- vs Yahoo-Splitfaktor

# Sonderbewertung einzelner Titel (z.B. delistet -> Wert 0, kein Kurs -> zu Einstand)
# kommt aus einer optionalen, NICHT versionierten overrides.json (siehe load_overrides).

# Waehrung -> benoetigtes Yahoo-FX-Paar (EUR pro 1 Einheit Fremdwaehrung)
FX_PAIRS = {"USD":"EURUSD=X","GBP":"EURGBP=X","HKD":"EURHKD=X",
            "DKK":"EURDKK=X","CAD":"EURCAD=X","CHF":"EURCHF=X","JPY":"EURJPY=X"}

# ---------------------------------------------------------------- Hilfsfunktionen
def de_num(s):
    """deutsches Zahlformat '1.234,56' -> 1234.56"""
    s = s.strip()
    if not s:
        return 0.0
    return float(s.replace(".", "").replace(",", "."))

def xirr(cashflows):
    """Geldgewichtete Jahresrendite. cashflows: Liste (datum, betrag),
    betrag<0 = Geld raus (Kauf), betrag>0 = Geld rein (Verkauf/Endwert)."""
    cf = sorted(cashflows, key=lambda x: x[0])
    t0 = cf[0][0]
    years = [(d - t0).days / 365.0 for d, _ in cf]
    amts  = [a for _, a in cf]
    def npv(r):
        return sum(a / (1 + r) ** y for a, y in zip(amts, years))
    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-6:
            return mid
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


import urllib.request

def load_overrides(path=None):
    """Optionale, NICHT versionierte Sonderbewertung je ISIN (overrides.json):
        {"<ISIN>": "zero"}  -> Marktwert 0   (z.B. delistet/wertlos)
        {"<ISIN>": "cost"}  -> zu Einstand   (z.B. kein verlaesslicher Boersenkurs)
    Haelt persoenliche Bestandsdetails aus dem (oeffentlichen) Repo heraus."""
    fp = path or "overrides.json"
    if os.path.exists(fp):
        try:
            data = json.load(open(fp, encoding="utf-8"))
            return {k: v for k, v in data.items() if v in ("zero", "cost")}
        except Exception as e:
            sys.stderr.write(f"   {fp} ignoriert ({e})\n")
    return {}

def _yahoo_search_isin(isin):
    """ISIN -> Yahoo-Ticker ueber die oeffentliche Such-API (kein LLM, kein Key)."""
    url = (f"https://query2.finance.yahoo.com/v1/finance/search?q={isin}"
           f"&quotesCount=4&newsCount=0")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for _ in range(3):
        try:
            d = json.load(urllib.request.urlopen(req, timeout=12))
            q = d.get("quotes", [])
            if q:
                return q[0].get("symbol")
            return None
        except Exception:
            time.sleep(2)
    return None

def resolve_tickers(isins):
    """ISIN -> Ticker fuer alle ISINs; Ergebnis wird in tickers.json gecached."""
    fp = os.path.join(CACHE, "tickers.json")
    cache = {}
    if os.path.exists(fp):
        raw = json.load(open(fp, encoding="utf-8"))
        for k, v in raw.items():                       # altes & neues Format unterstuetzen
            cache[k] = v.get("ticker") if isinstance(v, dict) else v
    changed = False
    for isin in isins:
        if isin not in cache:
            sys.stderr.write(f"   ISIN-Aufloesung: {isin} ...\n")
            cache[isin] = _yahoo_search_isin(isin)
            changed = True
            time.sleep(0.4)
    if changed:
        json.dump(cache, open(fp, "w"), indent=1, ensure_ascii=False)
    return {k: v for k, v in cache.items() if v}

# ---------------------------------------------------------------- yfinance Layer
import yfinance as yf

def _retry(fn, tries=4, wait=3):
    for i in range(tries):
        try:
            r = fn()
            if r is not None and (not hasattr(r, "empty") or not r.empty):
                return r
        except Exception as e:
            sys.stderr.write(f"   retry ({e})\n")
        time.sleep(wait * (i + 1))
    return None

def get_hist(ticker):
    """Taegliche (split-bereinigte) Schlusskurse in Heimatwaehrung; gecached."""
    fp = os.path.join(CACHE, f"hist_{ticker.replace('/','_')}.csv")
    if os.path.exists(fp):
        s = pd.read_csv(fp, index_col=0, parse_dates=True).iloc[:, 0]
        s.name = ticker
        # Inkrementell aktualisieren wenn Cache älter als heute
        if not s.empty and pd.Timestamp(s.index[-1]).date() < TODAY.date():
            start = (pd.Timestamp(s.index[-1]) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
            end   = (TODAY + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            upd = _retry(lambda: yf.download(ticker, start=start, end=end,
                                              auto_adjust=False, progress=False, threads=False))
            if upd is not None and not upd.empty:
                c2 = upd["Close"]
                if isinstance(c2, pd.DataFrame):
                    c2 = c2.iloc[:, 0]
                c2.index = pd.to_datetime(c2.index).tz_localize(None)
                c2 = c2.dropna()
                s = pd.concat([s[s.index < c2.index[0]], c2]).sort_index()
                s.to_frame("close").to_csv(fp)
                time.sleep(0.3)
        return s
    df = _retry(lambda: yf.download(ticker, start="2020-12-01",
                                    end=(TODAY + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                                    auto_adjust=False, progress=False, threads=False))
    if df is None or df.empty:
        return None
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None)
    close = close.dropna()
    close.to_frame("close").to_csv(fp)
    close.name = ticker
    time.sleep(0.3)
    return close

def get_splits(ticker):
    """Yahoo-Splits (nur fuer den Abgleich mit den Flatex-Splits)."""
    fp = os.path.join(CACHE, f"splits_{ticker.replace('/','_')}.json")
    if os.path.exists(fp):
        d = json.load(open(fp))
        return {pd.Timestamp(k): v for k, v in d.items()}
    try:
        sp = yf.Ticker(ticker).splits
    except Exception:
        sp = None
    out = {}
    if sp is not None and len(sp):
        sp.index = pd.to_datetime(sp.index).tz_localize(None).normalize()
        out = {ts: float(r) for ts, r in sp.items()}
    json.dump({k.strftime("%Y-%m-%d"): v for k, v in out.items()}, open(fp, "w"))
    return out

def get_dividends(ticker):
    """Ausschüttungen je Ex-Tag (split-bereinigt, in Heimatwährung); gecached."""
    fp = os.path.join(CACHE, f"divs_{ticker.replace('/','_')}.json")
    if os.path.exists(fp):
        d = json.load(open(fp))
        return {pd.Timestamp(k): v for k, v in d.items()}
    try:
        dv = yf.Ticker(ticker).dividends
    except Exception:
        dv = None
    out = {}
    if dv is not None and len(dv):
        dv.index = pd.to_datetime(dv.index).tz_localize(None).normalize()
        out = {ts: float(v) for ts, v in dv.items() if v > 0}
    json.dump({k.strftime("%Y-%m-%d"): v for k, v in out.items()}, open(fp, "w"))
    time.sleep(0.15)
    return out


def get_sector(ticker):
    """Sektor/Kategorie eines Titels von Yahoo Finance, gecached."""
    fp = os.path.join(CACHE, "sectors.json")
    cache = json.load(open(fp, encoding="utf-8")) if os.path.exists(fp) else {}
    if ticker in cache:
        return cache[ticker]
    sector = None
    try:
        info = yf.Ticker(ticker).info
        sector = info.get("sector") or info.get("category")
    except Exception:
        pass
    cache[ticker] = sector
    json.dump(cache, open(fp, "w"), ensure_ascii=False)
    time.sleep(0.2)
    return sector


def get_currency(ticker):
    fp = os.path.join(CACHE, "currency.json")
    cur = json.load(open(fp)) if os.path.exists(fp) else {}
    if ticker in cur:
        return cur[ticker]
    c = None
    try:
        c = yf.Ticker(ticker).fast_info.currency
    except Exception:
        pass
    cur[ticker] = c
    json.dump(cur, open(fp, "w"))
    time.sleep(0.2)
    return c

_FX_CACHE = {}
def fx_series(ccy, index):
    """Faktor-Serie: Fremdwaehrung -> EUR, auf 'index' reindiziert."""
    if ccy in ("EUR", None):
        return pd.Series(1.0, index=index)
    if ccy == "GBp":           # Pence
        base = fx_series("GBP", index)
        return base / 100.0
    if ccy not in _FX_CACHE:
        pair = FX_PAIRS.get(ccy)
        s = get_hist(pair) if pair else None
        if s is None:
            sys.stderr.write(f"   WARN: kein FX fuer {ccy}, nutze 1.0\n")
            _FX_CACHE[ccy] = pd.Series(1.0, index=index)
        else:
            _FX_CACHE[ccy] = (1.0 / s).reindex(index).ffill().bfill()
    return _FX_CACHE[ccy].reindex(index).ffill().bfill()

def price_eur_series(ticker, index):
    raw = get_hist(ticker)
    if raw is None:
        return None
    ccy = get_currency(ticker) or "USD"
    px = raw.reindex(index).ffill()
    return px * fx_series(ccy, index)

# ---------------------------------------------------------------- Transaktionen
ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}[0-9])\b")

def find_flatex_csv():
    """Findet die Flatex-Wertpapier-CSV im Ordner (Header mit Buchungstag/ISIN/
    Nominal); bei mehreren die mit den meisten Zeilen. Kodierungs-unabhaengig."""
    best, best_rows = None, -1
    for fp in glob.glob("*.csv"):
        try:
            enc = _detect_enc(fp)
            with open(fp, encoding=enc) as f:
                head = f.readline()
                if not all(k in head for k in ("Buchungstag", "ISIN", "Nominal")):
                    continue
                n = sum(1 for ln in f if ln.strip())
            if n > best_rows:
                best, best_rows = fp, n
        except Exception:
            continue
    if not best:
        sys.exit("Keine Flatex-Wertpapier-CSV gefunden (Header mit Buchungstag/ISIN/Nominal).")
    return best

def classify(info):
    """Transaktionsart aus der Buchungsinformation."""
    low = info.lower()
    if "verkauf" in low:
        return "sell"
    if "kauf" in low:
        return "buy"
    return "corp"            # Split/Aufteilung/Stockdividende/Thesaurierung/Storno/Aussch.

def load_transactions(csv_path):
    """Liest ALLE Buchungen; klassifiziert in buy/sell/corp; liest Ziel-ISIN
    von Kapitalmassnahmen ('... in ISIN XYZ') aus. Robust gegen Kodierung,
    Trennzeichen und Spaltenreihenfolge."""
    rows = _read_table(csv_path)
    if not rows:
        sys.exit(f"Leere Datei: {csv_path}")
    cm = _colmap(rows[0])
    need = [k for k in ("date", "isin", "qty", "betrag", "info") if cm[k] is None]
    if need:
        sys.exit(f"Im Wertpapier-Export fehlen Spalten {need}.\nHeader war: {rows[0]}")
    maxi = max(v for v in cm.values() if v is not None)
    txns = []
    for ln in rows[1:]:
        if not ln or len(ln) <= maxi or not ln[cm["date"]].strip():
            continue
        info = ln[cm["info"]]
        kind = classify(info)
        isin = ln[cm["isin"]].strip()
        ref = None
        if kind == "corp":
            hits = [m for m in ISIN_RE.findall(info) if m != isin]
            ref = hits[0] if hits else None            # Ziel-ISIN bei ISIN-Wechsel
        txns.append(dict(
            date=datetime.strptime(ln[cm["date"]].strip(), "%d.%m.%Y"),
            isin=isin, name=ln[cm["name"]].strip() if cm["name"] is not None else isin,
            kind=kind, qty=de_num(ln[cm["qty"]]), betrag=de_num(ln[cm["betrag"]]),
            kurs=de_num(ln[cm["kurs"]]) if cm["kurs"] is not None else 0.0,
            ref_isin=ref))
    txns.sort(key=lambda t: (t["date"], 0 if t["kind"] == "corp" else 1))
    return txns

def _detect_enc(path):
    """Kodierung erkennen: utf-8 ZUERST (schlaegt bei cp1252 sauber fehl),
    sonst cp1252 (typischer Flatex-Rohexport), zuletzt latin-1."""
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                f.read()
            return enc
        except Exception:
            continue
    return "latin-1"

def _read_table(path):
    """Liest eine Flatex-CSV robust: erkennt Kodierung und Trennzeichen (',' oder
    ';') selbst. Liefert die Zeilen als Listen."""
    enc = _detect_enc(path)
    with open(path, encoding=enc, newline="") as f:
        header = f.readline()
        delim = ";" if header.count(";") > header.count(",") else ","
        f.seek(0)
        return [row for row in csv.reader(f, delimiter=delim)]

def _colmap(header):
    """Ordnet benoetigte Spalten ueber ihre Header-Namen den Indizes zu
    (robust gegen Spaltenreihenfolge/Leerspalten/Namensvarianten)."""
    idx = [(h.strip().lower(), i) for i, h in enumerate(header)]
    def col(*prefixes):
        for pre in prefixes:
            for name, i in idx:
                if name.startswith(pre):
                    return i
        return None
    return {"date": col("buchungstag"), "isin": col("isin"),
            "name": col("bezeichnung"), "qty": col("nominal"),
            "betrag": col("betrag"), "kurs": col("kurs"),
            "info": col("buchungsinformation"),
            "pfl": col("zahlungspfl", "gegenkonto", "auftraggeber")}

def find_cash_csv():
    """Findet den Flatex-Verrechnungskonto-Export im aktuellen Ordner
    (Spalte 'Zahlungspfl.'). Liefert Pfad oder None. Kodierungs-unabhaengig."""
    for fp in glob.glob("*.csv"):
        try:
            head = open(fp, encoding=_detect_enc(fp)).readline()
        except Exception:
            continue
        if "Zahlungspfl" in head:
            return fp
    return None

def load_cash_account(path):
    """Wertet die Kontoumsaetze aus: echte Bardividenden je ISIN, Ein-/Auszahlungen
    (externe Ueberweisungen mit Gegenkonto), Zinsen, Gebuehren, Steuern.
    Kauf/Verkauf-Cashlegs werden ignoriert (stehen schon im Wertpapierexport).
    Robust gegen Kodierung, Trennzeichen und Spaltenreihenfolge."""
    rows = _read_table(path)
    if not rows:
        return None
    cm = _colmap(rows[0])
    if cm["info"] is None or cm["betrag"] is None:
        sys.stderr.write(f"   Konto-Export unlesbar (Header: {rows[0]}) – wird ignoriert\n")
        return None
    ci, cb, cp, cd = cm["info"], cm["betrag"], cm["pfl"], cm["date"]
    maxi = max(x for x in (ci, cb, cp, cd) if x is not None)
    div = defaultdict(float)
    div_monthly: dict = defaultdict(float)   # YYYY-MM -> tatsächlich erhaltene EUR-Dividenden
    deposits = withdrawals = interest = fees = taxes = 0.0
    for ln in rows[1:]:
        if not ln or len(ln) <= maxi:
            continue
        info = ln[ci]
        betrag = de_num(ln[cb])
        pfl = ln[cp].strip() if cp is not None else ""
        low = info.lower()
        if "dividend" in low or low.startswith("ertr"):       # Dividende/Ausschuettung
            m = ISIN_RE.search(info)
            div[m.group(1) if m else "?"] += betrag
            # Monatliche Aufschlüsselung für den Verlaufschart
            if cd is not None and len(ln) > cd and ln[cd].strip():
                try:
                    dt_str = ln[cd].strip()
                    dt = datetime.strptime(dt_str, "%d.%m.%Y")
                    div_monthly[dt.strftime("%Y-%m")] += betrag
                except ValueError:
                    pass
        elif "order" in low and ("kauf" in low or "verkauf" in low):
            continue                                          # Cashleg -> aus Wertpapierexport
        elif pfl:                                             # externe Ueberweisung
            if betrag > 0:
                deposits += betrag
            else:
                withdrawals += -betrag
        elif "zins" in low:
            interest += betrag
        elif "steuer" in low or low.startswith("storno"):
            taxes += betrag
        elif "geb" in low and "hr" in low:                    # Gebuehr (umlaut-tolerant)
            fees += betrag
    return dict(div=dict(div), total_div=sum(div.values()),
                div_monthly=dict(div_monthly),
                deposits=deposits, withdrawals=withdrawals,
                interest=interest, fees=fees, taxes=taxes)

def build_groups(txns):
    """Verschmilzt ISINs, die per Kapitalmassnahme zusammenhaengen (Union-Find).
    Liefert: isin -> group_id, und je Gruppe die zugehoerigen ISINs."""
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        parent[find(a)] = find(b)
    for t in txns:
        find(t["isin"])
        if t["ref_isin"]:
            union(t["isin"], t["ref_isin"])
    groups = defaultdict(list)
    for isin in list(parent):
        groups[find(isin)].append(isin)
    return {isin: find(isin) for isin in parent}, groups

def flatex_splits(group_txns):
    """Leitet Splitfaktoren je Kapitalmassnahmen-Datum aus den Bestandssspruengen
    ab: factor = Bestand_nachher / Bestand_vorher (deckt Split, Reverse-Split,
    Bonus/Stockdividende, ISIN-Wechsel und Storni einheitlich ab)."""
    tx = sorted(group_txns, key=lambda t: t["date"])
    corp_dates = sorted({t["date"] for t in tx if t["kind"] == "corp"})
    splits = {}
    for d in corp_dates:
        before = sum(t["qty"] for t in tx if t["date"] < d)
        dn = sum(t["qty"] for t in tx if t["date"] == d and t["kind"] == "corp")
        if before > 1e-9 and abs(dn) > 1e-9:
            factor = (before + dn) / before
            if factor > 1e-9 and abs(factor - 1) > 1e-6:
                splits[pd.Timestamp(d)] = factor
    return splits

def split_mismatch(ins):
    """True, wenn der aus Flatex abgeleitete Gesamt-Splitfaktor vom Yahoo-Faktor
    im Zeitraum [erster Kauf .. heute] abweicht. Yahoo-Kurse sind IMMER auf die
    heutige Basis bereinigt; passt das nicht zur Flatex-Stueckbasis (eigener
    Split, der nicht gehalten wurde, oder fehlerhafte Yahoo-Daten), sind die
    historischen Kurse unbrauchbar -> Position zu Einstand bewerten."""
    if not ins["txns"] or not ins.get("ticker"):
        return False
    sf = 1.0
    for f in ins["splits"].values():
        sf *= f
    first = pd.Timestamp(min(t["date"] for t in ins["txns"]))
    sy = 1.0
    for sd, r in (get_splits(ins["ticker"]) or {}).items():
        if first < sd <= TODAY:
            sy *= r
    return sy <= 0 or abs(sf / sy - 1) > SPLIT_TOL

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="portfolio_analyse.py", allow_abbrev=False,
        description="Portfolio-Auswertung aus rohen Flatex-Exporten "
                    "(ohne Konfiguration, ohne LLM).",
        epilog="Beispiele:\n"
               "  python3 portfolio_analyse.py            # alles automatisch erkennen\n"
               "  python3 portfolio_analyse.py depot.csv --konto konto.csv\n"
               "  python3 portfolio_analyse.py -t depot.csv -k konto.csv -o report/\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("transactions", nargs="?", metavar="WERTPAPIER_CSV",
                   help="Wertpapier-Export (Käufe/Verkäufe/Splits). "
                        "Ohne Angabe: Auto-Erkennung im aktuellen Ordner.")
    p.add_argument("-t", "--transactions", dest="transactions_opt", metavar="CSV",
                   help="Alternative zur Positionsangabe des Wertpapier-Exports.")
    p.add_argument("-k", "--konto", "--cash", dest="cash", metavar="CSV",
                   help="Verrechnungskonto-Export (Dividenden, Ein-/Auszahlungen). "
                        "Ohne Angabe: Auto-Erkennung.")
    p.add_argument("-o", "--outdir", default=".", metavar="ORDNER",
                   help="Ausgabeordner für Report/CSV/Cache (Standard: aktueller Ordner).")
    p.add_argument("--today", metavar="JJJJ-MM-TT",
                   help="Stichtag der Bewertung (Standard: heute).")
    p.add_argument("--no-konto", "--no-cash", dest="no_cash", action="store_true",
                   help="Kontoexport ignorieren; Dividenden werden dann geschätzt.")
    p.add_argument("--overrides", metavar="JSON",
                   help='Sonderbewertung je ISIN ({"ISIN":"zero|cost"}). '
                        "Standard: overrides.json im aktuellen Ordner, falls vorhanden.")
    a = p.parse_args(argv)
    a.transactions = a.transactions or a.transactions_opt
    return a

def compute_payload(tx_path, cash_path=None, cache_dir=None, today=None,
                    overrides_path=None, log=None):
    """Führt die vollständige Portfolio-Analyse durch und gibt (payload, ctx) zurück.
    payload: Report-Datenstruktur (für HTML-Export und Flask-Server).
    ctx:     interne Berechnungsergebnisse für main() (Konsole, CSV, PNG).
    log:     optionaler Callback(str) für Fortschrittsmeldungen."""
    _log = log if callable(log) else (lambda m: None)
    global TODAY, CACHE
    TODAY = pd.Timestamp(today).normalize() if today else pd.Timestamp.today().normalize()
    if cache_dir:
        CACHE = cache_dir
    os.makedirs(CACHE, exist_ok=True)

    _log("Transaktionen laden …")
    special_map = load_overrides(overrides_path)
    txns = load_transactions(tx_path)
    _log(f"  {len(txns)} Buchungen geladen")

    isins = {t["isin"] for t in txns} | {t["ref_isin"] for t in txns if t["ref_isin"]}
    _log(f"Ticker auflösen ({len(isins)} ISINs) …")
    isin2ticker = resolve_tickers(isins)
    bdays = pd.bdate_range(min(t["date"] for t in txns), TODAY)

    isin2grp, groups = build_groups(txns)
    instruments = {}
    for gid, isins in groups.items():
        gtx = sorted((t for t in txns if isin2grp[t["isin"]] == gid),
                     key=lambda t: t["date"])
        canon = gtx[-1]["isin"]
        special = next((special_map[i] for i in isins if i in special_map), None)
        name = next((t["name"] for t in reversed(gtx) if t["kind"] != "corp"),
                    gtx[-1]["name"])
        instruments[gid] = {"name": name, "ticker": isin2ticker.get(canon),
                            "isin": canon,
                            "special": special, "isins": isins,
                            "txns": [t for t in gtx if t["kind"] in ("buy", "sell")],
                            "all_txns": gtx}

    _log(f"Splits ableiten ({len(instruments)} Positionen) …")
    for gid, ins in instruments.items():
        ins["splits"] = flatex_splits(ins["all_txns"])
        if not ins["splits"] and ins.get("ticker") and not ins["special"] and ins["txns"]:
            first = pd.Timestamp(min(t["date"] for t in ins["txns"]))
            ins["splits"] = {d: r for d, r in get_splits(ins["ticker"]).items()
                             if d > first}
        for t in ins["txns"]:
            factor = 1.0
            for sdate, ratio in ins["splits"].items():
                if sdate > pd.Timestamp(t["date"]):
                    factor *= ratio
            t["adj_qty"] = t["qty"] * factor

    incomplete = []
    incomplete_isins = set()
    for gid, ins in instruments.items():
        ba = sum(t["adj_qty"] for t in ins["txns"] if t["adj_qty"] > 0)
        sa = sum(-t["adj_qty"] for t in ins["txns"] if t["adj_qty"] < 0)
        ins["incomplete"] = sa > ba + 1e-6
        if ins["incomplete"]:
            incomplete.append(ins["name"])
            incomplete_isins.update(ins["isins"])

    if cash_path:
        _log("Verrechnungskonto laden (Dividenden) …")
    cash = load_cash_account(cash_path) if cash_path else None
    cash_div = cash["div"] if cash else None

    portfolio_value = pd.Series(0.0, index=bdays)
    inst_values = {}
    rows = []
    pos_txns = {}
    missing = []
    val_ok = val_bad = 0
    active_ins = [ins for ins in instruments.values() if not ins["incomplete"]]
    _log(f"Kurse & Marktwerte abrufen ({len(active_ins)} Titel) …")
    realized_by_year: dict = {}
    currency_vals: dict = defaultdict(float)
    sector_vals: dict = defaultdict(float)

    from collections import deque
    for key, ins in sorted(instruments.items(), key=lambda x: x[1]["name"]):
        if ins["incomplete"]:
            continue
        tx = sorted(ins["txns"], key=lambda t: t["date"])
        buys  = sum(t["betrag"] for t in tx if t["betrag"] > 0)
        sells = sum(-t["betrag"] for t in tx if t["betrag"] < 0)

        buy_adj = sum(t["adj_qty"] for t in tx if t["adj_qty"] > 0)

        lots = deque()
        realized = 0.0
        for t in tx:
            if t["adj_qty"] > 0:
                t["_rem"] = t["adj_qty"]       # Restbestand dieses Kaufs (FIFO reduziert ihn)
                lots.append([t["adj_qty"], t["betrag"] / t["adj_qty"], t])
            elif t["adj_qty"] < 0:
                sell_rest = -t["adj_qty"]
                price_per = (-t["betrag"]) / sell_rest
                t_real = t_cost = 0.0          # realisierter G/V + Einstand dieses Verkaufs
                while sell_rest > 1e-9 and lots:
                    lot = lots[0]
                    take = min(sell_rest, lot[0])
                    t_real += take * (price_per - lot[1])
                    t_cost += take * lot[1]
                    lot[0] -= take; sell_rest -= take
                    lot[2]["_rem"] = lot[0]    # noch gehaltene Stücke des Ursprungskaufs
                    if lot[0] <= 1e-9:
                        # Datum merken, an dem dieser Kauf vollständig (per FIFO) verkauft war
                        lot[2]["_sold_out"] = pd.Timestamp(t["date"]).strftime("%d.%m.%Y")
                        lots.popleft()
                realized += t_real
                t["_real"], t["_cost"] = t_real, t_cost
        remaining = sum(l[0] for l in lots)
        cost_rest = sum(l[0] * l[1] for l in lots)
        cost_price = (buys / buy_adj) if buy_adj > 1e-9 else 0.0
        avg_cost   = (cost_rest / remaining) if remaining > 1e-9 else cost_price

        hold = pd.Series(0.0, index=bdays)
        for t in tx:
            hold.loc[pd.Timestamp(t["date"]):] += t["adj_qty"]

        if ins["special"] is None and split_mismatch(ins):
            ins["special"] = "cost"
            missing.append(f"{ins['name']} (Split Flatex≠Yahoo)")

        pe = None
        if ins["special"] == "zero":
            val = pd.Series(0.0, index=bdays)
        elif ins["special"] == "cost":
            val = hold * cost_price
        elif not ins.get("ticker"):
            val = hold * cost_price
            missing.append(ins["name"])
        else:
            pe = price_eur_series(ins["ticker"], bdays)
            if pe is None:
                val = hold * cost_price
                missing.append(f"{ins['name']} ({ins['ticker']})")
            else:
                val = hold * pe

        if cash_div is not None:
            div_total = sum(cash_div.get(i, 0.0) for i in ins["isins"])
        else:
            div_total = 0.0
            tk = ins.get("ticker")
            if tk and not ins["special"]:
                ccy = get_currency(tk) or "USD"
                fxf = fx_series(ccy, bdays)
                for dt, dps in get_dividends(tk).items():
                    if bdays[0] <= dt <= bdays[-1]:
                        sh = hold.asof(dt)
                        if pd.notna(sh) and sh > 1e-9:
                            fv = fxf.asof(dt)
                            div_total += sh * dps * (fv if pd.notna(fv) else 1.0)

        if pe is not None:
            splits = ins["splits"]
            for t in tx:
                if t["qty"] > 0 and t["kurs"] > 0:
                    f = 1.0
                    for sdate, ratio in splits.items():
                        if sdate > pd.Timestamp(t["date"]):
                            f *= ratio
                    d = pd.Timestamp(t["date"])
                    model = pe.reindex([d]).ffill().iloc[0]
                    if pd.notna(model) and model > 0:
                        recon = model * f
                        if abs(recon / t["kurs"] - 1) < 0.30:
                            val_ok += 1
                        else:
                            val_bad += 1

        val = val.fillna(0.0)
        portfolio_value += val
        inst_values[ins["name"]] = val
        cur_val = float(val.iloc[-1])
        unreal = cur_val - cost_rest
        if ins["special"] == "zero":
            cur_px = 0.0
            price_known = True
        elif ins["special"] == "cost" or pe is None:
            cur_px = cost_price
            price_known = False
        else:
            cur_px = float(pe.iloc[-1])
            price_known = True

        # --- Neue Position-Metriken ---
        # Haltedauer (Tage seit erstem Kauf, nur für noch gehaltene Positionen)
        buy_dates = [pd.Timestamp(t["date"]) for t in tx if t["adj_qty"] > 0]
        first_buy = min(buy_dates) if buy_dates else None
        holding_days = int((TODAY - first_buy).days) if (first_buy and remaining > 0.01) else 0

        # Sektor (gecached, Yahoo info)
        pos_sector = None
        if ins.get("ticker") and price_known:
            pos_sector = get_sector(ins["ticker"])

        # Handelswährung (GBp → GBP normalisiert)
        pos_ccy = "EUR"
        if ins.get("ticker"):
            raw_ccy = get_currency(ins["ticker"]) or "EUR"
            pos_ccy = "GBP" if raw_ccy == "GBp" else raw_ccy

        # Dividendenrendite (letzte 12 Monate, annualisiert, in EUR)
        ttm_div = 0.0
        if price_known and ins.get("ticker") and not ins["special"] and cur_val > 1.0:
            one_yr_ago = TODAY - pd.Timedelta(days=365)
            fxf_div = fx_series(pos_ccy, bdays)
            for dt, dps in get_dividends(ins["ticker"]).items():
                if one_yr_ago <= dt <= TODAY:
                    sh = hold.asof(dt)
                    if pd.notna(sh) and sh > 1e-9:
                        fv = float(fxf_div.asof(dt)) if pd.notna(fxf_div.asof(dt)) else 1.0
                        ttm_div += sh * dps * fv
        div_yield_pos = round(ttm_div / cur_val * 100, 2) if (cur_val > 1.0 and ttm_div > 0) else 0.0

        # Max. Drawdown dieser Position
        pos_peak = val.cummax()
        dd_pos = (val - pos_peak) / pos_peak.where(pos_peak > 1.0)
        max_dd_pos = round(float(dd_pos.min()) * 100, 1) if dd_pos.notna().any() else 0.0

        # Währungs- und Sektorakkumulation
        currency_vals[pos_ccy] += cur_val
        if pos_sector:
            sector_vals[pos_sector] += cur_val

        # Realisierter G/V je Kalenderjahr (für Steuerübersicht)
        for t in tx:
            if t["adj_qty"] < 0 and "_real" in t:
                yr = pd.Timestamp(t["date"]).year
                realized_by_year[yr] = realized_by_year.get(yr, 0.0) + t["_real"]

        # Kauf-/Verkauf-Historie je Position inkl. Performance je Einzeltransaktion.
        # FIFO: älteste Stücke werden zuerst verkauft.
        #   Kauf    -> dev_*:  Wertentwicklung dieses Einstands bis heute (wenn gehalten)
        #   Verkauf -> real_*: tatsächlich realisierter G/V dieses Verkaufs (FIFO)
        #              dev_*:  was die verkauften Stücke heute wert wären vs. Erlös
        # dev_* nur, wenn ein echter Marktkurs vorliegt (sonst N/A -> "–").
        txd = []
        for t in tx:
            rec = {"d": pd.Timestamp(t["date"]).strftime("%d.%m.%Y"),
                   "k": "Kauf" if t["kind"] == "buy" else "Verkauf",
                   "q": round(abs(t["qty"]), 4), "p": round(t["kurs"], 2),
                   "a": round(t["betrag"], 2)}
            if t["adj_qty"] > 0:                              # Kauf
                rem = t.get("_rem", 0.0)                      # nur noch gehaltene Stücke (FIFO)
                adj = t["adj_qty"]
                rec["rem_q"] = round(rec["q"] * (rem / adj), 4) if adj > 1e-12 else 0.0
                if rem <= 1e-9 and t.get("_sold_out"):        # später vollständig verkauft
                    rec["sold_date"] = t["_sold_out"]
                cost_per = t["betrag"] / t["adj_qty"]
                if price_known and rem > 1e-9:
                    cost_rem = rem * cost_per
                    now_val = cur_px * rem
                    rec["dev_abs"] = round(now_val - cost_rem, 2)
                    rec["dev_pct"] = round((now_val / cost_rem - 1) * 100, 2)
            elif t["adj_qty"] < 0:                            # Verkauf
                proceeds = -t["betrag"]
                cb = t.get("_cost", 0.0)
                if cb > 1e-9:
                    rec["real_abs"] = round(t.get("_real", 0.0), 2)
                    rec["real_pct"] = round(t["_real"] / cb * 100, 2)
                if price_known and proceeds > 1e-9:
                    now_val = cur_px * (-t["adj_qty"])
                    rec["dev_abs"] = round(now_val - proceeds, 2)
                    rec["dev_pct"] = round((now_val / proceeds - 1) * 100, 2)
            txd.append(rec)
        pos_txns[ins["name"]] = txd

        sold_adj = sum(-t["adj_qty"] for t in tx if t["adj_qty"] < 0)
        rows.append(dict(name=ins["name"], ticker=ins.get("ticker") or "-",
                         isin=ins.get("isin") or "-",
                         currency=pos_ccy, sector=pos_sector or "",
                         first_buy=first_buy.strftime("%Y-%m-%d") if first_buy else "",
                         holding_days=holding_days, div_yield=div_yield_pos,
                         max_dd=max_dd_pos,
                         shares_now=round(remaining, 4), avg_cost=round(avg_cost, 2),
                         buys=round(buys, 2), sells=round(sells, 2),
                         cur_value=round(cur_val, 2), dividends=round(div_total, 2),
                         realized=round(realized, 2), unrealized=round(unreal, 2),
                         total=round(realized + unreal, 2),
                         sold_adj=round(sold_adj, 4), cur_px=round(cur_px, 4),
                         special=ins["special"] or ""))

    _log(f"  {len(rows)} Positionen bewertet, {len(missing)} ohne Marktdaten")
    cash_txns = [t for t in txns if t["kind"] in ("buy", "sell")
                 and t["isin"] not in incomplete_isins]

    _log("XIRR & Benchmark berechnen …")
    flow = pd.Series(0.0, index=bdays)
    flow_cost = pd.Series(0.0, index=bdays)   # für Chart: Kostenbasis statt Erlös beim Verkauf
    for t in cash_txns:
        d = pd.Timestamp(t["date"])
        flow.loc[d:] += t["betrag"]
        if t["betrag"] > 0:                   # Kauf: voller Betrag
            flow_cost.loc[d:] += t["betrag"]
        else:                                  # Verkauf: nur FIFO-Kostenbasis abziehen
            flow_cost.loc[d:] -= t.get("_cost", 0.0)
    net_invested    = flow                     # bleibt für XIRR / G/V-Diff korrekt
    invested_capital = flow_cost.clip(lower=0) # Chart: nie negativ, geht auf 0 wenn alles verkauft

    bench_pe = price_eur_series(BENCH_TICKER, bdays)
    bench_value = None
    if bench_pe is not None:
        units = pd.Series(0.0, index=bdays)
        for t in cash_txns:
            d = pd.Timestamp(t["date"])
            p = bench_pe.loc[d] if d in bench_pe.index else bench_pe.reindex([d]).ffill().iloc[0]
            if p and p > 0:
                units.loc[d:] += t["betrag"] / p
        bench_value = units * bench_pe

    # Pro Position: kumuliertes eingezahltes Geld + hypothetischer MSCI-Wert (gleiche
    # Cashflows in den Index). Erlaubt dem Chip-Filter im Frontend, Depot- und G/V-Chart
    # aus den sichtbaren Positionen neu zu berechnen (Summe über alle == Gesamtlinien).
    inst_invested, inst_bench = {}, {}
    for ins in instruments.values():
        if ins["incomplete"]:
            continue
        inv = pd.Series(0.0, index=bdays)
        units = pd.Series(0.0, index=bdays)
        for t in ins["txns"]:
            d = pd.Timestamp(t["date"])
            if t["betrag"] > 0:
                inv.loc[d:] += t["betrag"]
            else:
                inv.loc[d:] -= t.get("_cost", 0.0)
            if bench_pe is not None:
                p = bench_pe.loc[d] if d in bench_pe.index else bench_pe.reindex([d]).ffill().iloc[0]
                if p and p > 0:
                    units.loc[d:] += t["betrag"] / p
        inst_invested[ins["name"]] = inv.clip(lower=0)
        if bench_pe is not None:
            inst_bench[ins["name"]] = units * bench_pe

    df = pd.DataFrame(rows).sort_values("total", ascending=False).reset_index(drop=True)
    df["returned"] = df["sells"] + df["cur_value"]
    df["ret_pct"]  = df["total"] / df["buys"] * 100
    df["status"] = df.apply(
        lambda r: "offen" if (r["shares_now"] > 0.01 and r["sells"] < 0.01)
        else ("teilw. verkauft" if r["shares_now"] > 0.01 else "verkauft"), axis=1)
    df["wert_heute_verkauft"] = df["sold_adj"] * df["cur_px"]
    df["timing"] = df["wert_heute_verkauft"] - df["sells"]
    df.loc[df["sold_adj"] < 0.01, "timing"] = 0.0
    df.loc[df["special"] == "cost", "timing"] = 0.0

    cur_value   = float(portfolio_value.iloc[-1])
    invested    = float(net_invested.iloc[-1])
    total_buys  = sum(t["betrag"] for t in cash_txns if t["betrag"] > 0)
    total_sells = sum(-t["betrag"] for t in cash_txns if t["betrag"] < 0)
    total_pl    = cur_value - invested
    realized_pl = df["realized"].sum()
    unreal_pl   = df["unrealized"].sum()

    flows = [(pd.Timestamp(t["date"]), -t["betrag"]) for t in cash_txns]
    r_port  = xirr(flows + [(TODAY, cur_value)])
    r_bench = xirr(flows + [(TODAY, float(bench_value.iloc[-1]))]) if bench_value is not None else None

    best   = df.loc[df["total"].idxmax()]
    worst  = df.loc[df["total"].idxmin()]
    bestp  = df.loc[df["ret_pct"].idxmax()]
    worstp = df[df["buys"] > 0].loc[df[df["buys"] > 0]["ret_pct"].idxmin()]
    sold   = df[df["sold_adj"] > 0.01]
    early  = sold.loc[sold["timing"].idxmax()] if len(sold) else None
    smart  = sold.loc[sold["timing"].idxmin()] if len(sold) else None

    inst_df = pd.DataFrame(inst_values).reindex(bdays).fillna(0.0)
    pv_w  = portfolio_value
    ni_w  = net_invested       # Netto-Cashflow (für G/V-Diff korrekt, kann negativ sein)
    ic_w  = invested_capital   # Kostenbasis-Reihe (Chart-Anzeige, nie negativ)
    bn_w  = bench_value
    dates = [d.strftime("%Y-%m-%d") for d in bdays]

    peak = inst_df.max()
    last = inst_df.iloc[-1]
    active  = [c for c in inst_df.columns if peak[c] > 0.5]
    ordered = sorted(active, key=lambda n: (float(last[n]), float(peak[n])), reverse=True)
    CAP = 24
    chosen, others = ordered[:CAP], ordered[CAP:]
    stack_labels = list(chosen) + (["Sonstige"] if others else [])
    stack_series = [[round(v, 2) for v in inst_df[n].values] for n in chosen]
    if others:
        stack_series.append([round(v, 2) for v in inst_df[others].sum(axis=1).values])

    def _bundle(series_map):
        """Per-Position-Reihen in dieselbe chosen/Sonstige-Struktur bringen."""
        if not series_map:
            return None
        sw = pd.DataFrame(series_map).reindex(bdays).fillna(0.0)
        out = [[round(v, 2) for v in sw[n].values] for n in chosen]
        if others:
            out.append([round(v, 2) for v in sw[others].sum(axis=1).values])
        return out
    stack_invested = _bundle(inst_invested)
    stack_bench    = _bundle(inst_bench)

    _log("Zusatzanalysen: Benchmark 2, Sektoren, Drawdown, Steuern …")

    # Portfolio-Drawdown
    pv_peak = portfolio_value.cummax()
    pv_dd_pct = ((portfolio_value - pv_peak) / pv_peak.where(pv_peak > 1.0) * 100).fillna(0.0)
    max_dd_portfolio = round(float(pv_dd_pct.min()), 1)
    pv_dd_w = pv_dd_pct.fillna(0.0)

    # Zweiter Benchmark: S&P 500 (SPY, EUR-konvertiert)
    bench2_value = None; r_bench2 = None; bn2_w = None; stack_bench2 = None
    bench2_pe = price_eur_series("SPY", bdays)
    if bench2_pe is not None:
        u2 = pd.Series(0.0, index=bdays)
        for t in cash_txns:
            d2 = pd.Timestamp(t["date"])
            p2 = bench2_pe.loc[d2] if d2 in bench2_pe.index else bench2_pe.reindex([d2]).ffill().iloc[0]
            if p2 and p2 > 0:
                u2.loc[d2:] += t["betrag"] / p2
        bench2_value = u2 * bench2_pe
        r_bench2 = xirr(flows + [(TODAY, float(bench2_value.iloc[-1]))])
        bn2_w = bench2_value
        ib2 = {}
        for ins in instruments.values():
            if ins["incomplete"]: continue
            u_ins = pd.Series(0.0, index=bdays)
            for t in ins["txns"]:
                d2 = pd.Timestamp(t["date"])
                p2 = bench2_pe.loc[d2] if d2 in bench2_pe.index else bench2_pe.reindex([d2]).ffill().iloc[0]
                if p2 and p2 > 0:
                    u_ins.loc[d2:] += t["betrag"] / p2
            ib2[ins["name"]] = u_ins * bench2_pe
        stack_bench2 = _bundle(ib2)

    # Währungsexposition
    tot_cv = sum(currency_vals.values()) or 1.0
    currency_exposure = {k: {"value": round(v, 2), "pct": round(v / tot_cv * 100, 1)}
                         for k, v in sorted(currency_vals.items(), key=lambda x: -x[1]) if v > 0.5}

    # Sektorverteilung
    tot_sv = sum(sector_vals.values()) or 1.0
    sector_alloc = {k: {"value": round(v, 2), "pct": round(v / tot_sv * 100, 1)}
                    for k, v in sorted(sector_vals.items(), key=lambda x: -x[1]) if k}

    # Dividendenhistorie (monatliche Summe)
    # Priorität: echte Konto-Daten > Yahoo-Brutto-Schätzung (lfd. Monat ausgeschlossen)
    curr_month = TODAY.strftime("%Y-%m")
    if cash and cash.get("div_monthly"):
        # Echte Dividenden aus dem Konto-Export → kein Schätzfehler möglich
        div_monthly_hist = cash["div_monthly"]
        div_hist_source = "konto"
    else:
        # Yahoo-Schätzung: nur abgeschlossene Monate (lfd. Monat hat oft falsche/fehlende Daten)
        div_monthly_hist_acc: dict = defaultdict(float)
        for ins in instruments.values():
            if ins["incomplete"] or not ins.get("ticker") or ins["special"]: continue
            ins_ccy = get_currency(ins["ticker"]) or "EUR"
            if ins_ccy == "GBp": ins_ccy = "GBP"
            fxf2 = fx_series(ins_ccy, bdays)
            h2 = pd.Series(0.0, index=bdays)
            for t in ins["txns"]:
                h2.loc[pd.Timestamp(t["date"]):] += t["adj_qty"]
            for dt, dps in get_dividends(ins["ticker"]).items():
                if bdays[0] <= dt <= TODAY and dt.strftime("%Y-%m") != curr_month:
                    sh2 = h2.asof(dt)
                    if pd.notna(sh2) and sh2 > 1e-9:
                        fv2 = float(fxf2.asof(dt)) if pd.notna(fxf2.asof(dt)) else 1.0
                        div_monthly_hist_acc[dt.strftime("%Y-%m")] += sh2 * dps * fv2
        div_monthly_hist = dict(div_monthly_hist_acc)
        div_hist_source = "yahoo"
    div_hist_months = sorted(div_monthly_hist.keys())
    div_hist_values = [round(div_monthly_hist[m], 2) for m in div_hist_months]

    # Steuerübersicht: realisierter G/V je Jahr -> geschätzte Steuer DE / AT
    tax_rows = []
    for yr in sorted(realized_by_year.keys()):
        gain = realized_by_year[yr]
        fb_de = 1000.0 if yr >= 2023 else 801.0
        tax_de = round(max(0.0, gain - fb_de) * 0.26375, 2)
        tax_at = round(max(0.0, gain) * 0.275, 2)
        tax_rows.append({"year": yr, "gain": round(gain, 2),
                         "tax_de": tax_de, "tax_at": tax_at, "fb_de": fb_de})

    # Ø Haltedauer offener Positionen
    held_days = [r["holding_days"] for r in rows if r.get("holding_days", 0) > 0]
    avg_holding_days = int(sum(held_days) / len(held_days)) if held_days else 0

    name2label = {n: n for n in chosen}
    for n in others:
        name2label[n] = "Sonstige"
    ev = {}
    for t in cash_txns:
        gid = isin2grp.get(t["isin"])
        iname = instruments[gid]["name"] if gid in instruments else t["name"]
        d = pd.Timestamp(t["date"]).strftime("%Y-%m-%d")
        e = ev.setdefault(d, {"date": d, "items": []})
        e["items"].append({"name": iname, "label": name2label.get(iname, "Sonstige"),
                           "kind": "Kauf" if t["betrag"] > 0 else "Verkauf",
                           "amt": round(abs(t["betrag"]))})
    events = [ev[d] for d in sorted(ev)]
    _log("Report zusammenstellen …")

    def a(r):
        return None if r is None else {"name": r["name"], "total": float(r["total"]),
                                       "ret_pct": float(r["ret_pct"]), "timing": float(r["timing"]),
                                       "sells": float(r["sells"]),
                                       "wert_heute": float(r["wert_heute_verkauft"])}
    payload = {
        "today": TODAY.strftime("%d.%m.%Y"),
        "incomplete": incomplete,
        "stats": {"total_pl": total_pl, "cur_value": cur_value, "invested": invested,
                  "buys": total_buys, "sells": total_sells, "realized": realized_pl,
                  "unreal": unreal_pl, "dividends": float(df["dividends"].sum()),
                  "div_real": bool(cash),
                  "deposits": cash["deposits"] if cash else None,
                  "withdrawals": cash["withdrawals"] if cash else None,
                  "interest": cash["interest"] if cash else None,
                  "fees": cash["fees"] if cash else None,
                  "xirr": (r_port or 0) * 100,
                  "bench": float(bench_value.iloc[-1]) if bench_value is not None else None,
                  "bench_xirr": (r_bench or 0) * 100 if bench_value is not None else None,
                  "bench2": float(bench2_value.iloc[-1]) if bench2_value is not None else None,
                  "bench2_xirr": (r_bench2 or 0) * 100 if r_bench2 is not None else None,
                  "max_dd": max_dd_portfolio,
                  "avg_holding_days": avg_holding_days},
        "dates": dates,
        "line": {"depot": [round(v, 2) for v in pv_w.values],
                 "invested": [round(v, 2) for v in ni_w.values],
                 "invested_cost": [round(v, 2) for v in ic_w.values],
                 "diff": [round(float(d) - float(i), 2) for d, i in zip(pv_w.values, ni_w.values)],
                 "bench": [round(v, 2) for v in bn_w.values] if bn_w is not None else None,
                 "bench2": [round(v, 2) for v in bn2_w.values] if bn2_w is not None else None,
                 "portfolio_dd": [round(v, 1) for v in pv_dd_w.values]},
        "stack": {"labels": stack_labels, "series": stack_series,
                  "invested": stack_invested, "bench": stack_bench, "bench2": stack_bench2},
        "currency_exp": currency_exposure,
        "sectors": sector_alloc,
        "div_hist": {"months": div_hist_months, "values": div_hist_values,
                     "source": div_hist_source},
        "tax": tax_rows,
        "portfolio_dd": {"max_dd": max_dd_portfolio,
                         "series": [round(v, 1) for v in pv_dd_w.values]},
        "events": events,
        "positions": [{"name": r["name"].title(), "ticker": r["ticker"], "isin": r["isin"],
                       "cur_px": r["cur_px"], "status": r["status"],
                       "currency": r.get("currency", "EUR"), "sector": r.get("sector", ""),
                       "first_buy": r.get("first_buy", ""), "holding_days": r.get("holding_days", 0),
                       "div_yield": r.get("div_yield", 0.0), "max_dd": r.get("max_dd", 0.0),
                       "buys": r["buys"], "returned": r["returned"], "cur_value": r["cur_value"],
                       "dividends": r["dividends"], "realized": r["realized"],
                       "unrealized": r["unrealized"], "total": r["total"],
                       "ret_pct": r["ret_pct"], "timing": r["timing"],
                       "txns": pos_txns.get(r["name"], [])}
                      for _, r in df.sort_values("total", ascending=False).iterrows()],
        "analysis": {"best": a(best), "bestp": a(bestp), "worst": a(worst),
                     "worstp": a(worstp), "early": a(early), "smart": a(smart)},
    }
    ctx = {
        "df": df, "net_invested": net_invested, "portfolio_value": portfolio_value,
        "bench_value": bench_value, "r_port": r_port, "r_bench": r_bench,
        "cur_value": cur_value, "invested": invested, "total_pl": total_pl,
        "total_buys": total_buys, "total_sells": total_sells,
        "div_total": float(df["dividends"].sum()), "cash": cash,
        "val_ok": val_ok, "val_bad": val_bad,
        "incomplete": incomplete, "missing": missing,
        "best": best, "worst": worst, "bestp": bestp, "worstp": worstp,
        "early": early, "smart": smart,
    }
    return payload, ctx


def main(args=None):
    args = args if args is not None else parse_args()
    # Eingaben im Aufruf-Ordner suchen und zu absoluten Pfaden machen
    tx_path = os.path.abspath(args.transactions) if args.transactions else \
              os.path.abspath(find_flatex_csv())
    if args.no_cash:
        cash_path = None
    elif args.cash:
        cash_path = os.path.abspath(args.cash)
    else:
        cp = find_cash_csv()
        cash_path = os.path.abspath(cp) if cp else None
    ovr_path = os.path.abspath(args.overrides) if args.overrides else (
        os.path.abspath("overrides.json") if os.path.exists("overrides.json") else None)
    for label, pth in (("Wertpapier-Export", tx_path), ("Konto-Export", cash_path)):
        if pth and not os.path.exists(pth):
            sys.exit(f"Datei nicht gefunden: {pth}")
    os.makedirs(args.outdir, exist_ok=True)
    os.chdir(args.outdir)
    os.makedirs(CACHE, exist_ok=True)
    print(f"  Wertpapier-Export : {tx_path}")
    print(f"  Konto-Export      : {cash_path or '(keiner – Dividenden werden geschätzt)'}")

    payload, ctx = compute_payload(tx_path, cash_path, cache_dir=CACHE,
                                   today=args.today, overrides_path=ovr_path)
    df      = ctx["df"]
    cash    = ctx["cash"]
    best    = ctx["best"];  worst  = ctx["worst"]
    bestp   = ctx["bestp"]; worstp = ctx["worstp"]
    early   = ctx["early"]; smart  = ctx["smart"]

    def eur(x): return f"{x:,.2f} EUR".replace(",", "X").replace(".", ",").replace("X", ".")
    def pct(r): return "n/a" if r is None else f"{r*100:+.1f} % p.a."

    print("\n" + "=" * 64)
    print("  PORTFOLIO-ZUSAMMENFASSUNG  (Stand " + TODAY.strftime("%d.%m.%Y") + ")")
    print("=" * 64)
    print(f"  Gesamt gekauft (eingezahlt) : {eur(ctx['total_buys'])}")
    print(f"  Gesamt verkauft (ausgezahlt): {eur(ctx['total_sells'])}")
    print(f"  Noch investiert (netto)     : {eur(ctx['invested'])}")
    print(f"  Aktueller Depotwert         : {eur(ctx['cur_value'])}")
    print("  " + "-" * 60)
    print(f"  Realisierte G/V (verkauft)  : {eur(payload['stats']['realized'])}")
    print(f"  Unrealisierte G/V (offen)   : {eur(payload['stats']['unreal'])}")
    print(f"  GESAMT-GEWINN/-VERLUST      : {eur(ctx['total_pl'])}   "
          f"({ctx['total_pl']/ctx['invested']*100:+.1f} % auf netto investiertes Kapital)")
    div_lbl = "netto, lt. Konto" if cash else "brutto, geschätzt"
    print(f"  Dividenden ({div_lbl}): {eur(ctx['div_total'])}")
    print(f"  Gesamtertrag inkl. Dividenden : {eur(ctx['total_pl'] + ctx['div_total'])}")
    print(f"  Jahresrendite (geldgewichtet, XIRR): {pct(ctx['r_port'])}")
    if cash:
        print("  " + "-" * 60)
        print(f"  Auf Flatex eingezahlt       : {eur(cash['deposits'])}")
        print(f"  Auf andere Konten ausgezahlt: {eur(cash['withdrawals'])}")
        print(f"  Netto eingezahlt            : {eur(cash['deposits'] - cash['withdrawals'])}")
        print(f"  Zinsen/Gebühren gezahlt     : {eur(-(cash['interest'] + cash['fees']))}")
    print("=" * 64)
    print("  Vergleich: waerst du besser dran gewesen ...")
    print(f"    ... GAR NICHT investiert (Cash, 0% p.a.):")
    print(f"          Investieren brachte dir {eur(ctx['total_pl'])}  ->  JA, klar besser investiert")
    bench_value = ctx["bench_value"]
    if bench_value is not None:
        bv = float(bench_value.iloc[-1])
        diff = bv - ctx["cur_value"]
        print(f"    ... alles stur in den MSCI World ({pct(ctx['r_bench'])}):")
        print(f"          {eur(bv)} statt {eur(ctx['cur_value'])}  ->  "
              f"waere {eur(abs(diff))} {'MEHR' if diff>0 else 'WENIGER'} gewesen")
    print("=" * 64)
    print("\n  BESTE ENTSCHEIDUNG:")
    print(f"    + Größter Gewinn : {best['name'][:28]:28} {eur(best['total'])}  "
          f"({best['ret_pct']:+.0f} %)")
    print(f"    + Beste Rendite  : {bestp['name'][:28]:28} {bestp['ret_pct']:+.0f} %  "
          f"({eur(bestp['total'])})")
    if smart is not None and smart["timing"] < -20:
        print(f"    + Bester Ausstieg: {smart['name'][:28]:28} "
              f"{eur(-smart['timing'])} weniger Verlust durch Verkauf")
    print("\n  GRÖSSTER FEHLER:")
    print(f"    - Größter Verlust: {worst['name'][:28]:28} {eur(worst['total'])}  "
          f"({worst['ret_pct']:+.0f} %)")
    print(f"    - Schlecht. Rend.: {worstp['name'][:28]:28} {worstp['ret_pct']:+.0f} %  "
          f"({eur(worstp['total'])})")
    if early is not None and early["timing"] > 20:
        print(f"    - Zu früh verkauft: {early['name'][:27]:27} "
              f"{eur(early['timing'])} entgangener Gewinn "
              f"(verkauft für {eur(early['sells'])}, heute {eur(early['wert_heute_verkauft'])} wert)")
    print("=" * 64)
    render_table(df, eur)
    if ctx["missing"]:
        print("\n  Hinweis - ohne Marktdaten (zu Einstand bewertet): " + ", ".join(ctx["missing"]))
    if ctx["incomplete"]:
        print("\n  Achtung: Mehr verkauft als gekauft (Kauf liegt vermutlich VOR dem Export):")
        print("    " + ", ".join(ctx["incomplete"]))
        print("    -> Für korrekte Zahlen den vollständigen Flatex-Export ab "
              "Depoteröffnung verwenden.")
    if ctx["val_ok"] + ctx["val_bad"]:
        print(f"\n  Validierung Kaufkurse vs. Marktdaten: {ctx['val_ok']}/{ctx['val_ok']+ctx['val_bad']} "
              f"Käufe stimmen (±30%) überein.")
    open_neg = df[df["shares_now"] < -0.01]
    if len(open_neg):
        print("  WARN negative Restbestände:", list(open_neg["name"]))
    print(f"\n  Details je Position -> {OUT_CSV}")

    df.to_csv(OUT_CSV, index=False)
    make_plot(ctx["net_invested"], ctx["portfolio_value"], bench_value,
              ctx["total_pl"], ctx["cur_value"], ctx["invested"], ctx["r_port"], ctx["r_bench"])
    print(f"  Graph   -> {OUT_PNG}")
    export_html(payload)
    print(f"  Report  -> {OUT_HTML}\n")


if False:  # tot – alter main()-Körper, ersetzt durch compute_payload()
    _dummy_isin2grp, _dummy_groups = build_groups([])  # verhindert NameError bei import
    instruments = {}
    for gid, isins in groups.items():
        gtx = sorted((t for t in txns if isin2grp[t["isin"]] == gid),
                     key=lambda t: t["date"])
        canon = gtx[-1]["isin"]                       # aktuelle ISIN -> aktueller Ticker
        special = next((special_map[i] for i in isins if i in special_map), None)
        name = next((t["name"] for t in reversed(gtx) if t["kind"] != "corp"),
                    gtx[-1]["name"])
        instruments[gid] = {"name": name, "ticker": isin2ticker.get(canon),
                            "special": special, "isins": isins,
                            "txns": [t for t in gtx if t["kind"] in ("buy", "sell")],
                            "all_txns": gtx}

    # Splits AUS FLATEX ableiten (massgeblich); falls keine Kapitalmassnahme
    # vorliegt (z.B. Split nach Verkauf, oder Teil-Export), Yahoo-Splits nutzen -
    # aber nur solche NACH dem ersten Kauf (fruehere sind irrelevant).
    for gid, ins in instruments.items():
        ins["splits"] = flatex_splits(ins["all_txns"])
        if not ins["splits"] and ins.get("ticker") and not ins["special"] and ins["txns"]:
            first = pd.Timestamp(min(t["date"] for t in ins["txns"]))
            ins["splits"] = {d: r for d, r in get_splits(ins["ticker"]).items()
                             if d > first}
        for t in ins["txns"]:
            factor = 1.0
            for sdate, ratio in ins["splits"].items():
                if sdate > pd.Timestamp(t["date"]):
                    factor *= ratio
            t["adj_qty"] = t["qty"] * factor

    # Unvollständige Historie erkennen: mehr verkauft als gekauft -> Kauf liegt vor
    # dem Export. Einstand unbekannt -> Position aus allen Zahlen ausschliessen.
    incomplete = []
    incomplete_isins = set()
    for gid, ins in instruments.items():
        ba = sum(t["adj_qty"] for t in ins["txns"] if t["adj_qty"] > 0)
        sa = sum(-t["adj_qty"] for t in ins["txns"] if t["adj_qty"] < 0)
        ins["incomplete"] = sa > ba + 1e-6
        if ins["incomplete"]:
            incomplete.append(ins["name"])
            incomplete_isins.update(ins["isins"])

    # Verrechnungskonto (optional): echte Bardividenden je ISIN + Ein-/Auszahlungen
    cash = load_cash_account(cash_path) if cash_path else None
    cash_div = cash["div"] if cash else None

    # ---- Marktwert je Instrument ueber die Zeit + Kennzahlen je Position
    portfolio_value = pd.Series(0.0, index=bdays)
    inst_values = {}          # Name -> tägliche Wert-Zeitreihe (für Stacked-Chart)
    rows = []
    pos_txns = {}             # Name -> Kauf-/Verkauf-Historie (für Tabellen-Detailansicht)
    missing = []
    val_ok = val_bad = 0
    from collections import deque
    for key, ins in sorted(instruments.items(), key=lambda x: x[1]["name"]):
        if ins["incomplete"]:
            continue                       # Einstand unbekannt -> nicht bewertbar
        tx = sorted(ins["txns"], key=lambda t: t["date"])
        buys  = sum(t["betrag"] for t in tx if t["betrag"] > 0)
        sells = sum(-t["betrag"] for t in tx if t["betrag"] < 0)
        # Kauf-/Verkauf-Historie je Position (für die Detailansicht in der Tabelle)
        pos_txns[ins["name"]] = [
            {"d": pd.Timestamp(t["date"]).strftime("%d.%m.%Y"),
             "k": "Kauf" if t["kind"] == "buy" else "Verkauf",
             "q": round(abs(t["qty"]), 4), "p": round(t["kurs"], 2),
             "a": round(t["betrag"], 2)} for t in tx]

        buy_adj = sum(t["adj_qty"] for t in tx if t["adj_qty"] > 0)     # gekaufte Stk.

        # FIFO: realisierte G/V + verbleibende Lots (in heutiger Split-Basis)
        lots = deque()        # [adj_qty_rest, einstand_pro_adj_stk]
        realized = 0.0
        for t in tx:
            if t["adj_qty"] > 0:                       # Kauf
                lots.append([t["adj_qty"], t["betrag"] / t["adj_qty"]])
            elif t["adj_qty"] < 0:                     # Verkauf
                sell_rest = -t["adj_qty"]
                price_per = (-t["betrag"]) / sell_rest
                while sell_rest > 1e-9 and lots:
                    lot = lots[0]
                    take = min(sell_rest, lot[0])
                    realized += take * (price_per - lot[1])
                    lot[0] -= take; sell_rest -= take
                    if lot[0] <= 1e-9:
                        lots.popleft()
        remaining = sum(l[0] for l in lots)            # heutige Stueck
        cost_rest = sum(l[0] * l[1] for l in lots)     # Einstand offener Stueck
        # Einstandspreis pro adj. Stück (0, falls keine Käufe im Export -> kein Crash)
        cost_price = (buys / buy_adj) if buy_adj > 1e-9 else 0.0
        avg_cost   = (cost_rest / remaining) if remaining > 1e-9 else cost_price

        # tägliche Stückzahl (adjustiert) als Stufenfunktion
        hold = pd.Series(0.0, index=bdays)
        for t in tx:
            hold.loc[pd.Timestamp(t["date"]):] += t["adj_qty"]

        # Sicherheits-Check: weichen Flatex- und Yahoo-Splits ab -> Kurse unzuverlässig
        if ins["special"] is None and split_mismatch(ins):
            ins["special"] = "cost"
            missing.append(f"{ins['name']} (Split Flatex≠Yahoo)")

        # Marktwert-Serie
        pe = None
        if ins["special"] == "zero":
            val = pd.Series(0.0, index=bdays)
        elif ins["special"] == "cost":
            val = hold * cost_price                      # waehrend Haltedauer zu Einstand
        elif not ins.get("ticker"):
            val = hold * cost_price
            missing.append(ins["name"])
        else:
            pe = price_eur_series(ins["ticker"], bdays)
            if pe is None:
                val = hold * cost_price
                missing.append(f"{ins['name']} ({ins['ticker']})")
            else:
                val = hold * pe

        # Dividenden: echte Bargutschriften vom Konto (falls vorhanden), sonst
        # Brutto-Schaetzung aus der Yahoo-Ausschuettungshistorie.
        if cash_div is not None:
            div_total = sum(cash_div.get(i, 0.0) for i in ins["isins"])
        else:
            div_total = 0.0
            tk = ins.get("ticker")
            if tk and not ins["special"]:
                ccy = get_currency(tk) or "USD"
                fxf = fx_series(ccy, bdays)
                for dt, dps in get_dividends(tk).items():
                    if bdays[0] <= dt <= bdays[-1]:
                        sh = hold.asof(dt)
                        if pd.notna(sh) and sh > 1e-9:
                            fv = fxf.asof(dt)
                            div_total += sh * dps * (fv if pd.notna(fv) else 1.0)

        # Validierung: rekonstruierter (unbereinigter) Kaufkurs vs CSV-Kurs
        if pe is not None:
            splits = ins["splits"]
            for t in tx:
                if t["qty"] > 0 and t["kurs"] > 0:
                    f = 1.0
                    for sdate, ratio in splits.items():
                        if sdate > pd.Timestamp(t["date"]):
                            f *= ratio
                    d = pd.Timestamp(t["date"])
                    model = pe.reindex([d]).ffill().iloc[0]
                    if pd.notna(model) and model > 0:
                        recon = model * f          # zurueck auf damalige Basis
                        if abs(recon / t["kurs"] - 1) < 0.30:
                            val_ok += 1
                        else:
                            val_bad += 1

        val = val.fillna(0.0)
        portfolio_value += val
        inst_values[ins["name"]] = val
        cur_val = float(val.iloc[-1])
        unreal = cur_val - cost_rest
        # aktueller Kurs je adj. Stück (EUR) für Timing-Analyse
        if ins["special"] == "zero":
            cur_px = 0.0
        elif ins["special"] == "cost" or pe is None:
            cur_px = cost_price
        else:
            cur_px = float(pe.iloc[-1])
        sold_adj = sum(-t["adj_qty"] for t in tx if t["adj_qty"] < 0)
        rows.append(dict(name=ins["name"], ticker=ins.get("ticker") or "-",
                         shares_now=round(remaining, 4), avg_cost=round(avg_cost, 2),
                         buys=round(buys, 2), sells=round(sells, 2),
                         cur_value=round(cur_val, 2), dividends=round(div_total, 2),
                         realized=round(realized, 2), unrealized=round(unreal, 2),
                         total=round(realized + unreal, 2),
                         sold_adj=round(sold_adj, 4), cur_px=round(cur_px, 4),
                         special=ins["special"] or ""))

    # nur echte Geldfluesse (Kauf/Verkauf) zaehlen fuer Cashflow-Kennzahlen
    cash_txns = [t for t in txns if t["kind"] in ("buy", "sell")
                 and t["isin"] not in incomplete_isins]

    # ---- eingezahltes Geld (Cash-Basislinie = "nicht investiert")
    flow = pd.Series(0.0, index=bdays)
    for t in cash_txns:
        flow.loc[pd.Timestamp(t["date"]):] += t["betrag"]
    net_invested = flow                                # kumuliert Kaeufe - Verkaeufe

    # ---- hypothetischer MSCI World: identische Cashflows in den Index
    bench_pe = price_eur_series(BENCH_TICKER, bdays)
    bench_value = None
    if bench_pe is not None:
        units = pd.Series(0.0, index=bdays)
        for t in cash_txns:
            d = pd.Timestamp(t["date"])
            p = bench_pe.loc[d] if d in bench_pe.index else bench_pe.reindex([d]).ffill().iloc[0]
            if p and p > 0:
                units.loc[d:] += t["betrag"] / p       # Kauf: + Anteile, Verkauf: - Anteile
        bench_value = units * bench_pe

    # ---------------------------------------------------------- Ausgabe Zahlen
    df = pd.DataFrame(rows).sort_values("total", ascending=False).reset_index(drop=True)
    # abgeleitete Kennzahlen
    df["returned"] = df["sells"] + df["cur_value"]            # Erlös + heutiger Wert
    df["ret_pct"]  = df["total"] / df["buys"] * 100
    df["status"] = df.apply(
        lambda r: "offen" if (r["shares_now"] > 0.01 and r["sells"] < 0.01)
        else ("teilw. verkauft" if r["shares_now"] > 0.01 else "verkauft"), axis=1)
    # Timing: was wären die VERKAUFTEN Stücke heute wert? (+ = zu früh/billig verkauft)
    df["wert_heute_verkauft"] = df["sold_adj"] * df["cur_px"]
    df["timing"] = df["wert_heute_verkauft"] - df["sells"]
    df.loc[df["sold_adj"] < 0.01, "timing"] = 0.0            # nie verkauft -> kein Timing
    df.loc[df["special"] == "cost", "timing"] = 0.0          # ohne echten Kurs -> N/A
    df.to_csv(OUT_CSV, index=False)

    cur_value   = float(portfolio_value.iloc[-1])
    invested    = float(net_invested.iloc[-1])
    total_buys  = sum(t["betrag"] for t in cash_txns if t["betrag"] > 0)
    total_sells = sum(-t["betrag"] for t in cash_txns if t["betrag"] < 0)
    total_pl    = cur_value - invested
    realized_pl = df["realized"].sum()
    unreal_pl   = df["unrealized"].sum()

    # geldgewichtete Jahresrendite (XIRR): Käufe = Geld raus, Verkäufe = rein, + Endwert
    flows = [(pd.Timestamp(t["date"]), -t["betrag"]) for t in cash_txns]
    r_port  = xirr(flows + [(TODAY, cur_value)])
    r_bench = xirr(flows + [(TODAY, float(bench_value.iloc[-1]))]) if bench_value is not None else None

    def eur(x): return f"{x:,.2f} EUR".replace(",", "X").replace(".", ",").replace("X", ".")
    def pct(r): return "n/a" if r is None else f"{r*100:+.1f} % p.a."

    print("\n" + "=" * 64)
    print("  PORTFOLIO-ZUSAMMENFASSUNG  (Stand " + TODAY.strftime("%d.%m.%Y") + ")")
    print("=" * 64)
    print(f"  Gesamt gekauft (eingezahlt) : {eur(total_buys)}")
    print(f"  Gesamt verkauft (ausgezahlt): {eur(total_sells)}")
    print(f"  Noch investiert (netto)     : {eur(invested)}")
    print(f"  Aktueller Depotwert         : {eur(cur_value)}")
    print("  " + "-" * 60)
    print(f"  Realisierte G/V (verkauft)  : {eur(realized_pl)}")
    print(f"  Unrealisierte G/V (offen)   : {eur(unreal_pl)}")
    print(f"  GESAMT-GEWINN/-VERLUST      : {eur(total_pl)}   "
          f"({total_pl/invested*100:+.1f} % auf netto investiertes Kapital)")
    div_total = float(df["dividends"].sum())
    div_lbl = "netto, lt. Konto" if cash else "brutto, geschätzt"
    print(f"  Dividenden ({div_lbl}): {eur(div_total)}")
    print(f"  Gesamtertrag inkl. Dividenden : {eur(total_pl + div_total)}")
    print(f"  Jahresrendite (geldgewichtet, XIRR): {pct(r_port)}")
    if cash:
        print("  " + "-" * 60)
        print(f"  Auf Flatex eingezahlt       : {eur(cash['deposits'])}")
        print(f"  Auf andere Konten ausgezahlt: {eur(cash['withdrawals'])}")
        print(f"  Netto eingezahlt            : {eur(cash['deposits'] - cash['withdrawals'])}")
        print(f"  Zinsen/Gebühren gezahlt     : {eur(-(cash['interest'] + cash['fees']))}")
    print("=" * 64)
    print("  Vergleich: waerst du besser dran gewesen ...")
    print(f"    ... GAR NICHT investiert (Cash, 0% p.a.):")
    print(f"          Investieren brachte dir {eur(total_pl)}  ->  JA, klar besser investiert")
    if bench_value is not None:
        bv = float(bench_value.iloc[-1])           # MSCI-World-Depotwert (gleiche Flows)
        diff = bv - cur_value
        print(f"    ... alles stur in den MSCI World ({pct(r_bench)}):")
        print(f"          {eur(bv)} statt {eur(cur_value)}  ->  "
              f"waere {eur(abs(diff))} {'MEHR' if diff>0 else 'WENIGER'} gewesen")
    print("=" * 64)

    # ----------------------------- Beste Entscheidung / Größter Fehler
    best   = df.loc[df["total"].idxmax()]
    worst  = df.loc[df["total"].idxmin()]
    bestp  = df.loc[df["ret_pct"].idxmax()]
    worstp = df[df["buys"] > 0].loc[df[df["buys"] > 0]["ret_pct"].idxmin()]
    sold   = df[df["sold_adj"] > 0.01]
    early  = sold.loc[sold["timing"].idxmax()] if len(sold) else None   # zu früh verkauft
    smart  = sold.loc[sold["timing"].idxmin()] if len(sold) else None   # guter Ausstieg
    print("\n  BESTE ENTSCHEIDUNG:")
    print(f"    + Größter Gewinn : {best['name'][:28]:28} {eur(best['total'])}  "
          f"({best['ret_pct']:+.0f} %)")
    print(f"    + Beste Rendite  : {bestp['name'][:28]:28} {bestp['ret_pct']:+.0f} %  "
          f"({eur(bestp['total'])})")
    if smart is not None and smart["timing"] < -20:
        print(f"    + Bester Ausstieg: {smart['name'][:28]:28} "
              f"{eur(-smart['timing'])} weniger Verlust durch Verkauf")
    print("\n  GRÖSSTER FEHLER:")
    print(f"    - Größter Verlust: {worst['name'][:28]:28} {eur(worst['total'])}  "
          f"({worst['ret_pct']:+.0f} %)")
    print(f"    - Schlecht. Rend.: {worstp['name'][:28]:28} {worstp['ret_pct']:+.0f} %  "
          f"({eur(worstp['total'])})")
    if early is not None and early["timing"] > 20:
        print(f"    - Zu früh verkauft: {early['name'][:27]:27} "
              f"{eur(early['timing'])} entgangener Gewinn "
              f"(verkauft für {eur(early['sells'])}, heute {eur(early['wert_heute_verkauft'])} wert)")
    print("=" * 64)
    render_table(df, eur)
    if missing:
        print("\n  Hinweis - ohne Marktdaten (zu Einstand bewertet): " + ", ".join(missing))
    if incomplete:
        print("\n  Achtung: Mehr verkauft als gekauft (Kauf liegt vermutlich VOR dem Export):")
        print("    " + ", ".join(incomplete))
        print("    -> Für korrekte Zahlen den vollständigen Flatex-Export ab "
              "Depoteröffnung verwenden.")
    if (val_ok + val_bad):
        print(f"\n  Validierung Kaufkurse vs. Marktdaten: {val_ok}/{val_ok+val_bad} "
              f"Käufe stimmen (±30%) überein.")
    # Plausibilitaet: keine negativen Restbestände (Hinweis auf Split-Fehler)
    open_neg = df[df["shares_now"] < -0.01]
    if len(open_neg):
        print("  WARN negative Restbestände:", list(open_neg["name"]))
    print(f"\n  Details je Position -> {OUT_CSV}")

    # ---------------------------------------------------------- Plot (PNG)
    make_plot(net_invested, portfolio_value, bench_value,
              total_pl, cur_value, invested, r_port, r_bench)
    print(f"  Graph   -> {OUT_PNG}")

    # ---------------------------------------------------------- HTML-Report
    # Tägliche Handelstag-Stützstellen (volle Auflösung)
    inst_df = pd.DataFrame(inst_values).reindex(bdays).fillna(0.0)
    pv_w  = portfolio_value
    ni_w  = net_invested
    bn_w  = bench_value
    dates = [d.strftime("%Y-%m-%d") for d in bdays]

    # Stacking: ALLE jemals gehaltenen Positionen einzeln (auch laengst verkaufte),
    # damit das Chart zu jedem Zeitpunkt die damalige Zusammensetzung zeigt. Nur ein
    # kleiner, vernachlaessigbarer Rest (Peak-Wert <0,5 €) wandert in "Sonstige".
    peak = inst_df.max()
    last = inst_df.iloc[-1]
    active  = [c for c in inst_df.columns if peak[c] > 0.5]
    ordered = sorted(active, key=lambda n: (float(last[n]), float(peak[n])), reverse=True)
    CAP = 24
    chosen, others = ordered[:CAP], ordered[CAP:]
    stack_labels = list(chosen) + (["Sonstige"] if others else [])
    stack_series = [[round(v, 2) for v in inst_df[n].values] for n in chosen]
    if others:
        stack_series.append([round(v, 2) for v in inst_df[others].sum(axis=1).values])

    # Ereignisse (Kaeufe/Verkaeufe) je Tag -> Vertikallinien + Hover-Marker in den Charts.
    # Jeder Eintrag traegt den kanonischen Positionsnamen und das zugehoerige Stack-Label
    # ("Sonstige" fuer gebuendelte Titel), damit die JS-Seite die Ereignisse passend zu den
    # eingeblendeten Positionen filtern kann.
    name2label = {n: n for n in chosen}
    for n in others:
        name2label[n] = "Sonstige"
    ev = {}
    for t in cash_txns:
        gid = isin2grp.get(t["isin"])
        iname = instruments[gid]["name"] if gid in instruments else t["name"]
        d = pd.Timestamp(t["date"]).strftime("%Y-%m-%d")
        e = ev.setdefault(d, {"date": d, "items": []})
        e["items"].append({"name": iname, "label": name2label.get(iname, "Sonstige"),
                           "kind": "Kauf" if t["betrag"] > 0 else "Verkauf",
                           "amt": round(abs(t["betrag"]))})
    events = [ev[d] for d in sorted(ev)]

    def a(r):  # analysis-Eintrag -> dict
        return None if r is None else {"name": r["name"], "total": float(r["total"]),
                                       "ret_pct": float(r["ret_pct"]), "timing": float(r["timing"]),
                                       "sells": float(r["sells"]),
                                       "wert_heute": float(r["wert_heute_verkauft"])}
    payload = {
        "today": TODAY.strftime("%d.%m.%Y"),
        "incomplete": incomplete,
        "stats": {"total_pl": total_pl, "cur_value": cur_value, "invested": invested,
                  "buys": total_buys, "sells": total_sells, "realized": realized_pl,
                  "unreal": unreal_pl, "dividends": float(df["dividends"].sum()),
                  "div_real": bool(cash),
                  "deposits": cash["deposits"] if cash else None,
                  "withdrawals": cash["withdrawals"] if cash else None,
                  "interest": cash["interest"] if cash else None,
                  "fees": cash["fees"] if cash else None,
                  "xirr": (r_port or 0) * 100,
                  "bench": float(bench_value.iloc[-1]) if bench_value is not None else None,
                  "bench_xirr": (r_bench or 0) * 100 if bench_value is not None else None},
        "dates": dates,
        "line": {"depot": [round(v, 2) for v in pv_w.values],
                 "invested": [round(v, 2) for v in ni_w.values],
                 "diff": [round(float(d) - float(i), 2) for d, i in zip(pv_w.values, ni_w.values)],
                 "bench": [round(v, 2) for v in bn_w.values] if bn_w is not None else None},
        "stack": {"labels": stack_labels, "series": stack_series},
        "events": events,
        "positions": [{"name": r["name"].title(), "ticker": r["ticker"], "status": r["status"],
                       "buys": r["buys"], "returned": r["returned"], "cur_value": r["cur_value"],
                       "dividends": r["dividends"], "realized": r["realized"],
                       "unrealized": r["unrealized"], "total": r["total"],
                       "ret_pct": r["ret_pct"], "timing": r["timing"],
                       "txns": pos_txns.get(r["name"], [])}
                      for _, r in df.sort_values("total", ascending=False).iterrows()],
        "analysis": {"best": a(best), "bestp": a(bestp), "worst": a(worst),
                     "worstp": a(worstp), "early": a(early), "smart": a(smart)},
    }
    export_html(payload)
    print(f"  Report  -> {OUT_HTML}\n")


OUT_TABLE = "ergebnis_je_aktie.png"

def render_table(df, eur):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    d = df.sort_values("total", ascending=False).reset_index(drop=True)
    n = len(d)
    GREEN, RED, DARK, GREY = "#1a7a3a", "#c0241c", "#222222", "#777777"
    f0 = lambda x: f"{x:,.0f}".replace(",", ".") + " €"

    rowh = 0.34
    fig, ax = plt.subplots(figsize=(13.2, 1.7 + (n + 2) * rowh))
    fig.patch.set_facecolor("white")
    ax.set_xlim(0, 1); ax.set_ylim(0, n + 2.3); ax.axis("off")

    # Spalten: (rechter Rand x, Ausrichtung)
    X = dict(name=0.008, status=0.345, ein=0.555, zur=0.70, gv=0.855, pct=0.992)
    def cell(x, y, txt, ha="right", color=DARK, w="normal", fs=10.5):
        ax.text(x, y, txt, ha=ha, va="center", color=color, fontweight=w, fontsize=fs)

    yh = n + 1.4
    ax.text(X["name"], n + 2.0, "Ergebnis je Aktie – wie bin ich ausgestiegen?",
            ha="left", va="center", fontsize=15, fontweight="bold", color=DARK)
    for x, t, ha in [(X["name"], "Aktie", "left"), (X["status"], "Status", "left"),
                     (X["ein"], "eingezahlt", "right"), (X["zur"], "Erlös + Wert heute", "right"),
                     (X["gv"], "Gewinn/Verlust", "right"), (X["pct"], "Rendite", "right")]:
        cell(x, yh, t, ha, GREY, "bold", 10)
    ax.plot([0, 1], [yh - 0.45, yh - 0.45], color="#cccccc", lw=1.0)

    for i, r in d.iterrows():
        y = n - i
        if i % 2 == 0:
            ax.add_patch(Rectangle((0, y - 0.5), 1, 1.0, color="#f4f4f4", zorder=0))
        col = GREEN if r["total"] >= 0 else RED
        sym = {"offen": "●  ", "teilw. verkauft": "◐  ", "verkauft": "✓  "}[r["status"]]
        scol = {"offen": "#2c5fa8", "teilw. verkauft": "#8a6d1a", "verkauft": GREY}[r["status"]]
        cell(X["name"], y, r["name"][:30].title(), "left", DARK, "bold", 10.5)
        cell(X["status"], y, sym + r["status"], "left", scol, "normal", 9.5)
        cell(X["ein"], y, f0(r["buys"]), "right", DARK)
        cell(X["zur"], y, f0(r["returned"]), "right", DARK)
        cell(X["gv"], y, ("+" if r["total"] >= 0 else "") + f0(r["total"]), "right", col, "bold")
        cell(X["pct"], y, f"{r['ret_pct']:+.0f} %", "right", col, "bold")

    # Summenzeile
    ax.plot([0, 1], [0.05, 0.05], color="#999999", lw=1.2)
    tot = d["total"].sum()
    cell(X["name"], -0.55, "GESAMT", "left", DARK, "bold", 11)
    cell(X["ein"], -0.55, f0(d["buys"].sum()), "right", DARK, "bold")
    cell(X["zur"], -0.55, f0(d["returned"].sum()), "right", DARK, "bold")
    cell(X["gv"], -0.55, ("+" if tot >= 0 else "") + f0(tot), "right",
         GREEN if tot >= 0 else RED, "bold", 11)
    cell(X["pct"], -0.55, f"{tot/d['buys'].sum()*100:+.0f} %", "right",
         GREEN if tot >= 0 else RED, "bold", 11)
    ax.text(X["name"], -1.4,
            "●  offen (noch im Depot)    ◐  teilweise verkauft    ✓  komplett verkauft     "
            "| Erlös+Wert heute = Verkaufserlöse plus aktueller Depotwert",
            ha="left", va="center", fontsize=8.5, color=GREY)

    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.01)
    fig.savefig(OUT_TABLE, dpi=150, facecolor="white", bbox_inches="tight")
    print(f"  Tabelle -> {OUT_TABLE}")


def make_plot(net_invested, value, bench, total_pl, cur_value, invested, r_port, r_bench):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.ticker import FuncFormatter

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11})
    GREEN, RED, BLUE, GREY, BLACK = "#1a9850", "#d73027", "#2c5fa8", "#9a9a9a", "#111111"
    eur_fmt = FuncFormatter(lambda v, _: f"{v:,.0f}".replace(",", ".") + " €")

    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(14, 9.5), sharex=True,
                                  gridspec_kw={"height_ratios": [2.4, 1], "hspace": 0.12})
    fig.patch.set_facecolor("white")
    x = value.index
    profit = value - net_invested
    bprofit = (bench - net_invested) if bench is not None else None

    # ===================== Panel 1: Depotwert vs. eingezahltes Geld =====================
    ax.set_facecolor("#fafafa")
    ax.fill_between(x, net_invested, value, where=(value >= net_invested),
                    color=GREEN, alpha=0.16, interpolate=True)
    ax.fill_between(x, net_invested, value, where=(value < net_invested),
                    color=RED, alpha=0.16, interpolate=True)
    if bench is not None:
        ax.plot(x, bench, color=GREY, lw=1.5, ls="--",
                label="Hätte: alles stur in MSCI World")
    ax.plot(x, net_invested, color=BLUE, lw=1.9,
            label="Eingezahltes Geld (= gar nicht investiert)")
    ax.plot(x, value, color=BLACK, lw=2.5, label="Depotwert (echter Marktwert)")

    def endlabel(axx, yv, txt, color, weight="normal", fs=10.5):
        axx.scatter([x[-1]], [yv], color=color, zorder=6, s=30)
        axx.annotate(txt, (x[-1], yv), xytext=(8, 0), textcoords="offset points",
                     color=color, fontsize=fs, fontweight=weight, va="center",
                     annotation_clip=False)
    endlabel(ax, cur_value, f"Depot {cur_value:,.0f} €".replace(",", "."), BLACK, "bold", 11)
    endlabel(ax, invested, f"eingezahlt {invested:,.0f} €".replace(",", "."), BLUE)
    if bench is not None:
        endlabel(ax, float(bench.iloc[-1]),
                 f"MSCI World {float(bench.iloc[-1]):,.0f} €".replace(",", "."), GREY)

    ax.set_title("Entwicklung meines Aktien-Portfolios", fontsize=18, fontweight="bold",
                 loc="left", pad=46)
    col = GREEN if total_pl >= 0 else RED
    ax.text(0.0, 1.028,
            f"Gesamt-Gewinn: +{total_pl:,.0f} €".replace(",", ".") +
            f"   ·   Jahresrendite {r_port*100:+.1f} % p.a." +
            (f"   ·   MSCI World wäre {(float(bench.iloc[-1])-cur_value):+,.0f} € gewesen".replace(",", ".")
             if bench is not None else ""),
            transform=ax.transAxes, fontsize=13, color=col, fontweight="bold")
    ax.yaxis.set_major_formatter(eur_fmt)
    ax.grid(True, color="#e6e6e6", lw=0.7)
    ax.legend(loc="upper left", framealpha=0.92, fontsize=10.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    # ===================== Panel 2: reine Gewinn/Verlust-Kurve =====================
    ax2.set_facecolor("#fafafa")
    ax2.axhline(0, color="#555555", lw=1.0)
    ax2.fill_between(x, 0, profit, where=(profit >= 0), color=GREEN, alpha=0.30, interpolate=True)
    ax2.fill_between(x, 0, profit, where=(profit < 0), color=RED, alpha=0.30, interpolate=True)
    ax2.plot(x, profit, color=BLACK, lw=1.8)
    if bprofit is not None:
        ax2.plot(x, bprofit, color=GREY, lw=1.3, ls="--")
    endlabel(ax2, float(profit.iloc[-1]),
             f"+{float(profit.iloc[-1]):,.0f} €".replace(",", "."), col, "bold", 11)
    ax2.set_title("Gewinn / Verlust gegenüber eingezahltem Geld (über die Zeit)",
                  fontsize=12, loc="left", color="#333333", pad=6)
    ax2.yaxis.set_major_formatter(eur_fmt)
    ax2.grid(True, color="#e6e6e6", lw=0.7)
    for s in ("top", "right"):
        ax2.spines[s].set_visible(False)

    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.xaxis.set_minor_locator(mdates.MonthLocator((1, 4, 7, 10)))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.set_xlim(x[0], x[-1] + pd.Timedelta(days=40))

    fig.subplots_adjust(left=0.07, right=0.86, top=0.87, bottom=0.06)
    fig.savefig(OUT_PNG, dpi=150, facecolor="white")


_LOGO_CACHE = {}
def logo_data_uri(max_h=96):
    """flatalyzer-Logo als base64-data-URI (verkleinert, Weiss -> transparent),
    sodass es auf jedem Hintergrund sauber schwebt. None, wenn die Datei fehlt."""
    if max_h in _LOGO_CACHE:
        return _LOGO_CACHE[max_h]
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "assets", "flatalyzer_logo.png")
    uri = None
    if os.path.exists(path):
        try:
            import base64, io
            try:
                from PIL import Image
                im = Image.open(path).convert("RGBA")
                w, h = im.size
                if h > max_h:
                    im = im.resize((max(1, round(w * max_h / h)), max_h), Image.LANCZOS)
                px = im.load()
                W, H = im.size
                for y in range(H):
                    for x in range(W):
                        r, g, b, a = px[x, y]
                        if r > 244 and g > 244 and b > 244:
                            px[x, y] = (r, g, b, 0)
                buf = io.BytesIO()
                im.save(buf, format="PNG", optimize=True)
                data = buf.getvalue()
            except Exception:
                with open(path, "rb") as f:
                    data = f.read()
            uri = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        except Exception:
            uri = None
    _LOGO_CACHE[max_h] = uri
    return uri


def _brand_html():
    uri = logo_data_uri(96)
    logo = (f'<img class="logo" src="{uri}" alt="Flatalyzer">' if uri
            else '<span class="brand-fallback">Flatalyzer</span>')
    return logo + '<span class="brand-tag">Portfolio-Auswertung</span>'


def render_html(payload):
    """Vollständiges Report-HTML aus dem Payload (Logo/Brand + Daten eingesetzt)."""
    return (_HTML_TEMPLATE
            .replace("__BRAND__", _brand_html())
            .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False)))


def export_html(payload):
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(render_html(payload))


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio-Report</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>
  :root{ --green:#0a9d57; --green-bg:#e7f6ee; --red:#dc3a2c; --red-bg:#fdeceb;
         --blue:#2563b6; --blue-bg:#e9f0fb; --amber:#b7791f;
         --ink:#14161b; --muted:#6b7280; --muted-2:#9097a1;
         --line:#e6e8ee; --line-soft:#eef0f4; --bg:#f3f5f8; --card:#ffffff; --hover:#f6f8fc;
         --shadow:0 1px 2px rgba(16,24,40,.05),0 1px 2px rgba(16,24,40,.03);
         --radius:12px; }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--ink);
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       font-feature-settings:"tnum" 1;-webkit-font-smoothing:antialiased;font-size:14px}
  .right,.val,.kpi-val,.kpi-sub .v,.metric .val,td.right,.tnum{font-variant-numeric:tabular-nums}
  /* ---- Topbar ---- */
  .topbar{position:sticky;top:0;z-index:50;background:rgba(255,255,255,.86);
          -webkit-backdrop-filter:saturate(180%) blur(10px);backdrop-filter:saturate(180%) blur(10px);
          border-bottom:1px solid var(--line)}
  .topbar-in{max-width:1240px;margin:0 auto;padding:11px 24px;display:flex;align-items:center;gap:14px}
  .brand{display:flex;align-items:center;gap:12px;min-width:0}
  .brand .logo{height:30px;width:auto;display:block;flex:0 0 auto}
  .brand-fallback{font-size:19px;font-weight:800;letter-spacing:-.4px;color:var(--ink)}
  .brand-tag{font-size:12px;color:var(--muted);font-weight:500;padding-left:13px;
             border-left:1px solid var(--line);white-space:nowrap}
  .topbar .spacer{flex:1}
  .topbar .meta{font-size:12px;color:var(--muted);text-align:right;line-height:1.4}
  .topbar .meta b{color:var(--ink);font-weight:600}
  .wrap{max-width:1240px;margin:0 auto;padding:22px 24px 80px}
  h1{font-size:24px;font-weight:800;margin:0 0 2px;letter-spacing:-.4px}
  h2{font-size:14px;font-weight:700;margin:32px 0 4px;letter-spacing:-.1px;display:flex;align-items:center;gap:8px}
  h2:before{content:"";width:3px;height:14px;border-radius:2px;background:var(--blue);flex:0 0 auto}
  .hint{color:var(--muted);font-size:12.5px;margin:0 0 12px}
  /* ---- Hero ---- */
  .hero{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);
        padding:18px 22px;display:grid;grid-template-columns:1fr auto;gap:20px;align-items:center;margin-top:4px}
  .kpi-lbl{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;color:var(--muted)}
  .kpi-val{font-size:30px;font-weight:800;letter-spacing:-.9px;margin-top:3px;line-height:1}
  .kpi-sub{margin-top:14px;display:flex;gap:26px;flex-wrap:wrap}
  .kpi-sub .k{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:3px}
  .kpi-sub .v{font-size:16px;font-weight:700;letter-spacing:-.3px;display:flex;align-items:center;gap:8px}
  .spark{width:340px;height:88px;display:block}
  /* ---- Metric strip ---- */
  .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));gap:11px;margin-top:11px}
  .metric{background:var(--card);border:1px solid var(--line);border-radius:11px;box-shadow:var(--shadow);
          padding:13px 15px;transition:border-color .15s}
  .metric:hover{border-color:#d4d8e2}
  .metric .lbl{font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
  .metric .val{font-size:19px;font-weight:750;letter-spacing:-.4px;margin-top:6px}
  .metric .note{font-size:11px;color:var(--muted);margin-top:4px;line-height:1.35}
  .pos{color:var(--green)} .neg{color:var(--red)} .mut{color:var(--muted)}
  .pill{display:inline-flex;align-items:center;padding:2px 7px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:-.1px}
  .pill.up{background:var(--green-bg);color:var(--green)} .pill.down{background:var(--red-bg);color:var(--red)}
  /* ---- Banner / Cashflow ---- */
  .divstrip{margin:14px 0 0;background:var(--green-bg);border:1px solid #cce8d4;border-radius:11px;
            padding:11px 16px;font-size:13px;display:flex;flex-wrap:wrap;gap:5px 18px;align-items:baseline}
  .divstrip b{font-size:14px}
  .warnbanner{margin:14px 0 0;background:var(--red-bg);border:1px solid #f3c9c9;border-radius:11px;
              padding:12px 16px;font-size:12.5px;color:#8a2a22}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:11px;margin:10px 0 6px}
  .cards3{grid-template-columns:repeat(3,1fr)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:11px;box-shadow:var(--shadow);padding:14px 16px}
  .card .lbl{color:var(--muted);font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
  .card .val{font-size:20px;font-weight:750;letter-spacing:-.4px;margin-top:6px}
  .card .note{font-size:11px;color:var(--muted);margin-top:4px}
  /* ---- Panel & allocation donuts ---- */
  .panel{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);
         padding:10px 10px 4px;position:relative}
  .alloc-grid{display:grid;grid-template-columns:1.5fr 1fr 1fr;gap:11px}
  .donut-card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);
              padding:12px 14px 10px}
  .dc-h{font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:6px}
  .dc-body{display:flex;gap:12px;align-items:center}
  #allocPie{width:184px;height:196px;flex:0 0 auto}
  #currencyChart,#sectorChart{width:100%;height:212px}
  .ranklist{flex:1;min-width:0;max-height:196px;overflow:auto;display:flex;flex-direction:column;gap:9px;padding-right:2px}
  .rl .top{display:flex;align-items:center;gap:7px;font-size:11.5px;line-height:1.1}
  .rl .top .sw{width:9px;height:9px;border-radius:2px;flex:0 0 auto}
  .rl .top .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
  .rl .top .pc{color:var(--muted);font-variant-numeric:tabular-nums;flex:0 0 auto}
  .rl .bar{height:3px;border-radius:2px;background:var(--line-soft);margin-top:4px;overflow:hidden}
  .rl .bar>span{display:block;height:100%;border-radius:2px}
  /* ---- Composition side pie ---- */
  #stackPieList{max-height:230px;overflow:auto;padding:2px 2px 4px}
  .pl{display:flex;align-items:center;gap:6px;font-size:11px;padding:1px 0}
  .pl .sw{width:9px;height:9px;border-radius:2px;flex:0 0 auto}
  .pl .pn{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .pl .pv{font-variant-numeric:tabular-nums}
  .pl .pp{color:var(--muted);width:32px;text-align:right}
  /* ---- Buttons / chips ---- */
  .btnbar{display:flex;justify-content:flex-end;margin:0 0 8px}
  .btn{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:7px 15px;
       font-size:12px;font-weight:600;color:var(--ink);cursor:pointer;transition:background .15s,border-color .15s}
  .btn:hover{background:var(--blue-bg);border-color:#bcd0ec}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 6px}
  .chip{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;padding:4px 10px;border-radius:7px;
        border:1px solid var(--line);background:var(--card);cursor:pointer;user-select:none;white-space:nowrap;
        transition:border-color .15s,opacity .15s}
  .chip:hover{border-color:#c8cdda;background:var(--hover)}
  .chip .sw{width:9px;height:9px;border-radius:2px;flex:0 0 auto}
  .chip.off{opacity:.4;text-decoration:line-through}
  /* ---- Composition ---- */
  .composition{display:flex;gap:12px;align-items:flex-start}
  .sidepie{width:280px;flex:0 0 auto;padding:12px 12px 8px}
  .sidepie-title{font-size:12px;color:var(--muted);font-weight:600;margin:2px 2px 6px;text-align:center}
  .detailwrap{padding:6px 4px 10px}
  .detailcard{background:#fafbfc;border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .detailcard .dh{font-size:13.5px;font-weight:700;margin-bottom:8px}
  .dtbl{font-size:12.5px;box-shadow:none;table-layout:fixed}
  .dtbl th,.dtbl td{padding:7px 8px;white-space:normal}
  .dtbl td.dt{white-space:nowrap}
  .dtbl th{background:#f1f3f6;vertical-align:bottom;font-size:11px}
  .dev-note{color:var(--muted);font-size:11.5px;cursor:help;border-bottom:1px dotted var(--muted-2)}
  .dev-part{display:block;color:var(--muted);font-size:10px;margin-top:2px;cursor:help}
  .dtbl td[title]{cursor:help}
  .dsum td{font-weight:700;border-top:2px solid var(--line);background:#f1f3f6}
  td.detailcell{padding:0;background:#fafbfc}
  #tbl tbody tr.detailrow,#tbl tbody tr.detailrow:hover{cursor:default;background:#fafbfc}
  .posrow.open td{background:var(--blue-bg)}
  /* ---- Callouts ---- */
  .callouts{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:6px}
  .callout{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px;box-shadow:var(--shadow)}
  .callout h3{margin:0 0 10px;font-size:13.5px;font-weight:700}
  .callout .row{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px solid var(--line-soft);font-size:13px}
  .callout .row:first-of-type{border-top:none}
  .callout .row .k{color:var(--muted)}
  .callout .row .v{font-weight:600;text-align:right}
  /* ---- Table ---- */
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
        border-radius:var(--radius);overflow:hidden;font-size:13px;box-shadow:var(--shadow)}
  th,td{padding:9px 13px;text-align:right;white-space:nowrap}
  th{background:#f5f6f9;color:var(--muted);font-weight:600;font-size:11px;cursor:pointer;
     border-bottom:1px solid var(--line);user-select:none;text-transform:uppercase;letter-spacing:.3px}
  th:hover{color:var(--ink)}
  th.l,td.l{text-align:left}
  tbody tr:nth-child(even){background:#fafbfc}
  #tbl tbody tr{cursor:pointer}
  tbody tr:hover{background:var(--blue-bg)}
  td.name{font-weight:600}
  #tbl{table-layout:fixed}
  #tbl th{white-space:normal}
  #tbl td.name{white-space:normal;overflow-wrap:break-word}
  #tbl td.detailcell{overflow:hidden}
  .badge{font-size:11px;padding:2px 8px;border-radius:999px;font-weight:600}
  .b-open{background:var(--blue-bg);color:var(--blue)} .b-part{background:#fff3da;color:#9a6b13}
  .b-sold{background:#eef0f2;color:var(--muted)}
  tfoot td{font-weight:700;border-top:2px solid var(--line);background:#f5f6f9}
  .cvpct{font-size:10.5px;color:var(--muted);font-weight:400;margin-top:1px;font-variant-numeric:tabular-nums}
  tfoot .cvpct{font-weight:600}
  .right{font-variant-numeric:tabular-nums}
  /* ---- Datenübersicht (aufklappbar) ---- */
  .data-ov{margin-top:32px;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
           box-shadow:var(--shadow);overflow:hidden}
  .data-ov>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:12px;
                   padding:14px 18px;user-select:none;font-size:13.5px}
  .data-ov>summary::-webkit-details-marker{display:none}
  .data-ov>summary:hover{background:var(--hover)}
  .dov-ti{font-weight:700}
  .dov-meta{color:var(--muted);font-size:12px;flex:1}
  .dov-chev{color:var(--muted);font-size:12px;transition:transform .15s}
  .data-ov[open] .dov-chev{transform:rotate(90deg)}
  .dov-body{padding:4px 18px 18px;border-top:1px solid var(--line-soft)}
  .dov-srcs{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 14px}
  .dov-src{display:inline-flex;align-items:center;gap:7px;font-size:12px;padding:6px 12px;border-radius:8px;
           border:1px solid var(--line);background:#fafbfc}
  .dov-src .dot{width:8px;height:8px;border-radius:999px;flex:0 0 auto}
  .dov-src .dot.on{background:var(--green)} .dov-src .dot.off{background:var(--muted-2)}
  .dov-tbl{font-size:12.5px;box-shadow:none;border-color:var(--line-soft)}
  .dov-tbl td,.dov-tbl th{padding:7px 10px}
  .dov-tbl th{font-size:10.5px}
  .dov-warn{background:var(--red-bg);border:1px solid #f3c9c9;border-radius:9px;padding:11px 14px;
            font-size:12.5px;color:#8a2a22;margin:4px 0 14px}
  .dov-warn b{font-weight:700}
  .dov-mut{color:var(--muted)}
  @media(max-width:620px){.brand-tag{display:none}}
  footer{color:var(--muted);font-size:12px;margin-top:36px;padding-top:20px;
         border-top:1px solid var(--line);line-height:1.6}
  /* ---- Benchmark / tax ---- */
  .cards2{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin:10px 0 6px}
  .bench-header{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin:32px 0 4px;flex-wrap:wrap}
  .bench-header h2{margin:0}
  .bench-sel{font-size:12.5px;padding:7px 14px;border:1px solid var(--line);border-radius:8px;
             background:var(--card);color:var(--ink);cursor:pointer;outline:none;font-weight:600;
             box-shadow:var(--shadow);transition:border-color .15s,box-shadow .15s}
  .bench-sel:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(37,99,182,.14)}
  .tax-tbl{font-size:13px}
  .tax-tbl th{font-size:11px}
  .tax-tbl td{padding:9px 12px}
  .dd-chart-wrap{display:flex;gap:12px;align-items:stretch;margin:10px 0 6px}
  @media(max-width:1040px){.alloc-grid{grid-template-columns:1fr}#allocPie{width:200px}}
  @media(max-width:760px){.cards{grid-template-columns:repeat(2,1fr)}.callouts{grid-template-columns:1fr}
    .composition{flex-direction:column}.sidepie{width:100%}.cards2{grid-template-columns:1fr}
    .hero{grid-template-columns:1fr}.spark{width:100%}
    .bench-header{flex-direction:column;align-items:stretch;gap:8px}.dd-chart-wrap{flex-direction:column}}
</style>
</head>
<body>
<header class="topbar"><div class="topbar-in">
  <div class="brand">__BRAND__</div>
  <div class="spacer"></div>
  <div class="meta" id="topMeta"></div>
</div></header>
<div class="wrap">
  <div id="warnbanner"></div>

  <div class="hero" id="hero"></div>
  <div class="metrics" id="metrics"></div>
  <div class="divstrip" id="divbanner"></div>
  <div id="cashflow"></div>

  <div id="allocSec">
    <h2>Aufteilung des Depots</h2>
    <p class="hint">Verteilung des heutigen Depotwerts nach Position, Handelswährung und Sektor (Quelle: Yahoo Finance).</p>
    <div class="alloc-grid">
      <div class="donut-card" id="posCard">
        <div class="dc-h">Positionen</div>
        <div class="dc-body"><div id="allocPie"></div><div id="allocList" class="ranklist"></div></div>
      </div>
      <div class="donut-card" id="currCard">
        <div class="dc-h">Handelswährungen</div>
        <div id="currencyChart"></div>
      </div>
      <div class="donut-card" id="sectorCard">
        <div class="dc-h">Sektoren</div>
        <div id="sectorChart"></div>
      </div>
    </div>
  </div>

  <h2>Positionen filtern</h2>
  <p class="hint">Wirkt auf die <b>Zeitdiagramme unten</b>: ausgeblendete Titel werden herausgerechnet, ihre Käufe/Verkäufe nicht mehr markiert.</p>
  <div class="btnbar" style="justify-content:flex-start"><button class="btn" id="stackAll">Alle</button><button class="btn" id="stackNone">Keine</button></div>
  <div id="filterChips" class="chips"></div>

  <div class="bench-header">
    <div><h2>Entwicklung über die Zeit</h2>
    <p class="hint" style="margin:4px 0 0">Depotwert vs. eingezahltes Geld vs. gewählter Benchmark. Dreiecke markieren Käufe (grün) und Verkäufe (rot).</p></div>
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">
      <label style="font-size:12px;color:var(--muted)">Benchmark</label>
      <select id="benchSel" class="bench-sel">
        <option value="msci">MSCI World (IWRD)</option>
        <option value="sp500">S&amp;P 500 (SPY)</option>
        <option value="none">Ohne Benchmark</option>
      </select>
    </div>
  </div>
  <div class="panel" style="margin-bottom:14px"><div id="lineChart" style="height:440px"></div></div>

  <h2>Gewinn / Verlust gegenüber eingezahltem Geld (über die Zeit)</h2>
  <p class="hint">Reiner Gewinn/Verlust im Zeitverlauf: Depotwert minus eingezahltes Geld. <span class="pos">Grün</span> = im Plus, <span class="neg">rot</span> = im Minus. Dreiecke/Linien markieren Käufe und Verkäufe.</p>
  <div class="panel"><div id="profitChart" style="height:300px"></div></div>

  <h2>Woraus bestand mein Vermögen? (pro Aktie, über die Zeit)</h2>
  <p class="hint">Gestapelter Marktwert je Position — zu jedem Zeitpunkt sind alle damals gehaltenen Titel zu sehen. Die Torte rechts zeigt die Aufteilung zum zuletzt angefahrenen Zeitpunkt (bleibt stehen, bis man wieder hineinfährt); aufgeführt sind nur Positionen, die es damals im Depot gab. Senkrechte Linien markieren Käufe/Verkäufe.</p>
  <div class="composition">
    <div class="panel" style="flex:1;min-width:0"><div id="stackChart" style="height:460px"></div></div>
    <div class="panel sidepie"><div class="sidepie-title" id="stackPieTitle"></div><div id="stackPieChart"></div><div id="stackPieList"></div></div>
  </div>

  <h2>Dividendenhistorie</h2>
  <p class="hint" id="divHistHint">Monatliche Ausschüttungen über die Zeit.</p>
  <div class="panel" id="divHistWrap"><div id="divHistChart" style="height:260px"></div></div>

  <h2>Beste Entscheidung &amp; größter Fehler</h2>
  <div class="callouts" id="callouts"></div>

  <h2>Steuerübersicht (Schätzung)</h2>
  <p class="hint">Realisierte Kursgewinne je Jahr aus den Transaktionen (FIFO) — keine Anlageberatung, kein Steuerberater.</p>
  <div id="taxSection"></div>

  <h2>Ergebnis je Aktie</h2>
  <p class="hint">Spaltenkopf anklicken zum Sortieren, <b>eine Zeile anklicken</b> klappt die Kauf-/Verkauf-Historie direkt darunter auf — mit der <b>Wertentwicklung jedes einzelnen Kaufs bis heute</b> und dem realisierten Gewinn je Verkauf (jeweils in € und %). „Verkauf-Timing": was die verkauften Stücke heute wert wären — <span class="pos">grün</span> = guter Ausstieg, <span class="neg">rot</span> = zu früh verkauft.</p>
  <div class="btnbar"><button class="btn" id="csvBtn">Als CSV herunterladen</button></div>
  <table id="tbl">
    <colgroup><col style="width:22%"><col style="width:12%"><col style="width:17%"><col style="width:12%"><col style="width:15%"><col style="width:11%"><col style="width:11%"></colgroup>
    <thead><tr>
      <th class="l" data-k="name">Aktie</th>
      <th class="l" data-k="status">Status</th>
      <th data-k="cur_value" title="Aktueller Marktwert der noch gehaltenen Stücke und sein Anteil am gesamten Depotwert">Wert heute</th>
      <th data-k="dividends" title="Brutto-Dividenden, aus Ausschüttungshistorie geschätzt">Dividende</th>
      <th data-k="total">Gewinn/Verlust</th>
      <th data-k="ret_pct">Rendite</th>
      <th data-k="timing">Verkauf-Timing</th>
    </tr></thead>
    <tbody></tbody>
    <tfoot></tfoot>
  </table>

  <details class="data-ov" id="dataOv">
    <summary>
      <span class="dov-ti">Welche Daten sind importiert?</span>
      <span class="dov-meta" id="dovMeta"></span>
      <span class="dov-chev">▸</span>
    </summary>
    <div class="dov-body" id="dovBody"></div>
  </details>

  <footer id="foot"></footer>
</div>

<script>
const D = __PAYLOAD__;
const nf2 = new Intl.NumberFormat('de-DE',{minimumFractionDigits:2,maximumFractionDigits:2});
const nf0 = new Intl.NumberFormat('de-DE',{maximumFractionDigits:0});
const e0 = x => nf2.format(x)+' €';                                   // max. 2 Nachkommastellen
const e0r = x => nf0.format(x)+' €';                                  // gerundet, ganze €
const es = x => (x>=0?'+':'−')+nf2.format(Math.abs(x))+' €';
const ps = x => (x>=0?'+':'−')+nf2.format(Math.abs(x))+' %';
const ps1 = ps;
const cls = x => x>=0?'pos':'neg';

/* ---- Mini-Sparkline (Inline-SVG) für die Hero-Karte ---- */
function sparkline(vals,w,h,color){
  if(!vals||vals.length<2) return '';
  const mn=Math.min(...vals), mx=Math.max(...vals), rng=(mx-mn)||1, n=vals.length;
  const X=i=>(i/(n-1))*w, Y=v=>h-2-((v-mn)/rng)*(h-4);
  let d='M'+X(0).toFixed(1)+','+Y(vals[0]).toFixed(1);
  for(let i=1;i<n;i++) d+='L'+X(i).toFixed(1)+','+Y(vals[i]).toFixed(1);
  const area=d+'L'+w.toFixed(1)+','+h+'L0,'+h+'Z';
  const gid='sg'+Math.random().toString(36).slice(2,7);
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0" stop-color="${color}" stop-opacity=".20"/>
      <stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    <path d="${area}" fill="url(#${gid})"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

/* ---- Kopfzeile, Hero & Metrik-Leiste ---- */
document.getElementById('topMeta').innerHTML = 'Stand <b>'+D.today+'</b> · '+D.positions.length+' Positionen';
if(D.incomplete && D.incomplete.length){
  document.getElementById('warnbanner').innerHTML =
    '<b>Unvollständige Historie:</b> bei folgenden Titeln wurde mehr verkauft als gekauft – '+
    'der Kauf liegt vermutlich vor dem Export. Sie sind hier ausgeschlossen (Einstand unbekannt): <b>'+
    D.incomplete.join(', ')+'</b>. Für vollständige Zahlen den Flatex-Export ab Depoteröffnung verwenden.';
}
const s = D.stats;

/* Hero: Depotwert groß + Kennzahlen + Sparkline der Depotentwicklung */
const totPct = s.invested>0 ? s.total_pl/s.invested*100 : 0;
const sparkColor = s.total_pl>=0 ? '#0a9d57' : '#dc3a2c';
document.getElementById('hero').innerHTML =
  `<div>
     <div class="kpi-lbl">Depotwert heute</div>
     <div class="kpi-val">${e0(s.cur_value)}</div>
     <div class="kpi-sub">
       <div><div class="k">Gesamt-Gewinn</div><div class="v ${cls(s.total_pl)}">${es(s.total_pl)} <span class="pill ${s.total_pl>=0?'up':'down'}">${ps(totPct)}</span></div></div>
       <div><div class="k">Jahresrendite p.a.</div><div class="v ${cls(s.xirr)}">${ps(s.xirr)}</div></div>
       <div><div class="k">Eingezahlt netto</div><div class="v">${e0(s.invested)}</div></div>
     </div>
   </div>
   <div>${sparkline(D.line.depot,340,88,sparkColor)}</div>`;

/* Metrik-Leiste: kompakte Kacheln */
function fmtDays(d){
  if(!d||d<1) return '–';
  const y=Math.floor(d/365), m=Math.floor((d%365)/30);
  return (y>0?y+' J ':'')+m+' M';
}
const heldPos=D.positions.filter(r=>r.holding_days>0);
const avgH=heldPos.length?Math.round(heldPos.reduce((a,r)=>a+r.holding_days,0)/heldPos.length):0;
const portDivYield=s.cur_value>0?(s.dividends/s.cur_value*100):0;
const ddv=(D.portfolio_dd&&D.portfolio_dd.max_dd!=null)?D.portfolio_dd.max_dd:null;
const metrics=[
  {id:'benchMetric', lbl:'vs. Benchmark', val:'–', cls:'', note:''},
  {lbl:'Realisiert', val:es(s.realized), cls:cls(s.realized), note:'bereits verkauft'},
  {lbl:'Offen (Buchwert)', val:es(s.unreal), cls:cls(s.unreal), note:'noch im Depot'},
  {lbl:'Max. Drawdown', val:ddv!=null?ps(ddv):'–', cls:(ddv!=null&&ddv<0)?'neg':'', note:'Höchststand → Tief'},
  {lbl:'Ø Haltedauer', val:fmtDays(avgH), cls:'', note:'offene Positionen'},
  {lbl:'Div.-Rendite', val:portDivYield>0?('+'+portDivYield.toFixed(2)+' %'):'–', cls:portDivYield>0?'pos':'', note:'12 M / Depotwert'},
  {lbl:'Dividenden gesamt', val:e0(s.dividends), cls:'', note:s.div_real?'netto lt. Konto':'brutto geschätzt'},
];
document.getElementById('metrics').innerHTML=metrics.map(m=>
  `<div class="metric"${m.id?` id="${m.id}"`:''}><div class="lbl">${m.lbl}</div><div class="val ${m.cls||''}">${m.val}</div><div class="note">${m.note}</div></div>`).join('');

const divNote = s.div_real ? 'netto, tatsächlich gutgeschrieben lt. Konto'
                           : 'brutto, geschätzt, vor Quellensteuer/KESt';
document.getElementById('divbanner').innerHTML =
  `<span>Erhaltene <b>Dividenden: ${e0(s.dividends)}</b> (${divNote})</span>`+
  `<span style="color:var(--muted)">— Gesamtertrag inkl. Dividenden: <b class="${cls(s.total_pl+s.dividends)}">${es(s.total_pl+s.dividends)}</b></span>`;

if(s.deposits!=null){
  const net = s.deposits - s.withdrawals;
  document.getElementById('cashflow').innerHTML =
    `<h2>Ein- und Auszahlungen (Flatex-Konto)</h2>`+
    `<div class="cards cards3">`+
    `<div class="card"><div class="lbl">Auf Flatex eingezahlt</div><div class="val">${e0(s.deposits)}</div><div class="note">Überweisungen von deinen Konten</div></div>`+
    `<div class="card"><div class="lbl">Auf andere Konten ausgezahlt</div><div class="val">${e0(s.withdrawals)}</div><div class="note">Abhebungen von Flatex</div></div>`+
    `<div class="card"><div class="lbl">Netto eingezahlt</div><div class="val">${e0(net)}</div><div class="note">eingezahlt − ausgezahlt</div></div>`+
    `</div>`+
    `<p class="hint">Zusätzlich auf dem Konto: Bardividenden <span class="pos">${e0(s.dividends)}</span> · `+
    `Zinsen/Gebühren <span class="neg">${e0(s.interest+s.fees)}</span>. `+
    `Aktueller Depotwert ${e0(s.cur_value)} bei netto ${e0(net)} eingezahltem Geld.</p>`;
}

const palette = ['#2c5fa8','#1a9850','#e08214','#d12c20','#7b3294','#0d8b8b','#b8860b',
                 '#5b8a2b','#c2569a','#3b7dd8','#8c6d31','#557799','#999999','#a0522d',
                 '#2e8b8b','#9467bd','#d6604d','#4a8c2a','#cc6699','#1f6fb2','#777744',
                 '#6a3d9a','#3aa0a0','#bdb000','#888888'];
const stackColor = i => palette[i%palette.length];
const _evShapes = {};   // gespeicherte Event-Shapes je Chart (für Crosshair-Overlay)
const XHAIR = {type:'line',xref:'x',yref:'paper',y0:0,y1:1,
  line:{color:'#9aa0a6',width:1,dash:'dot'},opacity:0.65};

/* ---- Ereignis-Dekoration (Käufe/Verkäufe), optional auf sichtbare Titel gefiltert ---- */
const evColorBS = (buy,sell) => (buy>0 && sell>0) ? '#9a6b13' : (buy>0 ? '#1a9850' : '#d12c20');
function buildEvents(vis){          // vis: Set sichtbarer Stack-Labels oder null (= alle)
  const out=[];
  (D.events||[]).forEach(e=>{
    const items=e.items.filter(it=>!vis||vis.has(it.label));
    if(!items.length) return;
    let buy=0,sell=0; items.forEach(it=>{ if(it.kind==='Kauf') buy+=it.amt; else sell+=it.amt; });
    out.push({date:e.date,items,buy,sell});
  });
  return out;
}
function evShapes(E){
  return E.map(e=>({type:'line',x0:e.date,x1:e.date,yref:'paper',y0:0,y1:1,
    line:{color:evColorBS(e.buy,e.sell),width:1,dash:'dot'},opacity:0.22,layer:'below'}));
}
function evMarker(E){
  const txt=E.map(e=>{
    const head=e.date.split('-').reverse().join('.');
    const it=e.items.slice(0,10).map(i=>i.kind+' '+i.name+' '+e0(i.amt)).join('<br>');
    return '<b>'+head+'</b><br>'+it+(e.items.length>10?'<br>…':'');});
  return {x:E.map(e=>e.date),y:E.map(()=>0),mode:'markers',name:'Ereignisse',showlegend:false,
    marker:{symbol:'triangle-up',size:9,color:E.map(e=>evColorBS(e.buy,e.sell)),line:{color:'#fff',width:1}},
    text:txt,hovertemplate:'%{text}<extra></extra>',cliponaxis:false};
}
const allEvents = buildEvents(null);

/* ---- Globaler Filter: Sichtbarkeit der Positionen (wirkt auf ALLE Diagramme) ---- */
window._selBench = localStorage.getItem('benchSel') || 'msci';   // vor erstem Render!
const stackVis = {};
D.stack.labels.forEach(l=>{ stackVis[l]=true; });
function visibleSet(){ return new Set(D.stack.labels.filter(l=>stackVis[l])); }
function allVisible(){ return D.stack.labels.every(l=>stackVis[l]); }
/* Per-Datum-Summe über die aktuell sichtbaren Positionen (für die neu berechneten Linien). */
function sumVisible(arr){
  if(!arr) return null;
  const n=D.dates.length, out=new Array(n).fill(0);
  D.stack.labels.forEach((lab,i)=>{ if(!stackVis[lab]||!arr[i]) return;
    const s=arr[i]; for(let k=0;k<n;k++) out[k]+=s[k]||0; });
  return out;
}
/* Depot-/Eingezahlt-/Benchmark-/G-V-Linien aus den sichtbaren Positionen.
   Benchmark wechselt je nach window._selBench (msci / sp500 / none). */
function filteredLines(){
  const all=allVisible() || !D.stack.invested;
  const depot        = all?D.line.depot   :sumVisible(D.stack.series);
  const invested     = all?D.line.invested:sumVisible(D.stack.invested); // netto (für G/V-Diff)
  const invested_cost= all?(D.line.invested_cost||D.line.invested)      // Kostenbasis (für Chart-Linie)
                          :sumVisible(D.stack.invested);
  const diff    = depot.map((v,i)=>v-invested[i]);
  const bk=window._selBench;
  let bench=null;
  if(bk==='msci' && D.line.bench)
    bench = all ? D.line.bench : (D.stack.bench ? sumVisible(D.stack.bench) : D.line.bench);
  else if(bk==='sp500' && D.line.bench2)
    bench = all ? D.line.bench2 : (D.stack.bench2 ? sumVisible(D.stack.bench2) : D.line.bench2);
  return {depot,invested,invested_cost,diff,bench};
}
function benchLabel(){ return window._selBench==='sp500'?'S&P 500 (hypothetisch)':'MSCI World (hypothetisch)'; }

const baseLayout = {separators:',.',margin:{l:72,r:22,t:16,b:42},paper_bgcolor:'#fff',plot_bgcolor:'#fafbfc',
  font:{family:'-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif',size:12,color:'#333'},
  hovermode:'x unified',
  hoverlabel:{bgcolor:'#fff',bordercolor:'#dde0e8',font:{size:12.5}},
  legend:{orientation:'h',y:1.15,x:0,bgcolor:'rgba(255,255,255,.85)',font:{size:11.5}},
  xaxis:{showgrid:false,showline:true,linecolor:'#dde0e8',
         showspikes:true,spikemode:'across',spikesnap:'data',spikedistance:-1,
         spikethickness:1,spikecolor:'#9aa0a6',spikedash:'dot',tickfont:{size:11}},
  yaxis:{ticksuffix:' €',tickformat:',.0f',gridcolor:'#eaecf0',zeroline:false,tickfont:{size:11}}};
const cfg = {responsive:true,displaylogo:false,
  modeBarButtonsToRemove:['lasso2d','select2d','autoScale2d']};

/* ---- Verlaufschart (Depot vs. eingezahlt vs. MSCI World) ---- */
function drawLine(){
  const F=filteredLines();
  const E=buildEvents(visibleSet());
  const tr=[
    {x:D.dates,y:F.invested_cost,name:'Eingezahltes Kapital (Kostenbasis)',mode:'lines',
     line:{color:'#2c5fa8',width:1.8,shape:'hv'},hovertemplate:'%{y:,.2f} €<extra>Eingezahltes Kapital</extra>'},
    {x:D.dates,y:F.depot,name:'Depotwert',mode:'lines',customdata:F.diff,
     line:{color:'#111',width:2.5},
     hovertemplate:'%{y:,.2f} €  (G/V %{customdata:+,.2f} €)<extra>Depotwert</extra>'}
  ];
  if(F.bench) tr.unshift({x:D.dates,y:F.bench,name:benchLabel(),mode:'lines',
     line:{color:'#9a9a9a',width:1.6,dash:'dash'},hovertemplate:'%{y:,.2f} €<extra>'+benchLabel()+'</extra>'});
  tr.push(evMarker(E));
  const lay=JSON.parse(JSON.stringify(baseLayout));
  lay.shapes=evShapes(E); _evShapes.lineChart=lay.shapes;
  Plotly.react('lineChart',tr,lay,cfg);
}

/* ---- Gewinn/Verlust-Kurve gegenüber eingezahltem Geld (wie im PNG) ---- */
function drawProfit(){
  const F=filteredLines();
  const E=buildEvents(visibleSet());
  const diff=F.diff;
  const pos=diff.map(v=>v>0?v:0), neg=diff.map(v=>v<0?v:0);
  const pt=[
    {x:D.dates,y:pos,mode:'lines',fill:'tozeroy',fillcolor:'rgba(26,152,80,.28)',
     line:{width:0},hoverinfo:'skip',showlegend:false},
    {x:D.dates,y:neg,mode:'lines',fill:'tozeroy',fillcolor:'rgba(209,44,32,.28)',
     line:{width:0},hoverinfo:'skip',showlegend:false},
    {x:D.dates,y:diff,mode:'lines',name:'Gewinn/Verlust',line:{color:'#111',width:2},
     hovertemplate:'%{y:+,.2f} €<extra>Gewinn/Verlust</extra>'}
  ];
  if(F.bench){
    const bp=F.bench.map((b,i)=>b-F.invested[i]);
    const bl=benchLabel().replace(' (hypothetisch)','');
    pt.push({x:D.dates,y:bp,mode:'lines',name:bl+' (G/V)',
      line:{color:'#9a9a9a',width:1.4,dash:'dash'},
      hovertemplate:'%{y:+,.2f} €<extra>'+bl+'</extra>'});
  }
  pt.push(evMarker(E));
  const lay=JSON.parse(JSON.stringify(baseLayout));
  lay.yaxis.zeroline=true; lay.yaxis.zerolinecolor='#555'; lay.yaxis.zerolinewidth=1;
  lay.legend={orientation:'h',y:1.14,x:0};
  lay.shapes=evShapes(E); _evShapes.profitChart=lay.shapes;
  Plotly.react('profitChart',pt,lay,cfg);
}

/* ---- Aktuelle Aufteilung: Donut + Rangliste ---- */
(function(){
  const cur=D.positions.filter(r=>r.cur_value>0.5).sort((a,b)=>b.cur_value-a.cur_value);
  if(!cur.length){const w=document.getElementById('allocSec'); if(w) w.style.display='none'; return;}
  const tot=cur.reduce((a,r)=>a+r.cur_value,0);
  const maxv=cur[0].cur_value;
  Plotly.newPlot('allocPie',[{type:'pie',labels:cur.map(r=>r.name),values:cur.map(r=>r.cur_value),
    marker:{colors:cur.map((_,i)=>stackColor(i)),line:{color:'#fff',width:2}},
    textinfo:'none',hole:0.62,sort:false,direction:'clockwise',rotation:0,
    hovertemplate:'%{label}<br>%{value:,.0f} \u20ac (%{percent:.1%})<extra></extra>'}],
    {separators:',.',margin:{l:4,r:4,t:4,b:4},height:196,paper_bgcolor:'#fff',showlegend:false,
     annotations:[{text:'<b>'+e0r(tot)+'</b>',showarrow:false,font:{size:12.5}}]},
    {displaylogo:false,responsive:true,displayModeBar:false});
  document.getElementById('allocList').innerHTML=cur.map((r,i)=>{
    const c=stackColor(i), p=r.cur_value/tot*100;
    return `<div class="rl"><div class="top"><span class="sw" style="background:${c}"></span>`+
      `<span class="nm">${r.name}</span><span class="pc">${p.toFixed(1)}\u00a0%</span></div>`+
      `<div class="bar"><span style="width:${(r.cur_value/maxv*100).toFixed(1)}%;background:${c}"></span></div></div>`;
  }).join('');
})();

/* ---- Stacked Composition (alle jemals gehaltenen Positionen) ----
   Es werden NUR die aktuell sichtbaren Positionen als Stack-Spuren gezeichnet. Das
   frühere visible:'legendonly' ließ ausgeblendete Titel im stackgroup und erzeugte beim
   Filtern Stacking-Artefakte (falsche Grundlinie/Flächen). y=null außerhalb der
   Haltedauer; hoverinfo:'none' unterdrückt den nativen Tooltip (sonst 0-€-Titel). */
const stackLayout = JSON.parse(JSON.stringify(baseLayout));
stackLayout.legend={orientation:'h',y:-0.16,x:0,font:{size:11}};
stackLayout.margin.b=70;
const stackEl=document.getElementById('stackChart');
let stackVisLabels=[];          // Labels der aktuell gezeichneten Spuren (Legende→Label-Mapping)
function drawStack(){
  stackVisLabels=D.stack.labels.filter(l=>stackVis[l]);
  const tr=stackVisLabels.map(lab=>{
    const i=D.stack.labels.indexOf(lab);
    return {x:D.dates,y:D.stack.series[i].map(v=>v>0?v:null),name:lab,mode:'lines',
      stackgroup:'one',line:{width:0.5,color:stackColor(i)},fillcolor:stackColor(i),hoverinfo:'none'};
  });
  const E=buildEvents(visibleSet());
  tr.push(evMarker(E));
  const lay=JSON.parse(JSON.stringify(stackLayout));
  lay.shapes=evShapes(E); _evShapes.stackChart=lay.shapes;
  Plotly.react('stackChart',tr,lay,cfg);
}

/* ---- Seitliche Torte: bleibt stehen (letzter angefahrener Zeitpunkt) ---- */
const pieChartDiv=document.getElementById('stackPieChart');
const pieListDiv=document.getElementById('stackPieList');
const pieTitle=document.getElementById('stackPieTitle');
let lastPieIdx=D.dates.length-1;
function showPie(idx){
  if(idx==null) idx=D.dates.length-1;
  lastPieIdx=idx;
  const dstr=D.dates[idx].split('-').reverse().join('.');
  let items=[];
  D.stack.labels.forEach((lab,i)=>{ if(!stackVis[lab]) return;
    const v=D.stack.series[i][idx]; if(v>0) items.push({lab,v,c:stackColor(i)}); });
  if(!items.length){
    pieTitle.textContent=dstr+' · keine sichtbare Position';
    Plotly.react(pieChartDiv,[],{margin:{t:0,b:0,l:0,r:0},height:210,width:256,paper_bgcolor:'#fff'},
      {displayModeBar:false,responsive:false});
    pieListDiv.innerHTML=''; return;
  }
  items.sort((a,b)=>b.v-a.v);
  const tot=items.reduce((s,x)=>s+x.v,0);
  pieTitle.textContent=dstr+' · '+e0(tot);
  Plotly.react(pieChartDiv,[{type:'pie',labels:items.map(x=>x.lab),values:items.map(x=>x.v),
    marker:{colors:items.map(x=>x.c),line:{color:'#fff',width:1.5}},
    textinfo:'none',hovertemplate:'%{label}<br>%{value:,.2f} € (%{percent:.1%})<extra></extra>',
    sort:true,direction:'clockwise',rotation:0,hole:0.45}],
    {separators:',.',margin:{l:4,r:4,t:4,b:4},height:210,width:256,paper_bgcolor:'#fff',showlegend:false},
    {displayModeBar:false,responsive:false});
  pieListDiv.innerHTML=items.map(x=>
    `<div class="pl"><span class="sw" style="background:${x.c}"></span>`+
    `<span class="pn">${x.lab}</span><span class="pv">${e0(x.v)}</span>`+
    `<span class="pp">${(x.v/tot*100).toFixed(1)}%</span></div>`).join('');
}

/* ---- Zentrale Filter-Chips ---- */
function renderChips(){
  document.getElementById('filterChips').innerHTML=D.stack.labels.map((l,i)=>
    `<span class="chip ${stackVis[l]?'':'off'}" data-l="${encodeURIComponent(l)}">`+
    `<span class="sw" style="background:${stackColor(i)}"></span>${l}</span>`).join('');
}
function applyFilter(){ zooming=true; drawLine(); drawProfit(); drawStack(); renderChips(); showPie(lastPieIdx); zooming=false; }

/* ---- Erst-Rendering: legt die Plotly-Graphen an, BEVOR Events gebunden werden ---- */
drawLine(); drawProfit(); drawStack(); renderChips(); showPie(lastPieIdx);

/* ---- Events binden (Graph-Divs existieren jetzt) ---- */
document.getElementById('filterChips').addEventListener('click',e=>{
  const c=e.target.closest('.chip'); if(!c) return;
  const l=decodeURIComponent(c.dataset.l); stackVis[l]=!stackVis[l]; applyFilter();
});
document.getElementById('stackAll').addEventListener('click',
  ()=>{ D.stack.labels.forEach(l=>{stackVis[l]=true;}); applyFilter(); });
document.getElementById('stackNone').addEventListener('click',
  ()=>{ D.stack.labels.forEach(l=>{stackVis[l]=false;}); applyFilter(); });
stackEl.on('plotly_legendclick',d=>{
  const l=stackVisLabels[d.curveNumber];          // Legende zeigt nur sichtbare -> Klick blendet aus
  if(l){ stackVis[l]=false; applyFilter(); }
  return false;
});
stackEl.on('plotly_legenddoubleclick',d=>{
  const l=stackVisLabels[d.curveNumber];
  if(l){
    const onlyThis=D.stack.labels.every(x=> (x===l)===!!stackVis[x]);
    D.stack.labels.forEach(x=>{ stackVis[x]= onlyThis ? true : (x===l); });
    applyFilter();
  }
  return false;
});
stackEl.on('plotly_hover',ev=>{ const p=ev.points.find(pt=>pt.data.stackgroup==='one'); if(p) showPie(p.pointIndex); });

/* ---- Hover + Pan/Zoom über alle Zeit-Diagramme synchronisieren ----
   Hover wird per Index synchronisiert: xval muss in Achseneinheiten (ms) angegeben
   werden, ein Datums-String funktioniert bei einer Datumsachse nicht. */
const SYNC=['lineChart','profitChart','stackChart'];
let syncing=false, zooming=false;
SYNC.forEach(id=>{
  const gd=document.getElementById(id);
  gd.on('plotly_hover',e=>{
    if(syncing||!e.points||!e.points.length) return;
    const p=e.points.find(pt=>pt.data.name!=='Ereignisse'&&pt.pointIndex!=null)||e.points[0];
    const idx=(p.pointIndex!=null)?p.pointIndex:p.pointNumber;
    if(idx==null) return;
    const dateStr=D.dates[idx];
    const xh=Object.assign({},XHAIR,{x0:dateStr,x1:dateStr});
    syncing=true;
    SYNC.forEach(o=>{
      if(o!==id){
        try{ Plotly.Fx.hover(o,[{curveNumber:0,pointNumber:idx}]); }catch(_){}
        try{ Plotly.relayout(o,{shapes:[...(_evShapes[o]||[]),xh]}); }catch(_){}
      }
    });
    syncing=false;
  });
  gd.on('plotly_unhover',()=>{
    if(syncing) return; syncing=true;
    SYNC.forEach(o=>{
      if(o!==id){
        try{ Plotly.Fx.unhover(o); }catch(_){}
        try{ Plotly.relayout(o,{shapes:_evShapes[o]||[]}); }catch(_){}
      }
    });
    syncing=false;
  });
  gd.on('plotly_relayout',ev=>{
    if(zooming) return;
    let upd=null;
    if(ev['xaxis.range[0]']!==undefined) upd={'xaxis.range':[ev['xaxis.range[0]'],ev['xaxis.range[1]']]};
    else if(ev['xaxis.autorange']) upd={'xaxis.autorange':true};
    if(!upd) return;
    zooming=true;
    SYNC.forEach(o=>{ if(o!==id){ try{Plotly.relayout(o,upd);}catch(_){} } });
    zooming=false;
  });
});

/* ---- Callouts ---- */
const A = D.analysis;
const row = (k,v,c)=>`<div class="row"><span class="k">${k}</span><span class="v ${c||''}">${v}</span></div>`;
let best='', worst='';
if(A.best)  best += row('Größter Gewinn', A.best.name+' &nbsp;'+es(A.best.total)+' ('+ps(A.best.ret_pct)+')','pos');
if(A.bestp && A.bestp.name!==A.best.name) best += row('Beste Rendite', A.bestp.name+' &nbsp;'+ps(A.bestp.ret_pct),'pos');
if(A.smart && A.smart.timing<-20) best += row('Bester Ausstieg', A.smart.name+' &nbsp;+'+e0(-A.smart.timing)+' Verlust gespart','pos');
if(A.worst)  worst += row('Größter Verlust', A.worst.name+' &nbsp;'+es(A.worst.total)+' ('+ps(A.worst.ret_pct)+')','neg');
if(A.worstp && A.worstp.name!==A.worst.name) worst += row('Schlechteste Rendite', A.worstp.name+' &nbsp;'+ps(A.worstp.ret_pct),'neg');
if(A.early && A.early.timing>20) worst += row('Zu früh verkauft', A.early.name+' &nbsp;'+es(-A.early.timing)+' entgangen','neg');
document.getElementById('callouts').innerHTML =
  `<div class="callout"><h3>Beste Entscheidung</h3>${best}</div>`+
  `<div class="callout"><h3>Größter Fehler</h3>${worst}</div>`;

/* ---- Tabelle ---- */
const badge = s=>({'offen':'<span class="badge b-open">offen</span>',
  'teilw. verkauft':'<span class="badge b-part">teilweise</span>',
  'verkauft':'<span class="badge b-sold">verkauft</span>'}[s]);
function timingCell(t){
  if(Math.abs(t)<1) return '<span style="color:#aaa">–</span>';
  const impact = -t; // sold-too-early -> negativ (rot), guter Ausstieg -> positiv (grün)
  return `<span class="${cls(impact)}">${es(impact)}</span>`;
}
let sortK='cur_value', sortDir=-1;
const expanded=new Set();
/* Wertentwicklung/realisierte G/V je Transaktion: Absolutbetrag + Prozent gestapelt */
function pairCell(abs,pct){
  if(abs==null) return '<span style="color:#bbb">–</span>';
  return `<span class="${cls(abs)}">${es(abs)}<br><small style="opacity:.8">${ps(pct)}</small></span>`;
}
/* Stückzahl ohne überflüssige Nachkommastellen */
function fmtQ(x){ return (Math.round((x||0)*1e4)/1e4).toLocaleString('de-DE',{maximumFractionDigits:4}); }
/* "Entwicklung bis heute"-Zelle für eine KAUF-Zeile: macht sichtbar, ob die Stücke
   dieses Kaufs (per FIFO) bereits ganz/teilweise verkauft wurden. */
function devCellBuy(t){
  const q=t.q, rem=(t.rem_q!=null)?t.rem_q:q;
  if(q>0 && rem<=q*1e-4){            // vollständig später verkauft -> auf Verkaufszeilen verweisen
    const when=t.sold_date?(' am '+t.sold_date):'';
    const tip=`Alle ${fmtQ(q)} Stück dieses Kaufs wurden später wieder verkauft${when}. `+
      `Der dabei realisierte Gewinn/Verlust steht in den Verkaufszeilen unten.`;
    return `<td class="right"><span class="dev-note" title="${tip}">↓ verkauft${when}</span></td>`;
  }
  if(t.dev_abs==null) return `<td class="right"><span style="color:#bbb">–</span></td>`;
  if(rem<q*0.9999){                  // teilweise verkauft -> Restbestand-Hinweis
    const sold=q-rem;
    const vb=(Math.abs(sold-1)<1e-9)?'Stück wurde':'Stück wurden';
    const tip=`Wertentwicklung nur der ${fmtQ(rem)} heute noch gehaltenen von ursprünglich ${fmtQ(q)} Stück. `+
      `${fmtQ(sold)} ${vb} zwischenzeitlich verkauft (deren realisierter G/V steht in den Verkaufszeilen).`;
    return `<td class="right" title="${tip}"><span class="${cls(t.dev_abs)}">${es(t.dev_abs)}`+
      `<br><small style="opacity:.8">${ps(t.dev_pct)}</small></span>`+
      `<br><small class="dev-part">${fmtQ(rem)}/${fmtQ(q)} Stk gehalten</small></td>`;
  }
  return `<td class="right">${pairCell(t.dev_abs,t.dev_pct)}</td>`;   // vollständig gehalten
}
/* Inline aufklappbare Kauf-/Verkauf-Historie je Position */
function detailHtml(r){
  if(!r.txns||!r.txns.length)
    return '<div class="detailwrap"><div class="detailcard">Keine Transaktionen erfasst.</div></div>';
  let buy=0,sell=0,realTot=0,haveReal=false;
  const body=r.txns.map(t=>{
    const cf=-t.a;                      // Verkauf = +Geld rein, Kauf = −Geld raus
    if(t.a>0) buy+=t.a; else sell+=-t.a;
    if(t.real_abs!=null){ realTot+=t.real_abs; haveReal=true; }
    const devTd=(t.k==='Kauf')?devCellBuy(t):`<td class="right">${pairCell(t.dev_abs,t.dev_pct)}</td>`;
    return `<tr>
      <td class="l dt">${t.d}</td><td class="l">${t.k}</td>
      <td class="right">${fmtQ(t.q)}</td>
      <td class="right">${t.p?nf2.format(t.p):'–'}</td>
      <td class="right ${cf>=0?'pos':'neg'}">${es(cf)}</td>
      <td class="right">${t.real_abs!=null?pairCell(t.real_abs,t.real_pct):'<span style="color:#bbb">–</span>'}</td>
      ${devTd}</tr>`;}).join('');
  function fmtHold(d){
    if(!d||d<1) return null;
    const y=Math.floor(d/365),m=Math.floor((d%365)/30);
    return (y>0?y+' J ':'')+m+' M';
  }
  const holdStr = r.holding_days>0&&r.first_buy ? r.first_buy.split('-').reverse().join('.')+(fmtHold(r.holding_days)?' ('+fmtHold(r.holding_days)+')':'') : null;
  const metaParts=[
    `Ticker: <span style="user-select:all">${r.ticker||'–'}</span>`,
    `ISIN: <span style="user-select:all">${r.isin||'–'}</span>`,
    `Kurs: ${e0(r.cur_px)}`,
  ];
  if(r.currency && r.currency!=='EUR') metaParts.push(`Währung: ${r.currency}`);
  if(r.sector) metaParts.push(`Sektor: ${r.sector}`);
  if(holdStr) metaParts.push(`Gehalten seit: ${holdStr}`);
  if(r.div_yield>0) metaParts.push(`Div.-Rendite: <span class="pos">+${nf2.format(r.div_yield)} %</span>`);
  if(r.max_dd<-1) metaParts.push(`Max. Drawdown: <span class="neg">${nf2.format(r.max_dd)} %</span>`);
  return `<div class="detailwrap"><div class="detailcard">
    <div class="dh">${r.name} – Kauf-/Verkauf-Historie</div>
    <div style="font-size:12px;color:var(--muted);margin:-2px 0 10px;line-height:1.8">${metaParts.join(' · ')}</div>
    <table class="dtbl">
    <colgroup><col style="width:13%"><col style="width:12%"><col style="width:10%"><col style="width:12%"><col style="width:15%"><col style="width:19%"><col style="width:19%"></colgroup>
    <thead><tr>
      <th class="l">Datum</th><th class="l">Aktion</th><th>Stück</th><th>Kurs</th>
      <th>Cashflow</th>
      <th title="Beim Verkauf tatsächlich realisierter Gewinn/Verlust (FIFO: älteste Stücke zuerst), in € und %.">Realisiert</th>
      <th title="Kauf: Wertentwicklung der heute noch gehaltenen Stücke dieses Kaufs bis heute. Vollständig verkaufte Käufe sind als „verkauft“ markiert (G/V in den Verkaufszeilen); bei teilweise verkauften bezieht sich der Wert nur auf den Restbestand (Stückzahl darunter). Verkauf: was die verkauften Stücke heute wert wären gegenüber dem Erlös (− = guter Ausstieg, + = zu früh verkauft). Jeweils in € und %.">Entwicklung<br>bis heute</th>
      </tr></thead>
    <tbody>${body}</tbody>
    <tfoot><tr class="dsum">
      <td class="l" colspan="5">Summe · gekauft ${e0(buy)} · verkauft ${e0(sell)}</td>
      <td class="right">${haveReal?es(realTot):''}</td>
      <td></td></tr></tfoot></table>
    <p class="hint" style="margin:8px 2px 0">„Cashflow": <span class="pos">+</span> = Geld erhalten (Verkauf), <span class="neg">−</span> = Geld eingesetzt (Kauf). „Realisiert" = beim Verkauf tatsächlich erzielter G/V (FIFO, älteste Stücke zuerst). „Entwicklung bis heute" = bei jedem <b>Kauf</b> die Wertentwicklung der heute noch gehaltenen Stücke dieses Kaufs. Wurde ein Kauf zwischenzeitlich (FIFO) <b>vollständig verkauft</b>, steht hier „<span class="dev-note">↓ verkauft</span>" mit dem Verkaufsdatum – der erzielte G/V findet sich in den Verkaufszeilen. Bei <b>teilweise verkauften</b> Käufen bezieht sich der Wert nur auf den Restbestand (kleine Stückzahl „Rest/Gesamt" darunter, Details per Maus-Hover). Bei jedem <b>Verkauf</b> der heutige Wert der verkauften Stücke gegenüber dem Erlös – jeweils in € und %. Kurs in Handelswährung.</p>
  </div></div>`;
}
function draw(){
  const rows=[...D.positions].sort((a,b)=>{
    let x=a[sortK],y=b[sortK];
    if(typeof x==='string') return sortDir*x.localeCompare(y);
    return sortDir*(x-y);
  });
  const totCur=D.positions.reduce((a,r)=>a+(r.cur_value||0),0);
  document.querySelector('#tbl > tbody').innerHTML = rows.map(r=>{
    const open=expanded.has(r.name);
    const hd=r.holding_days>0&&r.first_buy?` · seit ${r.first_buy.split('-').reverse().join('.')}`:'';
    const meta=`Ticker: ${r.ticker||'–'} · ISIN: ${r.isin||'–'} · Kurs: ${e0(r.cur_px)}${r.currency&&r.currency!=='EUR'?' · '+r.currency:''}${hd}`;
    const cvCell=r.cur_value>0.5
      ? `${e0(r.cur_value)}<div class="cvpct">${totCur>0?(r.cur_value/totCur*100).toFixed(1):'0,0'} %</div>`
      : '<span style="color:#bbb">–</span>';
    let html=`<tr class="posrow${open?' open':''}" data-name="${r.name}" title="${meta}">
      <td class="l name">${open?'▾ ':'▸ '}${r.name}</td>
      <td class="l">${badge(r.status)}</td>
      <td class="right">${cvCell}</td>
      <td class="right">${r.dividends>0.5?'<span class="pos">'+e0(r.dividends)+'</span>':'<span style="color:#bbb">–</span>'}</td>
      <td class="right ${cls(r.total)}">${es(r.total)}</td>
      <td class="right ${cls(r.ret_pct)}">${ps(r.ret_pct)}</td>
      <td class="right">${timingCell(r.timing)}</td></tr>`;
    if(open) html+=`<tr class="detailrow"><td class="detailcell" colspan="7">${detailHtml(r)}</td></tr>`;
    return html;
  }).join('');
  const T=(k)=>D.positions.reduce((a,r)=>a+r[k],0);
  document.querySelector('#tbl > tfoot').innerHTML=`<tr>
    <td class="l">GESAMT</td><td></td>
    <td class="right">${e0(T('cur_value'))}<div class="cvpct">100 %</div></td>
    <td class="right pos">${e0(T('dividends'))}</td>
    <td class="right ${cls(T('total'))}">${es(T('total'))}</td>
    <td class="right ${cls(T('total'))}">${ps(T('total')/T('buys')*100)}</td>
    <td></td></tr>`;
}
document.querySelectorAll('#tbl th').forEach(th=>th.addEventListener('click',()=>{
  const k=th.dataset.k; sortDir = (sortK===k)? -sortDir : (k==='name'?1:-1); sortK=k; draw();
}));
document.querySelector('#tbl > tbody').addEventListener('click',e=>{
  const tr=e.target.closest('tr.posrow'); if(!tr) return;
  const n=tr.dataset.name; if(expanded.has(n)) expanded.delete(n); else expanded.add(n);
  draw();
});
draw();
function sortedRows(){
  return [...D.positions].sort((a,b)=>{
    let x=a[sortK],y=b[sortK];
    if(typeof x==='string') return sortDir*x.localeCompare(y);
    return sortDir*(x-y);
  });
}
function num(v){ return (typeof v==='number') ? v.toFixed(2).replace('.',',') : String(v); }
document.getElementById('csvBtn').addEventListener('click',()=>{
  const cols=['name','status','buys','returned','cur_value','dividends','realized','unrealized','total','ret_pct','timing'];
  const head=['Aktie','Status','Eingezahlt','Erlös+Wert heute','Wert heute','Dividende','Realisiert','Unrealisiert','Gewinn/Verlust','Rendite %','Verkauf-Timing'];
  const q=s=>'"'+String(s).replace(/"/g,'""')+'"';
  let csv='﻿'+head.map(q).join(';')+'\r\n';
  sortedRows().forEach(r=>{ csv+=cols.map(k=>q(num(r[k]))).join(';')+'\r\n'; });
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));
  a.download='positionen.csv'; document.body.appendChild(a); a.click(); a.remove();
});

/* ---- Benchmark-Auswahl ---- */
window._selBench = localStorage.getItem('benchSel') || 'msci';
(function(){
  const sel=document.getElementById('benchSel');
  if(!sel) return;
  sel.value=window._selBench;
  sel.addEventListener('change',function(){
    window._selBench=this.value; localStorage.setItem('benchSel',this.value);
    updateBenchCard(); applyFilter();
  });
})();

function updateBenchCard(){
  const bk=window._selBench;
  let bv=null,bx=null,bl='Kein Benchmark';
  if(bk==='msci'){bv=s.bench;bx=s.bench_xirr;bl='MSCI World';}
  else if(bk==='sp500'){bv=s.bench2;bx=s.bench2_xirr;bl='S&P 500';}
  const out=bv!=null?(s.cur_value-bv):null;   // Mehrwert ggue. Benchmark (ich - Index)
  const el=document.getElementById('benchMetric');
  if(!el) return;
  el.querySelector('.lbl').textContent='vs. '+bl;
  const v=el.querySelector('.val');
  v.textContent=out==null?'\u2013':(out>=0?'+':'\u2212')+nf2.format(Math.abs(out))+' \u20ac';
  v.className='val '+(out==null?'':(out>=0?'pos':'neg'));
  el.querySelector('.note').textContent=bv==null?'Ohne Vergleichsindex':(bl+' h\u00e4tte '+e0r(bv)+(bx!=null?' \u00b7 '+ps(bx)+' p.a.':''));
}
updateBenchCard();

/* ---- W\u00e4hrungsexposition (kompakter Donut) ---- */
(function(){
  if(!D.currency_exp||!Object.keys(D.currency_exp).length){
    document.getElementById('currCard').style.display='none'; return; }
  const ks=Object.keys(D.currency_exp),vs=ks.map(k=>D.currency_exp[k].value);
  Plotly.newPlot('currencyChart',[{type:'pie',labels:ks,values:vs,
    marker:{colors:palette,line:{color:'#fff',width:2}},
    textinfo:'none',hole:0.6,sort:true,direction:'clockwise',
    hovertemplate:'%{label}: %{value:,.0f} \u20ac (%{percent:.1%})<extra></extra>'}],
    {separators:',.',margin:{l:6,r:6,t:6,b:30},height:212,paper_bgcolor:'#fff',
     showlegend:true,legend:{orientation:'h',y:-0.05,x:0.5,xanchor:'center',font:{size:10.5}}},
    {displaylogo:false,responsive:true,displayModeBar:false});
})();

/* ---- Sektorverteilung (kompakter Donut) ---- */
(function(){
  const sk=D.sectors?Object.keys(D.sectors):[];
  if(!sk.length){
    document.getElementById('sectorChart').innerHTML=
      '<p style="padding:24px 12px;color:var(--muted);font-size:12px;text-align:center">Sektordaten werden beim n\u00e4chsten Aktualisieren geladen.</p>';
    return;
  }
  const sv=sk.map(k=>D.sectors[k].value);
  const scols=['#2563b6','#0a9d57','#e08214','#dc3a2c','#7b3294','#0d8b8b','#b8860b','#5b8a2b','#c2569a','#3b7dd8'];
  Plotly.newPlot('sectorChart',[{type:'pie',labels:sk,values:sv,
    marker:{colors:scols,line:{color:'#fff',width:2}},
    textinfo:'none',hole:0.6,sort:true,direction:'clockwise',
    hovertemplate:'%{label}: %{value:,.0f} \u20ac (%{percent:.1%})<extra></extra>'}],
    {separators:',.',margin:{l:6,r:6,t:6,b:30},height:212,paper_bgcolor:'#fff',
     showlegend:true,legend:{orientation:'h',y:-0.05,x:0.5,xanchor:'center',font:{size:10.5}}},
    {displaylogo:false,responsive:true,displayModeBar:false});
})();

/* ---- Dividendenhistorie ---- */
(function(){
  if(!D.div_hist||!D.div_hist.months||!D.div_hist.months.length){
    document.getElementById('divHistWrap').style.display='none'; return; }
  // Hinweistext je nach Datenquelle
  const hintEl=document.getElementById('divHistHint');
  if(hintEl){
    hintEl.textContent=D.div_hist.source==='konto'
      ? 'Monatliche Ausschüttungen über die Zeit — tatsächliche Netto-Bargutschriften lt. Konto-Export.'
      : 'Monatliche Ausschüttungen über die Zeit — Brutto-Schätzung aus Yahoo-Daten (vor Steuern/Quellensteuer). Laufender Monat wird ausgeblendet.';
  }
  const total=D.div_hist.values.reduce((a,b)=>a+b,0);
  Plotly.newPlot('divHistChart',[{
    type:'bar',x:D.div_hist.months,y:D.div_hist.values,name:'Dividenden',
    marker:{color:D.div_hist.values.map(v=>v>0?'#1a9850':'#d12c20'),opacity:0.82},
    hovertemplate:'%{x}: %{y:,.2f} €<extra>Dividenden</extra>',
    text:D.div_hist.values.map(v=>v>1?e0(v):''),textposition:'outside',
    cliponaxis:false}],
    {separators:',.',margin:{l:60,r:18,t:20,b:50},paper_bgcolor:'#fff',plot_bgcolor:'#fff',
     yaxis:{ticksuffix:' €',tickformat:',.0f',gridcolor:'#eee',zeroline:false},
     xaxis:{tickangle:-45,showgrid:false},
     annotations:[{text:'Gesamt: '+e0(total),showarrow:false,xref:'paper',yref:'paper',
                   x:1,y:1.06,xanchor:'right',font:{size:12,color:'var(--muted)'}}]},
    {displaylogo:false,responsive:true,displayModeBar:false});
})();

/* ---- Steuerübersicht ---- */
let _taxLand=localStorage.getItem('taxLand')||'de';
function renderTax(){
  const el=document.getElementById('taxSection');
  if(!D.tax||!D.tax.length){el.innerHTML='<p class="hint">Keine realisierten Gewinne/Verluste im Export vorhanden.</p>';return;}
  const isDe=_taxLand==='de';
  let totGain=0,totTax=0;
  const rows=D.tax.map(t=>{
    totGain+=t.gain;
    const tx=isDe?t.tax_de:t.tax_at; totTax+=tx;
    return `<tr>
      <td>${t.year}</td>
      <td class="right ${t.gain>=0?'pos':'neg'}">${es(t.gain)}</td>
      <td class="right" style="color:var(--muted);font-size:12px">${isDe?e0(t.fb_de)+' FB':''}</td>
      <td class="right ${tx>0?'neg':''}">${tx>0?'−'+e0(tx):'–'}</td>
    </tr>`;}).join('');
  el.innerHTML=
    `<div style="margin-bottom:12px;display:flex;gap:20px;flex-wrap:wrap">
      <label style="font-size:13px;cursor:pointer"><input type="radio" name="tl" value="de" ${isDe?'checked':''}> Deutschland (26,375 % inkl. Soli)</label>
      <label style="font-size:13px;cursor:pointer"><input type="radio" name="tl" value="at" ${!isDe?'checked':''}> Österreich (27,5 % KESt)</label>
    </div>
    <table class="tax-tbl" style="max-width:560px">
      <thead><tr>
        <th class="l">Jahr</th><th>Realisierter G/V</th>
        <th>${isDe?'Freistellungsbetrag':''}</th><th>Steuer (ca.)</th>
      </tr></thead>
      <tbody>${rows}</tbody>
      <tfoot><tr class="dsum"><td class="l">Gesamt</td>
        <td class="right ${totGain>=0?'pos':'neg'}">${es(totGain)}</td>
        <td></td>
        <td class="right ${totTax>0?'neg':''}">${totTax>0?'−'+e0(totTax):'–'}</td>
      </tr></tfoot>
    </table>
    <p class="hint" style="margin-top:8px">Schätzung auf Basis realisierter Cashflow-Gewinne (FIFO, ohne Verlustverrechnung aus Vorjahren und ohne Quellensteueranrechnung). ${isDe?'Freistellungsauftrag: 1.000 € (ab 2023) / 801 € (bis 2022).':''}</p>`;
  el.querySelectorAll('[name="tl"]').forEach(r=>r.addEventListener('change',function(){
    _taxLand=this.value; localStorage.setItem('taxLand',this.value); renderTax(); }));
}
renderTax();

/* ---- Datenübersicht: welche Daten/Positionen sind importiert? ---- */
(function(){
  const body=document.getElementById('dovBody'), metaEl=document.getElementById('dovMeta');
  if(!body) return;
  const toKey=s=>{const p=s.split('.'); return p.length<3?'':p[2]+p[1].padStart(2,'0')+p[0].padStart(2,'0');};
  const keyToDate=k=>k?k.slice(6,8)+'.'+k.slice(4,6)+'.'+k.slice(0,4):'–';
  let allTx=0, gMin=null, gMax=null;
  const rows=D.positions.map(p=>{
    const tx=p.txns||[]; allTx+=tx.length;
    let mn=null,mx=null;
    tx.forEach(t=>{const k=toKey(t.d); if(!k) return;
      if(mn==null||k<mn){mn=k;} if(mx==null||k>mx){mx=k;}
      if(gMin==null||k<gMin){gMin=k;} if(gMax==null||k>gMax){gMax=k;}});
    return {name:p.name,
            isin:(p.isin&&p.isin!=='-')?p.isin:'', ticker:(p.ticker&&p.ticker!=='-')?p.ticker:'',
            n:tx.length, first:keyToDate(mn), last:keyToDate(mx), status:p.status};
  }).sort((a,b)=>a.name.localeCompare(b.name));
  const nInc=(D.incomplete||[]).length;
  if(metaEl) metaEl.textContent=`${D.positions.length} Positionen · ${allTx} Transaktionen · ${keyToDate(gMin)} – ${keyToDate(gMax)}`+(nInc?` · ${nInc} unvollständig`:'');
  const hasKonto=s.deposits!=null;
  let html=`<div class="dov-srcs">
    <span class="dov-src"><span class="dot on"></span>Depot-Transaktionen · ${allTx} Buchungen</span>
    <span class="dov-src"><span class="dot ${hasKonto?'on':'off'}"></span>Konto-Umsätze (Ein-/Auszahlungen) · ${hasKonto?'importiert':'nicht importiert'}</span>
    <span class="dov-src"><span class="dot on"></span>Zeitraum · ${keyToDate(gMin)} bis ${keyToDate(gMax)}</span>
  </div>`;
  if(nInc){
    html+=`<div class="dov-warn"><b>${nInc} Position${nInc>1?'en':''} mit unvollständiger Historie:</b> `+
      D.incomplete.join(', ')+`. Hier wurde mehr verkauft als gekauft – der ursprüngliche Kauf liegt vermutlich vor dem Export. `+
      `Für vollständige Zahlen einen älteren Flatex-Export (ab Depoteröffnung) importieren.</div>`;
  }
  html+=`<table class="dov-tbl">
    <thead><tr><th class="l">Position</th><th class="l">ISIN</th><th class="l">Ticker</th>
      <th class="l">Status</th><th>Transaktionen</th><th class="l">Erste</th><th class="l">Letzte</th></tr></thead>
    <tbody>`+rows.map(r=>`<tr>
      <td class="l" style="font-weight:600">${r.name}</td>
      <td class="l dov-mut">${r.isin||'–'}</td>
      <td class="l dov-mut">${r.ticker||'–'}</td>
      <td class="l">${badge(r.status)}</td>
      <td class="right">${r.n}</td>
      <td class="l">${r.first}</td>
      <td class="l">${r.last}</td>
    </tr>`).join('')+`</tbody></table>
    <p class="hint" style="margin:12px 2px 0">Zeigt, welche Positionen und Buchungen bereits in den Daten stecken. Fehlt eine Position oder ein früherer Kaufzeitpunkt, einen weiteren Flatex-Export importieren.</p>`;
  body.innerHTML=html;
})();

document.getElementById('foot').innerHTML =
  'Realisierte G/V exakt aus den Cashflows · Marktwerte über Yahoo-Kurse, in EUR umgerechnet über tägliche Devisenkurse · '+
  'Aktiensplits aus den Buchungsdaten berücksichtigt · Positionen ohne verlässlichen Kurs werden zu Einstand bzw. 0 bewertet · '+
  (s.div_real ? 'Dividenden = tatsächliche Bargutschriften lt. Konto. '
              : 'Dividenden = Brutto-Schätzung aus der Ausschüttungshistorie (vor Steuern). ')+
  'Keine Anlageberatung.';
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
