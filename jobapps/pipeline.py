"""The daily pipeline, shared by the CLI (`ja today`) and the server's scheduler.

  fetch feeds -> (optional) Claude web search, verified -> stage 1: metadata rank
  -> stage 2: deterministic resume fit + eligibility -> stage 3: grounded Claude screen (cached)
  -> pick N (company cap, at most one stretch) -> draft answers -> report

Why each job was picked is frozen in the `picks` table at pick time; what was filtered out (and
the posting sentence that ruled it out) is kept per day so the dashboard can show it.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from jobapps import db, discover, drafts, fit, forms, llm, net, picker, report, screen, sources
from jobapps.config import Profile, load_learned
from jobapps.forms import Field
from jobapps.rules import Resolver, is_canadian


def fetch(conn, cfg: dict, log=print) -> None:
    log("Fetching listings...")
    listings, all_ok = sources.fetch_all(cfg["sources"], log, boards=cfg.get("boards") or [])
    if not listings:
        log("  ! no listings fetched; keeping what we had")
        return
    new, total = db.upsert_listings(conn, listings, deactivate_missing=all_ok)
    db.set_meta(conn, "last_fetch", datetime.now().isoformat(timespec="seconds"))
    stats: dict[str, dict] = {}
    for l in listings:
        name = "employer boards" if l["source"].startswith("board:") else l["source"]
        s = stats.setdefault(name, {"source": name, "active": 0, "canada": 0, "companies": set()})
        if l["active"]:
            s["active"] += 1
            s["canada"] += in_canada(l)
            s["companies"].add(l["company"])
    db.set_meta(conn, "source_stats", json.dumps({
        "fetched_at": datetime.now().isoformat(timespec="seconds"), "all_ok": all_ok,
        "sources": [{**s, "companies": len(s["companies"])} for s in sorted(stats.values(), key=lambda s: -s["active"])],
    }))
    log(f"  {total} listings, {new} new")


def maybe_fetch(conn, cfg: dict, force: bool = False, log=print) -> None:
    last = db.get_meta(conn, "last_fetch")
    if force or not last or datetime.now() - datetime.fromisoformat(last) > timedelta(hours=3):
        fetch(conn, cfg, log)


def still_open(conn, job: dict, log=print) -> bool:
    """Confirm the posting is live; store its form when the ATS is readable."""
    try:
        form = forms.check_open(job)
    except forms.PostingClosed:
        log(f"  - closed: {job['company']} - {job['title']}")
        return False
    except (net.HttpError, ValueError, KeyError, TypeError) as exc:
        log(f"  ? couldn't read {job['company']}'s form ({exc}); will read it in the browser")
        return True
    if form is not None:
        db.save_form(conn, job["id"], form.ats, form.description, [f.to_dict() for f in form.fields], form.apply_url)
        vague = not job.get("locations") or all(re.fullmatch(r"\d+ locations?", l.strip(), re.I) for l in job["locations"])
        if vague and form.locations:  # Workday lists multi-city jobs as "2 Locations"; the posting has the cities
            job["locations"] = form.locations
            db.set_locations(conn, job["id"], form.locations)
    return True


def description_for(conn, job: dict) -> str:
    """Posting text for fit checks: from the ATS API if we have it, else the page itself (once)."""
    form = db.get_form(conn, job["id"])
    if form:
        return form.get("description") or ""
    text = forms.page_description(job["url"])
    db.save_form(conn, job["id"], "page", text, [], "")  # 'page' = description only, questions read live
    return text


@dataclass
class Scored:
    job: dict
    stage1: picker.Verdict
    fit: fit.FitResult
    description: str
    screen: dict | None = None
    screen_points: int = 0

    @property
    def total(self) -> int:
        return self.stage1.score + self.fit.score + self.screen_points

    @property
    def verdict(self) -> str:
        if self.screen and self.screen.get("fit") is not None:
            return self.screen["verdict"]
        cov = self.fit.coverage
        return "unknown" if cov is None else "strong" if cov >= 0.75 else "good" if cov >= 0.5 else "stretch"

    def key(self):
        return (-self.total, -(self.job["date_posted"] or 0), self.job["company"].casefold(), self.job["id"])


def in_canada(job: dict) -> bool:
    """Any Canadian location (incl. 'Remote in Canada') counts: you could take it without a visa."""
    return any(is_canadian(x) for x in job.get("locations") or [])


def _exclude(excluded: list, job: dict, reason: str, quote: str = "") -> None:
    excluded.append({"id": job["id"], "company": job["company"], "title": job["title"], "reason": reason, "quote": quote})


_TERMISH = re.compile(r"\([^)]*\)|\[[^\]]*\]|\b(summer|fall|autumn|winter|spring|20\d\d|\d+[ -]?months?|\d+[ -]?mos?)\b", re.I)


_LEGAL = {"inc", "incorporated", "ltd", "limited", "llc", "llp", "corp", "corporation", "co", "company", "plc", "the",
          "group", "holdings", "ulc", "lp", "gmbh", "sa", "ag"}
_FILLER = {"of", "and", "for", "de", "du", "des", "la", "le"}


def company_keys(name: str) -> set[str]:
    """Ways one company is written: "Hewlett Packard Enterprise" -> {hewlettpackardenterprise, hpe};
    "Royal Bank of Canada" -> {royalbankofcanada, rbc}; "Motorola Solutions Inc." -> {motorolasolutions, ms}...
    Initials only count from 3 letters, so two-letter ones can't merge different companies."""
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and ")) if w not in _LEGAL]
    keys = {"".join(words)} if words else set()
    initials = "".join(w[0] for w in words if w not in _FILLER)
    if len(words) >= 2 and len(initials) >= 3:
        keys.add(initials)
    return keys


def company_key(name: str) -> str:
    """One canonical spelling (the shortest way of writing it) for counting a company once per list."""
    keys = company_keys(name)
    return min(keys, key=lambda k: (len(k), k)) if keys else ""


def same_job_key(company: str, title: str) -> str:
    """The same role posted twice (two career sites, two feeds, a repost): company + title without the term,
    year or length decorations ("Developer Intern (Summer 2027)" == "Developer Intern - 2027")."""
    t = _TERMISH.sub(" ", title or "")
    return f"{company_key(company)}|{re.sub(r'[^a-z0-9]+', ' ', t.lower()).strip()}"


def already_seen(conn) -> tuple[set[str], set[str]]:
    """Postings you've had (any list, any outcome) or that were ruled out, and their same-job keys."""
    ids = db.tracked_ids(conn) | {r[0] for r in conn.execute(
        "SELECT DISTINCT job_id FROM events WHERE kind IN ('picked', 'carried', 'added', 'released', 'ruled_out')")}
    keys = set()
    for job_id in ids - {r[0] for r in conn.execute("SELECT DISTINCT job_id FROM events WHERE kind = 'ruled_out'")}:
        if job := db.get_listing(conn, job_id):
            keys.add(same_job_key(job["company"], job["title"]))
    return ids, keys


def recent_companies(conn, today: date, days: int) -> set[str]:
    """Every way of writing the companies you've had in the last `days` days (see company_keys)."""
    since = (today - timedelta(days=days)).isoformat()
    rows = conn.execute("""SELECT DISTINCT l.company FROM batches b JOIN listings l ON l.id = b.job_id
                           WHERE b.date >= ? AND b.date < ?""", (since, today.isoformat())).fetchall()
    return set().union(*(company_keys(r[0]) for r in rows)) if rows else set()


def carry_over(conn, cfg: dict, today: date, log=print) -> list[str]:
    """Unfinished jobs from the last few days, still open. They go on today's list as well as (not instead
    of) the day's new picks, and stop being carried after `carry_days`; they stay "on your list" in Postings."""
    if not cfg.get("carry_over", True):
        return []
    since = (today - timedelta(days=int(cfg.get("carry_days", 3)))).isoformat()
    out = []
    for job_id in db.pending_before(conn, today.isoformat()):
        first = conn.execute("SELECT MIN(date) FROM batches WHERE job_id = ?", (job_id,)).fetchone()[0]
        job = db.get_listing(conn, job_id)
        if job is None or (first or "") < since or len(out) >= int(cfg.get("carry_max", 5)):
            continue
        if not still_open(conn, job, log):
            db.set_status(conn, job_id, "closed", "posting closed")
            continue
        out.append(job_id)
    return out


def build_batch(conn, cfg: dict, profile: Profile, today: date, extra: int = 0, log=print) -> None:
    key = today.isoformat()
    existing = db.batch(conn, key)
    if existing and not extra:
        return
    want = extra if existing else int(cfg["daily_count"])  # always this many NEW jobs; carried ones are on top
    carried = [] if existing else carry_over(conn, cfg, today, log)
    listed = [r["job_id"] for r in existing] + carried
    per_company = Counter(company_key((db.get_listing(conn, j) or {}).get("company", "")) for j in listed)
    limit = int(cfg["max_per_company"])
    chosen: list[str] = []
    if carried:
        log(f"  {len(carried)} unfinished job(s) from the last {cfg.get('carry_days', 3)} days stay on the list")
    seen_ids, seen_jobs = already_seen(conn)
    cooldown = recent_companies(conn, today, int(cfg.get("company_cooldown_days", 7)))

    fcfg, scfg = cfg.get("fit", {}), cfg.get("screen", {})
    background = drafts.background(profile) if drafts.has_material(profile) else ""
    cand = fit.candidate(profile, background)
    excluded: list[dict] = []

    # Stage 1: metadata ranking -> a shortlist of postings that are still open.
    shortlist, checked = [], 0
    size = int(fcfg.get("shortlist", 40))
    dupes = 0
    for job, verdict in picker.rank(db.active_listings(conn), cfg, today, exclude_ids=seen_ids):
        if len(shortlist) >= size or checked >= size * 2:
            break
        if per_company[company_key(job["company"])] >= limit:
            continue
        if same_job_key(job["company"], job["title"]) in seen_jobs:
            dupes += 1  # the same role you've already had, under another link
            continue
        checked += 1
        if not still_open(conn, job, log):
            db.deactivate(conn, job["id"])
            continue
        shortlist.append((job, verdict))
    log(f"  stage 1: {len(shortlist)} open postings shortlisted ({dupes} repeats of jobs you've had skipped)")

    # Stage 2: deterministic fit + eligibility against your resume.
    scored: list[Scored] = []
    for job, verdict in shortlist:
        desc = description_for(conn, job)
        result = fit.evaluate(job, desc, cand, cfg)
        if not result.eligible:
            b = result.blockers[0]
            _exclude(excluded, job, b["reason"], b["quote"])
            continue
        scored.append(Scored(job, verdict, result, desc))
    scored.sort(key=Scored.key)
    log(f"  stage 2: {len(scored)} eligible, {len(excluded)} ruled out")

    # Stage 3: grounded Claude screen of the best few (cached per job).
    top = int(scfg.get("top", 12))
    if scfg.get("enabled", True) and background and llm.backend(cfg["drafts"]) != "none" and scored:
        items = [(s.job, s.description) for s in scored[:top] if s.description]
        log(f"  stage 3: screening {len(items)} with Claude (cached ones are free)...")
        results = screen.screen_many(cfg, profile, background, items, log)
        weight, min_fit = int(scfg.get("weight", 40)), int(scfg.get("min_fit", 30))
        kept = []
        for s in scored:
            s.screen = results.get(s.job["id"])
            if s.screen:
                if s.screen["blockers"]:
                    b = s.screen["blockers"][0]
                    _exclude(excluded, s.job, f"Claude: {b['reason']}", b["quote"])
                    continue
                if s.screen["fit"] is not None:
                    if s.screen["fit"] < min_fit:
                        _exclude(excluded, s.job, f"long shot (verified fit {s.screen['fit']}/100)")
                        continue
                    s.screen_points = round(weight * s.screen["fit"] / 100)
            kept.append(s)
        scored = sorted(kept, key=Scored.key)

    # Pick: best total first, company cap, at most N stretch/reach roles, at most N outside Canada. Companies
    # you've had in the last week only come back when there aren't enough others.
    reach_names = set().union(*(company_keys(n) for n in cfg.get("reach_companies") or []))
    max_reach, reach_used = int(cfg.get("max_reach_per_day", 1)), 0
    max_abroad = cfg.get("max_outside_canada_per_day")
    abroad_used = sum(1 for job_id in listed if not in_canada(db.get_listing(conn, job_id) or {}))
    fresh = [s for s in scored if not company_keys(s.job["company"]) & cooldown]
    for s in fresh + [s for s in scored if s not in fresh]:
        if len(chosen) >= want:
            break
        company = company_key(s.job["company"])
        if per_company[company] >= limit:
            continue
        reach = bool(company_keys(s.job["company"]) & reach_names) or s.verdict in ("stretch", "long shot")
        if reach and reach_used >= max_reach:
            continue
        abroad = not in_canada(s.job)
        if abroad and max_abroad is not None and abroad_used >= int(max_abroad):
            continue
        abroad_used += abroad
        reach_used += reach
        chosen.append(s.job["id"])
        per_company[company] += 1
        db.save_pick(conn, key, s.job["id"], s.total, {
            "stage1": s.stage1.reasons, "fit": s.fit.to_dict(), "screen": s.screen,
            "verdict": s.verdict, "reach": reach, "screen_points": s.screen_points,
        })
    db.add_to_batch(conn, key, chosen)
    db.add_to_batch(conn, key, carried)  # after the new picks: shown apart, as "still open from earlier days"
    previous = json.loads(db.get_meta(conn, f"excluded:{key}") or "[]")
    for e in excluded:
        db.log_event(conn, e["id"], "ruled_out", key, {"reason": e["reason"], "quote": e["quote"]})
    db.set_meta(conn, f"excluded:{key}", json.dumps(previous + excluded))
    if len(chosen) < want:
        log(f"  only {len(chosen)} new job(s) passed (wanted {want}); loosen config.yaml filters for more")


def refresh_forms(conn, today: date, everything: bool = False, log=print) -> None:
    """Read forms that are missing (or all pending ones with everything=True); mark closed postings."""
    for row in db.batch(conn, today.isoformat()):
        job = db.get_listing(conn, row["job_id"])
        form = db.get_form(conn, row["job_id"]) if job else None
        if row["status"] == "pending" and job and (everything or not form or form["ats"] == "page"):
            if not still_open(conn, job, log):
                db.set_status(conn, job["id"], "closed", "posting closed")


def prepare_drafts(conn, cfg: dict, profile: Profile, today: date, only: list[str] | None = None,
                   force: bool = False, log=print) -> None:
    learned = load_learned()
    items = []
    for row in db.batch(conn, today.isoformat()):
        if row["status"] != "pending" or (only and row["job_id"] not in only):
            continue
        job, form = db.get_listing(conn, row["job_id"]), db.get_form(conn, row["job_id"])
        if not job or not form or not form["fields"]:
            continue
        resolver = Resolver(profile, job, learned, {})
        questions = [f for f in map(Field.from_dict, form["fields"]) if resolver.resolve(f).kind == "draft"]
        if questions:
            items.append((job, form["description"], questions))
    if not items:
        return
    dcfg = dict(cfg["drafts"])
    kind = llm.backend(dcfg)
    if kind != "none" and not drafts.has_material(profile):
        log("Skipping drafts: add your resume or fill in background.md first.")
        dcfg["backend"] = kind = "none"
    log("Writing prompts only (no drafting backend)." if kind == "none"
        else f"Drafting answers for {len(items)} job(s) with {kind} ({dcfg['model']})...")
    results = drafts.draft_many(dcfg, profile, items, today, force=force, log=log)
    for job, _, _ in items:
        r = results.get(job["id"], {})
        if r.get("status") == "drafted":
            log(f"  {job['company']}: {r['count']} answer(s)")
        elif r.get("status") == "error":
            log(f"  {job['company']}: failed - {r['error']}")


def run_daily(conn, cfg: dict, profile: Profile, today: date, refresh: bool = False, more: int = 0,
              draft: bool = True, fetch_listings: bool = True, log=print):
    if fetch_listings:
        maybe_fetch(conn, cfg, force=refresh, log=log)
    key = today.isoformat()
    if (not db.batch(conn, key) or more) and db.get_meta(conn, f"discovered:{key}") is None:
        background = drafts.background(profile) if drafts.has_material(profile) else ""
        discover.run(conn, cfg, profile, fit.candidate(profile, background).skills, log)
        db.set_meta(conn, f"discovered:{key}", datetime.now().isoformat(timespec="seconds"))
    log("Building today's batch...")
    build_batch(conn, cfg, profile, today, extra=more, log=log)
    refresh_forms(conn, today, everything=refresh, log=log)
    if draft:
        prepare_drafts(conn, cfg, profile, today, log=log)
    db.set_meta(conn, "last_run", datetime.now().isoformat(timespec="seconds"))
    return report.build(conn, cfg, profile, today)
