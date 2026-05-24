"""Dashboard Index Page — port 8500.

Serves a self-contained HTML page listing every service dashboard with its
port, URL, and live reachability status. Auto-refreshes every 30 seconds.

Also injects a floating navigation bar so operators can jump between
dashboards without manually changing port numbers.
"""

import concurrent.futures
import socket
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

SERVICES = [
    {"name": "Collector",         "port": 8501, "host": "telegramcollector_dashboard_collector",      "icon": "📡"},
    {"name": "Face Recognition",  "port": 8502, "host": "telegramcollector_dashboard_face",           "icon": "👤"},
    {"name": "User Intelligence", "port": 8503, "host": "telegramcollector_dashboard_user_intel",     "icon": "🧠"},
    {"name": "Link Discovery",    "port": 8504, "host": "telegramcollector_dashboard_link_discovery", "icon": "🔗"},
    {"name": "Bulk Sender",       "port": 8505, "host": "telegramcollector_dashboard_bulk_sender",    "icon": "📤"},
]

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="30">
  <title>telegramcollector &mdash; Service Dashboards</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f1117;
      color: #e0e0e0;
      margin: 0;
      padding: 0;
      display: flex;
      min-height: 100vh;
    }}

    /* ── Sidebar nav ─────────────────────────────────────────────── */
    nav {{
      width: 220px;
      min-width: 220px;
      background: #1a1d27;
      border-right: 1px solid #2a2d3a;
      padding: 1.5rem 0;
      position: sticky;
      top: 0;
      height: 100vh;
      overflow-y: auto;
    }}
    nav .nav-title {{
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #6b7280;
      padding: 0 1.2rem 0.8rem;
      border-bottom: 1px solid #2a2d3a;
      margin-bottom: 0.5rem;
    }}
    nav a {{
      display: flex;
      align-items: center;
      gap: 0.6rem;
      padding: 0.55rem 1.2rem;
      color: #d1d5db;
      text-decoration: none;
      font-size: 0.9rem;
      border-left: 3px solid transparent;
      transition: background 0.15s, border-color 0.15s;
    }}
    nav a:hover {{
      background: #252836;
      border-left-color: #60a5fa;
      color: #ffffff;
    }}
    nav a.active {{
      background: #1e2a3a;
      border-left-color: #3b82f6;
      color: #60a5fa;
    }}
    nav a .status-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      flex-shrink: 0;
      margin-left: auto;
    }}
    nav a .status-dot.up   {{ background: #22c55e; }}
    nav a .status-dot.down {{ background: #ef4444; }}
    nav .nav-index {{
      padding: 0.55rem 1.2rem 1rem;
      font-size: 0.75rem;
      color: #6b7280;
      border-bottom: 1px solid #2a2d3a;
      margin-bottom: 0.5rem;
    }}

    /* ── Main content ────────────────────────────────────────────── */
    main {{
      flex: 1;
      padding: 2rem 2.5rem;
    }}
    h1 {{
      font-size: 1.3rem;
      margin: 0 0 0.4rem;
      color: #ffffff;
    }}
    .subtitle {{
      font-size: 0.85rem;
      color: #6b7280;
      margin-bottom: 2rem;
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      max-width: 760px;
    }}
    th, td {{
      text-align: left;
      padding: 0.65rem 1rem;
      border-bottom: 1px solid #2a2d3a;
    }}
    th {{
      background: #1a1d27;
      color: #9ca3af;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    tr:hover td {{ background: #1a1d27; }}
    a.link {{
      color: #60a5fa;
      text-decoration: none;
    }}
    a.link:hover {{ text-decoration: underline; }}
    .badge {{ font-size: 0.9rem; }}
    .badge.up   {{ color: #22c55e; }}
    .badge.down {{ color: #ef4444; }}
    .open-btn {{
      display: inline-block;
      padding: 0.3rem 0.8rem;
      background: #1e3a5f;
      color: #60a5fa;
      border-radius: 4px;
      font-size: 0.8rem;
      text-decoration: none;
      border: 1px solid #2563eb44;
    }}
    .open-btn:hover {{ background: #1e4a7f; }}
    .refresh-note {{
      margin-top: 1.5rem;
      font-size: 0.75rem;
      color: #4b5563;
    }}
  </style>
</head>
<body>

  <!-- Sidebar navigation -->
  <nav>
    <div class="nav-title">telegramcollector</div>
    <div class="nav-index">📊 Index (this page)</div>
    {nav_links}
  </nav>

  <!-- Main table -->
  <main>
    <h1>Service Dashboards</h1>
    <p class="subtitle">Click a service to open its dashboard. Status auto-refreshes every 30 s.</p>
    <table>
      <thead>
        <tr>
          <th>Service</th>
          <th>Port</th>
          <th>URL</th>
          <th>Status</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {rows}
      </tbody>
    </table>
    <p class="refresh-note">⟳ Page auto-refreshes every 30 seconds</p>
  </main>

</body>
</html>
"""

ROW_TEMPLATE = """\
      <tr>
        <td>{icon} {name}</td>
        <td>{port}</td>
        <td><a class="link" href="http://localhost:{port}">localhost:{port}</a></td>
        <td class="badge {status_class}">{status_badge}</td>
        <td><a class="open-btn" href="http://localhost:{port}" target="_blank">Open →</a></td>
      </tr>"""

NAV_LINK_TEMPLATE = """\
    <a href="http://localhost:{port}" target="_blank">
      <span>{icon} {name}</span>
      <span class="status-dot {status_class}"></span>
    </a>"""

# ---------------------------------------------------------------------------
# Ping logic
# ---------------------------------------------------------------------------


def ping_service(host: str, port: int, timeout: int = 3) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout.

    Tries the Docker container hostname first, then falls back to localhost.
    """
    for h in (host, "localhost"):
        try:
            conn = socket.create_connection((h, port), timeout=timeout)
            conn.close()
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            continue
    return False


def get_all_statuses() -> dict:
    """Concurrently ping all service ports and return port → bool mapping."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(SERVICES)) as executor:
        futures = {
            executor.submit(ping_service, svc["host"], svc["port"]): svc["port"]
            for svc in SERVICES
        }
        results = {}
        for future in concurrent.futures.as_completed(futures):
            port = futures[future]
            try:
                results[port] = future.result()
            except Exception:
                results[port] = False
    return results


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class IndexHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/favicon.ico"):
            self.send_response(404)
            self.end_headers()
            return

        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        statuses = get_all_statuses()

        rows = "\n".join(
            ROW_TEMPLATE.format(
                icon=svc["icon"],
                name=svc["name"],
                port=svc["port"],
                status_class="up" if statuses.get(svc["port"]) else "down",
                status_badge="🟢 up" if statuses.get(svc["port"]) else "🔴 down",
            )
            for svc in SERVICES
        )

        nav_links = "\n".join(
            NAV_LINK_TEMPLATE.format(
                icon=svc["icon"],
                name=svc["name"],
                port=svc["port"],
                status_class="up" if statuses.get(svc["port"]) else "down",
            )
            for svc in SERVICES
        )

        body = HTML_TEMPLATE.format(rows=rows, nav_links=nav_links).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass


# ---------------------------------------------------------------------------
# Threading server
# ---------------------------------------------------------------------------


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a separate thread."""
    daemon_threads = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8500), IndexHandler)
    print("Dashboard index listening on http://0.0.0.0:8500")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
