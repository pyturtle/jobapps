import pytest

from jobapps.config import Profile
from jobapps.forms import Field
from jobapps.rules import MonthYear, Resolver, choose_option, detect_country, focus, job_country, norm

PROFILE = Profile({
    "name": {"first": "Ada", "last": "Lovelace", "preferred": "Ada"},
    "email": "ada@example.com", "phone": "416-555-0100", "location": "Toronto, Ontario, Canada",
    "address": {"city": "Toronto", "region": "Ontario", "country": "Canada"},
    "links": {"linkedin": "https://linkedin.com/in/ada", "github": "https://github.com/ada"},
    "education": {"school": "University of Toronto", "degree": ["Bachelor of Science", "Bachelor's"],
                  "major": "Computer Science", "graduation_month": "May", "graduation_year": 2028,
                  "currently_enrolled": True},
    "work_authorization": {"canada": True, "us": False},
    "needs_sponsorship": {"canada": False, "us": True},
    "preferences": {"heard_about": ["Job board", "Other"], "willing_onsite": True},
    "eeo": {"gender": "decline"},
    "custom_answers": [{"match": "hours (per|a) week", "answer": "40"}],
}, root=__import__("pathlib").Path("."))

TORONTO_JOB = {"company": "1Password", "locations": ["Toronto, ON, Canada"]}


def resolve(label, type="text", options=(), required=True, job=TORONTO_JOB, drafts=None, learned=()):
    return Resolver(PROFILE, job, learned, drafts).resolve(Field("k", label, type, required, list(options)))


def test_norm_and_focus():
    assert norm("Full name ✱") == "full name"
    assert norm("First Name *") == "first name"
    long = "this role requires you to be authorized. do you live in the countries listed?"
    assert focus(long) == "do you live in the countries listed?"
    abbrev = "are you enrolled at an institution (e.g. college, university)? we ask the u.s. ones too."
    assert focus(abbrev) == "are you enrolled at an institution (e.g. college, university)?"
    trailing = "are you a current or previously serving member in the military? if so, what capacity?"
    assert focus(trailing) == "are you a current or previously serving member in the military?"


def test_live_regressions():
    # 'How did you find out' inside an essay question must not trigger "how did you hear about us"
    assert resolve("Tell us about a time an AI tool gave you a wrong answer. How did you find out, and what did "
                   "you change?", type="textarea").kind == "draft"
    assert resolve("Are you a current or previously serving member in the military or armed forces? If so, what "
                   "capacity?", type="radio", options=["Yes", "No", "Prefer not to say"]).value == "Prefer not to say"
    assert resolve("Autofill from resume", type="file", required=False).kind == "skip"


@pytest.mark.parametrize("value,options,expected", [
    ("decline", ["Male", "Female", "Decline To Self Identify"], "Decline To Self Identify"),
    ("decline", ["Yes", "No", "I don't wish to answer"], "I don't wish to answer"),
    (True, ["Yes, I am authorized", "No"], "Yes, I am authorized"),
    (False, ["Yes", "No, I will not"], "No, I will not"),
    ("Canada", ["United States (+1)", "Canada (+1)"], "Canada (+1)"),
    (["Bachelor of Science", "Bachelor's"], ["Associate's", "Bachelor's Degree", "Master's"], "Bachelor's Degree"),
    (["Job board", "Other"], ["Agency", "Online job board", "Other"], "Online job board"),
    ("Toronto", ["Torontonian Society", "Toronto"], "Toronto"),
    ("us", ["Russia", "Belarus"], None),
])
def test_choose_option(value, options, expected):
    assert choose_option(value, options) == expected


def test_countries():
    assert detect_country("are you legally authorized to work in the united states?") == "us"
    assert detect_country("are you authorized to work in canada?") == "canada"
    assert detect_country("are you authorized to work in the us or canada?") is None
    assert job_country(["Toronto, ON, Canada", "Waterloo, ON"]) == "canada"
    assert job_country(["Austin, TX", "NYC"]) == "us"
    assert job_country(["Toronto, ON, Canada", "NYC"]) is None


def test_identity_fields():
    assert resolve("First Name").value == "Ada"
    assert resolve("Last Name").value == "Lovelace"
    assert resolve("Full Name").value == "Ada Lovelace"
    assert resolve("Email").value == "ada@example.com"
    assert resolve("LinkedIn URL").value.endswith("/ada")
    assert resolve("Current Location", type="combobox").value[0] == "Toronto, Ontario, Canada"
    assert resolve("X (formerly Twitter) URL", required=False).kind == "skip"
    assert resolve("Pronouns", required=False).kind == "skip"


def test_ashby_style_questions():
    r = resolve("Are you currently enrolled at an educational institution (e.g. college, university, bootcamp)?",
                type="boolean")
    assert (r.kind, r.value) == ("fill", True)
    r = resolve("Which institution do you attend? If your institution is not listed, please select 'Other'.",
                type="select", options=["Toronto Metropolitan University", "University of Toronto", "Other"])
    assert r.value == "University of Toronto"
    assert resolve("If your institution was not listed in the previous question, please enter it here.",
                   required=False).kind == "skip"
    r = resolve("Anticipated graduation date", type="date")
    assert r.value == MonthYear(2028, 5)
    r = resolve("This role requires you to already be legally authorized to work in the countries listed and we "
                "will not be offering sponsorship/transferring of a visa. Do you live in, and are you legally "
                "authorized to work in the countries listed in the job posting?", type="boolean")
    assert (r.kind, r.value) == ("fill", True)
    assert resolve("Are you a former 1Password employee?", type="boolean").value is False
    assert resolve("Interested in learning more about careers here? Check this box to sign up.",
                   type="checkbox", options=["Yes"], required=False).kind == "skip"
    assert resolve("I understand that offers are conditional on a background check.",
                   type="checkbox", options=["I understand"]).kind == "manual"


def test_telus_style_questions():
    assert resolve("When is your anticipated degree completion date?", type="date").value == MonthYear(2028, 5)
    r = resolve("Have you completed or are you currently enrolled in the course: Data Structures & Algorithms?",
                type="select", options=["Yes", "Currently taking", "Not yet"])
    assert r.kind == "manual"
    assert resolve("If you have been previously employed by TELUS Digital, please specify the company, your role, "
                   "and dates of employment:", required=False).kind == "skip"
    assert resolve("Program / course of study").value == "Computer Science"
    assert resolve("If you could build any product, what would it be?", type="textarea").kind == "draft"


def test_mongodb_style_questions():
    p = Profile({**PROFILE.data, "preferences": {**PROFILE.data["preferences"], "term": ["Summer 2027"],
                                                  "locations": ["New York", "Toronto"]}})
    res = lambda label, **kw: Resolver(p, TORONTO_JOB).resolve(
        Field("k", label, kw.get("type", "text"), kw.get("required", True), kw.get("options", [])))
    assert res("Chosen Name").value == "Ada"
    assert res("Are you available to work full-time for 12 weeks in Summer 2027?", type="radio",
               options=["Yes", "No"]).value == "Yes"
    assert res("Are you available to work in Fall 2027?", type="radio", options=["Yes", "No"]).kind == "manual"
    assert res("Which of the following locations are you open to living and working in?", type="checkbox",
               options=["Palo Alto", "New York City", "Austin"]).value == ["New York City"]
    assert res("Would you like to stay up to date with Careers content?", type="boolean", required=False).kind == "skip"
    assert res("Receive alerts for similar jobs?", type="boolean", required=False).kind == "skip"


def test_work_authorization_by_country():
    us_job = {"company": "X", "locations": ["Austin, TX"]}
    r = resolve("Are you legally authorized to work in the United States?", type="radio", options=["Yes", "No"])
    assert r.value == "No"
    r = resolve("Will you now or in the future require sponsorship for employment visa status?",
                type="radio", options=["Yes", "No"], job=us_job)
    assert r.value == "Yes"
    r = resolve("Are you legally authorized to work in the country this job is in?", type="radio",
                options=["Yes", "No"], job={"company": "X", "locations": ["Toronto, ON, Canada", "NYC"]})
    assert r.kind == "manual"


def test_eeo_and_consent():
    assert resolve("Gender", type="select", options=["Male", "Female", "Decline to self-identify"]).value \
        == "Decline to self-identify"
    assert resolve("Are you Hispanic/Latino?", type="select",
                   options=["Yes", "No", "Decline To Self Identify"]).value == "Decline To Self Identify"
    assert resolve("I agree to the privacy policy", type="boolean").kind == "manual"


def test_free_text_and_guards():
    r = resolve("Tell us about a time an AI coding tool gave you an answer that looked right but wasn't.",
                type="textarea")
    assert r.kind == "draft" and r.value is None
    drafted = resolve("Why do you want to work here?", type="textarea",
                      drafts={"why do you want to work here?": "Because..."})
    assert (drafted.kind, drafted.value) == ("draft", "Because...")
    # a yes/no rule must not hijack an open question
    assert resolve("Describe your experience with hybrid cloud deployments", type="textarea").kind == "draft"
    assert resolve("Additional Information", type="textarea", required=False).kind == "skip"
    assert resolve("Do you have experience in cybersecurity SaaS?", type="boolean", required=False).kind == "manual"


def test_custom_and_learned_take_priority():
    assert resolve("How many hours per week can you work?").value == "40"
    learned = [{"label": "What is your T-shirt size?", "answer": "M"}]
    assert resolve("What is your T-shirt size?", type="select", options=["S", "M", "L"], learned=learned).value == "M"


def test_profile_todo_is_missing():
    p = Profile({"phone": "TODO", "work_authorization": {"us": "TODO"}})
    assert p.get("phone") is None
    r = Resolver(p, {"locations": ["NYC"]}).resolve(
        Field("k", "Are you authorized to work in the US?", "radio", True, ["Yes", "No"]))
    assert r.kind == "manual"
