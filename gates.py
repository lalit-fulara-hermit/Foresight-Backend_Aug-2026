"""Foresight v2: the pure-code gates. No AI here; same input, same output, always.
Gate numbers follow the agreed specification."""
import re
from datetime import datetime, timezone

STOP = set("the a an and or of for in on with to from by is are at as into over "
           "under this that these those its their our your his her be been was were "
           "will would can could may might".split())

SOURCE_FAMILY = {  # for evidence counting: same family counts once
    "arxiv": "research", "crossref": "research", "openalex": "research",
    "federal_register": "regulation", "regulations_gov": "regulation",
    "gdelt": "news", "ieee_spectrum": "standards_news",
    "iec_news": "standards_news", "iso_news": "standards_news",
    "cordis": "funding",
}


def tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) >= 4 and w not in STOP}


def jaccard(a, b):
    """Strict similarity for duplicate detection."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def overlap(a, b):
    """Relatedness for evidence counting: shared key terms as a share of the
    shorter item's terms. Two texts about the same subject score high even
    when one is much longer."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def parse_date(d):
    if not d:
        return None
    d = str(d).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y%m%dT%H%M%SZ", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(d[:len(fmt.replace('%Y','2000').replace('%m','01').replace('%d','01').replace('%H','00').replace('%M','00').replace('%S','00'))], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    try:  # e.g. "2026-08-24T10:24:20Z" partial
        return datetime.fromisoformat(d.replace("Z", "+00:00"))
    except Exception:
        return None


def age_days(published):
    dt = parse_date(published)
    if dt is None:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


# ---- Gate 1 · Scan window -------------------------------------------------
def gate1_window(item, window_days_for_source):
    """Return (passes, flag, reason)."""
    a = age_days(item.get("published"))
    if a is None:
        return True, "undated", "no publication date; kept, penalised in ordering"
    if a <= window_days_for_source:
        return True, None, f"age {a}d within {window_days_for_source}d window"
    return False, None, f"age {a}d exceeds {window_days_for_source}d window"


# ---- Gate 2 · Deduplication ----------------------------------------------
def gate2_duplicate(item, existing_ids, existing_titles, sim_threshold):
    """existing_ids: set of 'source:refid'. existing_titles: list of (id,title).
    Return (passes, reason_if_rejected, matched_id)."""
    rid = f"{item['source']}:{item['source_id']}"
    if rid in existing_ids:
        return False, "exact duplicate (same source reference)", rid
    for eid, t in existing_titles:
        if jaccard(item.get("title", ""), t) >= sim_threshold:
            return False, f"near-duplicate title (similarity >= {sim_threshold})", eid
    return True, None, None


# ---- Gate 3 · Junk --------------------------------------------------------
STUBS = ("title pending", "untitled", "no title")

def gate3_junk(item):
    t = (item.get("title") or "").strip()
    if len(t) < 8:
        return False, "title shorter than 8 characters"
    if t.lower().startswith(STUBS):
        return False, "placeholder title"
    if not re.search(r"[a-zA-Z]{4,}", t):
        return False, "no real word in title"
    return True, None


# ---- Gate 5 · Corroboration count ----------------------------------------
def gate5_evidence(item, stored_items, sim_threshold):
    """stored_items: list of dicts with id,title,summary,source.
    Returns (count, [related ids]), one per source family, self excluded."""
    my_text = (item.get("title", "") + " " + (item.get("summary") or ""))
    my_rid = f"{item['source']}:{item['source_id']}"
    best_per_family = {}
    for j in stored_items:
        if j["id"] == my_rid:
            continue
        s = overlap(my_text, j.get("title", "") + " " + (j.get("summary") or ""))
        if s >= sim_threshold:
            fam = SOURCE_FAMILY.get(j.get("source"), j.get("source"))
            if fam == SOURCE_FAMILY.get(item.get("source"), item.get("source")) and \
               j.get("source") == item.get("source") and s >= 0.9:
                continue  # near-duplicate from same source, not evidence
            if fam not in best_per_family or s > best_per_family[fam][0]:
                best_per_family[fam] = (s, j["id"])
    ids = [v[1] for v in best_per_family.values()]
    return len(ids), ids


# ---- Gate 6 enforcement (code check after AI) -----------------------------
def gate6_enforce(label, evidence_count):
    """Label can never be 'building' with zero evidence."""
    if label == "building" and evidence_count == 0:
        return "weak", True  # corrected
    return label, False


# ---- Gate 7 enforcement ---------------------------------------------------
def gate7_enforce(action_type, committee_key, horizon, register_keys):
    """Returns (action_type, committee_key, horizon, corrected_flag, note)."""
    corrected, note = False, None
    if action_type in ("refer", "propose"):
        if committee_key not in register_keys:
            action_type, corrected = "monitor", True
            note = "committee not in register; downgraded to monitor"
            if horizon not in ("3 months", "6 months", "12 months"):
                horizon = "6 months"
    if action_type == "monitor" and horizon not in ("3 months", "6 months", "12 months"):
        horizon, corrected = "6 months", True
        note = (note + "; " if note else "") + "missing horizon; set to 6 months"
    return action_type, committee_key, horizon, corrected, note


# ---- Gate 8 · Priority ----------------------------------------------------
def gate8_priority(relevance_verdict, relevance_conf, published, window_days_for_source,
                   evidence_count, cfg):
    R = relevance_conf if relevance_verdict == "YES" else cfg["prio_borderline_rel"]
    a = age_days(published)
    if a is None:
        F = cfg["prio_min_fresh"]
    else:
        F = max(cfg["prio_min_fresh"], 1 - a / max(1, window_days_for_source))
    K = min(1.0, cfg["prio_corr_base"] + cfg["prio_corr_step"] * evidence_count)
    return round(R * F * K, 4), round(R, 3), round(F, 3), round(K, 3)
