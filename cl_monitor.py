#!/usr/bin/env python3
"""Resume Radar: Craigslist resume alert bot.

Searches the resumes section of every US Craigslist city for the keywords in
config.json (titles AND post text, like the craigslist.org search bar), keeps
every match in matches.db, and shows them on a dashboard with Chrome
notifications at http://localhost:<port>.

Run:  python cl_monitor.py        (Python 3.8+, no extra packages needed)
"""
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from scanner import Scanner, log
from server import make_handler
from store import Store

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "keywords": ["any work"],
    "category": "rrr",
    "areas": "all",
    "delay_between_requests_seconds": [3, 8],
    "long_break_every_requests": [40, 70],
    "long_break_seconds": [30, 90],
    "pause_between_rounds_minutes": [5, 15],
    "shuffle_city_order": True,
    "backoff_minutes_when_blocked": 15,
    "max_backoff_minutes": 120,
    "alert_on_first_scan": False,
    "alert_on_reposts": False,
    "repost_window_days": 30,
    "host": "127.0.0.1",
    "port": 8765,
    "password": "",
    "user_agent": "cl-resume-alert/1.0 (personal use)",
}


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update(json.loads((HERE / "config.json").read_text(encoding="utf-8")))
    return cfg


def load_areas(cfg):
    areas = json.loads((HERE / "us_areas.json").read_text(encoding="utf-8"))
    if cfg["areas"] != "all":
        wanted = {h.strip().lower() for h in cfg["areas"]}
        areas = [a for a in areas if a["host"] in wanted]
    return areas


def main():
    cfg = load_config()
    store = Store(HERE / "matches.db")
    store.backfill_areas(json.loads((HERE / "us_areas.json").read_text(encoding="utf-8")))
    stop = threading.Event()
    scanner = Scanner(cfg, load_areas(cfg), store, stop)
    threading.Thread(target=scanner.run, daemon=True).start()

    server = ThreadingHTTPServer((cfg["host"], cfg["port"]),
                                 make_handler(cfg, store, scanner, HERE / "index.html"))
    log(f"Dashboard: http://localhost:{cfg['port']}  (open in Chrome, then click 'Notifications')")
    if not cfg["password"]:
        log("No password set. Fine on your own PC; set one in config.json before sharing a tunnel link.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Stopping")
        stop.set()
        server.shutdown()


if __name__ == "__main__":
    main()
