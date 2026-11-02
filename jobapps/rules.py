"""Deterministic question -> value resolution.

Precedence for every field (first hit wins):
  1. profile.yaml `custom_answers`   - your own regex rules
  2. learned_answers.yaml            - exact questions you answered by hand on an earlier form
  3. RULES below                     - built-in, ordered; matched against the question sentence
  4. free-text questions -> drafted answer; any other unknown field -> left for you (and learned)

No randomness anywhere: the same field + profile + job always resolves to the same value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Callable

from jobapps.config import Profile
from jobapps.forms import Field

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
CHOICE_TYPES = {"select", "radio", "checkbox", "buttons", "boolean", "combobox", "dropdown", "multiselect"}


def norm(text: str) -> str:
    s = (text or "").lower().replace("’", "'").replace(" ", " ")
    s = re.sub(r"[✱*]+", " ", s)
    s = re.sub(r"\((required|optional)\)", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" :.-–—\t")


def qkey(label: str) -> str:
    """Key under which a drafted/learned answer for this question is stored."""
    return norm(label)[:300]


def neutral(label: str, company: str) -> str:
    """qkey with the company's name masked, so an answer carries over to the same question from another
    company. Only the name you applied to is masked (and its possessive); nothing else is fuzzy."""
    key = qkey(label)
    name = norm(company)
    if len(name) >= 2:
        key = re.sub(rf"(?<!\w){re.escape(name)}('s)?(?!\w)", "{company}", key)
    return key


_ABBREV = re.compile(r"\b(e\.g|i\.e|etc|vs|incl|approx|u\.s\.a|u\.s|a\.m|p\.m)\.", re.I)


def sentences(text: str) -> list[str]:
    """Split on sentence ends, ignoring abbreviations (e.g., u.s.) and anything inside parentheses."""
    protected = _ABBREV.sub(lambda m: m.group(0).replace(".", "\0"), text)
    out, depth, start = [], 0, 0
    for i, ch in enumerate(protected):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch in "?.!" and depth == 0 and (i + 1 == len(protected) or protected[i + 1] == " "):
            out.append(protected[start:i + 1].strip())
            start = i + 1
    if protected[start:].strip():
        out.append(protected[start:].strip())
    return [s.replace("\0", ".") for s in out]


_TRAILING_FOLLOWUP = re.compile(r"^(if (so|yes|no|not|applicable)|please (explain|elaborate|specify|describe)"
                                r"|why or why not|explain)\b")


def focus(label: str) -> str:
    """The actual question inside a multi-sentence label: its last sentence ending in '?',
    ignoring trailing follow-ups like 'If so, what capacity?'."""
    questions = [s for s in sentences(label) if s.endswith("?")]
    while len(questions) > 1 and _TRAILING_FOLLOWUP.match(questions[-1]):
        questions.pop()
    return questions[-1] if questions else label


@dataclass(frozen=True)
class MonthYear:
    year: int
    month: int

    def text(self) -> str:
        return f"{MONTHS[self.month - 1].title()} {self.year}"

    # Mid-month day: some date pickers shift a typed date by one day (time-zone bugs), and the
    # 15th stays in the right month either way. Only the month and year matter for graduation.
    DAY = 15

    def format_for(self, input_type: str = "", placeholder: str = "") -> str:
        if input_type == "date":
            return f"{self.year:04d}-{self.month:02d}-{self.DAY}"
        p = (placeholder or "").lower()
        if "mm/dd/yyyy" in p:
            return f"{self.month:02d}/{self.DAY}/{self.year}"
        if "dd/mm/yyyy" in p:
            return f"{self.DAY}/{self.month:02d}/{self.year}"
        if "mm/yyyy" in p:
            return f"{self.month:02d}/{self.year}"
        if "yyyy-mm" in p:
            return f"{self.year}-{self.month:02d}"
        return self.text()


def month_year(value: Any) -> MonthYear | None:
    """'June 2026', 'Jun 2026', '2026-06', '06/2026' -> MonthYear; anything else (e.g. 'present') -> None."""
    s = str(value or "").strip().lower()
    if m := re.fullmatch(r"([a-z]+)\.? (\d{4})", s):
        hit = [i for i, name in enumerate(MONTHS, 1) if name.startswith(m.group(1)[:3])]
        return MonthYear(int(m.group(2)), hit[0]) if hit else None
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", s):
        return MonthYear(int(m.group(1)), int(m.group(2)))
    if m := re.fullmatch(r"(\d{1,2})/(\d{4})", s):
        return MonthYear(int(m.group(2)), int(m.group(1)))
    return None


@dataclass
class Resolution:
    kind: str                # fill | file | draft | manual | skip
    value: Any = None
    source: str = ""
    note: str = ""
    overwrite: bool = False  # replace what's there (e.g. Workday's resume parser guessed this entry)
    many: bool = False       # the value is several separate picks (skills), not alternatives


def candidates(value: Any) -> list[str]:
    """All acceptable spellings of a value, in priority order."""
    if value is None:
        return []
    if isinstance(value, bool):
        return ["Yes" if value else "No"]
    if isinstance(value, MonthYear):
        return [value.text(), str(value.year)]
    if isinstance(value, (list, tuple)):
        return [c for v in value for c in candidates(v)]
    return [str(value)]


def display(value: Any) -> str:
    c = candidates(value)
    return c[0] if c else ""


_DECLINE = re.compile(r"decline|prefer not|don'?t wish|do not wish|not (to )?(say|answer|disclose|specify)"
                      r"|choose not|rather not|not wish to|wish not|(do not|don'?t) want to (answer|disclose|say|identify)")


_NEGATION = re.compile(r"\b(not|no|never|none|cannot|without)\b|n't")


def choose_option(value: Any, options: list[str]) -> str | None:
    """Map a value onto one of the offered options. Deterministic: candidate order first, then
    exact > yes/no prefix > shortest option containing the value as whole words > first in list."""
    if not options:
        return None
    normed = [norm(o) for o in options]
    for cand in candidates(value):
        c = norm(cand)
        if not c:
            continue
        if c == "decline":
            for opt, n in zip(options, normed):
                if _DECLINE.search(n):
                    return opt
            continue
        for opt, n in zip(options, normed):
            if n == c:
                return opt
        if c in ("yes", "no"):
            for opt, n in zip(options, normed):
                if re.match(rf"{c}\b", n):
                    return opt
            # Sentence options ("I am authorized to work..." / "I am not authorized..."): of exactly two
            # real answers, the negated one means no.
            pool = [(opt, n) for opt, n in zip(options, normed) if not _DECLINE.search(n)]
            negated = [bool(_NEGATION.search(n)) for _, n in pool]
            if len(pool) == 2 and sum(negated) == 1:
                return pool[negated.index(c == "no")][0]
            continue
        word = re.compile(rf"(?<!\w){re.escape(c)}(?!\w)")
        hits = [(len(n), i) for i, n in enumerate(normed)
                if word.search(n) or (len(n) >= 4 and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", c))]
        if hits:
            return options[min(hits)[1]]
    return None


# --- countries --------------------------------------------------------------

_CANADA = re.compile(r"canada|canadian|toronto|vancouver|montr[eé]al|waterloo|ottawa|calgary|edmonton"
                     r"|ontario|british columbia|quebec|alberta", re.I)
_CA_PROVINCE = re.compile(r",\s*(ON|BC|QC|AB|MB|SK|NS|NB|NL|PE)\b")
_US_TEXT = re.compile(r"united states|\bu\.s\.a?\b|\busa\b|\bus\b|\bamerica\b", re.I)
_US_LOC = re.compile(r"united states|\busa\b|\bu\.s\.|,\s*[A-Z]{2}$|^(NYC|SF|LA|South SF|DC)$", re.I)


# US/UK places that share a name with Canadian ones.
_NOT_CANADA = re.compile(r"waterloo,\s*(?:ia|iowa)\b|vancouver,\s*(?:wa|washington)\b|london,\s*(?:uk|england|united kingdom)"
                         r"|windsor,\s*(?:ct|connecticut|uk)\b|hamilton,\s*(?:nj|oh|nz|bermuda)\b|kingston,\s*(?:ny|jamaica)\b"
                         r"|richmond,\s*(?:va|virginia|ca)\b|victoria,\s*(?:tx|australia)\b", re.I)


def is_canadian(location: str) -> bool:
    loc = location or ""
    return bool(_CANADA.search(loc) or _CA_PROVINCE.search(loc) or re.search(r"\bCAN\b", loc)) and not _NOT_CANADA.search(loc)


def detect_country(text: str) -> str | None:
    ca, us = bool(_CANADA.search(text)), bool(_US_TEXT.search(text))
    if ca != us:
        return "canada" if ca else "us"
    return None


def job_country(locations: list[str]) -> str | None:
    kinds = set()
    for loc in locations or []:
        if is_canadian(loc):
            kinds.add("canada")
        elif _US_LOC.search(loc):
            kinds.add("us")
        else:
            kinds.add("unknown")
    return kinds.pop() if len(kinds) == 1 and kinds != {"unknown"} else None


# --- rule handlers ----------------------------------------------------------

Handler = Callable[["Resolver", Field, str], "Resolution | None"]


def P(key: str, optional: bool = False, fallback: str | None = None) -> Handler:
    """Fill from a profile key."""
    def handler(r: "Resolver", f: Field, label: str) -> Resolution:
        value = r.profile.get(key)
        if value is None and fallback:
            value = r.profile.get(fallback)
        if value is None:
            if optional and not f.required:
                return Resolution("skip", source=key, note=f"{key} is blank")
            return Resolution("manual", source=key, note=f"set {key} in profile.yaml")
        return Resolution("fill", value, source=key)
    return handler


def const(value: Any) -> Handler:
    return lambda r, f, label: Resolution("fill", value)


def leave(note: str) -> Handler:
    return lambda r, f, label: Resolution("manual", note=note)


def skip(r, f, label):
    return Resolution("skip")


def skip_unless_required(r, f, label):
    return Resolution("manual", note="required follow-up") if f.required else Resolution("skip")


def extra_info(r, f, label):
    return r.draft(f) if f.required else Resolution("skip")


def full_name(r, f, label):
    name = r.profile.full_name
    return Resolution("fill", name, "name") if name else Resolution("manual", note="set name.first / name.last")


def eeo(key: str) -> Handler:
    return lambda r, f, label: Resolution("fill", r.profile.get(f"eeo.{key}", "decline"), f"eeo.{key}")


def by_country(key: str) -> Handler:
    def handler(r: "Resolver", f: Field, label: str) -> Resolution:
        country = detect_country(label) or job_country(r.job.get("locations") or [])
        if not country:
            return Resolution("manual", source=key, note="can't tell which country this asks about")
        value = r.profile.get(f"{key}.{country}")
        if value is None:
            return Resolution("manual", source=f"{key}.{country}", note=f"set {key}.{country} in profile.yaml")
        return Resolution("fill", value, f"{key}.{country}")
    return handler


def location(r, f, label):
    values = [r.profile.get("location"), r.profile.get("address.city")]
    values = [v for v in values if v]
    return Resolution("fill", values, "location") if values else Resolution("manual", note="set location")


def graduation(r, f, label):
    year, month = r.profile.get("education.graduation_year"), r.profile.get("education.graduation_month")
    if year is None:
        return Resolution("manual", note="set education.graduation_year")
    m = MONTHS.index(str(month).lower()) + 1 if month and str(month).lower() in MONTHS else None
    if m is None and str(month or "").isdigit():
        m = int(month)
    if m is None:
        return Resolution("fill", str(year), "education.graduation_year")
    return Resolution("fill", MonthYear(int(year), m), "education.graduation")


def years_since_graduating(r, f, label):
    """'How long ago did you graduate?' while still in school: the not-yet-graduated option."""
    from datetime import date
    year = r.profile.get("education.graduation_year")
    if year is None:
        return Resolution("manual", note="set education.graduation_year")
    if int(year) < date.today().year:
        return Resolution("manual", note="you've graduated; answer this one yourself")
    return Resolution("fill", ["Not yet graduated", "Have not graduated", "I have not graduated", "Currently enrolled",
                               "Currently a student", "Still in school", "Student", "Not applicable", "N/A"], "education.graduation_year")


def term_available(r, f, label):
    """'Are you available ... in Summer 2027?' -> yes if that term is one you're targeting."""
    asked = re.search(r"(summer|fall|autumn|winter|spring) (20\d\d)", label)
    terms = [norm(t).replace("autumn", "fall") for t in candidates(r.profile.get("preferences.term"))]
    if not asked or not terms:
        return Resolution("manual", note="set preferences.term")
    wanted = f"{asked.group(1).replace('autumn', 'fall')} {asked.group(2)}"
    if wanted in terms:
        return Resolution("fill", True, "preferences.term")
    return Resolution("manual", source="preferences.term", note=f"{wanted} is not in preferences.term")


def resume(r, f, label):
    if f.type == "file":
        path = r.profile.file("files.resume")
        return Resolution("file", str(path), "files.resume") if path else \
            Resolution("manual", note="put your resume PDF in this folder and set files.resume")
    if f.type == "textarea":
        return Resolution("skip", note="resume is uploaded as a file")
    return None


def cover_letter(r, f, label):
    if f.type == "file":
        path = r.profile.file("files.cover_letter")
        if path:
            return Resolution("file", str(path), "files.cover_letter")
        return Resolution("manual", note="no cover letter file set") if f.required else Resolution("skip")
    if f.type == "textarea":
        return r.draft(f)
    return None


def skills(r, f, label):
    values = r.profile.get("skills.list")
    if not values:
        return Resolution("skip", note="no skills.list in profile.yaml")
    if f.type in ("text", "textarea"):
        return Resolution("fill", ", ".join(values), "skills.list")
    return Resolution("fill", list(values), "skills.list", many=True)


def today(r, f, label):
    from datetime import date
    return Resolution("fill", date.today(), "today") if f.type == "date" else None


def other_file(key: str) -> Handler:
    def handler(r, f, label):
        if f.type != "file":
            return None
        path = r.profile.file(key)
        if path:
            return Resolution("file", str(path), key)
        return Resolution("manual", note=f"set {key}") if f.required else Resolution("skip")
    return handler


# (name, pattern, handler, usable_on_textarea). Order matters: first match wins.
RULES: list[tuple[str, str, Handler, bool]] = [
    ("password", r"^((new|confirm|verify|re-?enter) )?password$", skip, True),
    ("captcha", r"captcha|i'?m not a robot", skip, True),
    ("followup", r"^(if|please specify|please explain)\b.*\b(other|not listed|yes|no|above|previous(ly)?|applicable|selected|so)\b"
                 r"|^if (you|your)\b.{0,80}\b(answered|responded|selected|chose|checked|referred|have been|are a)\b"
                 r"|^if (other|yes|no|so|applicable)\b|^other\b.{0,20}(specify|please)", skip_unless_required, True),
    ("marketing", r"sign up|newsletter|talent (community|network|pool)|keep me (informed|updated)|subscribe"
                  r"|stay (up to date|in touch|informed)|future (opportunities|openings|roles|positions)|\balerts?\b"
                  r"|marketing", skip, True),
    ("autofill_upload", r"auto-?fill|parse (my|your) resume|import (from|your) resume", skip, True),
    ("resume", r"resume|\bcv\b|curriculum vitae", resume, True),
    ("cover_letter", r"cover letter|motivation letter", cover_letter, True),
    ("transcript", r"transcript", other_file("files.transcript"), True),
    ("eeo_gender", r"\bgender\b|\bsex\b", eeo("gender"), False),
    ("eeo_transgender", r"transgender", eeo("transgender"), False),
    ("eeo_orientation", r"sexual orientation|\blgbt", eeo("sexual_orientation"), False),
    ("eeo_hispanic", r"hispanic|latin[oax]", eeo("hispanic"), False),
    ("eeo_race", r"\brace\b|ethnic", eeo("race"), False),
    ("eeo_veteran", r"veteran|military|armed forces", eeo("veteran"), False),
    ("eeo_disability", r"disabilit", eeo("disability"), False),
    ("eeo_age", r"age range|what is your age|\bage group", eeo("age"), False),
    ("has_preferred_name", r"i have a preferred name|use a (different|preferred) name", const(False), False),
    ("pronouns", r"pronoun", P("pronouns", optional=True), False),
    ("first_name", r"\b(first|given) name\b", P("name.first"), False),
    ("preferred_name", r"(preferred|chosen) (first )?name|nickname|name you (go by|prefer)",
     P("name.preferred", fallback="name.first"), False),
    ("middle_name", r"middle name", P("name.middle", optional=True), False),
    ("last_name", r"\b(last|family|sur) ?name\b|\bsurname\b", P("name.last"), False),
    ("full_name", r"^(your |full |legal |candidate |applicant )*name$|full (legal )?name", full_name, False),
    ("email", r"e-?mail", P("email"), False),
    ("phone_country", r"country code|phone country|country phone code|dialing code", P("address.country"), False),
    ("phone_type", r"(phone )?device type|phone type|type of phone", const(["Mobile", "Cell", "Personal Cell", "Cell Phone"]),
     False),
    ("phone_extension", r"\bextension\b|^ext\.?$", skip, False),
    ("phone", r"phone|mobile|cell", P("phone"), False),
    ("linkedin", r"linkedin", P("links.linkedin", optional=True), False),
    ("github", r"github", P("links.github", optional=True), False),
    ("twitter", r"twitter|^x\b|\bx \(formerly", P("links.twitter", optional=True), False),
    ("website", r"portfolio|personal (web)?site|^website|web ?site( url)?$|\bblog\b|other (website|url|link)",
     P("links.website", optional=True), False),
    ("location", r"^(current )?location( \(city\))?$|where (are you|do you) (currently )?(based|located|live|reside)"
                 r"|(?<!country of )current (city|location|residence)|city.{0,15}(state|province|country)|^location \(", location, False),
    ("city", r"^(current )?city( of residence)?$|^town", P("address.city"), False),
    ("region", r"^(state|province|region)\b|state ?/ ?province|province ?/ ?state", P("address.region"), False),
    ("country", r"^(current )?country( of residence)?$|country of (current )?residence|country (do )?you (live|reside)"
                r"|which country (do you live|are you)",
     P("address.country"), False),
    ("postal", r"\bzip\b|postal", P("address.postal_code"), False),
    ("street2", r"address line 2|\bline 2\b|apartment|\bsuite\b|\bunit\b", P("address.line2", optional=True), False),
    ("street", r"street|address line|^address( 1| line 1)?$|mailing address|home address", P("address.line1"), True),
    ("course", r"\bcourse\b(?! of study)|\bclass(es)? (you('ve| have)? )?(taken|completed)",
     leave("course question - answer it once and it will be learned"), False),
    ("enrolled", r"currently (enrolled|a student|attending|pursuing)|are you (currently )?(a )?(current )?(student|enrolled)"
                 r"|enrolled (in|at) (a|an)\b", P("education.currently_enrolled"), False),
    ("edu_start_month", r"^start (date )?month$", P("education.start_month"), False),
    ("edu_start_year", r"^start (date )?year$", P("education.start_year"), False),
    ("grad_month", r"(end|graduation|grad) (date )?month|month of graduation", P("education.graduation_month"), False),
    ("grad_year", r"(end|graduation|grad) (date )?year|year of graduation|graduating (class|year)|class of\b",
     P("education.graduation_year"), False),
    ("years_since_grad", r"how long ago.{0,40}graduat|(years|time) since (you )?graduat|how many years (ago )?(since|have you been) graduat",
     years_since_graduating, False),
    ("graduation", r"graduat|(degree|program) completion|complete your (degree|program|studies)|finish your (degree|studies)",
     graduation, False),
    ("year_of_study", r"year of study|(current|what) year (are you|of (your )?(study|program|school|degree))"
                      r"|academic (standing|level|year)|class (standing|level)", P("education.year_of_study"), False),
    ("gpa", r"\bgpa\b|grade point|cumulative average|\bcgpa\b", P("education.gpa"), False),
    ("degree", r"^degree|degree (type|level|program|pursuing|sought)|(highest|level of) (level of )?education"
               r"|education level|what degree|type of degree", P("education.degree"), False),
    ("major", r"\bmajor\b|discipline|field of study|area of study|program of study|concentration|course of study",
     P("education.major"), False),
    ("minor", r"\bminor\b", P("education.minor", optional=True), False),
    ("school", r"school|university|college|institution|alma mater", P("education.school"), False),
    ("sponsorship", r"sponsor|visa|h-?1b|immigration", by_country("needs_sponsorship"), False),
    ("work_auth", r"(authori[sz]ed|eligible|legally (able|permitted|entitled)|right) to work|work (authori[sz]ation|permit|eligibility)",
     by_country("work_authorization"), False),
    ("adult", r"(18|eighteen) years|at least 18|over (the age of )?18|legal (working )?age", const(True), False),
    ("former_employee", r"(previously|ever|formerly) (been )?(employed|worked)|former\b.{0,40}\b(employee|intern|contractor)"
                        r"|have you (ever )?worked (at|for)|worked (at|for) .{0,40} before", const(False), False),
    ("relatives", r"\brelatives?\b|family members?\b.{0,40}(employ|work)|related to (anyone|an employee)", const(False), False),
    ("referral", r"referred by|referr(al|er)('s)? name|name of (the )?(employee|person) who referred|who referred you"
                 r"|employee referral", P("preferences.referrer", optional=True), True),
    ("heard", r"(how|where) did you (first )?(hear|find|learn|come across|see|discover)\b.{0,12}\b(about )?(us|this|the (job|role|position|posting|opening|opportunity)|our)\b"
              r"|how did you hear|source of (this )?application|what brought you to|how were you referred|referral source",
     P("preferences.heard_about"), True),
    ("relocate", r"relocat", P("preferences.willing_to_relocate"), False),
    ("onsite", r"in[- ]person|on[- ]?site|in (the|our) office|hybrid|commut|work from (the|our) office",
     P("preferences.willing_onsite"), False),
    ("term_available", r"available\b.{0,60}\b(summer|fall|autumn|winter|spring) 20\d\d", term_available, False),
    ("open_locations", r"(which|what) (of the following |of these )?(locations?|offices?|cities|hubs)"
                       r"|open to (living|working|relocat)|preferred (work |office )?locations?|location preference",
     P("preferences.locations"), False),
    ("term", r"which (term|semester|season|session)|(internship|co-?op|work) (term|period|session|season)"
             r"|available (for|during) (the )?(summer|fall|winter|spring)|which (internship|co-?op)",
     P("preferences.term"), False),
    ("start_date", r"(earliest|available|availability).{0,30}start|when (can|could|would) you (start|begin)"
                   r"|desired start|^start date$|start date availability", P("preferences.start_date"), False),
    ("end_date", r"^end date$|(finish|end) date of (your )?(availability|internship)", P("preferences.end_date", optional=True), False),
    ("salary", r"salary|compensation expect|pay (expectation|range|rate)|hourly rate|desired (pay|wage|rate)|expected (pay|wage)",
     P("preferences.salary_expectation"), True),
    ("skills", r"^(your |technical |key )?skills?$|type to add skills|add (your )?skills", skills, True),
    ("signature_date", r"^(today'?s )?date$|^date signed$|signature date", today, False),
    ("employer", r"current (company|employer)|^(company|employer)( name)?$|most recent (employer|company)",
     P("experience.current_company", optional=True), False),
    ("job_title", r"current (job )?(title|role|position)|^(job )?title$", P("experience.current_title", optional=True), False),
    ("legal", r"non-?compete|non-?solicit|background check|convicted|criminal|felony|drug (test|screen)|export control"
              r"|\bitar\b|security clearance", leave("legal/eligibility question - answer it yourself"), True),
    ("consent", r"^i (agree|acknowledge|consent|certify|confirm|understand|have read|accept)|privacy (policy|notice)"
                r"|terms (and|&) conditions|\bconsent\b|attest|acknowledg", leave("consent - read it and tick it yourself"), False),
    ("extra_info", r"additional (information|comments|info)|anything else|other information|accommodat", extra_info, True),
]

# --- repeated sections (Workday's "Work Experience 2", "Education 1", "Websites 1") ---------
# The same label ("From", "Title", "URL") means a different thing in each block, so these fields
# are answered from the block they sit in, before any other rule.

SECTION_KINDS = [("work", r"work|employment|experience|job history"), ("education", r"education|school|academic"),
                 ("website", r"website|link|url|social")]

WORK_FIELDS = [
    ("current", r"currently (work|working|employed)|i (still )?work here|current (job|role|position|employer)$"),
    ("title", r"job title|^title$|position( title)?$|^role( title)?$"),
    ("company", r"company|employer|organi[sz]ation|business"),
    ("location", r"location|\bcity\b"),
    ("start", r"^(from|start)\b|start date|date from|^begin"),
    ("end", r"^(to|end)\b|end date|date to|^until"),
    ("description", r"description|responsibilit|summary|duties|achievement|details"),
]
EDU_FIELDS = [
    ("school", r"school|university|college|institution"),
    ("degree", r"degree|qualification|level of education"),
    ("major", r"field of study|major|discipline|area of study|program"),
    ("minor", r"\bminor\b"),
    ("gpa", r"overall result|gpa|grade|average"),
    ("start", r"^(from|start)\b|start date|first year|^begin"),
    ("end", r"^(to|end)\b|end date|last year|graduat|expected|completion"),
]


def section_of(text: str) -> tuple[str, int] | None:
    """'Work Experience 2' -> ('work', 2). Only numbered blocks count: they're the repeated ones."""
    m = re.fullmatch(r"(.*?)\s*\(?(\d+)\)?", norm(text))
    if not m:
        return None
    for kind, pattern in SECTION_KINDS:
        if re.search(pattern, m.group(1)):
            return kind, int(m.group(2))
    return None


def _work(r: "Resolver", f: Field, label: str, n: int) -> Resolution | None:
    jobs = r.profile.get("experience.history") or []
    if n > len(jobs):
        return Resolution("skip", note=f"you list {len(jobs)} jobs; this block is extra")
    job = jobs[n - 1]
    present = str(job.get("end") or "").strip().lower() in ("present", "now", "current", "")
    for key, pattern in WORK_FIELDS:
        if not re.search(pattern, label):
            continue
        src = f"experience.history[{n}].{key}"
        if key == "current":
            return Resolution("fill", present, src, overwrite=True)
        if key == "start" or key == "end":
            if key == "end" and present:
                return Resolution("skip", source=src, note="you still work here")
            when = month_year(job.get(key))
            return Resolution("fill", when, src, overwrite=True) if when else \
                Resolution("manual", source=src, note=f"write {src} like 'June 2026'")
        value = str(job.get(key) or "").strip()
        if not value:
            return Resolution("skip", source=src, note=f"{src} is blank")
        return Resolution("fill", value, src, overwrite=True)
    return None


def _education(r: "Resolver", f: Field, label: str, n: int) -> Resolution | None:
    if n > 1:
        return Resolution("skip", note="profile.yaml has one school; this block is extra")
    for key, pattern in EDU_FIELDS:
        if not re.search(pattern, label):
            continue
        if key == "start":
            year, month = r.profile.get("education.start_year"), r.profile.get("education.start_month")
            if year is None:
                return Resolution("manual", note="set education.start_year")
            m = month_year(f"{month} {year}") if month else None
            return Resolution("fill", m or str(year), "education.start", overwrite=True)
        if key == "end":
            res = graduation(r, f, label)
            res.overwrite = res.kind == "fill"
            return res
        res = P(f"education.{key}", optional=key in ("minor", "gpa"))(r, f, label)
        res.overwrite = res.kind == "fill"
        return res
    return None


def _website(r: "Resolver", f: Field, label: str, n: int) -> Resolution | None:
    if not re.search(r"url|website|link|address", label):
        return None
    links = [v for v in (r.profile.get(f"links.{k}") for k in ("linkedin", "github", "website", "twitter")) if v]
    if n > len(links):
        return Resolution("skip", note="no more links in profile.yaml")
    return Resolution("fill", links[n - 1], f"links ({n})", overwrite=True)


_SECTION_RESOLVERS = {"work": _work, "education": _education, "website": _website}

AUTHORITATIVE = ("needs_sponsorship", "work_authorization", "consent")


def authoritative(source: str) -> bool:
    return (source or "").split(".")[0] in AUTHORITATIVE


def rule_source(label: str, profile: Profile, job: dict | None = None) -> str:
    """Which built-in rule answers this question ('' if none): used to keep such answers out of memory."""
    res = Resolver(profile, job)._rules(Field("k", label, "text"), norm(label))
    return res.source if res else ""


_YESNO = re.compile(r"^(are|do|will|would|can|could|have|has|is|did|does|were|was|may)\b")
_QUESTION = re.compile(r"^(why|what|how|describe|tell (us|me)|explain|share|please (describe|explain|tell|share|provide|elaborate)"
                       r"|briefly|in (\d+|a few|one|two|three) (words|sentences)|give (us )?an example|walk us|if you could"
                       r"|which (project|experience)|have you ever|do you have (any )?experience)")


class Resolver:
    def __init__(self, profile: Profile, job: dict | None = None, learned: list[dict] = (),
                 drafts: dict[str, str] | None = None):
        self.profile = profile
        self.job = job or {}
        self.learned = {qkey(e["label"]): e for e in learned}
        # The same question from another company ("...work for Pebl?" / "...work for CIBC?"): company name masked.
        self.learned_any = {neutral(e["label"], e.get("learned_from") or ""): e for e in learned}
        self.drafts = drafts or {}

    def draft(self, f: Field) -> Resolution:
        answer = self.drafts.get(qkey(f.label))
        return Resolution("draft", answer, "draft", "" if answer else "no draft yet")

    def resolve(self, f: Field) -> Resolution:
        label = norm(f.label)
        if not label:
            return Resolution("manual", note="field has no label") if f.required else Resolution("skip")
        res = self._section(f, label) or self._custom(label)
        if res is None:
            ruled = self._rules(f, label)
            # Sponsorship / work authorization come from your profile and the job's country, and consent boxes
            # are yours to read each time: an answer remembered from another form never stands in for those.
            if ruled is not None and authoritative(ruled.source):
                res = ruled
            else:
                res = self._learned(label) or ruled or self._fallback(f, label)
        return self._fit(f, res)

    def _section(self, f: Field, label: str) -> Resolution | None:
        where = section_of(f.section) if f.section else None
        if not where:
            return None
        res = _SECTION_RESOLVERS[where[0]](self, f, label, where[1])
        if res and res.kind == "fill" and isinstance(res.value, bool) and f.type not in ("boolean", "checkbox", "radio",
                                                                                         "buttons", "select", "dropdown"):
            return None
        return res

    def _custom(self, label: str) -> Resolution | None:
        for rule in self.profile.custom_answers:
            try:
                hit = re.search(rule["match"], label, re.I)
            except re.error:
                continue
            if hit:
                if rule.get("answer") is None:
                    return Resolution("skip", source="custom_answers")
                return Resolution("fill", rule["answer"], "custom_answers")
        return None

    def _learned(self, label: str) -> Resolution | None:
        entry = self.learned.get(label) or self.learned_any.get(neutral(label, self.job.get("company") or ""))
        return Resolution("fill", entry["answer"], "learned", "you answered this before") \
            if entry and entry.get("answer") is not None else None

    def _rules(self, f: Field, label: str) -> Resolution | None:
        targets = [focus(label)]
        if targets[0] != label:
            targets.append(label)
        for text in targets:
            for name, pattern, handler, on_textarea in RULES:
                if f.type == "textarea" and not on_textarea:
                    continue
                if f.type == "file" and name not in ("autofill_upload", "resume", "cover_letter", "transcript"):
                    continue
                if not re.search(pattern, text):
                    continue
                res = handler(self, f, label)
                if res is None:
                    continue
                if isinstance(res.value, bool) and f.type in ("text", "textarea") and not _YESNO.match(focus(label)):
                    continue  # a yes/no rule fired on an open question; let it be drafted instead
                res.source = res.source or name
                return res
        return None

    def _fallback(self, f: Field, label: str) -> Resolution:
        if f.type == "file":
            return Resolution("manual", note="unknown upload") if f.required else Resolution("skip")
        q = focus(label)
        if f.type == "textarea" or (f.type == "text" and (q.endswith("?") or len(q) > 70 or _QUESTION.search(q))):
            return self.draft(f)
        if f.type in CHOICE_TYPES:
            return Resolution("manual", note="no rule - answer it once and it will be learned")
        if f.required:
            return Resolution("manual", note="no rule - answer it once and it will be learned")
        return Resolution("skip", note="optional, no rule")

    def _fit(self, f: Field, res: Resolution) -> Resolution:
        """Turn a raw value into exactly what this field can accept."""
        if res.kind != "fill":
            return res
        if f.type == "boolean":
            value = res.value
            if isinstance(value, str) and norm(value) in ("yes", "no", "true", "false"):
                value = norm(value) in ("yes", "true")
            if isinstance(value, bool):
                return replace(res, value=value)
            return Resolution("manual", source=res.source, note=f"expected yes/no, profile has '{display(value)}'")
        if f.options and f.type in ("select", "radio", "buttons", "checkbox"):
            if f.type == "checkbox":
                picked = []
                for cand in candidates(res.value):
                    opt = choose_option(cand, f.options)
                    if opt and opt not in picked:
                        picked.append(opt)
                if picked:
                    return replace(res, value=picked)
            else:
                opt = choose_option(res.value, f.options)
                if opt is not None:
                    return replace(res, value=opt)
            return Resolution("manual", source=res.source,
                              note=f"no option matches '{display(res.value)}'")
        return res
