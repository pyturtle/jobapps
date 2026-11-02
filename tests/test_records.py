"""The permanent record (events), the timer, and the postings archive / stats built from them."""
from jobapps import db, records


def listing(i, company, locations, active=True):
    return {"id": i, "source": "simplify", "company": company, "title": f"{company} Intern", "url": f"https://x/{i}",
            "ats": "workday" if i == "a" else "greenhouse", "locations": locations, "terms": [], "category": "",
            "sponsorship": "", "degrees": [], "date_posted": 1793000000, "active": active}


def test_record_timer_and_archive(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_listings(conn, [listing("a", "Trimble", ["Toronto, ON"]), listing("b", "Simbe", ["Burlingame, CA"]),
                              listing("c", "Altera", ["Toronto, ON"]), listing("d", "Old Co", ["Ottawa, ON"], False)], False)
    db.add_to_batch(conn, "2026-10-25", ["a", "b"])
    db.add_to_batch(conn, "2026-10-26", ["b"])              # unfinished: carried
    db.add_to_batch(conn, "2026-10-26", ["c"], how="added")  # you added it
    db.log_event(conn, "d", "ruled_out", "2026-10-25", {"reason": "needs a master's"})
    db.add_activity(conn, "a", at=1000.0)
    db.add_activity(conn, "a", at=1010.0)                    # same 30 s slice: counted once
    assert db.add_activity(conn, "a", at=1040.0) == 60
    for t in range(0, 600, 30):
        db.add_activity(conn, "b", at=5000.0 + t)            # 10 minutes
    db.set_status(conn, "a", "applied")
    db.set_status(conn, "b", "applied")
    assert [e["kind"] for e in db.events_for(conn, "b")] == ["picked", "carried", "applied"]
    assert [e["kind"] for e in db.events_for(conn, "c")] == ["added"]

    page = records.postings(conn)
    by_id = {r["id"]: r for r in page["rows"]}
    assert by_id["a"]["status"] == "applied" and by_id["a"]["minutes"] == 1.0
    assert by_id["c"]["status"] == "on_list" and by_id["c"]["how"] == "added"
    assert by_id["d"]["status"] == "ruled_out" and by_id["d"]["reason"] == "needs a master's"
    assert page["summary"]["total"] == 4 and page["summary"]["applied"] == 2 and page["summary"]["ever_listed"] == 3
    assert [r["id"] for r in records.postings(conn, region="other")["rows"]] == ["b"]
    assert {r["id"] for r in records.postings(conn, q="toronto")["rows"]} == {"a", "c"}
    assert [r["id"] for r in records.postings(conn, active="closed")["rows"]] == ["d"]
    assert "Old Co" in records.postings_csv(conn, status="ruled_out")

    s = records.stats(conn)
    assert s["applied"] == 2 and s["timed"] == 2 and s["average"] == 5.5 and s["fastest"]["id"] == "a"
    assert {x["ats"]: x["average"] for x in s["by_site"]} == {"workday": 1.0, "greenhouse": 10.0}


def test_repick_keeps_a_record(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_listings(conn, [listing("a", "Trimble", ["Toronto, ON"])], False)
    db.add_to_batch(conn, "2026-10-26", ["a"])
    assert db.reset_new_picks(conn, "2026-10-26") == 1
    assert [e["kind"] for e in db.events_for(conn, "a")] == ["picked", "released"]
    assert records.postings(conn)["rows"][0]["status"] == "released"


def test_history_keeps_links_to_past_applications(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_listings(conn, [listing("a", "Trimble", ["Toronto, ON"])], False)
    db.save_form(conn, "a", "workday", "Build test tooling in Python.", [])
    db.add_to_batch(conn, "2026-10-26", ["a"])
    db.set_status(conn, "a", "applied", url="https://trimble.wd1.myworkdayjobs.com/x/job/y_R1/apply/applyManually")
    row = db.history(conn)[0]
    assert row["url"] == "https://x/a" and row["saved_copy"] is True
    assert row["applied_url"].endswith("/apply/applyManually")
    assert records.postings(conn)["rows"][0]["applied_url"] == row["applied_url"]


def test_no_repeats(tmp_path, monkeypatch):
    from datetime import date

    from jobapps import pipeline
    from jobapps.sources import url_key
    # the same Workday requisition reposted on another of the company's career sites
    assert url_key("https://rbc.wd3.myworkdayjobs.com/RBCEARLYTALENT1/job/TORONTO/Developer_R-0000187114-1") == \
        url_key("https://rbc.wd3.myworkdayjobs.com/rbcglobal1/job/TORONTO/Developer_R-0000187114") == "workday:rbc:r-0000187114"
    assert pipeline.same_job_key("RBC", "Developer Intern (Summer 2027)") == pipeline.same_job_key("rbc", "Developer Intern - 2027")
    assert pipeline.same_job_key("RBC", "Developer Intern") != pipeline.same_job_key("RBC", "Data Intern")

    conn = db.connect(tmp_path / "t.db")
    db.upsert_listings(conn, [listing("a", "Trimble", ["Toronto, ON"]), listing("b", "Simbe", ["Toronto, ON"]),
                              listing("c", "Altera", ["Toronto, ON"]), listing("d", "Intel", ["Toronto, ON"])], False)
    db.add_to_batch(conn, "2026-10-20", ["a"])        # too old to carry any more
    db.add_to_batch(conn, "2026-10-27", ["b", "c"])
    db.set_status(conn, "c", "applied")
    db.log_event(conn, "d", "ruled_out", "2026-10-27", {"reason": "graduate students only"})
    monkeypatch.setattr(pipeline, "still_open", lambda conn, job, log=print: True)
    cfg = {"carry_over": True, "carry_days": 3, "carry_max": 5}
    assert pipeline.carry_over(conn, cfg, date(2026, 10, 28)) == ["b"]           # a is from 8 days ago
    ids, keys = pipeline.already_seen(conn)
    assert ids == {"a", "b", "c", "d"}                                            # never picked again
    assert pipeline.same_job_key("Altera", "Altera Intern") in keys and pipeline.same_job_key("Intel", "Intel Intern") not in keys
    assert pipeline.recent_companies(conn, date(2026, 10, 28), 7) == {"simbe", "altera"}
    # saying "applied" again is not a second event
    db.set_status(conn, "c", "applied")
    assert [e["kind"] for e in db.events_for(conn, "c")].count("applied") == 1


def test_one_company_many_spellings():
    from jobapps.pipeline import company_key, company_keys, same_job_key
    assert company_keys("Hewlett Packard Enterprise") & company_keys("HPE")
    assert company_keys("Royal Bank of Canada") & company_keys("RBC")
    assert company_key("Motorola Solutions Inc.") == company_key("Motorola Solutions")
    assert not company_keys("General Magic") & company_keys("GM")             # two letters never merge companies
    assert not company_keys("Uber") & company_keys("Disney")
    assert same_job_key("Hewlett Packard Enterprise", "Software Development Intern - AI-Assisted Development - Summer 2027")         == same_job_key("HPE", "Software Development Intern, AI-Assisted Development (Winter 2027)")
