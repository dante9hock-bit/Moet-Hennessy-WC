#!/usr/bin/env python3
"""World Cup 2026 Pool — standalone server."""

import http.server
import json
import os
import sys
import urllib.request
import time
from concurrent.futures import ThreadPoolExecutor

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

ESPN_WC_SCOREBOARD_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
# Full 2026 World Cup window. ESPN supports YYYYMMDD-YYYYMMDD ranges on this endpoint.
ESPN_WC_SCOREBOARD = f"{ESPN_WC_SCOREBOARD_BASE}?dates=20260611-20260719"
ESPN_CORE_GROUPS   = "https://sports.core.api.espn.com/v2/sports/soccer/leagues/fifa.world/seasons/2026/types/1/groups?lang=en&region=us"
CACHE_TTL = 60  # seconds

_wc_scores_cache    = {"data": None, "time": 0}
_wc_standings_cache = {"data": None, "time": 0}
_espn_executor = ThreadPoolExecutor(max_workers=16)


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


def _get_json(url, timeout=8):
    """Fetch a URL and parse JSON. Returns None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WCPool/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"core.api fetch error for {url}: {e}")
        return None


def _stat(stats, name, default=0):
    for s in stats or []:
        if s.get("name") == name:
            v = s.get("value")
            try:
                return int(v) if v is not None else default
            except (TypeError, ValueError):
                return default
    return default


def build_wc_standings():
    """
    Walk ESPN's core API to assemble the World Cup 2026 group standings.
    Returns a dict shaped: { "children": [ { "name": "Group A", "standings": [rows] } ] }
    where each row has team name, abbrev, GP/W/D/L/GF/GA/GD/Pts.
    """
    # 1. Group list
    root = _get_json(ESPN_CORE_GROUPS)
    if not root or not root.get("items"):
        return {"error": "groups list unavailable", "children": []}

    group_refs = [item["$ref"] for item in root["items"] if item.get("$ref")]

    # 2. Group docs in parallel
    groups = list(_espn_executor.map(_get_json, group_refs))

    # 3. Each group has standings -> standings/0 ref
    def resolve_standings(group):
        if not group or not isinstance(group, dict):
            return None
        st_ref = (group.get("standings") or {}).get("$ref")
        if not st_ref:
            return None
        st = _get_json(st_ref)
        if not st or not st.get("items"):
            return None
        # Take the first standings entry ("overall")
        entry_ref = st["items"][0].get("$ref")
        if not entry_ref:
            return None
        entry = _get_json(entry_ref)
        return {
            "name": group.get("name") or group.get("abbreviation") or "Group",
            "rows": entry.get("standings", []) if entry else []
        }

    resolved = list(_espn_executor.map(resolve_standings, groups))

    # 4. Resolve every team ref in parallel (across all groups)
    team_refs = set()
    for r in resolved:
        if not r:
            continue
        for row in r["rows"]:
            tref = (row.get("team") or {}).get("$ref")
            if tref:
                team_refs.add(tref)

    team_docs = {}
    fetched = list(_espn_executor.map(_get_json, list(team_refs)))
    for ref, doc in zip(team_refs, fetched):
        if doc:
            team_docs[ref] = doc

    # 5. Build clean output
    children = []
    for r in resolved:
        if not r:
            continue
        rows_out = []
        for row in r["rows"]:
            tref = (row.get("team") or {}).get("$ref", "")
            tdoc = team_docs.get(tref, {})
            stats = []
            for rec in row.get("records", []):
                if rec.get("type") == "total":
                    stats = rec.get("stats", [])
                    break
            rows_out.append({
                "team":    tdoc.get("displayName") or tdoc.get("name") or "?",
                "abbrev":  tdoc.get("abbreviation") or "",
                "logo":    (tdoc.get("logos") or [{}])[0].get("href", ""),
                "gp":      _stat(stats, "gamesPlayed"),
                "w":       _stat(stats, "wins"),
                "d":       _stat(stats, "ties"),
                "l":       _stat(stats, "losses"),
                "gf":      _stat(stats, "pointsFor"),
                "ga":      _stat(stats, "pointsAgainst"),
                "gd":      _stat(stats, "pointDifferential"),
                "pts":     _stat(stats, "points"),
            })
        # Sort by pts desc, then gd desc, then gf desc
        rows_out.sort(key=lambda x: (-x["pts"], -x["gd"], -x["gf"]))
        children.append({"name": r["name"], "standings": {"entries": rows_out}})

    return {"children": children}


def get_wc_standings():
    """Cached wrapper around build_wc_standings()."""
    now = time.time()
    cache = _wc_standings_cache
    if cache["data"] and (now - cache["time"]) < CACHE_TTL:
        return cache["data"]
    try:
        data = json.dumps(build_wc_standings()).encode()
        cache["data"] = data
        cache["time"] = now
        return data
    except Exception as e:
        print(f"build_wc_standings crashed: {e}")
        if cache["data"]:
            return cache["data"]
        return json.dumps({"error": str(e), "children": []}).encode()


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
            data = get_wc_standings()
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
