"""
Musi Player - local HTTP server with playlist persistence API.
Run with: python server.py
"""
import http.server
import webbrowser
import threading
import os
import socket
import sys
import json
import urllib.request
import urllib.error
from urllib.parse import urlparse, parse_qs

PORT = 8080
DIR  = os.path.dirname(os.path.abspath(__file__))
PLAYLISTS_FILE = os.path.join(DIR, 'playlists.json')
STATE_FILE     = os.path.join(DIR, 'state.json')

# Requests run on separate threads; this keeps them from reading a JSON file
# while another request is halfway through writing it.
DATA_LOCK = threading.RLock()

def load_state():
    with DATA_LOCK:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

def save_state(data):
    with DATA_LOCK, open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def fetch_musi_playlist(playlist_id):
    """Fetch a fresh tracklist for a playlist id from the feelthemusi API."""
    url = f"https://feelthemusi.com/api/v4/playlists/fetch/{playlist_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    success = raw.get("success")
    if not success:
        return None

    inner = success.get("data")
    if isinstance(inner, str):
        inner = json.loads(inner)

    tracks = inner.get("data") if inner else None
    if not tracks:
        return None

    return {
        "title": success.get("title") or inner.get("title") or "",
        "songs": [
            {
                "title": t.get("video_name"),
                "artist": t.get("video_creator"),
                "id": t.get("video_id"),
            }
            for t in tracks
        ],
    }

def refresh_one_playlist(playlist_id):
    """Re-fetch a single saved playlist by id. Returns the updated playlist dict,
    or None if it isn't found or the fetch didn't return any tracks."""
    playlists = load_playlists()
    idx = next((i for i, p in enumerate(playlists) if p.get("id") == playlist_id), None)
    if idx is None:
        return None

    pl = playlists[idx]
    name = pl.get("name", playlist_id)
    try:
        result = fetch_musi_playlist(playlist_id)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  x {name}: fetch failed ({e})")
        return None
    except Exception as e:
        print(f"  x {name}: unexpected error ({e})")
        return None

    if not result:
        print(f"  x {name}: no tracks returned")
        return None

    pl["songs"] = result["songs"]
    save_playlists(playlists)
    print(f"  ok {name}: {len(pl['songs'])} tracks (manual refresh)")
    return pl

def refresh_all_playlists():
    """Re-fetch every saved playlist from feelthemusi so links/tracks are current."""
    playlists = load_playlists()
    if not playlists:
        return

    print(f"Refreshing {len(playlists)} saved playlist(s)...")
    changed = False
    for pl in playlists:
        name = pl.get("name", pl.get("id", "?"))
        try:
            result = fetch_musi_playlist(pl["id"])
            if not result:
                print(f"  x {name}: no tracks returned, keeping cached copy")
                continue
            pl["songs"] = result["songs"]
            changed = True
            print(f"  ok {name}: {len(pl['songs'])} tracks")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            print(f"  x {name}: fetch failed ({e}), keeping cached copy")
        except Exception as e:
            print(f"  x {name}: unexpected error ({e}), keeping cached copy")

    if changed:
        save_playlists(playlists)
        print("Playlists updated.")
    else:
        print("No playlists needed updating.")

def load_playlists():
    with DATA_LOCK:
        if os.path.exists(PLAYLISTS_FILE):
            with open(PLAYLISTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return []

def save_playlists(data):
    with DATA_LOCK, open(PLAYLISTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == '/api/playlists':
            data = load_playlists()
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith('/api/refresh'):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            pid = qs.get('id', [None])[0]
            try:
                # Hold the lock across load-fetch-save so a concurrent save
                # isn't overwritten by the refresh.
                with DATA_LOCK:
                    if pid:
                        updated = refresh_one_playlist(pid)
                    else:
                        refresh_all_playlists()
                        data = load_playlists()
                if pid:
                    if updated is None:
                        body = b'{"error":"refresh failed"}'
                        self.send_response(404)
                        self.send_header('Content-Type', 'application/json; charset=utf-8')
                        self.send_header('Content-Length', str(len(body)))
                        self._cors()
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    body = json.dumps(updated, ensure_ascii=False).encode('utf-8')
                else:
                    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self._cors()
                self.end_headers()
                self.wfile.write(str(e).encode())
        elif self.path == '/api/state':
            data = load_state()
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def do_POST(self):
        if self.path in ('/api/playlists', '/api/state'):
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body.decode('utf-8'))
                if self.path == '/api/playlists':
                    save_playlists(data)
                else:
                    save_state(data)
                resp = b'{"ok":true}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(resp)))
                self._cors()
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                self.send_response(500)
                self._cors()
                self.end_headers()
                self.wfile.write(str(e).encode())
        else:
            self.send_response(405)
            self.end_headers()

    def log_message(self, format, *args):
        pass

def open_browser():
    webbrowser.open(f"http://localhost:{PORT}/index.html")

class Server(http.server.ThreadingHTTPServer):
    # Must be set before bind, i.e. on the class. Skipped on Windows, where
    # SO_REUSEADDR would let a second server silently share the port.
    allow_reuse_address = os.name != 'nt'
    address_family = socket.AF_INET6

    def server_bind(self):
        # Accept IPv4 too. "localhost" resolves to ::1 first, and an
        # IPv4-only server makes every connection wait for the fallback.
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def handle_error(self, request, client_address):
        # The browser closing a connection mid-response (reload, skipped
        # track, cancelled preload) is normal; don't print a traceback for it.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)

with Server(("::", PORT), Handler) as httpd:
    refresh_all_playlists()
    print(f"Musi Player  ->  http://localhost:{PORT}/index.html")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.8, open_browser).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
