#!/usr/bin/env python3
"""Local trend API over the sqlite db (stdlib only).

  python serve.py            # dashboard at http://localhost:8787

  GET /api/trends/<roblox|steam>?window=24&list=rising|breakout|big_movers
  GET /api/genres/<roblox|steam>?window=168
  GET /api/game/<roblox|steam>/<id>          full snapshot history + metadata
  GET /api/digest?window=24                  markdown digest
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tracker import db, report, scoring

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("TRACKER_DB", os.path.join(ROOT, "data", "trends.db"))
PUBLIC = os.path.realpath(os.path.join(ROOT, "public"))
PLAT = "(roblox|steam)"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body, indent=1).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        window = int(q.get("window", 24))
        con = db.connect(DB)
        now = con.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
        if now is None and u.path.startswith("/api/"):
            return self._send(503, {"error": "no data yet — run `python run.py all` first"})

        if m := re.fullmatch(rf"/api/trends/{PLAT}", u.path):
            r = scoring.rank(con, m[1], now, window)
            lst = q.get("list")
            return self._send(200, {"generated_at": now, "window_h": window,
                                    **({lst: r.get(lst, [])} if lst else r)})
        if m := re.fullmatch(rf"/api/genres/{PLAT}", u.path):
            return self._send(200, scoring.rank(con, m[1], now, window)["genres"])
        if m := re.fullmatch(rf"/api/game/{PLAT}/(\d+)", u.path):
            meta = con.execute("SELECT * FROM games WHERE platform=? AND game_id=?",
                               (m[1], m[2])).fetchone()
            if not meta:
                return self._send(404, {"error": "unknown game"})
            snaps = con.execute("SELECT ts, ccu, visits, favorites, reviews FROM snapshots "
                                "WHERE platform=? AND game_id=? ORDER BY ts",
                                (m[1], m[2])).fetchall()
            return self._send(200, {"meta": dict(meta), "snapshots": [dict(s) for s in snaps]})
        if u.path == "/api/digest":
            res = {p: scoring.rank(con, p, now, window) for p in ("roblox", "steam")}
            return self._send(200, report.digest_md(res, now, window), "text/markdown")
        # Anything else: static files from public/ (the dashboard and its json).
        rel = "index.html" if u.path in ("", "/") else u.path.lstrip("/")
        fp = os.path.realpath(os.path.join(PUBLIC, rel))
        if fp.startswith(PUBLIC + os.sep) and os.path.isfile(fp):
            ctype = {"html": "text/html", "json": "application/json",
                     "md": "text/markdown"}.get(fp.rsplit(".", 1)[-1], "text/plain")
            with open(fp, "rb") as f:
                return self._send(200, f.read(), ctype + "; charset=utf-8")
        return self._send(404, {"error": "see docstring in serve.py for endpoints"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8787))
    print(f"trend API on http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
