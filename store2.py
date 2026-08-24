"""Foresight v2: store. Nothing is deleted; every item carries its gate history."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

DB = "foresight2.db"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextmanager
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,             -- source:refid
            source TEXT, title TEXT, summary TEXT, url TEXT, published TEXT,
            fetched_at TEXT, run_topic TEXT,
            status TEXT,                     -- rejected | draft | published | discarded
            gate INTEGER,                    -- gate that rejected it (if rejected)
            gate_reason TEXT,
            flags TEXT,                      -- JSON list: undated, borderline, corrected...
            rel_verdict TEXT, rel_conf REAL, rel_reason TEXT,
            evidence_count INTEGER, evidence_ids TEXT,
            synopsis TEXT, lens TEXT, sector TEXT,
            committee_key TEXT, committee_label TEXT,
            label TEXT, label_basis TEXT,
            action_type TEXT, action_text TEXT, horizon TEXT,
            reasoning TEXT, model TEXT, reasoned_at TEXT,
            human_summary TEXT, human_note TEXT, annotated_by TEXT, annotated_at TEXT,
            prio REAL, prio_rel REAL, prio_fresh REAL, prio_corr REAL
        );
        CREATE TABLE IF NOT EXISTS decisions (
            id TEXT PRIMARY KEY, item_id TEXT, decision TEXT, reviewer TEXT,
            machine_proposal TEXT, human_final TEXT, changed_fields TEXT,
            reason_category TEXT,            -- reject feedback for the learning loop
            reason_note TEXT,
            decided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS action_status (
            item_id TEXT PRIMARY KEY, status TEXT, owner TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_status ON items(status);
        """)


def existing_ids():
    with db() as c:
        return {r["id"] for r in c.execute("SELECT id FROM items")}


def existing_titles():
    with db() as c:
        return [(r["id"], r["title"] or "") for r in
                c.execute("SELECT id,title FROM items WHERE status!='rejected'")]


def stored_for_evidence():
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT id,title,summary,source FROM items WHERE status IN ('draft','published')")]


def add(item, **fields):
    rid = f"{item['source']}:{item['source_id']}"
    cols = {"id": rid, "source": item["source"], "title": item.get("title", ""),
            "summary": item.get("summary", ""), "url": item.get("url", ""),
            "published": item.get("published", ""), "fetched_at": now()}
    cols.update(fields)
    for k in ("flags", "evidence_ids"):
        if k in cols and not isinstance(cols[k], str):
            cols[k] = json.dumps(cols[k])
    keys = ",".join(cols)
    q = f"INSERT OR REPLACE INTO items ({keys}) VALUES ({','.join('?'*len(cols))})"
    with db() as c:
        c.execute(q, list(cols.values()))
    return rid


def update(item_id, **fields):
    for k in ("flags", "evidence_ids"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    sets = ",".join(f"{k}=?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE items SET {sets} WHERE id=?", list(fields.values()) + [item_id])


def get(item_id):
    with db() as c:
        r = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return dict(r) if r else None


def list_by_status(status, limit=300):
    order = "prio DESC, fetched_at DESC" if status == "draft" else "fetched_at DESC"
    with db() as c:
        rows = c.execute(f"SELECT * FROM items WHERE status=? ORDER BY {order} LIMIT ?",
                         (status, limit)).fetchall()
    return [dict(r) for r in rows]


def review(item_id, decision, reviewer, edits, reason_category="", reason_note=""):
    it = get(item_id)
    if not it or it["status"] != "draft":
        return None
    machine = {k: it[k] for k in ("action_type", "action_text", "committee_key",
                                  "committee_label", "horizon", "label")}
    allowed = {"action_type", "action_text", "committee_key", "committee_label",
               "horizon", "label", "human_summary", "human_note"}
    changed = []
    for k, v in (edits or {}).items():
        if k in allowed and v is not None and v != it.get(k):
            update(item_id, **{k: v})
            changed.append(k)
    new_status = "published" if decision == "approve" else "rejected"
    if decision == "reject":
        update(item_id, status="rejected", gate=9, gate_reason=f"rejected by {reviewer}")
    else:
        update(item_id, status="published")
    it2 = get(item_id)
    human = {k: it2[k] for k in machine}
    if changed and any(k in ("human_summary", "human_note") for k in changed):
        update(item_id, annotated_by=reviewer, annotated_at=now())
    did = "dec_" + uuid.uuid4().hex[:10]
    with db() as c:
        c.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (did, item_id, decision, reviewer, json.dumps(machine),
                   json.dumps(human), json.dumps(changed),
                   reason_category or "", reason_note or "", now()))
        if decision == "approve":
            st = "scheduled" if it2["action_type"] == "monitor" else "new"
            c.execute("INSERT OR REPLACE INTO action_status VALUES (?,?,?,?)",
                      (item_id, st, "", now()))
    return it2


def annotate(item_id, reviewer, human_summary=None, human_note=None):
    fields = {"annotated_by": reviewer, "annotated_at": now()}
    if human_summary is not None:
        fields["human_summary"] = human_summary
    if human_note is not None:
        fields["human_note"] = human_note
    update(item_id, **fields)
    return get(item_id)


def restore(item_id, reviewer, corrections=None):
    it = get(item_id)
    if not it or it["status"] != "rejected":
        return None
    allowed = {"action_type", "action_text", "committee_key", "committee_label",
               "horizon", "label", "title", "summary"}
    for k, v in (corrections or {}).items():
        if k in allowed and v is not None:
            update(item_id, **{k: v})
    update(item_id, status="draft", gate=None,
           gate_reason=f"restored by {reviewer}")
    did = "dec_" + uuid.uuid4().hex[:10]
    with db() as c:
        c.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (did, item_id, "restore", reviewer, "{}", "{}",
                   json.dumps(list((corrections or {}).keys())), "", "", now()))
    return get(item_id)


def delete_item(item_id, reviewer):
    it = get(item_id)
    if not it or it["status"] != "rejected":
        return None
    update(item_id, status="deleted", title="[deleted]", summary="", url="",
           synopsis="", gate_reason=f"deleted by {reviewer} (stub kept for audit)")
    did = "dec_" + uuid.uuid4().hex[:10]
    with db() as c:
        c.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (did, item_id, "delete", reviewer, "{}", "{}", "[]", "", "", now()))
    return True


def set_action_status(item_id, status, owner):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO action_status VALUES (?,?,?,?)",
                  (item_id, status, owner or "", now()))


def dashboard():
    with db() as c:
        rows = c.execute("""
            SELECT i.*, a.status AS work_status, a.owner
            FROM items i LEFT JOIN action_status a ON a.item_id=i.id
            WHERE i.status='published' ORDER BY i.action_type, i.prio DESC""").fetchall()
    return [dict(r) for r in rows]


def accuracy():
    with db() as c:
        rows = c.execute("SELECT decision, changed_fields FROM decisions").fetchall()
    total = len(rows)
    unchanged_approvals = sum(1 for r in rows
                              if r["decision"] == "approve" and json.loads(r["changed_fields"]) == [])
    return {"decisions": total,
            "approved_unchanged": unchanged_approvals,
            "agreement_rate": round(unchanged_approvals / total, 2) if total else None}


def counts():
    with db() as c:
        by = {r["status"]: r["c"] for r in
              c.execute("SELECT status, COUNT(*) c FROM items GROUP BY status")}
    return by
