"""LLM fit screening, grounded and cached.

Claude reads one posting and your background and lists the posting's key requirements. For each
it must copy the requirement word for word from the posting and, if you meet it, copy the
evidence word for word from your resume/notes. grounding.py then checks every quote:

  - a requirement that isn't really in the posting is dropped
  - "met"/"partial" without a verifiable quote from your material is downgraded to "missing"
  - a blocker whose quote isn't in the posting is ignored
  - a blocker that isn't one of the HARD kinds (a term, a start date, a preference) is kept as a
    concern you can read; it never rules a posting out

The fit score is computed here from the verified items only, so the model's opinion never
directly sets the ranking. Results are cached per (job, posting text, your background, model),
so a job is judged once and judged the same way every time after.
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

from jobapps import db, grounding, llm
from jobapps.config import Profile

# The only reasons Claude may rule a posting out for; anything else it objects to is shown, not acted on.
HARD = ("degree_level", "graduation_date", "citizenship", "work_authorization", "years_experience")

SYSTEM ="""You screen internship postings for one candidate to judge whether they have a realistic chance of getting an interview.

Ground every statement in the text you were given:
- requirement: copy one requirement word for word from the posting (a phrase or sentence).
- evidence: copy, word for word, the phrase from the candidate background that shows they meet it. Leave it empty if nothing written in the background shows it. Never infer skills or experience that are not written down.
- status: "met" if the evidence clearly satisfies it, "partial" if it is related but weaker (e.g. a similar language or a course instead of work), otherwise "missing".
- blockers: eligibility rules stated in the posting that this candidate fails. quote: copy, word for word, the sentence that states the rule itself. kind: degree_level, graduation_date, citizenship, work_authorization or years_experience; use "other" for anything else (the work term or start date, availability, location, a course or skill). Do not list preferences as blockers.
- summary: one or two sentences spoken to the candidate as "you", naming the strongest match and the biggest gap.
List the posting's 4 to 10 most important requirements, required ones first."""

SCHEMA = {
    "type": "object",
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement": {"type": "string"},
                    "kind": {"type": "string", "enum": ["required", "preferred"]},
                    "status": {"type": "string", "enum": ["met", "partial", "missing"]},
                    "evidence": {"type": "string"},
                },
                "required": ["requirement", "kind", "status", "evidence"],
                "additionalProperties": False,
            },
        },
        "blockers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"quote": {"type": "string"}, "reason": {"type": "string"},
                               "kind": {"type": "string", "enum": [*HARD, "other"]}},
                "required": ["quote", "reason", "kind"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["requirements", "blockers", "summary"],
    "additionalProperties": False,
}

WEIGHT = {"required": 1.0, "preferred": 0.4}
CREDIT = {"met": 1.0, "partial": 0.5, "missing": 0.0}


def eligibility_facts(profile: Profile) -> str:
    g = profile.get
    facts = {
        "Expected graduation": " ".join(str(x) for x in (g("education.graduation_month"), g("education.graduation_year")) if x),
        "Degree in progress": (g("education.degree") or [None])[0] if isinstance(g("education.degree"), list) else g("education.degree"),
        "Citizenship": ", ".join(g("citizenship") or []) if isinstance(g("citizenship"), list) else g("citizenship"),
        "Authorized to work in Canada": g("work_authorization.canada"),
        "Authorized to work in the US": g("work_authorization.us"),
        "Needs sponsorship in Canada": g("needs_sponsorship.canada"),
        "Needs sponsorship in the US": g("needs_sponsorship.us"),
    }
    return "\n".join(f"- {k}: {v}" for k, v in facts.items() if v not in (None, ""))


def verify(raw: dict, posting: str, background: str) -> dict:
    """Keep only what can be quoted; score from what's left."""
    reqs, dropped, downgraded = [], 0, 0
    for item in raw.get("requirements") or []:
        if not grounding.quote_in(item.get("requirement", ""), posting):
            dropped += 1
            continue
        status, evidence = item.get("status", "missing"), (item.get("evidence") or "").strip()
        if status in ("met", "partial") and not (evidence and grounding.quote_in(evidence, background)):
            status, evidence = "missing", ""
            downgraded += 1
        reqs.append({"requirement": item["requirement"].strip(), "kind": item.get("kind", "required"),
                     "status": status, "evidence": evidence})
    quoted = [b for b in raw.get("blockers") or [] if grounding.quote_in(b.get("quote", ""), posting)]
    blockers = [b for b in quoted if b.get("kind") in HARD]
    total = sum(WEIGHT[r["kind"]] for r in reqs)
    fit = round(100 * sum(WEIGHT[r["kind"]] * CREDIT[r["status"]] for r in reqs) / total) if total else None
    return {"requirements": reqs, "blockers": blockers, "concerns": [b for b in quoted if b not in blockers],
            "summary": raw.get("summary", ""), "fit": fit, "dropped": dropped, "downgraded": downgraded}


def verdict(fit: int | None) -> str:
    if fit is None:
        return "unknown"
    return "strong" if fit >= 75 else "good" if fit >= 55 else "stretch" if fit >= 35 else "long shot"


def input_hash(model: str, background: str, posting: str) -> str:
    return hashlib.sha1("\x00".join((model, SYSTEM, background, posting)).encode("utf-8")).hexdigest()[:16]


def _model_cfg(cfg: dict) -> dict:
    return {**cfg["drafts"], **{k: v for k, v in cfg.get("screen", {}).items() if k in ("model", "effort")}}


def cached(conn, cfg: dict, background: str, job: dict, posting: str) -> dict | None:
    """The stored result for this posting text, background and model, if it was screened before."""
    return db.get_screen(conn, job["id"], input_hash(_model_cfg(cfg)["model"], background, posting))


def screen_job(conn, cfg: dict, profile: Profile, background: str, job: dict, posting: str) -> dict | None:
    dcfg = _model_cfg(cfg)
    key = input_hash(dcfg["model"], background, posting)
    if stored := db.get_screen(conn, job["id"], key):
        return stored
    user = (f"<candidate_background>\n{background}\n\nEligibility facts:\n{eligibility_facts(profile)}\n"
            f"</candidate_background>\n\n<posting>\nCompany: {job['company']}\nRole: {job['title']}\n"
            f"Location: {', '.join(job.get('locations') or [])}\n\n{posting}\n</posting>")
    raw = llm.structured(SYSTEM, user, SCHEMA, dcfg, effort=dcfg.get("effort", "low"))
    result = verify(raw, posting, f"{background}\n{eligibility_facts(profile)}")
    result["verdict"] = verdict(result["fit"])
    db.save_screen(conn, job["id"], key, dcfg["model"], result)
    return result


def screen_many(cfg: dict, profile: Profile, background: str, items: list[tuple[dict, str]], log=print) -> dict[str, dict]:
    def work(item):
        job, posting = item
        conn = db.connect()
        try:
            return job["id"], screen_job(conn, cfg, profile, background, job, posting)
        except llm.LLMError as exc:
            log(f"    ! screening {job['company']} failed: {exc}")
            return job["id"], None
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=max(1, int(cfg.get("screen", {}).get("parallel", 3)))) as pool:
        return {k: v for k, v in pool.map(work, items) if v is not None}
