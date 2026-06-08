#!/usr/bin/env python3
"""World Cup 2026 Pool — standalone server."""

import http.server
import json
import os
import sys
import urllib.request
import time

PORT = int(os.environ.get("PORT", 8080))

# ── Supabase ──────────────────────────────────────────────────────────────────
# Set these in your hosting provider's environment variables (Render → Environment).
# For local dev:  export SUPABASE_URL=...  export SUPABASE_KEY=...
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_KEY environment variables must be set.",
          file=sys.stderr)
    sys.exit(1)
# ─────────────────────────────────────────────────────────────────────────────

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

ESPN_WC_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
ESPN_WC_STANDINGS  = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/standings"
CACHE_TTL = 60  # seconds

_wc_scores_cache   = {"data": None, "time": 0}
_wc_standings_cache = {"data": None, "time": 0}


def supabase_request(method, path, body=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode() if body else None
    headers = dict(SUPABASE_HEADERS)
    if method == "GET":
        headers["Accept"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        print(f"Supabase error: {e.code} {e.read().decode()}")
        raise
    except Exception as e:
        print(f"Supabase error: {e}")
        raise


def load_wc_picks():
    rows = supabase_request(
        "GET",
        "wc_picks?select=id,name,tier1,tier2,tier3,tier4,tier5,created_at&order=created_at.asc"
    )
    return rows if rows is not None else []


def save_wc_pick(name, tier1, tier2, tier3, tier4, tier5):
    body = {"name": name, "tier1": tier1, "tier2": tier2,
            "tier3": tier3, "tier4": tier4, "tier5": tier5}
    url = f"{SUPABASE_URL}/rest/v1/wc_picks"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"WC pick saved: {name}")
            return True, None
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"WC pick error: {e.code} {err}")
        if e.code == 409 or "23505" in err:
            return False, "duplicate"
        return False, err
    except Exception as e:
        return False, str(e)


def fetch_espn(url, cache):
    now = time.time()
    if cache["data"] and (now - cache["time"]) < CACHE_TTL:
        return cache["data"]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WCPool/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            cache["data"] = raw
            cache["time"] = now
            return raw
    except Exception as e:
        print(f"ESPN fetch error: {e}")
        if cache["data"]:
            return cache["data"]
        return json.dumps({"error": str(e), "events": [], "children": []}).encode()


class Handler(http.server.SimpleHTTPRequestHandler):

    def send_json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/wc/picks":
            try:
                self.send_json(200, load_wc_picks())
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif self.path == "/api/wc/scores":
            data = fetch_espn(ESPN_WC_SCOREBOARD, _wc_scores_cache)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "max-age=30")
            self.end_headers()
            self.wfile.write(data if isinstance(data, bytes) else data.encode())

        elif self.path == "/api/wc/standings":
            data = fetch_espn(ESPN_WC_STANDINGS, _wc_standings_cache)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "max-age=30")
            self.end_headers()
            self.wfile.write(data if isinstance(data, bytes) else data.encode())

        else:
            super().do_GET()

    def do_POST(self):
        try:
            if self.path == "/api/wc/picks":
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError as e:
                    self.send_json(400, {"error": f"Invalid JSON body: {e}"})
                    return

                name  = (body.get("name") or "").strip()
                tier1 = (body.get("tier1") or "").strip()
                tier2 = (body.get("tier2") or "").strip()
                tier3 = (body.get("tier3") or "").strip()
                tier4 = (body.get("tier4") or "").strip()
                tier5 = (body.get("tier5") or "").strip()

                if not all([name, tier1, tier2, tier3, tier4, tier5]):
                    self.send_json(400, {"error": "Name and all 5 tier picks are required."})
                    return

                ok, err = save_wc_pick(name, tier1, tier2, tier3, tier4, tier5)
                if ok:
                    self.send_json(200, {"ok": True})
                elif err == "duplicate":
                    self.send_json(409, {"error": "Picks already submitted for that name."})
                else:
                    self.send_json(500, {"error": err or "Failed to save."})
            else:
                self.send_json(404, {"error": "Not found"})
        except Exception as e:
            print(f"do_POST crash: {e}")
            try:
                self.send_json(500, {"error": f"Server error: {e}"})
            except Exception:
                pass

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f"WC Pool running at http://0.0.0.0:{PORT}")
    http.server.HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
