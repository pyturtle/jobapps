"""Deterministic filtering and scoring of listings.

Same listings + same config + same date -> same ranking. Every point awarded is recorded
as a human-readable reason so `jobapps explain` can show why a job was (or wasn't) picked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from jobapps.rules import is_canadian


@dataclass
class Verdict:
    eligible: bool
    reason: str = ""                       # why it was filtered out
    score: int = 0
    reasons: list[str] = field(default_factory=list)


def _match(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, re.I):
            return p
    return None


_LEGAL = {"inc", "incorporated", "ltd", "limited", "llc", "llp", "corp", "corporation", "co", "company", "plc", "the",
          "group", "holdings", "ulc", "lp", "gmbh", "sa", "ag"}
_FILLER = {"of", "and", "for", "de", "du", "des", "la", "le"}


def _company_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and ")) if w not in _LEGAL]


def company_keys(name: str) -> set[str]:
    """Ways one company is written: "Hewlett Packard Enterprise" -> {hewlettpackardenterprise, hpe};
    "Royal Bank of Canada" -> {royalbankofcanada, rbc}; "Motorola Solutions Inc." -> {motorolasolutions, ms}...
    Initials only count from 3 letters, so two-letter ones can't merge different companies."""
    words = _company_words(name)
    keys = {"".join(words)} if words else set()
    initials = "".join(w[0] for w in words if w not in _FILLER)
    if len(words) >= 2 and len(initials) >= 3:
        keys.add(initials)
    return keys


def named(company: str, names: list[str]) -> bool:
    """Is `company` one of `names`? "Acme" covers "Acme Web Services" and "Acme Canada Campus"
    (whole leading words, so "Meta" isn't "Metabase"); "RBC" covers "Royal Bank of Canada" (company_keys)."""
    words, keys = _company_words(company), company_keys(company)
    for name in names or []:
        want = _company_words(name)
        if (want and words[:len(want)] == want) or company_keys(name) & keys:
            return True
    return False


_SEASON_END = {"winter": 4, "spring": 6, "summer": 8, "fall": 12, "autumn": 12}  # month each term is over


def past_term(term: str, today: date) -> bool:
    """'Summer 2026' on 2026-11-07: that term is over, so the posting is last cycle's."""
    m = re.fullmatch(r"(summer|fall|autumn|winter|spring) (20\d\d)", term.strip(), re.I)
    return bool(m) and (int(m.group(2)), _SEASON_END[m.group(1).lower()]) < (today.year, today.month)


def dated_terms(terms: list[str]) -> list[str]:
    """Feeds tag a posting ["Summer 2026", "Summer"]: the bare season is the dated one, not another term."""
    dated = {t.split()[0].lower() for t in terms if re.fullmatch(r"[A-Za-z]+ 20\d\d", t.strip())}
    return [t for t in terms if t.strip().lower() not in dated]


def age_days(listing: dict, today: date) -> int:
    if not listing.get("date_posted"):
        return 999
    posted = datetime.fromtimestamp(listing["date_posted"]).date()
    return max(0, (today - posted).days)


def _located(rule: dict, location: str) -> bool:
    """Whether a location scoring rule covers a place. `in: canada` asks rules.is_canadian, the test the daily
    cap on postings outside Canada uses, so the two can't disagree about "Montreal" or "Markham, IL"."""
    if rule.get("in") == "canada" and not is_canadian(location):
        return False
    return bool(re.search(rule["pattern"], location, re.I)) if rule.get("pattern") else bool(rule.get("in"))


def evaluate(l: dict, cfg: dict, today: date) -> Verdict:
    f, s = cfg["filters"], cfg["scoring"]
    age = age_days(l, today)
    locs = l["locations"] or []

    if age > f["max_age_days"]:
        return Verdict(False, f"posted {age}d ago (limit {f['max_age_days']}d)")
    if l["company"].casefold() in {c.casefold() for c in f["exclude_companies"]}:
        return Verdict(False, "company excluded")
    if f["categories"] and l["category"] and l["category"] not in f["categories"]:
        return Verdict(False, f"category '{l['category']}' not wanted")
    if f["title_include"] and not _match(f["title_include"], l["title"]):
        return Verdict(False, "title matches no include pattern")
    if p := _match(f["title_exclude"], l["title"]):
        return Verdict(False, f"title matches exclude /{p}/")
    terms = dated_terms(l["terms"] or [])
    if terms and all(t in f["exclude_terms"] or past_term(t, today) for t in terms):
        return Verdict(False, f"term {', '.join(terms)} excluded or already over")
    if f["require_degree"] and l["degrees"] and f["require_degree"] not in l["degrees"]:
        return Verdict(False, f"degree {', '.join(l['degrees'])} only")
    if l["sponsorship"] in f["exclude_sponsorship"]:
        return Verdict(False, l["sponsorship"])
    if f["location_include"] and not any(_match(f["location_include"], x) for x in locs):
        return Verdict(False, "no location matches include list")
    if f["location_exclude"] and locs and all(_match(f["location_exclude"], x) for x in locs):
        return Verdict(False, "all locations excluded")

    points: list[tuple[int, str]] = []
    points.append((max(0, s["recency_max"] - s["recency_per_day"] * age), f"posted {age}d ago"))
    ats = s["ats"].get(l["ats"], s["ats"].get("other", 0))
    points.append((ats, f"{l['ats']} form"))
    points.append((s["categories"].get(l["category"], 0), f"category {l['category']}"))
    term_pts = [(s["terms"][t], f"term {t}") for t in l["terms"] if t in s["terms"]]
    if term_pts:
        points.append(max(term_pts))
    for rule in s["locations"]:  # first matching rule wins; order them best-first
        if any(_located(rule, x) for x in locs):
            points.append((rule["points"], rule.get("label") or f"location ~ {rule['pattern']}"))
            break
    for rule in s["title_keywords"]:
        if re.search(rule["pattern"], l["title"], re.I):
            points.append((rule["points"], rule.get("label") or f"title ~ {rule['pattern']}"))
    fav = s["favorite_companies"]
    if named(l["company"], fav.get("names")):
        points.append((fav.get("points", 0), "favorite company"))

    points = [(p, why) for p, why in points if p]
    return Verdict(True, score=sum(p for p, _ in points),
                   reasons=[f"{p:+d} {why}" for p, why in points])


def sort_key(item: tuple[dict, Verdict]):
    l, v = item
    return (-v.score, -(l["date_posted"] or 0), l["company"].casefold(), l["id"])


def rank(listings: list[dict], cfg: dict, today: date, exclude_ids: set[str] = frozenset()) -> list[tuple[dict, Verdict]]:
    out = []
    for l in listings:
        if not l["active"] or l["id"] in exclude_ids:
            continue
        v = evaluate(l, cfg, today)
        if v.eligible:
            out.append((l, v))
    return sorted(out, key=sort_key)
