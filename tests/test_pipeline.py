"""The shortlist and screening steps of the daily pipeline: who gets a place, and what a place costs."""
from datetime import date, datetime

import pytest

from jobapps import db, pipeline, records, screen
from jobapps.config import Profile, load_config

TODAY = date(2026, 11, 2)
BACKGROUND = "Python React"
POSTING = ("About the role\nYou will build internal tools with our platform team.\n"
           "Qualifications\n- Pursuing a bachelor's degree in Computer Science\n- Experience with Python and React\n"
           "Responsibilities\n- Ship features end to end with your mentor during the internship.\n") * 3


def listing(i, company, location="Toronto, ON", days=1, title="Software Engineer Intern"):
    return {"id": i, "source": "t", "company": company, "title": title, "url": f"https://x/{i}", "ats": "greenhouse",
            "locations": [location], "terms": ["Summer 2027"], "category": "Software", "sponsorship": "", "degrees": [],
            "date_posted": int(datetime(2026, 11, 2, 12).timestamp()) - days * 86400, "active": True}


@pytest.fixture
def day(tmp_path, monkeypatch):
    """A day's build with every posting open and readable and Claude faked: day.build(listings, cfg) -> picked ids;
    day.read is what was shortlisted and day.screened what was sent to be screened, in order."""
    class Day:
        conn = db.connect(tmp_path / "t.db")
        read: list[str] = []
        screened: list[str] = []

        def config(self, **top_level):
            cfg = load_config()
            cfg["scoring"]["locations"] = []   # rank on age alone, so a test decides the order with `days`
            cfg.update(top_level)
            return cfg

        def build(self, listings, cfg):
            db.upsert_listings(self.conn, listings, False)
            pipeline.build_batch(self.conn, cfg, Profile({}), TODAY, log=lambda *a: None)
            return [r["job_id"] for r in db.batch(self.conn, TODAY.isoformat())]

    d = Day()
    monkeypatch.setattr(pipeline, "still_open", lambda conn, job, log=print: True)
    monkeypatch.setattr(pipeline, "description_for", lambda conn, job: d.read.append(job["id"]) or POSTING)
    monkeypatch.setattr(pipeline.drafts, "has_material", lambda profile: True)
    monkeypatch.setattr(pipeline.drafts, "background", lambda profile: BACKGROUND)
    monkeypatch.setattr(pipeline.llm, "backend", lambda cfg: "claude-cli")
    monkeypatch.setattr(pipeline.screen, "screen_many", lambda cfg, profile, bg, items, log=print: {
        j["id"]: d.screened.append(j["id"]) or {"fit": 80, "verdict": "strong", "blockers": [], "requirements": []}
        for j, _ in items})
    return d


def test_postings_outside_canada_get_only_their_share(day):
    """One pick in four may be outside Canada, so a quarter of the shortlist and of the new screens may be. The
    rest used to fill with postings the pick step then had to pass over."""
    abroad = [listing(f"us{i}", name, "Austin, TX", days=0)
              for i, name in enumerate(["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"])]
    home = [listing(f"ca{i}", name, days=3) for i, name in enumerate(["Birch", "Cedar", "Maple", "Spruce", "Tamarack"])]
    cfg = day.config(daily_count=4, max_outside_canada_per_day=1)
    cfg["fit"]["shortlist"], cfg["screen"]["top"] = 8, 4
    picked = day.build(abroad + home, cfg)
    assert day.read == ["us0", "us1", "ca0", "ca1", "ca2", "ca3", "ca4"]   # 2 of 8 places, though all six rank higher
    assert day.screened == ["us0", "ca0", "ca1", "ca2"]                    # 1 of 4 screens
    assert sorted(picked) == ["ca0", "ca1", "ca2", "us0"]



def test_no_share_left_once_the_day_has_its_posting_abroad(day):
    cfg = day.config(daily_count=2, max_outside_canada_per_day=1)
    day.build([listing("us0", "Alpha", "Austin, TX", days=0), listing("ca0", "Maple", days=3)], cfg)
    day.read.clear()
    more = [listing("us1", "Bravo", "Seattle, WA", days=0), listing("ca1", "Birch", days=3)]
    db.upsert_listings(day.conn, more, False)
    pipeline.build_batch(day.conn, cfg, Profile({}), TODAY, extra=2, log=lambda *a: None)
    assert day.read == ["ca1"]                                              # the one outside Canada isn't even read


def test_a_posting_screened_before_does_not_use_up_a_new_screen(day):
    cfg = day.config(daily_count=3)
    cfg["screen"]["top"] = 1
    model = screen._model_cfg(cfg)["model"]
    stored = {"fit": 60, "verdict": "good", "blockers": [], "requirements": []}
    db.save_screen(day.conn, "a", screen.input_hash(model, BACKGROUND, POSTING), model, stored)
    day.build([listing("a", "Acme", days=0), listing("b", "Globex", days=1), listing("c", "Initech", days=2)], cfg)
    assert day.screened == ["a", "b"]      # a costs nothing, so the day's one new screen goes to b; c waits


def test_one_role_under_two_links_takes_one_place(day):
    cfg = day.config(daily_count=5, max_per_company=3)
    db.upsert_listings(day.conn, [listing("old", "Initech")], False)
    db.log_event(day.conn, "old", "ruled_out", "2026-10-30", {"reason": "graduate students only"})
    picked = day.build([
        listing("a1", "Acme", days=0), listing("a2", "Acme", "CA, ON, Toronto", days=1),   # two feeds, one job
        listing("a3", "Acme", "Vancouver, BC", days=2),                                     # same title, another city
        listing("twin", "Initech", "Toronto, Ontario, Canada", days=0),                     # ruled out under another link
        listing("other", "Initech", days=1, title="Software Developer Intern"),
    ], cfg)
    assert day.read == ["a1", "other", "a3"]
    assert sorted(picked) == ["a1", "a3", "other"]


def test_companies_in_their_cooldown_are_shortlisted_last(day):
    cfg = day.config(daily_count=1)
    cfg["fit"]["shortlist"] = 1
    db.upsert_listings(day.conn, [listing("had", "Acme", days=5)], False)
    db.add_to_batch(day.conn, "2026-10-30", ["had"])
    db.set_status(day.conn, "had", "applied")
    picked = day.build([listing("acme-2", "Acme", days=0, title="Software Developer Intern"),
                        listing("zeta", "Zeta", days=9)], cfg)
    assert day.read == ["zeta"] and picked == ["zeta"]   # the only place goes to the company you haven't had


def test_recheck_puts_back_only_what_todays_rules_let_through(day):
    """A ruled-out posting is never read again, so one stopped by a rule that has since been fixed needs putting back."""
    cfg = day.config(daily_count=5)
    posts = [listing("misread", "Acme"), listing("grads", "Globex"), listing("had", "Initech"), listing("gone", "Hooli"),
             listing("weak", "Zeta"), listing("unread", "Birch")]
    posts[3]["active"] = False
    db.upsert_listings(day.conn, posts, False)
    for p in posts[:5]:                                         # "unread" has no stored text to check
        grads = "You must be currently enrolled in a Master's or PhD program.\n" if p["id"] == "grads" else ""
        db.save_form(day.conn, p["id"], "greenhouse", POSTING + grads, [])
    for job_id, reason in (("misread", "asks for 22+ years of experience"), ("grads", "graduate students only"),
                           ("had", "application deadline Oct 01, 2026 has passed"), ("gone", "asks for 5+ years of experience"),
                           ("weak", "long shot (verified fit 20/100)"), ("unread", "asks for 5+ years of experience")):
        db.log_event(day.conn, job_id, "ruled_out", "2026-10-30", {"reason": reason})
    db.log_event(day.conn, "had", "added", "2026-10-31")       # you put this one on a list yourself afterwards

    assert [b["id"] for b in pipeline.recheck(day.conn, cfg, Profile({}), TODAY, dry_run=True)] == ["misread"]
    assert "misread" in pipeline.already_seen(day.conn)[0]     # a dry run changes nothing
    back = pipeline.recheck(day.conn, cfg, Profile({}), TODAY)
    assert [(b["id"], b["reason"]) for b in back] == [("misread", "asks for 22+ years of experience")]
    assert [e["kind"] for e in db.events_for(day.conn, "misread")] == ["ruled_out", "rechecked"]   # the record keeps both
    assert pipeline.already_seen(day.conn)[0] == {"grads", "had", "gone", "weak", "unread"}
    status = {r["id"]: r["status"] for r in records.postings(day.conn)["rows"]}
    assert status["misread"] == "not_picked" and status["grads"] == "ruled_out"
    assert pipeline.recheck(day.conn, cfg, Profile({}), TODAY) == []   # nothing left to put back
    assert day.build([], cfg) == ["misread"]                           # the next daily list reads it like a new posting


def test_recheck_leaves_what_a_stored_screen_still_rules_out(day):
    cfg = day.config()
    db.upsert_listings(day.conn, [listing("a", "Acme")], False)
    db.save_form(day.conn, "a", "greenhouse", POSTING, [])
    db.log_event(day.conn, "a", "ruled_out", "2026-10-30", {"reason": "Claude: you need a work permit for this role"})
    model = screen._model_cfg(cfg)["model"]
    stored = {"fit": 70, "verdict": "good", "requirements": [],
              "blockers": [{"kind": "work_authorization", "reason": "you need a work permit", "quote": "work permit"}]}
    db.save_screen(day.conn, "a", screen.input_hash(model, BACKGROUND, POSTING), model, stored)
    assert pipeline.recheck(day.conn, cfg, Profile({}), TODAY) == []   # the same screen would only rule it out again


def test_a_posting_ruled_out_again_after_a_recheck_stays_out(day):
    db.upsert_listings(day.conn, [listing("a", "Acme")], False)
    for kind in ("ruled_out", "rechecked", "ruled_out"):
        db.log_event(day.conn, "a", kind, "2026-10-30", {"reason": "graduate students only"})
    assert pipeline.already_seen(day.conn)[0] == {"a"}
    assert records.postings(day.conn)["rows"][0]["status"] == "ruled_out"
