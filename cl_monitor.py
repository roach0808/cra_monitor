#!/usr/bin/env python3
"""Craigslist resume alert bot.

Searches the resumes section (cat=rrr) of every US Craigslist area for the
keywords in config.json, the same way the craigslist.org search bar does
(titles AND post text). New matches are stored in matches.db and shown as
Chrome notifications by the page at http://localhost:<port>.

Run:  python cl_monitor.py        (Python 3.8+, no extra packages needed)
"""
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEARCH_API = "https://sapi.craigslist.org/web/v8/postings/search/full"


def load_config():
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    cfg.setdefault("keywords", ["any work"])
    cfg.setdefault("category", "rrr")
    cfg.setdefault("areas", "all")
    cfg.setdefault("seconds_between_requests", 3)
    cfg.setdefault("seconds_between_rounds", 300)
    cfg.setdefault("backoff_minutes_when_blocked", 15)
    cfg.setdefault("alert_on_first_scan", False)
    cfg.setdefault("port", 8765)
    cfg.setdefault("user_agent", "cl-resume-alert/1.0 (personal use)")
    return cfg


def load_areas(cfg):
    areas = json.loads((HERE / "us_areas.json").read_text(encoding="utf-8"))
    if cfg["areas"] != "all":
        wanted = {h.strip().lower() for h in cfg["areas"]}
        areas = [a for a in areas if a["host"] in wanted]
    return areas


# ---------------------------------------------------------------- database

class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                posting_id INTEGER UNIQUE,
                title TEXT, url TEXT, area TEXT, place TEXT,
                keyword TEXT, posted INTEGER, found INTEGER,
                alert INTEGER            -- 1 = new post (notify), 0 = existed at first scan
            );
            CREATE TABLE IF NOT EXISTS scanned (area_kw TEXT PRIMARY KEY);
            """
        )
        self.db.commit()

    def was_scanned(self, key):
        with self.lock:
            return self.db.execute("SELECT 1 FROM scanned WHERE area_kw=?", (key,)).fetchone() is not None

    def mark_scanned(self, key):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO scanned VALUES (?)", (key,))
            self.db.commit()

    def add_many(self, posts, alert):
        """Insert matches in one transaction; returns the ones not seen before."""
        new, now = [], int(time.time())
        with self.lock:
            for p in posts:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO matches (posting_id,title,url,area,place,keyword,posted,found,alert)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (p["id"], p["title"], p["url"], p["area"], p["place"],
                     p["keyword"], p["posted"], now, 1 if alert else 0),
                )
                if cur.rowcount == 1:
                    new.append(p)
            self.db.commit()
        return new

    def since(self, after_id, limit=300):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,posting_id,title,url,area,place,keyword,posted,found,alert FROM matches"
                " WHERE id>? ORDER BY id DESC LIMIT ?", (after_id, limit)).fetchall()
        keys = ("id", "posting_id", "title", "url", "area", "place", "keyword", "posted", "found", "alert")
        return [dict(zip(keys, r)) for r in rows]


# ------------------------------------------------------------- craigslist

def fetch_area(area, keyword, cfg):
    params = {
        "batch": f"{area['id']}-0-360-0-0",
        "cc": "US",
        "lang": "en",
        "searchPath": cfg["category"],
        "query": keyword,
    }
    req = urllib.request.Request(
        SEARCH_API + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": cfg["user_agent"], "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_results(payload, area, keyword, category):
    """Turn the compact search response into a list of posts with real URLs."""
    data = payload.get("data") or {}
    dec = data.get("decode") or {}
    min_id = dec.get("minPostingId", 0)
    min_date = dec.get("minPostedDate", 0)
    locations = dec.get("locations") or []
    descriptions = dec.get("locationDescriptions") or []
    posts = []
    for item in data.get("items") or []:
        try:
            pid = min_id + int(item[0])
            posted = min_date + int(item[1])
            title = item[-1] if isinstance(item[-1], str) else ""
            slug = next((x[1] for x in item if isinstance(x, list) and len(x) > 1 and x[0] == 6), "post")

            host, sub, place = area["host"], "", ""
            loc = item[4] if len(item) > 4 and isinstance(item[4], str) else ""
            parts = loc.split("~")[0].split(":")
            if parts and parts[0].isdigit():
                idx = int(parts[0])
                if 0 < idx < len(locations) and isinstance(locations[idx], list):
                    entry = locations[idx]
                    host = entry[1] if len(entry) > 1 and entry[1] else host
                    sub = entry[2] if len(entry) > 2 and entry[2] else ""
            if len(parts) >= 3 and parts[2].isdigit():
                d = int(parts[2])
                if 0 < d < len(descriptions) and isinstance(descriptions[d], str):
                    place = descriptions[d]

            path = f"{sub}/" if sub else ""
            posts.append({
                "id": pid,
                "title": title,
                "url": f"https://{host}.craigslist.org/{path}{category}/d/{slug}/{pid}.html",
                "area": area["name"],
                "place": place,
                "keyword": keyword,
                "posted": posted,
            })
        except (IndexError, TypeError, ValueError):
            continue
    return posts


# ----------------------------------------------------------------- scanner

STATUS = {"state": "starting", "round": 0, "area": "", "done": 0, "total": 0,
          "last_error": "", "last_round_seconds": None, "matches_this_round": 0}


def log(msg):
    print(datetime.now().strftime("%H:%M:%S"), msg, flush=True)


def scanner(cfg, store, stop):
    areas = load_areas(cfg)
    jobs = [(a, kw) for a in areas for kw in cfg["keywords"]]
    STATUS["total"] = len(jobs)
    log(f"Watching {len(areas)} areas x {len(cfg['keywords'])} keyword(s) = {len(jobs)} requests per round")

    while not stop.is_set():
        STATUS.update(state="scanning", round=STATUS["round"] + 1, done=0, matches_this_round=0)
        started = time.time()
        for area, kw in jobs:
            if stop.is_set():
                return
            STATUS["area"] = area["name"]
            key = f"{area['host']}|{kw}|{cfg['category']}"
            first_scan = not store.was_scanned(key)
            try:
                posts = parse_results(fetch_area(area, kw, cfg), area, kw, cfg["category"])
            except urllib.error.HTTPError as e:
                if e.code in (403, 429):
                    mins = cfg["backoff_minutes_when_blocked"]
                    STATUS.update(state=f"blocked (HTTP {e.code}), pausing {mins} min",
                                  last_error=f"{datetime.now():%H:%M} HTTP {e.code} on {area['host']}")
                    log(f"Craigslist returned HTTP {e.code}; pausing {mins} minutes")
                    stop.wait(mins * 60)
                    STATUS["state"] = "scanning"
                else:
                    STATUS["last_error"] = f"{datetime.now():%H:%M} HTTP {e.code} on {area['host']}"
                    log(f"HTTP {e.code} on {area['host']}")
                STATUS["done"] += 1
                stop.wait(cfg["seconds_between_requests"])
                continue
            except Exception as e:  # network hiccup, bad JSON, ...
                STATUS["last_error"] = f"{datetime.now():%H:%M} {type(e).__name__} on {area['host']}: {e}"
                log(STATUS["last_error"])
                STATUS["done"] += 1
                stop.wait(cfg["seconds_between_requests"])
                continue

            alert = cfg["alert_on_first_scan"] or not first_scan
            new = store.add_many(posts, alert)
            if alert:
                STATUS["matches_this_round"] += len(new)
                for p in new:
                    log(f"NEW  [{p['area']}] {p['title']}  {p['url']}")
            if first_scan:
                store.mark_scanned(key)
                if new:
                    log(f"{area['name']}: {len(new)} existing match(es) saved (no alert, first scan)")
            STATUS["done"] += 1
            stop.wait(cfg["seconds_between_requests"])

        STATUS["last_round_seconds"] = int(time.time() - started)
        STATUS["state"] = "waiting for next round"
        log(f"Round {STATUS['round']} finished in {STATUS['last_round_seconds']}s, "
            f"{STATUS['matches_this_round']} new match(es)")
        stop.wait(cfg["seconds_between_rounds"])


# -------------------------------------------------------------- web page

def make_handler(store, cfg):
    page = (HERE / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self.send(200, page, "text/html; charset=utf-8")
            elif url.path == "/api/state":
                q = urllib.parse.parse_qs(url.query)
                after = int((q.get("after") or ["0"])[0] or 0)
                body = {"status": STATUS, "keywords": cfg["keywords"], "matches": store.since(after)}
                self.send(200, json.dumps(body).encode(), "application/json")
            else:
                self.send(404, b"not found", "text/plain")

    return Handler


def main():
    cfg = load_config()
    store = Store(HERE / "matches.db")
    stop = threading.Event()
    threading.Thread(target=scanner, args=(cfg, store, stop), daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), make_handler(store, cfg))
    log(f"Open http://localhost:{cfg['port']} in Chrome and click 'Enable notifications'")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Stopping")
        stop.set()
        server.shutdown()


if __name__ == "__main__":
    main()
