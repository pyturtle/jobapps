"""Read application forms (questions, options, job description) without opening a browser.

Greenhouse, Lever and Ashby expose this through public endpoints. Other ATS are read from the
live page at fill time instead (see filler.py).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from html import unescape
from urllib.parse import unquote

from jobapps import net


class PostingClosed(Exception):
    pass


@dataclass
class Field:
    key: str
    label: str
    type: str                 # text | textarea | select | radio | checkbox | boolean | buttons | combobox | file | date
                              # | dropdown | multiselect (Workday-style widgets read live in the browser)
    required: bool = False
    options: list[str] = field(default_factory=list)
    description: str = ""
    section: str = ""         # heading of a repeated block, e.g. "Work Experience 2"; "" for ordinary fields

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Field":
        return cls(key=d["key"], label=d["label"], type=d["type"], required=bool(d.get("required")),
                   options=list(d.get("options") or []), description=d.get("description") or "",
                   section=d.get("section") or "")


@dataclass
class Form:
    ats: str
    fields: list[Field]
    description: str
    apply_url: str = ""       # direct form URL when the listing points at a wrapper page
    title: str = ""           # posting metadata straight from the ATS (used to verify discovered links)
    company: str = ""
    locations: list[str] = field(default_factory=list)
    posted: int = 0           # unix seconds, 0 if unknown


def _epoch(value) -> int:
    if not value:
        return 0
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 1e11 else int(value)
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


_SCRIPT = re.compile(r"(?is)<(script|style)\b.*?</\1>")


def html_to_text(html: str) -> str:
    if not html:
        return ""
    # Scripts go before entities are decoded (an escaped "</script>" inside one would end it early), and again
    # after for HTML that arrives escaped (Greenhouse).
    html = _SCRIPT.sub(" ", unescape(_SCRIPT.sub(" ", html)))
    html = re.sub(r"(?i)<li[^>]*>", "\n- ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|ul|ol)>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html)).replace("\r", " ")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"(?m)^-\n", "", text)  # list items with nothing in them (icon menus)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- Greenhouse -------------------------------------------------------------

_GH_TYPES = {
    "input_text": "text", "textarea": "textarea", "input_file": "file",
    "multi_value_single_select": "select", "multi_value_multi_select": "checkbox",
}


def _gh_field(q: dict, key: str | None = None) -> Field | None:
    first = (q.get("fields") or [{}])[0]
    ftype = _GH_TYPES.get(first.get("type", ""))
    if ftype is None:
        return None
    return Field(
        key=key or first.get("name", q.get("label", "")),
        label=q.get("label", "").strip(),
        type=ftype,
        required=bool(q.get("required")),
        options=[v.get("label", "") for v in first.get("values") or []],
        description=html_to_text(q.get("description") or ""),
    )


def _board_guesses(company: str) -> list[str]:
    """Greenhouse board tokens are usually the company name squashed: 'MongoDB' -> 'mongodb'."""
    base = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|technologies|labs)\b\.?", "", company.lower())
    words = re.findall(r"[a-z0-9]+", base)
    guesses = ["".join(words), "-".join(words), words[0] if words else ""]
    return [g for g in dict.fromkeys(guesses) if g]


def greenhouse(url: str, company: str = "") -> Form | None:
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w.-]+)/jobs/(\d+)", url)
    if m:
        boards, job = [m.group(1)], m.group(2)
    elif jid := re.search(r"[?&]gh_jid=(\d+)", url):
        boards, job = _board_guesses(company), jid.group(1)  # company-hosted wrapper page
    else:
        return None
    d = board = None
    for board in boards:
        try:
            d = net.get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}?questions=true")
            break
        except net.HttpError as exc:
            if exc.status != 404:
                raise
    if d is None:
        if m:
            raise PostingClosed(url)
        return None  # couldn't find the board; the page is read at fill time instead
    fields = [f for q in d.get("questions") or [] if (f := _gh_field(q))]
    for section in d.get("compliance") or []:
        fields += [f for q in section.get("questions") or [] if (f := _gh_field(q))]
    for q in (d.get("demographic_questions") or {}).get("questions") or []:
        fields.append(Field(
            key=f"demographic_{q.get('id')}", label=q.get("label", "").strip(),
            type="checkbox" if q.get("type") == "multi_value_multi_select" else "select",
            required=bool(q.get("required")),
            options=[o.get("label", "") for o in q.get("answer_options") or []],
        ))
    apply_url = "" if m else f"https://job-boards.greenhouse.io/{board}/jobs/{job}"
    return Form("greenhouse", fields, html_to_text(d.get("content", "")), apply_url,
                title=d.get("title", ""), company=d.get("company_name") or board,
                locations=[x for x in [(d.get("location") or {}).get("name")] if x],
                posted=_epoch(d.get("first_published") or d.get("updated_at")))


# --- Lever ------------------------------------------------------------------

def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}="([^"]*)"', tag)
    return unescape(m.group(1)) if m else None


def parse_lever_form(html: str) -> list[Field]:
    fields: list[Field] = []
    chunks = re.split(r'(?=<li class="application-question)', html)[1:]
    for chunk in chunks:
        chunk = chunk.split("</form>")[0]
        lab = re.search(r'<div class="application-label[^"]*">(.*?)</div>', chunk, re.S)
        raw_label = lab.group(1) if lab else ""
        label = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", raw_label))).replace("✱", "").strip()
        required = "✱" in raw_label or bool(re.search(r"<(input|textarea|select)[^>]*\brequired\b", chunk))
        if m := re.search(r"<textarea[^>]*>", chunk):
            fields.append(Field(_attr(m.group(0), "name") or label, label, "textarea", required))
            continue
        if m := re.search(r"<select[^>]*>(.*?)</select>", chunk, re.S):
            opts = [unescape(re.sub(r"<[^>]+>", "", o)).strip()
                    for v, o in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', m.group(1), re.S) if v]
            fields.append(Field(_attr(m.group(0), "name") or label, label, "select", required, opts))
            continue
        inputs = [t for t in re.findall(r"<input\b[^>]*>", chunk) if (_attr(t, "type") or "text") != "hidden"]
        if not inputs:
            continue
        kind = _attr(inputs[0], "type") or "text"
        name = _attr(inputs[0], "name") or label
        if kind in ("radio", "checkbox"):
            opts = [_attr(t, "value") or "" for t in inputs if _attr(t, "type") == kind]
            ftype = "boolean" if kind == "checkbox" and len(opts) == 1 and not label else kind
            fields.append(Field(name, label or opts[0], ftype, required, opts))
        elif kind == "file":
            fields.append(Field(name, label, "file", required))
        else:
            fields.append(Field(name, label, "text", required))
    return fields


def lever(url: str) -> Form | None:
    m = re.search(r"jobs\.(eu\.)?lever\.co/([^/]+)/([0-9a-f-]{36})", url, re.I)
    if not m:
        return None
    eu, company, posting = m.group(1) or "", m.group(2), m.group(3)
    try:
        d = net.get_json(f"https://api.{eu}lever.co/v0/postings/{company}/{posting}")
    except net.HttpError as exc:
        if exc.status == 404:
            raise PostingClosed(url) from None
        raise
    parts = [d.get("openingPlain"), d.get("descriptionBodyPlain") or d.get("descriptionPlain")]
    parts += [f"{x.get('text', '')}\n{html_to_text(x.get('content', ''))}" for x in d.get("lists") or []]
    parts.append(d.get("additionalPlain"))
    html = net.get_text(f"https://jobs.{eu}lever.co/{company}/{posting}/apply")
    cats = d.get("categories") or {}
    locs = cats.get("allLocations") or [cats.get("location")]
    return Form("lever", parse_lever_form(html), "\n\n".join(p.strip() for p in parts if p and p.strip()),
                title=d.get("text", ""), company=company.replace("-", " ").title(),
                locations=[x for x in locs if x], posted=_epoch(d.get("createdAt")))


# --- Ashby ------------------------------------------------------------------

_ASHBY_QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    id title descriptionHtml locationName
    applicationForm { sections { title fieldEntries { ... on FormFieldEntry { id field isRequired descriptionHtml } } } }
  }
}"""

_ASHBY_TYPES = {
    "String": "text", "LongText": "textarea", "Email": "text", "Phone": "text", "Number": "text",
    "File": "file", "Boolean": "boolean", "ValueSelect": "select", "MultiValueSelect": "checkbox",
    "Location": "combobox", "Date": "date", "SocialLink": "text",
}


def ashby(url: str) -> Form | None:
    m = re.search(r"ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url, re.I)
    if not m:
        return None
    org, posting = unquote(m.group(1)), m.group(2)
    d = net.post_json(
        "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting",
        {"operationName": "ApiJobPosting", "query": _ASHBY_QUERY,
         "variables": {"organizationHostedJobsPageName": org, "jobPostingId": posting}},
    )
    jp = (d.get("data") or {}).get("jobPosting")
    if not jp:
        raise PostingClosed(url)
    fields, seen = [], set()
    for section in (jp.get("applicationForm") or {}).get("sections") or []:
        for entry in section.get("fieldEntries") or []:
            f = entry.get("field") or {}
            ftype = _ASHBY_TYPES.get(f.get("type"))
            key = f.get("path") or entry.get("id")  # field ids can repeat across copied questions; paths don't
            if not ftype or f.get("isDeactivated") or key in seen:
                continue
            seen.add(key)
            fields.append(Field(
                key=key, label=(f.get("title") or "").strip(), type=ftype,
                required=bool(entry.get("isRequired")),
                options=[v.get("label", "").strip() for v in f.get("selectableValues") or []],
                description=html_to_text(entry.get("descriptionHtml") or ""),
            ))
    return Form("ashby", fields, html_to_text(jp.get("descriptionHtml") or ""),
                title=jp.get("title", ""), company=org.replace("-", " ").title(),
                locations=[x for x in [jp.get("locationName")] if x])


# --- Workday (description only; its questions are read live in the browser) -------

def workday(url: str) -> Form | None:
    from urllib.parse import urlparse
    p = urlparse(url)
    if not re.match(r"^[\w-]+\.wd\d+\.myworkdayjobs\.com$", p.netloc):
        return None
    parts = [x for x in p.path.split("/") if x]
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]
    if "job" not in parts or parts.index("job") < 1:
        return None
    i = parts.index("job")
    api = f"https://{p.netloc}/wday/cxs/{p.netloc.split('.')[0]}/{parts[i - 1]}/{'/'.join(parts[i:])}"
    for attempt in range(2):
        try:
            d = net.get_json(api, headers={"Accept": "application/json"})
            break
        except net.HttpError as exc:
            if exc.status in (404, 410):
                raise PostingClosed(url) from None
            if exc.status != 403 or attempt:
                raise
            time.sleep(3)  # Workday answers a burst of requests with a short-lived 403
    info = d.get("jobPostingInfo") or {}
    if not info or info.get("canApply") is False:
        raise PostingClosed(url)
    locations = [info.get("location")] + list(info.get("additionalLocations") or [])
    # ats "page": a description to judge fit by; the form itself is read live by the filler/extension
    return Form("page", [], html_to_text(info.get("jobDescription") or ""), title=info.get("title", ""),
                company=(d.get("hiringOrganization") or {}).get("name", ""), locations=[x for x in locations if x],
                posted=_epoch(info.get("startDate")))


# --- Oracle Cloud and Workable (descriptions only, from their public APIs) ---------

def oracle(url: str) -> Form | None:
    from urllib.parse import urlparse
    p = urlparse(url)
    m = re.search(r"/sites/([^/]+)/job/(\d+)", p.path)
    if not m:
        return None
    try:
        d = net.get_json(f"https://{p.netloc}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                         f"?expand=all&onlyData=true&finder=ById;Id=%22{m.group(2)}%22,siteNumber={m.group(1)}")
    except net.HttpError as exc:
        if exc.status in (404, 410):
            raise PostingClosed(url) from None
        raise
    items = d.get("items") or []
    if not items:
        raise PostingClosed(url)
    it = items[0]
    text = "\n\n".join(html_to_text(it.get(k) or "") for k in ("ExternalDescriptionStr", "ExternalQualificationsStr",
                                                                 "ExternalResponsibilitiesStr", "CorporateDescriptionStr"))
    locations = [it.get("PrimaryLocation")] + [x.get("Name") for x in it.get("secondaryLocations") or []]
    return Form("page", [], text.strip(), title=it.get("Title", ""), locations=[x for x in locations if x],
                posted=_epoch(it.get("ExternalPostedStartDate")))


def workable(url: str) -> Form | None:
    m = re.search(r"apply\.workable\.com/([^/]+)/j/([0-9A-Za-z]+)", url)
    if not m:
        return None
    try:
        d = net.get_json(f"https://apply.workable.com/api/v2/accounts/{m.group(1)}/jobs/{m.group(2)}")
    except net.HttpError as exc:
        if exc.status in (404, 410):
            raise PostingClosed(url) from None
        raise
    if d.get("state") not in (None, "published"):
        raise PostingClosed(url)
    text = "\n\n".join(html_to_text(d.get(k) or "") for k in ("description", "requirements", "benefits"))
    return Form("page", [], text.strip(), title=d.get("title", ""), posted=_epoch(d.get("published")))


# --- dispatch ---------------------------------------------------------------

READERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday, "oracle": oracle,
            "workable": workable}


def prefetch(listing: dict) -> Form | None:
    """Return the form if this ATS is readable, None if not. Raises PostingClosed when the job is gone."""
    if listing["ats"] == "greenhouse":
        return greenhouse(listing["url"], listing.get("company", ""))
    reader = READERS.get(listing["ats"])
    return reader(listing["url"]) if reader else None


_POSTING_MARKERS = [re.compile(p, re.I) for p in (
    r"qualifications?\b", r"requirements?\b", r"responsibilit", r"\byou will\b|\byou['’]ll\b", r"\bexperience (with|in)\b",
    r"\b(bachelor|degree|enrolled|pursuing)\b", r"\b(intern(ship)?|co-?op)\b",
    r"what you|about (the|this) (role|job|team)|who you are|about you")]


MAX_PAGE_TEXT = 40_000  # one posting runs to a few thousand characters; a page this long lists many jobs


def looks_like_posting(text: str) -> bool:
    """A whole page's text that includes one job description: not just menus and a cookie banner (checked on
    2026 pages: pages showing a posting hit 4-7 of these, shells that load it with JavaScript 0-3), and not a
    careers page carrying every opening, whose rules would be read as this job's. For page text only: a
    posting on its own (from an API or JobPosting data) can be terse and hit just 2."""
    return 600 < len(text or "") <= MAX_PAGE_TEXT and sum(1 for m in _POSTING_MARKERS if m.search(text)) >= 4


# What JSON is made of and prose isn't: "key": value, {"key", "value"}, }, and inside its strings a spelled-out
# backslash-n, backslash-quote or backslash-u2019
_DATA = re.compile(r'"\s*:\s*(?:["{\[\d]|true|false|null)|[{\[]\s*"|"\s*[}\]]|[}\]]\s*,', re.I)
_ESCAPE = re.compile(r'\\u[0-9a-f]{4}|\\n|\\"', re.I)


def without_data(text: str) -> str:
    """Page text without the data some sites keep in their markup (a hidden element holding 150,000 characters
    of settings, consent forms for every country and every other opening), which would be read as this job's rules."""
    return "\n".join(line for line in text.split("\n") if not _ESCAPE.search(line) and len(_DATA.findall(line)) < 3)


def page_text(html: str) -> str:
    """What a page shows: no menus, no embedded data."""
    return without_data(html_to_text(re.sub(r"(?is)<(nav|header|footer)\b.*?</\1>", " ", html)))


def jsonld_posting(html: str) -> str:
    """The schema.org JobPosting description many career sites embed for Google's job search
    (employers' own sites, Eightfold and Recruitee sites...), even when the page itself needs JavaScript."""
    for m in re.finditer(r"(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for x in items:
            if isinstance(x, dict) and "JobPosting" in str(x.get("@type")) and x.get("description"):
                return html_to_text(x["description"])
    return ""


def page_description(url: str) -> str:
    """Job description from the posting's own page; '' if the page only has it after running JavaScript."""
    try:
        html = net.get_text(url, timeout=20, retries=1)
    except net.HttpError:
        return ""
    if len(text := jsonld_posting(html)) >= 300:
        return text
    text = page_text(html)
    return text if looks_like_posting(text) else ""


def check_open(listing: dict) -> Form | None:
    """Prefetch if possible; otherwise just confirm the page still exists."""
    form = prefetch(listing)
    if form is None:
        try:
            net.request(listing["url"], timeout=20, retries=1)
        except net.HttpError as exc:
            if exc.status in (404, 410):
                raise PostingClosed(listing["url"]) from None
    return form
