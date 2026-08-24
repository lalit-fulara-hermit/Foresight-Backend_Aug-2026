"""Foresight v2: the two AI calls.
Call 1 (Gate 4): relevance YES/NO + certainty + one reason.
Call 2 (Gates 6-7 proposal): synopsis, tags, label with cited condition, action.
Both instructions are stored, editable text. Code enforces the hard rules afterwards."""
import json
import os

import httpx

RULES_PATH = "rules.json"

DEFAULT_RULES = {
    "model": "claude-sonnet-4-6",
    "relevance_test": (
        "IEC covers electrical, electronic and related information technologies: "
        "their research, products, markets, safety, regulation, conformity "
        "assessment and standardization, across domains such as energy, grids, "
        "batteries, e-mobility, automation, semiconductors, medical devices, ICT "
        "and AI. Question: could this item plausibly matter to any of that work? "
        "If genuinely uncertain, answer YES."
    ),
    "labels": {
        "weak": "One item alone. No related evidence yet.",
        "building": "Two or more related items in our database, or one item where law or funding is moving.",
        "established": "A regulation in force, or standardization work already underway (the item's own text tells us; the full build checks IEC's live work programme).",
    },
    "version": 1,
}


def load_rules():
    if os.path.exists(RULES_PATH):
        with open(RULES_PATH) as f:
            r = json.load(f)
        for k, v in DEFAULT_RULES.items():
            r.setdefault(k, v)
        return r
    save_rules(dict(DEFAULT_RULES))
    return dict(DEFAULT_RULES)


def save_rules(r):
    r["version"] = int(r.get("version", 0)) + 1
    with open(RULES_PATH, "w") as f:
        json.dump(r, f, indent=1)
    return r


def _call(prompt, model, max_tokens=600):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", [])
                   if b.get("type") == "text")
    return json.loads(text.replace("```json", "").replace("```", "").strip())


def relevance(item, rules):
    """Gate 4. Returns dict: verdict YES|NO, confidence 0-1, reason."""
    p = (
        f"{rules['relevance_test']}\n\n"
        f"Item:\nTitle: {item['title']}\n"
        f"Summary: {item.get('summary') or '(none provided)'}\n"
        f"Source: {item['source']} · Date: {item.get('published') or 'unknown'}\n\n"
        "Respond ONLY with JSON, no fences:\n"
        '{"verdict": "YES|NO", "confidence": 0.0, "reason": "one plain sentence"}'
    )
    out = _call(p, rules["model"], 250)
    v = "YES" if str(out.get("verdict", "")).upper().startswith("Y") else "NO"
    c = max(0.0, min(1.0, float(out.get("confidence", 0.5))))
    return {"verdict": v, "confidence": c, "reason": str(out.get("reason", ""))[:300]}


def assess(item, evidence_count, evidence_titles, committee_register, rules):
    """Gates 6-7 proposal. Returns synopsis, lens, sector, committee, label with
    cited condition, action_type, action_text, horizon, reasoning."""
    reg_list = ", ".join(sorted(committee_register.values()))
    ev = "; ".join(evidence_titles[:5]) if evidence_titles else "none"
    p = (
        "You are the analysis engine of Foresight, a market intelligence tool for "
        "the IEC Market Strategy Board. Plain business English only. No jargon.\n\n"
        f"Item:\nTitle: {item['title']}\n"
        f"Summary: {item.get('summary') or '(none provided)'}\n"
        f"Source: {item['source']} · Date: {item.get('published') or 'unknown'}\n"
        f"Related evidence already in our database: {evidence_count} item(s): {ev}\n\n"
        "Label rules, apply in order, first match wins, and cite which condition fired:\n"
        f"- established: {rules['labels']['established']}\n"
        f"- building: {rules['labels']['building']} (evidence count above is the count)\n"
        f"- weak: {rules['labels']['weak']}\n\n"
        "Committee must be chosen from this official register ONLY (or 'none'):\n"
        f"{reg_list}\n\n"
        "Action: exactly one of refer, propose, liaise, monitor, discard.\n"
        "monitor requires a horizon of 3, 6 or 12 months.\n\n"
        "Respond ONLY with JSON, no fences:\n"
        '{"synopsis": "2-3 plain sentences: what does this source report",\n'
        ' "lens": "tech|market|reg|geo", "sector": "one word",\n'
        ' "committee": "exact name from register or none",\n'
        ' "label": "weak|building|established", "label_basis": "which condition fired and why",\n'
        ' "action_type": "refer|propose|liaise|monitor|discard",\n'
        ' "action_text": "one sentence under 15 words",\n'
        ' "horizon": "3 months|6 months|12 months|none",\n'
        ' "reasoning": "two short sentences"}'
    )
    out = _call(p, rules["model"], 700)
    return {
        "synopsis": str(out.get("synopsis", ""))[:600],
        "lens": out.get("lens", "tech"),
        "sector": str(out.get("sector", ""))[:30],
        "committee": str(out.get("committee", "none"))[:60],
        "label": out.get("label", "weak"),
        "label_basis": str(out.get("label_basis", ""))[:300],
        "action_type": str(out.get("action_type", "monitor")).lower(),
        "action_text": str(out.get("action_text", ""))[:200],
        "horizon": out.get("horizon", "none"),
        "reasoning": str(out.get("reasoning", ""))[:500],
        "model": rules["model"],
    }


def committee_key(name, register):
    """Map an AI-returned committee name to a register key, or None.
    Match on the committee code (e.g. 'TC 21', 'SC 62A'), never on substrings,
    so 'TC 21' can never match 'TC 2'."""
    import re as _re
    if not name or name.lower() == "none":
        return None

    def code_of(text):
        m = _re.search(r"\b(jtc\s*1\s*/?\s*sc\s*42|syc\s*[a-z]+|tc\s*\d+[a-z]?|sc\s*\d+[a-z]?)\b",
                       text.lower())
        return _re.sub(r"[\s/]+", "", m.group(1)) if m else None

    target = code_of(name)
    if not target:
        return None
    for k, label in register.items():
        if code_of(label) == target:
            return k
    return None


def fetch_page_summary(url, rules):
    """Gate 4b helper: when a source sends no summary, read the page and write one."""
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Foresight-Prototype/1.0"})
        r.raise_for_status()
        import re as _re
        text = _re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<[^>]+>", " ", r.text)
        text = _re.sub(r"\s+", " ", text)[:6000]
        out = _call(
            "Summarise what this web page reports, in 2-3 plain sentences. "
            "Respond ONLY with JSON, no fences: {\"summary\": \"...\"}\n\nPage text:\n" + text,
            rules["model"], 300)
        return str(out.get("summary", ""))[:600]
    except Exception:
        return ""
