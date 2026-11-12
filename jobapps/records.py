"""Your record: every posting ever scraped, what happened to it, and how long each application took.

Built from the listings table (never pruned), applications (current status), the event log (picked,
carried, added, released, ruled out, opened, applied, skipped...) and the activity timer.
"""
from __future__ import annotations

import csv
import io
import json
import statistics
from datetime import datetime

from jobapps import db
from jobapps.rules import is_canadian

STATUSES = ("applied", "skipped", "on_list", "ruled_out", "released", "closed", "not_picked")
LABEL = {"applied": "Applied", "skipped": "Skipped", "on_list": "On your list", "ruled_out": "Ruled out",
         "released": "Swapped out", "closed": "Closed", "not_picked": "Not picked"}


def _event_summary(conn) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT job_id, kind, at, day, detail FROM events ORDER BY id"):
        e = out.setdefault(r["job_id"], {})
        kind = r["kind"]
        if kind in ("picked", "carried", "added"):
            e.setdefault("listed_on", r["day"])
            e.setdefault("how", kind)
            e["last_listed"] = r["day"]
        elif kind == "ruled_out":
            e["ruled_out"] = (json.loads(r["detail"] or "{}") or {}).get("reason") or "ruled out"
            e["ruled_day"] = r["day"]
        elif kind == "rechecked":  # back in the running after a rule changed (pipeline.recheck)
            e.pop("ruled_out", None)
            e.pop("ruled_day", None)
        elif kind == "released":
            e["released"] = r["day"]
        elif kind in ("applied", "skipped", "closed"):
            e["done_at"] = r["at"]
            e["applied_url"] = (json.loads(r["detail"] or "{}") or {}).get("url", "") if kind == "applied" else ""
        elif kind == "opened":
            e.setdefault("opened_at", r["at"])
    return out


def _all(conn) -> list[dict]:
    apps = {r["job_id"]: r["status"] for r in conn.execute("SELECT job_id, status FROM applications")}
    secs = db.application_seconds(conn)
    ev = _event_summary(conn)
    rows = []
    for l in conn.execute("SELECT id, company, title, url, ats, source, locations, first_seen, last_seen, active, "
                          "date_posted FROM listings"):
        e = ev.get(l["id"], {})
        app = apps.get(l["id"])
        status = ("on_list" if app == "pending" else app) if app else \
            "ruled_out" if e.get("ruled_out") else "released" if e.get("released") else "not_picked"
        locs = json.loads(l["locations"] or "[]")
        rows.append({
            "id": l["id"], "company": l["company"], "title": l["title"], "url": l["url"], "ats": l["ats"],
            "source": l["source"], "locations": locs, "canada": any(is_canadian(x) for x in locs),
            "first_seen": (l["first_seen"] or "")[:10], "last_seen": (l["last_seen"] or "")[:10],
            "posted": datetime.fromtimestamp(l["date_posted"]).date().isoformat() if l["date_posted"] else "",
            "active": bool(l["active"]), "status": status, "status_label": LABEL.get(status, status),
            "listed_on": e.get("listed_on", ""), "how": e.get("how", ""), "done_at": (e.get("done_at") or "")[:16],
            "applied_url": e.get("applied_url", "") if status == "applied" else "",
            "reason": e.get("ruled_out", "") if status == "ruled_out" else "",
            "minutes": round(secs[l["id"]][0] / 60, 1) if l["id"] in secs else None,
            "estimated": secs[l["id"]][1] if l["id"] in secs else False,
        })
    return rows


def postings(conn, q: str = "", status: str = "all", region: str = "all", active: str = "all",
             sort: str = "newest", page: int = 1, size: int = 50, today_ids: set[str] = frozenset()) -> dict:
    rows = _all(conn)
    summary = {"total": len(rows), "active": sum(r["active"] for r in rows), "canada": sum(r["canada"] for r in rows),
               **{s: sum(r["status"] == s for r in rows) for s in STATUSES}}
    summary["ever_listed"] = sum(bool(r["listed_on"]) for r in rows)
    words = [w for w in q.lower().split() if w]
    if words:
        rows = [r for r in rows if all(w in f"{r['company']} {r['title']} {' '.join(r['locations'])}".lower() for w in words)]
    if status == "listed":
        rows = [r for r in rows if r["listed_on"]]
    elif status != "all":
        rows = [r for r in rows if r["status"] == status]
    if region in ("canada", "other"):
        rows = [r for r in rows if r["canada"] == (region == "canada")]
    if active in ("open", "closed"):
        rows = [r for r in rows if r["active"] == (active == "open")]
    key = {"newest": lambda r: (r["first_seen"], r["posted"]), "posted": lambda r: (r["posted"], r["first_seen"]),
           "company": lambda r: (r["company"].casefold(), r["title"].casefold()),
           "activity": lambda r: (r["done_at"] or r["listed_on"] or "", r["first_seen"])}.get(sort)
    rows.sort(key=key or (lambda r: r["first_seen"]), reverse=sort != "company")
    total = len(rows)
    start = max(0, (page - 1) * size)
    for r in rows[start:start + size]:
        r["can_add"] = r["active"] and r["status"] in ("not_picked", "released", "ruled_out") and r["id"] not in today_ids
    return {"summary": summary, "total": total, "page": page, "size": size, "rows": rows[start:start + size]}


CSV_COLUMNS = ["first_seen", "last_seen", "posted", "active", "company", "title", "locations", "canada", "source", "ats",
               "url", "status_label", "listed_on", "how", "done_at", "applied_url", "minutes", "reason", "id"]


def postings_csv(conn, **filters) -> str:
    data = postings(conn, **{**filters, "page": 1, "size": 10**9})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in data["rows"]:
        w.writerow({**r, "locations": " / ".join(r["locations"]), "status_label": r["status_label"]})
    return buf.getvalue()


def stats(conn) -> dict:
    """How long applications take: per application, on average, by site, by day. Applications the timer barely
    saw count with their estimate (db.estimated_seconds) and are flagged as estimated."""
    secs = db.application_seconds(conn)
    done = conn.execute(
        """SELECT a.job_id, a.status, a.updated_at, l.company, l.title, l.ats
           FROM applications a LEFT JOIN listings l ON l.id = a.job_id WHERE a.status = 'applied'
           ORDER BY a.updated_at DESC""").fetchall()
    apps = [{"id": r["job_id"], "company": r["company"] or "", "title": r["title"] or "", "ats": r["ats"] or "other",
             "applied_at": (r["updated_at"] or "")[:16], "day": (r["updated_at"] or "")[:10],
             "minutes": round(secs[r["job_id"]][0] / 60, 1) if r["job_id"] in secs else None,
             "estimated": secs[r["job_id"]][1] if r["job_id"] in secs else False} for r in done]
    timed = [a for a in apps if a["minutes"]]
    mins = [a["minutes"] for a in timed]
    by_ats: dict[str, list[float]] = {}
    for a in timed:
        by_ats.setdefault(a["ats"], []).append(a["minutes"])
    days: dict[str, dict] = {}
    for a in apps:
        d = days.setdefault(a["day"], {"day": a["day"], "applied": 0, "minutes": 0.0})
        d["applied"] += 1
        d["minutes"] += a["minutes"] or 0
    return {
        "applied": len(apps), "timed": len(timed), "estimated": sum(a["estimated"] for a in timed),
        "average": round(statistics.mean(mins), 1) if mins else None,
        "median": round(statistics.median(mins), 1) if mins else None,
        "fastest": min(timed, key=lambda a: a["minutes"]) if timed else None,
        "slowest": max(timed, key=lambda a: a["minutes"]) if timed else None,
        "total_minutes": round(sum(mins), 1),
        "by_site": sorted(({"ats": k, "count": len(v), "average": round(statistics.mean(v), 1)} for k, v in by_ats.items()),
                          key=lambda x: -x["count"]),
        "by_day": sorted(days.values(), key=lambda d: d["day"], reverse=True)[:30],
        "applications": apps[:200],
    }
