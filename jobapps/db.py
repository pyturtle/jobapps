"""SQLite state: listings, the daily batches, application status, prefetched forms, drafted answers."""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from jobapps.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    source TEXT, company TEXT, title TEXT, url TEXT, ats TEXT,
    locations TEXT, terms TEXT, category TEXT, sponsorship TEXT, degrees TEXT,
    date_posted INTEGER, active INTEGER, first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS applications (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,            -- pending | applied | skipped | closed
    first_assigned TEXT,
    updated_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS batches (
    date TEXT, position INTEGER, job_id TEXT,
    PRIMARY KEY (date, job_id)
);
CREATE TABLE IF NOT EXISTS forms (
    job_id TEXT PRIMARY KEY,
    fetched_at TEXT, ats TEXT, description TEXT, fields TEXT, apply_url TEXT
);
CREATE TABLE IF NOT EXISTS answers (
    job_id TEXT, qkey TEXT,
    question TEXT, answer TEXT, final_answer TEXT,
    prompt_file TEXT, backend TEXT, model TEXT, created_at TEXT,
    PRIMARY KEY (job_id, qkey)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS screens (          -- cached LLM fit screening, one per job + inputs
    job_id TEXT, input_hash TEXT, model TEXT, result TEXT, created_at TEXT,
    PRIMARY KEY (job_id, input_hash)
);
CREATE TABLE IF NOT EXISTS picks (            -- why each job was chosen, frozen at pick time
    date TEXT, job_id TEXT, score INTEGER, detail TEXT,
    PRIMARY KEY (date, job_id)
);
CREATE TABLE IF NOT EXISTS events (           -- permanent record: picked, carried, added, released, ruled_out,
    id INTEGER PRIMARY KEY AUTOINCREMENT,      -- opened, applied, skipped, closed, reopened. Never deleted.
    job_id TEXT, kind TEXT, at TEXT, day TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
CREATE TABLE IF NOT EXISTS unanswered (       -- questions JobApps couldn't fill, for you to answer once
    qkey TEXT PRIMARY KEY, label TEXT, type TEXT, options TEXT, rule TEXT, company TEXT, job_id TEXT,
    required INTEGER, seen INTEGER, first_seen TEXT, last_seen TEXT, dismissed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS activity (         -- the timer: each 30-second slice you were working on an application
    job_id TEXT, slot INTEGER,
    PRIMARY KEY (job_id, slot)
);
"""

SLOT = 30  # seconds per activity slice

MIGRATIONS = [("forms", "apply_url", "TEXT"), ("answers", "checks", "TEXT")]

LISTING_JSON_COLS = ("locations", "terms", "degrees")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def backup(keep: int = 7, path: Path = DB_PATH, files: tuple[Path, ...] = ()) -> Path | None:
    """Consistent copy of the database to data/backups/jobapps-YYYY-MM-DD.db, with a copy of each of `files` (what
    you wrote yourself: profile, notes, remembered answers) beside it; keeps the newest `keep` of each."""
    if not path.exists():
        return None
    folder, day = path.parent / "backups", datetime.now().date().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"jobapps-{day}.db"
    src, dst = sqlite3.connect(path), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    for old in sorted(folder.glob("jobapps-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    for f in files:
        if f.exists():
            shutil.copy2(f, folder / f"{f.stem}-{day}{f.suffix}")
            for old in sorted(folder.glob(f"{f.stem}-*{f.suffix}"))[:-keep]:
                old.unlink(missing_ok=True)
    return target


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # the server reads while the pipeline writes
    conn.executescript(SCHEMA)
    for table, column, kind in MIGRATIONS:
        if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    if conn.execute("SELECT 1 FROM events LIMIT 1").fetchone() is None:
        _backfill_events(conn)
    return conn


def _backfill_events(conn) -> None:
    """Start the event record from what the tables already know (databases from before it existed)."""
    for r in conn.execute("SELECT date, position, job_id FROM batches ORDER BY date, position").fetchall():
        log_event(conn, r["job_id"], "picked", r["date"], {"position": r["position"]}, at=f"{r['date']}T00:00:00")
    for r in conn.execute("SELECT job_id, status, updated_at FROM applications WHERE status != 'pending'").fetchall():
        log_event(conn, r["job_id"], r["status"], (r["updated_at"] or "")[:10], at=r["updated_at"])
    for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'excluded:%'").fetchall():
        day = r["key"].split(":", 1)[1]
        for e in json.loads(r["value"] or "[]"):
            log_event(conn, e.get("id"), "ruled_out", day, {"reason": e.get("reason"), "quote": e.get("quote")},
                      at=f"{day}T00:00:00")
    conn.commit()


def log_event(conn, job_id: str, kind: str, day: str | None = None, detail: dict | None = None,
              at: str | None = None) -> None:
    stamp = at or now()
    conn.execute("INSERT INTO events(job_id, kind, at, day, detail) VALUES(?, ?, ?, ?, ?)",
                 (job_id, kind, stamp, day or stamp[:10], json.dumps(detail) if detail else None))


def events_for(conn, job_id: str) -> list[dict]:
    rows = conn.execute("SELECT kind, at, day, detail FROM events WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]


# --- the timer ----------------------------------------------------------------

def add_activity(conn, job_id: str, at: float | None = None) -> int:
    """Count the current 30-second slice as time spent on this application; returns the total seconds."""
    import time
    conn.execute("INSERT OR IGNORE INTO activity(job_id, slot) VALUES(?, ?)", (job_id, int((at or time.time()) // SLOT)))
    conn.commit()
    return active_seconds(conn, [job_id]).get(job_id, 0)


def active_seconds(conn, job_ids: list[str] | None = None) -> dict[str, int]:
    if job_ids is not None and not job_ids:
        return {}
    where = f"WHERE job_id IN ({','.join('?' * len(job_ids))})" if job_ids else ""
    rows = conn.execute(f"SELECT job_id, COUNT(*) AS n FROM activity {where} GROUP BY job_id", job_ids or []).fetchall()
    return {r["job_id"]: r["n"] * SLOT for r in rows}


def _listing(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for col in LISTING_JSON_COLS:
        d[col] = json.loads(d[col] or "[]")
    d["active"] = bool(d["active"])
    return d


# --- meta -------------------------------------------------------------------

def get_meta(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
    conn.commit()


# --- listings ---------------------------------------------------------------

def upsert_listings(conn, listings: list[dict], deactivate_missing: bool) -> tuple[int, int]:
    """Insert/refresh listings. Returns (new, total). A listing that is still up keeps the earliest date it was
    given: a board that says "Posted 30+ Days Ago" on every read would otherwise never get older. One that was
    taken down and is back takes the new date."""
    stamp = now()
    existing = {r["id"] for r in conn.execute("SELECT id FROM listings")}
    new = 0
    for l in listings:
        if l["id"] not in existing:
            new += 1
        conn.execute(
            """INSERT INTO listings(id, source, company, title, url, ats, locations, terms, category,
                                    sponsorship, degrees, date_posted, active, first_seen, last_seen)
               VALUES(:id, :source, :company, :title, :url, :ats, :locations, :terms, :category,
                      :sponsorship, :degrees, :date_posted, :active, :stamp, :stamp)
               ON CONFLICT(id) DO UPDATE SET
                   company = excluded.company, title = excluded.title, url = excluded.url, ats = excluded.ats,
                   locations = excluded.locations, terms = excluded.terms, category = excluded.category,
                   sponsorship = excluded.sponsorship, degrees = excluded.degrees,
                   date_posted = CASE WHEN listings.active AND listings.date_posted > 0 AND
                                           (excluded.date_posted = 0 OR excluded.date_posted > listings.date_posted)
                                      THEN listings.date_posted ELSE excluded.date_posted END,
                   active = excluded.active, last_seen = excluded.last_seen""",
            {**l, **{c: json.dumps(l[c]) for c in LISTING_JSON_COLS}, "active": int(l["active"]), "stamp": stamp},
        )
    if deactivate_missing:  # only listings that came from feeds; searched-for ones are re-checked at pick time
        seen = [l["id"] for l in listings]
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen_ids(id TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM seen_ids")
        conn.executemany("INSERT OR IGNORE INTO seen_ids(id) VALUES(?)", [(i,) for i in seen])
        conn.execute("UPDATE listings SET active = 0 WHERE source != 'claude-search' "
                     "AND id NOT IN (SELECT id FROM seen_ids)")
    conn.commit()
    return new, len(listings)


def active_listings(conn) -> list[dict]:
    return [_listing(r) for r in conn.execute("SELECT * FROM listings WHERE active = 1")]


def get_listing(conn, job_id: str) -> dict | None:
    return _listing(conn.execute("SELECT * FROM listings WHERE id = ?", (job_id,)).fetchone())


def find_listing(conn, prefix: str) -> dict | None:
    rows = conn.execute("SELECT * FROM listings WHERE id LIKE ? ORDER BY id LIMIT 2", (prefix + "%",)).fetchall()
    return _listing(rows[0]) if len(rows) == 1 else None


def set_locations(conn, job_id: str, locations: list[str]) -> None:
    conn.execute("UPDATE listings SET locations = ? WHERE id = ?", (json.dumps(locations), job_id))
    conn.commit()


def deactivate(conn, job_id: str) -> None:
    conn.execute("UPDATE listings SET active = 0 WHERE id = ?", (job_id,))
    conn.commit()


# --- applications & batches -------------------------------------------------

def tracked_ids(conn) -> set[str]:
    return {r["job_id"] for r in conn.execute("SELECT job_id FROM applications")}


def get_status(conn, job_id: str) -> str | None:
    row = conn.execute("SELECT status FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    return row["status"] if row else None


def set_status(conn, job_id: str, status: str, note: str | None = None, url: str | None = None) -> None:
    """url: the page you were on when you said you'd submitted (kept in the record with the event)."""
    if get_status(conn, job_id) == status:
        last = conn.execute("SELECT detail FROM events WHERE job_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                            (job_id, status)).fetchone()
        if not url or (last and json.loads(last["detail"] or "{}").get("url")):
            return  # nothing changed: each frame of a page reports the same submission, and that is one event
    conn.execute(
        """INSERT INTO applications(job_id, status, first_assigned, updated_at, note) VALUES(?, ?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at,
               note = COALESCE(excluded.note, applications.note)""",
        (job_id, status, now()[:10], now(), note),  # first_assigned is a day, as for picked jobs
    )
    detail = {k: v for k, v in (("note", note), ("url", url)) if v}
    log_event(conn, job_id, "reopened" if status == "pending" else status, detail=detail or None)
    conn.commit()


def batch(conn, date: str) -> list[dict]:
    rows = conn.execute(
        """SELECT b.position, b.job_id, COALESCE(a.status, 'pending') AS status, a.first_assigned, a.note
           FROM batches b LEFT JOIN applications a ON a.job_id = b.job_id
           WHERE b.date = ? ORDER BY b.position""",
        (date,),
    ).fetchall()
    return [dict(r) for r in rows]


def pending_before(conn, date: str) -> list[str]:
    """Pending jobs assigned on earlier days, oldest first (stable order)."""
    rows = conn.execute(
        """SELECT a.job_id FROM applications a
           JOIN (SELECT job_id, MIN(date) AS d, MIN(position) AS p FROM batches GROUP BY job_id) b
             ON b.job_id = a.job_id
           WHERE a.status = 'pending' AND b.d < ?
           ORDER BY b.d, b.p, a.job_id""",
        (date,),
    ).fetchall()
    return [r["job_id"] for r in rows]


def unfinished(conn, date: str) -> list[dict]:
    """pending_before as batch()-shaped rows, so they can be shown before today's list is built."""
    rows = []
    for i, job_id in enumerate(pending_before(conn, date), start=1):
        a = conn.execute("SELECT status, first_assigned, note FROM applications WHERE job_id = ?", (job_id,)).fetchone()
        rows.append({"position": i, "job_id": job_id, **dict(a)})
    return rows


def days(conn, limit: int = 60) -> list[dict]:
    """Every day that had a list, newest first, with how much of it is done (status is per job, not per day)."""
    rows = conn.execute(
        """SELECT b.date, COUNT(*) AS total,
                  SUM(COALESCE(a.status, 'pending') != 'pending') AS done,
                  SUM(COALESCE(a.status, 'pending') = 'applied') AS applied
           FROM batches b LEFT JOIN applications a ON a.job_id = b.job_id
           GROUP BY b.date ORDER BY b.date DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_to_batch(conn, date: str, job_ids: list[str], how: str = "picked") -> None:
    """how: "picked" by the daily run (an unfinished job from an earlier day is logged as "carried"),
    or "added" by you from the postings list."""
    start = conn.execute("SELECT COALESCE(MAX(position), 0) FROM batches WHERE date = ?", (date,)).fetchone()[0]
    for offset, job_id in enumerate(job_ids, start=1):
        if conn.execute("SELECT 1 FROM batches WHERE date = ? AND job_id = ?", (date, job_id)).fetchone():
            continue
        earlier = conn.execute("SELECT 1 FROM applications WHERE job_id = ?", (job_id,)).fetchone()
        conn.execute("INSERT OR IGNORE INTO batches(date, position, job_id) VALUES(?, ?, ?)",
                     (date, start + offset, job_id))
        log_event(conn, job_id, "carried" if earlier and how == "picked" else how, date, {"position": start + offset})
        conn.execute("INSERT OR IGNORE INTO applications(job_id, status, first_assigned, updated_at) "
                     "VALUES(?, 'pending', ?, ?)", (job_id, date, now()))
    conn.commit()


def reset_new_picks(conn, date: str) -> int:
    """Forget today's untouched new picks (not carry-overs) so the batch can be rebuilt."""
    rows = conn.execute(
        """SELECT b.job_id FROM batches b JOIN applications a ON a.job_id = b.job_id
           WHERE b.date = ? AND a.status = 'pending' AND a.first_assigned = ?""", (date, date)).fetchall()
    for r in rows:
        log_event(conn, r["job_id"], "released", date, {"why": "re-picked"})
        conn.execute("DELETE FROM batches WHERE date = ? AND job_id = ?", (date, r["job_id"]))
        conn.execute("DELETE FROM applications WHERE job_id = ?", (r["job_id"],))
        conn.execute("DELETE FROM picks WHERE date = ? AND job_id = ?", (date, r["job_id"]))
    conn.execute("DELETE FROM meta WHERE key = ?", (f"excluded:{date}",))
    conn.commit()
    return len(rows)


def remove_from_batch(conn, date: str, job_id: str) -> None:
    conn.execute("DELETE FROM batches WHERE date = ? AND job_id = ?", (date, job_id))
    conn.commit()


def counts(conn) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM applications GROUP BY status")}


def history(conn, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        """SELECT a.job_id, a.status, a.updated_at, a.note, l.company, l.title, l.url,
                  (SELECT LENGTH(f.description) > 0 FROM forms f WHERE f.job_id = a.job_id) AS saved_copy,
                  (SELECT json_extract(e.detail, '$.url') FROM events e WHERE e.job_id = a.job_id AND e.kind = a.status
                   ORDER BY e.id DESC LIMIT 1) AS applied_url
           FROM applications a LEFT JOIN listings l ON l.id = a.job_id
           WHERE a.status != 'pending' ORDER BY a.updated_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [{**dict(r), "saved_copy": bool(r["saved_copy"])} for r in rows]


# --- forms & answers --------------------------------------------------------

def save_form(conn, job_id: str, ats: str, description: str, fields: list[dict], apply_url: str = "") -> None:
    conn.execute(
        """INSERT INTO forms(job_id, fetched_at, ats, description, fields, apply_url) VALUES(?, ?, ?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET fetched_at = excluded.fetched_at, ats = excluded.ats,
               description = excluded.description, fields = excluded.fields, apply_url = excluded.apply_url""",
        (job_id, now(), ats, description, json.dumps(fields), apply_url),
    )
    conn.commit()


def get_form(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM forms WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["fields"] = json.loads(d["fields"] or "[]")
    return d


def answers_for(conn, job_id: str) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM answers WHERE job_id = ?", (job_id,)).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        d["checks"] = json.loads(d["checks"]) if d.get("checks") else None
        out[r["qkey"]] = d
    return out


def save_answer(conn, job_id: str, qkey: str, question: str, answer: str | None,
                prompt_file: str, backend: str, model: str, checks: dict | None = None) -> None:
    conn.execute(
        """INSERT INTO answers(job_id, qkey, question, answer, prompt_file, backend, model, created_at, checks)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(job_id, qkey) DO UPDATE SET question = excluded.question,
               answer = COALESCE(excluded.answer, answers.answer), prompt_file = excluded.prompt_file,
               backend = excluded.backend, model = excluded.model, created_at = excluded.created_at,
               checks = COALESCE(excluded.checks, answers.checks)""",
        (job_id, qkey, question, answer, prompt_file, backend, model, now(),
         json.dumps(checks) if checks is not None else None),
    )
    conn.commit()


def save_final_answer(conn, job_id: str, qkey: str, final: str | None, question: str = "") -> None:
    """Your own text for an answer, kept beside the draft. "" is a box you emptied, None puts the draft back.
    A question nothing was drafted for gets its row here, so what you wrote for it is stored all the same."""
    if final is None:
        conn.execute("UPDATE answers SET final_answer = NULL WHERE job_id = ? AND qkey = ?", (job_id, qkey))
    else:
        conn.execute(
            """INSERT INTO answers(job_id, qkey, question, final_answer, created_at) VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(job_id, qkey) DO UPDATE SET final_answer = excluded.final_answer""",
            (job_id, qkey, question, final, now()))
    conn.commit()


def answer_text(a: dict | None) -> str:
    """An answer as it stands: your text if you edited it (an emptied box stays empty), else the draft."""
    a = a or {}
    final = a.get("final_answer")
    return (a.get("answer") or "") if final is None else final


def current_answers(answers: dict[str, dict]) -> dict[str, str]:
    """qkey -> text for the written answers that have any, as answer_text reads them."""
    return {k: text for k, a in answers.items() if (text := answer_text(a))}


def get_screen(conn, job_id: str, input_hash: str) -> dict | None:
    row = conn.execute("SELECT result FROM screens WHERE job_id = ? AND input_hash = ?", (job_id, input_hash)).fetchone()
    return json.loads(row["result"]) if row else None


def save_screen(conn, job_id: str, input_hash: str, model: str, result: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO screens(job_id, input_hash, model, result, created_at) VALUES(?, ?, ?, ?, ?)",
                 (job_id, input_hash, model, json.dumps(result), now()))
    conn.commit()


def latest_screen(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT result FROM screens WHERE job_id = ? ORDER BY created_at DESC LIMIT 1",
                       (job_id,)).fetchone()
    return json.loads(row["result"]) if row else None


def save_pick(conn, date: str, job_id: str, score: int, detail: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO picks(date, job_id, score, detail) VALUES(?, ?, ?, ?)",
                 (date, job_id, score, json.dumps(detail)))
    conn.commit()


def get_pick(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT score, detail FROM picks WHERE job_id = ? ORDER BY date DESC LIMIT 1",
                       (job_id,)).fetchone()
    return {"score": row["score"], **json.loads(row["detail"])} if row else None


def clear_answers(conn, job_id: str) -> None:
    conn.execute("DELETE FROM answers WHERE job_id = ?", (job_id,))
    conn.commit()


# --- questions you haven't answered yet ---------------------------------------

def note_unanswered(conn, items: list[dict], company: str, job_id: str) -> None:
    """items: {qkey, label, type, options, rule, required}. Seen again: counted, options/label refreshed."""
    stamp = now()
    for q in items:
        conn.execute(
            """INSERT INTO unanswered(qkey, label, type, options, rule, company, job_id, required, seen, first_seen, last_seen)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(qkey) DO UPDATE SET label = excluded.label, type = excluded.type,
                   options = CASE WHEN excluded.options != '[]' THEN excluded.options ELSE unanswered.options END,
                   rule = excluded.rule, company = excluded.company, job_id = excluded.job_id,
                   required = MAX(unanswered.required, excluded.required), seen = unanswered.seen + 1,
                   last_seen = excluded.last_seen""",
            (q["qkey"], q["label"], q["type"], json.dumps(q.get("options") or []), q.get("rule") or "", company, job_id,
             int(bool(q.get("required"))), stamp, stamp))
    conn.commit()


def unanswered(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM unanswered WHERE dismissed = 0 ORDER BY required DESC, seen DESC, last_seen DESC")
    return [{**dict(r), "options": json.loads(r["options"] or "[]")} for r in rows]


def answered(conn, qkeys: list[str], dismiss: bool = False) -> None:
    """Answered (removed) or 'not needed' (kept, hidden so it doesn't come back)."""
    for k in qkeys:
        if dismiss:
            conn.execute("UPDATE unanswered SET dismissed = 1 WHERE qkey = ?", (k,))
        else:
            conn.execute("DELETE FROM unanswered WHERE qkey = ?", (k,))
    conn.commit()
