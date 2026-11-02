"""Deterministic resume fit and eligibility (no model involved).

  - Hard blockers, each with the posting sentence that triggered it: graduate-students-only,
    3+ years of required experience, a graduation window that excludes yours, citizenship or
    clearance you don't have, no sponsorship when you need it, a GPA floor above yours,
    senior titles.
  - Skill overlap: a fixed vocabulary matched in the posting and in your resume + notes.

The LLM screen (screen.py) runs after this and can only add evidence it can quote.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobapps.config import Profile
from jobapps.rules import job_country, sentences

# canonical name -> regex. Patterns are case-insensitive unless the name is in CASE_SENSITIVE.
SKILLS: dict[str, str] = {
    "Python": r"\bpython\b", "Java": r"\bjava\b(?!script)", "C++": r"c\+\+|\bcpp\b", "C#": r"c#|\.net\b",
    "C": r"(?<![\w+#/.-])C(?![\w+#])(?=\s*(?:,|/|\band\b|\bor\b|programming|language))", "Go": r"\bGo(?:lang)?\b(?=\s*(?:,|/|\)|\band\b|\bor\b))|\bgolang\b",
    "Rust": r"\brust\b", "JavaScript": r"\bjavascript\b|\bjs\b", "TypeScript": r"\btypescript\b|\bts\b(?=\s*[,/)])",
    "Kotlin": r"\bkotlin\b", "Swift": r"\bswift\b", "Scala": r"\bscala\b", "Ruby": r"\bruby\b", "PHP": r"\bphp\b",
    "R": r"(?<![\w-])R(?![\w-])(?=\s*(?:,|/|\band\b|\bor\b|programming|\)))", "MATLAB": r"\bmatlab\b",
    "SQL": r"\bsql\b", "Bash": r"\bbash\b|shell scripting", "Haskell": r"\bhaskell\b", "OCaml": r"\bocaml\b",
    "Elixir": r"\belixir\b", "Dart": r"\bdart\b", "Assembly": r"\bassembly\b|\bx86\b|\barm64\b",
    "Verilog": r"\bverilog\b|\bvhdl\b|\bsystemverilog\b",
    "React": r"\breact(?:\.js|js)?\b(?! native)", "React Native": r"react native", "Next.js": r"\bnext\.?js\b",
    "Vue": r"\bvue(?:\.js)?\b", "Angular": r"\bangular\b", "Svelte": r"\bsvelte\b", "Node.js": r"\bnode(?:\.js|js)?\b",
    "Express": r"\bexpress(?:\.js)?\b(?= |,)", "Django": r"\bdjango\b", "Flask": r"\bflask\b", "FastAPI": r"\bfastapi\b",
    "Spring": r"\bspring(?: boot)?\b", "Rails": r"\brails\b", "GraphQL": r"\bgraphql\b", "REST APIs": r"\brest(?:ful)?\b",
    "HTML/CSS": r"\bhtml\b|\bcss\b", "Tailwind": r"\btailwind\b", "iOS": r"\bios\b", "Android": r"\bandroid\b",
    "Flutter": r"\bflutter\b",
    "Machine learning": r"machine learning|\bml\b", "Deep learning": r"deep learning|neural network|\bcnns?\b|transformers?\b",
    "Robotics": r"robotic|\bros2?\b|autonomous (?:vehicle|robot|system|driving)|path planning|\bslam\b|isaac (?:lab|sim)|manipulation",
    "AI agents": r"\bai agents?\b|agentic|agent sdk|\bmcp\b|tool[- ]use|llm agents?|multi-agent",
    "PyTorch": r"\bpytorch\b", "TensorFlow": r"\btensorflow\b|\bkeras\b", "JAX": r"\bjax\b", "scikit-learn": r"scikit|sklearn",
    "pandas": r"\bpandas\b", "NumPy": r"\bnumpy\b", "NLP": r"\bnlp\b|natural language",
    "Computer vision": r"computer vision|\bopencv\b|\byolo\w*|object detection|image (?:classification|segmentation)|perception",
    "LLMs": r"\bllms?\b|large language model|generative ai|\bgenai\b|prompt engineering|\brag\b|ai sdk|claude|openai|\bgpt|gemini",
    "Reinforcement learning": r"reinforcement learning|\brl\b|\bppo\b|policy (?:training|learning|gradient)",
    "Statistics": r"\bstatistic(?:s|al)\b|probability",
    "Data analysis": r"data analy(?:sis|tics)|\btableau\b|power bi", "Spark": r"\bspark\b|pyspark", "Airflow": r"\bairflow\b",
    "dbt": r"\bdbt\b", "Kafka": r"\bkafka\b", "Hadoop": r"\bhadoop\b",
    "PostgreSQL": r"postgre(?:s|sql)", "MySQL": r"\bmysql\b", "SQLite": r"\bsqlite\b", "MongoDB": r"\bmongo(?:db)?\b",
    "Redis": r"\bredis\b", "DynamoDB": r"dynamodb", "Elasticsearch": r"elasticsearch|opensearch", "Snowflake": r"\bsnowflake\b",
    "BigQuery": r"bigquery", "Databases": r"\bdatabases?\b|\bdbms\b",
    "AWS": r"\baws\b|amazon web services|\bec2\b|\bs3\b|\blambda\b", "GCP": r"\bgcp\b|google cloud", "Azure": r"\bazure\b",
    "Docker": r"\bdocker\b|container", "Kubernetes": r"kubernetes|\bk8s\b", "Terraform": r"terraform",
    "Linux": r"\blinux\b|\bunix\b", "Git": r"\bgit\b|github|gitlab", "CI/CD": r"ci/cd|continuous integration|github actions|jenkins",
    "gRPC": r"\bgrpc\b|protobuf", "Microservices": r"micro-?services", "Distributed systems": r"distributed systems?",
    "Operating systems": r"operating systems?|\bkernel\b", "Networking": r"\bnetworking\b|tcp/ip|\bnetwork protocols?",
    "Concurrency": r"concurren(?:cy|t)|multi-?thread|parallel programming", "Compilers": r"\bcompilers?\b",
    "Embedded": r"\bembedded\b|firmware|microcontroller|raspberry pi|arduino|\bqnx\b", "CUDA": r"\bcuda\b|\bgpu programming|tensorrt",
    "Unity": r"\bunity\b(?= |,)", "Supabase": r"\bsupabase\b|firebase",
    "Security": r"\bsecurity\b|cryptograph", "Data structures & algorithms": r"data structures|algorithms?",
    "OOP": r"object[- ]oriented|\boop\b",
    "Testing": r"unit tests?|testing|\bpytest\b|\bjest\b|\bjunit\b|playwright|cypress|selenium|end-to-end tests?|e2e",
    "Agile": r"\bagile\b|\bscrum\b", "Figma": r"\bfigma\b",
}
CASE_SENSITIVE = {"C", "Go", "R"}
_COMPILED = {name: re.compile(p, 0 if name in CASE_SENSITIVE else re.I) for name, p in SKILLS.items()}

_PREFERRED_HEAD = re.compile(r"prefer|nice[- ]to[- ]have|bonus|\bplus\b|good to have|desired|ideal|stand out|extra credit", re.I)
_REQUIRED_HEAD = re.compile(r"require|qualif|must|you have|you.?ll need|we.?re looking for|skills|about you|who you are|you bring|what we.?re looking", re.I)
_IGNORE_HEAD = re.compile(r"benefit|perks|compensation|salary|pay range|about us|about the company|who we are|equal opportunity|"
                          r"\beeo\b|diversity|accommodation|privacy", re.I)
_SOFT = re.compile(r"prefer|nice to have|bonus|\bplus\b|desir|ideal|is an asset|an advantage", re.I)


# Having the tool means having the field it belongs to (applied to your resume, not to postings).
IMPLIES = {
    "PyTorch": {"Machine learning", "Deep learning"}, "TensorFlow": {"Machine learning", "Deep learning"},
    "JAX": {"Machine learning", "Deep learning"}, "scikit-learn": {"Machine learning"},
    "Reinforcement learning": {"Machine learning"}, "Computer vision": {"Machine learning"},
    "Next.js": {"React", "JavaScript"}, "React Native": {"React", "JavaScript"}, "TypeScript": {"JavaScript"},
    "Supabase": {"PostgreSQL", "Databases"}, "PostgreSQL": {"SQL", "Databases"}, "MySQL": {"SQL", "Databases"},
    "SQLite": {"SQL", "Databases"},
}


def skills_in(text: str) -> set[str]:
    return {name for name, rx in _COMPILED.items() if rx.search(text or "")}


def with_implied(skills: set[str]) -> set[str]:
    out = set(skills)
    for s in skills:
        out |= IMPLIES.get(s, set())
    return out


@dataclass
class Candidate:
    skills: set[str]
    has_material: bool
    grad_year: int | None
    gpa: float | None
    citizenship: list[str]
    needs_sponsorship: dict[str, bool | None]
    degree_level: str = "bachelor"


def candidate(profile: Profile, background_text: str) -> Candidate:
    skills = with_implied(skills_in(background_text))
    skills |= {s for s in profile.get("skills.extra") or [] if s}
    skills -= {s for s in profile.get("skills.exclude") or []}
    year = profile.get("education.graduation_year")
    gpa = profile.get("education.gpa")
    try:
        gpa = float(gpa) if gpa is not None else None
    except (TypeError, ValueError):
        gpa = None
    citizenship = profile.get("citizenship") or []
    return Candidate(
        skills=skills, has_material=bool(background_text.strip()),
        grad_year=int(year) if str(year or "").isdigit() else None, gpa=gpa,
        citizenship=[c.lower() for c in (citizenship if isinstance(citizenship, list) else [citizenship])],
        needs_sponsorship={c: profile.get(f"needs_sponsorship.{c}") for c in ("us", "canada")},
    )


def sections(description: str) -> list[tuple[str, str]]:
    """Label each line of a posting as required / preferred / other / ignore, using its headings."""
    kind, out = "other", []
    for line in (description or "").splitlines():
        s = line.strip()
        if not s:
            continue
        keyword = _IGNORE_HEAD.search(s) or _PREFERRED_HEAD.search(s) or _REQUIRED_HEAD.search(s)
        is_heading = len(s) < 90 and not s.startswith("-") and (
            s.endswith(":") or (not re.search(r"[.!?;,]$", s) and (keyword or len(s.split()) >= 2)))
        if is_heading:
            if _IGNORE_HEAD.search(s):
                kind = "ignore"
            elif _PREFERRED_HEAD.search(s):
                kind = "preferred"
            elif _REQUIRED_HEAD.search(s):
                kind = "required"
            else:
                kind = "other"
        out.append((kind, s))
    return out


def _sentences(description: str) -> list[str]:
    out = []
    for kind, line in sections(description):
        if kind != "ignore":
            out += sentences(line.lstrip("- "))  # abbreviation-aware: "U.S. citizens" stays whole
    return out


@dataclass
class FitResult:
    eligible: bool = True
    points: list[tuple[int, str]] = field(default_factory=list)
    blockers: list[dict] = field(default_factory=list)   # {"reason", "quote"}
    warnings: list[dict] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    coverage: float | None = None

    @property
    def score(self) -> int:
        return sum(p for p, _ in self.points)

    def block(self, reason: str, quote: str):
        self.eligible = False
        self.blockers.append({"reason": reason, "quote": quote[:300]})

    def warn(self, reason: str, quote: str):
        self.warnings.append({"reason": reason, "quote": quote[:300]})

    def to_dict(self) -> dict:
        return {"eligible": self.eligible, "score": self.score, "points": [f"{p:+d} {w}" for p, w in self.points],
                "blockers": self.blockers, "warnings": self.warnings, "matched": self.matched,
                "missing": self.missing, "coverage": self.coverage}


_YEARS = re.compile(r"(\d+)\s*\+?\s*(?:(?:-|to|–)\s*\d+\s*)?years?\b[^.]{0,40}?\bexperience", re.I)
_GPA = re.compile(r"(?:minimum|min\.?|at least)[^.]{0,20}?\bgpa\b[^.\d]{0,15}(\d\.\d+)|\bgpa\b[^.\d]{0,15}(\d\.\d+)\s*(?:\+|or (?:higher|above|better))", re.I)
_SPONSOR_NO = re.compile(r"(?:not|unable to|cannot|can't|won'?t|will not|do not|does not)\s+(?:be able to\s+)?(?:provide|offer|support)?\s*"
                         r"(?:visa\s+|immigration\s+)?sponsor|sponsorship (?:is )?not (?:available|provided|offered)"
                         r"|without (?:the )?(?:need for |requiring )?(?:current or future |future )?(?:visa |employment )?sponsorship", re.I)
_US_CITIZEN = re.compile(r"u\.?s\.? citizen|united states citizen|citizenship is required|must be a citizen|\bu\.?s\.? persons?\b"
                         r"|security clearance|secret clearance|clearance (?:is )?required|obtain (?:and maintain )?(?:a )?(?:security )?clearance|\bitar\b", re.I)
_CA_CITIZEN = re.compile(r"canadian citizens?(?:hip)? or (?:a )?permanent resident|must be (?:a )?canadian citizen", re.I)
_GRAD_ONLY = re.compile(r"\b(?:ph\.?d|doctoral|doctorate|master'?s|m\.?s\.? (?:degree|student|program)|graduate (?:student|degree|program))\b", re.I)
# A sentence about *when you graduate* (not just "undergraduate" / "graduate degree").
_GRAD_WHEN = re.compile(r"graduation (?:dates?|year|term|window|timeline)|graduat(?:e|ing)\s+(?:in|between|by|no|before|after|from|during|on|"
                        r"spring|summer|fall|winter|autumn|december|may|june)\b|(?:expected|anticipated|planned) graduation|class of\b"
                        r"|\bgraduating\b|\bgrad date\b", re.I)
_RETURNING = re.compile(r"return(?:ing)? to (?:school|university|college|campus|(?:your )?studies)|remaining (?:semester|term)", re.I)
_ENROLL = re.compile(r"pursuing|enrolled|candidate|student|required|must|currently", re.I)
_UNDERGRAD = re.compile(r"bachelor|undergrad|\bb\.?s\.?c?\b|\bba\b|\bbs/|\bbsc\b|or higher|or above|any level|all levels", re.I)


def _grad_ok(sentence: str, grad: int) -> bool | None:
    years = [int(y) for y in re.findall(r"\b(20[2-4]\d)\b", sentence)]
    if not years:
        return None
    s = sentence.lower()
    if re.search(r"no earlier than|or later|or after|\bafter\b|beyond|onward|at least", s):
        return grad >= min(years)
    if re.search(r"no later than|or earlier|or before|\bbefore\b|\bby\b", s):
        return grad <= max(years)
    if len(years) >= 2 and re.search(r"between|through|\bto\b|[-–]", s):
        return min(years) <= grad <= max(years)
    return grad in years


def evaluate(job: dict, description: str, cand: Candidate, cfg: dict) -> FitResult:
    fcfg = cfg.get("fit", {})
    r = FitResult()
    if re.search(r"\b(senior|staff|principal|lead|manager|director)\b", job["title"], re.I) and \
            not re.search(r"intern|co-?op|student", job["title"], re.I):
        r.block("senior role", job["title"])
    if not description:
        r.points.append((int(fcfg.get("unknown_points", 8)), "no description to check fit against"))
        return r

    for s in _sentences(description):
        low = s.lower()
        soft = bool(_SOFT.search(s))
        if _GRAD_ONLY.search(s) and _ENROLL.search(s) and not _UNDERGRAD.search(s) and not soft:
            r.block("graduate students only", s)
        if not soft and (m := _YEARS.search(s)) and not re.search(r"cours|academic|school|project|personal", low):
            n = int(m.group(1))
            if n >= int(fcfg.get("max_years_required", 2)) + 1:
                r.block(f"asks for {n}+ years of experience", s)
            elif n >= 2:
                r.points.append((-8, f"asks for {n} years of experience"))
        has_year = bool(re.search(r"\b20[2-4]\d\b", s))
        if has_year and _GRAD_WHEN.search(low) and not re.search(r"full[- ]time|new grad(?:uate)? (?:role|position|program)", low):
            if cand.grad_year:
                if _grad_ok(s, cand.grad_year) is False:
                    r.block(f"graduation window excludes {cand.grad_year}", s)
            else:
                r.warn("check the graduation window (set education.graduation_year)", s)
        elif has_year and _RETURNING.search(low):
            back = min(int(y) for y in re.findall(r"\b(20[2-4]\d)\b", s))
            if cand.grad_year and cand.grad_year < back:
                r.block(f"must return to school in {back}; you graduate {cand.grad_year}", s)
        if (m := _GPA.search(s)) and not soft:
            floor = float(m.group(1) or m.group(2))
            if cand.gpa is not None and cand.gpa < floor:
                r.block(f"GPA floor {floor}", s)
            elif cand.gpa is None:
                r.warn(f"GPA floor {floor} (your GPA isn't set)", s)
        if _US_CITIZEN.search(s):
            if "united states" in cand.citizenship or "us" in cand.citizenship:
                pass
            elif cand.citizenship:
                r.block("U.S. citizenship / clearance required", s)
            else:
                r.warn("may need U.S. citizenship or clearance (set citizenship in profile)", s)
        if _CA_CITIZEN.search(s):
            if cand.citizenship and not any(c in ("canada", "canadian", "canada pr", "permanent resident") for c in cand.citizenship):
                r.block("Canadian citizenship / PR required", s)
        if _SPONSOR_NO.search(s):
            country = "canada" if re.search(r"canada", low) else "us" if re.search(r"u\.?s\.?|united states|america", low) \
                else job_country(job.get("locations") or [])
            need = cand.needs_sponsorship.get(country) if country else None
            if need is True:
                r.block(f"no visa sponsorship ({country.upper() if country == 'us' else country.title()})", s)
            elif need is None:
                r.warn("no sponsorship offered (set needs_sponsorship in profile)", s)

    secs = sections(description)
    pref_text = "\n".join(t for k, t in secs if k == "preferred")
    main_text = "\n".join(t for k, t in secs if k in ("required", "other"))
    required, preferred = skills_in(main_text), skills_in(pref_text) - skills_in(main_text)
    required -= {"Data structures & algorithms", "Testing", "Agile"} if len(required) > 4 else set()
    if not cand.has_material:
        r.points.append((int(fcfg.get("unknown_points", 8)), "add your resume to score skill fit"))
        return r
    r.matched = sorted(required & cand.skills) + sorted(preferred & cand.skills)
    r.missing = sorted(required - cand.skills)
    if required:
        r.coverage = len(required & cand.skills) / len(required)
        r.points.append((round(int(fcfg.get("skill_points", 30)) * r.coverage),
                         f"you have {len(required & cand.skills)}/{len(required)} of the skills it lists"))
    else:
        r.points.append((int(fcfg.get("unknown_points", 8)), "posting names no specific skills"))
    if preferred & cand.skills:
        r.points.append((min(8, 3 * len(preferred & cand.skills)), f"bonus skills: {', '.join(sorted(preferred & cand.skills))}"))
    return r
