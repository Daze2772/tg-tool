"""
Minimal FastAPI dashboard — live progress, session health,
rate-limit events, user counts, CSV export.
"""
import json
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TG Tool — Dashboard</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0d1117; color: #c9d1d9; padding: 24px; }
h1 { font-size: 1.5rem; margin-bottom: 16px; color: #58a6ff; }
h2 { font-size: 1.1rem; margin: 20px 0 10px; color: #8b949e; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
        gap: 16px; margin-bottom: 24px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
        padding: 16px; }
.card h3 { font-size: 0.9rem; color: #8b949e; margin-bottom: 8px; }
.stat { font-size: 2rem; font-weight: 700; color: #58a6ff; }
.stat-label { font-size: 0.8rem; color: #8b949e; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; }
th, td { text-align: left; padding: 6px 10px; font-size: 0.85rem;
         border-bottom: 1px solid #21262d; }
th { color: #8b949e; font-weight: 500; }
tr:hover { background: #1c2129; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px;
         font-size: 0.75rem; font-weight: 600; }
.badge-ok { background: #1b4728; color: #3fb950; }
.badge-warn { background: #5c3a1d; color: #d29922; }
.badge-bad { background: #4a1d1d; color: #f85149; }
.bar { height: 6px; background: #21262d; border-radius: 3px; margin-top: 4px;
       overflow: hidden; }
.bar-fill { height: 100%; background: #58a6ff; border-radius: 3px;
            transition: width 0.5s; }
.events { max-height: 300px; overflow-y: auto; margin-top: 8px; }
.event { padding: 4px 0; font-size: 0.8rem; border-bottom: 1px solid #21262d;
         font-family: 'SF Mono', monospace; }
.btn { background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
       padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 0.85rem;
       text-decoration: none; display: inline-block; }
.btn:hover { background: #30363d; }
.btn-primary { background: #1f6feb; border-color: #1f6feb; color: #fff; }
.btn-primary:hover { background: #388bfd; }
.controls { margin: 16px 0; display: flex; gap: 8px; flex-wrap: wrap; }
</style>
</head>
<body>
<h1>⚡ TG Tool Dashboard</h1>
<div class="grid">
  <div class="card">
    <h3>👥 Users Scraped</h3>
    <div class="stat" id="total_users">-</div>
    <div class="stat-label">in database</div>
  </div>
  <div class="card">
    <h3>✅ Added to Channel</h3>
    <div class="stat" id="added_users">-</div>
    <div class="stat-label">successfully added</div>
  </div>
  <div class="card">
    <h3>📋 Adder Progress</h3>
    <div class="stat" id="adder_status">idle</div>
    <div class="bar"><div class="bar-fill" id="adder_bar" style="width:0%"></div></div>
  </div>
</div>

<h2>📊 Scraper Progress</h2>
<div id="scraper_progress" class="card">
  <p style="color:#8b949e">Waiting for data...</p>
</div>

<h2>🔌 Session Health</h2>
<table>
<thead><tr><th>Session</th><th>Health</th><th>Status</th><th>Uses</th><th>Flood Waits</th><th>Success Rate</th><th>Proxy</th></tr></thead>
<tbody id="sessions_body"><tr><td colspan="7" style="color:#8b949e">Waiting...</td></tr></tbody>
</table>

<h2>⚠️ Rate Limit Events</h2>
<div class="events" id="rate_events">
  <p style="color:#8b949e">No events yet</p>
</div>

<div class="controls">
  <a href="/api/export" class="btn btn-primary">📥 Export CSV</a>
  <button class="btn" onclick="location.reload()">🔄 Refresh</button>
</div>

<script>
async function poll() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    document.getElementById('total_users').textContent = d.user_count ?? '-';
    document.getElementById('added_users').textContent = d.added_count ?? '-';
    document.getElementById('adder_status').textContent = d.adder?.status ?? 'idle';

    const added = d.adder?.added || 0;
    const failed = d.adder?.failed || 0;
    const total = added + failed;
    const pct = total > 0 ? (added / total * 100) : 0;
    document.getElementById('adder_bar').style.width = pct + '%';

    // Scraper progress
    const sp = document.getElementById('scraper_progress');
    if (d.scraper && Object.keys(d.scraper).length) {
      sp.innerHTML = Object.entries(d.scraper).map(([k,v]) =>
        `<div style="margin:4px 0"><b>${k}</b>: ${v.scraped ?? 0}/${v.total ?? '?'} — <span style="color:${v.status==='done'?'#3fb950':'#d29922'}">${v.status}</span></div>`
      ).join('');
    }

    // Sessions
    const sb = document.getElementById('sessions_body');
    if (d.sessions?.length) {
      sb.innerHTML = d.sessions.map(s =>
        `<tr>
          <td>${s.id}</td>
          <td>${s.health}</td>
          <td><span class="badge ${s.quarantined?'badge-bad':'badge-ok'}">${s.quarantined ? 'QUARANTINED' : 'active'}</span></td>
          <td>${s.uses}</td>
          <td>${s.flood_waits}</td>
          <td>${s.success_rate}</td>
          <td>${s.proxy}</td>
        </tr>`
      ).join('');
    }

    // Rate limits
    if (d.rate_events?.length) {
      document.getElementById('rate_events').innerHTML = d.rate_events.map(e =>
        `<div class="event">[${e.timestamp}] <b>${e.type}</b> user=${e.user_id} session=${e.session} ${e.seconds ? 'wait='+e.seconds+'s' : ''}</div>`
      ).join('');
    }
  } catch(e) { console.error(e); }
}
poll();
setInterval(poll, 3000);
</script>
</body>
</html>"""


def create_dashboard_app(scraper=None, adder=None, session_pool=None, database=None):
    app = FastAPI(title="TG Tool Dashboard")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return TEMPLATE

    @app.get("/api/status")
    async def status():
        data = {
            "user_count": await database.count_users() if database else 0,
            "added_count": await database.count_added() if database else 0,
            "sources": await database.count_by_source() if database else {},
            "scraper": scraper.get_progress() if scraper else {},
            "adder": adder.get_progress() if adder else {},
            "sessions": session_pool.get_status() if session_pool else [],
            "rate_events": adder.get_rate_limit_events()[-50:] if adder else [],
        }
        return data

    @app.get("/api/export")
    async def export_csv():
        if database is None:
            return PlainTextResponse("No database", status_code=500)
        path = "data/export.csv"
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        count = await database.export_csv(path)
        if count == 0:
            return PlainTextResponse("No data to export", status_code=404)
        return FileResponse(path, filename="tg_users.csv", media_type="text/csv")

    return app
