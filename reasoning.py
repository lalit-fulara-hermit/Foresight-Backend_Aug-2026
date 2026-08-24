"""Foresight backend: reasoning step.

One Claude API call per draft signal. Returns classification, suggested IEC
action, and reasoning. The prompt lives in config.json and is served to the
frontend verbatim: what the audience sees on screen is exactly what runs.
"""
import json
import os

import httpx

CONFIG_PATH = "config.json"

DEFAULT_CONFIG = {
    "model": "claude-sonnet-4-6",
    "classification_rules": {
        "weak": "Early evidence. Single source or first appearance. Direction unclear.",
        "building": "Multiple independent sources or clear momentum. Direction visible.",
        "established": "Widely reported, regulated, or standardized activity already underway.",
    },
    "prompt_template": (
        "You are the analysis engine of Foresight, a market and technology "
        "intelligence tool for the IEC (International Electrotechnical Commission) "
        "Market Strategy Board.\n\n"
        "A new item has been ingested:\n"
        "Title: {title}\n"
        "Summary: {summary}\n"
        "Source: {source}\n"
        "Date: {published}\n\n"
        "Classify this signal using exactly these rules:\n"
        "- weak: {rule_weak}\n"
        "- building: {rule_building}\n"
        "- established: {rule_established}\n\n"
        "Then extract these fields. Write in plain business English, no jargon.\n"
        "- lens: which of the four lenses this signal belongs to: "
        "tech (technology), market, reg (regulatory), geo (geopolitical).\n"
        "- committee: the single most relevant IEC committee, e.g. 'TC 65', "
        "'SC 62A', 'SyC AI'. If none fits, write 'none'.\n"
        "- finding: one plain sentence: what does the source actually report?\n"
        "- horizon: when to revisit: '3 months', '6 months', '12 months', or 'none' "
        "if immediate referral is warranted.\n"
        "- action: one short sentence, under 15 words, the suggested IEC step.\n"
        "- reasoning: two short sentences explaining the label and the action.\n\n"
        "Respond ONLY with JSON, no markdown fences:\n"
        '{{"classification": "weak|building|established", "lens": "tech|market|reg|geo", '
        '"committee": "...", "finding": "...", "horizon": "...", '
        '"action": "...", "reasoning": "..."}}'
    ),
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def build_prompt(signal, cfg):
    rules = cfg["classification_rules"]
    return cfg["prompt_template"].format(
        title=signal["title"],
        summary=signal.get("summary") or "(no summary available)",
        source=signal["source"],
        published=signal.get("published") or "unknown",
        rule_weak=rules["weak"],
        rule_building=rules["building"],
        rule_established=rules["established"],
    )


def reason(signal, cfg=None):
    """Call the Claude API. Returns dict with classification, action, reasoning, model.
    Raises on failure: the caller decides how to surface it."""
    cfg = cfg or load_config()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    prompt = build_prompt(signal, cfg)
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": cfg["model"],
            "max_tokens": 500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    r.raise_for_status()
    text = "".join(
        b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"
    )
    clean = text.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(clean)
    return {
        "classification": parsed.get("classification", "weak"),
        "lens": parsed.get("lens", ""),
        "committee": parsed.get("committee", ""),
        "finding": parsed.get("finding", ""),
        "horizon": parsed.get("horizon", ""),
        "action": parsed.get("action", ""),
        "reasoning": parsed.get("reasoning", ""),
        "model": cfg["model"],
    }
