"""Background loop that searches every city, with human-like pacing."""
import random
import time
from collections import deque
from datetime import datetime

import craigslist


def log(msg):
    print(datetime.now().strftime("%H:%M:%S"), msg, flush=True)


def between(pair):
    lo, hi = (pair, pair) if isinstance(pair, (int, float)) else pair
    return random.uniform(lo, hi)


class Scanner:
    def __init__(self, cfg, areas, store, stop):
        self.cfg, self.areas, self.store, self.stop = cfg, areas, store, stop
        self.activity = deque(maxlen=40)  # most recent requests, newest last
        self.status = {
            "state": "starting",          # starting | scanning | waiting_request | short_break | waiting_round | blocked
            "app_started_at": int(time.time()),
            "round": 0, "done": 0, "total": 0,
            "area": "", "area_state": "", "keyword": "",
            "round_started_at": None, "eta_seconds": None,
            "last_request_at": None, "last_result": None,
            "wait_until": None,           # when the current wait (request gap / break / round pause / block) ends
            "errors": 0, "new_this_round": 0, "last_error": "",
            "last_round_seconds": None,
            "pacing": {
                "delay": cfg["delay_between_requests_seconds"],
                "break_every": cfg["long_break_every_requests"],
                "break_seconds": cfg["long_break_seconds"],
                "round_pause_minutes": cfg["pause_between_rounds_minutes"],
            },
        }

    def wait(self, seconds, state):
        """Sleep while showing what we're waiting for. Returns False if the app is stopping."""
        seconds = max(0, seconds)
        self.status.update(state=state, wait_until=time.time() + seconds)
        stopped = self.stop.wait(seconds)
        self.status["wait_until"] = None
        return not stopped

    def record(self, area, kw, i, started, status, results=0, new=0, msg=""):
        took = round(time.time() - started, 2)
        entry = {"t": int(time.time()), "round": self.status["round"], "n": i, "total": self.status["total"],
                 "city": area["name"], "state": area.get("state", ""), "kw": kw,
                 "results": results, "new": new, "took": took, "status": status, "msg": msg}
        self.activity.append(entry)
        self.status.update(last_request_at=entry["t"], last_result=entry)
        tag = {"ok": "", "error": "  ERROR", "blocked": "  BLOCKED"}[status]
        log(f"[round {entry['round']} · {i}/{entry['total']}] {area['name']}: "
            f"{results} result(s), {new} new ({took}s){tag}{(' ' + msg) if msg else ''}")

    def update_eta(self, i):
        elapsed = time.time() - self.status["round_started_at"]
        remaining = self.status["total"] - i
        self.status["eta_seconds"] = int(elapsed / i * remaining) if i else None

    def run(self):
        cfg = self.cfg
        jobs = [(a, kw) for a in self.areas for kw in cfg["keywords"]]
        self.status["total"] = len(jobs)
        log(f"Watching {len(self.areas)} cities x {len(cfg['keywords'])} keyword(s) = "
            f"{len(jobs)} requests per round")
        log(f"Pacing: {cfg['delay_between_requests_seconds']} s between requests, "
            f"{cfg['long_break_seconds']} s break every {cfg['long_break_every_requests']} requests, "
            f"{cfg['pause_between_rounds_minutes']} min between rounds")
        blocked_streak = 0

        while not self.stop.is_set():
            if cfg["shuffle_city_order"]:
                random.shuffle(jobs)
            round_id = self.store.start_round()
            self.status.update(round=self.status["round"] + 1, done=0, errors=0, new_this_round=0,
                               round_started_at=int(time.time()), eta_seconds=None)
            log(f"--- Round {self.status['round']} started ---")
            started_round, blocked_count = time.time(), 0
            next_break = int(between(cfg["long_break_every_requests"]))

            for i, (area, kw) in enumerate(jobs, 1):
                if self.stop.is_set():
                    return
                self.status.update(state="scanning", area=area["name"], area_state=area.get("state", ""), keyword=kw)
                key = f"{area['host']}|{kw}|{cfg['category']}"
                first_scan = not self.store.was_scanned(key)
                t0 = time.time()
                try:
                    payload = craigslist.fetch(area, kw, cfg["category"], cfg["user_agent"])
                    posts = craigslist.parse(payload, area, kw, cfg["category"])
                    blocked_streak = 0
                except craigslist.Blocked as e:
                    blocked_streak += 1
                    blocked_count += 1
                    mins = min(cfg["backoff_minutes_when_blocked"] * 2 ** (blocked_streak - 1),
                               cfg["max_backoff_minutes"])
                    self.status["last_error"] = f"{datetime.now():%H:%M} HTTP {e.code} on {area['name']}"
                    self.record(area, kw, i, t0, "blocked", msg=f"HTTP {e.code}, pausing {mins:g} min")
                    self.status["done"] = i
                    self.update_eta(i)
                    if not self.wait(mins * 60, "blocked"):
                        return
                    continue
                except Exception as e:  # network hiccup, bad JSON, ...
                    self.status["errors"] += 1
                    self.status["last_error"] = f"{datetime.now():%H:%M} {type(e).__name__} on {area['name']}: {e}"
                    self.record(area, kw, i, t0, "error", msg=f"{type(e).__name__}: {e}"[:200])
                    self.status["done"] = i
                    self.update_eta(i)
                    if not self.wait(between(cfg["delay_between_requests_seconds"]), "waiting_request"):
                        return
                    continue

                alert = cfg["alert_on_first_scan"] or not first_scan
                new = self.store.add_many(posts, alert, cfg["repost_window_days"], cfg["alert_on_reposts"])
                alerted = [p for p in new if p["alert"]]
                self.status["new_this_round"] += len(alerted)
                if first_scan:
                    self.store.mark_scanned(key)
                note = f"first scan, saved {len(new)} existing" if first_scan and new else ""
                self.record(area, kw, i, t0, "ok", results=len(posts), new=len(alerted), msg=note)
                for p in alerted:
                    log(f"   NEW  {p['title']}  {p['url']}")
                self.status["done"] = i
                self.update_eta(i)

                # Pacing: random gap between requests, plus an occasional longer break.
                if i >= len(jobs):
                    break
                if i >= next_break:
                    secs = between(cfg["long_break_seconds"])
                    log(f"Short break for {secs:.0f}s")
                    if not self.wait(secs, "short_break"):
                        return
                    next_break = i + int(between(cfg["long_break_every_requests"]))
                elif not self.wait(between(cfg["delay_between_requests_seconds"]), "waiting_request"):
                    return

            took = int(time.time() - started_round)
            self.store.finish_round(round_id, len(jobs), self.status["errors"], blocked_count,
                                    self.status["new_this_round"])
            pause = between(cfg["pause_between_rounds_minutes"]) * 60
            self.status.update(last_round_seconds=took, eta_seconds=None, area="", keyword="")
            log(f"--- Round {self.status['round']} done in {took // 60} min {took % 60} s, "
                f"{self.status['new_this_round']} new post(s). Next round in {pause / 60:.1f} min ---")
            if not self.wait(pause, "waiting_round"):
                return
