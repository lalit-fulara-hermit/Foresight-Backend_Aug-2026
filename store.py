"""Foresight backend: data store. SQLite, single file, portable across hosts."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

DB_PATH = "foresight.db"


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_items (
                id TEXT PRIMARY KEY,          -- source:source_id
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                url TEXT,
                published TEXT,
                fetched_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT,
                source TEXT NOT NULL,
                source_url TEXT,
                raw_item_ids TEXT NOT NULL,   -- JSON list
                status TEXT NOT NULL DEFAULT 'draft',  -- draft|published|rejected
                classification TEXT,          -- weak|building|established
                lens TEXT,                    -- tech|market|reg|geo
                committee TEXT,               -- e.g. TC 65, or none
                finding TEXT,                 -- one-sentence summary of the source
                horizon TEXT,                 -- revisit timeframe
                action TEXT,                  -- suggested IEC action
                reasoning TEXT,               -- verbatim AI reasoning
                model TEXT,                   -- which model produced it
                created_at TEXT NOT NULL,
                reasoned_at TEXT
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL,
                decision TEXT NOT NULL,       -- approve|reject
                reviewer TEXT NOT NULL,
                edits TEXT,                   -- JSON of edited fields, if any
                decided_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
            """
        )


def add_raw_items(items):
    """Insert raw items, skipping duplicates. Returns count of new items."""
    new = 0
    with db() as conn:
        for it in items:
            rid = f"{it['source']}:{it['source_id']}"
            cur = conn.execute("SELECT 1 FROM raw_items WHERE id=?", (rid,))
            if cur.fetchone():
                continue
            conn.execute(
                "INSERT INTO raw_items (id, source, title, summary, url, published, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (rid, it["source"], it["title"], it.get("summary", ""),
                 it.get("url", ""), it.get("published", ""), now_iso()),
            )
            new += 1
    return new


def raw_items_without_signal():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT r.* FROM raw_items r
            WHERE NOT EXISTS (
                SELECT 1 FROM signals s WHERE s.raw_item_ids LIKE '%' || r.id || '%'
            )
            ORDER BY r.fetched_at
            """
        ).fetchall()
    return [dict(r) for r in rows]


def add_signal(title, summary, source, source_url, raw_item_ids):
    sid = "sig_" + uuid.uuid4().hex[:10]
    with db() as conn:
        conn.execute(
            "INSERT INTO signals (id, title, summary, source, source_url, raw_item_ids, "
            "status, created_at) VALUES (?,?,?,?,?,?,'draft',?)",
            (sid, title, summary, source, source_url,
             json.dumps(raw_item_ids), now_iso()),
        )
    return sid


def set_reasoning(signal_id, res):
    with db() as conn:
        conn.execute(
            "UPDATE signals SET classification=?, lens=?, committee=?, finding=?, "
            "horizon=?, action=?, reasoning=?, model=?, reasoned_at=? WHERE id=?",
            (res.get("classification"), res.get("lens"), res.get("committee"),
             res.get("finding"), res.get("horizon"), res.get("action"),
             res.get("reasoning"), res.get("model"), now_iso(), signal_id),
        )


def get_signals(status=None, limit=200):
    q = "SELECT * FROM signals"
    args = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        rows = conn.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["raw_item_ids"] = json.loads(d["raw_item_ids"])
        out.append(d)
    return out


def get_signal(signal_id):
    with db() as conn:
        r = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["raw_item_ids"] = json.loads(d["raw_item_ids"])
    return d


def review(signal_id, decision, reviewer, edits=None):
    """decision: approve|reject. edits: dict of fields to update before publish."""
    sig = get_signal(signal_id)
    if not sig:
        return None
    with db() as conn:
        if edits:
            allowed = {"title", "summary", "classification", "action", "horizon", "committee", "lens"}
            for k, v in edits.items():
                if k in allowed:
                    conn.execute(f"UPDATE signals SET {k}=? WHERE id=?", (v, signal_id))
        new_status = "published" if decision == "approve" else "rejected"
        conn.execute("UPDATE signals SET status=? WHERE id=?", (new_status, signal_id))
        did = "dec_" + uuid.uuid4().hex[:10]
        conn.execute(
            "INSERT INTO decisions (id, signal_id, decision, reviewer, edits, decided_at) "
            "VALUES (?,?,?,?,?,?)",
            (did, signal_id, decision, reviewer,
             json.dumps(edits or {}), now_iso()),
        )
    return get_signal(signal_id)


def decisions_for(signal_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE signal_id=? ORDER BY decided_at",
            (signal_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def counts():
    with db() as conn:
        raw = conn.execute("SELECT COUNT(*) c FROM raw_items").fetchone()["c"]
        by_status = {
            r["status"]: r["c"]
            for r in conn.execute(
                "SELECT status, COUNT(*) c FROM signals GROUP BY status"
            ).fetchall()
        }
    return {"raw_items": raw, "signals": by_status}


def snapshot():
    """Full export for the frontend cache layer."""
    return {
        "generated_at": now_iso(),
        "counts": counts(),
        "draft": get_signals("draft"),
        "published": get_signals("published"),
    }
