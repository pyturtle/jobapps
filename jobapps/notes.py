"""Extra notes for the drafts and the screen, listed under `notes` in config.yaml.

Each entry is a file on this PC, read as it is every time, or a page on the web. A page is fetched once
a day (by the daily run, or now with `ja notes --refresh`) and the copy is kept under data/notes/, so a
day the page can't be reached still has the last one. Nothing here is ever fetched while a request is
being answered: the dashboard and the drafts only read the copies.

The first use is the candidate's own website as one text file (nathan-nguyen.dev/llms.txt), built from
the site's data: every project, hobby and habit the site describes, read with background.md and quoted
the same way. The drafter still invents nothing: these are more facts to quote, not instructions.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from jobapps import net
from jobapps.config import NOTES_DIR, ROOT, atomic_write

MAX_BYTES = 400_000   # a text file about one person; anything bigger is the wrong page
TIMEOUT = 20


@dataclass
class Source:
    ref: str              # as written in config.yaml
    kind: str             # "page" or "file"
    text: str = ""
    status: str = ""      # one line for the Setup page and `ja doctor`
    fetched: str = ""     # when a page's copy was taken (ISO), "" for a file
    error: str = ""       # why the last fetch failed, "" if it didn't


def refs(cfg: dict) -> list[str]:
    return [str(r).strip() for r in (cfg.get("notes") or []) if str(r).strip()]


def is_page(ref: str) -> bool:
    return ref.startswith(("http://", "https://"))


def pages(cfg: dict) -> list[str]:
    return [r for r in refs(cfg) if is_page(r)]


def copy_path(url: str) -> Path:
    """data/notes/www.example.com-llms.txt.md: the copy of one page, named after it."""
    name = re.sub(r"[^\w.-]+", "-", re.sub(r"^https?://", "", url)).strip("-.")
    return NOTES_DIR / f"{name[:120]}.md"


def _read_copy(url: str) -> tuple[dict, str]:
    """A copy's header (one JSON line: url, fetched, tried, error, origin) and the text under it."""
    path = copy_path(url)
    if not path.exists():
        return {}, ""
    head, _, body = path.read_text(encoding="utf-8").partition("\n")
    try:
        meta = json.loads(head)
    except ValueError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), body


def _write_copy(url: str, meta: dict, body: str) -> None:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(copy_path(url), json.dumps(meta, ensure_ascii=False) + "\n" + body)


def fetch(url: str) -> str:
    """The page's text. Raises on anything but a plain text or Markdown page of a sensible size."""
    _, raw = net.request(url, timeout=TIMEOUT, retries=1,
                         headers={"Accept": "text/plain, text/markdown;q=0.9, */*;q=0.1"})
    if len(raw) > MAX_BYTES:
        raise ValueError(f"{len(raw):,} bytes is too big for a notes page")
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        raise ValueError("the page is empty")
    if re.match(r"<(!doctype|html|head|body)\b", text, re.I):
        raise ValueError("the page is HTML, not text")
    return text


def refresh(cfg: dict, *, force: bool = False, today: date | None = None, log=print) -> None:
    """Fetch each page once a day (or now, with force). A fetch that fails leaves the last copy in place and
    records why, so the Setup page and `ja doctor` can say so."""
    day = (today or date.today()).isoformat()
    for url in pages(cfg):
        meta, body = _read_copy(url)
        if not force and meta.get("tried") == day:
            continue
        try:
            body = fetch(url)
            meta = {"url": url, "fetched": datetime.now().isoformat(timespec="seconds"), "tried": day, "error": ""}
            log(f"notes: {url}: fetched, {len(body):,} characters")
        except Exception as exc:  # HttpError, a bad body, a disk error: the last copy stays
            meta = {**meta, "url": url, "tried": day, "error": str(exc)}
            kept = f"keeping the copy from {str(meta.get('fetched', ''))[:10]}" if body else "no copy yet"
            log(f"notes: {url}: {exc}; {kept}")
        _write_copy(url, meta, body)


def store(url: str, text: str, origin: str) -> None:
    """Keep a copy of a page taken some other way, such as a local build of the site before it is deployed.
    The next daily run replaces it with the page itself as soon as that can be fetched."""
    now = datetime.now().isoformat(timespec="seconds")
    _write_copy(url, {"url": url, "fetched": now, "tried": "", "error": "", "origin": origin}, text.strip())


def sources(cfg: dict) -> list[Source]:
    """Every entry with its text, if it has any, and a line on where that text is from. Never fetches."""
    out = []
    for ref in refs(cfg):
        if is_page(ref):
            meta, body = _read_copy(ref)
            s = Source(ref, "page", body.strip(), fetched=str(meta.get("fetched", "")), error=str(meta.get("error", "")))
            if s.text:
                origin = f" ({meta['origin']})" if meta.get("origin") else ""
                s.status = f"copy from {s.fetched[:10]}{origin}, {len(s.text):,} characters"
                if s.error:
                    s.status += f"; the last fetch failed: {s.error}"
            elif s.error:
                s.status = f"no copy yet; the last fetch failed: {s.error}"
            else:
                s.status = "no copy yet; the daily run fetches it, or `ja notes --refresh`"
        else:
            path = Path(ref) if Path(ref).is_absolute() else ROOT / ref
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                s = Source(ref, "file", text, status=f"{len(text):,} characters")
            else:
                s = Source(ref, "file", status="file not found")
        out.append(s)
    return out


def text(cfg: dict) -> str:
    """The sources that have text, each under a heading that names it, for the candidate background."""
    return "\n\n".join(f"## More from the candidate: {s.ref}\n\n{s.text}" for s in sources(cfg) if s.text)
