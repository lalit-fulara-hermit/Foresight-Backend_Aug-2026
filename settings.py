"""Foresight v2: settings. Every threshold lives here, editable via the API,
versioned on each save. These are the numbers agreed in the gate walkthrough."""
import json
import os
import time

PATH = "settings.json"

DEFAULTS = {
    "windows_days": {"papers_news": 90, "regulations": 365, "eu_projects": 730},
    "dup_title_sim": 0.9,        # Gate 2: titles this similar = same item
    "reject_no_conf": 0.8,       # Gate 4: NO below this certainty still passes as borderline
    "evidence_sim": 0.4,         # Gate 5: topic-term overlap to count as related
    "prio_borderline_rel": 0.3,  # Gate 8: relevance score for borderline items
    "prio_min_fresh": 0.2,       # Gate 8: freshness floor (window edge / undated)
    "prio_corr_base": 0.4,       # Gate 8: corroboration score at zero evidence
    "prio_corr_step": 0.2,       # Gate 8: added per related item, capped at 1
    "version": 1,
    "updated_at": None,
    "updated_by": "default",
}

SOURCE_WINDOW = {  # which window applies to which source
    "arxiv": "papers_news", "crossref": "papers_news", "openalex": "papers_news",
    "gdelt": "papers_news", "ieee_spectrum": "papers_news",
    "iec_news": "papers_news", "iso_news": "papers_news",
    "federal_register": "regulations", "regulations_gov": "regulations",
    "cordis": "eu_projects",
}


def load():
    if os.path.exists(PATH):
        with open(PATH) as f:
            s = json.load(f)
        for k, v in DEFAULTS.items():
            s.setdefault(k, v)
        return s
    save(dict(DEFAULTS), "default")
    return dict(DEFAULTS)


def save(s, by):
    s["version"] = int(s.get("version", 0)) + 1
    s["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    s["updated_by"] = by
    with open(PATH, "w") as f:
        json.dump(s, f, indent=1)
    return s


def window_days(settings, source):
    return settings["windows_days"].get(SOURCE_WINDOW.get(source, "papers_news"), 90)
