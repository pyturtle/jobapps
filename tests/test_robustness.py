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
    notes = tmp_path / "learned_answers.yaml"
    notes.write_text("a: b")
    for d in range(1, 6):
        (folder / f"learned_answers-2026-01-0{d}.yaml").write_text("old")
    target = db.backup(keep=3, path=src, files=(notes, tmp_path / "not-written-yet.md"))
    kept = sorted(p.name for p in folder.glob("jobapps-*.db"))
    assert len(kept) == 3 and target.name in kept
    assert sqlite3.connect(target).execute("SELECT x FROM t").fetchone() == (1,)
    copies = sorted(folder.glob("learned_answers-*.yaml"))   # what you wrote yourself is copied too
    assert len(copies) == 3 and copies[-1].read_text() == "a: b" and not list(folder.glob("not-written-yet*"))


def test_a_failed_backup_doesnt_cost_the_days_list(monkeypatch):
    from jobapps import server

    def full_disk(**kw):
        raise OSError("No space left on device")

    ran = []
    monkeypatch.setattr(server.db, "backup", full_disk)
    monkeypatch.setattr(server.pipeline, "run_daily", lambda *a, **kw: ran.append(1))
    monkeypatch.setattr(server.RUNNER, "log", lambda msg="": ran.append(msg))
    server.run_daily(backup=True)(None, {}, None)
    assert ran == ["! backup failed: No space left on device", 1]


def test_the_runner_is_free_again_when_the_database_cant_be_opened(monkeypatch):
    from jobapps import server

    def locked():
        raise sqlite3.OperationalError("database is locked")

    runner = server.Runner()
    runner.running = True
    monkeypatch.setattr(server.db, "connect", locked)
    monkeypatch.setattr(runner, "log", lambda msg="": None)
    monkeypatch.setattr(server.traceback, "print_exc", lambda: None)
    runner._wrap(lambda conn, cfg, profile: None)
    assert runner.running is False and "database is locked" in runner.last_error


def test_a_bad_reply_is_one_failed_request(monkeypatch):
    import gzip
    import http.client
    import ssl

    import pytest

    from jobapps import net

    class Reply:
        status, headers = 200, {"Content-Encoding": "gzip"}

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self.body

    def serve(reply):
        def urlopen(req, timeout=None):
            if isinstance(reply, Exception):
                raise reply
            return Reply(reply)
        monkeypatch.setattr(net.urllib.request, "urlopen", urlopen)

    for reply in (http.client.IncompleteRead(b"half"), ssl.SSLError("bad record mac"), b"<html>not gzip</html>",
                  gzip.compress(b"cut short")[:-6]):
        serve(reply)
        with pytest.raises(net.HttpError):
            net.request("https://acme.example/jobs", retries=0)
    serve(gzip.compress(b"fine"))
    assert net.request("https://acme.example/jobs", retries=0) == (200, b"fine")


def test_any_failure_of_a_model_call_is_one_kind_of_error(monkeypatch):
    import pytest

    from jobapps import llm

    def gone(*a, **kw):
        raise FileNotFoundError("claude.exe")

    monkeypatch.setattr(llm, "_cli", gone)
    monkeypatch.setattr(llm, "STATUS", {**llm.STATUS, "failures": 0})
    with pytest.raises(llm.LLMError, match="FileNotFoundError"):
        llm.structured("s", "u", {}, {"backend": "claude-cli", "model": "m"})
    assert llm.STATUS["failures"] == 1 and "claude.exe" in llm.STATUS["last_error"]


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
