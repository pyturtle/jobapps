"""Find the text of a posting JobApps can't read itself (pages that need JavaScript or turn
automated reads away), so its fit can still be judged. Uses your Claude login with web search.

Grounding: Claude's web tools return summaries, not the page's own words, so Claude only points to
pages that carry the same posting (the employer's other careers site, job boards that copy it) and
JobApps reads those pages itself; a page counts only if it names the company and most of the title
("verified"). If none can be read, Claude's summary of the requirements is used but marked
unverified, and the pipeline counts a screen built on it at half weight. One lookup per posting,
ever (the result, found or not, is kept in meta as research:<job id>); at most `max_per_day` a day.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from jobapps import db, forms, llm, sources

SYSTEM = """You find where one internship posting can be read on the web.

Open the posting's own link first. Then search for the same posting (same company, same title, same location) on the employer's careers site, job boards and aggregators that copy the full description.
- urls: up to 5 pages that show this exact posting's full description, best first. Only URLs you saw in search results or opened; never construct or guess one.
- summary: the posting's responsibilities, qualifications and eligibility rules (degree, graduation dates, work authorization, years of experience) as you read them, keeping the original wording wherever you can.
If you can't find this exact posting (only similar ones, or it has been taken down), return found=false, no urls and an empty summary."""

SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "urls": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["found", "urls", "summary"],
    "additionalProperties": False,
}


def same_posting(text: str, job: dict) -> bool:
    """The page is about this job: it names the company and at least 60% of the title's words."""
    low = (text or "").lower()
    company = [w for w in re.findall(r"[a-z0-9]+", job["company"].lower()) if len(w) >= 3]
    title = {w for w in re.findall(r"[a-z0-9]+", job["title"].lower()) if len(w) >= 3}
    return bool(company) and any(w in low for w in company) and \
        (not title or sum(w in low for w in title) / len(title) >= 0.6)


def cached(conn, job_id: str) -> dict | None:
    raw = db.get_meta(conn, f"research:{job_id}")
    return json.loads(raw) if raw else None


def lookup(cfg: dict, job: dict) -> dict:
    """{found, url, text, verified}; never raises for an ordinary miss."""
    rcfg = cfg.get("research", {})
    dcfg = {**cfg["drafts"], **{k: v for k, v in rcfg.items() if k in ("model", "effort", "timeout_seconds")}}
    user = (f"Company: {job['company']}\nTitle: {job['title']}\nLocation: {', '.join(job.get('locations') or [])}\n"
            f"Link: {job['url']}")
    try:
        raw = llm.structured(SYSTEM, user, SCHEMA, dcfg, effort=dcfg.get("effort", "low"), web=True,
                             budget_usd=float(rcfg.get("budget_usd", 0.3)))
    except llm.LLMError as exc:
        return {"found": False, "url": "", "text": "", "verified": False, "error": str(exc)[:200]}
    if not raw.get("found"):
        return {"found": False, "url": "", "text": "", "verified": False}
    own = sources.url_key(job["url"])
    urls = [u.strip() for u in raw.get("urls") or [] if u.strip().startswith("http")]
    for url in urls[:5]:
        if sources.url_key(url) == own:
            continue  # the page JobApps already couldn't read
        text = forms.page_description(url)
        if text and same_posting(text, job):
            return {"found": True, "url": url, "text": text, "verified": True}
    summary = (raw.get("summary") or "").strip()
    if len(summary) < 300:
        return {"found": False, "url": urls[0] if urls else "", "text": "", "verified": False}
    return {"found": True, "url": urls[0] if urls else "", "text": summary, "verified": False}


def research_many(conn, cfg: dict, jobs: list[dict], log=print) -> dict[str, dict]:
    """Look up the postings in `jobs` (best first) that haven't been looked up, within today's allowance.
    Returns every found result for `jobs`, cached or new."""
    rcfg = cfg.get("research", {})
    out, todo = {}, []
    for job in jobs:
        if (hit := cached(conn, job["id"])) is not None:
            if hit.get("found"):
                out[job["id"]] = hit
        else:
            todo.append(job)
    day = datetime.now().date().isoformat()
    used = int(db.get_meta(conn, f"research_count:{day}") or 0)
    todo = todo[:max(0, int(rcfg.get("max_per_day", 6)) - used)]
    if not todo:
        return out
    log(f"  looking up {len(todo)} posting(s) JobApps can't read, with Claude...")
    with ThreadPoolExecutor(max_workers=max(1, int(rcfg.get("parallel", 3)))) as pool:
        results = list(pool.map(lambda j: lookup(cfg, j), todo))
    for job, r in zip(todo, results):
        if r.get("error"):  # a failed call (login, timeout) isn't an answer; try again another day
            log(f"    ! {job['company']}: {r['error']}")
            continue
        r["at"] = datetime.now().isoformat(timespec="seconds")
        db.set_meta(conn, f"research:{job['id']}", json.dumps(r))
        log(f"    {job['company']}: " + ("not found" if not r["found"] else
                                          f"found at {r['url'][:80]}" + ("" if r["verified"] else " (Claude's copy, unverified)")))
        if r["found"]:
            out[job["id"]] = r
    db.set_meta(conn, f"research_count:{day}", str(used + len(todo)))
    return out
