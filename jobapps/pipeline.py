"""The daily pipeline, shared by the CLI (`ja today`) and the server's scheduler.

  fetch feeds -> (optional) Claude web search, verified -> stage 1: metadata rank
  -> stage 2: deterministic resume fit + eligibility -> stage 3: grounded Claude screen (cached)
  -> pick N (company cap, at most one stretch) -> draft answers -> report

Why each job was picked is frozen in the `picks` table at pick time; what was filtered out (and
the posting sentence that ruled it out) is kept per day so the dashboard can show it.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from jobapps import db, discover, drafts, fit, forms, llm, net, picker, report, research, screen, sources
from jobapps.config import Profile, load_learned
from jobapps.forms import Field
from jobapps.picker import company_keys
from jobapps.rules import Resolver, is_canadian, towns


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
        if unplaced(job) and form.locations:  # Workday lists multi-city jobs as "2 Locations"; the posting has the cities
            job["locations"] = form.locations
            db.set_locations(conn, job["id"], form.locations)
    return True


def description_for(conn, job: dict) -> str:
    """Posting text for fit checks: from the ATS API if we have it, else the page itself; '' if neither has it.
    Page text saved earlier that isn't a posting (menus of a page that loads it with JavaScript) is read again."""
    form = db.get_form(conn, job["id"])
    desc = (form or {}).get("description") or ""
    if form and (form["ats"] != "page" or job["ats"] in forms.READERS or forms.looks_like_posting(desc)):
        return desc
    text = forms.page_description(job["url"])
    if text or not form or len(desc) > forms.MAX_PAGE_TEXT:  # a whole careers page isn't kept as this job's text
        db.save_form(conn, job["id"], "page", text, [], "")  # 'page' = description only, questions read live
    return text


GOOD = ("good", "strong")


@dataclass
class Scored:
    job: dict
    stage1: picker.Verdict
    fit: fit.FitResult
    description: str
    screen: dict | None = None
    screen_points: int = 0
    source: str = "posting"   # where the text came from: posting | research | research-unverified (see research.py)
    source_url: str = ""

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


_TERMISH = re.compile(r"\b(summer|fall|autumn|winter|spring|20\d\d|\d+[ -]?months?|\d+[ -]?mos?)\b", re.I)
_BRACKETED = re.compile(r"\(([^)]*)\)|\[([^\]]*)\]")
# Bracket text that only says when, how long or what kind of placement: "(Winter 2027, 8 months)", "(Co-op)".
_WHEN = re.compile(r"\b(?:summer|fall|autumn|winter|spring|(?:l')?[ée]t[ée]|hiver|automne|printemps|jan(?:uary)?|feb(?:ruary)?"
                   r"|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
                   r"|20\d\d|\d+|months?|mos?|weeks?|co-?op|intern(?:ship)?s?|stagiaire|start|date|intake|term|contract"
                   r"|placement|on-?site|hybrid|remote|or|and|to)\b", re.I)


def company_key(name: str) -> str:
    """One canonical spelling (the shortest way of writing it) for counting a company once per list."""
    keys = company_keys(name)
    return min(keys, key=lambda k: (len(k), k)) if keys else ""


def same_job_key(company: str, title: str) -> str:
    """The same role posted twice (two career sites, two feeds, a repost): company + title without the term,
    year or length decorations ("Developer Intern (Summer 2027)" == "Developer Intern - 2027"). Brackets that
    name the team or the work stay: "Developer Intern (Payments)" is another role."""
    def bracket(m):
        text = m.group(1) or m.group(2) or ""
        return f" {text} " if re.search(r"[^\W_]", _WHEN.sub("", text)) else " "
    t = _TERMISH.sub(" ", _BRACKETED.sub(bracket, title or ""))
    return f"{company_key(company)}|{re.sub(r'[^a-z0-9]+', ' ', t.lower()).strip()}"


def unplaced(job: dict) -> bool:
    """No location to go by: none listed, or Workday's "2 Locations" (the posting itself names the cities)."""
    return not job.get("locations") or all(re.fullmatch(r"\d+ locations?", l.strip(), re.I) for l in job["locations"])


def places(job: dict) -> list[frozenset[str]]:
    """Each town the posting names (see rules.towns); empty when it could be anywhere."""
    return [t for loc in job.get("locations") or [] for part in re.split(r"[;|]", loc) if (t := towns(part))]


def is_repeat(job: dict, seen_jobs: dict[str, list]) -> bool:
    """The same role as one in `seen_jobs`: same company and title, and a town in common. A posting that names
    no town could be any of them, so it counts as the same."""
    mine = places(job)
    return any(not mine or not theirs or any(a <= b or b <= a for a in mine for b in theirs)
               for theirs in seen_jobs.get(same_job_key(job["company"], job["title"]), ()))


def ruled_out(conn) -> dict[str, str]:
    """Postings that are ruled out now, with the reason: each one's last rule-out, unless it has been checked
    again since (see recheck)."""
    out: dict[str, str] = {}
    for r in conn.execute("SELECT job_id, kind, detail FROM events WHERE kind IN ('ruled_out', 'rechecked') ORDER BY id"):
        if r["kind"] == "rechecked":
            out.pop(r["job_id"], None)
        else:
            out[r["job_id"]] = (json.loads(r["detail"] or "{}") or {}).get("reason") or ""
    return out


def ever_listed(conn) -> set[str]:
    """Postings you've had on a list, whatever came of them."""
    return db.tracked_ids(conn) | {r[0] for r in conn.execute(
        "SELECT DISTINCT job_id FROM events WHERE kind IN ('picked', 'carried', 'added', 'released')")}


def already_seen(conn) -> tuple[set[str], dict[str, list]]:
    """Postings you've had (any list, any outcome) or that are ruled out, and those roles by same-job key with the
    towns of each posting, so the same role under another link isn't read, screened or offered again."""
    ids = ever_listed(conn) | set(ruled_out(conn))
    keys: dict[str, list] = {}
    for job_id in sorted(ids):
        if job := db.get_listing(conn, job_id):
            keys.setdefault(same_job_key(job["company"], job["title"]), []).append(places(job))
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

    def cooling(job: dict) -> bool:
        return bool(company_keys(job["company"]) & cooldown)

    # Postings outside Canada can take only so many of the day's picks, so they get the same share of the
    # shortlist and of the new screens. Otherwise both fill with postings the pick step has to pass over.
    max_abroad = cfg.get("max_outside_canada_per_day")
    abroad_used = sum(1 for job_id in listed if not in_canada(db.get_listing(conn, job_id) or {}))

    def abroad_share(n: int) -> int:
        if max_abroad is None:
            return n
        return min(n, math.ceil(n * max(0, int(max_abroad) - abroad_used) / max(1, want)))

    fcfg, scfg = cfg.get("fit", {}), cfg.get("screen", {})
    background = drafts.background(profile) if drafts.has_material(profile) else ""
    cand = fit.candidate(profile, background)
    excluded: list[dict] = []

    # Stage 1: metadata ranking -> a shortlist of postings that are still open. Companies you've had lately
    # go last, as in the pick step: they only come back when there aren't enough others.
    shortlist, checked = [], 0
    size = int(fcfg.get("shortlist", 40))
    dupes, away, away_max = 0, 0, abroad_share(size)
    shortlisted = Counter()  # a company with a dozen openings gets a pick plus a spare, not a quarter of the list
    ranked = sorted(picker.rank(db.active_listings(conn), cfg, today, exclude_ids=seen_ids), key=lambda r: cooling(r[0]))
    for job, verdict in ranked:
        if len(shortlist) >= size or checked >= size * 2:
            break
        company = company_key(job["company"])
        if per_company[company] >= limit or shortlisted[company] >= 2 * limit:
            continue
        if is_repeat(job, seen_jobs):
            dupes += 1  # a role you've had, that was ruled out or that is already shortlisted, under another link
            continue
        if away >= away_max and not unplaced(job) and not in_canada(job):
            continue
        checked += 1
        if not still_open(conn, job, log):
            db.deactivate(conn, job["id"])
            continue
        if not in_canada(job):  # asked again: reading the posting can fill in where it is
            if away >= away_max:
                continue
            away += 1
        shortlist.append((job, verdict))
        shortlisted[company] += 1
        seen_jobs.setdefault(same_job_key(job["company"], job["title"]), []).append(places(job))
    log(f"  stage 1: {len(shortlist)} open postings shortlisted, {away} outside Canada ({dupes} repeats skipped)")

    # Stage 2: deterministic fit + eligibility against your resume. Postings whose text can't be read are
    # looked up by Claude first (best-ranked first, a few a day), so they're judged like the rest.
    texts = {job["id"]: description_for(conn, job) for job, _ in shortlist}
    unread = [job for job, _ in shortlist if not texts[job["id"]]]
    found = {}
    if unread and cfg.get("research", {}).get("enabled", True) and llm.backend(cfg["drafts"]) == "claude-cli":
        found = research.research_many(conn, cfg, unread, log)
    scored: list[Scored] = []
    for job, verdict in shortlist:
        desc, source, source_url = texts[job["id"]], "posting", ""
        if not desc and (r := found.get(job["id"])):
            desc, source, source_url = r["text"], "research" if r["verified"] else "research-unverified", r["url"]
        result = fit.evaluate(job, desc, cand, cfg, today)
        if not result.eligible:
            b = result.blockers[0]
            _exclude(excluded, job, b["reason"], b["quote"])
            continue
        scored.append(Scored(job, verdict, result, desc, source=source, source_url=source_url))
    scored.sort(key=Scored.key)
    log(f"  stage 2: {len(scored)} eligible, {len(excluded)} ruled out")

    # Most applications arrive in a posting's first days, so an older one is only worth it as a good fit,
    # judged by the screen when there is one (an old posting not screened yet waits; it isn't ruled out).
    screening = bool(scfg.get("enabled", True) and background and llm.backend(cfg["drafts"]) != "none")
    stale_days = int(cfg["filters"].get("stale_after_days", 21))

    def too_old(s: Scored) -> bool:
        if picker.age_days(s.job, today) <= stale_days:
            return False
        judged = not screening or (s.screen and s.screen.get("fit") is not None)
        return not judged or s.verdict not in GOOD

    # Stage 3: grounded Claude screen of the best few. A posting screened on an earlier day keeps its result
    # for free, so `top` counts only the new ones.
    top = int(scfg.get("top", 12))
    if screening and scored:
        # Companies you haven't had lately are screened first: a posting past stale_after_days can only be picked
        # with a screen, so spending the screens on companies in their cooldown brings those companies back.
        items, new, away, away_max = [], 0, 0, abroad_share(top)
        for s in sorted(scored, key=lambda s: (cooling(s.job), s.key())):
            if not s.description:
                continue
            if not screen.cached(conn, cfg, background, s.job, s.description):
                abroad = not in_canada(s.job)
                if new >= top or (abroad and away >= away_max):
                    continue
                new, away = new + 1, away + abroad
            items.append((s.job, s.description))
        log(f"  stage 3: screening {new} with Claude ({len(items) - new} screened on an earlier day)...")
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
                    if too_old(s):
                        _exclude(excluded, s.job, f"posted {picker.age_days(s.job, today)}d ago and only a {s.verdict} fit "
                                                  f"({s.screen['fit']}/100); after {stale_days} days only good fits are picked")
                        continue
                    # half weight when the text is Claude's own copy of a page JobApps couldn't read
                    trust = 0.5 if s.source == "research-unverified" else 1
                    s.screen_points = round(weight * s.screen["fit"] / 100 * trust)
            kept.append(s)
        scored = sorted(kept, key=Scored.key)

    # Pick: best total first, company cap, at most N stretch/reach roles, at most N outside Canada. Companies
    # you've had in the last week only come back when there aren't enough others.
    reach_names = set().union(*(company_keys(n) for n in cfg.get("reach_companies") or []))
    max_reach, reach_used = int(cfg.get("max_reach_per_day", 1)), 0
    fresh = [s for s in scored if not cooling(s.job)]
    for s in fresh + [s for s in scored if s not in fresh]:
        if len(chosen) >= want:
            break
        company = company_key(s.job["company"])
        if per_company[company] >= limit or too_old(s):
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
            "source": {"kind": s.source, "url": s.source_url},
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


def recheck(conn, cfg: dict, profile: Profile, today: date, dry_run: bool = False) -> list[dict]:
    """Give postings ruled out on an earlier day another chance under today's rules. A ruled-out posting is never
    read again, so the ones a rule stopped stay hidden after that rule is loosened. This puts back those that were
    never on a list, are still open, and that neither the filters, the fit rules nor a screen already stored for
    them would stop today. Each gets a `rechecked` event (the rule-out stays in the record) and is then read and
    screened by the next daily run like any other posting. Returns them, with the reason each had been ruled out."""
    background = drafts.background(profile) if drafts.has_material(profile) else ""
    cand = fit.candidate(profile, background)
    had, back = ever_listed(conn), []
    for job_id, reason in ruled_out(conn).items():
        job = db.get_listing(conn, job_id)
        # a low verified fit or an old posting with a weak one is a measurement, not a rule that can have misfired
        if job_id in had or not job or not job["active"] or reason.startswith(("long shot", "posted ")):
            continue
        text = (db.get_form(conn, job_id) or {}).get("description") or ""
        if not text or not picker.evaluate(job, cfg, today).eligible or not fit.evaluate(job, text, cand, cfg, today).eligible:
            continue
        if (screen.cached(conn, cfg, background, job, text) or {}).get("blockers"):
            continue
        back.append({"id": job_id, "company": job["company"], "title": job["title"], "reason": reason})
        if not dry_run:
            db.log_event(conn, job_id, "rechecked", today.isoformat(), {"was": reason})
    conn.commit()
    return back


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
