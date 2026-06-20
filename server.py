#!/usr/bin/env python3
"""Flask-Server: Flatex Portfolio-Analyse.

Start lokal:
    pip install flask
    python3 server.py

Docker:
    docker compose up
"""
import hashlib, json, os, shutil, sqlite3, tempfile
from datetime import datetime

from flask import Flask, Response, redirect, request, url_for

import portfolio_analyse as pa

app = Flask(__name__)

DB_PATH   = os.environ.get("DB_PATH",   "data/portfolio.db")
CACHE_DIR = os.environ.get("CACHE_DIR", "data/cache")

# ---------------------------------------------------------------- DB
def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS depot_rows
                   (hash TEXT PRIMARY KEY, row TEXT, source TEXT, imported_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS konto_rows
                   (hash TEXT PRIMARY KEY, row TEXT, source TEXT, imported_at TEXT)""")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    return con

def import_csv(con, table, raw, filename):
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

def export_temp(con, table):
    hdr = con.execute("SELECT value FROM meta WHERE key=?",
                      (f"{table}_header",)).fetchone()
    if not hdr:
        return None
    rows = con.execute(f"SELECT row FROM {table}").fetchall()
    if not rows:
        return None
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".csv",
                                     delete=False, encoding="utf-8", newline="")
    tf.write(hdr[0] + "\n")
    for (r,) in rows:
        tf.write(r + "\n")
    tf.close()
    return tf.name

def row_counts(con):
    d = con.execute("SELECT COUNT(*) FROM depot_rows").fetchone()[0]
    k = con.execute("SELECT COUNT(*) FROM konto_rows").fetchone()[0]
    return d, k

# ---------------------------------------------------------------- Upload-Seite
def _page(depot_cnt, konto_cnt, messages=None):
    msgs_html = ""
    if messages:
        items = "".join(f"<li>{m}</li>" for m in messages)
        msgs_html = f'<ul class="msgs">{items}</ul>'

    ready = depot_cnt > 0
    analyse_btn = (
        '<a href="/analyse" class="btn btn-primary">Analysieren</a>'
        if ready else
        '<span class="btn btn-primary disabled">Analysieren</span>'
    )

    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio Analyse</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          background: #f4f5f7; color: #1b1b1f; min-height: 100vh; }}
  .wrap {{ max-width: 640px; margin: 0 auto; padding: 3rem 1.5rem; }}
  h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 0.25rem; }}
  .sub {{ color: #666; font-size: 0.9rem; margin-bottom: 2rem; }}

  .card {{ background: #fff; border: 1px solid #e2e2e6; border-radius: 12px;
           padding: 1.5rem; margin-bottom: 1.25rem; }}
  .card h2 {{ font-size: 0.85rem; font-weight: 600; color: #555;
              text-transform: uppercase; letter-spacing: .05em; margin-bottom: 1rem; }}

  .stats {{ display: flex; gap: 2rem; }}
  .stat .num {{ font-size: 1.8rem; font-weight: 700; color: #2166ac; line-height: 1; }}
  .stat .lbl {{ font-size: 0.8rem; color: #888; margin-top: 0.2rem; }}

  .dropzone {{ border: 2px dashed #c8cad0; border-radius: 8px; padding: 1.25rem 1rem;
               text-align: center; cursor: pointer; transition: border-color .2s, background .2s;
               position: relative; }}
  .dropzone:hover, .dropzone.over {{ border-color: #2166ac; background: #f0f5ff; }}
  .dropzone input[type=file] {{ position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; }}
  .dropzone .icon {{ font-size: 1.6rem; margin-bottom: 0.4rem; }}
  .dropzone .label {{ font-size: 0.9rem; font-weight: 500; }}
  .dropzone .hint  {{ font-size: 0.78rem; color: #999; margin-top: 0.2rem; }}
  .dropzone .fname {{ font-size: 0.82rem; color: #2166ac; margin-top: 0.4rem;
                      font-weight: 500; display: none; }}
  .zones {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.9rem; margin-bottom: 1rem; }}

  .actions {{ display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: center; }}
  .btn {{ display: inline-block; padding: 0.55rem 1.25rem; border-radius: 8px;
          font-size: 0.9rem; font-weight: 600; text-decoration: none;
          border: none; cursor: pointer; transition: opacity .15s; }}
  .btn-primary {{ background: #2166ac; color: #fff; }}
  .btn-primary:hover {{ opacity: .88; }}
  .btn-primary.disabled {{ background: #b0bec5; pointer-events: none; }}
  .btn-secondary {{ background: #fff; color: #444; border: 1px solid #ccc; }}
  .btn-secondary:hover {{ background: #f5f5f5; }}
  .btn-danger {{ background: #fff; color: #c0241c; border: 1px solid #f5c6c4; font-size: 0.82rem; }}
  .btn-danger:hover {{ background: #fdf3f3; }}

  .msgs {{ margin: 0.75rem 0 0; padding-left: 1.2rem; font-size: 0.88rem; color: #2a6; }}
  .msgs li {{ margin-bottom: 0.2rem; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>📈 Portfolio Analyse</h1>
  <p class="sub">Flatex-Export hochladen · Kurse werden automatisch abgerufen</p>

  <div class="card">
    <h2>Gespeicherte Daten</h2>
    <div class="stats">
      <div class="stat">
        <div class="num">{depot_cnt}</div>
        <div class="lbl">Depot-Zeilen</div>
      </div>
      <div class="stat">
        <div class="num">{konto_cnt}</div>
        <div class="lbl">Konto-Zeilen</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>CSV-Import</h2>
    <form method="post" action="/import" enctype="multipart/form-data" id="importForm">
      <div class="zones">
        <div class="dropzone" id="dz-depot">
          <input type="file" name="depot" accept=".csv" id="inp-depot">
          <div class="icon">📄</div>
          <div class="label">Depot-Export</div>
          <div class="hint">Wertpapier-Transaktionen</div>
          <div class="fname" id="fn-depot"></div>
        </div>
        <div class="dropzone" id="dz-konto">
          <input type="file" name="konto" accept=".csv" id="inp-konto">
          <div class="icon">💶</div>
          <div class="label">Konto-Export</div>
          <div class="hint">Optional · für echte Dividenden</div>
          <div class="fname" id="fn-konto"></div>
        </div>
      </div>
      <div class="actions">
        <button type="submit" class="btn btn-secondary">Importieren</button>
        {analyse_btn}
        <a href="/clear-cache" class="btn btn-secondary"
           onclick="return confirm('Cache leeren?')">Cache leeren</a>
        <a href="/reset" class="btn btn-danger"
           onclick="return confirm('Alle gespeicherten Daten löschen?')">Daten löschen</a>
      </div>
    </form>
    {msgs_html}
  </div>
</div>

<script>
  function wire(inpId, fnId, dzId) {{
    const inp = document.getElementById(inpId);
    const fn  = document.getElementById(fnId);
    const dz  = document.getElementById(dzId);
    inp.addEventListener("change", () => {{
      if (inp.files.length) {{
        fn.textContent = "✓ " + inp.files[0].name;
        fn.style.display = "block";
      }}
    }});
    dz.addEventListener("dragover",  e => {{ e.preventDefault(); dz.classList.add("over"); }});
    dz.addEventListener("dragleave", () => dz.classList.remove("over"));
    dz.addEventListener("drop", e => {{
      e.preventDefault(); dz.classList.remove("over");
      const dt = e.dataTransfer;
      if (dt.files.length) {{
        // DataTransfer -> FileList ist nicht direkt zuweisbar; wir nutzen den input
        const file = dt.files[0];
        const transfer = new DataTransfer();
        transfer.items.add(file);
        inp.files = transfer.files;
        fn.textContent = "✓ " + file.name;
        fn.style.display = "block";
      }}
    }});
  }}
  wire("inp-depot", "fn-depot", "dz-depot");
  wire("inp-konto", "fn-konto", "dz-konto");
</script>
</body>
</html>"""

# ---------------------------------------------------------------- Routen
@app.route("/")
def index():
    con = get_db()
    d, k = row_counts(con)
    con.close()
    return _page(d, k)

@app.route("/import", methods=["POST"])
def import_data():
    con = get_db()
    msgs = []
    for field, table in [("depot", "depot_rows"), ("konto", "konto_rows")]:
        f = request.files.get(field)
        if f and f.filename:
            n, d = import_csv(con, table, f.read(), f.filename)
            label = "Depot" if field == "depot" else "Konto"
            msgs.append(f"{label}: {n} neue Zeilen importiert, {d} Duplikate übersprungen")
    d, k = row_counts(con)
    con.close()
    return _page(d, k, msgs)

@app.route("/analyse")
def analyse():
    con = get_db()
    tx_path = cash_path = None
    try:
        tx_path   = export_temp(con, "depot_rows")
        cash_path = export_temp(con, "konto_rows")
        if not tx_path:
            return redirect(url_for("index"))
        os.makedirs(CACHE_DIR, exist_ok=True)
        payload, _ = pa.compute_payload(tx_path, cash_path, cache_dir=CACHE_DIR)
        html = pa._HTML_TEMPLATE.replace(
            "__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
        return Response(html, content_type="text/html; charset=utf-8")
    finally:
        con.close()
        for p in (tx_path, cash_path):
            if p and os.path.exists(p):
                os.unlink(p)

@app.route("/clear-cache")
def clear_cache():
    if os.path.exists(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
    return redirect(url_for("index"))

@app.route("/reset")
def reset():
    con = get_db()
    for tbl in ("depot_rows", "konto_rows", "meta"):
        con.execute(f"DELETE FROM {tbl}")
    con.commit()
    con.close()
    return redirect(url_for("index"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
