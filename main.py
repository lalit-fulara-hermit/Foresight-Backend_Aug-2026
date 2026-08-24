"""Foresight backend, IEC Edition. Option D: thin, real, on-demand.

Endpoints:
  GET  /health              liveness check
  POST /fetch               pull fresh items from sources, form draft signals,
                            run reasoning on each. On-demand only, no schedule.
  GET  /signals?status=     draft -> review queue, published -> reader page
  POST /review              approve or reject a signal, with optional edits
  GET  /signals/{id}/trail  raw items + decisions behind one signal
  GET  /config              the live classification prompt and rules (shown on screen)
  PUT  /config              the live-edit moment: change rules, re-run reasoning
  POST /signals/{id}/rerun  re-run reasoning on one signal after a config edit
  GET  /snapshot            full JSON export for the frontend cache layer

Auth: single shared key in X-Foresight-Key header. Set FORESIGHT_KEY env var
to enable; if unset (local dev), auth is off.
"""
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import store
import sources_all
import reasoning

app = FastAPI(title="Foresight Backend", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo scope; tighten in build phase
    allow_methods=["*"],
    allow_headers=["*"],
)

store.init()

DEFAULT_QUERY = os.environ.get("FORESIGHT_QUERY", "solid state battery")


def check_auth(request: Request):
    expected = os.environ.get("FORESIGHT_KEY", "")
    if expected and request.headers.get("X-Foresight-Key", "") != expected:
        raise HTTPException(401, "missing or wrong X-Foresight-Key header")


@app.get("/health")
def health():
    return {"ok": True, "counts": store.counts()}


class FetchBody(BaseModel):
    query: str | None = None
    sources: list[str] | None = None
    per_source: int = 6
    reason_now: bool = True


@app.post("/fetch")
def fetch(body: FetchBody, request: Request):
    check_auth(request)
    query = body.query or DEFAULT_QUERY

    items, per_source, errors = sources_all.fetch_all(
        query, per_source=body.per_source, only=body.sources
    )
    new_count = store.add_raw_items(items)

    # Form: one new raw item -> one draft signal (simple and honest at demo scale)
    formed = []
    for it in store.raw_items_without_signal():
        sid = store.add_signal(
            title=it["title"],
            summary=it.get("summary", ""),
            source=it["source"],
            source_url=it.get("url", ""),
            raw_item_ids=[it["id"]],
        )
        formed.append(sid)

    # Reason: call Claude per new draft. Failures recorded per signal, not fatal.
    reasoned, reason_errors = 0, {}
    if body.reason_now:
        cfg = reasoning.load_config()
        for sid in formed:
            sig = store.get_signal(sid)
            try:
                res = reasoning.reason(sig, cfg)
                store.set_reasoning(sid, res)
                reasoned += 1
            except Exception as e:
                reason_errors[sid] = str(e)[:200]

    return {
        "query": query,
        "fetched": {"total_items": len(items), "per_source": per_source,
                    "source_errors": errors},
        "new_raw_items": new_count,
        "new_signals": len(formed),
        "reasoned": reasoned,
        "reasoning_errors": reason_errors,
        "counts": store.counts(),
    }


@app.get("/signals")
def signals(status: str = "draft"):
    if status not in ("draft", "published", "rejected"):
        raise HTTPException(400, "status must be draft, published, or rejected")
    return {"status": status, "signals": store.get_signals(status)}


class ReviewBody(BaseModel):
    signal_id: str
    decision: str  # approve | reject
    reviewer: str
    edits: dict | None = None


@app.post("/review")
def review(body: ReviewBody, request: Request):
    check_auth(request)
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    sig = store.review(body.signal_id, body.decision, body.reviewer, body.edits)
    if not sig:
        raise HTTPException(404, "signal not found")
    return {"signal": sig}


@app.get("/signals/{signal_id}/trail")
def trail(signal_id: str):
    sig = store.get_signal(signal_id)
    if not sig:
        raise HTTPException(404, "signal not found")
    return {
        "signal": sig,
        "decisions": store.decisions_for(signal_id),
    }


@app.get("/config")
def get_config():
    return reasoning.load_config()


@app.put("/config")
async def put_config(request: Request):
    check_auth(request)
    cfg = await request.json()
    for key in ("model", "classification_rules", "prompt_template"):
        if key not in cfg:
            raise HTTPException(400, f"config missing key: {key}")
    reasoning.save_config(cfg)
    return {"saved": True, "config": cfg}


@app.post("/signals/{signal_id}/rerun")
def rerun(signal_id: str, request: Request):
    check_auth(request)
    sig = store.get_signal(signal_id)
    if not sig:
        raise HTTPException(404, "signal not found")
    res = reasoning.reason(sig)
    store.set_reasoning(signal_id, res)
    return {"signal": store.get_signal(signal_id)}


@app.get("/snapshot")
def snapshot():
    return store.snapshot()
