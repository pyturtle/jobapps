"""Deterministic filtering and scoring of listings.

Same listings + same config + same date -> same ranking. Every point awarded is recorded
as a human-readable reason so `jobapps explain` can show why a job was (or wasn't) picked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime


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


def age_days(listing: dict, today: date) -> int:
    if not listing.get("date_posted"):
        return 999
    posted = datetime.fromtimestamp(listing["date_posted"]).date()
    return max(0, (today - posted).days)


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
    if l["terms"] and all(t in f["exclude_terms"] for t in l["terms"]):
        return Verdict(False, f"term {', '.join(l['terms'])} excluded")
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
        if any(re.search(rule["pattern"], x, re.I) for x in locs):
            points.append((rule["points"], rule.get("label") or f"location ~ {rule['pattern']}"))
            break
    for rule in s["title_keywords"]:
        if re.search(rule["pattern"], l["title"], re.I):
            points.append((rule["points"], rule.get("label") or f"title ~ {rule['pattern']}"))
    fav = s["favorite_companies"]
    if l["company"].casefold() in {n.casefold() for n in fav.get("names") or []}:
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
