"""Foresight v2 API. The nine-gate funnel, orchestrated.

POST /fetch        run the funnel on a topic; returns per-gate counts
GET  /items?status=draft|published|rejected
GET  /dashboard    published items with work status and owner
POST /review       approve/reject with edits; logs machine-vs-human delta
POST /action_status  update dashboard row status/owner
GET/PUT /settings  thresholds and date windows (Gate 1, 2, 4, 5, 8 numbers)
GET/PUT /rules     relevance test and label rules (the editable panel)
GET  /committees   the official register
GET  /accuracy     agreement rate from the decision log
GET  /health, /snapshot
"""
import json
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import store2 as store
import gates
import settings as settings_mod
import reasoning2 as ai
import sources_all

app = FastAPI(title="Foresight v2", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
store.init()

REGISTER = json.load(open("committees.json"))


def auth(req: Request):
    k = os.environ.get("FORESIGHT_KEY", "")
    if k and req.headers.get("X-Foresight-Key", "") != k:
        raise HTTPException(401, "missing or wrong X-Foresight-Key")


@app.get("/health")
def health():
    return {"ok": True, "counts": store.counts(), "version": 2}


class FetchBody(BaseModel):
    query: str
    per_source: int = 5
    sources: list[str] | None = None


@app.post("/fetch")
def fetch(body: FetchBody, req: Request):
    auth(req)
    cfg = settings_mod.load()
    rules = ai.load_rules()
    raw, per_source, src_errors = sources_all.fetch_all(
        body.query, per_source=body.per_source, only=body.sources)

    funnel = {"scanned": len(raw), "g1_window": 0, "g2_duplicate": 0,
              "g3_junk": 0, "g4_irrelevant": 0, "drafts": 0, "errors": 0}
    ids = store.existing_ids()
    titles = store.existing_titles()

    survivors = []
    for it in raw:
        rid = f"{it['source']}:{it['source_id']}"
        # Gate 1
        w = settings_mod.window_days(cfg, it["source"])
        ok, flag, reason = gates.gate1_window(it, w)
        if not ok:
            store.add(it, status="rejected", gate=1, gate_reason=reason,
                      run_topic=body.query, flags=[])
            funnel["g1_window"] += 1
            ids.add(rid)
            continue
        flags = [flag] if flag else []
        # Gate 2
        ok, reason, _m = gates.gate2_duplicate(it, ids, titles, cfg["dup_title_sim"])
        if not ok:
            funnel["g2_duplicate"] += 1  # duplicates are not stored again
            continue
        # Gate 3
        ok, reason = gates.gate3_junk(it)
        if not ok:
            store.add(it, status="rejected", gate=3, gate_reason=reason,
                      run_topic=body.query, flags=flags)
            funnel["g3_junk"] += 1
            ids.add(rid)
            continue
        survivors.append((it, flags))
        ids.add(rid)
        titles.append((rid, it.get("title", "")))

    # Gate 4 + 4b + 5 + 6/7 proposal, per survivor
    stored = store.stored_for_evidence()
    for it, flags in survivors:
        rid = f"{it['source']}:{it['source_id']}"
        try:
            rel = ai.relevance(it, rules)
        except Exception as e:
            store.add(it, status="rejected", gate=4,
                      gate_reason=f"relevance call failed: {e}", run_topic=body.query,
                      flags=flags)
            funnel["errors"] += 1
            continue
        if rel["verdict"] == "NO" and rel["confidence"] >= cfg["reject_no_conf"]:
            store.add(it, status="rejected", gate=4, gate_reason=rel["reason"],
                      run_topic=body.query, flags=flags,
                      rel_verdict=rel["verdict"], rel_conf=rel["confidence"],
                      rel_reason=rel["reason"])
            funnel["g4_irrelevant"] += 1
            continue
        if rel["verdict"] == "NO":
            flags = flags + ["borderline"]

        # Gate 4b: synopsis when the source sent nothing
        if not (it.get("summary") or "").strip() and it.get("url"):
            it["summary"] = ai.fetch_page_summary(it["url"], rules)

        # Gate 5
        k, ev_ids = gates.gate5_evidence(it, stored, cfg["evidence_sim"])
        ev_titles = [s["title"] for s in stored if s["id"] in ev_ids]

        # Gates 6-7 proposal + enforcement
        try:
            a = ai.assess(it, k, ev_titles, REGISTER, rules)
        except Exception as e:
            store.add(it, status="rejected", gate=6,
                      gate_reason=f"assessment call failed: {e}", run_topic=body.query,
                      flags=flags, rel_verdict=rel["verdict"],
                      rel_conf=rel["confidence"], rel_reason=rel["reason"])
            funnel["errors"] += 1
            continue
        label, corrected6 = gates.gate6_enforce(a["label"], k)
        ck = ai.committee_key(a["committee"], REGISTER)
        atype, ck, hz, corrected7, note7 = gates.gate7_enforce(
            a["action_type"], ck, a["horizon"], set(REGISTER))
        if corrected6:
            flags = flags + ["label corrected: building needs evidence"]
        if corrected7 and note7:
            flags = flags + [note7]

        # Gate 8
        w = settings_mod.window_days(cfg, it["source"])
        prio, R, F, K = gates.gate8_priority(
            rel["verdict"], rel["confidence"], it.get("published"), w, k, cfg)

        store.add(it, status="draft", run_topic=body.query, flags=flags,
                  rel_verdict=rel["verdict"], rel_conf=rel["confidence"],
                  rel_reason=rel["reason"], evidence_count=k, evidence_ids=ev_ids,
                  synopsis=a["synopsis"], lens=a["lens"], sector=a["sector"],
                  committee_key=ck or "", committee_label=REGISTER.get(ck, "") if ck else "",
                  label=label, label_basis=a["label_basis"],
                  action_type=atype, action_text=a["action_text"], horizon=hz,
                  reasoning=a["reasoning"], model=a["model"], reasoned_at=store.now(),
                  prio=prio, prio_rel=R, prio_fresh=F, prio_corr=K)
        stored.append({"id": rid, "title": it.get("title", ""),
                       "summary": it.get("summary", ""), "source": it["source"]})
        funnel["drafts"] += 1

    return {"query": body.query, "funnel": funnel, "per_source": per_source,
            "source_errors": src_errors, "counts": store.counts()}


@app.get("/items")
def items(status: str = "draft"):
    return {"status": status, "items": store.list_by_status(status)}


@app.get("/dashboard")
def dashboard():
    return {"rows": store.dashboard()}


class ReviewBody(BaseModel):
    item_id: str
    decision: str
    reviewer: str
    edits: dict | None = None
    reason_category: str = ""
    reason_note: str = ""


@app.post("/review")
def review(body: ReviewBody, req: Request):
    auth(req)
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    if not body.reviewer.strip():
        raise HTTPException(400, "reviewer name required; decisions are logged")
    if body.decision == "reject" and not body.reason_category:
        raise HTTPException(400, "rejection reason required (learning loop)")
    out = store.review(body.item_id, body.decision, body.reviewer, body.edits,
                       body.reason_category, body.reason_note)
    if not out:
        raise HTTPException(404, "item not found or not a draft")
    return {"item": out}


class StatusBody(BaseModel):
    item_id: str
    status: str
    owner: str = ""


@app.post("/action_status")
def action_status(body: StatusBody, req: Request):
    auth(req)
    if body.status not in ("new", "in progress", "done", "scheduled"):
        raise HTTPException(400, "bad status")
    store.set_action_status(body.item_id, body.status, body.owner)
    return {"ok": True}


@app.get("/settings")
def get_settings():
    return settings_mod.load()


@app.put("/settings")
async def put_settings(req: Request):
    auth(req)
    body = await req.json()
    s = settings_mod.load()
    for k in ("windows_days", "dup_title_sim", "reject_no_conf", "evidence_sim",
              "prio_borderline_rel", "prio_min_fresh", "prio_corr_base",
              "prio_corr_step"):
        if k in body:
            s[k] = body[k]
    return settings_mod.save(s, body.get("by", "user"))


@app.get("/rules")
def get_rules():
    return ai.load_rules()


@app.put("/rules")
async def put_rules(req: Request):
    auth(req)
    body = await req.json()
    r = ai.load_rules()
    for k in ("relevance_test", "labels", "model"):
        if k in body:
            r[k] = body[k]
    return ai.save_rules(r)


@app.post("/items/{item_id}/rerun")
def rerun(item_id: str, req: Request):
    auth(req)
    it = store.get(item_id)
    if not it:
        raise HTTPException(404, "not found")
    rules = ai.load_rules()
    cfg = settings_mod.load()
    stored = store.stored_for_evidence()
    k, ev_ids = gates.gate5_evidence(
        {"source": it["source"], "source_id": it["id"].split(":", 1)[1],
         "title": it["title"], "summary": it["summary"]}, stored, cfg["evidence_sim"])
    ev_titles = [s["title"] for s in stored if s["id"] in ev_ids]
    a = ai.assess({"title": it["title"], "summary": it["summary"],
                   "source": it["source"], "published": it["published"]},
                  k, ev_titles, REGISTER, rules)
    label, _ = gates.gate6_enforce(a["label"], k)
    ck = ai.committee_key(a["committee"], REGISTER)
    atype, ck, hz, _, _ = gates.gate7_enforce(a["action_type"], ck, a["horizon"],
                                              set(REGISTER))
    store.update(item_id, evidence_count=k, evidence_ids=ev_ids,
                 synopsis=a["synopsis"], label=label, label_basis=a["label_basis"],
                 committee_key=ck or "", committee_label=REGISTER.get(ck, "") if ck else "",
                 action_type=atype, action_text=a["action_text"], horizon=hz,
                 reasoning=a["reasoning"], reasoned_at=store.now())
    return {"item": store.get(item_id)}


class AnnotateBody(BaseModel):
    item_id: str
    reviewer: str
    human_summary: str | None = None
    human_note: str | None = None


@app.post("/annotate")
def annotate(body: AnnotateBody, req: Request):
    auth(req)
    if not body.reviewer.strip():
        raise HTTPException(400, "reviewer name required")
    out = store.annotate(body.item_id, body.reviewer, body.human_summary,
                         body.human_note)
    if not out:
        raise HTTPException(404, "not found")
    return {"item": out}


class RestoreBody(BaseModel):
    item_id: str
    reviewer: str
    corrections: dict | None = None


@app.post("/restore")
def restore(body: RestoreBody, req: Request):
    auth(req)
    out = store.restore(body.item_id, body.reviewer, body.corrections)
    if not out:
        raise HTTPException(404, "not a rejected item")
    return {"item": out}


class DeleteBody(BaseModel):
    item_id: str
    reviewer: str


@app.post("/delete")
def delete(body: DeleteBody, req: Request):
    auth(req)
    if not store.delete_item(body.item_id, body.reviewer):
        raise HTTPException(404, "not a rejected item")
    return {"ok": True}


class AskBody(BaseModel):
    question: str


@app.post("/ask")
def ask(body: AskBody, req: Request):
    auth(req)
    rules = ai.load_rules()
    ctx = {
        "drafts": [{k: d.get(k) for k in ("title", "label", "committee_label",
                    "action_type", "action_text", "synopsis", "rel_reason")}
                   for d in store.list_by_status("draft", 40)],
        "published": [{k: d.get(k) for k in ("title", "label", "committee_label",
                       "action_type", "action_text", "human_summary", "human_note")}
                      for d in store.list_by_status("published", 40)],
        "rejected": [{k: d.get(k) for k in ("title", "gate", "gate_reason")}
                     for d in store.list_by_status("rejected", 40)],
        "accuracy": store.accuracy(),
        "rules": {"relevance_test": rules["relevance_test"],
                  "labels": rules["labels"]},
    }
    prompt = (
        "You are the workspace assistant of Foresight, a market intelligence "
        "tool for the IEC Market Strategy Board. Answer ONLY from the workspace "
        "data below. Plain business English, short answers. If the data does "
        "not contain the answer, say so plainly.\n\nWorkspace data:\n"
        + json.dumps(ctx) + "\n\nQuestion: " + body.question[:500]
        + "\n\nRespond ONLY with JSON, no fences: {\"answer\": \"...\"}"
    )
    try:
        out = ai._call(prompt, rules["model"], 500)
        return {"answer": str(out.get("answer", ""))[:1500]}
    except Exception as e:
        raise HTTPException(502, f"assistant unavailable: {e}")


@app.get("/committees")
def committees():
    return REGISTER


@app.get("/accuracy")
def acc():
    return store.accuracy()


@app.get("/snapshot")
def snapshot():
    return {"generated_at": store.now(), "counts": store.counts(),
            "draft": store.list_by_status("draft"),
            "published": store.list_by_status("published"),
            "settings": settings_mod.load(), "rules": ai.load_rules()}
