#!/usr/bin/env python3
"""Streamlit-App: Flatex Portfolio-Analyse mit persistenter SQLite-Datenbank.

Start:
    streamlit run portfolio_app.py

Docker:
    docker compose up
"""
import hashlib, os, sqlite3, tempfile
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import portfolio_analyse as pa

# ---------------------------------------------------------------- Konfiguration
DB_PATH    = os.environ.get("DB_PATH",    "data/portfolio.db")
CACHE_DIR  = os.environ.get("CACHE_DIR",  "data/cache")

GREEN  = "#1a9850"
RED    = "#d12c20"
BLUE   = "#2c5fa8"
ORANGE = "#e67e22"
GREY   = "#888888"

COLORS = [
    "#2166ac","#d6604d","#4dac26","#8073ac","#f4a582",
    "#1a9850","#e08214","#74add1","#c51b7d","#4d9221",
    "#762a83","#de77ae","#7fbc41","#e7298a","#66bd63",
    "#d73027","#1b7837","#9970ab","#fdae61","#a6d854",
    "#f46d43","#3288bd","#abdda4","#fee08b",
]

def _rgba(hex_color: str, alpha: float = 0.75) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"

# ---------------------------------------------------------------- DB-Hilfsfunktionen
def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS depot_rows
                   (hash TEXT PRIMARY KEY, row TEXT, source TEXT, imported_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS konto_rows
                   (hash TEXT PRIMARY KEY, row TEXT, source TEXT, imported_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS meta
                   (key TEXT PRIMARY KEY, value TEXT)""")
    con.commit()
    return con


def import_csv(con: sqlite3.Connection, table: str, raw: bytes, filename: str):
    text = raw.decode("utf-8-sig", errors="replace")
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return 0, 0
    header, data = lines[0], lines[1:]
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"{table}_header", header))
    new = dup = 0
    for row in data:
        h = hashlib.sha256(row.strip().encode()).hexdigest()
        try:
            con.execute(f"INSERT INTO {table} VALUES (?,?,?,?)",
                        (h, row, filename, datetime.now().isoformat()))
            new += 1
        except sqlite3.IntegrityError:
            dup += 1
    con.commit()
    return new, dup


def export_temp(con: sqlite3.Connection, table: str) -> str | None:
    hdr = con.execute("SELECT value FROM meta WHERE key=?",
                      (f"{table}_header",)).fetchone()
    if not hdr:
        return None
    rows = con.execute(f"SELECT row FROM {table}").fetchall()
    if not rows:
        return None
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False,
                                     encoding="utf-8", newline="")
    tf.write(hdr[0] + "\n")
    for (r,) in rows:
        tf.write(r + "\n")
    tf.close()
    return tf.name


def row_counts(con: sqlite3.Connection) -> tuple[int, int]:
    d = con.execute("SELECT COUNT(*) FROM depot_rows").fetchone()[0]
    k = con.execute("SELECT COUNT(*) FROM konto_rows").fetchone()[0]
    return d, k


def clear_db(con: sqlite3.Connection):
    for tbl in ("depot_rows", "konto_rows", "meta"):
        con.execute(f"DELETE FROM {tbl}")
    con.commit()

# ---------------------------------------------------------------- Zahlenformatierung
def eur(x) -> str:
    sign = "+" if x > 0 else ""
    return f"{sign}{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")

def eur_plain(x) -> str:
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")

def pct(x) -> str:
    return f"{x:+.2f} %"

# ---------------------------------------------------------------- Chart-Funktionen
def build_line_chart(payload: dict) -> go.Figure:
    dates = payload["dates"]
    L     = payload["line"]

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.68, 0.32], vertical_spacing=0.04,
        subplot_titles=("Depotentwicklung", "Gewinn / Verlust gegenüber Einzahlung"),
    )
    fig.add_trace(go.Scatter(
        x=dates, y=L["depot"], name="Depotwert",
        line=dict(color=BLUE, width=2),
        hovertemplate="%{y:,.2f} €<extra>Depotwert</extra>",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=dates, y=L["invested"], name="Investiert (netto)",
        line=dict(color=GREY, width=1.5, dash="dot"),
        hovertemplate="%{y:,.2f} €<extra>Investiert</extra>",
    ), row=1, col=1)
    if L.get("bench"):
        fig.add_trace(go.Scatter(
            x=dates, y=L["bench"], name="MSCI World (Vergleich)",
            line=dict(color=ORANGE, width=1.5, dash="dash"),
            hovertemplate="%{y:,.2f} €<extra>MSCI World</extra>",
        ), row=1, col=1)

    diff = L["diff"]
    colors_diff = [GREEN if v >= 0 else RED for v in diff]
    fig.add_trace(go.Scatter(
        x=dates, y=diff, name="G/V",
        fill="tozeroy",
        line=dict(width=0),
        fillcolor="rgba(26,152,80,0.25)",
        marker=dict(color=colors_diff),
        hovertemplate="%{y:,.2f} €<extra>Gewinn/Verlust</extra>",
    ), row=2, col=1)
    fig.add_hline(y=0, line_width=1, line_color="#ccc", row=2, col=1)

    fig.update_layout(
        height=520, hovermode="x unified", margin=dict(l=0, r=0, t=30, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1,
                     spikecolor="#aaa", spikedash="dot")
    fig.update_yaxes(ticksuffix=" €", gridcolor="#ebebeb")
    return fig


def build_stack_chart(payload: dict, visible: set | None = None) -> go.Figure:
    dates  = payload["dates"]
    labels = payload["stack"]["labels"]
    series = payload["stack"]["series"]

    fig = go.Figure()
    for i, (label, vals) in enumerate(zip(labels, series)):
        show = visible is None or label in visible
        color = COLORS[i % len(COLORS)]
        fig.add_trace(go.Scatter(
            x=dates, y=vals, name=label,
            stackgroup="one",
            line=dict(color=color, width=0.8),
            fillcolor=_rgba(color, 0.7),
            visible=True if show else "legendonly",
            hovertemplate="%{y:,.2f} €<extra>" + label + "</extra>",
        ))
    fig.update_layout(
        height=380, hovermode="x unified", margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0,
                    font=dict(size=11)),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1,
                     spikecolor="#aaa", spikedash="dot")
    fig.update_yaxes(ticksuffix=" €", gridcolor="#ebebeb")
    return fig


def build_pie_chart(payload: dict) -> go.Figure:
    pos = [(p["name"], p["cur_value"]) for p in payload["positions"] if p["cur_value"] > 0.5]
    pos.sort(key=lambda x: x[1], reverse=True)
    if not pos:
        return go.Figure()
    labels = [p[0] for p in pos]
    values = [p[1] for p in pos]
    fig = go.Figure(go.Pie(
        labels=labels, values=values,
        sort=False,
        texttemplate="%{label}<br>%{percent:.1%}",
        hovertemplate="%{label}: %{value:,.2f} €  (%{percent:.1%})<extra></extra>",
        marker=dict(colors=[COLORS[i % len(COLORS)] for i in range(len(labels))]),
    ))
    fig.update_layout(
        height=380, margin=dict(l=0, r=0, t=10, b=0),
        showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig

# ---------------------------------------------------------------- Positions-Tabelle
def render_positions(positions: list):
    for pos in positions:
        total    = pos["total"]
        ret_pct  = pos["ret_pct"]
        status   = pos["status"]
        cur      = pos["cur_value"]
        color    = GREEN if total >= 0 else RED
        icon     = "▲" if total >= 0 else "▼"
        status_icon = {"offen": "🟢", "teilw. verkauft": "🟡", "verkauft": "⚫"}.get(status, "")

        label = (f"{status_icon} **{pos['name']}** ({pos['ticker']})  "
                 f"— G/V: :{('green' if total>=0 else 'red')}[{icon} {eur_plain(total)} / {pct(ret_pct)}]"
                 f"  |  Depotwert: {eur_plain(cur)}")

        with st.expander(label, expanded=False):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Investiert", eur_plain(pos["buys"]))
            c2.metric("Erlös + Wert", eur_plain(pos["returned"]))
            c3.metric("Dividenden", eur_plain(pos["dividends"]))
            c4.metric("Rendite", pct(ret_pct))

            txns = pos.get("txns", [])
            if txns:
                st.markdown("**Transaktionshistorie**")
                df_t = pd.DataFrame(txns)
                df_t.columns = ["Datum", "Art", "Stück", "Kurs (€)", "Betrag (€)"]
                st.dataframe(df_t, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- Hauptapp
st.set_page_config(page_title="Portfolio Analyse", layout="wide", page_icon="📈")
st.title("📈 Portfolio Analyse")

con = get_db()

# Sidebar
with st.sidebar:
    st.header("Daten importieren")
    depot_cnt, konto_cnt = row_counts(con)
    col_a, col_b = st.columns(2)
    col_a.metric("Depot-Zeilen", depot_cnt)
    col_b.metric("Konto-Zeilen", konto_cnt)

    st.divider()

    depot_file = st.file_uploader("Depot-Export (CSV)", type=["csv"], key="dep")
    konto_file = st.file_uploader("Konto-Export (CSV)", type=["csv"], key="kon",
                                  help="Optional – für echte Dividenden")

    if st.button("Importieren", type="primary", disabled=depot_file is None):
        if depot_file:
            n, d = import_csv(con, "depot_rows", depot_file.read(), depot_file.name)
            st.success(f"Depot: **{n}** neu importiert, {d} Duplikate übersprungen")
        if konto_file:
            n, d = import_csv(con, "konto_rows", konto_file.read(), konto_file.name)
            st.success(f"Konto: **{n}** neu importiert, {d} Duplikate übersprungen")
        depot_cnt, konto_cnt = row_counts(con)
        st.rerun()

    st.divider()

    do_analyse = st.button("Analysieren", type="primary",
                           disabled=depot_cnt == 0,
                           help="Berechnet Portfolio und lädt aktuelle Kurse")
    if do_analyse:
        st.session_state["run"] = True

    if st.button("Cache leeren", help="Kurse werden beim nächsten Analysieren neu abgerufen"):
        import shutil
        if os.path.exists(CACHE_DIR):
            shutil.rmtree(CACHE_DIR)
        st.toast("Cache geleert – Kurse werden neu geladen")

    if st.button("Alle Daten löschen", type="secondary"):
        clear_db(con)
        st.session_state.pop("payload", None)
        st.session_state.pop("run", None)
        st.rerun()

# Analyse ausführen
if st.session_state.get("run") and depot_cnt > 0:
    tx_path = cash_path = None
    try:
        with st.spinner("Analysiere Portfolio (Kurse werden abgerufen)…"):
            tx_path   = export_temp(con, "depot_rows")
            cash_path = export_temp(con, "konto_rows")
            os.makedirs(CACHE_DIR, exist_ok=True)
            payload, _ = pa.compute_payload(
                tx_path, cash_path, cache_dir=CACHE_DIR)
            st.session_state["payload"] = payload
    except Exception as e:
        st.error(f"Fehler bei der Analyse: {e}")
        raise
    finally:
        for p in (tx_path, cash_path):
            if p and os.path.exists(p):
                os.unlink(p)

# Ergebnisse anzeigen
payload = st.session_state.get("payload")
if not payload:
    st.info("Bitte Flatex-CSV-Exporte hochladen und **Analysieren** klicken.")
    st.stop()

stats = payload["stats"]

# KPI-Karten
st.subheader(f"Stand {payload['today']}")
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Depotwert",         eur_plain(stats["cur_value"]))
k2.metric("Investiert (netto)", eur_plain(stats["invested"]))
k3.metric("Gesamt G/V",         eur_plain(stats["total_pl"]),
           delta=pct(stats["total_pl"] / stats["invested"] * 100) if stats["invested"] else None)
k4.metric("XIRR",               f"{stats['xirr']:+.2f} % p.a.")
k5.metric("Dividenden",          eur_plain(stats["dividends"]),
           delta="lt. Konto" if stats["div_real"] else "Schätzung", delta_color="off")

if payload.get("incomplete"):
    st.warning("Unvollständige Historie (Kauf vor Export): " +
               ", ".join(payload["incomplete"]) +
               " → für vollständige Zahlen den Export ab Depoteröffnung verwenden.")

st.divider()

# Linien- + Gewinn-Chart
st.subheader("Depotentwicklung")
st.plotly_chart(build_line_chart(payload), use_container_width=True)

st.divider()

# Stack-Chart + Pie-Chart
col_stack, col_pie = st.columns([2, 1])
with col_stack:
    st.subheader("Woraus bestand mein Vermögen?")
    st.plotly_chart(build_stack_chart(payload), use_container_width=True)
with col_pie:
    st.subheader("Aktuelle Aufteilung")
    st.plotly_chart(build_pie_chart(payload), use_container_width=True)

st.divider()

# Positionen
st.subheader("Positionen")
render_positions(payload["positions"])

# Kennzahlen-Details (ausklappbar)
with st.expander("Details & Kennzahlen"):
    d1, d2 = st.columns(2)
    with d1:
        st.markdown("**Cashflows**")
        st.markdown(f"- Gesamt eingezahlt: {eur_plain(stats['buys'])}")
        st.markdown(f"- Gesamt ausgezahlt: {eur_plain(stats['sells'])}")
        st.markdown(f"- Realisierte G/V:   {eur_plain(stats['realized'])}")
        st.markdown(f"- Unrealisierte G/V: {eur_plain(stats['unreal'])}")
        if stats.get("deposits") is not None:
            st.markdown("---")
            st.markdown(f"- Einzahlungen auf Flatex:   {eur_plain(stats['deposits'])}")
            st.markdown(f"- Auszahlungen von Flatex:   {eur_plain(stats['withdrawals'])}")
    with d2:
        st.markdown("**Vergleich**")
        if stats.get("bench") is not None:
            diff = stats["bench"] - stats["cur_value"]
            st.markdown(f"- MSCI World-Depotwert: {eur_plain(stats['bench'])}")
            st.markdown(f"- Unterschied: {eur_plain(diff)} {'(MSCI World wäre besser)' if diff > 0 else '(du bist besser)'}")
            st.markdown(f"- MSCI World XIRR: {stats['bench_xirr']:+.2f} % p.a." if stats.get("bench_xirr") else "")
        an = payload.get("analysis", {})
        if an.get("best"):
            st.markdown("---")
            st.markdown(f"- Beste Position: **{an['best']['name']}** ({pct(an['best']['ret_pct'])})")
        if an.get("worst"):
            st.markdown(f"- Schlechteste Position: **{an['worst']['name']}** ({pct(an['worst']['ret_pct'])})")
