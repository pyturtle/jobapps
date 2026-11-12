"""The server's side of the extension, on a throwaway database: background drafting, the statuses the extension
may set, records for pages you found yourself, the "answer once" counts, and time estimates."""
import http.client
import json
import threading
import time

import pytest

from jobapps import db, records, server

PAGE = "https://careers.example.com/jobs/42"


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    real = db.connect
    monkeypatch.setattr(db, "connect", lambda p=path: real(p))
    monkeypatch.setattr(server, "DRAFTING", server.Drafting())
    monkeypatch.setattr(server.Handler, "allowed_hosts", {"127.0.0.1"})
    httpd = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def call(method, url, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, url, body=json.dumps(body) if body is not None else None,
                  headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    yield call, path
    httpd.shutdown()


def test_unknown_status_refused_and_records_survive_an_old_one(api):
    call, path = api
    assert call("POST", "/api/ext/status", {"url": PAGE, "title": "Intern", "status": "hired"})[0] == 400
    assert call("POST", "/api/ext/status", {"url": PAGE, "title": "Intern", "status": "skipped"}) == (200, {"ok": True})
    conn = db.connect(path)
    conn.execute("UPDATE applications SET status = 'weird'")  # stored before the check existed
    conn.commit()
    row = records.postings(conn)["rows"][0]
    assert row["status"] == "weird" and row["status_label"] == "weird"
    assert "weird" in records.postings_csv(conn)


def test_drafting_returns_at_once_and_reports_when_done(api, monkeypatch):
    call, path = api
    started, release = threading.Event(), threading.Event()
    seen = []

    def fake_draft(conn, cfg, profile, job, description, fields, today, maxlengths=None, log=print):
        seen.append([f.label for f in fields])
        started.set()
        release.wait(5)
        return {"status": "drafted", "count": len(fields)}

    monkeypatch.setattr(server.drafts, "draft_job", fake_draft)
    monkeypatch.setattr(server, "load_config", lambda: {"drafts": {}})
    monkeypatch.setattr(server, "load_profile", lambda: None)
    body = {"url": PAGE, "title": "Software Intern - Example", "page_text": "We build things.",
            "fields": [{"id": "1", "label": "Why us?", "type": "textarea", "maxlength": 500}]}
    t0 = time.monotonic()
    status, r = call("POST", "/api/ext/draft", body)
    assert status == 202 and r["state"] == "running" and time.monotonic() - t0 < 2
    job_id = r["job_id"]
    assert started.wait(5)
    assert call("GET", f"/api/ext/draft/{job_id}")[1]["state"] == "running"
    # Another page of the same application asks while the first is drafting: drafted right after it.
    second = {**body, "fields": [{"id": "2", "label": "Anything else?", "type": "textarea"}]}
    assert call("POST", "/api/ext/draft", second)[1]["state"] == "queued"
    release.set()
    for _ in range(50):
        if call("GET", f"/api/ext/draft/{job_id}")[1]["state"] == "done":
            break
        time.sleep(0.1)
    assert call("GET", f"/api/ext/draft/{job_id}")[1] == {"state": "done", "error": ""}
    assert seen == [["Why us?"], ["Anything else?"]]
    # The page you found yourself is in your record now, so its drafts show on the dashboard.
    conn = db.connect(path)
    assert db.get_listing(conn, job_id)["company"]
    assert db.get_form(conn, job_id)["description"] == "We build things."
    assert call("GET", "/api/ext/draft/0123456789")[1]["state"] == "idle"


def test_drafting_errors_reach_the_extension(api, monkeypatch):
    call, _ = api
    monkeypatch.setattr(server.drafts, "draft_job", lambda *a, **k: {"status": "error", "error": "Claude is busy"})
    monkeypatch.setattr(server, "load_config", lambda: {"drafts": {}})
    monkeypatch.setattr(server, "load_profile", lambda: None)
    job_id = call("POST", "/api/ext/draft", {"url": PAGE, "fields": [{"id": "1", "label": "Why?", "type": "textarea"}]})[1]["job_id"]
    for _ in range(50):
        r = call("GET", f"/api/ext/draft/{job_id}")[1]
        if r["state"] != "running":
            break
        time.sleep(0.1)
    assert r == {"state": "error", "error": "Claude is busy"}


def test_unanswered_counted_once_per_application(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    q = [{"qkey": "how did you hear about us", "label": "How did you hear about us?", "type": "select", "options": ["LinkedIn"]}]
    for _ in range(5):  # the same page scanned again and again
        db.note_unanswered(conn, q, "Acme", "job1")
    db.note_unanswered(conn, q, "Beta", "job2")
    assert db.unanswered(conn)[0]["seen"] == 2
    db.answered(conn, ["how did you hear about us"])
    db.note_unanswered(conn, q, "Acme", "job1")
    assert db.unanswered(conn)[0]["seen"] == 1


def test_dropdown_without_its_options_yet_is_not_a_question_to_answer_once():
    fields = [{"id": "1", "label": "Province", "type": "select", "options": [], "value": ""},
              {"id": "2", "label": "Pronouns", "type": "select", "options": ["She/her", "He/him"], "value": ""}]
    actions = [{"id": "1", "label": "Province", "state": "missing", "op": None, "rule": "", "required": True},
               {"id": "2", "label": "Pronouns", "state": "manual", "op": None, "rule": "", "required": False}]
    assert [q["label"] for q in server.unanswered_items(fields, actions)] == ["Pronouns"]


def test_time_estimated_from_opening_to_applying_when_the_timer_missed_it(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    listing = {"source": "simplify", "title": "Intern", "ats": "other", "locations": ["Toronto, ON"], "terms": [],
               "category": "", "sponsorship": "", "degrees": [], "date_posted": 0, "active": True}
    db.upsert_listings(conn, [{**listing, "id": i, "company": i, "url": f"https://x/{i}"} for i in "abcd"], False)
    for job, opened, applied in [("a", "10:00:00", "10:20:00"),   # career site, no ticks: about 20 min
                                 ("b", "10:00:00", "11:30:00"),   # 90 min: counted as 45
                                 ("c", "08:00:00", "11:00:00"),   # 3 hours: you weren't applying all that time
                                 ("d", "10:00:00", "10:12:00")]:  # timed by the extension: the timer wins
        db.log_event(conn, job, "opened", at=f"2026-11-12T{opened}")
        db.log_event(conn, job, "applied", at=f"2026-11-12T{applied}")
        conn.execute("INSERT INTO applications(job_id, status, first_assigned, updated_at) VALUES(?, 'applied', ?, ?)",
                     (job, "2026-11-12", f"2026-11-12T{applied}"))
    for t in range(0, 600, 30):
        db.add_activity(conn, "d", at=1_000_000.0 + t)  # 10 minutes of ticks
    db.add_activity(conn, "a", at=2_000_000.0)          # the one slice "Open application" counts
    timed = db.application_seconds(conn)
    assert timed["a"] == (1200, True) and timed["b"] == (2700, True)
    assert "c" not in timed
    assert timed["d"] == (600, False)
    s = records.stats(conn)
    assert s["estimated"] == 2 and s["timed"] == 3
    by_id = {r["id"]: r for r in records.postings(conn)["rows"]}
    assert by_id["a"]["minutes"] == 20.0 and by_id["a"]["estimated"] is True
    assert by_id["d"]["minutes"] == 10.0 and by_id["d"]["estimated"] is False
