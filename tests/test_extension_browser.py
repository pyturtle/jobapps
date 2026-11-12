"""The extension in a real Chromium: the zip the server builds, loaded unpacked, on made-up job-board pages
(served through Playwright's routing) against a stand-in JobApps server that decides values the simple way.

What this covers: page scripts can't press the panel's buttons, drafting runs in the background while the page
fills, dropdowns whose options load late, and the page watcher stopping once a job is marked. Skipped when
Playwright or its Chromium isn't installed.
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from jobapps import server  # noqa: E402

BOARD = "https://boards.greenhouse.io"
VALUES = {"first name": "Ada", "last name": "Lovelace", "email": "ada@example.com"}


class Fake:
    """The stand-in server's state, reset by each test."""

    def __init__(self):
        self.reset()

    def reset(self, in_batch=True, draft_seconds=2.0):
        self.in_batch = in_batch
        self.draft_seconds = draft_seconds
        self.calls: list[tuple[str, str, dict]] = []
        self.drafted: dict[str, str] = {}
        self.draft_state = "idle"
        self.lock = threading.Lock()

    def paths(self, method=None):
        return [p for m, p, _ in self.calls if method in (None, m)]

    def plan(self, body):
        actions, needs = [], []
        for f in body.get("fields") or []:
            label = (f.get("label") or "").strip().lower()
            base = {"id": f["id"], "label": f.get("label") or "", "required": True, "note": "", "op": None, "rule": "test"}
            if label in VALUES:
                actions.append({**base, "state": "filled", "op": "text", "text": VALUES[label]})
            elif label == "province" and f["type"] == "select":
                opts = f.get("options") or []
                if "Ontario" in opts:
                    value = (f.get("optionValues") or opts)[opts.index("Ontario")]
                    actions.append({**base, "state": "filled", "op": "select", "value": value, "shown": "Ontario"})
                else:
                    actions.append({**base, "state": "missing", "note": "no option matches 'Ontario'"})
            elif f["type"] == "textarea":
                if label in self.drafted:
                    actions.append({**base, "state": "draft", "op": "text", "text": self.drafted[label], "rule": "draft"})
                else:
                    actions.append({**base, "state": "missing", "rule": "draft"})
                    needs.append(f["id"])
            else:
                actions.append({**base, "state": "manual", "required": False})
        return {"job": {"id": "job1", "company": "Acme", "title": "Software Intern", "known": True,
                        "in_batch": self.in_batch, "seconds": 0},
                "actions": actions, "sections": [], "needs_draft": needs, "can_draft": True}

    def draft(self, body):
        labels = [(f.get("label") or "").strip().lower() for f in body.get("fields") or []]

        def finish():
            with self.lock:
                for label in labels:
                    self.drafted[label] = f"A drafted answer to {label}"
                self.draft_state = "done"

        with self.lock:
            self.draft_state = "running"
        threading.Timer(self.draft_seconds, finish).start()
        return {"state": "running", "job_id": "job1"}

    def answer(self, method, path, body):
        if path == "/api/ext/plan":
            return 200, self.plan(body)
        if path == "/api/ext/draft":
            return 202, self.draft(body)
        if path.startswith("/api/ext/draft/"):
            return 200, {"state": self.draft_state, "error": ""}
        if path == "/api/ext/choose":
            opts = [o.lower() for o in body.get("options") or []]
            idx = next((opts.index(c.lower()) for c in body.get("candidates") or [] if c.lower() in opts), -1)
            return 200, {"index": idx, "choice": None}
        if path == "/api/ext/activity":
            return 200, {"seconds": 0}
        if path == "/api/ext/learn":
            return 200, {"ok": True, "saved": 0, "where": []}
        if path == "/api/ext/status":
            return 200, {"ok": True}
        return 404, {"error": "no such endpoint"}


FAKE = Fake()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, code, data):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        FAKE.calls.append((method, self.path, body))
        self._reply(*FAKE.answer(method, self.path, body))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT")
        self.end_headers()


PAGES: dict[str, str] = {}


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    root = tmp_path_factory.mktemp("ext")
    zipfile.ZipFile(io.BytesIO(server.build_extension(f"http://127.0.0.1:{httpd.server_address[1]}"))).extractall(root)
    ext = root / "jobapps-extension"
    with sync_api.sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                str(root / "profile"), channel="chromium", headless=True,
                args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        except Exception as exc:  # noqa: BLE001 - no browser on this machine
            pytest.skip(f"Chromium with extensions isn't available: {exc}")
        if not ctx.service_workers:
            ctx.wait_for_event("serviceworker", timeout=15000)
        ctx.route(f"{BOARD}/**", lambda route: route.fulfill(
            status=200, content_type="text/html", body=PAGES.get(route.request.url.split("?")[0], "<p>no page</p>")))
        yield ctx
        ctx.close()
    httpd.shutdown()


def open_page(ctx, path, html, **fake):
    FAKE.reset(**fake)
    url = f"{BOARD}{path}"
    PAGES[url] = html
    page = ctx.new_page()
    page.goto(url)
    return page


def wait_until(test, seconds=12.0, step=0.2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if test():
            return True
        time.sleep(step)
    return bool(test())


def press_panel_button(page, label):
    """A real mouse click on a button inside the panel's closed shadow root (found through the DevTools protocol,
    which sees into closed roots; page scripts can't)."""
    cdp = page.context.new_cdp_session(page)
    doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})["root"]

    def find(node):
        if node.get("nodeName") == "BUTTON" and any(c.get("nodeValue") == label for c in node.get("children") or []):
            return node
        for child in (node.get("children") or []) + (node.get("shadowRoots") or []):
            got = find(child)
            if got:
                return got
        return None

    node = find(doc)
    assert node, f"no {label!r} button in the panel"
    quad = cdp.send("DOM.getBoxModel", {"backendNodeId": node["backendNodeId"]})["model"]["content"]
    page.mouse.click((quad[0] + quad[4]) / 2, (quad[1] + quad[5]) / 2)
    cdp.detach()


FORM = """<!doctype html><html><head><title>Apply: Software Intern</title></head><body><form>
  <label>First name <input name="first"></label>
  <label>Last name <input name="last"></label>
  <label>Email <input name="email"></label>
  {extra}
</form></body></html>"""


def test_page_scripts_cannot_press_the_panels_buttons(browser):
    page = open_page(browser, "/acme/jobs/1/apply", FORM.format(extra=""), in_batch=False)
    assert wait_until(lambda: page.evaluate("!!document.querySelector('[data-jobapps-panel]')")), "no offer to fill"
    assert page.evaluate("document.querySelector('[data-jobapps-panel]').shadowRoot") is None  # closed
    # What the old panel listened for: a page script sends it, nothing happens.
    page.evaluate("""() => { for (const d of ['rescan', 'applied']) window.dispatchEvent(new CustomEvent('jobapps-action', { detail: d })); }""")
    page.wait_for_timeout(3000)
    assert page.input_value("[name=first]") == ""
    assert "/api/ext/status" not in FAKE.paths()
    # Your own click still works.
    press_panel_button(page, "Fill this page")
    assert wait_until(lambda: page.input_value("[name=first]") == "Ada")
    assert wait_until(lambda: page.input_value("[name=email]") == "ada@example.com")
    page.close()


def test_drafting_runs_in_the_background_while_the_page_fills(browser):
    html = FORM.format(extra='<label>Why us? <textarea name="why"></textarea></label>')
    page = open_page(browser, "/acme/jobs/2", html, draft_seconds=4.0)
    assert wait_until(lambda: page.input_value("[name=first]") == "Ada"), "the rest of the page wasn't filled"
    assert page.input_value("[name=why]") == ""  # still drafting: filled when it arrives
    assert FAKE.paths("POST").count("/api/ext/draft") == 1
    assert wait_until(lambda: page.input_value("[name=why]") == "A drafted answer to why us?", seconds=20)
    assert any(p.startswith("/api/ext/draft/job1") for p in FAKE.paths("GET"))  # it asked how drafting went
    assert FAKE.paths("POST").count("/api/ext/draft") == 1  # and didn't ask for the same answer twice
    page.close()


LATE = """<script>
  setTimeout(() => {
    const s = document.querySelector('[name=province]');
    for (const [v, t] of [['BC', 'British Columbia'], ['ON', 'Ontario'], ['QC', 'Quebec']]) s.add(new Option(t, v));
  }, {ms});
</script>"""


@pytest.mark.parametrize("ms", [1200, 4500])
def test_dropdown_options_that_load_late(browser, ms):
    extra = '<label>Province <select name="province"><option value="">Select...</option></select></label>' + LATE.replace("{ms}", str(ms))
    page = open_page(browser, f"/acme/jobs/late{ms}", FORM.format(extra=extra))
    assert wait_until(lambda: page.input_value("[name=province]") == "ON", seconds=15)
    plans = [b for m, p, b in FAKE.calls if p == "/api/ext/plan" and b.get("fields")]
    sent = [f for b in plans for f in b["fields"] if f["label"] == "Province"]
    if ms < 2000:  # waited for them: never planned without its options
        assert sent and all(f["options"] for f in sent)
    else:          # planned again once they arrived
        assert sent[0]["options"] == [] and sent[-1]["options"]
    page.close()


def test_stops_watching_the_page_once_marked_applied(browser):
    page = open_page(browser, "/acme/jobs/3", FORM.format(extra=""))
    assert wait_until(lambda: page.input_value("[name=first]") == "Ada")
    page.wait_for_timeout(500)
    press_panel_button(page, "I submitted it")
    assert wait_until(lambda: "/api/ext/status" in FAKE.paths())
    page.wait_for_timeout(500)
    before = FAKE.paths().count("/api/ext/plan")
    # The page keeps changing (new questions, a ticker): nothing is scanned or planned any more.
    page.evaluate("""() => { let i = 0; const t = setInterval(() => {
        const l = document.createElement('label'); l.textContent = 'Question ' + (++i) + ' ';
        l.append(document.createElement('input')); document.querySelector('form').append(l);
        if (i > 12) clearInterval(t); }, 300); }""")
    page.wait_for_timeout(6000)
    assert FAKE.paths().count("/api/ext/plan") == before
    page.close()


def test_a_page_that_never_stops_changing_still_gets_its_new_questions_filled(browser):
    html = """<!doctype html><html><body><form><label>First name <input name="first"></label></form><p id="tick"></p></body></html>"""
    page = open_page(browser, "/acme/jobs/4", html)
    assert wait_until(lambda: page.input_value("[name=first]") == "Ada")
    # A clock on the page changes it every 100 ms, so it is never quiet; then a new question appears.
    page.evaluate("""() => { const p = document.getElementById('tick'); let i = 0;
        setInterval(() => { p.replaceChildren(document.createTextNode(String(++i))); }, 100);
        setTimeout(() => { const l = document.createElement('label'); l.textContent = 'Last name ';
          const x = document.createElement('input'); x.name = 'last'; l.append(x); document.querySelector('form').append(l); }, 800); }""")
    assert wait_until(lambda: page.input_value("[name=last]") == "Lovelace", seconds=12)
    page.close()


def test_viewport_follower_stops_when_the_panel_closes(browser):
    """filler.js on its own: an embedded form's panel follows the visible part of the page, and stops doing so
    (its markers and its 2-second timer) once closed."""
    page = browser.new_page()
    page.set_content("<p>form</p>")
    page.add_script_tag(path=str(server.FILLER_JS))
    page.evaluate("window.__jobapps.panel({ title: 'Acme', filled: 1, drafts: 0, todo: [], inFrame: true })")
    assert page.evaluate("!!document.querySelector('[data-jobapps-rail]')")
    assert page.evaluate("document.querySelector('[data-jobapps-panel]').shadowRoot") is None
    page.evaluate("window.__jobapps.closePanel()")
    assert page.evaluate("document.querySelector('[data-jobapps-rail]')") is None
    assert page.evaluate("document.querySelector('[data-jobapps-panel]')") is None
    page.wait_for_timeout(2500)  # the rail's rebuild timer would have put it back
    assert page.evaluate("document.querySelector('[data-jobapps-rail]')") is None
    page.close()


def test_built_extension_declares_opt_in_sites():
    z = zipfile.ZipFile(io.BytesIO(server.build_extension("https://pc.tailnet.ts.net:8443")))
    manifest = json.loads(z.read("jobapps-extension/manifest.json"))
    assert manifest["optional_host_permissions"] == ["https://*/*"]
    assert re.match(r"\d+\.\d+\.\d+ build [0-9a-f]{8}$", manifest["version_name"])
