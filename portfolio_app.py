#!/usr/bin/env python3
"""Streamlit-App: Flatex Portfolio-Analyse mit persistenter SQLite-Datenbank."""
import hashlib, os, sqlite3, tempfile
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import portfolio_analyse as pa

# ---------------------------------------------------------------- Konfiguration
DB_PATH   = os.environ.get("DB_PATH",   "data/portfolio.db")
CACHE_DIR = os.environ.get("CACHE_DIR", "data/cache")

GREEN  = "#1a9850"
RED    = "#d12c20"
BLUE   = "#2166ac"
ORANGE = "#e08214"
GREY   = "#888888"

COLORS = [
    "#2166ac","#d6604d","#4dac26","#8073ac","#e08214",
    "#1a9850","#c51b7d","#74add1","#762a83","#4d9221",
    "#d73027","#de77ae","#7fbc41","#e7298a","#66bd63",
    "#f46d43","#1b7837","#9970ab","#fdae61","#a6d854",
    "#3288bd","#b2182b","#abdda4","#fee08b",
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
def _de(x: float, sign=False) -> str:
    s = f"{abs(x):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
    if sign:
        s = ("+" if x >= 0 else "−") + " " + s
    return s

def _pct(x: float) -> str:
    return f"{x:+.2f} %"

# ---------------------------------------------------------------- CSS
def _inject_css():
    st.markdown("""
    <style>
    /* Karten-Sections */
    .section-card {
        background: #ffffff;
        border: 1px solid #e8e8ec;
        border-radius: 12px;
        padding: 1.25rem 1.5rem 1rem;
        margin: 1rem 0 0.5rem;
    }
    .section-title {
        font-size: 1rem;
        font-weight: 600;
        color: #444;
        margin: 0 0 0.75rem;
        letter-spacing: 0.01em;
    }
    /* Tabellen-Colorierung */
    .pos-green { color: #1a7a3a; font-weight: 600; }
    .pos-red   { color: #c0241c; font-weight: 600; }
    /* Metriken etwas kleiner */
    [data-testid="stMetric"] { padding: 0.5rem 0.75rem; }
    /* Pie-Hinweis */
    .pie-hint { font-size: 0.78rem; color: #999; text-align: center; margin-top: -0.5rem; }
    </style>
    """, unsafe_allow_html=True)

# ---------------------------------------------------------------- Charts
def build_performance_chart(payload: dict) -> go.Figure:
    dates = payload["dates"]
    L     = payload["line"]
    diff  = L["diff"]

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.65, 0.35], vertical_spacing=0.06,
        subplot_titles=("Depotentwicklung", "Gewinn / Verlust vs. Einzahlung"),
    )

    # ---- Zeile 1: Depotwert + Investiert + Benchmark
    fig.add_trace(go.Scatter(
        x=dates, y=L["depot"], name="Depotwert",
        line=dict(color=BLUE, width=2.5),
        hovertemplate="%{y:,.2f} €<extra>Depotwert</extra>",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=dates, y=L["invested"], name="Investiert (netto)",
        line=dict(color=GREY, width=1.5, dash="dot"),
        hovertemplate="%{y:,.2f} €<extra>Investiert</extra>",
    ), row=1, col=1)
    if L.get("bench"):
        fig.add_trace(go.Scatter(
            x=dates, y=L["bench"], name="MSCI World",
            line=dict(color=ORANGE, width=1.5, dash="dash"),
            hovertemplate="%{y:,.2f} €<extra>MSCI World</extra>",
        ), row=1, col=1)

    # ---- Zeile 2: G/V – grüne und rote Fläche getrennt
    pos_y = [v if v >= 0 else 0 for v in diff]
    neg_y = [v if v <= 0 else 0 for v in diff]

    fig.add_trace(go.Scatter(
        x=dates, y=pos_y, name="Gewinn",
        fill="tozeroy", fillcolor="rgba(26,152,80,0.22)",
        line=dict(width=0), showlegend=False,
        hoverinfo="skip",
    ), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=dates, y=neg_y, name="Verlust",
        fill="tozeroy", fillcolor="rgba(209,44,32,0.22)",
        line=dict(width=0), showlegend=False,
        hoverinfo="skip",
    ), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=dates, y=diff, name="G/V",
        line=dict(color="#555", width=1.5),
        hovertemplate="%{y:,.2f} €<extra>G/V</extra>",
        showlegend=False,
    ), row=2, col=1)
    fig.add_hline(y=0, line_width=1, line_color="#ddd", row=2, col=1)

    fig.update_layout(
        height=540, hovermode="x unified",
        margin=dict(l=0, r=0, t=36, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1,
                     spikecolor="#bbb", spikedash="dot", gridcolor="#efefef")
    fig.update_yaxes(ticksuffix=" €", gridcolor="#efefef")
    return fig


def build_stack_chart(payload: dict) -> go.Figure:
    dates  = payload["dates"]
    labels = payload["stack"]["labels"]
    series = payload["stack"]["series"]

    fig = go.Figure()
    for i, (label, vals) in enumerate(zip(labels, series)):
        color = COLORS[i % len(COLORS)]
        fig.add_trace(go.Scatter(
            x=dates, y=vals, name=label,
            stackgroup="one",
            line=dict(color=color, width=0.6),
            fillcolor=_rgba(color, 0.72),
            hovertemplate="%{y:,.2f} €<extra>" + label + "</extra>",
        ))
    fig.update_layout(
        height=400, hovermode="x unified",
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.01,
                    xanchor="left", x=0, font=dict(size=10)),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1,
                     spikecolor="#bbb", spikedash="dot", gridcolor="#efefef")
    fig.update_yaxes(ticksuffix=" €", gridcolor="#efefef")
    return fig


def build_pie_chart(labels: list[str], values: list[float], title: str = "") -> go.Figure:
    pairs = sorted(zip(labels, values), key=lambda x: x[1], reverse=True)
    pairs = [(l, v) for l, v in pairs if v > 0.5]
    if not pairs:
        return go.Figure()
    ls, vs = zip(*pairs)
    color_map = {lab: COLORS[i % len(COLORS)]
                 for i, lab in enumerate(payload["stack"]["labels"])}
    colors = [color_map.get(l, COLORS[i % len(COLORS)]) for i, l in enumerate(ls)]
    fig = go.Figure(go.Pie(
        labels=ls, values=vs, sort=False,
        texttemplate="%{label}<br>%{percent:.1%}",
        hovertemplate="%{label}: %{value:,.2f} €  (%{percent:.1%})<extra></extra>",
        marker=dict(colors=colors, line=dict(color="#fff", width=1)),
        textfont=dict(size=11),
    ))
    fig.update_layout(
        height=420, margin=dict(l=0, r=0, t=30, b=0),
        showlegend=False,
        title=dict(text=title, x=0.5, font=dict(size=12, color="#666")),
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def pie_from_stack_index(payload: dict, date_idx: int) -> tuple[list, list]:
    labels = payload["stack"]["labels"]
    series = payload["stack"]["series"]
    vals   = [s[date_idx] if date_idx < len(s) else 0.0 for s in series]
    return labels, vals


def pie_current(payload: dict) -> tuple[list, list]:
    pos = [(p["name"], p["cur_value"]) for p in payload["positions"]]
    if not pos:
        return [], []
    ls, vs = zip(*pos)
    return list(ls), list(vs)

# ---------------------------------------------------------------- Positions-Tabelle
def build_positions_table(positions: list) -> pd.DataFrame:
    rows = []
    for p in positions:
        rows.append({
            "Aktie":       p["name"].title(),
            "Ticker":      p["ticker"],
            "Status":      p["status"],
            "Investiert":  p["buys"],
            "Erlös+Wert":  p["returned"],
            "Dividenden":  p["dividends"],
            "G/V (€)":     p["total"],
            "Rendite (%)": p["ret_pct"],
            "Depotwert":   p["cur_value"],
        })
    return pd.DataFrame(rows)


def style_positions(df: pd.DataFrame):
    def color_num(val):
        try:
            v = float(val)
            return f"color: {'#1a7a3a' if v >= 0 else '#c0241c'}; font-weight: 600"
        except (ValueError, TypeError):
            return ""
    return df.style.map(color_num, subset=["G/V (€)", "Rendite (%)"])

# ================================================================ HAUPTAPP
st.set_page_config(page_title="Portfolio Analyse", layout="wide", page_icon="📈")
_inject_css()

st.title("📈 Portfolio Analyse")

con = get_db()

# ---------------------------------------------------------------- Sidebar
with st.sidebar:
    st.header("Daten")
    depot_cnt, konto_cnt = row_counts(con)
    ca, cb = st.columns(2)
    ca.metric("Depot", f"{depot_cnt} Zeilen")
    cb.metric("Konto",  f"{konto_cnt} Zeilen")

    st.divider()

    depot_file = st.file_uploader("Depot-Export (CSV)", type=["csv"], key="dep")
    konto_file = st.file_uploader("Konto-Export (CSV)", type=["csv"], key="kon",
                                  help="Optional – für echte Dividenden")

    if st.button("Importieren", type="primary", disabled=depot_file is None):
        if depot_file:
            n, d = import_csv(con, "depot_rows", depot_file.read(), depot_file.name)
            st.success(f"Depot: **{n}** neu, {d} Duplikate")
        if konto_file:
            n, d = import_csv(con, "konto_rows", konto_file.read(), konto_file.name)
            st.success(f"Konto: **{n}** neu, {d} Duplikate")
        depot_cnt, konto_cnt = row_counts(con)
        st.rerun()

    st.divider()

    if st.button("Analysieren", type="primary", disabled=depot_cnt == 0,
                 help="Berechnet Portfolio und lädt aktuelle Kurse"):
        st.session_state["run"] = True

    if st.button("Cache leeren", help="Kurse werden beim nächsten Analysieren neu abgerufen"):
        import shutil
        if os.path.exists(CACHE_DIR):
            shutil.rmtree(CACHE_DIR)
        st.toast("Cache geleert – Kurse werden neu geladen")

    if st.button("Alle Daten löschen", type="secondary"):
        clear_db(con)
        for k in ("payload", "run", "analysed_at"):
            st.session_state.pop(k, None)
        st.rerun()

# ---------------------------------------------------------------- Analyse
if st.session_state.get("run") and depot_cnt > 0:
    st.session_state["run"] = False
    tx_path = cash_path = None
    try:
        with st.spinner("Analysiere Portfolio…"):
            tx_path   = export_temp(con, "depot_rows")
            cash_path = export_temp(con, "konto_rows")
            os.makedirs(CACHE_DIR, exist_ok=True)
            payload, _ = pa.compute_payload(tx_path, cash_path, cache_dir=CACHE_DIR)
            st.session_state["payload"] = payload
            st.session_state["analysed_at"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    except Exception as e:
        st.error(f"Fehler: {e}")
        raise
    finally:
        for p in (tx_path, cash_path):
            if p and os.path.exists(p):
                os.unlink(p)

# ---------------------------------------------------------------- Ergebnisse
payload = st.session_state.get("payload")
if not payload:
    st.info("Flatex-CSV-Exporte hochladen und **Analysieren** klicken.")
    st.stop()

stats       = payload["stats"]
analysed_at = st.session_state.get("analysed_at", "")

# ---- KPI-Karten
st.markdown(f"**Stand {payload['today']}**" +
            (f"  ·  analysiert {analysed_at}" if analysed_at else ""))

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Depotwert",          _de(stats["cur_value"]))
k2.metric("Investiert (netto)", _de(stats["invested"]))
k3.metric("Gesamt G/V",         _de(stats["total_pl"]),
          delta=_pct(stats["total_pl"] / stats["invested"] * 100) if stats["invested"] else None)
k4.metric("XIRR",               f"{stats['xirr']:+.2f} % p.a.")
k5.metric("Dividenden",          _de(stats["dividends"]),
          delta="lt. Konto" if stats["div_real"] else "Schätzung", delta_color="off")

if payload.get("incomplete"):
    st.warning("Unvollständige Historie: " + ", ".join(payload["incomplete"]) +
               " → vollständigen Export ab Depoteröffnung verwenden.")

# ================================================================ Chart 1: Performance
st.markdown('<div class="section-card"><p class="section-title">Depotentwicklung</p>',
            unsafe_allow_html=True)
st.plotly_chart(build_performance_chart(payload), use_container_width=True)
st.markdown("</div>", unsafe_allow_html=True)

# ================================================================ Chart 2: Stack + Pie
st.markdown('<div class="section-card"><p class="section-title">Depotzusammensetzung über die Zeit</p>',
            unsafe_allow_html=True)

col_stack, col_pie = st.columns([3, 2])

with col_stack:
    ev = st.plotly_chart(
        build_stack_chart(payload),
        use_container_width=True,
        on_select="rerun",
        key="stack_select",
    )

# Pie: bei Klick Zusammensetzung an dem Datum, sonst aktuelle Aufteilung
pie_date_label = "Aktuelle Aufteilung"
pie_labels, pie_values = pie_current(payload)

pts = (ev.selection.points if ev and ev.selection else [])
if pts:
    clicked_x = pts[0].get("x")
    dates = payload["dates"]
    if clicked_x in dates:
        idx = dates.index(clicked_x)
        pie_labels, pie_values = pie_from_stack_index(payload, idx)
        pie_date_label = clicked_x

with col_pie:
    st.plotly_chart(
        build_pie_chart(pie_labels, pie_values, pie_date_label),
        use_container_width=True,
    )
    st.markdown('<p class="pie-hint">Auf Balken im Verlauf klicken → Aufteilung zu diesem Zeitpunkt</p>',
                unsafe_allow_html=True)

st.markdown("</div>", unsafe_allow_html=True)

# ================================================================ Positionen-Tabelle
st.markdown('<div class="section-card"><p class="section-title">Positionen</p>',
            unsafe_allow_html=True)

df_pos = build_positions_table(payload["positions"])
st.dataframe(
    style_positions(df_pos),
    use_container_width=True,
    hide_index=True,
    column_config={
        "Investiert":  st.column_config.NumberColumn(format="%.2f €"),
        "Erlös+Wert":  st.column_config.NumberColumn(format="%.2f €"),
        "Dividenden":  st.column_config.NumberColumn(format="%.2f €"),
        "G/V (€)":     st.column_config.NumberColumn(format="%.2f €"),
        "Rendite (%)": st.column_config.NumberColumn(format="%.2f %%"),
        "Depotwert":   st.column_config.NumberColumn(format="%.2f €"),
    },
)

# Transaktionshistorie je Position
st.markdown("<br>**Transaktionshistorie** – Position auswählen:", unsafe_allow_html=True)
pos_names = [p["name"].title() for p in payload["positions"] if p.get("txns")]
if pos_names:
    sel = st.selectbox("Position", pos_names, label_visibility="collapsed")
    sel_pos = next((p for p in payload["positions"]
                    if p["name"].title() == sel and p.get("txns")), None)
    if sel_pos:
        df_txn = pd.DataFrame(sel_pos["txns"])
        df_txn.columns = ["Datum", "Art", "Stück", "Kurs (€)", "Betrag (€)"]
        st.dataframe(df_txn, use_container_width=True, hide_index=True)

st.markdown("</div>", unsafe_allow_html=True)

# ================================================================ Cashflow-Details
with st.expander("Cashflow-Details & Vergleich"):
    d1, d2 = st.columns(2)
    with d1:
        st.markdown("**Cashflows**")
        rows_cf = [
            ("Gesamt eingezahlt",   stats["buys"]),
            ("Gesamt ausgezahlt",   stats["sells"]),
            ("Realisierte G/V",     stats["realized"]),
            ("Unrealisierte G/V",   stats["unreal"]),
        ]
        if stats.get("deposits") is not None:
            rows_cf += [
                ("Einzahlungen auf Flatex",   stats["deposits"]),
                ("Auszahlungen von Flatex",   stats["withdrawals"]),
                ("Zinsen / Gebühren",         stats.get("interest", 0) + stats.get("fees", 0)),
            ]
        for label, val in rows_cf:
            color = "pos-green" if val >= 0 else "pos-red"
            st.markdown(f'<span>{label}:</span> <span class="{color}">{_de(val, sign=True)}</span>',
                        unsafe_allow_html=True)
    with d2:
        st.markdown("**Vergleich**")
        if stats.get("bench") is not None:
            diff_bench = stats["bench"] - stats["cur_value"]
            better = diff_bench <= 0
            st.markdown(f"MSCI World-Depotwert: **{_de(stats['bench'])}**")
            st.markdown(f"Unterschied: **{_de(diff_bench, sign=True)}** "
                        f"({'du bist besser' if better else 'MSCI World wäre besser'})")
            if stats.get("bench_xirr"):
                st.markdown(f"MSCI World XIRR: **{stats['bench_xirr']:+.2f} % p.a.**")
        an = payload.get("analysis", {})
        if an.get("best"):
            st.markdown("---")
            st.markdown(f"Beste Position: **{an['best']['name']}** "
                        f"({_pct(an['best']['ret_pct'])})")
        if an.get("worst"):
            st.markdown(f"Schlechteste Position: **{an['worst']['name']}** "
                        f"({_pct(an['worst']['ret_pct'])})")
