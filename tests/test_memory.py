"""Answering what JobApps couldn't fill, once: what gets listed, where answers go, how they're matched later."""
from pathlib import Path

from jobapps import db, server
from jobapps.config import Profile
from jobapps.forms import Field
from jobapps.rules import Resolver, neutral

PROFILE = Profile({"name": {"first": "Ada", "last": "Lovelace"}, "address": {"postal_code": ""}}, root=Path("."))


def test_learned_answers_carry_over_between_companies():
    learned = [{"label": "Have you ever interned at Pebl before?", "answer": "No", "learned_from": "Pebl"},
               {"label": "How many hours per week can you work?", "answer": "40", "learned_from": "Steel"}]
    r = Resolver(PROFILE, {"company": "CIBC"}, learned)
    assert r.resolve(Field("k", "Have you ever interned at CIBC before?", "select", options=["Yes", "No"])).value == "No"
    assert r.resolve(Field("k", "How many hours per week can you work?", "text")).value == "40"
    assert r.resolve(Field("k", "Have you ever interned at Google before?", "text")).kind != "fill"  # only the name masked
    assert neutral("Why Pebl's mission?", "Pebl") == "why {company} mission?"


def test_what_goes_on_the_answer_once_list():
    fields = [{"id": "1", "label": "Postal Code", "type": "text", "value": ""},
              {"id": "2", "label": "How many hours per week can you work?", "type": "text", "value": ""},
              {"id": "3", "label": "Why us?", "type": "textarea", "value": ""},
              {"id": "4", "label": "I consent to the privacy notice", "type": "boolean", "value": False},
              {"id": "5", "label": "Job Title", "type": "text", "value": "", "section": "Work Experience 4"},
              {"id": "6", "label": "Notice period", "type": "radio", "value": ["2 weeks"], "options": ["2 weeks", "1 month"]}]
    actions = [{"id": "1", "label": "Postal Code", "state": "manual", "op": None, "rule": "address.postal_code", "required": False},
               {"id": "2", "label": "How many hours per week can you work?", "state": "missing", "op": None, "rule": "", "required": True},
               {"id": "3", "label": "Why us?", "state": "missing", "op": None, "rule": "draft", "required": True},
               {"id": "4", "label": "I consent to the privacy notice", "state": "missing", "op": None, "rule": "consent", "required": True},
               {"id": "5", "label": "Job Title", "state": "missing", "op": None, "rule": "", "required": True},
               {"id": "6", "label": "Notice period", "state": "manual", "op": None, "rule": "", "required": False}]
    items = server.unanswered_items(fields, actions)
    assert [i["label"] for i in items] == ["Postal Code", "How many hours per week can you work?"]
    assert items[0]["rule"] == "address.postal_code"


def test_answers_go_to_the_profile_or_memory(monkeypatch):
    profile_writes, learned = [], []
    monkeypatch.setattr(server.config, "update_profile", lambda updates: profile_writes.append(updates))
    monkeypatch.setattr(server, "load_learned", lambda: list(learned))
    monkeypatch.setattr(server, "save_learned", lambda entries: (learned.clear(), learned.extend(entries)))
    assert server.remember("Postal Code", "M5S 1A1", "address.postal_code") == "profile: address.postal_code"
    assert profile_writes == [{"address.postal_code": "M5S 1A1"}]
    assert server.remember("How many hours per week?", "40", "") == "learned"
    assert server.remember("Notice period", ["2 weeks"], "", "radio", "Pebl") == "learned"
    assert {e["label"]: e["answer"] for e in learned} == {"How many hours per week?": "40", "Notice period": "2 weeks"}
    # sponsorship / work authorization / consent come from the profile each time; "on" is a browser default
    assert server.remember("Authorized to work in the US?", "Yes", "work_authorization.us") == "not stored"
    assert server.remember("Terms and Conditions: I agree to the above", "I agree", "") == "not stored"
    assert server.remember("Choose your AI preference", "on", "") == "not stored"
    assert len(learned) == 2


def test_profile_beats_memory_for_legal_and_consent():
    profile = Profile({"work_authorization": {"canada": True, "us": False},
                       "needs_sponsorship": {"canada": False, "us": True}}, root=Path("."))
    learned = [{"label": "Will you now or in the future require sponsorship for employment visa status?", "answer": "Yes",
                "learned_from": "Sardine"},
               {"label": "Terms and Conditions", "answer": "I agree", "learned_from": "CIBC"},
               {"label": "Have you worked with us before?", "answer": "No", "learned_from": "CIBC"}]
    toronto = Resolver(profile, {"company": "Pebl", "locations": ["Toronto, ON, Canada"]}, learned)
    q = Field("k", "Will you now or in the future require sponsorship for employment visa status?", "select", options=["Yes", "No"])
    assert toronto.resolve(q).value == "No"                                    # your profile, for a Canadian job
    unknown = Resolver(profile, {"company": "X", "locations": []}, learned)
    assert unknown.resolve(q).kind == "manual"                                 # can't tell the country: ask you
    assert toronto.resolve(Field("k", "Terms and Conditions", "boolean")).kind == "manual"
    assert toronto.resolve(Field("k", "Have you worked with us before?", "select", options=["Yes", "No"])).value == "No"


def test_open_questions_are_counted_and_cleared(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    q = {"qkey": "how many hours per week can you work?", "label": "How many hours per week can you work?", "type": "text",
         "options": [], "rule": "", "required": True}
    db.note_unanswered(conn, [q], "Pebl", "a")
    db.note_unanswered(conn, [q], "CIBC", "b")
    (row,) = db.unanswered(conn)
    assert row["seen"] == 2 and row["company"] == "CIBC"
    db.answered(conn, [q["qkey"]], dismiss=True)
    assert db.unanswered(conn) == []
    db.note_unanswered(conn, [q], "Steel", "c")  # dismissed stays hidden
    assert db.unanswered(conn) == []


def test_a_picked_option_never_overwrites_the_profile(monkeypatch):
    writes, learned = [], []
    monkeypatch.setattr(server.config, "update_profile", lambda updates: writes.append(updates))
    monkeypatch.setattr(server, "load_learned", lambda: list(learned))
    monkeypatch.setattr(server, "save_learned", lambda entries: (learned.clear(), learned.extend(entries)))
    # a salary-range dropdown is that question's answer, not your salary expectation
    assert server.remember("Desired salary", "Below $60,000", "preferences.salary_expectation", "dropdown") == "learned"
    assert server.remember("Desired salary", "25", "preferences.salary_expectation", "text").startswith("profile")
    assert writes == [{"preferences.salary_expectation": "25"}]


def test_employer_rule_only_matches_questions_asking_for_it():
    profile = Profile({"experience": {"current_company": "Initech", "current_title": "Developer"}}, root=Path("."))
    r = Resolver(profile, {})
    assert r.resolve(Field("k", "Current Employer", "text")).value == "Initech"
    assert r.resolve(Field("k", "Name of your current company", "text")).value == "Initech"
    notice = r.resolve(Field("k", "Please specify your required notice period with your current employer", "radio",
                             options=["2 weeks", "No notice period required"]))
    assert notice.source != "experience.current_company"
    assert r.resolve(Field("k", "What do you enjoy about your current role?", "text")).kind == "draft"
