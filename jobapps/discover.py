"""Optional daily web search for postings the public feeds missed, using your Claude login.

Grounding: the model only proposes links. A link is kept only if it is a Greenhouse, Lever or
Ashby posting whose public API answers right now; the title, company, locations and posting date
stored come from that API, never from the model. Kept postings then go through exactly the same
deterministic ranking as everything else.
"""
from __future__ import annotations

import time

from jobapps import db, forms, llm, sources
from jobapps.config import Profile

SYSTEM = """You search the web for currently open internship postings that fit one candidate.

Only return links to individual job postings hosted on job-boards.greenhouse.io, boards.greenhouse.io, jobs.lever.co or jobs.ashbyhq.com. Only include links you actually saw in search results or opened; never construct or guess a URL. Prefer postings from the last two weeks. Skip roles that require a graduate degree or several years of experience."""

SCHEMA = {
    "type": "object",
    "properties": {
        "postings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"url": {"type": "string"}, "company": {"type": "string"}, "title": {"type": "string"}},
                "required": ["url", "company", "title"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["postings"],
    "additionalProperties": False,
}


def brief(profile: Profile, skills: set[str], cfg: dict) -> str:
    g = profile.get
    terms = g("preferences.term") or ["Summer 2027"]
    roles = cfg.get("roles") or ["software engineering", "machine learning", "data"]
    places = cfg.get("locations") or [g("address.city") or "Toronto", "Canada", "remote", "United States"]
    return (f"Candidate: {g('education.year_of_study') or 'undergraduate'} {g('education.major') or 'computer science'} student at "
            f"{g('education.school') or 'a university'}, graduating {g('education.graduation_year') or 'in a few years'}.\n"
            f"Skills: {', '.join(sorted(skills)) or 'general software engineering'}.\n"
            f"Looking for: {', '.join(terms)} internships in {', '.join(roles)}.\n"
            f"Locations, best first: {', '.join(places)}.\n"
            f"Find up to {cfg.get('max_results', 15)} postings.")


def run(conn, cfg: dict, profile: Profile, skills: set[str], log=print) -> int:
    dcfg = cfg.get("discover", {})
    if not dcfg.get("enabled"):
        return 0
    if llm.backend(cfg["drafts"]) != "claude-cli":
        log("  discovery needs the claude-cli backend; skipped")
        return 0
    log("Searching the web for more postings (Claude)...")
    try:
        data = llm.structured(SYSTEM, brief(profile, skills, dcfg), SCHEMA, cfg["drafts"], effort=dcfg.get("effort", "low"),
                              web=True, budget_usd=float(dcfg.get("budget_usd", 1.0)))
    except llm.LLMError as exc:
        log(f"  ! search failed: {exc}")
        return 0
    known = {r["id"] for r in conn.execute("SELECT id FROM listings")}
    kept, why = [], {"already known": 0, "not a Greenhouse/Lever/Ashby link": 0, "closed or not found": 0}
    for p in data.get("postings", []):
        url = sources.canonical_url(p.get("url", ""))
        listing_id = sources.listing_id(url)
        ats = sources.detect_ats(url)
        if ats not in sources.API_ATS:
            why["not a Greenhouse/Lever/Ashby link"] += 1
            continue
        if listing_id in known:
            why["already known"] += 1
            continue
        try:
            form = forms.prefetch({"url": url, "ats": ats, "company": p.get("company", "")})
        except Exception:  # closed, malformed, or unreachable: unverifiable, so dropped
            form = None
        if not form or not form.title:
            why["closed or not found"] += 1
            continue
        kept.append({
            "id": listing_id, "source": "claude-search", "company": form.company or p.get("company", ""),
            "title": form.title, "url": url, "ats": ats, "locations": form.locations, "terms": [],
            "category": "", "sponsorship": "", "degrees": [], "date_posted": form.posted or int(time.time()),
            "active": True,
        })
        db.save_form(conn, listing_id, form.ats, form.description, [f.to_dict() for f in form.fields], form.apply_url)
    if kept:
        db.upsert_listings(conn, kept, deactivate_missing=False)
    dropped = ", ".join(f"{n} {reason}" for reason, n in why.items() if n)
    log(f"  {len(kept)} new verified posting(s) from {len(data.get('postings', []))} suggested" + (f" ({dropped})" if dropped else ""))
    return len(kept)
