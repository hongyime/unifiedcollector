#!/usr/bin/env python3
"""
API Server for Telegram Toolkit
Serves data directly from SQLite database via REST API.
"""
import http.server
import socketserver
import json
import sqlite3
import sys
import os
from pathlib import Path
from typing import Any, Dict, List
from src.core.sqlite_utils import connect_sqlite, describe_database_lock, is_database_lock_error

PORT = 8001  # Different port from static file server
DB_PATH = Path("data/users_analysis.db")

class APIHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for API endpoints"""

    def _open_connection(self):
        return connect_sqlite(DB_PATH)

    def _send_database_error(self, context: str, error: Exception):
        if is_database_lock_error(error):
            self.send_error(503, describe_database_lock(context, DB_PATH))
        else:
            self.send_error(500, str(error))
    
    def serve_health(self):
        """Health check endpoint for monitoring"""
        from datetime import datetime
        
        conn = None
        try:
            # Attempt to connect to database
            conn = self._open_connection()
            
            # Check connectivity with simple query
            conn.execute('SELECT 1')
            
            # Get schema version
            cursor = conn.execute('SELECT MAX(version) FROM schema_version')
            schema_version = cursor.fetchone()
            schema_ver = schema_version[0] if schema_version else None
            
            # Check database integrity
            cursor = conn.execute('PRAGMA integrity_check')
            integrity_result = cursor.fetchone()[0]
            
            conn.close()
            
            # Build healthy response
            health_data = {
                'status': 'healthy',
                'database': {
                    'connected': True,
                    'schema_version': schema_ver,
                    'integrity': integrity_result
                },
                'timestamp': datetime.utcnow().isoformat() + 'Z'
            }
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(health_data, indent=2).encode('utf-8'))
            
        except Exception as e:
            # Build unhealthy response
            health_data = {
                'status': 'unhealthy',
                'database': {
                    'connected': False,
                    'error': str(e)
                },
                'timestamp': datetime.utcnow().isoformat() + 'Z'
            }
            
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(health_data, indent=2).encode('utf-8'))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except:
                    pass
    
    def do_GET(self):
        """Handle GET requests"""
        if self.path.startswith('/api/'):
            self.handle_api_request()
        else:
            self.send_error(404, "Not Found")
    
    def handle_api_request(self):
        """Route API requests to appropriate handlers"""
        path = self.path
        
        if path == '/api/health':
            self.serve_health()
        elif path == '/api/users':
            self.serve_users()
        elif path == '/api/memberships':
            self.serve_memberships()
        elif path == '/api/stats':
            self.serve_stats()
        elif path.startswith('/api/users/'):
            user_id = path.split('/')[-1]
            self.serve_user(user_id)
        elif path.startswith('/api/memberships/'):
            user_id = path.split('/')[-1]
            self.serve_user_memberships(user_id)
        elif path.startswith('/api/graph'):
            self.serve_graph()
        else:
            self.send_error(404, "Endpoint not found")
    
    def serve_users(self):
        """Serve all users from database"""
        conn = None
        try:
            conn = self._open_connection()
            cursor = conn.execute("SELECT * FROM users ORDER BY user_id LIMIT 1000")
            users = [dict(row) for row in cursor]
            
            self.send_json_response(users)
        except Exception as e:
            self._send_database_error("serving users", e)
        finally:
            if conn is not None:
                conn.close()
    
    def serve_user(self, user_id: str):
        """Serve a single user by ID"""
        try:
            user_id_int = int(user_id)
        except (ValueError, OverflowError):
            self.send_error(400, "Invalid user ID: must be numeric")
            return
        conn = None
        try:
            conn = self._open_connection()
            cursor = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id_int,))
            row = cursor.fetchone()
            
            if row:
                self.send_json_response(dict(row))
            else:
                self.send_error(404, "User not found")
        except Exception as e:
            self._send_database_error(f"serving user {user_id}", e)
        finally:
            if conn is not None:
                conn.close()
    
    def serve_memberships(self):
        """Serve all memberships from database"""
        conn = None
        try:
            conn = self._open_connection()
            cursor = conn.execute("SELECT * FROM memberships ORDER BY user_id, group_id LIMIT 1000")
            memberships = [dict(row) for row in cursor]
            
            self.send_json_response(memberships)
        except Exception as e:
            self._send_database_error("serving memberships", e)
        finally:
            if conn is not None:
                conn.close()
    
    def serve_user_memberships(self, user_id: str):
        """Serve memberships for a specific user"""
        try:
            user_id_int = int(user_id)
        except (ValueError, OverflowError):
            self.send_error(400, "Invalid user ID: must be numeric")
            return
        conn = None
        try:
            conn = self._open_connection()
            cursor = conn.execute(
                "SELECT * FROM memberships WHERE user_id = ? ORDER BY group_id",
                (user_id_int,)
            )
            memberships = [dict(row) for row in cursor]
            
            self.send_json_response(memberships)
        except Exception as e:
            self._send_database_error(f"serving memberships for user {user_id}", e)
        finally:
            if conn is not None:
                conn.close()
    
    def serve_stats(self):
        """Serve aggregated statistics"""
        conn = None
        try:
            conn = self._open_connection()
            
            # Total users
            cursor = conn.execute("SELECT COUNT(*) as total FROM users")
            total_users = cursor.fetchone()['total']
            
            # Total memberships
            cursor = conn.execute("SELECT COUNT(*) as total FROM memberships")
            total_memberships = cursor.fetchone()['total']
            
            # Unique groups
            cursor = conn.execute("SELECT COUNT(DISTINCT group_id) as total FROM memberships")
            total_groups = cursor.fetchone()['total']
            
            # Premium users
            cursor = conn.execute("SELECT COUNT(*) as total FROM users WHERE is_premium = 1")
            premium_users = cursor.fetchone()['total']
            
            # Bot users
            cursor = conn.execute("SELECT COUNT(*) as total FROM users WHERE is_bot = 1")
            bot_users = cursor.fetchone()['total']
            
            stats = {
                'total_users': total_users,
                'total_memberships': total_memberships,
                'total_groups': total_groups,
                'premium_users': premium_users,
                'bot_users': bot_users,
                'human_users': total_users - bot_users
            }
            
            self.send_json_response(stats)
        except Exception as e:
            self._send_database_error("serving stats", e)
        finally:
            if conn is not None:
                conn.close()
    
    def serve_graph(self):
        """Serve paginated graph data for Cytoscape.js visualization."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        limit = int(params.get('limit', ['500'])[0])
        offset = int(params.get('offset', ['0'])[0])

        conn = None
        try:
            conn = self._open_connection()

            user_rows = conn.execute(
                "SELECT u.user_id AS id,"
                " COALESCE(u.username, u.first_name, CAST(u.user_id AS TEXT)) AS label,"
                " 'user' AS type, COUNT(m.group_id) AS member_count"
                " FROM users u LEFT JOIN memberships m ON u.user_id=m.user_id"
                " GROUP BY u.user_id LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()

            group_rows = conn.execute(
                "SELECT m.group_id AS id,"
                " COALESCE(m.group_name, m.group_id) AS label,"
                " 'group' AS type, COUNT(m.user_id) AS member_count"
                " FROM memberships m GROUP BY m.group_id"
            ).fetchall()

            nodes = [dict(r) for r in user_rows] + [dict(r) for r in group_rows]

            edge_rows = conn.execute(
                "SELECT CAST(user_id AS TEXT) AS source,"
                " group_id AS target,"
                " 'member_of' AS type"
                " FROM memberships LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()

            edges = [dict(r) for r in edge_rows]

            self.send_json_response({'nodes': nodes, 'edges': edges})
        except Exception as e:
            self._send_database_error("serving graph", e)
        finally:
            if conn is not None:
                conn.close()

    def send_json_response(self, data: Any):
        """Send JSON response"""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', 'http://localhost:8000')
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode('utf-8'))

    def send_error(self, code: int, message: str):
        """Send error response"""
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', 'http://localhost:8000')
        self.end_headers()
        error = {'error': message}
        self.wfile.write(json.dumps(error).encode('utf-8'))
    
    def log_message(self, format, *args):
        """Override to customize logging"""
        print(f"[API] {args[0]}")


def main():
    """Start the API server"""
    os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    
    print("========================================")
    print("  TELEGRAM TOOLKIT API SERVER")
    print("========================================\n")
    print(f"🚀 Starting API server on port {PORT}...")
    print(f"📊 API endpoints:")
    print(f"   GET /api/health         - Health check endpoint")
    print(f"   GET /api/users          - List all users (limit 1000)")
    print(f"   GET /api/users/<id>     - Get user by ID")
    print(f"   GET /api/memberships    - List all memberships (limit 1000)")
    print(f"   GET /api/memberships/<user_id> - Get user memberships")
    print(f"   GET /api/stats          - Get aggregated statistics")
    print(f"\n🌐 Server will be available at: http://localhost:{PORT}")
    print(f"\nPress Ctrl+C to stop the server\n")
    
    # Check if database exists
    if not DB_PATH.exists():
        print(f"⚠️  Warning: Database not found at {DB_PATH}")
        print(f"   Please run a scan first to generate data.\n")
    
    handler = APIHandler
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")


if __name__ == "__main__":
    main()
