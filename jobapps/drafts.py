"""Draft answers to free-text application questions, grounded in your resume and notes.

The prompt is built deterministically from profile.yaml, your resume, background.md and the job
posting, and saved under data/prompts/ so you can see (or reuse) exactly what was asked.
Each answer must list the facts it relies on, quoted from your material; grounding.py checks
those quotes and any numbers, and the result is stored next to the answer as `checks`.
Answers are cached per (job, question): they never change unless you redraft.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from jobapps import db, grounding, llm
from jobapps.config import BACKGROUND_PATH, DATA_DIR, PROMPTS_DIR, Profile, atomic_write
from jobapps.forms import Field
from jobapps.rules import qkey

# Built from what screening actually does (2025-26 ATS docs, studies, career-centre guidance): auto-rejects
# fire on structured knockout answers, which are filled from profile.yaml and never drafted; AI screeners
# read the resume, and at most check that a written answer is on topic; a person skims the written
# answers in seconds. Generic, posting-echoing text stopped earning callbacks once AI made it free.
SYSTEM_RULES = """You draft answers to the written questions on one candidate's internship applications. The candidate reads and edits every answer before submitting.

Who reads them: a recruiter or engineer skimming many applications for a few seconds each, sometimes after an AI screener has checked that each answer is on topic. Knockout questions (work authorization, sponsorship, location, dates) are filled elsewhere and never come to you. An answer wins by being specific, true and quick to skim. It loses by sounding like every other applicant, or like AI.

FACTS (non-negotiable)
- Use only facts from <candidate_background>. Never invent employers, projects, metrics, awards, courses, tools, dates, or experiences. If the question explicitly asks for something the background lacks (a lesson learned, a specific conflict), leave a short [bracketed placeholder] for the candidate. Never add placeholders for optional extras the question didn't ask for; just leave them out.
- facts_used: every phrase from the background the answer relies on, copied word for word.
- Facts about the company need a source too: use only what the job description says (its products, problems, stack, team, values). Never fake familiarity ("I've long admired..."). posting_points: the 0-3 phrases from the job description the answer speaks to, copied word for word.

METHOD, for each question
1. Set question_type. Choose the posting points to speak to, in this order of preference: the role's responsibilities and the team's problem; named stack or requirements the candidate really has; a named product or user; values or mission only when tied to a candidate fact.
2. Pick the single strongest evidence for those points: the job, project or result closest to the posting's stack. Prefer recent professional work and shipped, used results over coursework or hackathons, unless <candidate_preferences> says otherwise.
3. The first sentence answers the question directly. Then the evidence: what the candidate did ("I ...") and the result, with a number from the background when there is one. Then, if there's room, the link to this team.
4. Use the posting's exact name for each skill or tool the candidate really has, once, attached to evidence ("built 44 Next.js routes on Postgres"), never as a list of keywords. Mirror the posting's requirement nouns, not its marketing adjectives, and don't paraphrase the posting back to them.

PLAYBOOK by question_type
- why_company ("Why X?"): one or two specific reasons from the job description (a product, problem, stack or team), each tied to something the candidate did. Not the mission statement recited back; not perks or prestige. Shape: [specific thing they build or solve]. [related thing I did, with detail]. [what I want to learn or contribute there].
- why_role ("Why this role/team?"): match one or two listed responsibilities to the closest evidence, naming the tool. Shape: [responsibility] is what I did at/in [job or project], where I [action and result]. I want to go deeper on [area from the posting].
- project ("a project you're proud of", "your most significant accomplishment", "a technical challenge"): the project closest to the posting's stack. One sentence on what it is and why, then the hard part, the candidate's decisions or trade-offs, and the result (metric, users, what shipped).
- behavioral (failure, conflict, teamwork, leadership): Context-Action-Result. One sentence of context, about 60% on the candidate's own actions, then the result. Add what the candidate learned or does differently now only for a failure, or when the question asks. Use only events the background describes; if none fits exactly, use the closest real one and put [placeholders] where the background lacks specifics. Conflict is about the work, settled with evidence. Leadership can be informal.
- career_goals: near-term (the next 2-4 years) and concrete: skills or a domain, linked to this internship and consistent with the resume. No grand visions.
- fit ("Why are you a good fit?"): two or three stated requirements, one proof each, plus one differentiator a resume skim would miss.
- product_interest: one concrete observation about the product or problem from the job description, linked to the candidate's experience. No superlatives.
- short_factual ("Describe your experience with X"): no preamble: duration or context, what was built, scale. If the background shows no experience with X, say so plainly or name the nearest real equivalent.
- anything_else: only decision-relevant facts not covered elsewhere (availability or term, graduation date, a portfolio or GitHub link). Two sentences at most.
- other: apply the same method.

LENGTH
- Obey stated limits exactly as stated (characters or words). Characters: about 6 per word including spaces; aim for 85-95% of the limit. Words: 70-100% of the limit. Cut setup before evidence.
- No limit: 80-150 words for open questions, one or two sentences for short or factual ones. Shorter beats longer; never pad.

STYLE
- First person, plain and direct, the way a capable student talks to an engineer. Contractions are fine.
- Plain text: no markdown, bullets, headings or emojis. No em dashes; use commas or periods.
- Match the posting's spelling (Canadian spelling for Canadian employers unless the posting uses American).
- Don't restate the question, don't start two answers the same way, and don't end on a summary or flourish sentence.
- Never use these words or phrases: passionate, excited to, thrilled, eager to, I am writing to, leverage, aligns perfectly, resonates, deeply, meaningful impact, drive impact, unique blend, hardworking, detail-oriented, fast-paced, dynamic environment, cutting-edge, innovative solutions, at the intersection of, hit the ground running, delve, tapestry, testament, underscore, pivotal, intricate, showcase, meticulous, foster, harness, elevate, embark, journey, navigate the complexities, ever-evolving, landscape, seamless, robust (as praise), in today's world, I am confident that, thank you for considering.
- Avoid these patterns: "not only ... but also"; "It's not just X, it's Y"; three adjectives in a row; a trailing "-ing" clause that comments on the sentence (", showcasing my ..."); repeating the company's marketing copy.
- Each answer stands alone; never refer to another answer.
- Write in English (on bilingual forms, answer the English version)."""

QUESTION_TYPES = ["why_company", "why_role", "project", "behavioral", "career_goals", "fit", "product_interest",
                  "short_factual", "anything_else", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {  # planned before written: type and posting points come first
                    "id": {"type": "string"},
                    "question_type": {"type": "string", "enum": QUESTION_TYPES},
                    "posting_points": {"type": "array", "items": {"type": "string"}},
                    "answer": {"type": "string"},
                    "facts_used": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "question_type", "posting_points", "answer", "facts_used"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["answers"],
    "additionalProperties": False,
}

# Phrases recruiters read as filler or as AI-written (Wikipedia "Signs of AI writing", Kobak et al. 2024,
# career-centre guidance). Flagged on every answer and fixed by one automatic rewrite.
GENERIC = re.compile(
    r"\b(passionate|passion for|excited to|thrilled|eager to|i am writing to|leverag\w*|aligns? perfectly|resonat\w*|"
    r"deeply|meaningful impact|drive impact|unique blend|hard-?working|detail-oriented|fast-paced|dynamic environment|"
    r"cutting-edge|innovative solutions?|at the intersection of|hit the ground running|delv\w*|tapestry|testament|"
    r"underscor\w*|pivotal|intricate|showcas\w*|meticulous\w*|foster\w*|harness\w*|elevat\w*|embark\w*|journey|"
    r"navigat\w* the complexit\w*|ever-evolving|rapidly evolving|landscape|seamless\w*|in today's\b[\w\s-]{0,20}world|"
    r"i am confident that|thank you for considering|not only\b.{1,80}\bbut also|it's not just)", re.I)

_LIMIT = re.compile(r"(\d[\d,]*)\s*(?:-|\s)?\s*(characters?|chars?|words?)\b", re.I)


# --- candidate background ---------------------------------------------------

def fix_kerning(text: str) -> str:
    """Undo letter splits that PDF extraction makes in kerned (often LaTeX) resumes:
    'A WS' -> 'AWS', 'F rameworks' -> 'Frameworks', 'Y ouT ube' -> 'YouTube'."""
    text = re.sub(r"(?<![\w])([B-HJ-Z]) (?=[a-z]{2,})", r"\1", text)
    text = re.sub(r"(?<=[a-z])([A-Z]) (?=[a-z]{2,})", r"\1", text)
    text = re.sub(r"(?<![\w])([A-Z]) (?=[A-Z]{2,}\b)", r"\1", text)
    return text


def resume_text(profile: Profile) -> str:
    path = profile.file("files.resume")
    if not path:
        return ""
    cache = DATA_DIR / "resume.txt"
    stamp = f"v2|{path}|{path.stat().st_mtime_ns}"
    if cache.exists():
        head, _, body = cache.read_text(encoding="utf-8").partition("\n")
        if head == stamp:
            return body
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        text = fix_kerning("\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages))
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"[ \t]+", " ", text).strip()
    cache.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(cache, f"{stamp}\n{text}")
    return text


def notes_text() -> str:
    """background.md without its template comments."""
    if not BACKGROUND_PATH.exists():
        return ""
    text = re.sub(r"<!--.*?-->", "", BACKGROUND_PATH.read_text(encoding="utf-8"), flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def has_material(profile: Profile) -> bool:
    """Enough real background to draft from (otherwise drafts would be all placeholders)."""
    notes = re.sub(r"^#.*$", "", notes_text(), flags=re.M).strip()
    return bool(resume_text(profile)) or len(notes) > 200


def background(profile: Profile) -> str:
    g = profile.get
    facts = [
        ("Name", profile.full_name), ("Location", g("location")),
        ("School", g("education.school")), ("Degree", g("education.degree")),
        ("Major", g("education.major")), ("Minor", g("education.minor")),
        ("Expected graduation", " ".join(str(x) for x in (g("education.graduation_month"), g("education.graduation_year")) if x)),
        ("Year of study", g("education.year_of_study")), ("GPA", g("education.gpa")),
        ("LinkedIn", g("links.linkedin")), ("GitHub", g("links.github")), ("Website", g("links.website")),
    ]
    # Your own answers to the legal questions, so drafts like "Do you need sponsorship?" can say it plainly.
    if citizenship := g("citizenship"):
        facts.append(("Citizenship", ", ".join(map(str, citizenship))))
    for code, place in (("canada", "Canada"), ("us", "the United States")):
        for key, text in (("work_authorization", f"Authorized to work in {place}"),
                          ("needs_sponsorship", f"Needs visa sponsorship to work in {place}")):
            value = g(f"{key}.{code}")
            if isinstance(value, bool):
                facts.append((text, "yes" if value else "no"))
    # What you're applying for, so "anything else?" and availability answers can state it.
    facts += [("Internship term wanted", ", ".join(map(str, g("preferences.term") or []))),
              ("Available to start", g("preferences.start_date"))]
    lines = [f"- {k}: {v[0] if isinstance(v, list) else v}" for k, v in facts if v]
    parts = ["## Profile", "\n".join(lines)]
    if text := resume_text(profile):
        parts += ["## Resume", text]
    if notes := notes_text():
        parts += ["## Notes from the candidate", notes]
    return "\n\n".join(parts)


def preferences_block(profile: Profile) -> str:
    """Your standing instructions for how answers are written (not facts: they're never quoted as evidence)."""
    prefs = profile.get("writing_preferences") or []
    if not prefs:
        return ""
    items = "\n".join(f"- {p}" for p in prefs)
    return ("\n\n<candidate_preferences>\nThe candidate's own instructions for how to write their answers. Follow them "
            "unless a question clearly needs otherwise. They are not facts about the candidate.\n"
            f"{items}\n</candidate_preferences>")


def system_prompt(profile: Profile) -> str:
    return f"{SYSTEM_RULES}{preferences_block(profile)}\n\n<candidate_background>\n{background(profile)}\n</candidate_background>"


# --- per-job prompt ---------------------------------------------------------

def length_limit(f: Field, maxlength: int | None = None) -> str:
    for text in (f.label, f.description):
        if m := _LIMIT.search(text or ""):
            unit = "words" if m.group(2).lower().startswith("word") else "characters"
            return f"max {m.group(1).replace(',', '')} {unit}"
    return f"max {maxlength} characters" if maxlength else ""


def user_prompt(job: dict, description: str, questions: list[tuple[str, Field, str]]) -> str:
    lines = [
        f"Company: {job['company']}",
        f"Role: {job['title']}",
        f"Location: {', '.join(job.get('locations') or []) or 'not stated'}",
        f"Posting: {job['url']}",
        "",
        "<job_description>",
        (description or "(not available - rely on the company and role name)").strip(),
        "</job_description>",
        "",
        "Draft an answer for every question below. Return one entry per id.",
        "",
    ]
    for qid, f, limit in questions:
        meta = "; ".join(x for x in (limit, "required" if f.required else "optional") if x)
        lines.append(f"[{qid}] {f.label.strip()} ({meta})")
        if f.description and f.description.strip() not in f.label:
            lines.append(f"    Note under the question: {f.description.strip()}")
    return "\n".join(lines)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "job"


def write_prompt_file(job: dict, system: str, user: str, today: date, suffix: str = "") -> Path:
    folder = PROMPTS_DIR / today.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_slug(job['company'])}-{job['id']}{f'-{suffix}' if suffix else ''}.md"
    path.write_text(f"# System prompt\n\n{system}\n\n# User prompt\n\n{user}\n", encoding="utf-8")
    return path


def pick_backend(cfg: dict) -> str:
    return llm.backend(cfg)


def check_answer(answer: str, facts: list[str], source: str, points: list[str] | None = None,
                 description: str = "", question_type: str = "") -> dict:
    """Report for one answer; empty lists mean nothing suspicious was found.
    unsupported_*: claims about you not in your material. unsourced_points: "facts" about the company not in
    the posting. generic_phrases: stock wording recruiters read as filler or AI. addresses: the posting lines
    it answers (verified)."""
    points = [x for x in points or [] if x.strip()]
    sourced = [x for x in points if grounding.phrase_in(x, description)] if description else []
    return {
        "unsupported_facts": [f for f in facts if f.strip() and not grounding.phrase_in(f, source)],
        "unsupported_numbers": grounding.unsupported_numbers(answer, source),
        "placeholders": grounding.placeholders(answer),
        "generic_phrases": list(dict.fromkeys(m.group(0).lower() for m in GENERIC.finditer(answer or ""))),
        "em_dashes": (answer or "").count("\u2014"),
        "addresses": sourced,
        "unsourced_points": [x for x in points if x not in sourced] if description else [],
        "question_type": question_type,
    }


def _run(system: str, user: str, cfg: dict) -> dict[str, dict]:
    data = llm.structured(system, user, SCHEMA, cfg)
    return {a["id"]: a for a in data.get("answers", []) if a.get("answer", "").strip()}


def _length(answer: str, limit: str) -> tuple[int, int] | None:
    m = re.match(r"max (\d+) (words|characters)", limit or "")
    if not m:
        return None
    return (len(answer.split()) if m.group(2) == "words" else len(answer)), int(m.group(1))


def _over_limit(answer: str, limit: str) -> bool:
    n = _length(answer, limit)
    return bool(n and n[0] > n[1])


def _checks(a: dict, source: str, description: str) -> dict:
    return check_answer(a["answer"].strip(), a.get("facts_used") or [], source, a.get("posting_points") or [],
                        description, a.get("question_type") or "")


def problems(a: dict, limit: str, source: str, description: str) -> list[str]:
    """Rule breaks we can check without a person: each one triggers a single automatic rewrite."""
    c, out = _checks(a, source, description), []
    if (n := _length(a["answer"], limit)) and n[0] > n[1]:
        out.append(f"it is {n[0]} {limit.split()[-1]}, over the limit of {n[1]}; cut setup, keep the evidence")
    if c["unsupported_facts"]:
        out.append("these facts_used are not in the candidate background word for word, so remove or correct those claims: "
                   + "; ".join(f'"{x}"' for x in c["unsupported_facts"][:4]))
    if c["unsupported_numbers"]:
        out.append(f"these numbers are not in the background: {', '.join(c['unsupported_numbers'][:4])}")
    if c["unsourced_points"]:
        out.append("these posting_points are not in the job description; only use what the posting says: "
                   + "; ".join(f'"{x}"' for x in c["unsourced_points"][:3]))
    if c["generic_phrases"]:
        out.append(f"it uses stock phrases recruiters skip: {', '.join(c['generic_phrases'][:6])}; say the concrete thing instead")
    if c["em_dashes"]:
        out.append("it uses em dashes; use commas or periods")
    return out


def _improve(system: str, prompt_for, questions: list[tuple[str, Field, str]], answers: dict[str, dict], cfg: dict,
             source: str, description: str) -> dict[str, dict]:
    """One automatic rewrite of the answers that broke a checkable rule; a rewrite is kept only if it's better."""
    issues = {qid: problems(answers[qid], lim, source, description) for qid, _, lim in questions if qid in answers}
    bad = [q for q in questions if issues.get(q[0])]
    if not bad:
        return answers
    notes = "\n".join(f"[{qid}] problems: {' | '.join(issues[qid])}\n<previous_draft>\n{answers[qid]['answer'].strip()}\n"
                      "</previous_draft>" for qid, _, _ in bad)
    retry = (prompt_for(bad) + "\n\nYour previous drafts for these questions broke rules. Rewrite each one, fixing every "
             "listed problem and keeping what was good. Every rule above still applies.\n\n" + notes)
    try:
        fixed = _run(system, retry, cfg)
    except llm.LLMError:
        return answers  # the first drafts stand, with their problems shown to you
    for qid, _, lim in bad:
        if qid in fixed and len(problems(fixed[qid], lim, source, description)) < len(issues[qid]):
            answers[qid] = fixed[qid]
    return answers


# --- public -----------------------------------------------------------------

def draft_job(conn, cfg: dict, profile: Profile, job: dict, description: str, fields: list[Field],
              today: date, maxlengths: dict[str, int] | None = None, force: bool = False, log=print) -> dict:
    """Draft every not-yet-answered question in `fields`. Returns a status dict."""
    existing = db.answers_for(conn, job["id"])
    seen, unique = set(), []
    for f in fields:
        k = qkey(f.label)
        if k not in seen and (force or not (existing.get(k) or {}).get("answer")):
            seen.add(k)
            unique.append(f)
    if not unique:
        return {"status": "cached", "count": 0}
    questions = [(f"q{i}", f, length_limit(f, (maxlengths or {}).get(qkey(f.label))))
                 for i, f in enumerate(unique, 1)]
    system, user = system_prompt(profile), user_prompt(job, description, questions)
    prompt_file = write_prompt_file(job, system, user, today)
    kind = llm.backend(cfg)
    if kind == "none":
        for _, f, _ in questions:
            db.save_answer(conn, job["id"], qkey(f.label), f.label, None, str(prompt_file), "none", "")
        return {"status": "prompt-only", "count": len(questions), "prompt_file": prompt_file}
    source = background(profile)
    try:
        answers = _run(system, user, cfg)
        answers = _improve(system, lambda qs: user_prompt(job, description, qs), questions, answers, cfg, source, description)
    except llm.LLMError as exc:
        log(f"    ! drafting failed for {job['company']}: {exc}")
        for _, f, _ in questions:
            db.save_answer(conn, job["id"], qkey(f.label), f.label, None, str(prompt_file), kind, cfg["model"])
        return {"status": "error", "error": str(exc), "count": len(questions), "prompt_file": prompt_file}
    for qid, f, _ in questions:
        a = answers.get(qid)
        db.save_answer(conn, job["id"], qkey(f.label), f.label, a["answer"].strip() if a else None, str(prompt_file),
                       kind, cfg["model"], checks=_checks(a, source, description) if a else None)
    return {"status": "drafted", "count": sum(1 for q, _, _ in questions if q in answers), "prompt_file": prompt_file}


def redraft_answer(conn, cfg: dict, profile: Profile, job: dict, description: str, f: Field, instruction: str,
                   current: str, today: date) -> dict:
    """Redraft one answer, following your instruction ("lead with my internship, not the hackathon project").
    Same rules and checks as a first draft; your edits to it are replaced by the new draft."""
    def prompt_for(qs):
        user = user_prompt(job, description, qs)
        if current:
            user += f"\n\nThe candidate's current draft for q1:\n<current_draft>\n{current.strip()}\n</current_draft>"
        if instruction:
            user += (f"\n\nThe candidate asked for this change:\n<change_request>\n{instruction.strip()}\n</change_request>\n"
                     "Rewrite q1 so it follows the change request. Every rule above still applies: use only facts "
                     "from the candidate background, and list them in facts_used.")
        else:
            user += "\n\nWrite a fresh draft for q1 that takes a different angle from the current one."
        return user

    questions = [("q1", f, length_limit(f))]
    system, user = system_prompt(profile), prompt_for(questions)
    prompt_file = write_prompt_file(job, system, user, today, suffix=f"redraft-{_slug(f.label)[:24]}")
    if llm.backend(cfg) == "none":
        raise llm.LLMError("no drafting backend is configured")
    source = background(profile)
    answers = _improve(system, prompt_for, questions, _run(system, user, cfg), cfg, source, description)
    a = answers.get("q1")
    if not a:
        raise llm.LLMError("Claude returned no answer")
    text, checks = a["answer"].strip(), _checks(a, source, description)
    key = qkey(f.label)
    db.save_answer(conn, job["id"], key, f.label, text, str(prompt_file), llm.backend(cfg), cfg["model"], checks=checks)
    db.save_final_answer(conn, job["id"], key, None)
    return {"answer": text, "checks": checks}


def draft_many(cfg: dict, profile: Profile, jobs: list[tuple[dict, str, list[Field]]], today: date,
               force: bool = False, log=print) -> dict[str, dict]:
    """Draft several jobs concurrently (each worker uses its own DB connection)."""
    def work(item):
        job, description, fields = item
        conn = db.connect()
        try:
            return job["id"], draft_job(conn, cfg, profile, job, description, fields, today, force=force, log=log)
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=max(1, int(cfg.get("parallel", 3)))) as pool:
        return dict(pool.map(work, jobs))
