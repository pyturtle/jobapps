"""Workday's My Information / My Experience pages: repeated sections, split dates, search boxes."""
from datetime import date
from pathlib import Path

from jobapps import plan
from jobapps.config import Profile
from jobapps.rules import MonthYear, Resolver, month_year, section_of

PROFILE = Profile({
    "name": {"first": "Ada", "last": "Lovelace"},
    "email": "ada@example.com", "phone": "416-555-0100", "location": "Toronto, Ontario, Canada",
    "address": {"city": "Toronto", "region": "Ontario", "country": "Canada", "line2": ""},
    "links": {"linkedin": "https://linkedin.com/in/ada", "github": "https://github.com/ada", "website": ""},
    "education": {"school": "University of Toronto", "degree": ["Bachelor of Science", "Bachelor's"],
                  "major": "Computer Science", "gpa": "3.6", "start_month": "September", "start_year": 2024,
                  "graduation_month": "May", "graduation_year": 2028},
    "experience": {"history": [
        {"title": "Software Developer", "company": "Initech", "location": "", "start": "March 2025", "end": "present",
         "description": "Built things."},
        {"title": "Front-end Developer", "company": "Globex", "location": "Oakville, ON",
         "start": "October 2021", "end": "August 2023", "description": "Shipped an app."},
    ]},
    "skills": {"list": ["Python", "C", "AWS", "React"]},
    "preferences": {"heard_about": ["Job board", "LinkedIn"]},
}, root=Path("."))
JOB = {"company": "Trimble", "locations": ["Richmond Hill, Canada"]}


def act(label, type="text", section="", value=None, required=True, **extra):
    f = {"id": "1", "label": label, "type": type, "required": required, "section": section, "value": value, **extra}
    return plan.action(f, Resolver(PROFILE, JOB).resolve(plan.to_field(f)))


def test_sections_and_dates_parse():
    assert section_of("Work Experience 2") == ("work", 2)
    assert section_of("Education 1") == ("education", 1)
    assert section_of("Websites 1") == ("website", 1)
    assert section_of("Work Experience") is None  # not a repeated block
    assert month_year("June 2026") == MonthYear(2026, 6)
    assert month_year("Sept 2024") == MonthYear(2024, 9)
    assert month_year("2022-12") == MonthYear(2022, 12)
    assert month_year("present") is None


def test_work_experience_blocks():
    assert act("Job Title", section="Work Experience 1")["text"] == "Software Developer"
    assert act("Company", section="Work Experience 2")["text"] == "Globex"
    assert act("Location", section="Work Experience 2")["text"] == "Oakville, ON"
    assert act("Location", section="Work Experience 1")["state"] == "skip"           # blank: left alone
    assert act("Role Description", "textarea", "Work Experience 2")["text"] == "Shipped an app."
    current = act("I currently work here", "boolean", "Work Experience 1")
    assert current["op"] == "check" and current["on"] is True
    assert act("I currently work here", "boolean", "Work Experience 2", value=True) == \
        {**act("I currently work here", "boolean", "Work Experience 2", value=True), "op": "check", "on": False}
    start = act("From", "date", "Work Experience 2", parts=["month", "year"])
    assert start["op"] == "dateparts" and start["parts"] == {"month": 10, "year": 2021}
    assert act("To", "date", "Work Experience 1", parts=["month", "year"])["state"] == "skip"  # still there
    assert act("Job Title", section="Work Experience 3")["state"] == "skip"           # no third job


def test_workday_resume_parser_guesses_are_replaced():
    a = act("Job Title", section="Work Experience 1", value="Initech / Payroll Software Developer")
    assert a["op"] == "text" and a["text"] == "Software Developer"
    assert act("Job Title", section="Work Experience 1", value="Software Developer")["op"] is None
    # outside a repeated block an existing value is still yours and stays
    assert act("Email Address", value="me@else.com")["note"] == "already filled"


def test_education_block():
    assert act("School or University", "multiselect", "Education 1")["items"] == [["University of Toronto"]]
    assert "Bachelor's" in act("Degree", "dropdown", "Education 1")["candidates"]
    assert act("Field of Study", "multiselect", "Education 1")["items"] == [["Computer Science"]]
    assert act("Overall Result (GPA)", section="Education 1")["text"] == "3.6"
    assert act("From", "date", "Education 1", parts=["year"])["parts"] == {"year": 2024}
    assert act("To (Actual or Expected)", "date", "Education 1", parts=["year"])["parts"] == {"year": 2028}
    assert act("From", "date", "Education 1", parts=["month", "year"])["parts"] == {"month": 9, "year": 2024}


def test_websites_skills_and_my_information():
    assert act("URL", section="Websites 1")["text"] == "https://linkedin.com/in/ada"
    assert act("URL", section="Websites 2")["text"] == "https://github.com/ada"
    assert act("URL", section="Websites 3")["state"] == "skip"
    skills = act("Type to Add Skills", "multiselect", value=["Python (Programming Language)"])
    assert skills["items"] == [["C"], ["AWS"], ["React"]] and skills["exact"]
    assert act("Phone Device Type", "dropdown")["candidates"][0] == "Mobile"
    assert act("Country Phone Code", "multiselect")["items"] == [["Canada"]]
    assert act("Phone Extension")["state"] == "skip"
    assert act("Address Line 2", required=False)["state"] == "skip"
    assert act("Phone Number")["text"] == "416-555-0100"
    heard = act("How Did You Hear About Us?", "multiselect")
    assert heard["items"] == [["Job board", "LinkedIn"]] and heard["pick_first"]
    assert act("I have a preferred name", "boolean")["op"] is None
    signed = act("Date", "date", parts=["month", "day", "year"])
    assert signed["parts"] == {"month": date.today().month, "day": date.today().day, "year": date.today().year}


def test_skill_matching_is_strict():
    opts = ["AWS Lambda", "Amazon Web Services (AWS)", "C++ (Programming Language)", "C (Programming Language)",
            "React.js", "Reactive Programming"]
    assert plan.choose_exact("AWS", opts) == "Amazon Web Services (AWS)"
    assert plan.choose_exact("C", opts) == "C (Programming Language)"
    assert plan.choose_exact("C++", opts) == "C++ (Programming Language)"
    assert plan.choose_exact("React", opts) == "React.js"
    assert plan.choose_exact("Rust", opts) is None


def test_long_yes_no_options_and_graduation_question():
    from jobapps.rules import choose_option
    opts = ["I am authorized to work in Canada", "I am not authorized to work in Canada", "I prefer not to answer"]
    assert choose_option(True, opts) == opts[0]
    assert choose_option(False, opts) == opts[1]
    assert choose_option("decline", opts) == opts[2]
    sponsor = ["I will require sponsorship", "I will not require sponsorship"]
    assert choose_option(False, sponsor) == sponsor[1]
    assert act("How long ago was your date of graduation?", "dropdown")["candidates"][0] == "Not yet graduated"
