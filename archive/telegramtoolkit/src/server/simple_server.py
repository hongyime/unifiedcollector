#!/usr/bin/env python3
"""
Simple HTTP Server for Web Dashboards
Serves static files with CORS support and security blocks
"""
import http.server
import socketserver
import sys
import os
import webbrowser
from pathlib import Path
from typing import Any

PORT = 8000
BLOCKED_PATTERNS = ('.env', 'sessions/', '.git/', 'config.py', '.session', '.db', '__pycache__', '.pyc', '.backup')


class CORSRequestHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP request handler with CORS support and security blocks"""
    
    def __init__(self, *args: Any, **kwargs: Any):
        # Set directory to project root
        base_dir = Path(__file__).parent.parent.parent
        super().__init__(*args, directory=str(base_dir), **kwargs)
    
    def do_GET(self):
        """Handle GET requests with security checks"""
        # Block access to sensitive files
        path_lower = self.path.lower()
        for pattern in BLOCKED_PATTERNS:
            if pattern in path_lower:
                self.send_error(403, "Forbidden")
                return
        super().do_GET()
    
    def end_headers(self):
        """Add CORS headers"""
        self.send_header('Access-Control-Allow-Origin', f'http://localhost:{PORT}')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()
    
    def log_message(self, format, *args):
        """Custom logging format"""
        print(f"[HTTP] {args[0]}")


def main():
    """Start the HTTP server"""
    # Change to project root
    os.chdir(Path(__file__).parent.parent.parent)
    
    # Parse command line arguments
    open_browser = True
    page = "enhanced_dashboard.html"
    
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg == "no-browser":
            open_browser = False
        elif arg == "dashboard":
            page = "enhanced_dashboard.html"
        elif arg == "visualize":
            page = "visualize.html"
    
    print("========================================")
    print("  SIMPLE HTTP SERVER")
    print("========================================\n")
    print(f"🚀 Starting server on port {PORT}...")
    print(f"📁 Serving from: {os.getcwd()}")
    print(f"\n🌐 Available URLs:")
    print(f"   Dashboard:  http://localhost:{PORT}/web/enhanced_dashboard.html")
    print(f"   Visualizer: http://localhost:{PORT}/web/visualize.html")
    print(f"\n🔒 Security: Blocking access to sensitive files")
    print(f"✅ CORS: Enabled for localhost:{PORT}")
    print(f"\nPress Ctrl+C to stop the server\n")
    
    # Open browser if requested
    if open_browser:
        url = f"http://localhost:{PORT}/web/{page}"
        print(f"🌐 Opening {page} in browser...")
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"⚠️ Could not open browser: {e}")
            print(f"📌 Manually open: {url}")
    
    # Start server
    handler = CORSRequestHandler
    try:
        with socketserver.TCPServer(("127.0.0.1", PORT), handler) as httpd:
            print(f"✅ Server running on http://localhost:{PORT}\n")
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n\n⚠️ Server stopped by user")
    except OSError as e:
        if "address already in use" in str(e).lower():
            print(f"\n❌ Port {PORT} is already in use!")
            print(f"💡 Another server may already be running")
            print(f"📌 Try opening: http://localhost:{PORT}/web/{page}")
        else:
            print(f"\n❌ Error starting server: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
