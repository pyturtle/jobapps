import sqlite3
from datetime import datetime, time

from jobapps import db
from jobapps.server import schedule_decision

AT = time(7, 30)


def test_schedule_waits_for_daily_time():
    due, nxt = schedule_decision(datetime(2026, 10, 26, 6, 0), AT, False, None)
    assert not due and nxt == datetime(2026, 10, 26, 7, 30)


def test_schedule_runs_once_then_retries_hourly_until_a_list_exists():
    due, _ = schedule_decision(datetime(2026, 10, 26, 7, 31), AT, False, None)
    assert due
    due, nxt = schedule_decision(datetime(2026, 10, 26, 8, 0), AT, False, datetime(2026, 10, 26, 7, 31))
    assert not due and nxt == datetime(2026, 10, 26, 8, 31)       # failed run: retry an hour later
    due, _ = schedule_decision(datetime(2026, 10, 26, 8, 32), AT, False, datetime(2026, 10, 26, 7, 31))
    assert due


def test_schedule_after_sleep_and_after_success():
    due, _ = schedule_decision(datetime(2026, 10, 26, 13, 0), AT, False, datetime(2026, 10, 25, 7, 31))
    assert due                                                     # PC slept through 07:30: run on wake
    due, nxt = schedule_decision(datetime(2026, 10, 26, 9, 0), AT, True, datetime(2026, 10, 26, 7, 31))
    assert not due and nxt == datetime(2026, 10, 27, 7, 30)        # done for today


def test_backup_keeps_newest(tmp_path):
    src = tmp_path / "jobapps.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    folder = tmp_path / "backups"
    folder.mkdir()
    for d in range(1, 10):
        (folder / f"jobapps-2026-01-0{d}.db").write_text("old")
    target = db.backup(keep=3, path=src)
    kept = sorted(p.name for p in folder.glob("jobapps-*.db"))
    assert len(kept) == 3 and target.name in kept
    assert sqlite3.connect(target).execute("SELECT x FROM t").fetchone() == (1,)


def test_earlier_days_stay_visible(tmp_path):
    conn = db.connect(tmp_path / "jobapps.db")
    db.add_to_batch(conn, "2026-10-24", ["a", "b"])
    db.add_to_batch(conn, "2026-10-25", ["c", "d", "e"])
    db.set_status(conn, "a", "applied")
    db.set_status(conn, "d", "skipped")
    assert db.days(conn) == [
        {"date": "2026-10-25", "total": 3, "done": 1, "applied": 0},
        {"date": "2026-10-24", "total": 2, "done": 1, "applied": 1},
    ]
    # before the 26th's list is built, its view shows what's still open, oldest first
    rows = db.unfinished(conn, "2026-10-26")
    assert [(r["position"], r["job_id"], r["first_assigned"]) for r in rows] == \
        [(1, "b", "2026-10-24"), (2, "c", "2026-10-25"), (3, "e", "2026-10-25")]
