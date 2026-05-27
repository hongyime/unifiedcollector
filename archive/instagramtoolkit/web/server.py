"""
Simple HTTP server for Instagram Dashboard.
Serves static files and JSON data from the SQLite database.
"""
import os
import sys
import json
import logging as _logging
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# Add parent directory to path so we can import src package
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    from src.db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class DashboardHandler(SimpleHTTPRequestHandler):
    """Enhanced request handler with JSON API endpoints backed by SQLite."""

    def __init__(self, *args, **kwargs):
        self.web_dir = _ROOT / 'web'
        super().__init__(*args, directory=str(self.web_dir), **kwargs)

    def _set_json_headers(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

    def _send_json(self, data):
        self._set_json_headers()
        self.wfile.write(json.dumps(data, indent=2).encode('utf-8'))

    def _api_users(self):
        """Serve user summary from DB (replaces users_summary.json)."""
        try:
            db = _get_db()
            rows = db.fetchall("SELECT * FROM profiles ORDER BY followers_count DESC")
            result = {r['username']: dict(r) for r in rows}
            self._send_json(result)
        except Exception as e:
            _logging.error("Dashboard API error: %s", e)
            self._send_json({'error': 'internal server error'})

    def _api_relationships(self):
        """Serve relationships from DB (replaces relationships.json)."""
        try:
            from urllib.parse import urlparse, parse_qs
            
            # Parse pagination parameters
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            limit = int(qs.get('limit', ['500'])[0])
            offset = int(qs.get('offset', ['0'])[0])
            
            # Enforce reasonable limits
            limit = min(limit, 10000)  # Max 10k rows per request
            offset = max(offset, 0)    # No negative offsets
            
            db = _get_db()
            rows = db.fetchall(
                "SELECT source, target, type, collected_by, collected_ts FROM relationships LIMIT ? OFFSET ?",
                (limit, offset)
            )
            self._send_json([dict(r) for r in rows])
        except Exception as e:
            _logging.error("Dashboard API error: %s", e)
            self._send_json({'error': 'internal server error'})

    def _api_stats(self):
        """Serve summary statistics from DB."""
        try:
            db = _get_db()
            profiles = db.fetchone("SELECT COUNT(*) as cnt FROM profiles")
            rels = db.fetchone("SELECT COUNT(*) as cnt FROM relationships")
            usernames = db.fetchone("SELECT COUNT(*) as cnt FROM usernames")
            self._send_json({
                'profiles': profiles['cnt'] if profiles else 0,
                'relationships': rels['cnt'] if rels else 0,
                'usernames': usernames['cnt'] if usernames else 0,
            })
        except Exception as e:
            _logging.error("Dashboard API error: %s", e)
            self._send_json({'error': 'internal server error'})

    def _api_graph(self):
        """Serve graph data for Cytoscape.js visualization."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        limit = int(qs.get('limit', ['500'])[0])
        offset = int(qs.get('offset', ['0'])[0])
        limit = min(limit, 5000)  # Max 5k nodes/edges per request
        offset = max(offset, 0)
        
        try:
            db = _get_db()
            
            # Get nodes (profiles with basic info)
            node_rows = db.fetchall(
                """SELECT username, followers_count, following_count, is_public
                FROM profiles
                ORDER BY followers_count DESC
                LIMIT ? OFFSET ?""",
                (limit // 2, offset // 2)
            )
            
            nodes = [
                {
                    'id': r['username'],
                    'label': r['username'],
                    'followers': r['followers_count'],
                    'following': r['following_count'],
                    'is_public': bool(r['is_public'])
                }
                for r in node_rows
            ]
            
            # Get edges (relationships)
            if nodes:
                node_usernames = [n['id'] for n in nodes]
                placeholders = ','.join(['?' for _ in node_usernames])
                edge_rows = db.fetchall(
                    f"""SELECT source, target, type, collected_by
                       FROM relationships
                       WHERE source IN ({placeholders}) OR target IN ({placeholders})
                       ORDER BY collected_ts DESC
                       LIMIT ?""",
                    node_usernames * 2 + [limit]
                )
                
                edges = [
                    {
                        'source': r['source'],
                        'target': r['target'],
                        'type': r['type'],
                        'collected_by': r['collected_by']
                    }
                    for r in edge_rows
                ]
            else:
                edges = []
            
            self._send_json({
                'nodes': nodes,
                'edges': edges,
                'total_nodes': len(nodes),
                'total_edges': len(edges)
            })
        except Exception as e:
            _logging.error("Dashboard API error: %s", e)
            self._send_json({'error': 'internal server error', 'details': str(e)})

    def _api_profile_history(self, username):
        """Serve profile snapshot history for a specific user."""
        try:
            db = _get_db()
            rows = db.fetchall(
                """SELECT username, user_id, followers_count, following_count, media_count,
                       is_public, scraped_by, snapshot_ts
                FROM profile_snapshots
                WHERE username = ?
                ORDER BY snapshot_ts DESC
                LIMIT 100""",
                (username,)
            )
            
            history = [
                {
                    'username': r['username'],
                    'user_id': r['user_id'],
                    'followers_count': r['followers_count'],
                    'following_count': r['following_count'],
                    'media_count': r['media_count'],
                    'is_public': bool(r['is_public']),
                    'scraped_by': r['scraped_by'],
                    'snapshot_ts': r['snapshot_ts']
                }
                for r in rows
            ]
            
            self._send_json(history)
        except Exception as e:
            _logging.error("Dashboard API error: %s", e)
            self._send_json({'error': 'internal server error', 'details': str(e)})

    def do_GET(self):
        if self.path.startswith('/api/'):
            endpoint = self.path[4:]  # keep leading /
            if endpoint == '/users':
                self._api_users()
            elif endpoint == '/relationships':
                self._api_relationships()
            elif endpoint == '/stats':
                self._api_stats()
            elif endpoint == '/graph':
                self._api_graph()
            elif endpoint.startswith('/profile/') and endpoint.endswith('/history'):
                # Extract username from path: /profile/username/history
                parts = endpoint.split('/')
                if len(parts) == 4:
                    username = parts[2]
                    self._api_profile_history(username)
                else:
                    self.send_response(400)
                    self._send_json({'error': 'Invalid profile history endpoint'})
            elif endpoint == '/health':
                try:
                    _get_db().fetchone("SELECT 1")
                    self._send_json({'status': 'ok'})
                except Exception:
                    self.send_response(503)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"status":"unavailable"}')
            else:
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Unknown endpoint'}).encode())
            return
        super().do_GET()

    def log_message(self, format, *args):
        print(f"[DASHBOARD] {format % args}")


def run_dashboard(host='127.0.0.1', port=8080):
    """
    Run dashboard server.
    
    WARNING: Restricted to localhost only (127.0.0.1).
    Suitable for local development only - not production.
    
    For production deployment:
    - Add authentication (login system, API keys, or OAuth)
    - Restrict CORS to specific origins
    - Use HTTPS with valid SSL certificates
    - Implement rate limiting to prevent abuse
    - Add input validation and sanitization
    - Use a production-grade web server (nginx, Apache, gunicorn)
    
    Security Implications:
    - Restricted to localhost: Only local machine can access the dashboard
    - CORS allows any origin: For convenient local development
    - No encryption: Data transmitted in plain text
    - No rate limiting: Vulnerable to DoS attacks
    """
    import signal
    
    server_address = (host, port)
    httpd = HTTPServer(server_address, DashboardHandler)
    
    # Add SIGTERM handler for graceful shutdown
    def _sigterm(signum, frame):
        print("\n[DASHBOARD] Received SIGTERM, shutting down gracefully...")
        httpd.shutdown()
    
    signal.signal(signal.SIGTERM, _sigterm)
    
    print(f"[DASHBOARD] Starting server at http://{host}:{port}")
    print(f"[DASHBOARD] Press Ctrl+C to stop")
    print()
    print("Available URLs:")
    print(f"  - Dashboard: http://{host}:{port}/dashboard.html")
    print(f"  - Users API: http://{host}:{port}/api/users")
    print(f"  - Relationships API: http://{host}:{port}/api/relationships")
    print()
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[DASHBOARD] Shutting down...")
        httpd.shutdown()
        print("[DASHBOARD] Server stopped")


if __name__ == '__main__':
    import sys
    
    host = '127.0.0.1'  # Restricted to localhost for security
    port = 8080
    
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    
    run_dashboard(host, port)
