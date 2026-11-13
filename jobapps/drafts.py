"""Draft answers to free-text application questions, grounded in your resume and notes.

The prompt is built deterministically from profile.yaml, your resume, background.md, the files and pages
listed under `notes` in config.yaml, and the job posting, and saved under data/prompts/ so you can see (or
reuse) exactly what was asked.
Each answer must list the facts it relies on, quoted from your material; grounding.py checks
those quotes and any numbers, and the result is stored next to the answer as `checks`.
Answers are cached per (job, question): they never change unless you redraft.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from jobapps import db, grounding, llm, notes
from jobapps.config import BACKGROUND_PATH, DATA_DIR, PROMPTS_DIR, Profile, atomic_write, load_config
from jobapps.forms import Field
from jobapps.rules import qkey

# Rebuilt 2026-11 from research on how written answers are read (reports/Authentic job application answers.md):
# readers can't reliably tell AI text but mark it down once they suspect it; what gives drafts away now is
# shape, not vocabulary (the same stats in every answer, verdicts about yourself, a closing line claiming fit);
# tailoring stopped carrying signal once AI made it free, so the value is in what only the candidate knows
# (a reason, a mistake, a teammate's part). Humble means no verdicts, not hedging. The candidate's own edits
# deleted exactly those closing and self-praising lines. Written as prose with reasons: the prompt's own
# shape leaks into the answers, and bracketed answer templates came back as same-shaped answers.
SYSTEM_RULES = """You draft answers to the written questions on one student's internship applications. The candidate reads and edits every answer before submitting it, so write what they would write on a good day: a capable student explaining their work to an engineer, plainly and in their own register.

The people reading already have the candidate's resume. An answer earns its place by telling them what the resume can't: why the candidate did something, the moment it got hard, the decision they made, who they worked with, and how it turned out or what they'd do differently. Recited resume bullets, stacked numbers and tool lists, and claims of being a good fit are what generated answers look like, and readers mark an answer down once they suspect it was generated. Owning results plainly reads as confident. Judging yourself ("I already ship...", "I know how to...") reads as bragging, and hedging or false modesty reads as weak. Knockout questions (work authorization, sponsorship, location, dates) are filled elsewhere and never come to you.

FACTS (non-negotiable)
- Use only facts from <candidate_background>. Never invent employers, projects, metrics, awards, courses, tools, dates, experiences, motives or feelings. facts_used: every phrase from the background the answer relies on, copied word for word.
- Facts about the company come only from the job description: its products, problems, stack, team and values. Never fake familiarity ("I've long admired..."). posting_points: the 0-3 phrases from the job description the answer speaks to, copied word for word.
- Applying already implies interest, so you may say which part of the work the candidate wants to learn. Personal reasons and reflections are different: why this company matters to them, why they built something, what drove them, what surprised them, what they learned, what they do differently now or would change. Use one only if the background states it. Otherwise leave a short [bracketed placeholder] that names what's missing, such as [why this team, in your words], and write the rest of the answer around it. The candidate would rather fill a gap than delete a reason they never had. Don't add placeholders for things the question didn't ask for.

HOW TO WRITE EACH ANSWER
Set question_type and choose posting_points first. Answer the question in the first sentence. Then tell one story: the job, project or experience closest to what the question and posting ask about, preferring recent professional work over hackathons or coursework unless <candidate_preferences> says otherwise. Spend most of the words on what the candidate did and decided, and why. One concrete detail is enough: a single number or a single named tool where it explains something; use a second number only when the question asks about results. Name tools the way the posting does, inside the story, never as a list.

The answers for one application are read together. Give each answer a different story, or a different part of the same story, and don't repeat a number, a tool list or a sentence from another answer, including those in <already_answered>. For team work, say "we" for what the team did and "I" for the candidate's own part, and name a teammate's role when the background does. Never make the candidate's role bigger than the background says.

End on the last concrete thing: the result, the part of the work they want to learn next, or a lesson the background states (a placeholder if the question asks for one and the background has none). Deciding whether the candidate fits is the reader's job, so don't close by connecting the candidate's work to the company's ("that's close to what you do", "which is why this role fits me"), and don't sum up.

BY QUESTION TYPE
- why_company: one specific thing from the job description (a product, a problem, a technical choice) and the candidate's own reason it matters to them, or a placeholder for that reason. If a competitor's name could replace the company's and the answer still worked, it isn't specific enough.
- why_role: one listed responsibility, the closest thing the candidate has done, and which part of the job they'd want to learn.
- project ("a project you're proud of", "your most significant accomplishment", "a technical challenge"): what it is and why it exists, the hard part, one decision the candidate made, and how it turned out. What they'd change comes from the background, or is a placeholder when the question asks for it.
- behavioral (failure, conflict, teamwork, leadership, initiative, learning something new): one clause of context, then most of the words on the difficult moment and what the candidate did, then the outcome. Use only events and lessons the background describes, with [placeholders] where it lacks specifics. Conflict is about the work, settled with evidence. Leadership can be informal.
- career_goals: the next 2-4 years, concrete: what the candidate can do now, what they want to learn next, and how this internship fits. No grand visions.
- fit ("Why should we hire you?"): two pieces of evidence for requirements the posting states, said plainly. No claims about being a great fit and no comparisons with other applicants.
- product_interest: one concrete observation or trade-off about the product or problem from the job description, linked to something the candidate built or used. No superlatives.
- short_factual ("Describe your experience with X"): no preamble: where, what was built, for how long. If the background shows no experience with X, say so plainly and name the nearest real equivalent.
- anything_else: only decision-relevant facts not covered elsewhere (availability or term, graduation date, a portfolio or GitHub link). Two sentences at most.
- other: the same approach.

LENGTH
A stated limit (characters, words or sentences) is a ceiling, never a target: obey it exactly, and stop when the answer is done, which is usually well under it. For characters, count about 6 per word including spaces. With no limit, write 60-120 words for open questions and one or two sentences for short or factual ones. Cut setup before evidence.

STYLE
Write in first person, plain and direct, with contractions where they're natural and mostly short sentences. Use plain text: no markdown, bullets, headings or emojis, and commas or periods instead of dashes. Match the posting's spelling (Canadian spelling for Canadian employers unless the posting uses American). Write about the work, never about the application: don't mention resumes, recruiters or what a reader might miss, don't restate the question, and don't refer to other answers. Start each answer differently.

Stock phrases make an answer sound like every other applicant's, so say the concrete thing instead. These are flagged automatically: passionate, excited to, thrilled, eager to, I am writing to, leverage, aligns perfectly, resonates, deeply, meaningful impact, drive impact, unique blend, hardworking, detail-oriented, fast-paced, dynamic environment, cutting-edge, innovative solutions, at the intersection of, hit the ground running, delve, tapestry, testament, underscore, pivotal, intricate, showcase, meticulous, foster, harness, elevate, embark, journey, navigate the complexities, ever-evolving, landscape, seamless, robust (as praise), in today's world, I am confident that, thank you for considering. So are "not only ... but also", "it's not just X, it's Y", three adjectives in a row, and a trailing "-ing" clause that comments on the sentence (", showcasing my ...").

Write in English (on bilingual forms, answer the English version)."""

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

# Shape, not wording: what the candidate deleted from drafts by hand, and what readers who use LLMs every day
# pick out (Russell et al. 2025: formulaic structure, cheerful summary endings). A closing line that ties the
# candidate's work to the company's ("close to what you do", "which is why this role fits me"); verdicts about
# themselves; talk about the application itself (a prompt word once leaked as "a resume skim might miss...");
# numbers stacked in one sentence; the same numbers and wording repeated across one application's answers.
_FIT_STRONG = re.compile(
    r"\b(which is why|that's why|this is why|fits? (?:me|my\b)|(?:good|great|strong|natural|perfect|ideal) fit|"
    r"(?:I'd|I would|I can|I'll) bring|makes? me (?:a )?(?:good|great|strong|ideal|natural)|closest (?:thing|match)|"
    r"lines? up (?:with|well)|align(?:s|ed)? (?:with|well)|same (?:set of |kind of )?(?:problems|work|skills)|"
    r"directly (?:applicable|relevant)|translates? (?:directly|well)|close to what)\b", re.I)
_FIT_WEAK = re.compile(r"\bclose to (?:the|your|how)\b", re.I)
_FIT_CONTEXT = re.compile(r"\b(you|your|role|team|internship|position|company|stack|work|product)\b", re.I)
SELF_VERDICT = re.compile(
    r"\b(I already (?:ship|build|know|have|do|work)\w*|I know how to|I hold (?:my|myself)\w*|"
    r"I(?:'m| am) (?:confident|the kind of|a (?:strong|great|natural|quick|fast) \w+|well[- ]suited|strongest)|"
    r"my experience covers|I excel|I thrive|I bring|(?:shows|proves|demonstrates) (?:that )?I)\b", re.I)
META = re.compile(r"\b(r[eé]sum[eé]s?|skim\w*|recruiters?|hiring managers?|hiring team|as you can see|on paper|"
                  r"this (?:answer|application|question))\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9])")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*\+?%?(?!\w)")
_YEAR = re.compile(r"(?:19|20)\d\d")
_ASKS_NUMBERS = re.compile(r"how many|number of|quantif|metric|results?\b|impact|gpa|grade|salary|compensation", re.I)
_PLAIN_TYPES = ("short_factual", "anything_else")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split((text or "").strip()) if s.strip()]


def numbers(text: str) -> list[str]:
    """Figures in the text, years left out ('44', '380+', '50%')."""
    return [n for n in _NUMBER.findall(text or "") if not _YEAR.fullmatch(n)]


def fit_closer(answer: str, company: str = "") -> str:
    """The last sentence when it ties the candidate's work to the company's, else ''."""
    s = sentences(answer)
    if len(s) < 2:
        return ""
    last = s[-1]
    if _FIT_STRONG.search(last):
        return last
    if _FIT_WEAK.search(last) and (_FIT_CONTEXT.search(last) or (company and company.lower() in last.lower())):
        return last
    return ""


def trim_closer(answer: str, company: str = "") -> tuple[str, str]:
    """Cut a closing fit line, as the candidate does by hand. Only when two or more sentences are left."""
    closer = fit_closer(answer, company)
    if not closer or len(sentences(answer)) < 3:
        return answer, ""
    return answer.strip()[: answer.strip().rfind(closer)].rstrip(), closer


def _words(text: str) -> list[tuple[str, str]]:
    """(original, normalised) words, for spotting wording shared between answers."""
    out = []
    for w in (text or "").split():
        n = w.strip(".,;:!?\"'()[]").lower()
        if n:
            out.append((w, n))
    return out


def repeats(answer: str, others: list[str] | tuple = (), n: int = 6) -> list[str]:
    """Numbers and runs of `n` words this answer shares with the other answers for the same application."""
    if not others:
        return []
    other_numbers = {x for o in others for x in numbers(o)}
    found = [x for x in dict.fromkeys(numbers(answer)) if x in other_numbers and (len(x) > 1 or not x.isdigit())]
    other_runs = set()
    for o in others:
        w = [b for _, b in _words(o)]
        other_runs |= {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}
    mine = _words(answer)
    i = 0
    while i <= len(mine) - n and len(found) < 6:
        if " ".join(b for _, b in mine[i:i + n]) in other_runs:
            j = i + n  # extend to the whole shared run
            while j < len(mine) and " ".join(b for _, b in mine[j - n + 1:j + 1]) in other_runs:
                j += 1
            found.append(" ".join(a for a, _ in mine[i:j]).strip(".,;:!?\"'"))
            i = j
        else:
            i += 1
    return found


# --- candidate background ---------------------------------------------------

def fix_kerning(text: str) -> str:
    """Undo letter splits that PDF extraction makes in kerned (often LaTeX) resumes:
    'A WS' -> 'AWS', 'F rameworks' -> 'Frameworks', 'Y ouT ube' -> 'YouTube'."""
    text = re.sub(r"(?<![\w])([B-HJ-Z]) (?=[a-z]{2,})", r"\1", text)
    text = re.sub(r"(?<=[a-z])([A-Z]) (?=[a-z]{2,})", r"\1", text)
    text = re.sub(r"(?<![\w])([A-Z]) (?=[A-Z]{2,}\b)", r"\1", text)
    return text


def pdf_text(source) -> str:
    """The text of a PDF (a path or an open file). Raises on a file pypdf can't read."""
    from pypdf import PdfReader
    return fix_kerning("\n".join(page.extract_text() or "" for page in PdfReader(source).pages))


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
        try:
            text = pdf_text(str(path))
        except Exception:  # a damaged file fails in pypdf in many ways; the app then runs as if there were no resume
            text = ""
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
    own = re.sub(r"^#.*$", "", f"{notes_text()}\n{notes.text(load_config())}", flags=re.M).strip()
    return bool(resume_text(profile)) or len(own) > 200


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
    if own := notes_text():
        parts += ["## Notes from the candidate", own]
    if more := notes.text(load_config()):   # the files and pages under `notes` in config.yaml
        parts.append(more)
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


def answered_elsewhere(existing: dict[str, dict], skip: set[str] | frozenset = frozenset()) -> list[tuple[str, str]]:
    """(question, text) of the written answers this application already has (yours if you edited them), so new
    drafts don't repeat their stories. Short factual ones ("N/A", a city) say nothing to repeat and are left out."""
    out = []
    for k, a in existing.items():
        text = db.answer_text(a).strip()
        if k not in skip and len(text.split()) >= 15:
            out.append((a.get("question") or "", text))
    return out


def user_prompt(job: dict, description: str, questions: list[tuple[str, Field, str]],
                answered: list[tuple[str, str]] | None = None) -> str:
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
    ]
    if answered:
        lines += ["<already_answered>",
                  "Answers already written for this application. They are not facts; tell different stories.", ""]
        for q, text in answered[:8]:
            lines += [f"Q: {q.strip()[:200]}", f"A: {text[:700]}", ""]
        lines += ["</already_answered>", ""]
    lines += ["Draft an answer for every question below. Return one entry per id.", ""]
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
                 description: str = "", question_type: str = "", question: str = "", company: str = "",
                 others: list[str] | tuple = ()) -> dict:
    """Report for one answer; empty lists mean nothing suspicious was found.
    unsupported_*: claims about you not in your material. unsourced_points: "facts" about the company not in
    the posting. generic_phrases: stock wording recruiters read as filler or AI. addresses: the posting lines
    it answers (verified). Shape: fit_closer (a last line tying your work to theirs), self_verdicts ("I already
    ship..."), meta (talk of resumes or recruiters), stat_stacks / many_numbers (figures piled up where the
    question didn't ask for results), repeats (numbers or wording shared with `others`, the application's
    other answers)."""
    text = answer or ""
    points = [x for x in points or [] if x.strip()]
    sourced = [x for x in points if grounding.phrase_in(x, description)] if description else []
    open_question = question_type not in _PLAIN_TYPES
    count_numbers = open_question and not _ASKS_NUMBERS.search(question or "")
    n = len(numbers(text))
    return {
        "unsupported_facts": [f for f in facts if f.strip() and not grounding.phrase_in(f, source)],
        "unsupported_numbers": grounding.unsupported_numbers(text, source),
        "placeholders": grounding.placeholders(text),
        "generic_phrases": list(dict.fromkeys(m.group(0).lower() for m in GENERIC.finditer(text))),
        "em_dashes": text.count("\u2014"),
        "addresses": sourced,
        "unsourced_points": [x for x in points if x not in sourced] if description else [],
        "question_type": question_type,
        "fit_closer": fit_closer(text, company) if open_question else "",
        "self_verdicts": list(dict.fromkeys(m.group(0) for m in SELF_VERDICT.finditer(text))) if open_question else [],
        "meta": [] if META.search(question or "") else list(dict.fromkeys(m.group(0).lower() for m in META.finditer(text))),
        "stat_stacks": [s for s in sentences(text) if len(numbers(s)) >= 2] if count_numbers else [],
        "number_count": n,
        "many_numbers": count_numbers and n >= 3,
        "repeats": repeats(text, others) if open_question else [],
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


def _checks(a: dict, source: str, description: str, question: str = "", company: str = "",
            others: list[str] | tuple = ()) -> dict:
    c = check_answer(a["answer"].strip(), a.get("facts_used") or [], source, a.get("posting_points") or [],
                     description, a.get("question_type") or "", question, company, others)
    c["trimmed_closer"] = a.get("trimmed_closer") or ""
    return c


def _clip(text: str, n: int = 90) -> str:
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "..."


def problems(a: dict, limit: str, source: str, description: str, question: str = "", company: str = "",
             others: list[str] | tuple = ()) -> list[str]:
    """Rule breaks we can check without a person: each one triggers a single automatic rewrite."""
    c, out = _checks(a, source, description, question, company, others), []
    if (n := _length(a["answer"], limit)) and n[0] > n[1]:
        out.append(f"it is {n[0]} {limit.split()[-1]}, over the limit of {n[1]}; cut setup, keep the evidence")
    if c["unsupported_facts"]:
        out.append("these facts_used are not in the candidate background, so remove or correct those claims: "
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
    if c["fit_closer"]:
        out.append(f"its last sentence ties the candidate's work to the company's (\"{_clip(c['fit_closer'])}\"); "
                   "end on the last concrete fact instead")
    if c["self_verdicts"]:
        out.append(f"it judges the candidate instead of showing the work ({', '.join(c['self_verdicts'][:3])}); "
                   "say what they did and let the reader judge")
    if c["meta"]:
        out.append(f"it talks about the application instead of the work ({', '.join(c['meta'][:3])}); cut that")
    if c["stat_stacks"]:
        out.append(f"it packs several numbers into one sentence (\"{_clip(c['stat_stacks'][0])}\"); "
                   "keep the one that explains the most")
    elif c["many_numbers"]:
        out.append(f"it uses {c['number_count']} numbers; keep one, or two if the question asks about results")
    if c["repeats"]:
        out.append("it repeats " + ", ".join(f'"{_clip(x, 60)}"' for x in c["repeats"][:3])
                   + " from another answer for this application; use a different detail, or drop it")
    return out


# The rewrite fixes by cutting, never by adding: a rewrite asked to "sound more human" invents reactions,
# enthusiasm or humility, which is a tell of its own and breaks grounding.
REWRITE_RULES = ("Your previous drafts for these questions have problems. Rewrite each one so every listed problem "
                 "is gone and what was good stays. The fix is usually a cut: delete the sentence, number or phrase "
                 "rather than replacing it with something new. Add nothing the background doesn't contain: no new "
                 "facts, no reactions or enthusiasm, no humility phrases such as \"I'll admit\" or \"I'm still "
                 "learning\". If a cut leaves a gap the question needs filled, put a short [placeholder] there. "
                 "Every rule above still applies.")


def _trim(answers: dict[str, dict], company: str) -> None:
    """Cut closing fit lines in code, the way the candidate does by hand: no model call, nothing added."""
    for qid, a in list(answers.items()):
        text, cut = trim_closer(a["answer"], company)
        if cut:
            answers[qid] = {**a, "answer": text, "trimmed_closer": cut}


def _others(questions: list[tuple[str, Field, str]], answers: dict[str, dict], qid: str,
            earlier: list[str] | tuple) -> list[str]:
    """What an answer is read alongside: the application's earlier answers, then the ones drafted before it."""
    before = []
    for q, _, _ in questions:
        if q == qid:
            break
        if q in answers:
            before.append(answers[q]["answer"])
    return [*earlier, *before]


def _improve(system: str, prompt_for, questions: list[tuple[str, Field, str]], answers: dict[str, dict], cfg: dict,
             source: str, description: str, company: str = "", earlier: list[str] | tuple = ()) -> dict[str, dict]:
    """Cut closing fit lines, then one automatic rewrite of the answers that broke a checkable rule; a rewrite is
    kept only if it's better."""
    _trim(answers, company)

    def issues_of(a, qid, f, lim):
        return problems(a, lim, source, description, f.label, company, _others(questions, answers, qid, earlier))

    issues = {qid: issues_of(answers[qid], qid, f, lim) for qid, f, lim in questions if qid in answers}
    bad = [q for q in questions if issues.get(q[0])]
    if not bad:
        return answers
    notes = "\n".join(f"[{qid}] problems: {' | '.join(issues[qid])}\n<previous_draft>\n{answers[qid]['answer'].strip()}\n"
                      "</previous_draft>" for qid, _, _ in bad)
    retry = prompt_for(bad) + "\n\n" + REWRITE_RULES + "\n\n" + notes
    try:
        fixed = _run(system, retry, cfg)
    except llm.LLMError:
        return answers  # the first drafts stand, with their problems shown to you
    _trim(fixed, company)
    for qid, f, lim in bad:
        if qid in fixed and len(issues_of(fixed[qid], qid, f, lim)) < len(issues[qid]):
            answers[qid] = fixed[qid]
    return answers


# --- public -----------------------------------------------------------------

def draft_job(conn, cfg: dict, profile: Profile, job: dict, description: str, fields: list[Field],
              today: date, maxlengths: dict[str, int] | None = None, force: bool = False, log=print) -> dict:
    """Draft every not-yet-answered question in `fields` (one you wrote yourself is answered). Returns a status dict."""
    existing = db.answers_for(conn, job["id"])
    seen, unique = set(), []
    for f in fields:
        k, a = qkey(f.label), existing.get(qkey(f.label)) or {}
        if k not in seen and (force or not (a.get("answer") or a.get("final_answer") is not None)):
            seen.add(k)
            unique.append(f)
    if not unique:
        return {"status": "cached", "count": 0}
    questions = [(f"q{i}", f, length_limit(f, (maxlengths or {}).get(qkey(f.label))))
                 for i, f in enumerate(unique, 1)]
    answered = answered_elsewhere(existing, {qkey(f.label) for f in unique})
    earlier = [text for _, text in answered]
    system, user = system_prompt(profile), user_prompt(job, description, questions, answered)
    prompt_file = write_prompt_file(job, system, user, today)
    kind = llm.backend(cfg)
    if kind == "none":
        for _, f, _ in questions:
            db.save_answer(conn, job["id"], qkey(f.label), f.label, None, str(prompt_file), "none", "")
        return {"status": "prompt-only", "count": len(questions), "prompt_file": prompt_file}
    source, company = background(profile), job.get("company") or ""
    try:
        answers = _run(system, user, cfg)
        answers = _improve(system, lambda qs: user_prompt(job, description, qs, answered), questions, answers, cfg,
                           source, description, company, earlier)
    except llm.LLMError as exc:
        log(f"    ! drafting failed for {job['company']}: {exc}")
        for _, f, _ in questions:
            db.save_answer(conn, job["id"], qkey(f.label), f.label, None, str(prompt_file), kind, cfg["model"])
        return {"status": "error", "error": str(exc), "count": len(questions), "prompt_file": prompt_file}
    for qid, f, _ in questions:
        a = answers.get(qid)
        checks = _checks(a, source, description, f.label, company, _others(questions, answers, qid, earlier)) if a else None
        db.save_answer(conn, job["id"], qkey(f.label), f.label, a["answer"].strip() if a else None, str(prompt_file),
                       kind, cfg["model"], checks=checks)
    return {"status": "drafted", "count": sum(1 for q, _, _ in questions if q in answers), "prompt_file": prompt_file}


def redraft_answer(conn, cfg: dict, profile: Profile, job: dict, description: str, f: Field, instruction: str,
                   current: str, today: date) -> dict:
    """Redraft one answer, following your instruction ("lead with my internship, not the hackathon project").
    Same rules and checks as a first draft; your edits to it are replaced by the new draft."""
    key = qkey(f.label)
    answered = answered_elsewhere(db.answers_for(conn, job["id"]), {key})
    earlier = [text for _, text in answered]

    def prompt_for(qs):
        user = user_prompt(job, description, qs, answered)
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
    source, company = background(profile), job.get("company") or ""
    answers = _improve(system, prompt_for, questions, _run(system, user, cfg), cfg, source, description, company, earlier)
    a = answers.get("q1")
    if not a:
        raise llm.LLMError("Claude returned no answer")
    text, checks = a["answer"].strip(), _checks(a, source, description, f.label, company, earlier)
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
