"""Pull listings from public feeds and normalize them into one shape.

Every listing gets a stable id derived from its canonical application URL, so the same job
seen in two feeds (or on two days) is one row.
"""
from __future__ import annotations

import hashlib
import re
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from jobapps import net
from jobapps.config import atomic_write

API_ATS = ("greenhouse", "lever", "ashby")  # application questions are readable before opening the page

_TRACKING_KEYS = re.compile(r"^(utm_\w+|gh_src|lever-source|lever-origin|source|ref|src|trk)$", re.I)

_ATS_HOSTS = [
    ("greenhouse", r"greenhouse\.io$"),
    ("lever", r"lever\.co$"),
    ("ashby", r"ashbyhq\.com$"),
    ("workday", r"myworkday(jobs|site)\.com$"),
    ("smartrecruiters", r"smartrecruiters\.com$"),
    ("workable", r"workable\.com$"),
    ("icims", r"icims\.com$"),
    ("oracle", r"oraclecloud\.com$"),
    ("eightfold", r"eightfold\.ai$"),
    ("rippling", r"rippling\.com$"),
    ("jobvite", r"jobvite\.com$"),
    ("bamboohr", r"bamboohr\.com$"),
]


def canonical_url(url: str) -> str:
    """Strip tracking parameters and fragments; keep everything that identifies the job."""
    p = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING_KEYS.match(k)]
    deduped = list(dict.fromkeys(query))
    return urlunparse((p.scheme or "https", p.netloc, p.path, "", urlencode(deduped), ""))


def detect_ats(url: str) -> str:
    p = urlparse(url)
    host = p.netloc.lower().split(":")[0]
    if "gh_jid" in dict(parse_qsl(p.query)):
        return "greenhouse"
    for name, pattern in _ATS_HOSTS:
        if re.search(pattern, host):
            return name
    return "other"


def url_key(url: str) -> str:
    """Identity of a posting independent of tracking params, /apply suffixes and board hostnames."""
    p = urlparse(canonical_url(url))
    query = dict(parse_qsl(p.query))
    if "gh_jid" in query:
        return f"greenhouse:{query['gh_jid']}"
    if "greenhouse.io" in p.netloc and "token" in query and query["token"].isdigit():  # embedded form iframe
        return f"greenhouse:{query['token']}"
    if m := re.search(r"greenhouse\.io/.*?jobs/(\d+)", url):
        return f"greenhouse:{m.group(1)}"
    if m := re.search(r"lever\.co/[^/]+/([0-9a-f-]{36})", url, re.I):
        return f"lever:{m.group(1).lower()}"
    if m := re.search(r"ashbyhq\.com/[^/]+/([0-9a-f-]{36})", url, re.I):
        return f"ashby:{m.group(1).lower()}"
    if (m := re.match(r"([\w-]+)\.wd\d+\.myworkday(?:jobs|site)\.com$", p.netloc.lower())) and \
            (req := re.search(r"_([A-Za-z]*-?\d[\w-]*)(?:/apply(?:/[\w-]*)?)?/?$", p.path)):
        # same job with or without /en-US/, and on its /apply/applyManually wizard pages
        # "R-0000187114-1" is "R-0000187114" reposted on another of the company's career sites
        return f"workday:{m.group(1)}:{re.sub(r'(?<=\d)-\d{1,2}$', '', req.group(1).lower())}"
    if m := re.search(r"jobs\.smartrecruiters\.com/([^/]+)/(\d+)", url, re.I):
        return f"smartrecruiters:{m.group(2)}"
    host = p.netloc.lower().removeprefix("www.")
    path = re.sub(r"/(apply|application)/?$", "", p.path.rstrip("/"), flags=re.I).lower()
    return host + path + (f"?{p.query}" if p.query else "")


def posting_keys(url: str) -> set[str]:
    """Every identity one posting's pages share, for telling which job a page belongs to. Besides url_key: the job
    number a career site keeps when it hands you to its ATS (careers.x.com/jobs/91359?icims=1 and
    x.icims.com/jobs/91359/...; x.eightfold.ai/careers/job/446721828828 and careers.x.com/careers/apply?pid=446721828828)
    and Workable's two spellings (/x/j/9B3CB646E8 and /j/9B3CB646E8)."""
    keys = {url_key(url)}
    p = urlparse(url)
    query = dict(parse_qsl(p.query))
    if m := re.search(r"workable\.com/(?:[\w-]+/)?j/([0-9a-f]{6,})", url, re.I):
        keys.add(f"workable:{m.group(1).upper()}")
    if m := re.search(r"/careers/job/(\d{6,})", p.path):
        keys.add(f"eightfold:{m.group(1)}")
    if query.get("pid", "").isdigit():
        keys.add(f"eightfold:{query['pid']}")
    if (".icims.com" in p.netloc.lower() or "icims" in query) and (m := re.search(r"/jobs/(\d+)", p.path)):
        keys.add(f"icims:{m.group(1)}")
    return keys


def listing_id(url: str) -> str:
    return hashlib.sha1(url_key(url).encode("utf-8")).hexdigest()[:10]


def from_feed_item(item: dict, source: str) -> dict | None:
    """Normalize one entry of a Simplify-format listings.json (SimplifyJobs and vanshb03 share it)."""
    url, company, title = item.get("url"), item.get("company_name"), item.get("title")
    if not (url and company and title):
        return None
    url = canonical_url(url)
    terms = item.get("terms") or ([item["season"]] if item.get("season") else [])
    return {
        "id": listing_id(url),
        "source": source,
        "company": company.strip(),
        "title": re.sub(r"\s+", " ", title).strip(),
        "url": url,
        "ats": detect_ats(url),
        "locations": [l.strip() for l in item.get("locations") or [] if l and l.strip()],
        "terms": [t for t in terms if t and t != "N/A"],
        "category": item.get("category") or "",
        "sponsorship": item.get("sponsorship") or "",
        "degrees": item.get("degrees") or [],
        "date_posted": int(item.get("date_posted") or 0),
        "active": bool(item.get("active")) and bool(item.get("is_visible", True)),
    }


def _prefer(a: dict, b: dict) -> dict:
    """Pick one listing when two feeds carry the same job. Deterministic: active, then API ATS, then source name."""
    def rank(l):
        return (not l["active"], l["ats"] not in API_ATS, l["source"])
    keep, other = sorted((a, b), key=rank)
    merged = dict(keep)
    merged["active"] = a["active"] or b["active"]
    dates = [d for d in (a["date_posted"], b["date_posted"]) if d]
    merged["date_posted"] = min(dates) if dates else 0
    merged["category"] = keep["category"] or other["category"]
    merged["terms"] = list(dict.fromkeys(keep["terms"] + other["terms"]))  # keep e.g. a "16 months" tag from either feed
    return merged


# --- short links (vanshb03 now publishes go.vansh.nyc redirects) ---------------------

SHORT_LINK = re.compile(r"^https?://(?:go\.vansh\.nyc|bit\.ly|tinyurl\.com)/", re.I)


def _redirect_target(url: str) -> str | None:
    import http.client
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    req = urllib.request.Request(url, headers={"User-Agent": net.UA})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as resp:
            return resp.headers.get("Location")
    except urllib.error.HTTPError as exc:
        return exc.headers.get("Location") if 300 <= exc.code < 400 else None
    except (OSError, http.client.HTTPException, ValueError):
        return None


def resolve_short_links(urls: list[str]) -> dict[str, str]:
    """short URL -> real ATS URL, cached on disk so each link is looked up once."""
    import json
    from concurrent.futures import ThreadPoolExecutor
    from jobapps.config import DATA_DIR
    cache_file = DATA_DIR / "redirects.json"
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    todo = sorted({u for u in urls if u not in cache})
    if todo:
        with ThreadPoolExecutor(max_workers=16) as pool:
            for short, target in zip(todo, pool.map(_redirect_target, todo)):
                if target and target.startswith("http"):
                    cache[short] = target
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(cache_file, json.dumps(cache, indent=0, sort_keys=True))
    return cache


# --- other feed formats ---------------------------------------------------------------

def _strip(cell: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", cell or ""))).strip()


def _item(company: str, title: str, url: str, location: str, posted: int, terms: list[str]) -> dict:
    return {"company_name": company, "title": title, "url": url, "locations": [location] if location else [],
            "terms": terms, "active": True, "is_visible": True, "date_posted": posted,
            "category": "", "sponsorship": "", "degrees": []}


def _canada(location: str) -> str:
    from jobapps.rules import is_canadian
    return location if is_canadian(location) else f"{location}, Canada".lstrip(", ")


def _days_ago(days: int, today=None) -> int:
    """The start of the day `days` ago, in unix seconds. A site that only says "3 days ago" gives every posting
    from that day the same date, so their order doesn't depend on the second each board was read."""
    from datetime import date, datetime, timedelta
    return int(datetime.combine((today or date.today()) - timedelta(days=days), datetime.min.time()).timestamp())


def parse_francklin(md: str) -> list[dict]:
    """Francklin9999/canada-internship INTERNSHIPS.md: the open 'Tech internships' table only.
    Row: | Company | Role | Location | Details | Posted | Apply | <!--id:N-->"""
    from datetime import datetime
    start = md.find("## Tech internships")
    end = md.find("## Other internships", start)
    out = []
    for line in md[start:end if end > 0 else None].splitlines():
        if not line.startswith("|") or "<!--id:" not in line:
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:7]]
        if len(cells) < 6:
            continue
        company, role, location, details, posted, apply = cells
        link = re.search(r'href="([^"]+)"', apply)
        if not link or "linkedin.com" in link.group(1):
            continue  # LinkedIn-only rows can't be read or autofilled
        title = _strip(re.sub(r"^\s*NEW\s+", "", role).replace("\\|", "|"))
        when = re.match(r"\s*([A-Z][a-z]{2} \d{1,2}, \d{4})", posted)
        ts = int(datetime.strptime(when.group(1), "%b %d, %Y").timestamp()) if when else 0
        tags = _strip(details)
        terms = _terms_from_title(tags) + [f"{n} months" for n in re.findall(r"(\d+)[ -]month", tags)]
        out.append(_item(_strip(company), title, unescape(link.group(1)), _canada(_strip(location)), ts, terms))
    return out


def parse_dreamwork(data: dict, country: str = "CA") -> list[dict]:
    from jobapps.forms import _epoch
    out = []
    for x in data.get("listings") or []:
        if country and x.get("locationCountryCode") != country:
            continue
        company = x.get("company") or ""
        if "." in company and " " not in company:  # slug-derived names like 'Rivianvw.Tech'
            company = company.split(".")[0].title()
        location = x.get("location") or ""
        out.append(_item(company, x.get("title") or "", x.get("url") or "",
                         _canada(location) if country == "CA" else location,
                         _epoch(x.get("postedAt") or x.get("firstIndexedAt")), _terms_from_title(x.get("title") or "")))
    return out


def parse_speedyapply(md: str, country: str = "Canada") -> list[dict]:
    """speedyapply INTERN_INTL.md tables: | Company | Position | Location | (Salary) | Posting | Age |"""
    out, header = [], None
    for line in md.splitlines():
        if line.startswith("| Company"):
            header = [h.strip().lower() for h in line.strip().strip("|").split("|")]
            continue
        if not line.startswith("|") or header is None or set(line.strip()) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != len(header):
            continue
        row = dict(zip(header, cells))
        location = re.sub(r"\s*\+\d+$", "", _strip(row.get("location", "")))
        link = re.search(r'href="([^"]+)"', row.get("posting", ""))
        if (country and country.lower() not in location.lower()) or not link:
            continue
        age = re.match(r"(\d+)\s*d", _strip(row.get("age", "")))
        posted = _days_ago(int(age.group(1))) if age else 0
        title = _strip(row.get("position", ""))
        out.append(_item(_strip(row.get("company", "")), title, unescape(link.group(1)), location, posted,
                         _terms_from_title(title)))
    return out


def _load_source(src: dict) -> list[dict]:
    kind, url = src.get("type", "simplify"), src["url"]
    if kind == "simplify":
        return net.get_json(url, timeout=90)
    if kind == "francklin":
        return parse_francklin(net.get_text(url, timeout=60))
    if kind == "dreamwork":
        return parse_dreamwork(net.get_json(url, timeout=60), src.get("country", "CA"))
    if kind == "speedyapply":
        return parse_speedyapply(net.get_text(url, timeout=60), src.get("country", "Canada"))
    raise ValueError(f"unknown source type {kind!r}")


# --- employers' own job boards (polled directly) ------------------------------------

STUDENT_TITLE = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b|\bstudent\b|\bplacement\b|\bpey\b|summer analyst|work term"
                           r"|\bstagiaire\b", re.I)  # \b keeps out 'internal', 'international', 'replacement'
_TERM = re.compile(r"\b(summer|fall|autumn|winter|spring)\s*[-/]?\s*(20\d\d)\b", re.I)


def _terms_from_title(title: str) -> list[str]:
    return list(dict.fromkeys(f"{s.title().replace('Autumn', 'Fall')} {y}" for s, y in _TERM.findall(title)))


def _ago(text: str, today=None) -> int:
    """Workday's 'Posted 3 Days Ago' -> unix seconds. 'Posted 30+ Days Ago' is all it says about anything older;
    db.upsert_listings keeps the first date a listing was given, so those age from the day they were first seen."""
    t = (text or "").lower()
    days = 0 if "today" in t else 1 if "yesterday" in t else int(m.group(1)) if (m := re.search(r"(\d+)\+?\s*day", t)) else 30
    return _days_ago(days, today)


def _board_listing(company: str, title: str, url: str, locations: list[str], posted: int, source: str) -> dict:
    url = canonical_url(url)
    return {"id": listing_id(url), "source": source, "company": company, "title": re.sub(r"\s+", " ", title).strip(),
            "url": url, "ats": detect_ats(url), "locations": [l for l in locations if l], "terms": _terms_from_title(title),
            "category": "", "sponsorship": "", "degrees": [], "date_posted": posted, "active": True}


def _board_greenhouse(b: dict) -> list[dict]:
    from jobapps.forms import _epoch
    d = net.get_json(f"https://boards-api.greenhouse.io/v1/boards/{b['id']}/jobs", timeout=30)
    return [_board_listing(b["company"], j["title"], j["absolute_url"], [(j.get("location") or {}).get("name")],
                           _epoch(j.get("first_published") or j.get("updated_at")), f"board:{b['company']}")
            for j in d.get("jobs") or []]


def _board_lever(b: dict) -> list[dict]:
    out = []
    for j in net.get_json(f"https://api.lever.co/v0/postings/{b['id']}?mode=json", timeout=30) or []:
        cats = j.get("categories") or {}
        out.append(_board_listing(b["company"], j.get("text", ""), j.get("hostedUrl", ""),
                                  cats.get("allLocations") or [cats.get("location")], int((j.get("createdAt") or 0) / 1000),
                                  f"board:{b['company']}"))
    return out


def _board_ashby(b: dict) -> list[dict]:
    from jobapps.forms import _epoch
    d = net.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{b['id']}", timeout=30)
    out = []
    for j in d.get("jobs") or []:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
        out.append(_board_listing(b["company"], j.get("title", ""), j.get("jobUrl", ""), locs,
                                  _epoch(j.get("publishedAt")), f"board:{b['company']}"))
    return out


def _board_workday(b: dict) -> list[dict]:
    host_part, site = b["id"].split("/", 1)            # "clio.wd3/ClioCareerSite"
    tenant = host_part.split(".")[0]
    host = f"{host_part}.myworkdayjobs.com"
    seen, out = set(), []
    for query in ("intern", "co-op", "student"):
        for offset in range(0, 200, 20):  # Workday pages 20 at a time; big employers have 100+ intern reqs
            d = net.post_json(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                              {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": query}, timeout=30)
            page = d.get("jobPostings") or []
            for j in page:
                path = j.get("externalPath") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                out.append(_board_listing(b["company"], j.get("title", ""), f"https://{host}/{site}{path}",
                                          [j.get("locationsText")], _ago(j.get("postedOn")), f"board:{b['company']}"))
            if len(page) < 20 or offset + 20 >= int(d.get("total") or 0):
                break
    return out


def _board_smartrecruiters(b: dict) -> list[dict]:
    from jobapps.forms import _epoch
    d = net.get_json(f"https://api.smartrecruiters.com/v1/companies/{b['id']}/postings?q=intern&limit=100", timeout=30)
    out = []
    for j in d.get("content") or []:
        loc = j.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country", "").upper()) if x)
        out.append(_board_listing(b["company"], j.get("name", ""), f"https://jobs.smartrecruiters.com/{b['id']}/{j.get('id')}",
                                  [where], _epoch(j.get("releasedDate")), f"board:{b['company']}"))
    return out


_BOARD_READERS = {"greenhouse": _board_greenhouse, "lever": _board_lever, "ashby": _board_ashby,
                  "workday": _board_workday, "smartrecruiters": _board_smartrecruiters}


def fetch_boards(boards: list[dict], log=print) -> tuple[list[dict], bool]:
    """Student/intern postings straight from each employer's board. Returns (listings, all_ok)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(b):
        reader = _BOARD_READERS.get(b.get("ats", ""))
        if not reader or not b.get("id") or b.get("enabled") is False:
            return b, [], None
        try:
            return b, [l for l in reader(b) if STUDENT_TITLE.search(l["title"])], None
        except (net.HttpError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return b, [], exc

    listings, all_ok, failed = [], True, []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for b, found, err in pool.map(one, boards):
            if err is not None:
                all_ok = False
                failed.append(b["company"])
            listings += found
    log(f"  employer boards: {len(listings)} student postings from {len(boards) - len(failed)} of {len(boards)} boards"
        + (f" (failed: {', '.join(failed[:8])}{'…' if len(failed) > 8 else ''})" if failed else ""))
    return listings, all_ok


def fetch_all(sources: list[dict], log=print, boards: list[dict] | None = None) -> tuple[list[dict], bool]:
    """Fetch every configured source. Returns (listings, all_sources_ok)."""
    by_id: dict[str, dict] = {}
    all_ok = True
    if boards:
        board_listings, boards_ok = fetch_boards(boards, log)
        all_ok = all_ok and boards_ok
        for l in board_listings:
            by_id[l["id"]] = _prefer(by_id[l["id"]], l) if l["id"] in by_id else l
    for src in sources:
        name, url = src.get("name") or src.get("url"), src.get("url")
        if not url or src.get("enabled") is False:
            continue
        try:
            items = _load_source(src)
        except (net.HttpError, ValueError, KeyError, TypeError, AttributeError) as exc:
            all_ok = False  # a feed that moved or broke never deactivates anything
            log(f"  ! {name}: {exc}")
            continue
        shorts = [i["url"] for i in items if SHORT_LINK.match(i.get("url") or "")]
        if shorts:
            targets = resolve_short_links(shorts)
            for i in items:
                if i.get("url") in targets:
                    i["url"] = targets[i["url"]]
        n = 0
        for item in items:
            l = from_feed_item(item, name)
            if l is None:
                continue
            n += 1
            by_id[l["id"]] = _prefer(by_id[l["id"]], l) if l["id"] in by_id else l
        log(f"  {name}: {n} listings")
    return sorted(by_id.values(), key=lambda l: l["id"]), all_ok
