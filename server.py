#!/usr/bin/env python3
"""Flask-Server: Flatex Portfolio-Analyse.

Lokal starten:
    pip install flask
    python3 server.py

Docker:
    docker compose up --build
"""
import hashlib, json, os, queue, shutil, sqlite3, tempfile, threading
from datetime import datetime

from flask import Flask, Response, redirect, request, stream_with_context, url_for

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
_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #f4f5f7; color: #1b1b1f; min-height: 100vh; }
.wrap { max-width: 660px; margin: 0 auto; padding: 3rem 1.5rem; }
h1 { font-size: 1.6rem; font-weight: 700; margin-bottom: 0.25rem; }
.sub { color: #666; font-size: 0.9rem; margin-bottom: 2rem; }

.card { background: #fff; border: 1px solid #e2e2e6; border-radius: 12px;
        padding: 1.5rem; margin-bottom: 1.25rem; }
.card h2 { font-size: 0.8rem; font-weight: 700; color: #777;
           text-transform: uppercase; letter-spacing: .06em; margin-bottom: 1.1rem; }

.stats { display: flex; gap: 2.5rem; }
.stat .num { font-size: 2rem; font-weight: 700; color: #2166ac; line-height: 1; }
.stat .lbl { font-size: 0.78rem; color: #999; margin-top: 0.2rem; }

/* Dropzonen */
.zones { display: grid; grid-template-columns: 1fr 1fr; gap: 0.9rem; margin-bottom: 1rem; }
.dropzone { border: 2px dashed #cdd0d8; border-radius: 10px; padding: 1.2rem 0.8rem;
            text-align: center; cursor: pointer; transition: border-color .2s, background .2s;
            position: relative; user-select: none; }
.dropzone.over, .dropzone:hover { border-color: #2166ac; background: #f0f5ff; }
.dropzone.has-files { border-color: #4dac26; border-style: solid; background: #f4fbf0; }
.dropzone input[type=file] { position: absolute; inset: 0; opacity: 0;
                              cursor: pointer; width: 100%; height: 100%; }
.dropzone .icon { font-size: 1.6rem; margin-bottom: 0.4rem; pointer-events: none; }
.dropzone .label { font-size: 0.9rem; font-weight: 600; pointer-events: none; }
.dropzone .hint  { font-size: 0.76rem; color: #aaa; margin-top: 0.2rem; pointer-events: none; }
.dropzone .flist { font-size: 0.78rem; color: #2a7a1a; margin-top: 0.5rem;
                   font-weight: 500; text-align: left; pointer-events: none; }

/* Buttons */
.actions { display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: center; margin-top: 0.5rem; }
.btn { display: inline-block; padding: 0.55rem 1.3rem; border-radius: 8px;
       font-size: 0.9rem; font-weight: 600; text-decoration: none;
       border: none; cursor: pointer; transition: opacity .15s; white-space: nowrap; }
.btn-primary   { background: #2166ac; color: #fff; }
.btn-primary:hover { opacity: .88; }
.btn-primary.disabled { background: #b0bec5; pointer-events: none; }
.btn-secondary { background: #fff; color: #444; border: 1px solid #ccc; }
.btn-secondary:hover { background: #f5f5f5; }
.btn-danger { background: #fff; color: #c0241c; border: 1px solid #f5c6c4;
              font-size: 0.82rem; }
.btn-danger:hover { background: #fdf3f3; }

/* Meldungen */
.msgs { margin-top: 0.9rem; padding-left: 1.2rem; font-size: 0.88rem; color: #2a7a1a; }
.msgs li { margin-bottom: 0.2rem; }

/* Progress-Seite */
.progress-wrap { max-width: 660px; margin: 0 auto; padding: 3rem 1.5rem; }
.progress-bar-outer { background: #e8e8ec; border-radius: 999px; height: 8px;
                       margin: 1.5rem 0 1rem; overflow: hidden; }
.progress-bar-inner { height: 100%; border-radius: 999px; background: #2166ac;
                       width: 0%; transition: width .4s ease; }
.log-box { background: #1a1a2e; color: #a8d8a8; font-family: "SF Mono", "Fira Code",
           monospace; font-size: 0.8rem; border-radius: 10px; padding: 1rem 1.2rem;
           min-height: 200px; max-height: 360px; overflow-y: auto;
           white-space: pre-wrap; word-break: break-word; }
.status-line { font-size: 0.9rem; color: #555; margin-bottom: 0.4rem; }
"""

def _page(depot_cnt, konto_cnt, messages=None):
    msgs_html = ""
    if messages:
        items = "".join(f"<li>{m}</li>" for m in messages)
        msgs_html = f'<ul class="msgs">{items}</ul>'
    analyse_cls = "" if depot_cnt > 0 else " disabled"
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio Analyse</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
  <h1>📈 Portfolio Analyse</h1>
  <p class="sub">Flatex-Export hochladen · Kurse werden automatisch abgerufen</p>

  <div class="card">
    <h2>Gespeicherte Daten</h2>
    <div class="stats">
      <div class="stat"><div class="num">{depot_cnt}</div><div class="lbl">Depot-Zeilen</div></div>
      <div class="stat"><div class="num">{konto_cnt}</div><div class="lbl">Konto-Zeilen</div></div>
    </div>
  </div>

  <div class="card">
    <h2>CSV-Import</h2>
    <form method="post" action="/import" enctype="multipart/form-data" id="frm">
      <div class="zones">
        <div class="dropzone" id="dz-d">
          <input type="file" name="depot" accept=".csv" multiple id="inp-d">
          <div class="icon">📄</div>
          <div class="label">Depot-Export</div>
          <div class="hint">Wertpapier-Transaktionen<br>Mehrere Dateien möglich</div>
          <div class="flist" id="fl-d"></div>
        </div>
        <div class="dropzone" id="dz-k">
          <input type="file" name="konto" accept=".csv" multiple id="inp-k">
          <div class="icon">💶</div>
          <div class="label">Konto-Export</div>
          <div class="hint">Optional · echte Dividenden<br>Mehrere Dateien möglich</div>
          <div class="flist" id="fl-k"></div>
        </div>
      </div>
      <div class="actions">
        <button type="submit" class="btn btn-secondary">Importieren</button>
        <a href="/analyse" class="btn btn-primary{analyse_cls}">Analysieren</a>
        <a href="/clear-cache" class="btn btn-secondary"
           onclick="return confirm('Kurs-Cache leeren?')">Cache leeren</a>
        <a href="/reset" class="btn btn-danger"
           onclick="return confirm('Alle gespeicherten Daten löschen?')">Daten löschen</a>
      </div>
    </form>
    {msgs_html}
  </div>
</div>
<script>
function wire(inpId, flId, dzId) {{
  const inp = document.getElementById(inpId);
  const fl  = document.getElementById(flId);
  const dz  = document.getElementById(dzId);
  function showFiles(files) {{
    if (!files.length) return;
    dz.classList.add("has-files");
    fl.innerHTML = Array.from(files).map(f => "✓ " + f.name).join("<br>");
  }}
  inp.addEventListener("change", () => showFiles(inp.files));
  dz.addEventListener("dragover",  e => {{ e.preventDefault(); dz.classList.add("over"); }});
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", e => {{
    e.preventDefault(); dz.classList.remove("over");
    const dt = new DataTransfer();
    Array.from(e.dataTransfer.files)
         .filter(f => f.name.endsWith(".csv"))
         .forEach(f => dt.items.add(f));
    inp.files = dt.files;
    showFiles(dt.files);
  }});
}}
wire("inp-d", "fl-d", "dz-d");
wire("inp-k", "fl-k", "dz-k");
</script>
</body></html>"""


def _progress_page():
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Analysiere …</title>
<style>{_CSS}</style></head>
<body><div class="progress-wrap">
  <h1>📈 Portfolio Analyse</h1>
  <p class="sub" id="status">Starte Analyse …</p>
  <div class="progress-bar-outer">
    <div class="progress-bar-inner" id="bar"></div>
  </div>
  <div class="log-box" id="log"></div>
</div>
<script>
const bar    = document.getElementById("bar");
const logBox = document.getElementById("log");
const status = document.getElementById("status");

const steps = [
  "Transaktionen laden",
  "Ticker auflösen",
  "Splits ableiten",
  "Verrechnungskonto",
  "Kurse & Marktwerte",
  "XIRR & Benchmark",
  "Report zusammenstellen",
];
let stepIdx = 0;
function progress() {{
  const pct = Math.min(95, Math.round((stepIdx / steps.length) * 100));
  bar.style.width = pct + "%";
}}

const es = new EventSource("/analyse-stream");
es.onmessage = (e) => {{
  const d = JSON.parse(e.data);
  if (d.type === "log") {{
    logBox.textContent += d.msg + "\\n";
    logBox.scrollTop = logBox.scrollHeight;
    status.textContent = d.msg.replace(/^\\s+/, "");
    steps.forEach((s, i) => {{ if (d.msg.includes(s)) stepIdx = i + 1; }});
    progress();
  }} else if (d.type === "done") {{
    bar.style.width = "100%";
    bar.style.background = "#4dac26";
    status.textContent = "Fertig – öffne Report …";
    es.close();
    setTimeout(() => window.location.href = "/report", 600);
  }} else if (d.type === "error") {{
    bar.style.background = "#d12c20";
    status.textContent = "Fehler: " + d.msg;
    logBox.textContent += "\\nFEHLER: " + d.msg;
    es.close();
  }}
}};
es.onerror = () => {{
  status.textContent = "Verbindung unterbrochen.";
  es.close();
}};
</script>
</body></html>"""

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
    for field, table, label in [("depot", "depot_rows", "Depot"),
                                 ("konto", "konto_rows", "Konto")]:
        files = request.files.getlist(field)
        for f in files:
            if f and f.filename:
                n, d = import_csv(con, table, f.read(), f.filename)
                msgs.append(f'{label} „{f.filename}“: {n} neu, {d} Duplikate')
    d, k = row_counts(con)
    con.close()
    return _page(d, k, msgs)

@app.route("/analyse")
def analyse():
    con = get_db()
    d, _ = row_counts(con)
    con.close()
    if d == 0:
        return redirect(url_for("index"))
    return _progress_page()

@app.route("/analyse-stream")
def analyse_stream():
    def generate():
        log_q    = queue.Queue()
        result   = {}

        def log_fn(msg):
            log_q.put(("log", msg))

        def run():
            tx_path = cash_path = None
            try:
                con = get_db()
                tx_path   = export_temp(con, "depot_rows")
                cash_path = export_temp(con, "konto_rows")
                con.close()
                os.makedirs(CACHE_DIR, exist_ok=True)
                payload, _ = pa.compute_payload(
                    tx_path, cash_path, cache_dir=CACHE_DIR, log=log_fn)
                html = pa._HTML_TEMPLATE.replace(
                    "__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
                result["html"] = html
                log_q.put(("done", None))
            except Exception as exc:
                log_q.put(("error", str(exc)))
            finally:
                for p in (tx_path, cash_path):
                    if p and os.path.exists(p):
                        os.unlink(p)

        t = threading.Thread(target=run, daemon=True)
        t.start()

        while True:
            try:
                typ, data = log_q.get(timeout=180)
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping'})}\n\n"
                continue

            if typ == "log":
                yield f"data: {json.dumps({'type':'log','msg':data})}\n\n"
            elif typ == "done":
                # Report in DB speichern
                con = get_db()
                con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                            ("last_report", result["html"]))
                con.commit()
                con.close()
                yield f"data: {json.dumps({'type':'done'})}\n\n"
                break
            elif typ == "error":
                yield f"data: {json.dumps({'type':'error','msg':data})}\n\n"
                break

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/report")
def report():
    con = get_db()
    row = con.execute("SELECT value FROM meta WHERE key='last_report'").fetchone()
    con.close()
    if not row:
        return redirect(url_for("index"))
    return Response(row[0], content_type="text/html; charset=utf-8")

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
