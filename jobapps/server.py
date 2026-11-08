"""Dashboard, API and daily scheduler.

Listens on 127.0.0.1 only. Other devices reach it through `tailscale serve` (HTTPS, tailnet-only):
    tailscale serve --bg --https=8443 http://127.0.0.1:8765
Requests that arrive through Tailscale Funnel (the public internet) are always refused, requests
from tailnet users must come from an allowed login, and the Host header is checked so a web page
can't reach the API through DNS rebinding.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import re
import shutil
import subprocess
import threading
import time
import traceback
import zipfile
from collections import deque
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jobapps import config, db, drafts, fit, llm, pipeline, plan, records, sources
from jobapps.config import ROOT, load_config, load_learned, load_profile, save_learned
from jobapps.forms import Field
from jobapps.rules import Resolver, authoritative, choose_option, display, month_year, qkey, rule_source

WEB = Path(__file__).with_name("web")
EXT = Path(__file__).with_name("extension")
FILLER_JS = Path(__file__).with_name("filler.js")

# Fields the dashboard's setup form edits (dotted key, label, kind).
SETUP_FIELDS = [
    ("name.first", "First name", "text"), ("name.last", "Last name", "text"), ("email", "Email", "text"),
    ("phone", "Phone", "text"), ("location", "Location (City, Province, Country)", "text"),
    ("links.linkedin", "LinkedIn URL", "text"), ("links.github", "GitHub URL", "text"), ("links.website", "Website", "text"),
    ("education.school", "School", "text"), ("education.major", "Major", "text"),
    ("education.start_month", "University start month", "month"), ("education.start_year", "University start year", "year"),
    ("education.graduation_month", "Graduation month", "month"), ("education.graduation_year", "Graduation year", "year"),
    ("education.year_of_study", "Year of study, each way forms say it (comma-separated)", "list"), ("education.gpa", "GPA (blank to skip)", "text"),
    ("citizenship", "Citizenship(s), comma-separated", "list"),
    ("work_authorization.canada", "Authorized to work in Canada?", "bool"),
    ("work_authorization.us", "Authorized to work in the US?", "bool"),
    ("needs_sponsorship.canada", "Need visa sponsorship in Canada?", "bool"),
    ("needs_sponsorship.us", "Need visa sponsorship in the US?", "bool"),
    ("preferences.term", "Terms you want (comma-separated)", "list"),
    ("preferences.locations", "Offices you'd pick when asked (comma-separated)", "list"),
]


# Profile fields an answer you type can fill directly (so every wording of the question gets it). Legal
# yes/no answers are not here: those are set deliberately in Setup.
PROFILE_TARGETS = {
    "address.line1", "address.line2", "address.postal_code", "address.city", "address.region", "address.country",
    "phone", "links.website", "links.twitter", "name.middle", "pronouns", "education.gpa", "education.minor",
    "preferences.referrer", "preferences.salary_expectation", "preferences.start_date", "preferences.end_date",
    "experience.current_company", "experience.current_title",
}


def remember(label: str, answer, rule: str = "", kind: str = "", source: str = "you") -> str:
    """Store an answer you gave: into profile.yaml when it's one of PROFILE_TARGETS, else learned_answers.yaml.
    Returns where it went. Sponsorship / work-authorization / consent answers aren't stored (see
    rules.AUTHORITATIVE), nor browser placeholders like "on"."""
    if isinstance(answer, str) and answer.strip().lower() in ("on", "off"):
        return "not stored"
    if authoritative(rule) or authoritative(rule_source(label, load_profile())):
        return "not stored"
    # Only a typed answer becomes a profile value; a picked option ("Below $60,000") belongs to that one question.
    if rule in PROFILE_TARGETS and kind in ("text", "combobox", "") and isinstance(answer, str) and answer.strip():
        try:
            config.update_profile({rule: answer.strip()})
            return f"profile: {rule}"
        except (KeyError, ValueError):
            pass  # not in this profile.yaml's layout: keep it as a learned answer instead
    if isinstance(answer, list) and len(answer) == 1:
        answer = answer[0]
    with config.WRITE_LOCK:
        by_key = {qkey(e["label"]): e for e in load_learned()}
        by_key[qkey(label)] = {"label": label, "answer": answer, "type": kind, "learned_from": source,
                               "date": date.today().isoformat()}
        save_learned(list(by_key.values()))
    return "learned"


def unanswered_items(fields: list[dict], actions: list[dict]) -> list[dict]:
    """Questions on a page that JobApps left for you and has no answer for (not essays, uploads or consent boxes
    you should read each time, and not fields inside repeated blocks like "Work Experience 2")."""
    by_id = {f["id"]: f for f in fields}
    out = []
    for a in actions:
        f = by_id.get(a["id"]) or {}
        if a.get("state") not in ("missing", "manual") or a.get("op") or not a.get("label"):
            continue
        if f.get("type") in ("file", "textarea") or a.get("rule") in ("draft", "consent", "captcha") or f.get("section"):
            continue
        if plan.has_value(f.get("value")):
            continue
        out.append({"qkey": qkey(a["label"]), "label": a["label"], "type": f.get("type", ""), "options": f.get("options") or [],
                    "rule": a.get("rule") or "", "required": a.get("required")})
    return out


# --- background pipeline runner -------------------------------------------------

class Runner:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.lines: deque[str] = deque(maxlen=300)
        self.last_finished: str | None = None
        self.last_error: str | None = None
        self.task = ""

    def log(self, msg: str = "") -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        self.lines.append(line)
        print(line, flush=True)

    def start(self, task: str, fn) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running, self.task, self.last_error = True, task, None
        threading.Thread(target=self._wrap, args=(fn,), daemon=True).start()
        return True

    def _wrap(self, fn):
        conn = db.connect()
        try:
            fn(conn, load_config(), load_profile())
        except Exception as exc:  # surfaced in the dashboard instead of killing the thread
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.log(f"! {self.last_error}")
            traceback.print_exc()
        finally:
            conn.close()
            self.last_finished = datetime.now().isoformat(timespec="seconds")
            self.running = False
            self.log(f"done: {self.task}")

    def state(self) -> dict:
        return {"running": self.running, "task": self.task, "lines": list(self.lines)[-60:],
                "last_finished": self.last_finished, "last_error": self.last_error}


RUNNER = Runner()


def run_daily(more: int = 0, refresh: bool = False, rebuild: bool = False, backup: bool = False):
    def job(conn, cfg, profile):
        today = date.today()
        if backup:
            db.backup(keep=7)
        extra = more
        if rebuild:
            n = db.reset_new_picks(conn, today.isoformat())
            RUNNER.log(f"re-picking today ({n} untouched pick(s) released)")
            rows = db.batch(conn, today.isoformat())
            kept = sum(1 for r in rows if r["first_assigned"] == today.isoformat())  # carried ones don't count
            extra = max(0, int(cfg["daily_count"]) - kept) if rows else 0
        pipeline.run_daily(conn, cfg, profile, today, refresh=refresh, more=extra, log=RUNNER.log)
    return job


SCHEDULE = {"last_attempt": None, "next_run": None}
RETRY_MINUTES = 60


def schedule_decision(now: datetime, at, has_batch: bool, last_attempt: datetime | None) -> tuple[bool, datetime]:
    """(run now?, when the next run will be). Pure, so it can be tested."""
    today_at = datetime.combine(now.date(), at)
    if has_batch:
        return False, today_at + timedelta(days=1)
    if now < today_at:
        return False, today_at
    if last_attempt is None or last_attempt.date() != now.date() or now - last_attempt >= timedelta(minutes=RETRY_MINUTES):
        return True, now + timedelta(minutes=RETRY_MINUTES)
    return False, last_attempt + timedelta(minutes=RETRY_MINUTES)


def scheduler():
    """Build the day's list at server.daily_at (or as soon as the PC is awake after that). If a run fails or
    finds nothing - network down, a feed broken - it tries again every hour until today's list exists."""
    while True:
        try:
            at = datetime.strptime(str(load_config().get("server", {}).get("daily_at", "07:30")), "%H:%M").time()
            now = datetime.now()
            conn = db.connect()
            has_batch = bool(db.batch(conn, now.date().isoformat()))
            conn.close()
            due, next_run = schedule_decision(now, at, has_batch, SCHEDULE["last_attempt"])
            if due and not RUNNER.running:
                SCHEDULE["last_attempt"] = now
                RUNNER.start("daily run", run_daily(backup=True))
            SCHEDULE["next_run"] = next_run.isoformat(timespec="minutes")
        except Exception:
            traceback.print_exc()
        time.sleep(60)


# --- tailscale ------------------------------------------------------------------

def tailscale_info() -> dict:
    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    me = data.get("Self") or {}
    user = (data.get("User") or {}).get(str(me.get("UserID")), {})
    return {"dns": (me.get("DNSName") or "").rstrip("."), "ips": me.get("TailscaleIPs") or [],
            "login": user.get("LoginName", ""), "host": me.get("HostName", "")}


# --- view models ----------------------------------------------------------------

def _drafts(answers: dict) -> dict[str, str]:
    return {k: a["final_answer"] or a["answer"] for k, a in answers.items() if a["final_answer"] or a["answer"]}


def _question(key: str, label: str, required: bool, limit: str, a: dict | None) -> dict:
    a = a or {}
    return {"qkey": key, "label": label, "required": required, "limit": limit,
            "answer": a.get("final_answer") or a.get("answer"), "edited": bool(a.get("final_answer")), "checks": a.get("checks")}


def _prompt_text(path: str) -> str | None:
    p = Path(path) if path else None
    return p.read_text(encoding="utf-8").split("# User prompt", 1)[-1].strip() if p and p.exists() else None


def job_view(conn, cfg, profile, learned, row: dict, today: date, day: str | None = None) -> dict | None:
    job = db.get_listing(conn, row["job_id"])
    if job is None:
        return None
    form = db.get_form(conn, job["id"])
    answers = db.answers_for(conn, job["id"])
    pick = db.get_pick(conn, job["id"])
    resolver = Resolver(profile, job, learned, _drafts(answers))
    questions, plan_rows = [], []
    readable = bool(form and form["ats"] != "page")
    for f in map(Field.from_dict, (form or {}).get("fields") or []):
        res = resolver.resolve(f)
        plan_rows.append({"label": f.label, "required": f.required, "kind": res.kind,
                          "value": display(res.value) if res.kind == "fill" else Path(str(res.value)).name if res.kind == "file" else "",
                          "note": res.note or res.source})
        if res.kind == "draft":
            questions.append(_question(qkey(f.label), f.label, f.required, drafts.length_limit(f), answers.get(qkey(f.label))))
    if not readable:  # answers the extension drafted on the live form (e.g. Workday) still show and can be redrafted
        for k, a in answers.items():
            if a.get("answer") or a.get("final_answer"):
                f = Field(key=k, label=a["question"] or k, type="textarea")
                questions.append(_question(k, f.label, False, drafts.length_limit(f), a))
    # The prompt behind the first drafts; a redrafted answer shows its own prompt under it.
    firsts = [a["prompt_file"] for a in answers.values() if a.get("prompt_file") and "-redraft-" not in a["prompt_file"]]
    prompt = _prompt_text(max(set(firsts), key=firsts.count)) if firsts else None
    for q in questions:
        pf = (answers.get(q["qkey"]) or {}).get("prompt_file") or ""
        q["prompt"] = _prompt_text(pf) if "-redraft-" in pf else None
    posted = datetime.fromtimestamp(job["date_posted"]).date() if job["date_posted"] else None
    return {
        "id": job["id"], "position": row["position"], "status": row["status"], "company": job["company"],
        "title": job["title"], "locations": job["locations"], "ats": job["ats"], "source": job["source"],
        "url": (form or {}).get("apply_url") or job["url"], "posting_url": job["url"],
        "age_days": (today - posted).days if posted else None, "first_assigned": row.get("first_assigned"),
        "carried": row.get("first_assigned") not in (None, day or today.isoformat()),
        "pick": pick, "readable": readable, "questions": questions, "plan": plan_rows, "prompt": prompt,
    }


HISTORY_KEYS = ("title", "company", "location", "start", "end", "description")


def setup_view(profile) -> dict:
    cfg = load_config()
    resume = profile.file("files.resume")
    background = drafts.background(profile) if drafts.has_material(profile) else ""
    cand = fit.candidate(profile, background)
    values = {}
    for key, _, kind in SETUP_FIELDS:
        v = profile.get(key)
        values[key] = ", ".join(map(str, v)) if kind == "list" and isinstance(v, list) else v
    return {
        "missing": profile.missing(), "fields": [{"key": k, "label": l, "kind": t} for k, l, t in SETUP_FIELDS],
        "values": values, "resume": {"name": resume.name, "size": resume.stat().st_size} if resume else None,
        "skills": sorted(cand.skills), "skills_list": profile.get("skills.list") or [],
        "writing_preferences": profile.get("writing_preferences") or [],
        "history": [{k: str(j.get(k) or "") for k in HISTORY_KEYS} for j in profile.get("experience.history") or []],
        "background": drafts.notes_text() if config.BACKGROUND_PATH.exists() else "",
        "background_raw": config.BACKGROUND_PATH.read_text(encoding="utf-8") if config.BACKGROUND_PATH.exists() else "",
        "backend": llm.backend(cfg["drafts"]), "model": cfg["drafts"]["model"],
        "daily_at": cfg.get("server", {}).get("daily_at", "07:30"),
        "health": health(),
    }


STARTED = datetime.now().isoformat(timespec="seconds")


def health() -> dict:
    """What the dashboard shows under Setup > System: is everything that runs unattended working?"""
    conn = db.connect()
    try:
        stats = json.loads(db.get_meta(conn, "source_stats") or "null") or {}
        last_fetch = db.get_meta(conn, "last_fetch")
        last_run = db.get_meta(conn, "last_run") or RUNNER.last_finished
    finally:
        conn.close()
    backups = sorted((config.DATA_DIR / "backups").glob("jobapps-*.db")) if (config.DATA_DIR / "backups").exists() else []
    llm_status = dict(llm.STATUS)
    llm_status["failing"] = bool(llm_status["last_error_at"]) and (llm_status["last_ok"] or "") < llm_status["last_error_at"]
    return {
        "server_started": STARTED, "next_run": SCHEDULE["next_run"], "last_run": last_run,
        "last_run_error": RUNNER.last_error, "last_fetch": last_fetch,
        "sources_ok": stats.get("all_ok"), "sources": len(stats.get("sources") or []),
        "claude": llm_status, "claude_cli": llm.claude_exe(),
        "latest_backup": backups[-1].name if backups else None, "backups": len(backups),
    }


def state(conn, day: str | None = None) -> dict:
    """One day's list (today by default). Before today's list is built, today shows the jobs you
    haven't finished from earlier days, since those are what today's run carries over first."""
    cfg, profile, learned, today = load_config(), load_profile(), load_learned(), date.today()
    view = day if day and day < today.isoformat() else today.isoformat()
    rows = db.batch(conn, view)
    preview = view == today.isoformat() and not rows
    if preview:
        rows = db.unfinished(conn, view)
    jobs = [j for r in rows if (j := job_view(conn, cfg, profile, learned, r, today, view))]
    secs = db.active_seconds(conn, [j["id"] for j in jobs])
    daily = int(cfg["daily_count"])
    fresh = 0
    for j in jobs:
        j["seconds"] = secs.get(j["id"], 0)
        listed = [e for e in db.events_for(conn, j["id"]) if e["kind"] in ("picked", "carried", "added") and e["day"] == view]
        j["how"] = "carried" if preview else (listed[0]["kind"] if listed else ("carried" if j["carried"] else "picked"))
        if j["how"] != "carried":
            fresh += 1
        j["extra"] = j["how"] != "carried" and fresh > daily  # beyond the day's main list
    return {
        "date": view, "today": today.isoformat(), "preview": preview, "days": db.days(conn),
        "daily_count": int(cfg["daily_count"]), "next_run": SCHEDULE["next_run"],
        "jobs": jobs, "done": sum(1 for j in jobs if j["status"] != "pending"),
        "unanswered": len(db.unanswered(conn)),
        "excluded": json.loads(db.get_meta(conn, f"excluded:{view}") or "[]"),
        "totals": db.counts(conn), "backlog": len(db.pending_before(conn, today.isoformat())),
        "last_fetch": db.get_meta(conn, "last_fetch"), "run": RUNNER.state(),
        "sources": json.loads(db.get_meta(conn, "source_stats") or "null"),
        "setup": {"missing": profile.missing(), "has_material": drafts.has_material(profile),
                  "backend": llm.backend(cfg["drafts"])},
        "claude_failing": llm.STATUS["last_error"] if (llm.STATUS["last_error_at"] or "") > (llm.STATUS["last_ok"] or "") else None,
    }


# --- extension support ------------------------------------------------------------

def match_job(conn, urls: list[str]) -> dict | None:
    today = date.today().isoformat()
    for url in urls:
        if not url or not url.startswith("http"):
            continue
        job = db.get_listing(conn, sources.listing_id(url))
        if job:
            return job
    keys = {sources.url_key(u) for u in urls if u and u.startswith("http")}
    for row in db.batch(conn, today):
        form = db.get_form(conn, row["job_id"]) or {}
        job = db.get_listing(conn, row["job_id"])
        if job and keys & {sources.url_key(job["url"]), sources.url_key(form.get("apply_url") or job["url"])}:
            return job
    return None


def adhoc_job(url: str, title: str) -> dict:
    host = urlparse(url).netloc.removeprefix("www.")
    return {"id": sources.listing_id(url), "company": (title or host).split(" - ")[-1].split(" | ")[-1][:60] or host,
            "title": (title or "Application")[:120], "url": url, "locations": [], "source": "page", "ats": sources.detect_ats(url)}


def extension_build(origin: str) -> str:
    """A fingerprint of every extension file: when it changes, a copy kept in sync by mac-setup.sh updates."""
    digest = hashlib.sha256(origin.encode() + FILLER_JS.read_bytes())
    for f in sorted(EXT.rglob("*")):
        if f.is_file():
            digest.update(f.relative_to(EXT).as_posix().encode() + f.read_bytes())
    version = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))["version"]
    return f"{version} build {digest.hexdigest()[:8]}"


def keep_copy(job_id: str) -> None:
    """Postings are taken down when they close: keep the text of every one you applied to (in the background)."""
    def work():
        conn = db.connect()
        try:
            job, form = db.get_listing(conn, job_id), db.get_form(conn, job_id)
            if job and not (form and (form.get("description") or "").strip()):
                pipeline.description_for(conn, job)
        except Exception as exc:  # the posting may already be gone; the link and your answers are still kept
            RUNNER.log(f"  couldn't save a copy of the posting {job_id}: {exc}")
        finally:
            conn.close()
    threading.Thread(target=work, daemon=True).start()


def build_extension(server_url: str) -> bytes:
    origin = server_url.rstrip("/")
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    manifest["host_permissions"] = [f"{origin}/*"] + manifest.get("host_permissions", [])
    manifest["version_name"] = extension_build(origin)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("jobapps-extension/manifest.json", json.dumps(manifest, indent=2))
        z.writestr("jobapps-extension/config.js", f"const JOBAPPS_SERVER = {json.dumps(origin)};\n")
        z.write(FILLER_JS, "jobapps-extension/filler.js")
        for f in sorted(EXT.rglob("*")):
            if f.is_file() and f.name != "manifest.json":
                z.write(f, f"jobapps-extension/{f.relative_to(EXT).as_posix()}")
    return buf.getvalue()


# --- HTTP -------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "JobApps/1.0"
    allowed_hosts: set[str] = set()
    allowed_logins: set[str] = set()

    def log_message(self, fmt, *args):  # quiet access log
        pass

    # auth --------------------------------------------------------------------
    def _allowed(self) -> bool:
        if self.headers.get("Tailscale-Funnel-Request"):
            return False  # came in over the public internet
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in self.allowed_hosts:
            return False
        login = self.headers.get("Tailscale-User-Login")
        if login is not None:
            return not self.allowed_logins or login.lower() in self.allowed_logins
        return self.client_address[0] in ("127.0.0.1", "::1")

    # helpers -----------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200):
        self._send(code, json.dumps(data, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20 * 1024 * 1024:
            raise ValueError("upload too large")
        return self.rfile.read(n) if n else b""

    def _jbody(self) -> dict:
        raw = self._body()
        return json.loads(raw) if raw else {}

    def _route(self, method: str):
        if not self._allowed():
            return self._send(403, b"forbidden", "text/plain")
        path = urlparse(self.path).path
        try:
            for m, pattern, fn in ROUTES:
                if m == method and (match := re.fullmatch(pattern, path)):
                    return fn(self, *match.groups())
            if method == "GET":
                return self._static(path)
            self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return  # the browser went away (page reloaded, tab closed): nothing to answer
        except Exception as exc:
            traceback.print_exc()
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

    def _static(self, path: str):
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        file = (WEB / name).resolve()
        if WEB.resolve() not in file.parents or not file.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = {".js": "text/javascript", ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml"}.get(
            file.suffix, mimetypes.guess_type(file.name)[0] or "application/octet-stream")
        self._send(200, file.read_bytes(), f"{ctype}; charset=utf-8" if ctype.startswith(("text", "application/json")) else ctype,
                   {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                                               "font-src https://fonts.gstatic.com; img-src 'self' data:"} if file.suffix == ".html" else None)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    # dashboard API -------------------------------------------------------------
    def api_state(self):
        day = (parse_qs(urlparse(self.path).query).get("day") or [""])[0]
        conn = db.connect()
        try:
            self._json(state(conn, day if re.fullmatch(r"\d{4}-\d\d-\d\d", day) else None))
        finally:
            conn.close()

    def api_run_state(self):
        self._json(RUNNER.state())

    def api_run(self):
        body = self._jbody()
        started = RUNNER.start("re-pick today" if body.get("rebuild") else "more jobs" if body.get("more") else "refresh",
                               run_daily(more=int(body.get("more") or 0), refresh=bool(body.get("refresh")),
                                         rebuild=bool(body.get("rebuild"))))
        self._json({"started": started, "run": RUNNER.state()})

    def api_status(self, job_id):
        status = self._jbody().get("status")
        if status not in ("pending", "applied", "skipped"):
            return self._json({"error": "bad status"}, 400)
        conn = db.connect()
        try:
            db.set_status(conn, job_id, status)
        finally:
            conn.close()
        if status == "applied":
            keep_copy(job_id)
        self._json({"ok": True})

    def api_answer(self, job_id):
        body = self._jbody()
        conn = db.connect()
        try:
            db.save_final_answer(conn, job_id, body["qkey"], body.get("text") or None)
        finally:
            conn.close()
        self._json({"ok": True})

    def api_redraft(self, job_id):
        def job(conn, cfg, profile):
            db.clear_answers(conn, job_id)
            pipeline.prepare_drafts(conn, cfg, profile, date.today(), only=[job_id], force=True, log=RUNNER.log)
        self._json({"started": RUNNER.start("redraft", job)})

    def api_redraft_one(self, job_id):
        """Redraft one answer from the dashboard, with your instruction; optionally remember it for every draft."""
        body = self._jbody()
        key, instruction = body.get("qkey") or "", str(body.get("instruction") or "").strip()[:2000]
        if body.get("remember") and instruction:
            prefs = load_profile().get("writing_preferences") or []
            if instruction not in prefs:
                config.update_profile({"writing_preferences": [*prefs, instruction]})
        conn = db.connect()
        try:
            job, a = db.get_listing(conn, job_id), db.answers_for(conn, job_id).get(key)
            if not job or not a:
                return self._json({"error": "that answer isn't on this job any more"}, 404)
            form = db.get_form(conn, job_id) or {}
            f = next((x for x in map(Field.from_dict, form.get("fields") or []) if qkey(x.label) == key),
                     Field(key=key, label=a["question"] or key, type="textarea"))
            cfg = load_config()
            try:
                result = drafts.redraft_answer(conn, cfg["drafts"], load_profile(), job, form.get("description") or "", f,
                                               instruction, a.get("final_answer") or a.get("answer") or "", date.today())
            except llm.LLMError as exc:
                return self._json({"error": f"Claude couldn't redraft it: {exc}"}, 502)
            self._json({"ok": True, **result})
        finally:
            conn.close()

    def api_opened(self, job_id):
        """You pressed Open application on the dashboard: the clock starts."""
        conn = db.connect()
        try:
            db.log_event(conn, job_id, "opened")
            self._json({"seconds": db.add_activity(conn, job_id)})
        finally:
            conn.close()

    def _filters(self) -> dict:
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        return {"q": q.get("q", "")[:200], "status": q.get("status", "all"), "region": q.get("region", "all"),
                "active": q.get("active", "all"), "sort": q.get("sort", "newest"),
                "page": max(1, int(q["page"])) if q.get("page", "").isdigit() else 1}

    def api_postings(self):
        conn = db.connect()
        try:
            today_ids = {r["job_id"] for r in db.batch(conn, date.today().isoformat())}
            self._json(records.postings(conn, **self._filters(), today_ids=today_ids))
        finally:
            conn.close()

    def api_postings_csv(self):
        conn = db.connect()
        try:
            f = self._filters()
            f.pop("page")
            body = records.postings_csv(conn, **f).encode("utf-8-sig")  # BOM so Excel/Numbers read the accents
        finally:
            conn.close()
        self._send(200, body, "text/csv; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="jobapps-postings-{date.today().isoformat()}.csv"'})

    def api_add_today(self, job_id):
        """Put any posting on today's list (an extra on top of the daily ones): check it's open, read its form, draft."""
        def job(conn, cfg, profile):
            listing = db.get_listing(conn, job_id)
            if listing is None:
                raise ValueError("no such posting")
            db.add_to_batch(conn, date.today().isoformat(), [job_id], how="added")
            RUNNER.log(f"Added {listing['company']}: {listing['title']}")
            if not pipeline.still_open(conn, listing, RUNNER.log):
                db.set_status(conn, job_id, "closed", "posting closed")
                return
            pipeline.prepare_drafts(conn, cfg, profile, date.today(), only=[job_id], log=RUNNER.log)
        self._json({"started": RUNNER.start("add to today", job)})

    def api_memory(self):
        """Questions waiting for your answer, and every answer JobApps remembers."""
        conn = db.connect()
        try:
            open_qs = [{**q, "to_profile": q["rule"] in PROFILE_TARGETS} for q in db.unanswered(conn)]
        finally:
            conn.close()
        learned = [{**e, "qkey": qkey(e["label"])} for e in load_learned()]
        self._json({"unanswered": open_qs, "learned": learned})

    def api_memory_answer(self):
        body = self._jbody()
        label, answer = str(body.get("label") or "").strip(), body.get("answer")
        if not label or answer in (None, "", []):
            return self._json({"error": "type an answer first"}, 400)
        where = remember(label, answer, body.get("rule") or "", body.get("type") or "", "you")
        conn = db.connect()
        try:
            db.answered(conn, [qkey(label)])
        finally:
            conn.close()
        self._json({"ok": True, "where": where})

    def api_memory_forget(self):
        key = self._jbody().get("qkey") or ""
        with config.WRITE_LOCK:
            save_learned([e for e in load_learned() if qkey(e["label"]) != key])
        self._json({"ok": True})

    def api_memory_dismiss(self):
        conn = db.connect()
        try:
            db.answered(conn, [self._jbody().get("qkey") or ""], dismiss=True)
        finally:
            conn.close()
        self._json({"ok": True})

    def api_stats(self):
        conn = db.connect()
        try:
            self._json(records.stats(conn))
        finally:
            conn.close()

    def api_history(self):
        conn = db.connect()
        try:
            self._json(db.history(conn, 200))
        finally:
            conn.close()

    def api_days(self):
        conn = db.connect()
        try:
            self._json(db.days(conn))
        finally:
            conn.close()

    def api_setup(self):
        self._json(setup_view(load_profile()))

    def api_setup_profile(self):
        updates = {}
        kinds = {k: t for k, _, t in SETUP_FIELDS}
        for key, value in self._jbody().items():
            if key not in kinds:
                continue
            kind = kinds[key]
            if kind == "bool":
                value = {"true": True, "false": False}.get(str(value).lower(), "TODO")
            elif kind == "list":
                value = [v.strip() for v in str(value or "").split(",") if v.strip()]
            elif kind == "year":
                value = int(value) if str(value).isdigit() else "TODO"
            else:
                value = str(value or "").strip() or ("TODO" if key in ("phone",) else "")
            updates[key] = value
        config.update_profile(updates)
        self._json({"ok": True, "setup": setup_view(load_profile())})

    def api_setup_experience(self):
        """Work history and the skills list, edited in Setup (typed into Workday's My Experience page)."""
        body = self._jbody()
        jobs = []
        for j in body.get("history") or []:
            job = {k: str(j.get(k) or "").strip() for k in HISTORY_KEYS}
            if not (job["title"] or job["company"]):
                continue
            for k in ("start", "end"):
                if job[k] and not (month_year(job[k]) or (k == "end" and job[k].lower() == "present")):
                    return self._json({"error": f"write dates like 'June 2026' ({job['title'] or job['company']}: {job[k]})"}, 400)
            jobs.append(job)
        skills = list(dict.fromkeys(s.strip() for s in body.get("skills") or [] if s.strip()))
        config.update_profile({"experience.history": jobs, "skills.list": skills})
        self._json({"ok": True, "setup": setup_view(load_profile())})

    def api_setup_preferences(self):
        prefs = [str(x).strip()[:2000] for x in self._jbody().get("items") or [] if str(x).strip()]
        config.update_profile({"writing_preferences": prefs})
        self._json({"ok": True, "setup": setup_view(load_profile())})

    def api_setup_background(self):
        text = self._body().decode("utf-8")
        config.atomic_write(config.BACKGROUND_PATH, text)
        self._json({"ok": True, "setup": setup_view(load_profile())})

    def api_setup_resume(self):
        data = self._body()
        if not data.startswith(b"%PDF"):
            return self._json({"error": "please upload a PDF"}, 400)
        (ROOT / "resume.pdf").write_bytes(data)
        if load_profile().get("files.resume") != "resume.pdf":
            config.update_profile({"files.resume": "resume.pdf"})
        profile = load_profile()
        drafts.resume_text(profile)  # extract now so errors show up immediately
        # Today's list was picked without this resume: re-pick the jobs you haven't started.
        repicking = RUNNER.start("re-pick with your resume", run_daily(rebuild=True))
        self._json({"ok": True, "setup": setup_view(profile), "repicking": repicking})

    def api_mac_setup(self):
        """The one-time Mac command that keeps the extension updated: curl -fsSL <server>/mac-setup.sh | bash"""
        host = self.headers.get("Host") or "127.0.0.1:8765"
        scheme = "http" if host.startswith(("127.0.0.1", "localhost")) else "https"
        script = (ROOT / "scripts" / "mac-setup.sh").read_text(encoding="utf-8").replace("__SERVER__", f"{scheme}://{host}")
        self._send(200, script.encode("utf-8"), "text/plain; charset=utf-8")

    def _origin(self) -> str:
        host = self.headers.get("Host") or "127.0.0.1:8765"
        return f"{'http' if host.startswith(('127.0.0.1', 'localhost')) else 'https'}://{host}"

    def api_extension_build(self):
        """What mac-setup.sh's sync asks first, so it only downloads the extension when it changed."""
        self._send(200, extension_build(self._origin()).encode(), "text/plain; charset=utf-8")

    def api_extension(self):
        body = build_extension(self._origin())
        self._send(200, body, "application/zip", {"Content-Disposition": 'attachment; filename="jobapps-extension.zip"'})

    # extension API -------------------------------------------------------------
    def _job_for(self, conn, body) -> dict:
        urls = [body.get("frame_url"), body.get("url")]
        if body.get("job_id"):
            job = db.get_listing(conn, body["job_id"])
            if job:
                return job
        return match_job(conn, urls) or adhoc_job(body.get("url") or "", body.get("title") or "")

    def api_ext_plan(self):
        body = self._jbody()
        conn = db.connect()
        try:
            job = self._job_for(conn, body)
            profile = load_profile()
            resolver = Resolver(profile, job, load_learned(), _drafts(db.answers_for(conn, job["id"])))
            fields = body.get("fields") or []
            in_batch = any(r["job_id"] == job["id"] for r in db.batch(conn, date.today().isoformat()))
            actions = plan.actions(resolver, fields)
            if fields:
                db.note_unanswered(conn, unanswered_items(fields, actions), job["company"], job["id"])
            self._json({
                "job": {"id": job["id"], "company": job["company"], "title": job["title"], "known": job.get("source") != "page",
                        "in_batch": in_batch, "seconds": db.active_seconds(conn, [job["id"]]).get(job["id"], 0)},
                "actions": actions,
                "sections": plan.sections(resolver),
                "needs_draft": [f["id"] for f in plan.needs_draft(resolver, fields)],
                "can_draft": llm.backend(load_config()["drafts"]) != "none" and drafts.has_material(profile),
            })
        finally:
            conn.close()

    def api_ext_choose(self):
        body = self._jbody()
        options = [o.strip() for o in body.get("options") or []]
        if body.get("exact"):  # skills: only the skill itself, never a longer one that contains it
            choice = next((c for c in map(lambda v: plan.choose_exact(v, options), body.get("candidates") or []) if c), None)
        else:
            choice = choose_option(body.get("candidates") or [], options)
        if choice is None and body.get("pick_first") and options:
            choice = options[0]
        self._json({"choice": choice, "index": options.index(choice) if choice is not None else -1})

    def api_ext_draft(self):
        body = self._jbody()
        conn = db.connect()
        try:
            job = self._job_for(conn, body)
            cfg, profile = load_config(), load_profile()
            fields = [plan.to_field(f) for f in body.get("fields") or []]
            form = db.get_form(conn, job["id"]) or {}
            description = form.get("description") or (body.get("page_text") or "")[:40000]
            if not form and description:  # keep the page text so a redraft from the dashboard has the same context
                db.save_form(conn, job["id"], "page", description, [], body.get("url") or "")
            result = drafts.draft_job(conn, cfg["drafts"], profile, job, description, fields, date.today(),
                                      maxlengths={qkey(f["label"]): f["maxlength"] for f in body.get("fields") or [] if f.get("maxlength")},
                                      log=RUNNER.log)
            self._json({"status": result.get("status"), "error": result.get("error")})
        finally:
            conn.close()

    def api_ext_learn(self):
        body = self._jbody()
        entries = [e for e in body.get("entries") or [] if e.get("label") and e.get("answer") not in (None, "", [])]
        conn = db.connect()
        try:
            job = self._job_for(conn, body)
            for e in body.get("edited_drafts") or []:
                db.save_final_answer(conn, job["id"], qkey(e["label"]), str(e["answer"]).strip())
        finally:
            conn.close()
        where = [remember(e["label"], e["answer"], e.get("rule") or "", e.get("type", ""), job["company"]) for e in entries]
        if entries:
            conn = db.connect()
            try:
                db.answered(conn, [qkey(e["label"]) for e in entries])
            finally:
                conn.close()
        self._json({"ok": True, "saved": len(entries), "where": where})

    def api_ext_status(self):
        body = self._jbody()
        status = body.get("status", "applied")
        if status not in ("applied", "skipped", "closed", "pending"):
            return self._json({"error": "bad status"}, 400)
        conn = db.connect()
        try:
            job = self._job_for(conn, body)
            if db.get_listing(conn, job["id"]) is None:  # a job you found yourself: keep it in your record
                db.upsert_listings(conn, [{**job, "terms": [], "category": "", "sponsorship": "", "degrees": [],
                                           "date_posted": 0, "active": True}], deactivate_missing=False)
            db.set_status(conn, job["id"], status, url=body.get("form_url") or body.get("frame_url") or body.get("url"))
        finally:
            conn.close()
        if status == "applied":
            keep_copy(job["id"])
        self._json({"ok": True})

    def api_ext_activity(self):
        """The timer: the extension reports each 30 seconds you spend working on an application page."""
        body = self._jbody()
        conn = db.connect()
        try:
            job = self._job_for(conn, body)
            self._json({"seconds": db.add_activity(conn, job["id"])})
        finally:
            conn.close()

    def api_posting(self, job_id):
        """One posting's record: links, the saved copy of its text, and its timeline."""
        conn = db.connect()
        try:
            job, form = db.get_listing(conn, job_id), db.get_form(conn, job_id) or {}
            if job is None:
                return self._json({"error": "no such posting"}, 404)
            self._json({"id": job_id, "company": job["company"], "title": job["title"], "url": job["url"],
                        "locations": job["locations"], "description": form.get("description") or "",
                        "saved_at": (form.get("fetched_at") or "")[:16], "events": db.events_for(conn, job_id),
                        "answers": [{"question": a["question"], "answer": a["final_answer"] or a["answer"]}
                                    for a in db.answers_for(conn, job_id).values() if a["final_answer"] or a["answer"]],
                        "minutes": round(db.active_seconds(conn, [job_id]).get(job_id, 0) / 60, 1)})
        finally:
            conn.close()

    def api_ext_file(self, key):
        profile = load_profile()
        path = profile.file(key) if key in ("files.resume", "files.cover_letter", "files.transcript") else None
        if not path:
            return self._json({"error": "no such file"}, 404)
        self._json({"name": path.name, "type": mimetypes.guess_type(path.name)[0] or "application/pdf",
                    "b64": base64.b64encode(path.read_bytes()).decode("ascii")})


ROUTES = [
    ("GET", r"/api/state", Handler.api_state),
    ("GET", r"/api/run", Handler.api_run_state),
    ("POST", r"/api/run", Handler.api_run),
    ("POST", r"/api/job/(\w+)/status", Handler.api_status),
    ("POST", r"/api/job/(\w+)/answer", Handler.api_answer),
    ("POST", r"/api/job/(\w+)/redraft", Handler.api_redraft),
    ("POST", r"/api/job/(\w+)/redraft-one", Handler.api_redraft_one),
    ("GET", r"/api/history", Handler.api_history),
    ("GET", r"/api/postings", Handler.api_postings),
    ("GET", r"/api/postings\.csv", Handler.api_postings_csv),
    ("POST", r"/api/postings/(\w+)/add", Handler.api_add_today),
    ("POST", r"/api/job/(\w+)/opened", Handler.api_opened),
    ("GET", r"/api/stats", Handler.api_stats),
    ("GET", r"/api/memory", Handler.api_memory),
    ("POST", r"/api/memory/answer", Handler.api_memory_answer),
    ("POST", r"/api/memory/forget", Handler.api_memory_forget),
    ("POST", r"/api/memory/dismiss", Handler.api_memory_dismiss),
    ("GET", r"/api/posting/(\w+)", Handler.api_posting),
    ("POST", r"/api/ext/activity", Handler.api_ext_activity),
    ("GET", r"/api/days", Handler.api_days),
    ("GET", r"/api/setup", Handler.api_setup),
    ("POST", r"/api/setup/profile", Handler.api_setup_profile),
    ("PUT", r"/api/setup/background", Handler.api_setup_background),
    ("PUT", r"/api/setup/experience", Handler.api_setup_experience),
    ("PUT", r"/api/setup/preferences", Handler.api_setup_preferences),
    ("PUT", r"/api/setup/resume", Handler.api_setup_resume),
    ("GET", r"/jobapps-extension\.zip", Handler.api_extension),
    ("GET", r"/mac-setup\.sh", Handler.api_mac_setup),
    ("GET", r"/jobapps-extension\.build", Handler.api_extension_build),
    ("POST", r"/api/ext/plan", Handler.api_ext_plan),
    ("POST", r"/api/ext/choose", Handler.api_ext_choose),
    ("POST", r"/api/ext/draft", Handler.api_ext_draft),
    ("POST", r"/api/ext/learn", Handler.api_ext_learn),
    ("POST", r"/api/ext/status", Handler.api_ext_status),
    ("GET", r"/api/ext/file/([\w.]+)", Handler.api_ext_file),
]


def serve(port: int | None = None) -> None:
    cfg = load_config().get("server", {})
    port = port or int(cfg.get("port", 8765))
    ts = tailscale_info()
    Handler.allowed_hosts = {"127.0.0.1", "localhost", "::1", *(h.lower() for h in cfg.get("allowed_hosts") or [])}
    if ts.get("dns"):
        Handler.allowed_hosts |= {ts["dns"].lower(), ts["dns"].split(".")[0].lower(), *ts["ips"]}
    logins = cfg.get("allowed_logins") or ([ts["login"]] if ts.get("login") else [])
    Handler.allowed_logins = {l.lower() for l in logins}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=scheduler, daemon=True).start()
    https_port = cfg.get("tailscale_https_port", 8443)
    print(f"JobApps dashboard on http://127.0.0.1:{port}", flush=True)
    if ts.get("dns"):
        print(f"From your Mac: https://{ts['dns']}:{https_port}  (needs: tailscale serve --bg --https={https_port} "
              f"http://127.0.0.1:{port})", flush=True)
        print(f"Allowed tailnet logins: {', '.join(sorted(Handler.allowed_logins)) or 'any'}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
