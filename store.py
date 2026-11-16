"""SQLite storage: every match ever found, its read/star/hide state, scan history."""
import secrets
import sqlite3
import threading
import time
from datetime import datetime

COLUMNS = ("id", "posting_id", "title", "url", "area", "area_host", "state", "place",
           "keyword", "posted", "found", "alert", "is_read", "starred", "hidden", "repost_of")

# Columns added after the first version; older databases are upgraded in place.
ADDED_COLUMNS = {
    "area_host": "TEXT DEFAULT ''",
    "state": "TEXT DEFAULT ''",
    "is_read": "INTEGER DEFAULT 0",
    "read_at": "INTEGER",
    "starred": "INTEGER DEFAULT 0",
    "hidden": "INTEGER DEFAULT 0",
    "repost_of": "INTEGER",
}

VIEWS = {
    "unread": "is_read=0 AND hidden=0",
    "all": "hidden=0",
    "starred": "starred=1",
    "hidden": "hidden=1",
}


def local_midnight():
    now = datetime.now()
    return int(datetime(now.year, now.month, now.day).timestamp())


class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                posting_id INTEGER UNIQUE,
                title TEXT, url TEXT, area TEXT, place TEXT,
                keyword TEXT, posted INTEGER, found INTEGER,
                alert INTEGER       -- 1 = appeared while watching, 0 = already there at first scan
            );
            CREATE TABLE IF NOT EXISTS scanned (area_kw TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started INTEGER, finished INTEGER,
                requests INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
                blocked INTEGER DEFAULT 0, new_posts INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        have = {r[1] for r in self.db.execute("PRAGMA table_info(matches)")}
        for col, decl in ADDED_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE matches ADD COLUMN {col} {decl}")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_repost ON matches(area_host, title COLLATE NOCASE)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_found ON matches(found)")
        self.db.commit()

    # ------------------------------------------------------------ helpers
    def _rows(self, sql, args=()):
        return [dict(zip(COLUMNS, r)) for r in self.db.execute(sql, args).fetchall()]

    def secret(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key='secret'").fetchone()
            if row:
                return row[0]
            value = secrets.token_hex(32)
            self.db.execute("INSERT INTO meta VALUES ('secret', ?)", (value,))
            self.db.commit()
            return value

    def backfill_areas(self, areas):
        """Rows saved by the first version have no area_host/state; fill them in from the city name."""
        with self.lock:
            for a in areas:
                self.db.execute("UPDATE matches SET area_host=?, state=? WHERE area=? AND"
                                " (area_host IS NULL OR area_host='')", (a["host"], a.get("state", ""), a["name"]))
            self.db.commit()

    # ------------------------------------------------------------ scanning
    def was_scanned(self, key):
        with self.lock:
            return self.db.execute("SELECT 1 FROM scanned WHERE area_kw=?", (key,)).fetchone() is not None

    def mark_scanned(self, key):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO scanned VALUES (?)", (key,))
            self.db.commit()

    def add_many(self, posts, alert, repost_window_days=30, alert_on_reposts=False):
        """Save posts not seen before. Returns the new ones; each gets 'alert' and 'repost_of'."""
        new, now = [], int(time.time())
        since = now - repost_window_days * 86400
        cols = ", ".join(c for c in COLUMNS if c != "id")
        marks = ", ".join("?" for c in COLUMNS if c != "id")
        with self.lock:
            for p in posts:
                if self.db.execute("SELECT 1 FROM matches WHERE posting_id=?", (p["id"],)).fetchone():
                    continue
                dup = None
                if p["title"]:
                    row = self.db.execute(
                        "SELECT posting_id FROM matches WHERE area_host=? AND title=? COLLATE NOCASE"
                        " AND found>=? LIMIT 1", (p["area_host"], p["title"], since)).fetchone()
                    dup = row[0] if row else None
                p_alert = 1 if alert and (dup is None or alert_on_reposts) else 0
                self.db.execute(
                    f"INSERT INTO matches ({cols}) VALUES ({marks})",
                    (p["id"], p["title"], p["url"], p["area"], p["area_host"], p["state"], p["place"],
                     p["keyword"], p["posted"], now, p_alert, 0, 0, 0, dup),
                )
                new.append(dict(p, alert=p_alert, repost_of=dup))
            self.db.commit()
        return new

    def start_round(self):
        with self.lock:
            cur = self.db.execute("INSERT INTO rounds (started) VALUES (?)", (int(time.time()),))
            self.db.commit()
            return cur.lastrowid

    def finish_round(self, round_id, requests, errors, blocked, new_posts):
        with self.lock:
            self.db.execute(
                "UPDATE rounds SET finished=?, requests=?, errors=?, blocked=?, new_posts=? WHERE id=?",
                (int(time.time()), requests, errors, blocked, new_posts, round_id))
            self.db.commit()

    # ------------------------------------------------------------ reading
    def list(self, view="unread", q="", state="", sort="found", limit=50, offset=0):
        where, args = [VIEWS.get(view, VIEWS["unread"])], []
        if q:
            where.append("(title LIKE ? OR area LIKE ? OR place LIKE ?)")
            args += [f"%{q}%"] * 3
        if state:
            where.append("state=?")
            args.append(state)
        clause = " AND ".join(where)
        order = "posted DESC" if sort == "posted" else "id DESC"
        with self.lock:
            total = self.db.execute(f"SELECT COUNT(*) FROM matches WHERE {clause}", args).fetchone()[0]
            rows = self._rows(f"SELECT {', '.join(COLUMNS)} FROM matches WHERE {clause}"
                              f" ORDER BY {order} LIMIT ? OFFSET ?", args + [limit, offset])
        return rows, total

    def alerts_after(self, after_id, limit=50):
        """New (non-repost) posts found after a given row id, for notifications."""
        with self.lock:
            return self._rows(f"SELECT {', '.join(COLUMNS)} FROM matches WHERE id>? AND alert=1"
                              " ORDER BY id DESC LIMIT ?", (after_id, limit))

    def max_id(self):
        with self.lock:
            return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM matches").fetchone()[0]

    def counts(self):
        with self.lock:
            q = lambda sql, a=(): self.db.execute(sql, a).fetchone()[0]
            return {
                "unread": q("SELECT COUNT(*) FROM matches WHERE " + VIEWS["unread"]),
                "all": q("SELECT COUNT(*) FROM matches WHERE " + VIEWS["all"]),
                "starred": q("SELECT COUNT(*) FROM matches WHERE " + VIEWS["starred"]),
                "hidden": q("SELECT COUNT(*) FROM matches WHERE " + VIEWS["hidden"]),
                "new_today": q("SELECT COUNT(*) FROM matches WHERE alert=1 AND found>=?", (local_midnight(),)),
                "reposts": q("SELECT COUNT(*) FROM matches WHERE repost_of IS NOT NULL"),
            }

    def per_day(self, days=14):
        start = local_midnight() - (days - 1) * 86400
        with self.lock:
            rows = self.db.execute(
                "SELECT date(found,'unixepoch','localtime'), COUNT(*) FROM matches"
                " WHERE alert=1 AND found>=? GROUP BY 1", (start,)).fetchall()
        got = dict(rows)
        out = []
        for i in range(days):
            day = datetime.fromtimestamp(start + i * 86400 + 3600).strftime("%Y-%m-%d")
            out.append({"day": day, "count": got.get(day, 0)})
        return out

    def states(self):
        with self.lock:
            return [{"state": s, "count": c} for s, c in self.db.execute(
                "SELECT state, COUNT(*) FROM matches WHERE hidden=0 AND state!='' GROUP BY state ORDER BY state")]

    def recent_rounds(self, n=8):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, started, finished, requests, errors, blocked, new_posts FROM rounds"
                " ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        keys = ("id", "started", "finished", "requests", "errors", "blocked", "new_posts")
        return [dict(zip(keys, r)) for r in rows]

    # ------------------------------------------------------------ changes
    def update(self, row_id, fields):
        allowed = {"is_read", "starred", "hidden"}
        sets = {k: 1 if v else 0 for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        if "is_read" in sets:
            sets["read_at"] = int(time.time()) if sets["is_read"] else None
        sql = "UPDATE matches SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?"
        with self.lock:
            cur = self.db.execute(sql, list(sets.values()) + [row_id])
            self.db.commit()
            return cur.rowcount == 1

    def mark_all_read(self, up_to_id, state=""):
        sql = "UPDATE matches SET is_read=1, read_at=? WHERE is_read=0 AND hidden=0 AND id<=?"
        args = [int(time.time()), up_to_id]
        if state:
            sql += " AND state=?"
            args.append(state)
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.rowcount
