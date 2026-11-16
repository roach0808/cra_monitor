"""Background loop that searches every city, with human-like pacing."""
import random
import time
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
        self.status = {
            "state": "starting", "round": 0, "area": "", "done": 0, "total": 0,
            "errors": 0, "last_error": "", "next_round_at": None, "resume_at": None,
            "last_round_seconds": None, "new_this_round": 0,
        }

    def wait(self, seconds):
        """Sleep, but wake immediately when the app is stopping. Returns False if stopping."""
        return not self.stop.wait(max(0, seconds))

    def run(self):
        cfg = self.cfg
        jobs = [(a, kw) for a in self.areas for kw in cfg["keywords"]]
        self.status["total"] = len(jobs)
        log(f"Watching {len(self.areas)} cities x {len(cfg['keywords'])} keyword(s) = "
            f"{len(jobs)} requests per round")
        blocked_streak = 0

        while not self.stop.is_set():
            if cfg["shuffle_city_order"]:
                random.shuffle(jobs)
            round_id = self.store.start_round()
            self.status.update(state="scanning", round=self.status["round"] + 1, done=0,
                               errors=0, new_this_round=0, next_round_at=None)
            started, blocked_count = time.time(), 0
            next_break = int(between(cfg["long_break_every_requests"]))

            for i, (area, kw) in enumerate(jobs, 1):
                if self.stop.is_set():
                    return
                self.status["area"] = area["name"]
                key = f"{area['host']}|{kw}|{cfg['category']}"
                first_scan = not self.store.was_scanned(key)
                try:
                    payload = craigslist.fetch(area, kw, cfg["category"], cfg["user_agent"])
                    posts = craigslist.parse(payload, area, kw, cfg["category"])
                    blocked_streak = 0
                except craigslist.Blocked as e:
                    blocked_streak += 1
                    blocked_count += 1
                    mins = min(cfg["backoff_minutes_when_blocked"] * 2 ** (blocked_streak - 1),
                               cfg["max_backoff_minutes"])
                    self.status.update(state="paused (blocked)", resume_at=int(time.time() + mins * 60),
                                       last_error=f"{datetime.now():%H:%M} HTTP {e.code} on {area['host']}")
                    log(f"Craigslist returned HTTP {e.code}; pausing {mins} min")
                    if not self.wait(mins * 60):
                        return
                    self.status.update(state="scanning", resume_at=None)
                    self.status["done"] = i
                    continue
                except Exception as e:  # network hiccup, bad JSON, ...
                    self.status["errors"] += 1
                    self.status["last_error"] = f"{datetime.now():%H:%M} {type(e).__name__} on {area['host']}: {e}"
                    log(self.status["last_error"])
                    self.status["done"] = i
                    if not self.wait(between(cfg["delay_between_requests_seconds"])):
                        return
                    continue

                alert = cfg["alert_on_first_scan"] or not first_scan
                new = self.store.add_many(posts, alert, cfg["repost_window_days"], cfg["alert_on_reposts"])
                for p in new:
                    if p["alert"]:
                        self.status["new_this_round"] += 1
                        log(f"NEW  [{p['area']}] {p['title']}  {p['url']}")
                if first_scan:
                    self.store.mark_scanned(key)
                    if new:
                        log(f"{area['name']}: saved {len(new)} existing match(es) (first scan, no alert)")
                self.status["done"] = i

                # Pacing: random gap between requests, plus an occasional longer break.
                if i >= next_break and i < len(jobs):
                    secs = between(cfg["long_break_seconds"])
                    self.status["state"] = "short break"
                    if not self.wait(secs):
                        return
                    self.status["state"] = "scanning"
                    next_break = i + int(between(cfg["long_break_every_requests"]))
                elif not self.wait(between(cfg["delay_between_requests_seconds"])):
                    return

            took = int(time.time() - started)
            self.store.finish_round(round_id, len(jobs), self.status["errors"], blocked_count,
                                    self.status["new_this_round"])
            pause = between(cfg["pause_between_rounds_minutes"]) * 60
            self.status.update(state="waiting", last_round_seconds=took,
                               next_round_at=int(time.time() + pause))
            log(f"Round {self.status['round']} done in {took // 60} min, "
                f"{self.status['new_this_round']} new post(s). Next round in {int(pause // 60)} min")
            if not self.wait(pause):
                return
