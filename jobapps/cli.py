"""Command line entry point. Run `ja <command> -h` for options.

  ja serve      run the dashboard + daily scheduler (open it from your other computer over Tailscale)
  ja today      build (or show) today's batch, draft answers, write the report
  ja apply [N]  fill today's jobs in a browser on this machine, one by one
  ja done N     mark job N applied        ja skip N     drop job N
  ja status     today's batch, backlog and totals
  ja explain N  why job N was picked and how each field will be filled
  ja redraft N  throw away job N's drafted answers and draft again
  ja skills     what the fit check reads from your resume
  ja fetch      refresh listings now      ja doctor     check your setup
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from datetime import date, datetime
from pathlib import Path

from jobapps import config, db, drafts, fit, llm, picker, pipeline, report
from jobapps.config import (BROWSER_PROFILE_DIR, CONFIG_PATH, DATA_DIR, PROFILE_PATH, load_config,
                            load_learned, load_profile)
from jobapps.forms import Field
from jobapps.rules import Resolver, display

STATUS_MARK = {"pending": "[ ]", "applied": "[x]", "skipped": "[-]", "closed": "[c]"}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _setup_output() -> None:
    if sys.stdout is None:  # pythonw.exe (background task): no console
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log_file = DATA_DIR / "jobapps.log"
        if log_file.exists() and log_file.stat().st_size > 5 * 1024 * 1024:  # keep one previous log, cap at ~10 MB
            log_file.replace(DATA_DIR / "jobapps.log.1")
        stream = open(log_file, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
        print(f"\n=== {datetime.now().isoformat(timespec='seconds')} {' '.join(sys.argv[1:])}")
    else:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


def open_file(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)
    else:
        webbrowser.open(path.as_uri())


def print_batch(conn, today: date) -> None:
    rows = db.batch(conn, today.isoformat())
    log(f"\nToday ({today.isoformat()}): {sum(r['status'] != 'pending' for r in rows)} of {len(rows)} done")
    for r in rows:
        job = db.get_listing(conn, r["job_id"]) or {}
        pick = db.get_pick(conn, r["job_id"]) or {}
        tag = f" [{pick['verdict']}]" if pick.get("verdict") not in (None, "unknown") else ""
        log(f"  {r['position']}. {STATUS_MARK.get(r['status'], '[?]')} {job.get('company')} - {job.get('title')}"
            f"  ({job.get('ats')}){tag}")


def resolve_ref(conn, today: date, ref: str) -> dict | None:
    if ref.isdigit() and len(ref) <= 2:
        for row in db.batch(conn, today.isoformat()):
            if row["position"] == int(ref):
                return db.get_listing(conn, row["job_id"])
        return None
    return db.find_listing(conn, ref)


# --- commands -------------------------------------------------------------------

def cmd_today(args, conn, cfg, profile, today):
    path = pipeline.run_daily(conn, cfg, profile, today, refresh=args.refresh, more=args.more,
                              draft=not args.no_draft, fetch_listings=not args.no_fetch, log=log)
    print_batch(conn, today)
    if missing := profile.missing():
        log(f"\nprofile.yaml still needs: {', '.join(missing)}")
    log(f"\nReport: {path}\nNext:   ja apply   (or open the dashboard: ja serve)")
    if args.open:
        open_file(path)


def cmd_serve(args, conn, cfg, profile, today):
    from jobapps import server
    conn.close()
    server.serve(port=args.port)


def cmd_apply(args, conn, cfg, profile, today):
    try:
        from jobapps import filler
    except ImportError:  # Playwright is an optional install; only this command drives a browser itself
        log('`ja apply` needs Playwright: pip install -e ".[browser]" then: playwright install chromium\n'
            "(The Chrome extension fills forms without it.)")
        return 1

    if args.refs:
        jobs = []
        for ref in args.refs:
            job = resolve_ref(conn, today, ref)
            if job is None:
                log(f"No job '{ref}' in today's batch (use a number from `ja status` or a listing id).")
                return 1
            jobs.append(job)
        in_batch = {r["job_id"] for r in db.batch(conn, today.isoformat())}
        db.add_to_batch(conn, today.isoformat(), [j["id"] for j in jobs if j["id"] not in in_batch], how="added")
    else:
        rows = [r for r in db.batch(conn, today.isoformat()) if r["status"] == "pending"]
        if not rows:
            log("Nothing pending today. Run `ja today` (or `ja today --more 3` for extra).")
            return 0
        jobs = [db.get_listing(conn, r["job_id"]) for r in rows]
    if missing := profile.missing():
        log(f"Note: profile.yaml is missing {', '.join(missing)}; those fields will be left for you.")
    results = filler.run(conn, cfg, profile, jobs, today, log)
    for job_id, outcome in results:
        if outcome in ("applied", "skipped"):
            db.set_status(conn, job_id, outcome)
    report.build(conn, cfg, profile, today)
    print_batch(conn, today)
    return 0


def _set_status(args, conn, cfg, profile, today, status):
    for ref in args.refs:
        job = resolve_ref(conn, today, ref)
        if job is None:
            log(f"No job '{ref}'.")
            continue
        db.set_status(conn, job["id"], status, args.note)
        log(f"{STATUS_MARK[status]} {job['company']} - {job['title']}")
    report.build(conn, cfg, profile, today)


def cmd_status(args, conn, cfg, profile, today):
    print_batch(conn, today)
    backlog = db.pending_before(conn, today.isoformat())
    totals = db.counts(conn)
    log(f"\nBacklog from earlier days: {len(backlog)}")
    log("All time: " + (", ".join(f"{k} {v}" for k, v in sorted(totals.items())) if totals else "nothing yet"))
    log(f"Last fetch: {db.get_meta(conn, 'last_fetch') or 'never'}")
    excluded = json.loads(db.get_meta(conn, f"excluded:{today.isoformat()}") or "[]")
    if excluded:
        log(f"\nRuled out today: {len(excluded)}")
        for e in excluded[:10]:
            log(f"  - {e['company']} - {e['title'][:50]}: {e['reason']}")


def cmd_explain(args, conn, cfg, profile, today):
    job = resolve_ref(conn, today, args.ref)
    if job is None:
        log(f"No job '{args.ref}'.")
        return 1
    log(f"{job['company']} - {job['title']}  [{job['id']}]\n{job['url']}")
    log(f"locations: {', '.join(job['locations'])}  terms: {', '.join(job['terms']) or '-'}  "
        f"category: {job['category'] or '-'}  source: {job['source']}")
    pick = db.get_pick(conn, job["id"])
    if pick:
        log(f"\nPicked with score {pick['score']} ({pick['verdict']}{', reach' if pick.get('reach') else ''}):")
        for r in pick["stage1"] + pick["fit"]["points"]:
            log(f"   {r}")
        if pick["fit"]["matched"] or pick["fit"]["missing"]:
            log(f"   skills you have: {', '.join(pick['fit']['matched']) or '-'}")
            log(f"   skills it lists that you don't: {', '.join(pick['fit']['missing']) or '-'}")
        for w in pick["fit"]["warnings"]:
            log(f"   ! {w['reason']}: \"{w['quote'][:120]}\"")
        if pick.get("screen"):
            s = pick["screen"]
            log(f"   +{pick['screen_points']} Claude screen: verified fit {s['fit']}/100 - {s['summary']}")
            for req in s["requirements"]:
                ev = f"  <- \"{req['evidence'][:70]}\"" if req["evidence"] else ""
                log(f"     [{req['status']:7}] {req['requirement'][:80]}{ev}")
            if s["dropped"] or s["downgraded"]:
                log(f"     ({s['dropped']} unquotable requirement(s) dropped, {s['downgraded']} claim(s) without evidence downgraded)")
    else:
        v = picker.evaluate(job, cfg, today)
        log(f"eligible: {v.eligible}" + (f" ({v.reason})" if v.reason else f"  score {v.score}"))
    form = db.get_form(conn, job["id"])
    if not form or form["ats"] == "page":
        log("\nForm not prefetched (this ATS is read in the browser).")
        return 0
    answers = db.answers_for(conn, job["id"])
    resolver = Resolver(profile, job, load_learned(), db.current_answers(answers))
    log(f"\nForm ({form['ats']}, read {form['fetched_at']}):")
    for f in map(Field.from_dict, form["fields"]):
        r = resolver.resolve(f)
        shown = {"draft": "drafted" if r.value else "needs draft", "file": Path(str(r.value)).name}.get(
            r.kind, display(r.value) if r.kind == "fill" else r.note)
        log(f"  {r.kind:6} {f.label[:70]:70} {'*' if f.required else ' '} {shown}" + (f"  ({r.source})" if r.source else ""))
    return 0


def cmd_redraft(args, conn, cfg, profile, today):
    job = resolve_ref(conn, today, args.ref)
    if job is None:
        log(f"No job '{args.ref}'.")
        return 1
    db.clear_answers(conn, job["id"])
    pipeline.prepare_drafts(conn, cfg, profile, today, only=[job["id"]], force=True, log=log)
    log(f"Report: {report.build(conn, cfg, profile, today)}")
    return 0


def cmd_skills(args, conn, cfg, profile, today):
    background = drafts.background(profile) if drafts.has_material(profile) else ""
    cand = fit.candidate(profile, background)
    log(f"Read from: {'resume + background.md' if background else 'nothing yet (add resume.pdf / background.md)'}")
    log(f"Skills ({len(cand.skills)}): {', '.join(sorted(cand.skills)) or '-'}")
    log(f"Graduation year: {cand.grad_year or 'not set'}   GPA: {cand.gpa or 'not set'}   "
        f"Citizenship: {', '.join(cand.citizenship) or 'not set'}")
    log("Add or remove skills with skills.extra / skills.exclude in profile.yaml.")


def cmd_fetch(args, conn, cfg, profile, today):
    pipeline.fetch(conn, cfg, log)


def cmd_report(args, conn, cfg, profile, today):
    path = report.build(conn, cfg, profile, today)
    log(f"Report: {path}")
    open_file(path)


def cmd_history(args, conn, cfg, profile, today):
    for h in db.history(conn, args.limit):
        log(f"  {h['updated_at'][:10]}  {STATUS_MARK.get(h['status'])} {h['company']} - {h['title']}")


def cmd_doctor(args, conn, cfg, profile, today):
    missing = profile.missing()
    log(f"profile.yaml: {'complete' if not missing else 'missing ' + ', '.join(missing)}")
    log(f"resume text: {'found' if drafts.resume_text(profile) else 'none (set files.resume to a PDF in this folder)'}")
    log(f"background for drafts/fit: {'ok' if drafts.has_material(profile) else 'thin - add resume or fill background.md'}")
    kind = llm.backend(cfg["drafts"])
    log(f"LLM backend: {kind}" + {"api": " (ANTHROPIC_API_KEY)", "claude-cli": f" ({shutil.which('claude')}, your Claude login)",
                                  "none": " - prompts only"}.get(kind, ""))
    log(f"browser: {cfg['browser'].get('channel') or 'bundled chromium'}; profile dir {BROWSER_PROFILE_DIR}")
    log(f"listings: {len(db.active_listings(conn))} active; last fetch {db.get_meta(conn, 'last_fetch') or 'never'}")
    if sys.platform == "win32":
        task = subprocess.run(["schtasks", "/query", "/tn", "JobApps Server"], capture_output=True, text=True)
        log(f"server at login: {'installed' if task.returncode == 0 else 'not installed (scripts/install-server.ps1)'}")
    log(f"config: {CONFIG_PATH}\nprofile: {PROFILE_PATH}")
    return 0 if not missing else 1


def main(argv: list[str] | None = None) -> int:
    _setup_output()
    config.ensure_user_files()
    ap = argparse.ArgumentParser(prog="ja", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="act as if today were YYYY-MM-DD")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("serve", help="dashboard + daily scheduler")
    p.add_argument("--port", type=int, default=None)
    p = sub.add_parser("today", help="build/show today's batch and report")
    p.add_argument("--more", type=int, default=0, help="add N more jobs to today's batch")
    p.add_argument("--refresh", action="store_true", help="re-fetch listings and re-read today's forms")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--no-draft", action="store_true")
    p.add_argument("--open", action="store_true", help="open the HTML report")
    p = sub.add_parser("apply", help="fill today's pending jobs in a browser on this machine")
    p.add_argument("refs", nargs="*", help="job numbers from `ja status` or listing ids; default: all pending today")
    for name, help_ in (("done", "mark applied"), ("skip", "drop from your list")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("refs", nargs="+")
        p.add_argument("--note")
    sub.add_parser("status", help="today's batch, backlog, totals")
    p = sub.add_parser("explain", help="why a job was picked and how its fields resolve")
    p.add_argument("ref")
    p = sub.add_parser("redraft", help="re-draft a job's answers")
    p.add_argument("ref")
    sub.add_parser("skills", help="what the fit check reads from your resume")
    sub.add_parser("fetch", help="refresh listings")
    sub.add_parser("report", help="rebuild and open today's HTML report")
    p = sub.add_parser("history", help="recent applications")
    p.add_argument("--limit", type=int, default=30)
    sub.add_parser("doctor", help="check your setup")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 0

    today = date.fromisoformat(args.date) if args.date else date.today()
    cfg, profile = load_config(), load_profile()
    conn = db.connect()
    handlers = {
        "serve": cmd_serve, "today": cmd_today, "apply": cmd_apply, "status": cmd_status, "explain": cmd_explain,
        "redraft": cmd_redraft, "skills": cmd_skills, "fetch": cmd_fetch, "report": cmd_report,
        "history": cmd_history, "doctor": cmd_doctor,
        "done": lambda *a: _set_status(*a, "applied"), "skip": lambda *a: _set_status(*a, "skipped"),
    }
    try:
        return handlers[args.cmd](args, conn, cfg, profile, today) or 0
    except KeyboardInterrupt:
        log("\nStopped.")
        return 130
    finally:
        try:
            conn.close()
        except Exception:
            pass
