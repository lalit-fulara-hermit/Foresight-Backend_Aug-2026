"""End-to-end flow test with mocked source data and mocked AI response.

Verifies: ingest -> dedupe -> signal formation -> reasoning stored ->
review with edits -> publish -> trail -> snapshot. Run: python3 test_flow.py
"""
import os
import json

os.environ.pop("FORESIGHT_KEY", None)  # auth off for test

import store
import reasoning
from fastapi.testclient import TestClient
import main

client = TestClient(main.app)

FAKE_ITEMS = [
    {"source": "arxiv", "source_id": "2508.00001",
     "title": "Solid-state electrolyte breakthrough at room temperature",
     "summary": "We demonstrate a sulfide electrolyte with high conductivity.",
     "url": "https://arxiv.org/abs/2508.00001", "published": "2026-08-20"},
    {"source": "federal_register", "source_id": "2026-12345",
     "title": "Proposed rule on battery transport safety",
     "summary": "DOT proposes updated requirements for solid state batteries.",
     "url": "https://www.federalregister.gov/d/2026-12345", "published": "2026-08-21"},
    {"source": "arxiv", "source_id": "2508.00001",  # duplicate on purpose
     "title": "Solid-state electrolyte breakthrough at room temperature",
     "summary": "dup", "url": "x", "published": "2026-08-20"},
]

FAKE_AI = {"classification": "building",
           "action": "Refer to TC 21 for review of solid-state battery safety requirements.",
           "reasoning": "Two independent sources, one scientific and one regulatory. Momentum is visible."}


def run():
    if os.path.exists(store.DB_PATH):
        os.remove(store.DB_PATH)
    if os.path.exists(reasoning.CONFIG_PATH):
        os.remove(reasoning.CONFIG_PATH)
    store.init()

    # 1. Ingest with dedupe
    new = store.add_raw_items(FAKE_ITEMS)
    assert new == 2, f"expected 2 new items after dedupe, got {new}"

    # 2. Form signals
    formed = []
    for it in store.raw_items_without_signal():
        formed.append(store.add_signal(it["title"], it["summary"], it["source"],
                                       it["url"], [it["id"]]))
    assert len(formed) == 2, f"expected 2 signals, got {len(formed)}"

    # 3. Reasoning stored (mocked response; prompt building still real)
    cfg = reasoning.load_config()
    prompt = reasoning.build_prompt(store.get_signal(formed[0]), cfg)
    assert "Classify this signal" in prompt and "TC" in cfg["prompt_template"]
    for sid in formed:
        store.set_reasoning(sid, FAKE_AI["classification"], FAKE_AI["action"],
                            FAKE_AI["reasoning"], "mock-model")

    # 4. Review queue via API
    r = client.get("/signals?status=draft").json()
    assert len(r["signals"]) == 2
    assert r["signals"][0]["action"] == FAKE_AI["action"]

    # 5. Approve one with an edit
    sid = r["signals"][0]["id"]
    rev = client.post("/review", json={
        "signal_id": sid, "decision": "approve", "reviewer": "Lalit",
        "edits": {"action": "Refer to TC 21 and SC 21A jointly."},
    }).json()
    assert rev["signal"]["status"] == "published"
    assert rev["signal"]["action"].endswith("jointly.")

    # 6. Reject the other
    sid2 = r["signals"][1]["id"]
    client.post("/review", json={"signal_id": sid2, "decision": "reject",
                                 "reviewer": "Lalit"})

    # 7. Published page shows exactly one
    pub = client.get("/signals?status=published").json()
    assert len(pub["signals"]) == 1

    # 8. Trail has the decision with reviewer and edits
    trail = client.get(f"/signals/{sid}/trail").json()
    assert trail["decisions"][0]["reviewer"] == "Lalit"
    assert json.loads(trail["decisions"][0]["edits"])["action"]

    # 9. Config is served and editable
    cfg = client.get("/config").json()
    cfg["classification_rules"]["weak"] = "EDITED RULE"
    saved = client.put("/config", json=cfg).json()
    assert saved["config"]["classification_rules"]["weak"] == "EDITED RULE"

    # 10. Snapshot exports both queues
    snap = client.get("/snapshot").json()
    assert snap["counts"]["raw_items"] == 2
    assert len(snap["published"]) == 1

    print("ALL 10 CHECKS PASSED")
    print("counts:", store.counts())


if __name__ == "__main__":
    run()
