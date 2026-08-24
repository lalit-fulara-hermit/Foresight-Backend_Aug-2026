"""Foresight backend: source adapters.

Eight sources, all usable immediately. Only Regulations.gov needs a key
(instant, from api.data.gov). Each adapter returns a list of dicts:
{source, source_id, title, summary, url, published}

All adapters take (query, limit) and fail soft: on any error they return
an empty list and record the error, so one bad source never kills a fetch.
"""
import os
import xml.etree.ElementTree as ET

import httpx
import feedparser

TIMEOUT = 20
UA = {"User-Agent": "Foresight-Prototype/1.0 (market intelligence demo)"}

LAST_ERRORS = {}


def _get(url, params=None, headers=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    r = httpx.get(url, params=params, headers=h, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r


def _safe(source_name):
    """Decorator: fail soft, record error."""
    def wrap(fn):
        def inner(query, limit=10):
            try:
                items = fn(query, limit)
                LAST_ERRORS.pop(source_name, None)
                return items
            except Exception as e:
                LAST_ERRORS[source_name] = str(e)[:300]
                return []
        inner.__name__ = source_name
        return inner
    return wrap


@_safe("arxiv")
def fetch_arxiv(query, limit=10):
    r = _get(
        "https://export.arxiv.org/api/query",
        params={
            "search_query": f"all:{query}",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": limit,
        },
    )
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(r.text)
    out = []
    for e in root.findall("a:entry", ns):
        eid = e.findtext("a:id", "", ns)
        out.append({
            "source": "arxiv",
            "source_id": eid.rsplit("/", 1)[-1],
            "title": " ".join((e.findtext("a:title", "", ns) or "").split()),
            "summary": " ".join((e.findtext("a:summary", "", ns) or "").split())[:600],
            "url": eid,
            "published": e.findtext("a:published", "", ns),
        })
    return out


@_safe("crossref")
def fetch_crossref(query, limit=10):
    r = _get(
        "https://api.crossref.org/works",
        params={"query": query, "rows": limit, "sort": "published", "order": "desc"},
    )
    out = []
    for w in r.json().get("message", {}).get("items", []):
        title = (w.get("title") or [""])[0]
        if not title:
            continue
        out.append({
            "source": "crossref",
            "source_id": w.get("DOI", ""),
            "title": title,
            "summary": (w.get("abstract") or "")[:600],
            "url": w.get("URL", ""),
            "published": "-".join(
                str(p) for p in (w.get("published", {}).get("date-parts", [[""]])[0])
            ),
        })
    return out


@_safe("openalex")
def fetch_openalex(query, limit=10):
    r = _get(
        "https://api.openalex.org/works",
        params={"search": query, "per-page": limit, "sort": "publication_date:desc"},
    )
    out = []
    for w in r.json().get("results", []):
        out.append({
            "source": "openalex",
            "source_id": w.get("id", "").rsplit("/", 1)[-1],
            "title": w.get("display_name", ""),
            "summary": "",  # abstracts are inverted-index; skip for demo speed
            "url": w.get("id", ""),
            "published": w.get("publication_date", ""),
        })
    return out


@_safe("federal_register")
def fetch_federal_register(query, limit=10):
    r = _get(
        "https://www.federalregister.gov/api/v1/documents.json",
        params={
            "conditions[term]": query,
            "per_page": limit,
            "order": "newest",
        },
    )
    out = []
    for d in r.json().get("results", []):
        out.append({
            "source": "federal_register",
            "source_id": d.get("document_number", ""),
            "title": d.get("title", ""),
            "summary": (d.get("abstract") or "")[:600],
            "url": d.get("html_url", ""),
            "published": d.get("publication_date", ""),
        })
    return out


@_safe("regulations_gov")
def fetch_regulations_gov(query, limit=10):
    key = os.environ.get("REGULATIONS_GOV_API_KEY", "")
    if not key:
        raise RuntimeError("REGULATIONS_GOV_API_KEY not set")
    r = _get(
        "https://api.regulations.gov/v4/documents",
        params={"filter[searchTerm]": query, "page[size]": limit,
                "sort": "-postedDate"},
        headers={"X-Api-Key": key},
    )
    out = []
    for d in r.json().get("data", []):
        a = d.get("attributes", {})
        out.append({
            "source": "regulations_gov",
            "source_id": d.get("id", ""),
            "title": a.get("title", ""),
            "summary": (a.get("docAbstract") or "")[:600],
            "url": f"https://www.regulations.gov/document/{d.get('id','')}",
            "published": a.get("postedDate", ""),
        })
    return out


@_safe("gdelt")
def fetch_gdelt(query, limit=10):
    r = _get(
        "https://api.gdeltproject.org/api/v2/doc/doc",
        params={"query": query, "mode": "artlist", "maxrecords": limit,
                "format": "json", "sort": "datedesc"},
    )
    out = []
    for a in r.json().get("articles", []):
        out.append({
            "source": "gdelt",
            "source_id": a.get("url", ""),
            "title": a.get("title", ""),
            "summary": "",
            "url": a.get("url", ""),
            "published": a.get("seendate", ""),
        })
    return out


@_safe("cordis")
def fetch_cordis(query, limit=10):
    r = _get(
        "https://cordis.europa.eu/search/en",
        params={"q": f"'{query}' AND contenttype='project'",
                "num": limit, "format": "json"},
    )
    hits = (r.json().get("payload", {}).get("results", [])
            or r.json().get("hits", {}).get("hit", []))
    out = []
    for h in hits[:limit]:
        rec = h.get("project", h) if isinstance(h, dict) else {}
        rcn = str(rec.get("rcn", "") or rec.get("id", ""))
        title = rec.get("title", "")
        if not title:
            continue
        out.append({
            "source": "cordis",
            "source_id": rcn,
            "title": title,
            "summary": (rec.get("teaser") or rec.get("objective") or "")[:600],
            "url": f"https://cordis.europa.eu/project/rcn/{rcn}" if rcn else "",
            "published": str(rec.get("startDate", "")),
        })
    return out


RSS_FEEDS = [
    ("iec_news", "https://www.iec.ch/blog/rss.xml"),
    ("iso_news", "https://www.iso.org/rss/news.rss"),
    ("ieee_spectrum", "https://spectrum.ieee.org/feeds/feed.rss"),
]


@_safe("standards_rss")
def fetch_standards_rss(query, limit=10):
    out = []
    per_feed = max(2, limit // len(RSS_FEEDS))
    q_words = [w.lower() for w in query.split() if len(w) > 3]
    for name, url in RSS_FEEDS:
        try:
            r = _get(url)
            feed = feedparser.parse(r.text)
        except Exception:
            continue
        kept = 0
        for e in feed.entries:
            title = e.get("title", "")
            summary = e.get("summary", "")[:600]
            text = (title + " " + summary).lower()
            # keep if query words match, or keep latest few if no match logic applies
            if q_words and not any(w in text for w in q_words):
                continue
            out.append({
                "source": name,
                "source_id": e.get("id", e.get("link", title)),
                "title": title,
                "summary": summary,
                "url": e.get("link", ""),
                "published": e.get("published", e.get("updated", "")),
            })
            kept += 1
            if kept >= per_feed:
                break
    return out


ADAPTERS = {
    "arxiv": fetch_arxiv,
    "crossref": fetch_crossref,
    "openalex": fetch_openalex,
    "federal_register": fetch_federal_register,
    "regulations_gov": fetch_regulations_gov,
    "gdelt": fetch_gdelt,
    "cordis": fetch_cordis,
    "standards_rss": fetch_standards_rss,
}


def fetch_all(query, per_source=6, only=None):
    """Fetch from all (or selected) sources. Returns (items, per_source_counts, errors)."""
    items, counts = [], {}
    names = only or list(ADAPTERS.keys())
    for name in names:
        fn = ADAPTERS.get(name)
        if not fn:
            continue
        got = fn(query, per_source)
        counts[name] = len(got)
        items.extend(got)
    return items, counts, dict(LAST_ERRORS)
