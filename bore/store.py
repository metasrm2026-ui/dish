"""SQLite storage: items, page routing, collection state, engagement overlay, sent index."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,            -- x | reddit | web
    native_id TEXT,
    url TEXT,
    author TEXT,                       -- lowercased handle / reddit user / web domain
    original_author TEXT,              -- for reposts: the author being amplified
    is_repost INTEGER DEFAULT 0,
    title TEXT,
    text TEXT,
    subreddit TEXT,
    query TEXT,                        -- web search query that found it
    created_at REAL,
    collected_at REAL NOT NULL,
    engagement INTEGER DEFAULT 0,      -- capture-time engagement
    metrics TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at);
CREATE TABLE IF NOT EXISTS page_items (
    page TEXT NOT NULL, item_id TEXT NOT NULL, via TEXT,
    PRIMARY KEY (page, item_id)
);
CREATE TABLE IF NOT EXISTS source_state (
    source TEXT PRIMARY KEY, last_success REAL, last_attempt REAL, last_error TEXT,
    last_count INTEGER
);
CREATE TABLE IF NOT EXISTS overlay (
    item_id TEXT PRIMARY KEY, engagement INTEGER NOT NULL, refreshed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sent_index (
    page TEXT NOT NULL, key TEXT NOT NULL, signature TEXT NOT NULL,
    sent_at REAL NOT NULL, engagement INTEGER, sources INTEGER, score REAL, headline TEXT,
    PRIMARY KEY (page, key)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, started REAL, finished REAL,
    status TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS spend (
    at REAL NOT NULL, actor TEXT, usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    run TEXT, page TEXT, brief TEXT, decision TEXT, reason TEXT, headline TEXT, at REAL,
    PRIMARY KEY (run, brief)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS escalations (
    page TEXT NOT NULL, run_at REAL NOT NULL, queries TEXT, items INTEGER
);
"""


# what the saved state must never lose: run history, the no-repeat index, the budget ledger, with
# how long prune() keeps their rows (table: (time column, days))
KEEP = {"runs": ("started", 14), "sent_index": ("sent_at", 8), "spend": ("at", 45)}
STATE_TABLES = tuple(KEEP)


def check_file(path: str | Path, at: float | None = None) -> tuple[str, dict[str, int]]:
    """SQLite's own quick check of a database file ('ok' when sound) and, per STATE_TABLES table, the
    rows that pruning at time `at` (default now) keeps: rows pruning removes are not what a push may
    lose (after the routine was paused for days, its first run prunes most of them).
    Raises sqlite3.DatabaseError for a file that is not a database at all."""
    if not Path(path).is_file():                  # connecting would create an empty one
        raise FileNotFoundError(f"no database at {path}")
    at = time.time() if at is None else at
    db = sqlite3.connect(str(path))
    try:
        check = "; ".join(r[0] for r in db.execute("PRAGMA quick_check").fetchmany(3))
        counts = {}
        for t, (col, days) in KEEP.items():
            try:                                  # the rows prune() does not delete
                counts[t] = db.execute(f"SELECT COUNT(*) FROM {t} WHERE NOT ({col} < ?)",
                                       (at - days * 86400,)).fetchone()[0]
            except sqlite3.OperationalError:      # an older database without the table
                counts[t] = 0
        return check, counts
    finally:
        db.close()


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------------ items
    def upsert_item(self, item: dict, pages: list[tuple[str, str]]) -> bool:
        """Insert or update an item; engagement never decreases. Returns True if new."""
        cur = self.db.execute("SELECT engagement FROM items WHERE id=?", (item["id"],)).fetchone()
        if cur is None:
            self.db.execute(
                "INSERT INTO items (id, platform, native_id, url, author, original_author, is_repost,"
                " title, text, subreddit, query, created_at, collected_at, engagement, metrics)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["id"], item["platform"], item.get("native_id"), item.get("url"),
                 item.get("author"), item.get("original_author"), int(item.get("is_repost", 0)),
                 item.get("title"), item.get("text"), item.get("subreddit"), item.get("query"),
                 item.get("created_at"), item.get("collected_at", time.time()),
                 int(item.get("engagement", 0)), json.dumps(item.get("metrics", {}))))
            new = True
        else:
            if int(item.get("engagement", 0)) > (cur["engagement"] or 0):
                self.db.execute("UPDATE items SET engagement=?, metrics=? WHERE id=?",
                                (int(item["engagement"]), json.dumps(item.get("metrics", {})), item["id"]))
            new = False
        for page, via in pages:
            self.db.execute("INSERT OR IGNORE INTO page_items (page, item_id, via) VALUES (?,?,?)",
                            (page, item["id"], via))
        return new

    def page_items(self, page: str, since: float) -> list[dict]:
        rows = self.db.execute(
            "SELECT i.*, COALESCE(MAX(i.engagement, o.engagement), i.engagement) AS eng_now,"
            " o.refreshed_at AS refreshed_at"
            " FROM items i JOIN page_items p ON p.item_id=i.id"
            " LEFT JOIN overlay o ON o.item_id=i.id"
            " WHERE p.page=? AND COALESCE(i.created_at, i.collected_at) >= ?",
            (page, since)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["engagement"] = d.pop("eng_now") or 0
            d["metrics"] = json.loads(d["metrics"] or "{}")
            out.append(d)
        return out

    def items_since(self, since: float) -> list[dict]:
        rows = self.db.execute(
            "SELECT i.*, o.refreshed_at AS refreshed_at, o.engagement AS overlay_eng FROM items i"
            " LEFT JOIN overlay o ON o.item_id=i.id WHERE COALESCE(i.created_at, i.collected_at) >= ?",
            (since,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- overlay
    def overlay_upsert(self, item_id: str, engagement: int, now: float | None = None) -> None:
        """Never let a count decrease: a partial response must not erase known engagement."""
        now = now or time.time()
        row = self.db.execute("SELECT engagement FROM overlay WHERE item_id=?", (item_id,)).fetchone()
        base = self.db.execute("SELECT engagement FROM items WHERE id=?", (item_id,)).fetchone()
        floor = max(row["engagement"] if row else 0, base["engagement"] if base else 0)
        self.db.execute(
            "INSERT INTO overlay (item_id, engagement, refreshed_at) VALUES (?,?,?)"
            " ON CONFLICT(item_id) DO UPDATE SET engagement=excluded.engagement,"
            " refreshed_at=excluded.refreshed_at",
            (item_id, max(int(engagement), floor), now))

    def overlay_count(self, since: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM overlay WHERE refreshed_at>=?", (since,)).fetchone()[0]

    # ------------------------------------------------------- routine decisions
    def record_decision(self, run: str, page: str, brief: str, decision: str, reason: str = "",
                        headline: str = "") -> None:
        """What the routine did with each brief (uploaded / skipped + why): the record that
        explains a run's output, kept with the state so it can be read afterwards."""
        self.db.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?)",
                        (run, page, brief, decision, reason[:500], headline[:200], time.time()))
        self.db.commit()

    def decisions(self, since: float) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM decisions WHERE at>=? ORDER BY at", (since,))]

    # ---------------------------------------------------------------- kv
    def get_kv(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_kv(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    # ----------------------------------------------------------- source state
    def get_state(self, source: str) -> dict | None:
        r = self.db.execute("SELECT * FROM source_state WHERE source=?", (source,)).fetchone()
        return dict(r) if r else None

    def all_states(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM source_state")]

    def mark_attempt(self, source: str, error: str | None = None, count: int | None = None,
                     success_at: float | None = None) -> None:
        now = time.time()
        self.db.execute("INSERT OR IGNORE INTO source_state (source) VALUES (?)", (source,))
        self.db.execute("UPDATE source_state SET last_attempt=?, last_error=? WHERE source=?",
                        (now, error, source))
        if success_at is not None:
            self.db.execute("UPDATE source_state SET last_success=?, last_count=? WHERE source=?",
                            (success_at, count, source))
        self.db.commit()

    # ------------------------------------------------------------- sent index
    def sent_for_page(self, page: str, since: float) -> list[dict]:
        rows = self.db.execute("SELECT * FROM sent_index WHERE page=? AND sent_at>=?", (page, since))
        out = []
        for r in rows:
            d = dict(r)
            d["signature"] = set(json.loads(d["signature"]))
            out.append(d)
        return out

    def record_sent(self, page: str, key: str, signature: list[str], engagement: int,
                    sources: int, score: float, headline: str, now: float | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO sent_index (page, key, signature, sent_at, engagement, sources,"
            " score, headline) VALUES (?,?,?,?,?,?,?,?)",
            (page, key, json.dumps(sorted(signature)), now or time.time(), engagement, sources,
             score, headline[:300]))

    # ------------------------------------------------------------------ spend
    def record_spend(self, actor: str, usd: float) -> None:
        self.db.execute("INSERT INTO spend (at, actor, usd) VALUES (?,?,?)", (time.time(), actor, usd))
        self.db.commit()

    def spend_since(self, since: float, actor: str | None = None) -> float:
        q = "SELECT COALESCE(SUM(usd), 0) FROM spend WHERE at>=?"
        args: tuple = (since,)
        if actor:
            q += " AND actor=?"
            args += (actor,)
        return self.db.execute(q, args).fetchone()[0]

    def apify_spend_since(self, since: float) -> float:
        """Apify spend only: Krea images are logged in the same table but have their own monthly cap."""
        return self.db.execute("SELECT COALESCE(SUM(usd), 0) FROM spend WHERE at>=? AND actor NOT LIKE 'krea:%'",
                               (since,)).fetchone()[0]

    def last_cost(self, actor: str) -> float | None:
        r = self.db.execute("SELECT usd FROM spend WHERE actor=? ORDER BY at DESC LIMIT 1", (actor,)).fetchone()
        return r[0] if r else None

    def uploaded_count(self, page: str, since: float) -> int:
        """Posts this routine uploaded to the dashboard since `since` (the daily cap's local fallback)."""
        r = self.db.execute("SELECT COUNT(*) FROM decisions WHERE page=? AND decision='uploaded' AND at>=?",
                            (page, since)).fetchone()
        return int(r[0] or 0)

    def sent_count(self, page: str, since: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM sent_index WHERE page=? AND sent_at>=?",
                               (page, since)).fetchone()[0]

    # ------------------------------------------------------------- runs, misc
    def log_run(self, kind: str, started: float, status: str, detail: dict | str = "") -> None:
        self.db.execute("INSERT INTO runs (kind, started, finished, status, detail) VALUES (?,?,?,?,?)",
                        (kind, started, time.time(), status,
                         detail if isinstance(detail, str) else json.dumps(detail)))
        self.db.commit()

    def record_escalation(self, page: str, queries: list[str], items: int) -> None:
        self.db.execute("INSERT INTO escalations (page, run_at, queries, items) VALUES (?,?,?,?)",
                        (page, time.time(), json.dumps(queries), items))
        self.db.commit()

    def last_escalation(self, page: str) -> dict | None:
        r = self.db.execute("SELECT * FROM escalations WHERE page=? ORDER BY run_at DESC LIMIT 1",
                            (page,)).fetchone()
        return dict(r) if r else None

    def prune(self, item_days: float = 3) -> None:
        """Keep the database small enough to carry between hourly runs (state tables: KEEP)."""
        now = time.time()
        cut = now - item_days * 86400
        self.db.execute("DELETE FROM items WHERE COALESCE(created_at, collected_at) < ?", (cut,))
        self.db.execute("DELETE FROM page_items WHERE item_id NOT IN (SELECT id FROM items)")
        self.db.execute("DELETE FROM overlay WHERE item_id NOT IN (SELECT id FROM items)")
        for t, (col, days) in KEEP.items():
            self.db.execute(f"DELETE FROM {t} WHERE {col} < ?", (now - days * 86400,))
        self.db.commit()
        self.db.execute("VACUUM")

    def commit(self) -> None:
        self.db.commit()
