"""Deterministic resume fit and eligibility (no model involved).

  - Hard blockers, each with the posting sentence that triggered it: graduate-students-only,
    3+ years of required experience, a graduation window that excludes yours, citizenship or
    clearance you don't have, no sponsorship when you need it, a GPA floor above yours,
    senior titles, an application deadline that has passed.
  - Timing and intent: a deadline within a week moves a posting up; a posting that says it isn't for an
    existing vacancy (Ontario postings must say so since 2026: a talent pool, not a job) moves down.
  - Skill overlap: a fixed vocabulary matched in the posting and in your resume + notes.

The LLM screen (screen.py) runs after this and can only add evidence it can quote.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from jobapps.config import Profile
from jobapps.rules import job_country, named_countries, sentences

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
_SOFT = re.compile(r"prefer|nice to have|bonus|\bplus\b|desir|ideal|is an asset|an advantage|beneficial", re.I)


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
    out, label = [], ""
    for kind, line in sections(description):
        if kind == "ignore":
            label = ""
            continue
        held, label = label, ""
        line = f"{held} {line.lstrip('- ')}".strip()
        if not held and line.endswith(":") and _DEADLINE.search(line):
            label = line  # "Application Deadline:" with its date on the next line is read as one sentence
            continue
        out += sentences(line)  # abbreviation-aware: "U.S. citizens" stays whole
    return out + ([label] if label else [])


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


_YEARS = re.compile(r"(\d+)\s*\+?\s*(?:(?:-|to|–)\s*\d+\s*)?years?\b(?!\s*(?:old|of age))[^.]{0,40}?\bexperience", re.I)
_AT_MOST = re.compile(r"(?:less than|fewer than|under|up to|no more than|maximum(?: of)?|at most|within)\s*$", re.I)
# "a firm with over 22 years of experience": the company's years, unless the sentence also speaks of you
_FIRM = re.compile(r"\b(?:firm|company|organi[sz]ation|business|leader|provider|founded|established|history|heritage"
                   r"|combined|collective)\b|\b(?:with|has|have|brings?) (?:over|more than|nearly|almost) \d", re.I)
_OF_YOU = re.compile(r"\b(?:you|your|candidates?|applicants?|must|requir\w+|minimum|at least|should|someone|looking for"
                     r"|seeking)\b", re.I)
_HYPOTHETICAL = re.compile(r"\bwould (?:typically |normally |usually )?(?:require|need|expect|ask for)\b", re.I)


def years_asked(sentence: str) -> int | None:
    """The years of experience a sentence asks of you, if it asks for any. Not an age ("18 years old"), a
    ceiling ("less than 3 years"), the employer's own history, or what the job "would require" of someone else."""
    m = _YEARS.search(sentence)
    if not m or _AT_MOST.search(sentence[:m.start()]) or _HYPOTHETICAL.search(sentence):
        return None
    if _FIRM.search(sentence) and not _OF_YOU.search(sentence):
        return None
    return int(m.group(1))


_GPA =re.compile(r"(?:minimum|min\.?|at least)[^.]{0,20}?\bgpa\b[^.\d]{0,15}(\d\.\d+)|\bgpa\b[^.\d]{0,15}(\d\.\d+)\s*(?:\+|or (?:higher|above|better))", re.I)
_VISA_WORDS = r"(?:(?:any|current|future|or|now|company|employer|visa|visas|employment|immigration|work|permit|h-?1b)\s+){0,4}"
_SPONSOR_NO = re.compile(
    # "we do not / cannot (provide) (visa) sponsor...", "is not currently sponsoring work permits", "will not be sponsored"
    r"(?:\bnot|\bunable to|\bcannot|\bno longer|n['’]t)\s+(?:be\s+)?(?:able to\s+)?(?:currently\s+|presently\s+)?"
    rf"(?:(?:provid|offer|support|consider|extend)\w*\s+)?{_VISA_WORDS}sponsor"
    # "sponsorship (for this role) is not available", "sponsorship or support is unavailable"
    r"|sponsorship(?:\s+(?:or|and|of|for|support|assistance|employment|visas?|work|permits?|is|are|will|would|currently"
    r"|this|the|role|position))*\s+(?:not\s+(?:be\s+)?|un)(?:available|provided|offered|possible|supported)"
    # "no visa sponsorship", "not eligible for immigration sponsorship"
    rf"|\bno\s+{_VISA_WORDS}sponsorship|\b(?:not eligible|ineligible)\s+for\s+(?:\S+\s+){{0,3}}?sponsorship"
    # "without (the need for) company sponsorship", "will not require visa sponsorship now or in the future"
    rf"|\bwithout\s+(?:the\s+)?(?:need\s+(?:for|of)\s+|needing\s+|requiring\s+)?{_VISA_WORDS}sponsorship"
    rf"|(?:\bnot|\bnever|n['’]t)\s+(?:now or in the future\s+)?(?:requir|need)\w*\s+{_VISA_WORDS}sponsorship"
    # "candidates who require sponsorship are not eligible", "not hiring individuals who require sponsorship"
    rf"|\b(?:requir|need)\w*\s+{_VISA_WORDS}sponsorship\b.{{0,200}}?\b(?:not eligible|ineligible|not be considered)"
    rf"|\bnot\s+(?:currently\s+)?(?:hiring|considering|accepting)\b.{{0,200}}?\b(?:requir|need)\w*\s+{_VISA_WORDS}sponsorship",
    re.I)
# "We aren't able to sponsor visas for every role": not a no
_SPONSOR_SOME = re.compile(r"\bevery\b|\ball (?:roles|positions|candidates)\b|\bsome (?:roles|positions)\b|case[- ]by[- ]case"
                           r"|\bmay not\b|\bmight not\b", re.I)
# Work authorization an employer can't give you, which comes to the same thing as no sponsorship
_OWN_AUTH = re.compile(r"\bunrestricted (?:work )?authori[sz]ation|\b(?:permanent|unrestricted|indefinite) (?:right|authori[sz]ation"
                       r"|eligibility) to work|authori[sz]ed to work\b.{0,60}?(?:\bfor any employer|\bindependently"
                       r"|\bwithout restrictions?|\bnow and in the future|\bon a permanent basis)", re.I)
_US_CITIZEN =re.compile(r"u\.?s\.? citizen|united states citizen|citizenship is required|must be a citizen|\bu\.?s\.? persons?\b"
                         r"|security clearance|secret clearance|clearance (?:is )?required|obtain (?:and maintain )?(?:a )?(?:security )?clearance|\bitar\b", re.I)
# Which country a posting sentence is about. "US" only as a name ("U.S.", "USA", "US"), never "must", "business",
# "status" or "contact us".
_IN_US = re.compile(r"\bU\.S\.A?\b|\bUSA?\b|(?i:united states|\bamerica\b)")
_IN_CANADA = re.compile(r"canada|canadian", re.I)


def _country(sentence: str) -> str | None:
    ca, us = bool(_IN_CANADA.search(sentence)), bool(_IN_US.search(sentence))
    return ("canada" if ca else "us") if ca != us else None


_NOT_NEEDED = re.compile(r"\bnot (?:be )?(?:a )?(?:required|requirement|needed|necessary|mandatory)\b"
                         r"|\bno (?:security )?clearance (?:is )?(?:required|needed)|\bregardless of\b|\bwithout regard to\b"
                         r"|:\s*(?:no|none|n/?a|not applicable)\s*\.?$", re.I)
_CLEARANCE_CUE = re.compile(r"must|requir|obtain|maintain|eligib|ability to|able to|active|current|hold|possess|need", re.I)


def _a_condition(rx: re.Pattern, sentence: str, soft: bool) -> bool:
    """Whether a sentence makes citizenship or a clearance a condition of the job: not "preferred" or "not
    required", not a heading whose value is on the next line, and not a clearance only named in passing."""
    hits = [m.group(0).lower() for m in rx.finditer(sentence)]
    if not hits or soft or sentence.rstrip().endswith(":") or _NOT_NEEDED.search(sentence):
        return False
    return any(h not in ("security clearance", "secret clearance") for h in hits) or bool(_CLEARANCE_CUE.search(sentence))


_US_ONLY = re.compile(r"u\.?s\.? citizen|united states citizen|\bu\.?s\.? persons?\b|\bitar\b", re.I)
_CA_STATUS = ("canada", "canadian", "canada pr", "permanent resident", "canadian citizen")
_CA_CITIZEN = re.compile(r"canadian citizens?(?:hip)? or (?:a )?permanent resident|must be (?:a )?canadian citizen", re.I)
_GRAD_ONLY = re.compile(r"\b(?:ph\.?d|doctoral|doctorate|master'?s|m\.?s\.? (?:degree|student|program)|graduate (?:student|degree|program))\b", re.I)
# A sentence about *when you graduate* (not just "undergraduate" / "graduate degree").
_GRAD_WHEN = re.compile(r"graduation (?:dates?|year|term|window|timeline)|graduat(?:e|ing)\s+(?:in|between|by|no|before|after|from|during|on|"
                        r"spring|summer|fall|winter|autumn|december|may|june)\b|(?:expected|anticipated|planned) graduation|class of\b"
                        r"|\bgraduating\b|\bgrad date\b", re.I)
_RETURNING = re.compile(r"return(?:ing)? to (?:school|university|college|campus|(?:your )?studies)|remaining (?:semester|term)", re.I)
_ENROLL = re.compile(r"pursuing|enrolled|candidate|student|required|must|currently", re.I)
_UNDERGRAD = re.compile(r"bachelor|baccalaur|undergrad|\bb\.?s\.?c?\b|\bba\b|\bbs/|\bbsc\b|\bb\.?a\.?sc\b|\bb\.?eng\b|\bbcs\b"
                        r"|\bbmath\b|\bb\.?tech\b|\bbcomp\b|or higher|or above|any level|all levels", re.I)


_DEADLINE = re.compile(r"\bdeadline\b|apply by|applications? (?:close|closes|are due|due|will be accepted until|must be (?:received|"
                       r"submitted) by)|closing date|posting (?:closes|close date|end date)|accepting applications until", re.I)
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE = re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d\d)\b|\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH},?\s+(20\d\d)\b"
                   r"|\b(20\d\d)-(\d\d)-(\d\d)\b", re.I)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_NOT_VACANCY = re.compile(r"\bnot\s+(?:for\s+|to fill\s+)?an?\s+(?:existing|current)\s+vacanc|\bis not (?:currently )?vacant"
                          r"|(?:posting|requisition|role|position) is (?:an? )?(?:evergreen|pipeline|general interest)\b", re.I)


def deadline(sentence: str) -> date | None:
    """The application deadline a sentence states ("Applications close October 15, 2026"), if any. Only a date
    that follows the deadline words counts: in "2026-10-26 Application Deadline: 2026-11-20" the first date is
    when the posting went up. Dates must carry a year, so "5/6" style dates and stray months are never guessed at."""
    found = []
    for words in _DEADLINE.finditer(sentence):
        for m in _DATE.finditer(sentence, words.end()):
            if m.start() - words.end() > 80:
                break
            try:
                if m.group(1):
                    found.append(date(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2))))
                elif m.group(4):
                    found.append(date(int(m.group(6)), _MONTHS[m.group(5)[:3].lower()], int(m.group(4))))
                else:
                    found.append(date(int(m.group(7)), int(m.group(8)), int(m.group(9))))
            except ValueError:
                continue
    return max(found) if found else None  # "extended from Oct 1 to Oct 15": the later one is when it closes


def _grad_ok(sentence: str, grad: int) -> bool | None:
    years = [int(y) for y in re.findall(r"\b(20[2-4]\d)\b", sentence)]
    if not years:
        return None
    s = sentence.lower()
    if re.search(r"(?:\bnot|\bnever|\bcannot|n['’]t)\b[^.;]{0,25}\bbefore\b", s):   # "must not graduate before December 2027"
        return grad >= min(years)
    if re.search(r"(?:\bnot|\bnever|\bcannot|n['’]t)\b[^.;]{0,25}\bafter\b", s):
        return grad <= max(years)
    if re.search(r"not? earlier than|or later|or after|\bafter\b|beyond|onward|at least", s):
        return grad >= min(years)
    if re.search(r"not? later than|or earlier|or before|\bbefore\b|\bby\b", s):
        return grad <= max(years)
    if len(years) >= 2 and re.search(r"between|through|\bto\b|[-–]", s):
        return min(years) <= grad <= max(years)
    return grad in years


def evaluate(job: dict, description: str, cand: Candidate, cfg: dict, today: date | None = None) -> FitResult:
    fcfg = cfg.get("fit", {})
    today = today or date.today()
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
        if (due := deadline(s)) is not None:
            if due < today:
                r.block(f"application deadline {due:%b %d, %Y} has passed", s)
            elif (due - today).days <= 7:
                r.points.append((10, f"deadline {due:%b %d}: apply soon"))
        if _NOT_VACANCY.search(s):
            r.points.append((-15, "says it isn't for an existing vacancy (a talent pool)"))
        main = re.sub(r"\([^)]*\)", " ", s)  # "(Masters: May 2028)" is an aside, not who the posting is for
        if _GRAD_ONLY.search(main) and _ENROLL.search(s) and not _UNDERGRAD.search(s) and not soft:
            r.block("graduate students only", s)
        if not soft and (n := years_asked(s)) is not None and not re.search(
                r"cours|academic|school|project|personal|universit|college|degree|stud(y|ies)|program|undergrad|education", low):
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
        where = _country(s) or job_country(job.get("locations") or [])
        citizens = _a_condition(_US_CITIZEN, s, soft)
        if citizens and where == "canada" and not _US_ONLY.search(s):
            # "eligible to obtain a Government of Canada security clearance": Canadian citizens and PRs can
            if cand.citizenship and not any(c in _CA_STATUS for c in cand.citizenship):
                r.block("Canadian citizenship / PR required (security clearance)", s)
        elif citizens:
            if "united states" in cand.citizenship or "us" in cand.citizenship:
                pass
            elif _IN_CANADA.search(s) and any(c in _CA_STATUS for c in cand.citizenship):
                r.warn("check the citizenship rule: it names Canada as well", s)  # "a list of countries, which includes Canada"
            elif cand.citizenship:
                r.block("U.S. citizenship / clearance required", s)
            else:
                r.warn("may need U.S. citizenship or clearance (set citizenship in profile)", s)
        if _a_condition(_CA_CITIZEN, s, soft):
            if cand.citizenship and not any(c in _CA_STATUS for c in cand.citizenship):
                r.block("Canadian citizenship / PR required", s)
        if (_SPONSOR_NO.search(s) or _OWN_AUTH.search(s)) and not s.rstrip().endswith("?"):  # a form question isn't a rule
            country = where  # word-bounded: "must" and "business" aren't "US"
            need = cand.needs_sponsorship.get(country) if country else None
            if not _country(s) and "other" in named_countries(s):
                pass  # "a permanent right to work in the UK": about an office somewhere else
            elif need is True and _SPONSOR_SOME.search(s):
                r.warn("may not sponsor a visa for this role", s)
            elif need is True:
                r.block(f"no visa sponsorship ({country.upper() if country == 'us' else country.title()})", s)
            elif need is None:
                r.warn("no sponsorship offered" + (" (set needs_sponsorship in profile)" if country else ""), s)

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
