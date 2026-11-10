"""Screenshot the dashboard's Today page with made-up data.

Nothing here talks to the real server: every request is answered from the dashboard's static files on disk
or from demo_state.py, so no real posting, company or answer can appear in the picture.

  python scripts/readme-screenshot/make_shot.py jobapps/web docs/images/today.png 1226

Needs a browser Playwright can drive: Edge, or `playwright install chromium`.
"""
import json
import mimetypes
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from demo_state import STATE

WEB, OUT = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
HEIGHT = int(sys.argv[3]) if len(sys.argv) > 3 else 1000
HOST = "jobapps.demo"


def handle(route):
    url = urlparse(route.request.url)
    if url.hostname in ("fonts.googleapis.com", "fonts.gstatic.com"):
        return route.continue_()
    if url.hostname != HOST:
        return route.abort()
    if url.path == "/api/state":
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(STATE))
    if url.path == "/api/run":
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(STATE["run"]))
    if url.path.startswith("/api/"):
        return route.fulfill(status=200, content_type="application/json", body="{}")
    name = "index.html" if url.path in ("", "/") else url.path.lstrip("/")
    f = (WEB / name).resolve()
    if WEB not in f.parents or not f.is_file():
        return route.fulfill(status=404, body="not found")
    route.fulfill(status=200, content_type=mimetypes.guess_type(f.name)[0] or "application/octet-stream", body=f.read_bytes())


with sync_playwright() as pw:
    try:
        browser = pw.chromium.launch()
    except Exception:
        browser = pw.chromium.launch(channel="msedge")
    page = browser.new_page(viewport={"width": 1180, "height": HEIGHT}, device_scale_factor=2, color_scheme="light")
    page.route("**/*", handle)
    page.goto(f"http://{HOST}/")
    page.wait_for_selector("article.job.open textarea")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(400)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT), clip={"x": 0, "y": 0, "width": 1180, "height": HEIGHT})
    print("saved", OUT, OUT.stat().st_size, "bytes")
    browser.close()
