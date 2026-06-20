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
    ci, cb, cp = cm["info"], cm["betrag"], cm["pfl"]
    maxi = max(x for x in (ci, cb, cp) if x is not None)
    div = defaultdict(float)
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
    from collections import deque
    for key, ins in sorted(instruments.items(), key=lambda x: x[1]["name"]):
        if ins["incomplete"]:
            continue
        tx = sorted(ins["txns"], key=lambda t: t["date"])
        buys  = sum(t["betrag"] for t in tx if t["betrag"] > 0)
        sells = sum(-t["betrag"] for t in tx if t["betrag"] < 0)
        pos_txns[ins["name"]] = [
            {"d": pd.Timestamp(t["date"]).strftime("%d.%m.%Y"),
             "k": "Kauf" if t["kind"] == "buy" else "Verkauf",
             "q": round(abs(t["qty"]), 4), "p": round(t["kurs"], 2),
             "a": round(t["betrag"], 2)} for t in tx]

        buy_adj = sum(t["adj_qty"] for t in tx if t["adj_qty"] > 0)

        lots = deque()
        realized = 0.0
        for t in tx:
            if t["adj_qty"] > 0:
                lots.append([t["adj_qty"], t["betrag"] / t["adj_qty"]])
            elif t["adj_qty"] < 0:
                sell_rest = -t["adj_qty"]
                price_per = (-t["betrag"]) / sell_rest
                while sell_rest > 1e-9 and lots:
                    lot = lots[0]
                    take = min(sell_rest, lot[0])
                    realized += take * (price_per - lot[1])
                    lot[0] -= take; sell_rest -= take
                    if lot[0] <= 1e-9:
                        lots.popleft()
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

    _log(f"  {len(rows)} Positionen bewertet, {len(missing)} ohne Marktdaten")
    cash_txns = [t for t in txns if t["kind"] in ("buy", "sell")
                 and t["isin"] not in incomplete_isins]

    _log("XIRR & Benchmark berechnen …")
    flow = pd.Series(0.0, index=bdays)
    for t in cash_txns:
        flow.loc[pd.Timestamp(t["date"]):] += t["betrag"]
    net_invested = flow

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
    wk = inst_df.resample("W-FRI").last().ffill().fillna(0.0)
    pv_w  = portfolio_value.resample("W-FRI").last().ffill()
    ni_w  = net_invested.resample("W-FRI").last().ffill()
    bn_w  = bench_value.resample("W-FRI").last().ffill() if bench_value is not None else None
    dates = [d.strftime("%Y-%m-%d") for d in wk.index]

    peak = wk.max()
    last = wk.iloc[-1]
    active  = [c for c in wk.columns if peak[c] > 0.5]
    ordered = sorted(active, key=lambda n: (float(last[n]), float(peak[n])), reverse=True)
    CAP = 24
    chosen, others = ordered[:CAP], ordered[CAP:]
    stack_labels = list(chosen) + (["Sonstige"] if others else [])
    stack_series = [[round(v, 2) for v in wk[n].values] for n in chosen]
    if others:
        stack_series.append([round(v, 2) for v in wk[others].sum(axis=1).values])

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
    # wöchentliche Stützstellen (leichtgewichtig & glatt genug)
    inst_df = pd.DataFrame(inst_values).reindex(bdays).fillna(0.0)
    wk = inst_df.resample("W-FRI").last().ffill().fillna(0.0)
    pv_w  = portfolio_value.resample("W-FRI").last().ffill()
    ni_w  = net_invested.resample("W-FRI").last().ffill()
    bn_w  = bench_value.resample("W-FRI").last().ffill() if bench_value is not None else None
    dates = [d.strftime("%Y-%m-%d") for d in wk.index]

    # Stacking: ALLE jemals gehaltenen Positionen einzeln (auch laengst verkaufte),
    # damit das Chart zu jedem Zeitpunkt die damalige Zusammensetzung zeigt. Nur ein
    # kleiner, vernachlaessigbarer Rest (Peak-Wert <0,5 €) wandert in "Sonstige".
    peak = wk.max()
    last = wk.iloc[-1]
    active  = [c for c in wk.columns if peak[c] > 0.5]
    ordered = sorted(active, key=lambda n: (float(last[n]), float(peak[n])), reverse=True)
    CAP = 24
    chosen, others = ordered[:CAP], ordered[CAP:]
    stack_labels = list(chosen) + (["Sonstige"] if others else [])
    stack_series = [[round(v, 2) for v in wk[n].values] for n in chosen]
    if others:
        stack_series.append([round(v, 2) for v in wk[others].sum(axis=1).values])

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


def export_html(payload):
    html = _HTML_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio-Report</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>
  :root{ --green:#1a9850; --red:#d12c20; --blue:#2c5fa8; --ink:#1b1b1f; --muted:#70737a;
         --line:#e8e8ec; --bg:#f6f7f9; --card:#ffffff; }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
  .wrap{max-width:1120px;margin:0 auto;padding:32px 20px 64px;}
  h1{font-size:26px;margin:0 0 2px;letter-spacing:-.3px}
  .sub{color:var(--muted);font-size:14px;margin-bottom:24px}
  h2{font-size:18px;margin:34px 0 4px;letter-spacing:-.2px}
  .hint{color:var(--muted);font-size:13px;margin:0 0 12px}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:10px 0 6px}
  .cards3{grid-template-columns:repeat(3,1fr)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;
        box-shadow:0 1px 2px rgba(0,0,0,.03)}
  .card .lbl{color:var(--muted);font-size:12.5px;margin-bottom:6px}
  .card .val{font-size:23px;font-weight:700;letter-spacing:-.5px}
  .card .note{font-size:12px;color:var(--muted);margin-top:3px}
  .pos{color:var(--green)} .neg{color:var(--red)}
  .divbanner{margin:14px 0 2px;background:#eef7f0;border:1px solid #cfe8d6;border-radius:12px;
             padding:13px 18px;font-size:14.5px;display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline}
  .divbanner b{font-size:16px}
  .warnbanner{margin:14px 0 2px;background:#fdeeee;border:1px solid #f3c9c9;border-radius:12px;
              padding:13px 18px;font-size:14px;color:#8a2a22}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 8px 4px;
         box-shadow:0 1px 2px rgba(0,0,0,.03);position:relative}
  #stackPieList{max-height:230px;overflow:auto;padding:2px 2px 4px}
  .pl{display:flex;align-items:center;gap:6px;font-size:11px;padding:1px 0}
  .pl .sw{width:9px;height:9px;border-radius:2px;flex:0 0 auto}
  .pl .pn{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .pl .pv{font-variant-numeric:tabular-nums}
  .pl .pp{color:var(--muted);width:32px;text-align:right}
  .btnbar{display:flex;justify-content:flex-end;margin:0 0 8px}
  .btn{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px;
       font-size:13px;font-weight:600;color:var(--ink);cursor:pointer}
  .btn:hover{background:#f0f4fb}
  .chips{display:flex;flex-wrap:wrap;gap:7px;margin:2px 0 6px}
  .chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;padding:4px 10px;border-radius:999px;
        border:1px solid var(--line);background:var(--card);cursor:pointer;user-select:none;white-space:nowrap}
  .chip .sw{width:9px;height:9px;border-radius:2px;flex:0 0 auto}
  .chip.off{opacity:.42;text-decoration:line-through}
  .composition{display:flex;gap:16px;align-items:flex-start}
  .sidepie{width:280px;flex:0 0 auto;padding:12px 12px 8px}
  .sidepie-title{font-size:12.5px;color:var(--muted);font-weight:600;margin:2px 2px 6px;text-align:center}
  .detailwrap{padding:6px 4px 10px}
  .detailcard{background:#fafbfc;border:1px solid var(--line);border-radius:12px;padding:12px 14px}
  .detailcard .dh{font-size:14px;font-weight:700;margin-bottom:8px}
  .dtbl{font-size:13px;box-shadow:none}
  .dtbl th{background:#f1f3f6}
  .dsum td{font-weight:700;border-top:2px solid var(--line);background:#f1f3f6}
  td.detailcell{padding:0;background:#fafbfc}
  #tbl tbody tr.detailrow,#tbl tbody tr.detailrow:hover{cursor:default;background:#fafbfc}
  .posrow.open td{background:#eef2f8}
  .callouts{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:10px}
  .callout{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
  .callout h3{margin:0 0 10px;font-size:15px}
  .callout .row{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px solid var(--line);font-size:14px}
  .callout .row:first-of-type{border-top:none}
  .callout .row .k{color:var(--muted)}
  .callout .row .v{font-weight:600;text-align:right}
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
        border-radius:14px;overflow:hidden;font-size:14px}
  th,td{padding:10px 12px;text-align:right;white-space:nowrap}
  th{background:#fafbfc;color:var(--muted);font-weight:600;font-size:12.5px;cursor:pointer;
     border-bottom:1px solid var(--line);user-select:none}
  th:hover{color:var(--ink)}
  th.l,td.l{text-align:left}
  tbody tr:nth-child(even){background:#fafbfc}
  #tbl tbody tr{cursor:pointer}
  tbody tr:hover{background:#f0f4fb}
  td.name{font-weight:600}
  .badge{font-size:11.5px;padding:2px 8px;border-radius:999px;font-weight:600}
  .b-open{background:#e7f0ff;color:#2c5fa8} .b-part{background:#fff3da;color:#9a6b13}
  .b-sold{background:#eef0f2;color:#70737a}
  tfoot td{font-weight:700;border-top:2px solid var(--line);background:#fafbfc}
  .right{font-variant-numeric:tabular-nums}
  footer{color:var(--muted);font-size:12px;margin-top:26px;line-height:1.6}
  @media(max-width:760px){.cards{grid-template-columns:repeat(2,1fr)}.callouts{grid-template-columns:1fr}
    .composition{flex-direction:column}.sidepie{width:100%}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Mein Aktien-Portfolio</h1>
  <div class="sub" id="sub"></div>
  <div id="warnbanner"></div>
  <div class="cards" id="cards"></div>
  <div class="divbanner" id="divbanner"></div>
  <div id="cashflow"></div>

  <h2>Positionen filtern</h2>
  <p class="hint">Wirkt auf <b>alle Diagramme</b>: ausgeblendete Titel verschwinden aus dem Zusammensetzungs-Chart, und ihre Käufe/Verkäufe werden auch in den anderen Diagrammen nicht mehr markiert.</p>
  <div class="btnbar" style="justify-content:flex-start"><button class="btn" id="stackAll">Alle</button><button class="btn" id="stackNone">Keine</button></div>
  <div id="filterChips" class="chips"></div>

  <div id="allocWrap">
    <h2>Aktuelle Aufteilung des Depots</h2>
    <p class="hint">Anteil jeder offenen Position am heutigen Depotwert.</p>
    <div class="panel"><div id="allocPie" style="height:360px"></div></div>
  </div>

  <h2>Entwicklung über die Zeit</h2>
  <p class="hint">Depotwert vs. eingezahltes Geld (= „gar nicht investiert") vs. hypothetischer MSCI&nbsp;World. Beim Hovern wird zusätzlich der Gewinn/Verlust gegenüber dem eingezahlten Geld angezeigt. Dreiecke am unteren Rand markieren Käufe (grün) und Verkäufe (rot).</p>
  <div class="panel"><div id="lineChart" style="height:440px"></div></div>

  <h2>Gewinn / Verlust gegenüber eingezahltem Geld (über die Zeit)</h2>
  <p class="hint">Reiner Gewinn/Verlust im Zeitverlauf: Depotwert minus eingezahltes Geld. <span class="pos">Grün</span> = im Plus, <span class="neg">rot</span> = im Minus. Dreiecke/Linien markieren Käufe und Verkäufe.</p>
  <div class="panel"><div id="profitChart" style="height:300px"></div></div>

  <h2>Woraus bestand mein Vermögen? (pro Aktie, über die Zeit)</h2>
  <p class="hint">Gestapelter Marktwert je Position — zu jedem Zeitpunkt sind alle damals gehaltenen Titel zu sehen. Die Torte rechts zeigt die Aufteilung zum zuletzt angefahrenen Zeitpunkt (bleibt stehen, bis man wieder hineinfährt); aufgeführt sind nur Positionen, die es damals im Depot gab. Senkrechte Linien markieren Käufe/Verkäufe.</p>
  <div class="composition">
    <div class="panel" style="flex:1;min-width:0"><div id="stackChart" style="height:460px"></div></div>
    <div class="panel sidepie"><div class="sidepie-title" id="stackPieTitle"></div><div id="stackPieChart"></div><div id="stackPieList"></div></div>
  </div>

  <h2>Beste Entscheidung &amp; größter Fehler</h2>
  <div class="callouts" id="callouts"></div>

  <h2>Ergebnis je Aktie</h2>
  <p class="hint">Spaltenkopf anklicken zum Sortieren, <b>eine Zeile anklicken</b> klappt die Kauf-/Verkauf-Historie direkt darunter auf. „Verkauf-Timing": was die verkauften Stücke heute wert wären — <span class="pos">grün</span> = guter Ausstieg, <span class="neg">rot</span> = zu früh verkauft.</p>
  <div class="btnbar"><button class="btn" id="csvBtn">Als CSV herunterladen</button></div>
  <table id="tbl">
    <thead><tr>
      <th class="l" data-k="name">Aktie</th>
      <th class="l" data-k="status">Status</th>
      <th data-k="buys">Eingezahlt</th>
      <th data-k="returned">Erlös + Wert heute</th>
      <th data-k="dividends" title="Brutto-Dividenden, aus Ausschüttungshistorie geschätzt">Dividende</th>
      <th data-k="total">Gewinn/Verlust</th>
      <th data-k="ret_pct">Rendite</th>
      <th data-k="timing">Verkauf-Timing</th>
    </tr></thead>
    <tbody></tbody>
    <tfoot></tfoot>
  </table>

  <footer id="foot"></footer>
</div>

<script>
const D = __PAYLOAD__;
const nf2 = new Intl.NumberFormat('de-DE',{minimumFractionDigits:2,maximumFractionDigits:2});
const e0 = x => nf2.format(x)+' €';                                   // max. 2 Nachkommastellen
const es = x => (x>=0?'+':'−')+nf2.format(Math.abs(x))+' €';
const ps = x => (x>=0?'+':'−')+nf2.format(Math.abs(x))+' %';
const ps1 = ps;
const cls = x => x>=0?'pos':'neg';

/* ---- Stat-Karten ---- */
document.getElementById('sub').textContent = 'Stand '+D.today+' · '+D.positions.length+' Positionen';
if(D.incomplete && D.incomplete.length){
  document.getElementById('warnbanner').innerHTML =
    '<b>Unvollständige Historie:</b> bei folgenden Titeln wurde mehr verkauft als gekauft – '+
    'der Kauf liegt vermutlich vor dem Export. Sie sind hier ausgeschlossen (Einstand unbekannt): <b>'+
    D.incomplete.join(', ')+'</b>. Für vollständige Zahlen den Flatex-Export ab Depoteröffnung verwenden.';
}
const s = D.stats;
const benchDiff = s.bench!=null ? (s.bench - s.cur_value) : null;
const cards = [
  ['Gesamt-Gewinn', es(s.total_pl), cls(s.total_pl),
     'realisiert '+es(s.realized)+' · offen '+es(s.unreal)],
  ['Depotwert heute', e0(s.cur_value), '', 'eingezahlt (netto) '+e0(s.invested)],
  ['Jahresrendite', ps1(s.xirr), cls(s.xirr), 'geldgewichtet (XIRR) · Cash = 0&nbsp;%'],
  ['vs. MSCI World', benchDiff==null?'–':es(-benchDiff), benchDiff==null?'':cls(-benchDiff),
     s.bench==null?'':'Index hätte '+e0(s.bench)+' ergeben']
];
document.getElementById('cards').innerHTML = cards.map(c=>
  `<div class="card"><div class="lbl">${c[0]}</div><div class="val ${c[2]}">${c[1]}</div><div class="note">${c[3]}</div></div>`).join('');

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
const EVENT_IDX = D.stack.labels.length;        // Index der Ereignis-Marker-Spur
const stackVis = {};
D.stack.labels.forEach(l=>{ stackVis[l]=true; });
function visibleSet(){ return new Set(D.stack.labels.filter(l=>stackVis[l])); }

const baseLayout = {separators:',.',margin:{l:64,r:18,t:10,b:36},paper_bgcolor:'#fff',plot_bgcolor:'#fff',
  hovermode:'x unified',legend:{orientation:'h',y:1.12,x:0},
  xaxis:{showgrid:false,showspikes:true,spikemode:'across',spikesnap:'cursor',
         spikethickness:1,spikecolor:'#9aa0a6',spikedash:'dot'},
  yaxis:{ticksuffix:' €',tickformat:',.0f',gridcolor:'#eee',zeroline:false}};
const cfg = {responsive:true,displaylogo:false,
  modeBarButtonsToRemove:['lasso2d','select2d','autoScale2d']};

/* ---- Verlaufschart (Depot vs. eingezahlt vs. MSCI World) ---- */
function drawLine(){
  const E=buildEvents(visibleSet());
  const tr=[
    {x:D.dates,y:D.line.invested,name:'Eingezahlt (= nicht investiert)',mode:'lines',
     line:{color:'#2c5fa8',width:2,shape:'hv'},hovertemplate:'%{y:,.2f} €<extra>Eingezahlt</extra>'},
    {x:D.dates,y:D.line.depot,name:'Depotwert',mode:'lines',customdata:D.line.diff,
     line:{color:'#111',width:2.6},
     hovertemplate:'%{y:,.2f} €  (G/V %{customdata:+,.2f} €)<extra>Depotwert</extra>'}
  ];
  if(D.line.bench) tr.unshift({x:D.dates,y:D.line.bench,name:'MSCI World (hypothetisch)',mode:'lines',
     line:{color:'#9a9a9a',width:1.6,dash:'dash'},hovertemplate:'%{y:,.2f} €<extra>MSCI World</extra>'});
  tr.push(evMarker(E));
  const lay=JSON.parse(JSON.stringify(baseLayout));
  lay.shapes=evShapes(E);
  Plotly.react('lineChart',tr,lay,cfg);
}

/* ---- Gewinn/Verlust-Kurve gegenüber eingezahltem Geld (wie im PNG) ---- */
function drawProfit(){
  const E=buildEvents(visibleSet());
  const diff=D.line.diff;
  const pos=diff.map(v=>v>0?v:0), neg=diff.map(v=>v<0?v:0);
  const pt=[
    {x:D.dates,y:pos,mode:'lines',fill:'tozeroy',fillcolor:'rgba(26,152,80,.28)',
     line:{width:0},hoverinfo:'skip',showlegend:false},
    {x:D.dates,y:neg,mode:'lines',fill:'tozeroy',fillcolor:'rgba(209,44,32,.28)',
     line:{width:0},hoverinfo:'skip',showlegend:false},
    {x:D.dates,y:diff,mode:'lines',name:'Gewinn/Verlust',line:{color:'#111',width:2},
     hovertemplate:'%{y:+,.2f} €<extra>Gewinn/Verlust</extra>'}
  ];
  if(D.line.bench){
    const bp=D.line.bench.map((b,i)=>b-D.line.invested[i]);
    pt.push({x:D.dates,y:bp,mode:'lines',name:'MSCI World (G/V)',
      line:{color:'#9a9a9a',width:1.4,dash:'dash'},
      hovertemplate:'%{y:+,.2f} €<extra>MSCI World</extra>'});
  }
  pt.push(evMarker(E));
  const lay=JSON.parse(JSON.stringify(baseLayout));
  lay.yaxis.zeroline=true; lay.yaxis.zerolinecolor='#555'; lay.yaxis.zerolinewidth=1;
  lay.legend={orientation:'h',y:1.14,x:0};
  lay.shapes=evShapes(E);
  Plotly.react('profitChart',pt,lay,cfg);
}

/* ---- Aktuelle Aufteilung (Pie in der Übersicht) ---- */
(function(){
  const cur=D.positions.filter(r=>r.cur_value>0.5).sort((a,b)=>b.cur_value-a.cur_value);
  if(!cur.length){const w=document.getElementById('allocWrap'); if(w) w.style.display='none'; return;}
  Plotly.newPlot('allocPie',[{type:'pie',labels:cur.map(r=>r.name),values:cur.map(r=>r.cur_value),
    marker:{colors:cur.map((_,i)=>stackColor(i)),line:{color:'#fff',width:1}},
    texttemplate:'%{label} %{percent:.1%}',textposition:'inside',insidetextorientation:'radial',
    hovertemplate:'%{label}<br>%{value:,.2f} € (%{percent:.1%})<extra></extra>',hole:0.5,
    sort:true,direction:'clockwise',rotation:0}],
    {separators:',.',margin:{l:10,r:10,t:10,b:10},height:360,paper_bgcolor:'#fff',
     showlegend:true,legend:{orientation:'v',x:1.02,y:0.5,font:{size:11}},
     annotations:[{text:e0(D.stats.cur_value),showarrow:false,font:{size:15}}]},
    {displaylogo:false,responsive:true,displayModeBar:false});
})();

/* ---- Stacked Composition (alle jemals gehaltenen Positionen) ----
   Sichtbarkeit zentral über stackVis; Chart bei jeder Änderung komplett neu aufgebaut
   (Plotly.react) — vermeidet den Render-Fehler (weiße Flächen) beim Wiedereinblenden
   und das Timing-Problem der Ereignis-Marker. y=null außerhalb der Haltedauer;
   hoverinfo:'none' unterdrückt den nativen Tooltip (sonst tauchten 0-€-Titel auf). */
const stackLayout = JSON.parse(JSON.stringify(baseLayout));
stackLayout.legend={orientation:'h',y:-0.16,x:0,font:{size:11}};
stackLayout.margin.b=70;
const stackEl=document.getElementById('stackChart');
function drawStack(){
  const tr=D.stack.labels.map((lab,i)=>({
    x:D.dates,y:D.stack.series[i].map(v=>v>0?v:null),name:lab,mode:'lines',stackgroup:'one',
    line:{width:0.5,color:stackColor(i)},fillcolor:stackColor(i),hoverinfo:'none',
    visible: stackVis[lab] ? true : 'legendonly'}));
  const E=buildEvents(visibleSet());
  tr.push(evMarker(E));
  const lay=JSON.parse(JSON.stringify(stackLayout));
  lay.shapes=evShapes(E);
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
    marker:{colors:items.map(x=>x.c),line:{color:'#fff',width:1}},
    texttemplate:'%{percent:.1%}',hovertemplate:'%{label}<br>%{value:,.2f} € (%{percent:.1%})<extra></extra>',
    sort:true,direction:'clockwise',rotation:0,hole:0.45}],
    {separators:',.',margin:{l:6,r:6,t:6,b:6},height:210,width:256,paper_bgcolor:'#fff',showlegend:false},
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
  if(d.curveNumber<EVENT_IDX){ const l=D.stack.labels[d.curveNumber]; stackVis[l]=!stackVis[l]; applyFilter(); }
  return false;
});
stackEl.on('plotly_legenddoubleclick',d=>{
  if(d.curveNumber<EVENT_IDX){
    const l=D.stack.labels[d.curveNumber];
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
const xnum=D.dates.map(d=>new Date(d).getTime());
let syncing=false, zooming=false;
SYNC.forEach(id=>{
  const gd=document.getElementById(id);
  gd.on('plotly_hover',e=>{
    if(syncing||!e.points||!e.points.length) return;
    const p=e.points.find(pt=>pt.data.name!=='Ereignisse' && pt.pointIndex!=null)||e.points[0];
    const idx=(p.pointIndex!=null)?p.pointIndex:p.pointNumber;
    if(idx==null||xnum[idx]==null) return;
    syncing=true;
    SYNC.forEach(o=>{ if(o!==id){ try{Plotly.Fx.hover(o,{xval:xnum[idx]});}catch(_){} } });
    syncing=false;
  });
  gd.on('plotly_unhover',()=>{
    if(syncing) return; syncing=true;
    SYNC.forEach(o=>{ if(o!==id){ try{Plotly.Fx.unhover(o);}catch(_){} } });
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
let sortK='total', sortDir=-1;
const expanded=new Set();
/* Inline aufklappbare Kauf-/Verkauf-Historie je Position */
function detailHtml(r){
  if(!r.txns||!r.txns.length)
    return '<div class="detailwrap"><div class="detailcard">Keine Transaktionen erfasst.</div></div>';
  let net=0,buy=0,sell=0;
  const body=r.txns.map(t=>{
    net+=t.a; const cf=-t.a;            // Verkauf = +Geld rein, Kauf = −Geld raus
    if(t.a>0) buy+=t.a; else sell+=-t.a;
    return `<tr>
      <td class="l">${t.d}</td><td class="l">${t.k}</td>
      <td class="right">${nf2.format(t.q)}</td>
      <td class="right">${t.p?nf2.format(t.p):'–'}</td>
      <td class="right ${cf>=0?'pos':'neg'}">${es(cf)}</td>
      <td class="right">${e0(net)}</td></tr>`;}).join('');
  return `<div class="detailwrap"><div class="detailcard">
    <div class="dh">${r.name} – Kauf-/Verkauf-Historie</div>
    <table class="dtbl"><thead><tr>
      <th class="l">Datum</th><th class="l">Aktion</th><th>Stück</th><th>Kurs</th>
      <th>Cashflow</th><th>Netto investiert</th></tr></thead>
    <tbody>${body}</tbody>
    <tfoot><tr class="dsum"><td class="l">Summe</td><td></td><td></td><td></td>
      <td class="right">gekauft ${e0(buy)} · verkauft ${e0(sell)}</td>
      <td class="right">${e0(net)}</td></tr></tfoot></table>
    <p class="hint" style="margin:8px 2px 0">„Cashflow": <span class="pos">+</span> = Geld erhalten (Verkauf), <span class="neg">−</span> = Geld eingesetzt (Kauf). „Netto investiert" = bis dahin per Saldo investiertes Geld. Kurs in Handelswährung.</p>
  </div></div>`;
}
function draw(){
  const rows=[...D.positions].sort((a,b)=>{
    let x=a[sortK],y=b[sortK];
    if(typeof x==='string') return sortDir*x.localeCompare(y);
    return sortDir*(x-y);
  });
  document.querySelector('#tbl tbody').innerHTML = rows.map(r=>{
    const open=expanded.has(r.name);
    let html=`<tr class="posrow${open?' open':''}" data-name="${r.name}">
      <td class="l name">${open?'▾ ':'▸ '}${r.name}</td>
      <td class="l">${badge(r.status)}</td>
      <td class="right">${e0(r.buys)}</td>
      <td class="right">${e0(r.returned)}</td>
      <td class="right">${r.dividends>0.5?'<span class="pos">'+e0(r.dividends)+'</span>':'<span style="color:#bbb">–</span>'}</td>
      <td class="right ${cls(r.total)}">${es(r.total)}</td>
      <td class="right ${cls(r.ret_pct)}">${ps(r.ret_pct)}</td>
      <td class="right">${timingCell(r.timing)}</td></tr>`;
    if(open) html+=`<tr class="detailrow"><td class="detailcell" colspan="8">${detailHtml(r)}</td></tr>`;
    return html;
  }).join('');
  const T=(k)=>D.positions.reduce((a,r)=>a+r[k],0);
  document.querySelector('#tbl tfoot').innerHTML=`<tr>
    <td class="l">GESAMT</td><td></td>
    <td class="right">${e0(T('buys'))}</td>
    <td class="right">${e0(T('returned'))}</td>
    <td class="right pos">${e0(T('dividends'))}</td>
    <td class="right ${cls(T('total'))}">${es(T('total'))}</td>
    <td class="right ${cls(T('total'))}">${ps(T('total')/T('buys')*100)}</td>
    <td></td></tr>`;
}
document.querySelectorAll('#tbl th').forEach(th=>th.addEventListener('click',()=>{
  const k=th.dataset.k; sortDir = (sortK===k)? -sortDir : (k==='name'?1:-1); sortK=k; draw();
}));
document.querySelector('#tbl tbody').addEventListener('click',e=>{
  const tr=e.target.closest('tr.posrow'); if(!tr) return;
  const n=tr.dataset.name; if(expanded.has(n)) expanded.delete(n); else expanded.add(n);
  draw();
});
draw();

/* ---- Tabelle als CSV exportieren (in aktueller Sortierung) ---- */
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
