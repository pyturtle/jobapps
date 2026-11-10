"""Answering what JobApps couldn't fill, once: what gets listed, where answers go, how they're matched later."""
from pathlib import Path

from jobapps import db, server
from jobapps.config import Profile
from jobapps.forms import Field
from jobapps.rules import Resolver, neutral

PROFILE = Profile({"name": {"first": "Ada", "last": "Lovelace"}, "address": {"postal_code": ""}}, root=Path("."))


def test_learned_answers_carry_over_between_companies():
    """What you told one company about itself ("No, never interned at Pebl") isn't offered to the next one until a
    second company got the same answer; the exact same wording is always reused."""
    asked = Field("k", "Have you ever interned at CIBC before?", "select", options=["Yes", "No"])
    one = [{"label": "Have you ever interned at Pebl before?", "answer": "No", "learned_from": "Pebl"},
           {"label": "How many hours per week can you work?", "answer": "40", "learned_from": "Steel"}]
    r = Resolver(PROFILE, {"company": "CIBC"}, one)
    assert r.resolve(asked).kind == "manual"
    assert r.resolve(Field("k", "How many hours per week can you work?", "text")).value == "40"
    two = one + [{"label": "Have you ever interned at Steel before?", "answer": "No", "learned_from": "Steel"}]
    r = Resolver(PROFILE, {"company": "CIBC"}, two)
    assert r.resolve(asked).value == "No"
    assert r.resolve(Field("k", "Have you ever interned at Google before?", "text")).kind != "fill"  # only the name masked
    differ = one + [{"label": "Have you ever interned at Steel before?", "answer": "Yes", "learned_from": "Steel"}]
    assert Resolver(PROFILE, {"company": "CIBC"}, differ).resolve(asked).kind == "manual"   # they disagree: ask you
    assert neutral("Why Pebl's mission?", "Pebl") == "why {company} mission?"


def test_what_goes_on_the_answer_once_list():
    fields = [{"id": "1", "label": "Postal Code", "type": "text", "value": ""},
              {"id": "2", "label": "How many hours per week can you work?", "type": "text", "value": ""},
              {"id": "3", "label": "Why us?", "type": "textarea", "value": ""},
              {"id": "4", "label": "I consent to the privacy notice", "type": "boolean", "value": False},
              {"id": "5", "label": "Job Title", "type": "text", "value": "", "section": "Work Experience 4"},
              {"id": "6", "label": "Notice period", "type": "radio", "value": ["2 weeks"], "options": ["2 weeks", "1 month"]},
              {"id": "7", "label": "Are you authorized to work where this job is?", "type": "radio", "value": [],
               "options": ["Yes", "No"]},
              {"id": "8", "label": "Reference name", "type": "text", "value": ""},
              {"id": "9", "label": "I certify that my answers are true", "type": "select", "value": "", "options": ["Yes"]}]
    actions = [{"id": "1", "label": "Postal Code", "state": "manual", "op": None, "rule": "address.postal_code", "required": False},
               {"id": "7", "label": "Are you authorized to work where this job is?", "state": "manual", "op": None,
                "rule": "work_eligibility", "required": True},
               {"id": "8", "label": "Reference name", "state": "manual", "op": None, "rule": "other_person", "required": True},
               {"id": "9", "label": "I certify that my answers are true", "state": "manual", "op": None, "rule": "legal",
                "required": True},
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
    monkeypatch.setattr(server, "load_profile", lambda: PROFILE)  # not your own profile.yaml
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


def test_which_legal_answers_are_remembered(monkeypatch):
    learned = []
    monkeypatch.setattr(server, "load_learned", lambda: list(learned))
    monkeypatch.setattr(server, "save_learned", lambda entries: (learned.clear(), learned.extend(entries)))
    monkeypatch.setattr(server, "load_profile", lambda: PROFILE)
    # the profile's own two facts, whatever rule the form said it was
    assert server.remember("Are you legally authorized to work in Canada?", "Yes", "", "radio", "Acme") == "not stored"
    # no country named: the answer is about that job's country
    assert server.remember("Do you require a work permit to work where this job is located?", "No", "", "radio",
                           "Acme") == "not stored"
    assert server.remember("Will you require Acme to sponsor a visa?", "No", "", "radio", "Acme") == "not stored"
    # names its country and asks more than the profile knows: answered once
    assert server.remember("Do you require a work permit to work in Canada?", "No", "", "radio", "Acme") == "learned"
    assert server.remember("Reference name", "Grace Hopper", "other_person", "text", "Acme") == "not stored"
    assert server.remember("Emergency contact phone number", "416-555-0199", "", "text", "Acme") == "not stored"
    assert [e["label"] for e in learned] == ["Do you require a work permit to work in Canada?"]


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
    monkeypatch.setattr(server, "load_profile", lambda: PROFILE)  # not your own profile.yaml
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


def test_what_is_never_remembered(monkeypatch):
    """From the audit of 2026-11-07: one form's education dates had been learned as "Start Date Year" for every form,
    a checkbox's own label as its answer, and a phone number the profile already has (in another format)."""
    learned = []
    monkeypatch.setattr(server, "load_learned", lambda: list(learned))
    monkeypatch.setattr(server, "save_learned", lambda entries: (learned.clear(), learned.extend(entries)))
    monkeypatch.setattr(server, "load_profile", lambda: Profile({"phone": "(416) 555-0100", "email": "ada@example.com"},
                                                                root=Path(".")))
    assert server.remember("Start Date Year", "2024", "", "combobox") == "not stored"
    assert server.remember("End Date Month", "May", "", "combobox") == "not stored"
    assert server.remember("Current role", "Current role", "", "boolean") == "not stored"
    assert server.remember("Phone Number", "4165550100", "", "text") == "not stored"
    assert server.remember("Email", "ada@example.com", "", "text") == "not stored"
    assert server.remember("Country code", "(+1) Canada", "", "combobox") == "learned"  # the profile's value didn't fit
    assert [e["label"] for e in learned] == ["Country code"]


def test_junk_labels_are_not_questions_waiting_for_you():
    fields = [{"id": "1", "label": "on", "type": "boolean", "value": False},
              {"id": "2", "label": "Custom", "type": "boolean", "value": False},
              {"id": "3", "label": "Notice period", "type": "radio", "value": [], "options": ["2 weeks"]}]
    actions = [{"id": i, "label": l, "state": "manual", "op": None, "rule": "", "required": False}
               for i, l in (("1", "on"), ("2", "Custom"), ("3", "Notice period"))]
    assert [i["label"] for i in server.unanswered_items(fields, actions)] == ["Notice period"]


def test_an_answer_that_cant_be_that_profile_value_is_dropped(monkeypatch):
    """An Oracle form reused its Postal Code box for City: the city was saved as your postal code and "Yes" as
    address line 2."""
    writes, learned = [], []
    monkeypatch.setattr(server.config, "update_profile", lambda updates: writes.append(updates))
    monkeypatch.setattr(server, "load_learned", lambda: list(learned))
    monkeypatch.setattr(server, "save_learned", lambda entries: (learned.clear(), learned.extend(entries)))
    monkeypatch.setattr(server, "load_profile", lambda: PROFILE)
    assert server.remember("Postal Code", "Toronto", "address.postal_code", "text") == "not stored"
    assert server.remember("Address Line 2", "Yes", "address.line2", "text") == "not stored"
    assert server.remember("Address Line 2", "Yes", "address.line2", "dropdown") == "not stored"  # nor learned for the label
    assert server.remember("Phone", "Mobile", "phone", "text") == "not stored"
    assert server.remember("ZIP Code", "94105-1804", "address.postal_code", "text").startswith("profile")
    assert server.remember("Postal Code", "m5s 1a1", "address.postal_code", "text").startswith("profile")
    assert writes == [{"address.postal_code": "94105-1804"}, {"address.postal_code": "m5s 1a1"}] and learned == []
