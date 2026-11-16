"""Small web server: the dashboard page, a JSON API, and an optional password."""
import hashlib
import hmac
import json
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler

COOKIE = "clm_auth"

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Resume Radar · Sign in</title>
<style>
:root{--bg:#f4f5f8;--card:#fff;--text:#14171c;--muted:#626b7a;--line:#e3e6ec;--accent:#4f46e5}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--text:#e8eaee;--muted:#9097a3;--line:#2a2e36;--accent:#8b85ff}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px}
form{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px;width:100%;max-width:340px}
h1{font-size:20px;margin:0 0 4px}p{color:var(--muted);margin:0 0 18px;font-size:14px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:transparent;color:inherit;font:inherit}
button{margin-top:12px;width:100%;padding:10px;border:0;border-radius:9px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
.err{color:#e5484d;font-size:13px;margin-top:10px}
</style></head><body><form method="post" action="/login">
<h1>Resume Radar</h1><p>Enter the password to see alerts.</p>
<input type="password" name="password" autofocus placeholder="Password" aria-label="Password">
<button>Sign in</button>__ERROR__</form></body></html>"""


def make_handler(cfg, store, scanner, page_path):
    password = cfg.get("password") or ""
    token = hmac.new(store.secret().encode(), password.encode(), hashlib.sha256).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        # ---------------------------------------------------------- utils
        def send(self, code, body, ctype="application/json", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def authed(self):
            if not password:
                return True
            jar = SimpleCookie(self.headers.get("Cookie", ""))
            got = jar[COOKIE].value if COOKIE in jar else ""
            return hmac.compare_digest(got, token)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(min(n, 65536)) if n else b""

        # ---------------------------------------------------------- GET
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}

            if url.path == "/login":
                return self.send(200, LOGIN_PAGE.replace("__ERROR__", ""), "text/html; charset=utf-8")
            if url.path == "/logout":
                return self.send(303, "", "text/plain", {
                    "Location": "/login", "Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"})
            if not self.authed():
                if url.path.startswith("/api/"):
                    return self.send(401, {"error": "login required"})
                return self.send(303, "", "text/plain", {"Location": "/login"})

            if url.path in ("/", "/index.html"):
                return self.send(200, page_path.read_bytes(), "text/html; charset=utf-8")

            if url.path == "/api/state":
                after = int(q.get("after") or 0)
                return self.send(200, {
                    "status": scanner.status,
                    "now": int(time.time()),
                    "keywords": cfg["keywords"],
                    "cities": len(scanner.areas),
                    "counts": store.counts(),
                    "max_id": store.max_id(),
                    "alerts": store.alerts_after(after) if after else [],
                    "per_day": store.per_day(14),
                    "states": store.states(),
                    "rounds": store.recent_rounds(8),
                    "password_on": bool(password),
                })

            if url.path == "/api/matches":
                try:
                    limit = max(1, min(200, int(q.get("limit") or 50)))
                    offset = max(0, int(q.get("offset") or 0))
                except ValueError:
                    return self.send(400, {"error": "bad paging"})
                items, total = store.list(q.get("view", "unread"), q.get("q", "").strip(),
                                          q.get("state", ""), q.get("sort", "found"), limit, offset)
                return self.send(200, {"items": items, "total": total})

            self.send(404, {"error": "not found"})

        # ---------------------------------------------------------- POST
        def do_POST(self):
            url = urllib.parse.urlparse(self.path)

            if url.path == "/login":
                form = urllib.parse.parse_qs(self.body().decode("utf-8", "replace"))
                given = (form.get("password") or [""])[0]
                if password and hmac.compare_digest(given.encode(), password.encode()):
                    return self.send(303, "", "text/plain", {
                        "Location": "/",
                        "Set-Cookie": f"{COOKIE}={token}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax"})
                time.sleep(1)  # slow down password guessing
                page = LOGIN_PAGE.replace("__ERROR__", '<div class="err">Wrong password.</div>')
                return self.send(401, page, "text/html; charset=utf-8")

            if not self.authed():
                return self.send(401, {"error": "login required"})
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self.send(415, {"error": "send JSON"})
            try:
                data = json.loads(self.body() or b"{}")
            except ValueError:
                return self.send(400, {"error": "bad JSON"})

            if url.path.startswith("/api/matches/"):
                try:
                    row_id = int(url.path.rsplit("/", 1)[1])
                except ValueError:
                    return self.send(400, {"error": "bad id"})
                ok = store.update(row_id, data)
                return self.send(200 if ok else 404, {"ok": ok})

            if url.path == "/api/mark-all-read":
                n = store.mark_all_read(int(data.get("up_to_id") or 0), data.get("state") or "")
                return self.send(200, {"ok": True, "count": n})

            self.send(404, {"error": "not found"})

    return Handler
