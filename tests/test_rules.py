import pytest

from jobapps.config import Profile
from jobapps.forms import Field
from jobapps.rules import MonthYear, Resolver, choose_option, detect_country, focus, is_canadian, job_country, norm

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
US_JOB = {"company": "Acme", "locations": ["Austin, TX"]}
YES_NO = ["Yes", "No"]


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
    # a place that isn't clearly one of the two gives no country, so nothing is answered from a guess
    assert job_country(["London, UK"]) is None
    assert job_country(["Bangalore, IN"]) is None
    assert job_country(["Remote (US or Canada)"]) is None
    assert job_country(["Toronto, ON, CA"]) == "canada"       # CA after a province is the country, not California
    assert job_country(["Markham, IL"]) == "us" and not is_canadian("Markham, IL")
    assert detect_country("will you require sponsorship from us?") is None  # the employer, not the country
    assert detect_country("Are you authorized to work in the US?") == "us"


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
    # where you live and whether you may work there are two questions: one yes can't answer both
    r = resolve("This role requires you to already be legally authorized to work in the countries listed and we "
                "will not be offering sponsorship/transferring of a visa. Do you live in, and are you legally "
                "authorized to work in the countries listed in the job posting?", type="boolean")
    assert (r.kind, r.source) == ("manual", "work_eligibility")
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


def test_recorded_form_mistakes():
    """Questions from real forms that were drafted, mis-filled or left for you before (audit of 2026-11-07)."""
    # Workday's honeypot: anything typed here flags the application as a bot
    assert resolve("Enter website. This input is for robots only, do not enter if you're human.").kind == "skip"
    # sponsorship in a text box is still the profile's answer, never a draft with [Yes/No] placeholders
    r = resolve("Do you need work visa sponsorship now or any time in the future? (if yes, please provide details)",
                type="textarea", job={"company": "X", "locations": ["Austin, TX"]})
    assert (r.kind, r.source, r.value) == ("fill", "needs_sponsorship.us", True)
    assert resolve("Please list the names of any relatives currently employed by Acme.", type="textarea").value == "None"
    assert resolve("Do you have relatives working at Acme?", type="radio", options=["Yes", "No"]).value == "No"
    assert resolve("Please provide your Acme Login ID (alias) if you are a current or former Acme employee; if you "
                   "have never worked for Acme, enter 'N/A'.").value == "N/A"
    assert resolve("CERTIFICATION: By initialing below, you hereby agree that all information you provided is "
                   "true.").kind == "manual"
    assert resolve("What is your accounting GPA? Please put N/A if not applicable").kind == "manual"
    assert resolve("Level of education completed once you've graduated?", type="select",
                   options=["GED or High School", "Bachelor", "Masters"]).value == "Bachelor"
    assert resolve("I would like to receive monthly job recommendations by email.", type="boolean",
                   required=False).kind == "skip"
    assert resolve("Current role", type="boolean", required=False).kind == "skip"
    for label in ("Enter verification code digit 3 of six.", "Keep me signed in", "Job title, skill, keyword"):
        assert resolve(label, type="text").kind == "skip", label
    assert resolve("What are your pronouns?", type="textarea", required=False).kind == "skip"
    assert resolve("Preferred name", type="textarea").value == "Ada"
    assert resolve("Why do you want this role? Tell us about yourself.", type="textarea").kind == "draft"


def test_resides_in():
    assert resolve("Are you located in Toronto, Ontario?", type="buttons", options=["Yes", "No"]).value == "Yes"
    assert resolve("Are you currently residing in the province of Nova Scotia?", type="radio",
                   options=["Yes", "No"]).value == "No"
    assert resolve("Do you live in Springfield?", type="radio", options=["Yes", "No"]).kind == "manual"


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


def test_legal_questions_come_before_rules_that_share_a_word():
    """Found in review (2026-11-07): graduation / school / job-alert rules answered these first, which also let an
    answer remembered from another form fill them."""
    learned = [{"label": "Will you now or in the future require visa sponsorship to continue working after you graduate?",
                "answer": "Yes", "learned_from": "Other"}]
    r = resolve("Will you now or in the future require visa sponsorship to continue working after you graduate?",
                type="radio", options=["Yes", "No"], learned=learned)
    assert (r.source, r.value) == ("needs_sponsorship.canada", "No")
    r = resolve("Are you legally authorized to work in Canada for the duration of your university co-op term?",
                type="radio", options=["Yes", "No"])
    assert (r.source, r.value) == ("work_authorization.canada", "Yes")
    assert resolve("Do you hold a valid visa?", type="radio", options=["Yes", "No"]).kind == "manual"
    # a company called Visa isn't a visa question
    assert resolve("Have you ever worked at Visa before?", type="radio", options=["Yes", "No"],
                   job={"company": "Visa", "locations": ["Toronto, ON"]}).value == "No"
    consent = "I consent to receive emails about my application and agree to the privacy policy"
    assert resolve(consent, type="boolean", required=False,
                   learned=[{"label": consent, "answer": True, "learned_from": "X"}]).kind != "fill"


def test_legal_questions_the_profile_answers():
    """One plain yes/no about one country: the only kind filled from the profile."""
    q = "Are you legally authorized to work in the U.S.?"
    assert resolve(q, type="radio", options=YES_NO).value == "No"            # the country it names, not the job's
    r = resolve("Do you now or will you in the future require sponsorship for employment visa status?",
                type="radio", options=YES_NO, job=US_JOB)
    assert (r.source, r.value) == ("needs_sponsorship.us", "Yes")
    # "us" is the employer here, so the country is the job's
    r = resolve("Will you require sponsorship from us to work in this role?", type="radio", options=YES_NO)
    assert (r.source, r.value) == ("needs_sponsorship.canada", "No")
    # the same question in French beside it, examples in brackets, and an N/A choice don't make it a second question
    r = resolve("Êtes-vous légalement autorisé à travailler au Canada? | Are you legally authorized to work in Canada?",
                type="radio", options=["Oui / Yes", "Non / No"])
    assert (r.source, r.value) == ("work_authorization.canada", "Oui / Yes")
    r = resolve("Will you now or in the future require employment sponsorship? (e.g. H-1B, OPT, TN)?",
                type="select", options=["Yes, I will require sponsorship", "No, I will not require sponsorship"], job=US_JOB)
    assert r.value == "Yes, I will require sponsorship"
    assert resolve("Are you legally authorized to work in Canada?", type="select",
                   options=["Select...", "Yes", "No", "N/A"]).value == "Yes"
    # a lead-in that only mentions sponsorship doesn't make the question a legal one
    r = resolve("We are unable to sponsor visas for this role. When do you expect to graduate?", type="date")
    assert r.value == MonthYear(2028, 5)
    assert resolve("If yes, what type of sponsorship would you need?", required=False).kind == "skip"


@pytest.mark.parametrize("label", [
    "Are you authorized to work in the United States without sponsorship?",          # two questions in one
    "Are you legally authorized to work in Canada for any employer?",                # how far it goes
    "Are you legally entitled to work in Canada on a permanent, full-time basis?",
    "Are there any restrictions on your right to work in Canada?",                   # yes means the opposite
    "Are you unable to work in Canada without employer sponsorship?",
    "Do you require a work permit to work in Canada?",                               # which status you hold
    "Are you a citizen or permanent resident of Canada?",
    "Do you live in Canada, and are you legally authorized to work there?",
    "Are you authorized to work in the US or Canada?",                               # two countries
    "Are you legally authorized to work in the United Kingdom?",                     # a country the profile doesn't have
])
def test_legal_questions_asking_more_than_the_profile_knows(label):
    """Found in review (2026-11-08): these were answered from the profile's plain yes/no. A blank is better than a
    wrong answer to a legal question."""
    assert resolve(label, type="radio", options=YES_NO).kind == "manual"


def test_legal_choices_that_say_more_than_yes_or_no():
    q = "Are you legally authorized to work in Canada?"
    for options in (["Yes, with sponsorship", "Yes, without sponsorship", "No"],
                    ["Yes", "No", "I hold a work permit"],
                    ["Yes, I am a citizen or permanent resident", "No"],
                    ["Yes", "No", "Maybe later", "It depends"]):
        r = resolve(q, type="select", options=options)
        assert (r.kind, r.source) == ("manual", "work_authorization.canada"), options
    assert resolve(q, type="multiselect", options=YES_NO).kind == "manual"
    assert resolve("Work authorization in Canada", type="text").kind == "manual"   # not a yes/no question


def test_legal_answers_and_memory():
    """Memory never answers what the profile answers, nor a question whose answer depends on the job's country.
    One that names its country and asks more than the profile knows is answered once."""
    here = "Do you require a work permit to work in the country where this job is located?"
    canada = "Do you require a work permit to work in Canada?"
    plain = "Are you legally authorized to work in Canada?"
    learned = [{"label": here, "answer": "Yes", "learned_from": "Acme"},
               {"label": canada, "answer": "No", "learned_from": "Acme"},
               {"label": plain, "answer": "No", "learned_from": "Acme"}]
    r = resolve(here, type="radio", options=YES_NO, learned=learned)
    assert (r.kind, r.source) == ("manual", "work_eligibility")
    r = resolve(canada, type="radio", options=YES_NO, learned=learned)
    assert (r.source, r.value) == ("learned", "No")
    r = resolve(plain, type="radio", options=YES_NO, learned=learned)
    assert (r.source, r.value) == ("work_authorization.canada", "Yes")


def test_consent_is_never_ticked_for_you():
    r = resolve("I certify that I am at least 18 years of age", type="checkbox", options=["I certify"])
    assert (r.kind, r.source) == ("manual", "consent")
    r = resolve("By selecting Yes, I confirm that I am willing to work on-site", type="select", options=YES_NO)
    assert (r.kind, r.source) == ("manual", "consent")
    assert resolve("Applicant declaration", type="checkbox", options=["I agree"]).kind == "manual"
    said = "I authorize the company to verify the information in this application"
    learned = [{"label": said, "answer": "Yes", "learned_from": "Acme"}]
    assert resolve(said, type="select", options=["Yes"], learned=learned).kind == "manual"
    # the same facts asked as a question are still answered
    assert resolve("Are you at least 18 years of age?", type="radio", options=YES_NO).value == "Yes"
    assert resolve("Are you under 18 years of age?", type="radio", options=YES_NO).value == "No"
    # an opt-in is left alone unless the form insists on an answer
    assert resolve("Sign up for our newsletter", type="checkbox", options=["Yes"], required=False).kind == "skip"
    assert resolve("Join our talent community?", type="select", options=YES_NO).kind == "manual"


def test_someone_elses_details_are_never_yours():
    """Found in review: a reference's email box got your email, and their name was offered to the next form."""
    assert resolve("Reference name", required=False).kind == "skip"
    r = resolve("Emergency contact phone number")
    assert (r.kind, r.source) == ("manual", "other_person")
    assert resolve("Supervisor's email", required=False).kind == "skip"
    learned = [{"label": "Reference name", "answer": "Grace Hopper", "learned_from": "Acme"}]
    assert resolve("Reference name", learned=learned).kind == "manual"
    in_block = lambda label, section, required=True: Resolver(PROFILE, TORONTO_JOB).resolve(
        Field("k", label, "text", required, section=section))
    assert (in_block("Email", "References 1").kind, in_block("Email", "References 1").source) == ("manual", "other_person")
    assert in_block("Phone", "Emergency Contact", required=False).kind == "skip"
    assert resolve("Email").value == "ada@example.com"
    assert resolve("Job reference number", required=False).source != "other_person"


def test_rules_that_fired_on_a_shared_word():
    """Found in review (2026-11-08), each on a stored form."""
    # your profile has your university, not your high school
    assert resolve("Year of High School Graduation", type="select", options=["2024", "2026", "2028"]).kind == "manual"
    # needing an accommodation is not agreeing to come in
    r = resolve("Will you require a reasonable accommodation for our in-person interviews?", type="radio", options=YES_NO)
    assert (r.kind, r.source) == ("manual", "accommodation")
    assert resolve("Please describe any accommodation you need for the hiring process.", required=False).kind == "skip"
    r = resolve("We provide accommodations on request. Are you able to work on-site three days a week?",
                type="radio", options=YES_NO)
    assert (r.source, r.value) == ("preferences.willing_onsite", "Yes")
    # extending a work term is not a phone extension
    assert resolve("Are you available for a possible extension of your co-op term?", type="radio",
                   options=YES_NO).kind == "manual"
    assert resolve("Extension", required=False).kind == "skip"
    # the site's password box, however it is worded, but not a question about passwords
    assert resolve("Create a password").kind == "skip"
    assert resolve("Portfolio or website password", required=False).kind == "skip"
    assert resolve("Which password manager do you use, and why?", type="textarea").kind == "draft"
    # a question that mentions your resume is still a question
    assert resolve("What is one thing that is not on your resume that you are proud of?", type="textarea").kind == "draft"
    assert resolve("Paste your resume", type="textarea", required=False).kind == "skip"
    # a required box with nothing in the profile to put in it is flagged, not skipped
    assert resolve("Skills", type="multiselect").kind == "manual"
    # several choices start with Yes: the first one isn't the answer
    r = resolve("Are you able to commute to one of our offices?", type="radio",
                options=["Yes, New York", "Yes, Chicago", "Yes, either office", "No"])
    assert r.kind == "manual"
