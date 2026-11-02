"""Open applications in a real browser, fill them from your profile, and wait for you to submit.

Safety rules: never clicks submit, never overwrites a field that already has a value, never types
passwords, and leaves legal/consent questions to you.
"""
from __future__ import annotations

import queue
import re
import sys
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from jobapps import db, drafts
from jobapps.config import BROWSER_PROFILE_DIR, Profile, load_learned, save_learned
from jobapps.plan import has_value, text_for, to_field
from jobapps.rules import Resolution, Resolver, candidates, choose_option, display, norm, qkey

JS = Path(__file__).with_name("filler.js").read_text(encoding="utf-8")
JS_FN = "() => {\n" + JS + "\n}"

SUBMITTED_TEXT = re.compile(
    r"thank(s| you) for (applying|your application|submitting)|application (was |has been )?(successfully )?"
    r"(submitted|received|sent|complete)|we('ve| have) received your application|your application (is|was|has been) "
    r"(in|submitted|received)", re.I)
SUBMITTED_URL = re.compile(r"confirm|thank|submitted|success", re.I)
# Dropdown options across widget libraries (ARIA listboxes, react-select, Greenhouse's select).
OPTION_SEL = ", ".join(f"{s}:visible" for s in ('[role="option"]', '[id*="-option-"]', ".select__option"))
_PLACEHOLDER_OPTION = re.compile(r"^(no options|no results|loading|searching|type to search)", re.I)


class FillError(Exception):
    pass


class Keys:
    """Reads terminal lines on a background thread so the browser keeps running while we wait."""

    def __init__(self):
        self.q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in sys.stdin:
            self.q.put(line.strip().lower())
        self.q.put("q")  # stdin closed: no one is at the terminal, so stop cleanly

    def poll(self) -> str | None:
        try:
            return self.q.get_nowait()
        except queue.Empty:
            return None

    def ask(self, prompt: str) -> str:
        print(prompt, end=" ", flush=True)
        return self.q.get()


def _same(a, b) -> bool:
    return {norm(c) for c in candidates(a)} == {norm(c) for c in candidates(b)}


class FillSession:
    def __init__(self, page, conn, cfg: dict, profile: Profile, job: dict, form: dict | None, today: date, log=print):
        self.page, self.conn, self.cfg, self.profile, self.job, self.today, self.log = \
            page, conn, cfg, profile, job, today, log
        self.description = (form or {}).get("description") or ""
        self.resolver = Resolver(profile, job, load_learned(), self._drafts())
        self.handled: dict[tuple, tuple[dict, Resolution, str]] = {}
        self.typed: dict[tuple, object] = {}   # exactly what we put in each field
        self.user_values: dict[tuple, object] = {}
        self.start_url = ""
        self.drafted_live = False

    def _drafts(self) -> dict[str, str]:
        return {k: v["final_answer"] or v["answer"] for k, v in db.answers_for(self.conn, self.job["id"]).items()
                if v["final_answer"] or v["answer"]}

    # --- page plumbing ------------------------------------------------------

    def on_record(self, source, payload):
        self.user_values[(id(source["frame"]), str(payload.get("id")))] = payload.get("value")

    def _frames_and_fields(self) -> list[tuple]:
        out = []
        for frame in self.page.frames:
            try:
                if not frame.evaluate("() => !!window.__jobapps"):
                    frame.evaluate(JS_FN)
                fields = frame.evaluate("() => window.__jobapps.scan()")
                frame.evaluate("() => window.__jobapps.watch()")
            except PlaywrightError:
                continue
            out += [(frame, f) for f in fields]
        return out

    def wait_for_form(self, timeout: float = 20):
        deadline, last = time.monotonic() + timeout, -1
        while time.monotonic() < deadline:
            n = len(self._frames_and_fields())
            if n and n == last:
                return
            last = n
            self.page.wait_for_timeout(1200)

    def page_text(self) -> str:
        try:
            return self.page.evaluate("() => document.body ? document.body.innerText : ''")[:40000]
        except PlaywrightError:
            return ""

    # --- filling ------------------------------------------------------------

    def fill_pass(self) -> int:
        """Fill every field not handled yet. Returns how many new fields were found."""
        found = [(fr, f) for fr, f in self._frames_and_fields() if (id(fr), f["id"]) not in self.handled]
        if not found:
            return 0
        self._draft_missing(found)
        ordered = sorted(found, key=lambda item: item[1]["type"] == "file")  # uploads last: some ATS re-parse them
        for frame, f in ordered:
            key = (id(frame), f["id"])
            res = self.resolver.resolve(to_field(f))
            state = self._apply(frame, f, res, key)
            self.handled[key] = (f, res, state)
            try:
                frame.evaluate("([id, s, n]) => window.__jobapps.mark(id, s, n)",
                               [f["id"], state, res.note or res.source])
            except PlaywrightError:
                pass
        return len(found)

    def _draft_missing(self, found):
        if self.drafted_live:
            return
        need = []
        for _, f in found:
            fld = to_field(f)
            res = self.resolver.resolve(fld)
            if res.kind == "draft" and not res.value and not has_value(f.get("value")):
                need.append((fld, f.get("maxlength")))
        if not need or drafts.pick_backend(self.cfg["drafts"]) == "none":
            return
        self.drafted_live = True
        self.description = self.description or self.page_text()
        self.log(f"  drafting {len(need)} answer(s) for questions found on the page...")
        drafts.draft_job(self.conn, self.cfg["drafts"], self.profile, self.job, self.description,
                         [f for f, _ in need], self.today,
                         maxlengths={qkey(f.label): m for f, m in need if m}, log=self.log)
        self.resolver.drafts = self._drafts()

    def _apply(self, frame, f: dict, res: Resolution, key: tuple) -> str:
        if res.kind == "skip":
            return "skip"
        if has_value(f.get("value")):
            self.typed[key] = f.get("value")
            return "filled"
        if res.kind == "manual" or (res.kind == "draft" and not res.value):
            return "missing" if f.get("required") else "manual"
        try:
            self.typed[key] = self._set(frame, f, res)
        except (PlaywrightError, FillError) as exc:
            res.note = f"could not fill: {str(exc).splitlines()[0][:120]}"
            return "missing" if f.get("required") else "manual"
        return "draft" if res.kind == "draft" else "filled"

    def _set(self, frame, f: dict, res: Resolution):
        """Put the value into the field; return exactly what was entered."""
        sel = f'[data-jobapps-id="{f["id"]}"]'
        loc = frame.locator(sel).first
        kind, value = f["type"], res.value
        if res.kind == "file":
            loc.set_input_files(value)
            return Path(value).name
        if kind in ("text", "textarea", "date"):
            text = text_for(f, value)
            loc.fill(text, timeout=5000)
            return text
        if kind == "select":
            opts = f.get("options") or []
            chosen = value if value in opts else choose_option(value, opts)
            if chosen is None:
                raise FillError(f"no option matches {display(value)!r}")
            loc.select_option(value=f["optionValues"][opts.index(chosen)], timeout=5000)
            return chosen
        if kind in ("radio", "checkbox", "buttons"):
            opts, picked = f.get("options") or [], []
            for want in (value if isinstance(value, list) else [value]):
                opt = want if want in opts else choose_option(want, opts)
                if opt is None:
                    raise FillError(f"no option matches {display(want)!r}")
                target = frame.locator(f'{sel}[data-jobapps-opt="{opts.index(opt)}"]').first
                if kind == "buttons":
                    target.click(timeout=5000)
                else:
                    target.check(force=True, timeout=5000)
                picked.append(opt)
            return picked
        if kind == "boolean":
            if value is True:
                loc.check(force=True, timeout=5000)
            return value is True
        if kind == "combobox":
            return self._combobox(frame, loc, res)
        raise FillError(f"unsupported field type {kind}")

    @staticmethod
    def _options(frame):
        loc = frame.locator(OPTION_SEL)
        try:
            texts = [t.strip() for t in loc.all_inner_texts()]
        except PlaywrightError:
            texts = []
        return loc, [t for t in texts if t]

    def _combobox(self, frame, el, res: Resolution):
        """Open the menu and pick from it; type a search only if the right option isn't listed."""
        el.click(timeout=5000)
        frame.wait_for_timeout(500)
        loc, texts = self._options(frame)
        chosen = choose_option(res.value, texts) if texts else None
        for query in ([] if chosen else candidates(res.value)[:3]):
            el.fill("")
            el.press_sequentially(query[:60], delay=25)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:  # async menus (schools, cities) take a moment
                frame.wait_for_timeout(300)
                loc, texts = self._options(frame)
                if texts and not all(_PLACEHOLDER_OPTION.match(t) for t in texts):
                    break
            texts_ok = [t for t in texts if not _PLACEHOLDER_OPTION.match(t)]
            chosen = choose_option(res.value, texts_ok) or (texts_ok[0] if res.source == "location" and texts_ok else None)
            if chosen:
                break
        if chosen:
            loc.nth(texts.index(chosen)).click(timeout=5000)
            return chosen
        el.fill("")
        el.press("Escape")
        raise FillError("no dropdown option matched")

    def render(self):
        items = list(self.handled.values())
        data = {
            "title": f"{self.job['company']}",
            "filled": sum(1 for _, _, s in items if s == "filled"),
            "drafts": sum(1 for _, _, s in items if s == "draft"),
            "todo": [{"id": f["id"], "label": f.get("label"), "required": s == "missing", "note": r.note}
                     for f, r, s in items if s in ("missing", "manual")],
        }
        data["todo"].sort(key=lambda t: not t["required"])
        try:
            self.page.main_frame.evaluate("(d) => window.__jobapps && window.__jobapps.panel(d)", data)
        except PlaywrightError:
            pass
        return data

    # --- submission & learning ---------------------------------------------

    def submitted(self) -> bool:
        try:
            url = self.page.url
            if url != self.start_url and SUBMITTED_URL.search(urlparse(url).path):
                return True
            for frame in self.page.frames:
                text = frame.evaluate("() => document.body ? document.body.innerText.slice(0, 20000) : ''")
                if SUBMITTED_TEXT.search(text):
                    remaining = frame.evaluate("() => window.__jobapps ? window.__jobapps.count() : 0")
                    if remaining <= 2:
                        return True
        except PlaywrightError:
            return False
        return False

    def collect_learning(self) -> list[dict]:
        """Values you typed into fields we could not fill, plus your edits to drafted answers."""
        learned = []
        for key, value in self.user_values.items():
            entry = self.handled.get(key)
            if not entry or not has_value(value):
                continue
            f, res, _ = entry
            if res.kind == "draft":
                if str(value).strip() and str(value).strip() != str(res.value or "").strip():
                    db.save_final_answer(self.conn, self.job["id"], qkey(f["label"]), str(value).strip())
                continue
            if f["type"] in ("file", "textarea") or not f.get("label"):
                continue
            if key in self.typed and _same(value, self.typed[key]):
                continue  # still what we entered: nothing new to learn
            if len(str(value)) > 200:
                continue
            if isinstance(value, list) and len(value) == 1:
                value = value[0]
            learned.append({"label": f["label"], "answer": value, "type": f["type"]})
        return learned


def _remember(keys: Keys, job: dict, entries: list[dict], log=print) -> None:
    if not entries:
        return
    log("\n  You answered these yourself:")
    for e in entries:
        log(f"    - {e['label'][:80]}  ->  {display(e['answer'])}")
    if keys.ask("  Remember them for future forms? [Y/n]").startswith("n"):
        return
    current = load_learned()
    by_key = {qkey(e["label"]): e for e in current}
    for e in entries:
        by_key[qkey(e["label"])] = {**e, "learned_from": job["company"], "date": date.today().isoformat()}
    save_learned(list(by_key.values()))
    log(f"  Saved {len(entries)} answer(s) to learned_answers.yaml")


def run(conn, cfg: dict, profile: Profile, jobs: list[dict], today: date, log=print,
        profile_dir: Path = BROWSER_PROFILE_DIR, headless: bool = False) -> list[tuple[str, str]]:
    """Walk through jobs one at a time. Returns [(job_id, outcome)]; outcome in applied|skipped|later|quit."""
    keys = Keys()
    results = []
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        launch = dict(user_data_dir=str(profile_dir), headless=headless, no_viewport=True,
                      ignore_default_args=["--enable-automation"], args=["--start-maximized"])
        channel = cfg["browser"].get("channel")
        try:
            context = pw.chromium.launch_persistent_context(channel=channel, **launch) if channel else \
                pw.chromium.launch_persistent_context(**launch)
        except PlaywrightError as exc:
            log(f"Could not start {channel or 'chromium'} ({str(exc).splitlines()[0]}); trying bundled Chromium.")
            context = pw.chromium.launch_persistent_context(**launch)
        context.add_init_script(JS)
        for i, job in enumerate(jobs, 1):
            outcome = _run_one(context, keys, conn, cfg, profile, job, today, f"[{i}/{len(jobs)}]", log)
            results.append((job["id"], outcome))
            if outcome == "quit":
                break
        try:
            context.close()
        except PlaywrightError:
            pass
    return results


def _run_one(context, keys: Keys, conn, cfg, profile, job, today, tag, log) -> str:
    form = db.get_form(conn, job["id"])
    url = (form or {}).get("apply_url") or job["url"]
    log(f"\n{tag} {job['company']} - {job['title']}")
    log(f"      {url}")
    page = context.new_page()
    session = FillSession(page, conn, cfg, profile, job, form, today, log)
    page.expose_binding("__jobappsRecord", session.on_record)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except PlaywrightError as exc:
        log(f"  ! could not open the page: {str(exc).splitlines()[0]}")
    session.start_url = page.url
    session.wait_for_form()
    session.fill_pass()
    summary = session.render()
    log(f"  filled {summary['filled']}, drafted {summary['drafts']}, needs you {len(summary['todo'])}"
        + (f" ({sum(1 for t in summary['todo'] if t['required'])} required)" if summary['todo'] else ""))
    for t in summary["todo"]:
        log(f"    {'*' if t['required'] else '-'} {(t['label'] or '(unlabelled)')[:80]}" + (f"  [{t['note']}]" if t['note'] else ""))
    log("  Review in the browser and submit there. Then press Enter = applied, s = skip, l = later, r = re-scan, q = quit")

    outcome, last_pass = None, time.monotonic()
    while outcome is None:
        try:
            page.wait_for_timeout(400)
        except PlaywrightError:
            pass
        if page.is_closed():
            answer = keys.ask("  Tab closed. Did you submit it? [y = applied / n = keep for later / s = skip]")
            outcome = {"y": "applied", "s": "skipped"}.get(answer[:1], "later")
            break
        cmd = keys.poll()
        if cmd is not None:
            if cmd == "r":
                session.fill_pass()
                session.render()
                log("  re-scanned")
                continue
            outcome = {"": "applied", "a": "applied", "y": "applied", "s": "skipped", "l": "later", "q": "quit"}.get(cmd)
            if outcome is None:
                log("  Enter = applied, s = skip, l = later, r = re-scan, q = quit")
            continue
        if time.monotonic() - last_pass > 2.5:
            last_pass = time.monotonic()
            if session.submitted():
                log("  Submission detected.")
                outcome = "applied"
                break
            if session.fill_pass():
                session.render()

    _remember(keys, job, session.collect_learning(), log)
    if not page.is_closed() and outcome != "quit":
        try:
            page.close()
        except PlaywrightError:
            pass
    return outcome
