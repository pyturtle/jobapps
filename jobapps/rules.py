"""Deterministic question -> value resolution.

Precedence for every field (first hit wins):
  1. profile.yaml `custom_answers`   - your own regex rules
  2. consent boxes                   - never ticked for you
  3. legal questions                 - work authorization and sponsorship from your profile, and only when the
                                       question asks one plain thing about one country (see "legal questions")
  4. learned_answers.yaml            - exact questions you answered by hand on an earlier form
  5. RULES below                     - built-in, ordered; matched against the question sentence
  6. free-text questions -> drafted answer; any other unknown field -> left for you (and learned)

Someone else's details (a reference, an emergency contact) are never filled with yours and never remembered.

No randomness anywhere: the same field + profile + job always resolves to the same value.
"""
from __future__ import annotations

import re
import unicodedata
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


_ABBREV = re.compile(r"\b(e\.g|i\.e|etc|vs|incl|approx|u\.s\.a|u\.s|a\.m|p\.m"
                     r"|(?:jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec)(?=\. \d))\.", re.I)


def sentences(text: str) -> list[str]:
    """Split on sentence ends, ignoring abbreviations (e.g., u.s., "Nov. 15") and anything inside parentheses."""
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
            starts = [opt for opt, n in zip(options, normed) if re.match(rf"{c}\b", n)]
            if len(starts) == 1:
                return starts[0]
            if starts:  # "Yes, with sponsorship" / "Yes, without sponsorship": which yes is yours to pick
                continue
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

_PROVINCE_CODES = "ON|BC|QC|AB|MB|SK|NS|NB|NL|PE"
_CANADA = re.compile(r"canada|canadian|toronto|vancouver|montr[eé]al|waterloo|ottawa|calgary|edmonton|mississauga"
                     r"|markham|brampton|kitchener|guelph|winnipeg|saskatoon|burnaby|oakville|kanata|gatineau"
                     r"|etobicoke|north york|richmond hill|vaughan|fredericton|moncton|kelowna|sherbrooke|ontario"
                     r"|british columbia|qu[eé]bec|alberta|manitoba|saskatchewan|nova scotia|newfoundland", re.I)
# "Toronto, ON", and Workday's "CA-ON-Ottawa"
_CA_PROVINCE = re.compile(rf"(?:,\s*|\bCA[-. ])(?:{_PROVINCE_CODES})\b")

# Places elsewhere that share a name or a two-letter code with Canadian ones.
_NOT_CANADA = re.compile(r"waterloo,\s*(?:ia|iowa)\b|vancouver,\s*(?:wa|washington)\b|london,\s*(?:uk|england|united kingdom)"
                         r"|windsor,\s*(?:ct|connecticut|uk)\b|hamilton,\s*(?:nj|oh|nz|bermuda)\b|kingston,\s*(?:ny|jamaica)\b"
                         r"|richmond,\s*(?:va|virginia|ca)\b|victoria,\s*(?:tx|australia)\b|markham,\s*(?:il|illinois)\b"
                         r"|richmond hill,\s*(?:ga|georgia)\b|^\s*ontario,\s*(?:ca|california|oh|ohio)\b"
                         r"|(?:amsterdam|rotterdam|eindhoven|utrecht),\s*nl\b|lima,\s*pe\b|bratislava,\s*sk\b", re.I)

# Georgia is left out (it is also a country), and so are DE and ID as bare codes (Germany, Indonesia).
_US_STATES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|hawaii|idaho|illinois"
              "|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi"
              "|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina"
              "|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee"
              "|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming")
_US_STATE_CODES = ("AL|AK|AZ|AR|CA|CO|CT|FL|GA|HI|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND"
                   "|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC")
_US_LOC = re.compile(rf"(?i:united states|\busa\b|\bu\.s\.|\b(?:{_US_STATES})\b|^(?:nyc|sf|la|south sf|dc)$)|\bUS\b"
                     rf"|[,.]\s*(?:{_US_STATE_CODES})\b|(?i:\b(?:wilmington|newark|new castle|dover)),\s*DE\b"
                     r"|(?i:\b(?:boise|meridian|nampa|idaho falls|pocatello|coeur d'alene)),\s*ID\b")

_COUNTRIES = (r"united kingdom|\bu\.?k\b|britain|england|scotland|wales|ireland|\bindia\b|germany|france|spain|italy"
              r"|netherlands|poland|portugal|sweden|norway|denmark|finland|switzerland|austria|belgium|czech|hungary"
              r"|romania|bulgaria|serbia|ukraine|israel|turkey|emirates|\buae\b|saudi|egypt|nigeria|kenya|south africa"
              r"|china|hong kong|taiwan|japan|korea|singapore|malaysia|thailand|vietnam|indonesia|philippines"
              r"|australia|new zealand|brazil|argentina|colombia|\bchile\b|\bperu\b|costa rica|(?<!new )mexico"
              r"|europe|\beu\b|\bemea\b|\bapac\b|\blatam\b|\basia\b|\bafrica\b|(?:north|south|latin|central) america"
              r"|middle east")
# Cities abroad whose two-letter code reads as a US state or a province ("Bangalore, IN", "Tel Aviv, IL").
_CITIES_ABROAD = (r"london(?!,\s*(?:on|ontario|ky)\b)|dublin(?!,\s*(?:ca|california|oh|ohio)\b)|berlin|munich|stuttgart"
                  r"|frankfurt|amsterdam|madrid|barcelona|lisbon|zurich|stockholm|warsaw(?!,\s*(?:in|indiana)\b)|prague"
                  r"|paris(?!,\s*(?:tx|texas|on|ontario)\b)|bangalore|bengaluru|hyderabad|\bpune\b|chennai|mumbai"
                  r"|gurgaon|gurugram|noida|new delhi|tel aviv|herzliya|haifa|jerusalem|jakarta|tokyo|seoul|shanghai"
                  r"|beijing|shenzhen|sydney(?!,\s*(?:ns|nova scotia)\b)|melbourne(?!,\s*(?:fl|florida)\b)|s[aã]o paulo"
                  r"|bogot[aá]|buenos aires")
_ELSEWHERE = re.compile(rf"(?i:{_COUNTRIES}|{_CITIES_ABROAD})|,\s*(?:UK|GB|IE|FR|ES|IT|PT|PL|CZ|RO|BG|RS|UA|AT|CH|SE|NO"
                        r"|DK|FI|BE|CN|JP|KR|SG|MY|HK|TH|VN|TW|AU|NZ|BR|MX)\b")

_US_NAMED = re.compile(r"united states|\bu\.s\b|\busa\b|\b(?:in|within) (?:the )?us\b"
                       r"|(?<!north )(?<!south )(?<!latin )(?<!central )\bamerican?\b", re.I)


def is_canadian(location: str) -> bool:
    loc = location or ""
    return bool(_CANADA.search(loc) or _CA_PROVINCE.search(loc) or re.search(r"\bCAN\b", loc)) and not _NOT_CANADA.search(loc)


_REGIONS = re.compile(r"\b(?:canada|united states(?: of america)?|usa|united kingdom|ontario|quebec|british columbia|alberta"
                      r"|manitoba|saskatchewan|nova scotia|new brunswick|newfoundland(?: and labrador)?|prince edward island)\b"
                      rf"|,\s*(?:{_US_STATES})\b")
_NOT_A_TOWN = {"remote", "hybrid", "virtual", "onsite", "site", "multiple", "various", "location", "locations", "office",
               "offices", "headquarters", "greater", "area", "region", "metro", "downtown", "can", "the", "and"}


def towns(location: str) -> frozenset[str]:
    """The words of a location that name its town, without the country, province or state: "Kanata, Ottawa, ON,
    Canada" -> {kanata, ottawa}, "CA-BC-VANCOUVER" -> {vancouver}. Empty when it names none ("Remote in Canada",
    "3 Locations", "SF"), so two postings can be told apart by city only when both say one."""
    text = unicodedata.normalize("NFKD", location or "").encode("ascii", "ignore").decode().lower()
    text = _REGIONS.sub(" ", re.sub(r"\bnyc\b", "new york", text))
    return frozenset(w for w in re.findall(r"[a-z]{3,}", text) if w not in _NOT_A_TOWN)


def named_countries(text: str, company: str = "") -> set[str]:
    """The countries a question names: canada, us, or "other" for anywhere else. The employer's own name is
    masked first (Bank of America, Air Canada), and a lower-case "us" is the pronoun unless it follows "in".
    Only the country itself counts: a question about "our Toronto office" is answered from the job's location."""
    if len(company.strip()) >= 2:
        text = re.sub(rf"(?<!\w){re.escape(company.strip())}(?!\w)", " ", text, flags=re.I)
    found = set()
    if re.search(r"\bcanad(a|ian)", text, re.I):
        found.add("canada")
    if _US_NAMED.search(text) or (text != text.upper() and re.search(r"\bUS\b", text)):
        found.add("us")
    if re.search(_COUNTRIES, text, re.I):
        found.add("other")
    return found


def detect_country(text: str) -> str | None:
    """canada or us when the text names that country and no other."""
    found = named_countries(text)
    return found.pop() if len(found) == 1 and "other" not in found else None


def _location_countries(location: str) -> set[str]:
    loc = location or ""
    if re.fullmatch(r"\s*ontario,\s*ca\s*", loc, re.I):  # the province, or the city in California
        return set()
    found = set()
    if is_canadian(loc):
        found.add("canada")
        # "Toronto, ON, CA": after a Canadian place that CA is the country, not California
        parts = [p.strip() for p in loc.split(",")]
        loc = ", ".join(p for i, p in enumerate(parts) if not (i and p == "CA" and (
            parts[i - 1] in _PROVINCE_CODES.split("|") or _CANADA.fullmatch(parts[i - 1]))))
    if _US_LOC.search(loc):
        found.add("us")
    if _ELSEWHERE.search(loc):
        found.add("other")
    return found


def job_country(locations: list[str]) -> str | None:
    """canada or us when every location is in that one country. "Remote (US or Canada)", "London, UK" and a
    place that can't be read all give None, so nothing is answered from a guess."""
    kinds = set()
    for loc in locations or []:
        found = _location_countries(loc)
        kinds.add(found.pop() if len(found) == 1 else "unknown")
    return kinds.pop() if len(kinds) == 1 and not kinds & {"unknown", "other"} else None


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


def leave_if_required(note: str) -> Handler:
    """Yours to answer when the form insists on it, otherwise left alone."""
    return lambda r, f, label: Resolution("manual", note=note) if f.required else Resolution("skip")


skip_unless_required = leave_if_required("required follow-up")
opt_in = leave_if_required("opt-in - your choice")
CONSENT_NOTE = "consent - read it and tick it yourself"
OTHER_PERSON_NOTE = "someone else's details - fill them in yourself"


def extra_info(r, f, label):
    return r.draft(f) if f.required else Resolution("skip")


def password(r, f, label):
    """The site's own password box, however it words its rules; "Which password manager do you use?" is a question."""
    q = focus(label)
    return None if q.endswith("?") or _QUESTION.search(q) else Resolution("skip")


_UNDER_18 = r"\b(under|younger than|below|less than)\b[^?]{0,20}\b(18|eighteen)\b|\ba minor\b"


def adult(r, f, label):
    """"Are you at least 18?" is yes, "Are you under 18?" is no."""
    return Resolution("fill", not re.search(_UNDER_18, focus(label)))


_ACCOMMODATION = r"\baccommodations?\b|\baccommodate (you|your|any)\b"


def accommodation(r, f, label):
    """Whether you need an accommodation is yours to say. A lead-in that only offers one doesn't count."""
    if not re.search(_ACCOMMODATION, focus(label)):
        return None
    return Resolution("manual", note="accommodation request - answer it yourself") if f.required else Resolution("skip")


def no_or(text: str) -> Handler:
    """A yes/no question gets No; a text box asking for details ("list any relatives who work here", "your
    login ID if you're a former employee, else N/A") gets `text`."""
    def handler(r, f, label):
        if f.type in ("text", "textarea") and not _YESNO.match(focus(label)):
            return Resolution("fill", text)
        return Resolution("fill", False)
    return handler


def current_job_box(r, f, label):
    return skip_unless_required(r, f, label) if f.type in ("boolean", "checkbox") else None


_PROVINCES = {"ontario", "quebec", "british columbia", "alberta", "manitoba", "saskatchewan", "nova scotia",
              "new brunswick", "newfoundland and labrador", "newfoundland", "prince edward island", "yukon",
              "northwest territories", "nunavut"}
_RESIDES = re.compile(r"^(are|do) you (currently )?(reside|residing|live|living|located|based) in (the )?"
                      r"((province|state|city|region|area) of )?(?P<place>[a-z][a-z .,'-]*?)\??$")


def resides_in(r, f, label):
    """"Are you currently residing in the province of Nova Scotia?" / "Are you located in Toronto, Ontario?",
    answered from where your profile says you live; a place it can't place is left for you."""
    m = _RESIDES.match(focus(label))
    if not m:
        return None
    home = {norm(str(v)) for v in (r.profile.get("address.city"), r.profile.get("address.region"),
                                   r.profile.get("address.country")) if v}
    home |= {norm(p) for p in str(r.profile.get("location") or "").split(",") if p.strip()}
    parts = [norm(p) for p in m.group("place").split(",") if p.strip()]
    if not home or not parts:
        return None
    if all(p in home for p in parts):
        return Resolution("fill", True, "location")
    if any(p in _PROVINCES and p not in home for p in parts):
        return Resolution("fill", False, "location")
    return Resolution("manual", source="location", note="can't tell if that's where you live")


def full_name(r, f, label):
    name = r.profile.full_name
    return Resolution("fill", name, "name") if name else Resolution("manual", note="set name.first / name.last")


def eeo(key: str) -> Handler:
    return lambda r, f, label: Resolution("fill", r.profile.get(f"eeo.{key}", "decline"), f"eeo.{key}")


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
    if f.type == "textarea":  # "Paste your resume" - but a question that mentions your resume is still a question
        q = focus(label)
        if q.endswith("?") or _QUESTION.search(q):
            return None
        return Resolution("manual", note="paste your resume text") if f.required else \
            Resolution("skip", note="resume is uploaded as a file")
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
        return Resolution("manual" if f.required else "skip", note="no skills.list in profile.yaml")
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
    # Honeypots ("for robots only"), one-time codes and the site's own controls: never filled, drafted or remembered.
    ("honeypot", r"for robots only|if you('| a)re (a )?human|leave this (field )?(blank|empty)", skip, True),
    ("password", r"\bpass(word|code|phrase)\b", password, True),
    ("captcha", r"captcha|i'?m not a robot|verification code|one[- ]time (pass)?code|\botp\b|\bdigit \d+ of\b", skip, True),
    ("site_control", r"^(keep me (signed|logged) in|remember me|stay signed in)$|^(search|search jobs|keywords?)$"
                     r"|^job title, skill", skip, False),
    ("followup", r"^(if|please specify|please explain)\b.*\b(other|not listed|yes|no|above|previous(ly)?|applicable|selected|so)\b"
                 r"|^if (you|your)\b.{0,80}\b(answered|responded|selected|chose|checked|referred|have been|are a)\b"
                 r"|^if (other|yes|no|so|applicable)\b|^other\b.{0,20}(specify|please)", skip_unless_required, True),
    # Opt-ins are never ticked for you. Not on text boxes: "Describe your marketing experience" is a question.
    ("marketing", r"sign up|newsletter|talent (community|network|pool)|keep me (informed|updated)|subscribe"
                  r"|stay (up to date|in touch|informed)|future (opportunities|openings|roles|positions)"
                  r"|\b(job|e-?mail|sms|text) alerts?\b|\balerts? (for|about|when)\b"
                  r"|marketing (e-?mails?|communications?|messages?|materials?|purposes|updates)", opt_in, False),
    ("job_alerts", r"job (recommendations|matches|alerts)|recommendations by e-?mail|receive (\w+ ){0,3}(e-?mails|newsletters)",
     opt_in, False),
    ("autofill_upload", r"auto-?fill|parse (my|your) resume|import (from|your) resume", skip, True),
    # A reference's or an emergency contact's details are theirs: never filled with yours, never remembered.
    ("other_person", r"\b(references?(?! (number|code|id|#))|referees?|emergency contact|next of kin)\b.{0,30}?"
                     r"\b(name|e-?mail|phone|number|title|company|relationship|address)\b"
                     r"|\b(supervisor|manager|guardian|parent|spouse|recruiter)('s)? (first |last |full )?(name|e-?mail|phone)\b"
                     r"|\b(name|e-?mail|phone)( number| address)? of (your |the |an? )?(reference|referee|supervisor|manager"
                     r"|emergency contact|guardian|parent|spouse)|^(emergency contact|next of kin)$",
     leave_if_required(OTHER_PERSON_NOTE), False),
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
    ("phone_extension", r"^(phone |telephone )?ext(ension)?\.?$|phone ext|extension (number|#)", skip, False),
    ("phone", r"phone|mobile|cell", P("phone"), False),
    ("linkedin", r"linkedin", P("links.linkedin", optional=True), False),
    ("github", r"github", P("links.github", optional=True), False),
    ("twitter", r"twitter|^x\b|\bx \(formerly", P("links.twitter", optional=True), False),
    ("website", r"portfolio|personal (web)?site|^website|web ?site( url)?$|\bblog\b|other (website|url|link)",
     P("links.website", optional=True), False),
    ("resides_in", _RESIDES.pattern, resides_in, False),
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
    # Before the education rules: your profile has your university, not your high school.
    ("high_school", r"high ?school|secondary school|\bcegep\b",
     leave("high school question - answer it once and it will be learned"), False),
    ("course", r"\bcourse\b(?! of study)|\bclass(es)? (you('ve| have)? )?(taken|completed)",
     leave("course question - answer it once and it will be learned"), False),
    ("enrolled", r"currently (enrolled|a student|attending|pursuing)|are you (currently )?(a )?(current )?(student|enrolled)"
                 r"|enrolled (in|at) (a|an)\b", P("education.currently_enrolled"), False),
    ("edu_start_month", r"^start (date )?month$", P("education.start_month"), False),
    ("edu_start_year", r"^start (date )?year$", P("education.start_year"), False),
    ("grad_month", r"(end|graduation|grad) (date )?month|month of graduation", P("education.graduation_month"), False),
    ("grad_year", r"(end|graduation|grad) (date )?year|year of graduation|graduating (class|year)|class of\b",
     P("education.graduation_year"), False),
    # "Level of education completed once you've graduated?" asks for the degree, not the date
    ("degree_level", r"level of education|education level|(highest|level of) degree", P("education.degree"), False),
    ("years_since_grad", r"how long ago.{0,40}graduat|(years|time) since (you )?graduat|how many years (ago )?(since|have you been) graduat",
     years_since_graduating, False),
    ("graduation", r"graduat|(degree|program) completion|complete your (degree|program|studies)|finish your (degree|studies)",
     graduation, False),
    ("year_of_study", r"year of study|(current|what) year (are you|of (your )?(study|program|school|degree))"
                      r"|academic (standing|level|year)|class (standing|level)", P("education.year_of_study"), False),
    ("gpa_other", r"\b(major|accounting|program|department|subject|technical|core|engineering|math(ematics)?|cs|course"
                  r"|semester|term)\b[^?.]{0,20}\b(gpa|grade point|average)\b", leave("asks for a GPA other than your overall one"),
     False),
    ("gpa", r"\bgpa\b|grade point|cumulative average|\bcgpa\b", P("education.gpa"), False),
    ("degree", r"^degree|degree (type|level|program|pursuing|sought)|(highest|level of) (level of )?education"
               r"|education level|what degree|type of degree", P("education.degree"), False),
    ("major", r"\bmajor\b|discipline|field of study|area of study|program of study|concentration|course of study",
     P("education.major"), False),
    ("minor", r"\bminor\b", P("education.minor", optional=True), False),
    ("school", r"school|university|college|institution|alma mater", P("education.school"), False),
    # Sponsorship, work authorization and immigration status are not here: see "legal questions" below.
    ("adult", r"(18|eighteen) years|at least 18|over (the age of )?18|\b18 (or|and) (older|over|above)|legal (working )?age"
              rf"|{_UNDER_18}", adult, False),
    ("former_employee", r"(previously|ever|formerly) (been )?(employed|worked)|former\b.{0,40}\b(employee|intern|contractor)"
                        r"|have you (ever )?worked (at|for)|worked (at|for) .{0,40} before", no_or("N/A"), True),
    ("relatives", r"\brelatives?\b|family members?\b.{0,40}(employ|work)|related to (anyone|an employee)", no_or("None"), True),
    ("referral", r"referred by|referr(al|er)('s)? name|name of (the )?(employee|person) who referred|who referred you"
                 r"|employee referral", P("preferences.referrer", optional=True), True),
    ("heard", r"(how|where) did you (first )?(hear|find|learn|come across|see|discover)\b.{0,12}\b(about )?(us|this|the (job|role|position|posting|opening|opportunity)|our)\b"
              r"|how did you hear|source of (this )?application|what brought you to|how were you referred|referral source",
     P("preferences.heard_about"), True),
    # Before on-site: "Do you need an accommodation to work on-site?" is not asking whether you'd come in.
    ("accommodation", _ACCOMMODATION, accommodation, True),
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
    # Only questions asking for the name itself ("Current employer", "Name of your current company"), not ones
    # that merely mention it ("notice period with your current employer").
    ("employer", r"^((your|name of (your )?)?(current|most recent|present) )?(company|employer)( name)?\??$"
                 r"|^(what is the )?name of your (current|most recent) (company|employer)|^who is your current employer",
     P("experience.current_company", optional=True), False),
    # A lone "Current role" checkbox is "I work here now" for an entry we can't see: yours to tick
    ("current_job_box", r"^(current|currently) (role|job|position|employer)$|i (currently|still) work here", current_job_box, False),
    ("job_title", r"^((your|current|most recent|present) )+(job )?(title|role|position)( title)?\??$|^(job )?title$",
     P("experience.current_title", optional=True), False),
    ("legal", r"non-?compete|non-?solicit|background check|convicted|criminal|felony|drug (test|screen)|export control"
              r"|\bitar\b|security clearance", leave("legal/eligibility question - answer it yourself"), True),
    ("consent", r"^i (hereby )?(agree|acknowledge|consent|certify|confirm|understand|have read|accept|declare|affirm"
                r"|authori[sz]e|verify|attest)|\bi hereby\b|privacy (policy|notice)|terms (and|&) conditions|\bconsent\b"
                r"|attest|acknowledg|by (checking|clicking|ticking|selecting|submitting|initial+ing|signing"
                r"|typing your (full )?name)|\b(certify|declare|affirm) that\b|^(applicant('s)? )?declaration\b"
                r"|e-?signature|electronic signature|^(your )?initials$", leave(CONSENT_NOTE), False),
    ("extra_info", r"additional (information|comments|info)|anything else|other information", extra_info, True),
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

# Never answered from memory and never remembered: your profile answers the first two, and the rest are yours to
# answer on each form (a legal question the profile can't answer, a consent box, someone else's contact details).
AUTHORITATIVE = ("needs_sponsorship", "work_authorization", "work_eligibility", "consent", "other_person")
_OTHER_PERSON_SECTION = re.compile(r"\b(references?|referees?|emergency|next of kin|guardian|parent|spouse|dependents?"
                                   r"|beneficiar)")
# Not questions at all: a skip from one of these stands, whatever else the label mentions.
FORM_MACHINERY = ("honeypot", "captcha", "site_control", "followup")

# One-line facts a form may still ask in a text box (see Resolver._rules).
SHORT_ON_TEXTAREA = {"pronouns", "preferred_name", "first_name", "middle_name", "last_name", "full_name", "email",
                     "phone", "linkedin", "github", "twitter", "website", "location", "city", "region", "country",
                     "postal", "school", "major", "degree", "degree_level", "grad_month", "grad_year", "graduation",
                     "year_of_study", "employer", "job_title", "open_locations", "start_date"}


_RULE = {name: (pattern, handler) for name, pattern, handler, _ in RULES}


def authoritative(source: str) -> bool:
    return (source or "").split(".")[0] in AUTHORITATIVE


def is_consent(label: str) -> bool:
    """Reads as a consent or attestation, whichever rule answers it ("I consent to receive emails and agree to the
    privacy policy" is answered by the job-alerts rule): never remembered from one form for another."""
    return bool(re.search(_RULE["consent"][0], norm(label)))


def rule_source(label: str, profile: Profile, job: dict | None = None) -> str:
    """Which built-in rule answers this question ('' if none): used to keep such answers out of memory. A legal
    question is the profile's when it asks one of its two facts about one named country, nobody's when it names no
    country (the answer depends on the job), and otherwise yours to answer once for that exact wording."""
    asks = legal(label, (job or {}).get("company") or "")
    if asks:
        if not asks.reusable:
            return "work_eligibility"
        return f"{asks.key}.{asks.country()}" if asks.key and asks.country() else ""
    res = Resolver(profile, job)._rules(Field("k", label, "text"), norm(label))
    return res.source if res else ""


_YESNO = re.compile(r"^(are|do|will|would|can|could|have|has|is|did|does|were|was|may)\b")
_QUESTION = re.compile(r"^(why|what|how|describe|tell (us|me)|explain|share|please (describe|explain|tell|share|provide|elaborate)"
                       r"|briefly|in (\d+|a few|one|two|three) (words|sentences)|give (us )?an example|walk us|if you could"
                       r"|which (project|experience)|have you ever|do you have (any )?experience)")


# --- legal questions --------------------------------------------------------
# Work authorization, sponsorship and immigration status. The profile holds two yes/no facts per country
# (work_authorization, needs_sponsorship), so it answers a question only when the question asks one of those two
# things and nothing more. Two questions in one, a condition ("without sponsorship", "for any employer"), a
# restriction, or which status you hold: those are left for you, and a wrong guess here is worse than a blank.

_SPONSOR = re.compile(r"sponsor")
_AUTH = re.compile(r"\b(authori[sz]ed|eligible|entitled|permitted|allowed|legally able|right|permission|authori[sz]ation"
                   r"|eligibility) to (legally |lawfully )?(work|be employed)|\b(legally|lawfully) (work|be employed)"
                   r"|\bwork legally|\bwork(ing)? (authori[sz]ation|eligibility|rights?)"
                   r"|\bemployment (authori[sz]ation|eligibility)|\b(authori[sz]ed|eligible) for employment")
_STATUS = re.compile(r"\bvisas?\b|h-?1b|\bimmigration\b|\b(work|study|employment|residence|co-?op|open|valid|student) permits?\b"
                     r"|\bpermits? to work|\bcitizen(ship)?\b|\bcitizens (or|and|of)\b|nationalit"
                     r"|permanent residen(t|cy|ce)(?! address)|green card|\b(legal|immigration|residency|citizenship) status"
                     r"|\bwork status in")
# Asks which country you belong to, which is the same on every form
_WHICH_COUNTRY = re.compile(r"\bnationalit(y|ies)\b|\bcountr(y|ies) of (citizenship|nationality|birth|origin)\b"
                            r"|\b(which|what) (other )?(countr(y|ies)|citizenships?|nationalit(y|ies))\b|^citizenships?$"
                            r"|\bcountr(y|ies)\b[^?.]{0,40}\b(citizen|national\b|permanent residen)|\bdual (citizen|national)")
_SPONSOR_PLAIN = re.compile(r"\b(requir|need)\w*\b[^?]{0,60}?\bsponsor|\bsponsor\w*\b[^?]{0,20}\b(required|needed|necessary)\b")
_EXAMPLES = re.compile(r"\((e\.g\.?|for example|for instance|such as)[^)]*\)")
_FRENCH = re.compile(r"[éèêàçùâîôûœ]|\b(vous|votre|est-ce)\b", re.I)
# Words that turn the question around ("Are you unable to work...", "exempt from needing sponsorship")
_INVERTED = re.compile(r"\b(lack|unable|ineligible|unauthori[sz]ed|exempt|waive[ds]?|no longer)\b|prohibit|prevent"
                       r"|\bbarr(ed|iers?)\b")
_AUTH_QUALIFIED = re.compile(
    r"\b(and|or|but|if|unless|except|other than)\b|sponsor"                                           # a second question
    r"|restrict|limit|condition|temporar|permanent|indefinite|expir|\bvalid|any employer|unrestricted"  # how far it goes
    r"|full[- ]time|part[- ]time|\bhours\b"
    r"|\b(live|living|reside|residing|residen\w*)\b|\byou(r| are| currently are)? (located|based)\b"  # where you live
    r"|\b18\b|eighteen|\bage\b|\bminor\b"
    r"|visa|\bpermits?\b|citizen|\bstatus\b|proof|document|evidence|verif|\bsin\b|\bssn\b|green card|passport"
    r"|\bstud(y|ies|ent)\b|h-?1b|\bopt\b|\bcpt\b"                                                       # which status
    r"|hybrid|remote|on-?site|in[- ]person|office|shift|weekend|overtime|schedule|travel")            # the arrangement
# An option that says more than yes or no ("Yes, with sponsorship", "No, I am a citizen")
_OPTION_QUALIFIED = {
    "needs_sponsorship": re.compile(r"authori|eligib|citizen|resident|permit|green card|status"
                                    r"|\b(but|if|only|until|except|unless|depending)\b"),
    "work_authorization": re.compile(r"sponsor|visa|permit|citizen|resident|restrict|condition|temporar|student|status"
                                     r"|green card|\b(but|if|only|until|except|unless|depending)\b"),
}
# Options that aren't an answer: "Select...", "N/A", "Not sure"
_NO_ANSWER = re.compile(r"^(-+|(please )?(select|choose)\b.*|n/?a|not applicable|not sure|unsure|maybe\b.*"
                        r"|i (do not|don't) know)?$")


@dataclass(frozen=True)
class Legal:
    key: str | None        # work_authorization / needs_sponsorship when the question asks that and nothing more
    countries: frozenset   # the countries it names: canada, us, other
    reusable: bool         # it names its country (or asks for one), so your answer holds wherever it is worded this way
    question: str

    def country(self) -> str | None:
        """canada or us when that is the only country named."""
        return next(iter(self.countries)) if len(self.countries) == 1 and "other" not in self.countries else None

    def takes_yes_no(self, f: Field) -> bool:
        """Whether a plain yes or no is a complete answer in this field."""
        if not self.key or f.type == "multiselect":
            return False
        if f.options and f.type in ("select", "radio", "buttons", "checkbox"):
            real = [n for n in map(norm, f.options) if not _NO_ANSWER.match(n) and not _DECLINE.search(n)]
            if len(real) != 2 or any(_OPTION_QUALIFIED[self.key].search(n) for n in real):
                return False
            yes, no = (sum(bool(re.match(rf"{word}\b", n)) for n in real) for word in ("yes", "no"))
            return (yes, no) == (1, 1) or ((yes, no) == (0, 0) and sum(bool(_NEGATION.search(n)) for n in real) == 1)
        if f.type in ("text", "textarea", "boolean", "checkbox"):
            return bool(_YESNO.match(re.sub(r"^[^:?]{1,40}:\s*", "", self.question)))
        return True  # a dropdown whose options aren't known yet: Yes/No is offered, and picked only if it's there


def _plain(q: str) -> str | None:
    """needs_sponsorship or work_authorization when `q` asks exactly that."""
    if _NEGATION.search(q) or _INVERTED.search(q):
        return None
    if _SPONSOR_PLAIN.search(q):
        at = q.index("sponsor")
        before = re.sub(r"\b((work|employment|immigration) )?visa $", "", q[:at])  # "need work visa sponsorship"
        if _AUTH.search(before) or re.search(r"citizen|permanent residen|green card|\bpermit|\bstatus\b|\bvisa\b", before):
            return None
        if re.search(r"\b(and|or|but) (are|do|have|can|is|will) you\b|other than|another|different|elsewhere|outside", q[at:]):
            return None
        return "needs_sponsorship"
    if _AUTH.search(q) and not _AUTH_QUALIFIED.search(q):
        return "work_authorization"
    return None


def legal(label: str, company: str = "") -> Legal | None:
    """What a label asks about work authorization, sponsorship or immigration status. None when it doesn't ask:
    a lead-in that only mentions it doesn't count ("We can't sponsor visas. When do you graduate?"), and the
    employer's own name is masked first (a company called Visa isn't a visa)."""
    # norm() without the lower-casing: "US" is the country and "us" is the employer
    text = re.sub(r"[✱*]+|\((required|optional)\)", " ", (label or "").replace("’", "'").replace(" ", " "), flags=re.I)
    if len(company.strip()) >= 2:
        text = re.sub(rf"(?<!\w){re.escape(company.strip())}(?!\w)", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" :.-–—\t")
    parts: list[str] = []
    for s in sentences(text):
        s = s.lstrip(" |/-–—")  # "Question en français ? | Question in English?"
        if parts and s.startswith("(") and s.endswith("?"):  # "...sponsorship? (H-1B, OPT)?" is one question
            parts[-1] = f"{parts[-1].rstrip('?')} {s}"
        elif s:
            parts.append(s)
    questions = [s for s in parts if s.endswith("?")]
    while questions and _TRAILING_FOLLOWUP.match(questions[-1].lower()):
        questions.pop()
    questions = questions or [text]
    asked = [q for q in questions if _SPONSOR.search(q.lower()) or _AUTH.search(q.lower()) or _STATUS.search(q.lower())]
    if not asked:
        return None
    question = " ".join(asked).lower()
    key = None
    # One question, apart from the same one in French beside it
    if len([q for q in questions if q in asked or not _FRENCH.search(q)]) == 1:
        q = re.sub(r"\(\s*(yes|y)\s*(/|or)\s*(no|n)\s*\)", " ", _EXAMPLES.sub(" ", question))
        q = re.sub(r"^if (you are |you're )?(hired|offered|selected|successful|employed)\b[^,?]{0,60},\s*", "", q.strip())
        key, question = _plain(q), q
    countries = named_countries(" ".join(asked)) or named_countries(text)
    return Legal(key, frozenset(countries), bool(countries) or bool(_WHICH_COUNTRY.search(question)), question)


class Resolver:
    def __init__(self, profile: Profile, job: dict | None = None, learned: list[dict] = (),
                 drafts: dict[str, str] | None = None):
        self.profile = profile
        self.job = job or {}
        self.learned = {qkey(e["label"]): e for e in learned}
        # The same question from another company ("...work for Acme?" / "...work for Initech?"), company name masked.
        # One company's answer may be about that company ("Are you a current Acme employee?"), so it carries over
        # only once two companies got the same answer from you and none got a different one.
        asked: dict[str, dict[str, dict]] = {}
        for e in learned:
            company = norm(e.get("learned_from") or "")
            masked = neutral(e["label"], company)
            if company not in ("", "you") and e.get("answer") is not None and masked != qkey(e["label"]):
                asked.setdefault(masked, {})[company] = e
        self.learned_any = {masked: next(iter(by.values())) for masked, by in asked.items()
                            if len(by) >= 2 and len({norm(display(e["answer"])) for e in by.values()}) == 1}
        self.drafts = drafts or {}

    def draft(self, f: Field) -> Resolution:
        answer = self.drafts.get(qkey(f.label))
        return Resolution("draft", answer, "draft", "" if answer else "no draft yet")

    def resolve(self, f: Field) -> Resolution:
        label = norm(f.label)
        if not label:
            return Resolution("manual", note="field has no label") if f.required else Resolution("skip")
        res = self._section(f, label) or self._custom(label)
        if res is not None:
            return self._fit(f, res)
        ruled = self._rules(f, label)
        if ruled is not None and ruled.kind == "skip" and ruled.source in FORM_MACHINERY:
            return ruled
        res = self._consent(f, label, ruled) or self._legal(f)
        if res is None:
            # Someone else's details and consent wording are yours each time: an answer remembered from another
            # form never stands in for those.
            if ruled is not None and (authoritative(ruled.source) or is_consent(label)):
                res = ruled
            else:
                res = self._learned(label) or ruled or self._fallback(f, label)
        return self._fit(f, res)

    def _consent(self, f: Field, label: str, ruled: Resolution | None) -> Resolution | None:
        """A box or a choice that reads as consent or an attestation is never ticked for you, whichever rule its
        other words would trigger ("I certify that I am at least 18"). An opt-in the form doesn't require stays
        skipped."""
        if f.type not in CHOICE_TYPES or not is_consent(label):
            return None
        if ruled is not None and ruled.kind == "skip":
            return ruled
        return Resolution("manual", source="consent", note=CONSENT_NOTE)

    def _section(self, f: Field, label: str) -> Resolution | None:
        if f.section and _OTHER_PERSON_SECTION.search(norm(f.section)):  # "References 1", "Emergency contact"
            return Resolution("manual" if f.required else "skip", source="other_person", note=OTHER_PERSON_NOTE)
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

    def _legal(self, f: Field) -> Resolution | None:
        """Sponsorship, work authorization and immigration status, before every other rule: "...require visa
        sponsorship to keep working after you graduate?" is a sponsorship question, not a graduation date. Filled
        from the profile only when it is one plain yes/no question about one country (see `legal`)."""
        asks = None if f.type == "file" else legal(f.label, self.job.get("company") or "")
        if asks is None:
            return None
        if asks.key and (asks.country() or not asks.countries):
            country = asks.country() or job_country(self.job.get("locations") or [])
            if not country:
                return Resolution("manual", source=asks.key, note="can't tell which country this asks about")
            key = f"{asks.key}.{country}"
            if not asks.takes_yes_no(f):
                return Resolution("manual", source=key, note="legal question - the choices say more than yes or no")
            value = self.profile.get(key)
            if value is None:
                return Resolution("manual", source=key, note=f"set {key} in profile.yaml")
            return Resolution("fill", value, key)
        if asks.reusable:  # it names the country, so what you answered to these exact words still holds
            return self._learned(norm(f.label)) or \
                Resolution("manual", note="legal question - answer it once and it will be learned")
        return Resolution("manual", source="work_eligibility", note="legal question - answer it yourself")

    def _rules(self, f: Field, label: str) -> Resolution | None:
        targets = [focus(label)]
        if targets[0] != label:
            targets.append(label)
        # Some forms use a text box for one-line facts ("What are your pronouns?"): answer those from the
        # profile instead of drafting them, but never a longer or open question.
        short = len(targets[0]) <= 60 and not re.search(r"comment|explain|describe|elaborate|\bwhy\b|tell us", label)
        for text in targets:
            for name, pattern, handler, on_textarea in RULES:
                if f.type == "textarea" and not on_textarea and not (short and name in SHORT_ON_TEXTAREA):
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
