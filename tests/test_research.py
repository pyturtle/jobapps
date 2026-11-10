"""Reading postings JobApps can't prefetch, Claude lookups, and the timing rules from the 2026 research."""
from datetime import date, datetime

from jobapps import db, forms, research
from jobapps.config import Profile, load_config
from jobapps.fit import Candidate, deadline, evaluate

TODAY = date(2026, 11, 2)
POSTING = ("About the role\nYou will build internal tools with our platform team.\n"
           "Qualifications\n- Pursuing a bachelor's degree in Computer Science\n- Experience with Python and React\n"
           "Responsibilities\n- Ship features end to end with your mentor during the internship.\n") * 3


def test_menus_are_not_a_posting_but_embedded_jobposting_data_is():
    assert forms.looks_like_posting(POSTING)
    assert not forms.looks_like_posting("Home - Careers - Search jobs - Sign in - Cookie settings - " * 30)
    html = ('<html><body><div id="app"></div><script type="application/ld+json">{"@context": "https://schema.org", '
            '"@type": "JobPosting", "title": "SWE Intern", "description": ' + __import__("json").dumps(POSTING) +
            '}</script></body></html>')
    assert forms.looks_like_posting(forms.jsonld_posting(html))
    assert forms.jsonld_posting("<html><body>no data</body></html>") == ""


def test_deadlines_and_talent_pools():
    assert deadline("Applications close October 15, 2026.") == date(2026, 10, 15)
    assert deadline("Apply by 20 Sept 2026") == date(2026, 9, 20)
    assert deadline("Application deadline: 2026-11-05") == date(2026, 11, 5)
    assert deadline("Posting opens Sept 1, 2026 and applications close Oct 1, 2026") == date(2026, 10, 1)
    assert deadline("Expected graduation May 2028") is None          # a date, but not a deadline
    cand = Candidate(skills={"Python"}, has_material=True, grad_year=2028, gpa=3.6, citizenship=["canada"],
                     needs_sponsorship={"us": True, "canada": False})
    job = {"title": "Software Intern", "locations": ["Toronto, ON"]}
    cfg = load_config()
    passed = evaluate(job, POSTING + "\nApplications close October 15, 2026.", cand, cfg, TODAY)
    assert not passed.eligible and "deadline" in passed.blockers[0]["reason"]
    soon = evaluate(job, POSTING + "\nApplication deadline: November 6, 2026.", cand, cfg, TODAY)
    assert soon.eligible and any("apply soon" in why for _, why in soon.points)
    pool = evaluate(job, POSTING + "\nThis posting is not for an existing vacancy.", cand, cfg, TODAY)
    plain = evaluate(job, POSTING, cand, cfg, TODAY)
    assert pool.score == plain.score - 15


JOB = {"id": "j1", "company": "Acme", "title": "Software Engineer Intern", "locations": ["Vancouver, BC"],
       "url": "https://campus-acme.icims.com/jobs/1234/software-engineer-intern/job"}


def test_lookup_reads_the_pages_claude_points_to(monkeypatch):
    cfg = load_config()
    pages = {"https://www.acme.example/careers/1234": "Acme - Software Engineer Intern\n" + POSTING,
             "https://jobs.example.com/other": "Initech - Data Intern\n" + POSTING}
    monkeypatch.setattr(forms, "page_description", lambda url: pages.get(url, ""))

    def claude(urls, summary=""):
        monkeypatch.setattr(research.llm, "structured", lambda *a, **k: {"found": True, "urls": urls, "summary": summary})

    claude([JOB["url"], "https://jobs.example.com/other", "https://www.acme.example/careers/1234"])
    r = research.lookup(cfg, JOB)          # skips the unreadable original and a page about another job
    assert r["verified"] and r["url"] == "https://www.acme.example/careers/1234" and "Qualifications" in r["text"]
    claude(["https://unreadable.example.com/x"], summary="Qualifications: pursuing a CS degree. " * 20)
    r = research.lookup(cfg, JOB)
    assert r["found"] and not r["verified"]                      # Claude's own summary, flagged
    claude(["https://unreadable.example.com/x"], summary="Software intern.")
    assert not research.lookup(cfg, JOB)["found"]               # too little to judge fit by


def test_lookups_happen_once_per_posting_and_within_the_daily_allowance(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db")
    cfg = load_config()
    cfg["research"]["max_per_day"] = 2
    calls = []
    monkeypatch.setattr(research, "lookup", lambda cfg, job: calls.append(job["id"]) or
                        {"found": True, "url": "u", "text": POSTING, "verified": True})
    jobs = [{**JOB, "id": f"j{i}"} for i in range(4)]
    assert set(research.research_many(conn, cfg, jobs)) == {"j0", "j1"}
    assert set(research.research_many(conn, cfg, jobs)) == {"j0", "j1"}   # cached, and today's allowance is used up
    assert calls == ["j0", "j1"]


def test_old_postings_need_a_good_verified_fit(tmp_path, monkeypatch):
    from jobapps import pipeline, screen

    def ago(days):
        return int(datetime(2026, 11, 2, 12).timestamp()) - days * 86400

    def listing(i, company, days):
        return {"id": i, "source": "t", "company": company, "title": "Software Engineer Intern", "url": f"https://x/{i}",
                "ats": "greenhouse", "locations": ["Toronto, ON"], "terms": ["Summer 2027"], "category": "Software",
                "sponsorship": "", "degrees": [], "date_posted": ago(days), "active": True}

    conn = db.connect(tmp_path / "t.db")
    db.upsert_listings(conn, [listing("fresh", "Acme", 2), listing("old-good", "Hooli", 40),
                              listing("old-weak", "Globex", 40), listing("old-unscreened", "Initech", 40)], False)
    fits = {"fresh": 40, "old-good": 80, "old-weak": 45}
    monkeypatch.setattr(pipeline, "still_open", lambda conn, job, log=print: True)
    monkeypatch.setattr(pipeline, "description_for", lambda conn, job: POSTING)
    monkeypatch.setattr(pipeline.drafts, "has_material", lambda profile: True)
    monkeypatch.setattr(pipeline.drafts, "background", lambda profile: "Python React")
    monkeypatch.setattr(pipeline.llm, "backend", lambda cfg: "claude-cli")
    monkeypatch.setattr(pipeline.screen, "screen_many", lambda cfg, profile, bg, items, log=print: {
        j["id"]: {"fit": fits[j["id"]], "verdict": screen.verdict(fits[j["id"]]), "blockers": [], "requirements": []}
        for j, _ in items if j["id"] in fits})
    cfg = load_config()
    cfg["daily_count"] = 10
    pipeline.build_batch(conn, cfg, Profile({}), TODAY, log=lambda *a: None)
    picked = {r["job_id"] for r in db.batch(conn, TODAY.isoformat())}
    assert picked == {"fresh", "old-good"}
    ruled_out = {e["id"]: e["reason"] for e in __import__("json").loads(db.get_meta(conn, f"excluded:{TODAY}"))}
    assert "only a stretch fit" in ruled_out["old-weak"]
    assert "old-unscreened" not in ruled_out                     # waits for a screen instead of being ruled out


def test_screens_go_to_companies_outside_their_cooldown_first(tmp_path, monkeypatch):
    """With one screen a day, an old posting from a company you had last week used to take it, and the picker then
    fell back to that company (the same company came back three days running)."""
    from jobapps import pipeline, screen

    def listing(i, company):
        return {"id": i, "source": "t", "company": company, "title": "Software Engineer Intern", "url": f"https://x/{i}",
                "ats": "greenhouse", "locations": ["Toronto, ON"], "terms": ["Summer 2027"], "category": "Software",
                "sponsorship": "", "degrees": [], "date_posted": int(datetime(2026, 9, 20, 12).timestamp()), "active": True}

    conn = db.connect(tmp_path / "t.db")
    db.upsert_listings(conn, [listing("had", "Acme"), {**listing("acme-2", "Acme"), "title": "Software Developer Intern"},
                              listing("zeta", "Zeta")], False)
    db.add_to_batch(conn, "2026-10-30", ["had"])
    db.set_status(conn, "had", "applied")
    monkeypatch.setattr(pipeline, "still_open", lambda conn, job, log=print: True)
    monkeypatch.setattr(pipeline, "description_for", lambda conn, job: POSTING)
    monkeypatch.setattr(pipeline.drafts, "has_material", lambda profile: True)
    monkeypatch.setattr(pipeline.drafts, "background", lambda profile: "Python React")
    monkeypatch.setattr(pipeline.llm, "backend", lambda cfg: "claude-cli")
    screened = []
    monkeypatch.setattr(pipeline.screen, "screen_many", lambda cfg, profile, bg, items, log=print: {
        j["id"]: screened.append(j["id"]) or {"fit": 80, "verdict": screen.verdict(80), "blockers": [], "requirements": []}
        for j, _ in items})
    cfg = load_config()
    cfg["daily_count"], cfg["screen"]["top"] = 1, 1
    pipeline.build_batch(conn, cfg, Profile({}), TODAY, log=lambda *a: None)
    assert screened == ["zeta"]
    assert [r["job_id"] for r in db.batch(conn, TODAY.isoformat())] == ["zeta"]
