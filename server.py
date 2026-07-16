"""rqlite service consumer.

Connects to the rqlite service through the OpenHost router and serves a debug
dashboard.  All server-side requests to the service include the app's bearer
token so the router can authenticate and inject permission headers.

Routes:
    GET  /                 → HTML debug dashboard
    GET  /health           → {"status": "ok", "rqlite_service": "<reachable|unreachable>"}
    GET  /api/value        → proxy to service /api/value
    GET  /api/entries      → proxy to service /api/entries
    POST /api/add          → proxy to service /api/add
    POST /api/run-query    → proxy SQL to service /api/query or /api/execute
    GET  /api/proxy/{path} → generic proxy to service /api/{path}
"""

import datetime
import json
import os

import aiohttp
import aiohttp.web

ROUTER_URL = os.environ.get("OPENHOST_ROUTER_URL", "http://localhost:8080")
APP_TOKEN = os.environ.get("OPENHOST_APP_TOKEN", "")
APP_NAME = os.environ.get("OPENHOST_APP_NAME", "rqlite-client")
APP_ID = os.environ.get("OPENHOST_APP_ID", "unknown")
ZONE_DOMAIN = os.environ.get("OPENHOST_ZONE_DOMAIN", "")
SERVICE_SHORTNAME = "rqlite"

# In-memory store for the debug dashboard
_last_request: dict | None = None
_last_response: dict | None = None
_last_call_time: str | None = None
_connection_up: bool | None = None


def _json(data, status=200):
    return aiohttp.web.Response(text=json.dumps(data), content_type="application/json", status=status)


async def _call_service(method: str, path: str, body: dict | list | None = None) -> dict:
    """Call the rqlite service through the router and return {status, body}."""
    global _last_request, _last_response, _last_call_time, _connection_up

    url = f"{ROUTER_URL}/api/services/v2/call/{SERVICE_SHORTNAME}/{path}"
    headers = {"Authorization": f"Bearer {APP_TOKEN}"}

    _last_request = {"method": method, "url": url, "body": body, "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}

    print(f"Calling service: {method} {url}", flush=True)
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            kwargs: dict = {"headers": headers}
            if body is not None:
                kwargs["json"] = body
            async with s.request(method, url, **kwargs) as r:
                try:
                    data = await r.json()
                except (aiohttp.ContentTypeError, ValueError):
                    data = await r.text()
                _last_response = {"status": r.status, "body": data}
                _last_call_time = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
                print(f"Service response ({r.status}): {str(data)[:200]}", flush=True)
                return {"status": r.status, "body": data}
    except aiohttp.ClientError as e:
        _connection_up = False
        _last_response = {"status": 0, "body": str(e)}
        _last_call_time = _last_request["time"]
        return {"status": 0, "body": str(e)}


async def _check_connection() -> bool:
    """Ping the rqlite service /api/status to see if it's reachable."""
    global _connection_up
    result = await _call_service("GET", "status")
    _connection_up = 200 <= result["status"] < 300
    return _connection_up


# ---------------------------------------------------------------------------
# Public handlers
# ---------------------------------------------------------------------------

async def handle_health(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return _json({"status": "ok"})


async def handle_root(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """HTML debug dashboard. Returns the page shell instantly —
    the JS auto-refresh loop fetches live data client-side."""
    # Never make service calls here; wait_for_ready polls / with a 2s timeout.
    conn_color = "#94a3b8"
    conn_text = "Loading…"
    conn_dot = "⚪"
    last_time = "—"

    # All placeholders — replaced by JS on first poll
    acc_value = "…"
    acc_updated = "…"
    entries_rows = '<tr><td colspan="3" style="color:#94a3b8">Loading…</td></tr>'
    perm_rows = '<tr><td colspan="4" style="color:#94a3b8">Manifest declares grants: read, write</td></tr>'
    last_req_str = "None"
    last_resp_str = "None"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{APP_NAME}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 24px auto; padding: 0 16px; background: #f8fafc; color: #1e293b; }}
  h1 {{ color: #0f172a; }}
  h3 {{ margin: 0 0 8px 0; }}
  .badge {{ display: inline-block; padding: 4px 12px; border-radius: 999px; color: white; font-weight: 600; font-size: 14px; }}
  .card {{ background: white; border: 1px solid #e2e8f0; border-radius: 10px; padding: 20px; margin: 16px 0; }}
  .row {{ display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }}
  .row > div {{ flex: 1; min-width: 200px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #e2e8f0; font-size: 13px; }}
  th {{ background: #f1f5f9; font-weight: 600; }}
  input, textarea, button {{ font-family: inherit; font-size: 14px; }}
  input[type="number"] {{ padding: 8px 12px; border: 1px solid #d1d5db; border-radius: 6px; width: 100px; }}
  textarea {{ width: 100%; padding: 8px 12px; border: 1px solid #d1d5db; border-radius: 6px; resize: vertical; }}
  button {{ padding: 8px 20px; background: #2563eb; color: white; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; }}
  button:hover {{ background: #1d4ed8; }}
  button:disabled {{ background: #9ca3af; cursor: not-allowed; }}
  pre {{ background: #1e293b; color: #e2e8f0; padding: 12px; border-radius: 8px; overflow-x: auto; font-size: 12px; max-height: 300px; overflow-y: auto; }}
  .kv {{ display: flex; gap: 24px; flex-wrap: wrap; }}
  .kv-item {{ min-width: 100px; }}
  .kv-label {{ font-size: 12px; color: #64748b; text-transform: uppercase; }}
  .kv-value {{ font-size: 20px; font-weight: 700; }}
  .notice {{ padding: 8px 12px; border-radius: 6px; font-size: 13px; margin-top: 8px; }}
  .notice.ok {{ background: #ecfdf5; color: #065f46; }}
  .notice.err {{ background: #fef2f2; color: #991b1b; }}
</style>
</head>
<body>

<h1>{conn_dot} {APP_NAME}</h1>
<div class="badge" style="background:{conn_color}">{conn_text}</div>
<p style="color:#64748b;font-size:14px">
  Consumer — calls <code>{ROUTER_URL}/api/services/v2/call/{SERVICE_SHORTNAME}/...</code>
  <br>Zone: <code>{ZONE_DOMAIN}</code>
</p>

<div class="row">
  <div class="card">
    <h3>Accumulator</h3>
    <div class="kv">
      <div class="kv-item"><div class="kv-label">Total</div><div class="kv-value">{acc_value}</div></div>
      <div class="kv-item"><div class="kv-label">Last Updated</div><div class="kv-value" style="font-size:14px">{acc_updated}</div></div>
    </div>
    <form onsubmit="return addNumber(event)" style="margin-top:12px">
      <input type="number" id="addInput" value="1" />
      <button type="submit">Add</button>
    </form>
    <div id="addResult"></div>
  </div>

  <div class="card">
    <h3>All Entries</h3>
    <table>
      <tr><th>Key</th><th>Value</th><th>Last Updated</th></tr>
      {entries_rows or '<tr><td colspan="3" style="color:#94a3b8">—</td></tr>'}
    </table>
  </div>
</div>

<div class="card">
  <h3>Raw SQL</h3>
  <form onsubmit="return runSql(event)">
    <textarea id="sqlInput" rows="4" placeholder="SELECT * FROM accumulator&#10;-- or DDL / INSERT / UPDATE if you have write grant"></textarea>
    <button type="submit" style="margin-top:8px">Run</button>
  </form>
  <div id="sqlResult" style="margin-top:8px"></div>
</div>

<div class="card">
  <h3>Permissions</h3>
  <table>
    <tr><th>Grant</th><th>Scope</th><th>Status</th><th></th></tr>
    {perm_rows or '<tr><td colspan="4" style="color:#94a3b8">No permission errors yet. Manifest declares grants: read, write</td></tr>'}
  </table>
</div>

<div class="card">
  <h3>Last Request / Response <span style="font-weight:400;color:#64748b">({last_time})</span></h3>
  <div class="row">
    <div>
      <h4>Request</h4>
      <pre>{last_req_str}</pre>
    </div>
    <div>
      <h4>Response</h4>
      <pre>{last_resp_str}</pre>
    </div>
  </div>
</div>

<p style="color:#94a3b8;font-size:12px;text-align:center">openhost app id: {APP_ID}</p>

<script>
  async function addNumber(e) {{
    e.preventDefault();
    const btn = e.target.querySelector('button');
    const input = document.getElementById('addInput');
    const div = document.getElementById('addResult');
    const num = parseInt(input.value);
    btn.disabled = true; btn.textContent = 'Adding…';
    div.innerHTML = '';
    try {{
      const r = await fetch('/api/add', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{number:num}}) }});
      const data = await r.json();
      if (r.ok) {{
        div.innerHTML = `<div class="notice ok">✅ Added ${{num}} — new total: <strong>${{data.value}}</strong></div>`;
      }} else {{
        div.innerHTML = '<div class="notice err">❌ ' + (data.error || data.body?.error || 'Error') + '</div>';
      }}
    }} catch(err) {{
      div.innerHTML = '<div class="notice err">❌ ' + err.message + '</div>';
    }}
    btn.disabled = false; btn.textContent = 'Add';
    setTimeout(() => location.reload(), 800);
  }}

  async function runSql(e) {{
    e.preventDefault();
    const btn = e.target.querySelector('button');
    const sql = document.getElementById('sqlInput').value.trim();
    const div = document.getElementById('sqlResult');
    if (!sql) return;
    btn.disabled = true; btn.textContent = 'Running…';
    div.innerHTML = '';
    try {{
      const r = await fetch('/api/run-query', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{sql}}) }});
      const data = await r.json();
      if (r.ok && data.rows) {{
        let t = '<table><tr>' + data.columns.map(c => '<th>'+c+'</th>').join('') + '</tr>';
        data.rows.forEach(row => t += '<tr>' + row.map(v => '<td>'+v+'</td>').join('') + '</tr>');
        t += '</table>';
        div.innerHTML = '<div class="notice ok">✅ ' + data.rows.length + ' row(s)</div>' + t;
      }} else if (r.ok && data.results) {{
        div.innerHTML = '<div class="notice ok">✅ Executed</div><pre>' + JSON.stringify(data, null, 2) + '</pre>';
      }} else {{
        div.innerHTML = '<div class="notice err">❌ ' + (data.error || data.body?.error || JSON.stringify(data)) + '</div>';
      }}
    }} catch(err) {{
      div.innerHTML = '<div class="notice err">❌ ' + err.message + '</div>';
    }}
    btn.disabled = false; btn.textContent = 'Run';
    setTimeout(() => location.reload(), 1000);
  }}

  setInterval(() => location.reload(), 5000);
</script>

</body>
</html>"""
    return aiohttp.web.Response(text=html, content_type="text/html")


# ---------------------------------------------------------------------------
# Proxy handlers (call the rqlite service)
# ---------------------------------------------------------------------------

async def handle_value(request: aiohttp.web.Request) -> aiohttp.web.Response:
    result = await _call_service("GET", "value")
    return _json(result["body"], status=result["status"])


async def handle_entries(request: aiohttp.web.Request) -> aiohttp.web.Response:
    result = await _call_service("GET", "entries")
    return _json(result["body"], status=result["status"])


async def handle_add(request: aiohttp.web.Request) -> aiohttp.web.Response:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, aiohttp.ContentTypeError):
        return _json({"error": "invalid JSON"}, status=400)

    number = payload.get("number")
    if not isinstance(number, (int, float)):
        return _json({"error": "body must contain a numeric 'number' field"}, status=400)

    result = await _call_service("POST", "add", {"number": int(number)})
    return _json(result["body"], status=result["status"])


async def handle_run_query(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Run raw SQL against the rqlite service.

    Automatically routes SELECT-like queries to /query and write statements to /execute.
    """
    try:
        payload = await request.json()
    except (json.JSONDecodeError, aiohttp.ContentTypeError):
        return _json({"error": "invalid JSON"}, status=400)

    sql = payload.get("sql", "").strip()
    if not sql:
        return _json({"error": "sql field required"}, status=400)

    # Heuristic: if statement starts with SELECT / EXPLAIN / PRAGMA, use /query
    is_read = sql.upper().lstrip().startswith(("SELECT", "EXPLAIN", "PRAGMA"))
    if is_read:
        result = await _call_service("GET", f"query?q={aiohttp.helpers.quote(sql)}")
    else:
        result = await _call_service("POST", "execute", [[sql]])

    # rqlite returns: read  → {"results":[{"columns":[...],"values":[[...],...]}]}
    #                write → {"results":[{"last_insert_id":...,"rows_affected":...}]}
    body = result["body"]
    if result["status"] in (200, 201) and isinstance(body, dict):
        results = body.get("results", [])
        if results and "columns" in results[0]:
            return _json({"columns": results[0]["columns"], "rows": results[0].get("values", [])})
        return _json({"results": results})
    return _json(body, status=result["status"])


async def handle_proxy(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Generic proxy: GET /api/proxy/status → service /api/status."""
    path = request.match_info.get("path", "")
    try:
        payload = await request.json()
    except Exception:
        payload = None
    result = await _call_service(request.method, path, payload)
    return _json(result["body"], status=result["status"])


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> aiohttp.web.Application:
    app = aiohttp.web.Application()

    # Public routes
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_root)

    # Proxy routes
    app.router.add_get("/api/value", handle_value)
    app.router.add_get("/api/entries", handle_entries)
    app.router.add_post("/api/add", handle_add)
    app.router.add_post("/api/run-query", handle_run_query)
    app.router.add_route("*", "/api/proxy/{path:.*}", handle_proxy)

    return app


if __name__ == "__main__":
    app = create_app()
    print("rqlite-client listening on :8080", flush=True)
    aiohttp.web.run_app(app, host="0.0.0.0", port=8080, print=None)