from datetime import date

from jobapps import fit
from jobapps.config import load_config
from jobapps.fit import Candidate, deadline, sections, skills_in, years_asked

CFG = load_config()
JOB = {"title": "Software Engineer Intern", "company": "Acme", "locations": ["Austin, TX"]}
TODAY = date(2026, 11, 9)


def evaluate(job, description, candidate, cfg, today=TODAY):
    """fit.evaluate on a fixed day, so no test depends on when it runs."""
    return fit.evaluate(job, description, candidate, cfg, today)


def cand(**kw):
    base = dict(skills={"Python", "React", "PostgreSQL", "Git", "Docker"}, has_material=True, grad_year=2028, gpa=3.6,
                citizenship=["canada"], needs_sponsorship={"us": True, "canada": False})
    base.update(kw)
    return Candidate(**base)


def test_skills_vocab():
    text = "Experience with Python, Java, C/C++ and Go, React or Vue, PostgreSQL, AWS, Kubernetes. JavaScript a plus."
    found = skills_in(text)
    assert {"Python", "Java", "C++", "Go", "React", "Vue", "PostgreSQL", "AWS", "Kubernetes", "JavaScript"} <= found
    assert "R" not in skills_in("Requirements: Research and Reporting")
    assert "Go" not in skills_in("Go to market strategy and Go big")


def test_sections_split_required_and_preferred():
    desc = "About us\nWe sell widgets.\nRequirements:\n- Python and SQL\nNice to have:\n- Kubernetes\nBenefits\n- Free lunch"
    kinds = dict((t, k) for k, t in sections(desc))
    assert kinds["- Python and SQL"] == "required"
    assert kinds["- Kubernetes"] == "preferred"
    assert kinds["- Free lunch"] == "ignore"


def test_skill_coverage_scores():
    desc = "Requirements:\n- Python, React and PostgreSQL\n- Kubernetes\nNice to have:\n- Docker"
    r = evaluate(JOB, desc, cand(), CFG)
    assert r.eligible
    assert r.coverage == 0.75
    assert "Kubernetes" in r.missing and "Python" in r.matched
    assert any("bonus skills: Docker" in why for _, why in r.points)


def test_blockers_quote_the_posting():
    cases = [
        ("Currently pursuing a PhD in Computer Science.", "graduate students only"),
        ("You must be enrolled in a Master's program.", "graduate students only"),
        ("3+ years of professional software engineering experience.", "3+ years"),
        ("Must be graduating between December 2026 and June 2027.", "graduation window"),
        ("Only for students with Winter 2027 graduation dates.", "graduation window"),
        ("You must be returning to school in Fall 2029 after the internship.", "return to school"),
        ("Applicants must be U.S. citizens due to ITAR.", "citizenship"),
        ("We are unable to sponsor visas for this role. Must be authorized to work in the US.", "sponsorship"),
        ("Minimum GPA of 3.8 required.", "GPA floor"),
    ]
    for sentence, reason in cases:
        r = evaluate(JOB, f"Requirements:\n- {sentence}", cand(), CFG)
        assert not r.eligible, sentence
        assert reason.lower() in r.blockers[0]["reason"].lower(), (sentence, r.blockers)
        assert r.blockers[0]["quote"].startswith(sentence[:20])


def test_not_blockers():
    ok = [
        "Pursuing a Bachelor's, Master's or PhD in Computer Science.",
        "Master's students preferred.",
        "Graduation date in Fall 2027, Spring 2028, Summer 2028, or Fall 2028.",
        "Graduating no earlier than December 2027.",
        "1+ years of programming experience through coursework or projects.",
        "2+ years of experience with Python (school projects count).",
        "Education: Current enrollment in an undergraduate or graduate degree program, returning to school in Fall 2027.",
        "This internship is only for undergraduate students with Spring 2028 graduation dates.",
        "We hire undergraduate and graduate students from 2025 onward cohorts.",
    ]
    for sentence in ok:
        r = evaluate(JOB, f"Requirements:\n- {sentence}", cand(), CFG)
        assert r.eligible, (sentence, r.blockers)


def test_unknown_profile_values_warn_instead_of_block():
    r = evaluate(JOB, "Requirements:\n- Must be graduating in 2027.", cand(grad_year=None), CFG)
    assert r.eligible and r.warnings
    r = evaluate(JOB, "Requirements:\n- We do not sponsor visas.", cand(needs_sponsorship={"us": None}), CFG)
    assert r.eligible and r.warnings
    r = evaluate(JOB, "Requirements:\n- Must be a U.S. citizen.", cand(citizenship=[]), CFG)
    assert r.eligible and r.warnings


def test_senior_titles_blocked():
    assert not evaluate({**JOB, "title": "Senior Software Engineer"}, "x", cand(), CFG).eligible
    assert evaluate({**JOB, "title": "Team Lead Intern"}, "x", cand(), CFG).eligible


def test_canadian_postings_are_not_ruled_out_by_us_lookalikes():
    """Found in review (2026-11-07): "must"/"business" read as "US", university years read as work experience, and a
    Canadian government clearance treated as a US one."""
    toronto = {**JOB, "locations": ["Toronto, ON, Canada"]}
    for sentence in ("Applicants must be able to work without sponsorship.",
                     "We are unable to sponsor visas; this is a business requirement, contact us with questions."):
        assert evaluate(toronto, f"Requirements:\n- {sentence}", cand(), CFG).eligible, sentence
    assert not evaluate(JOB, "Requirements:\n- We are unable to sponsor visas in the US.", cand(), CFG).eligible
    assert evaluate(toronto, "Requirements:\n- Must have completed at least 3 years of university and have experience "
                             "with Python.", cand(), CFG).eligible
    clearance = "Requirements:\n- Must be eligible to obtain a Government of Canada security clearance."
    assert evaluate(toronto, clearance, cand(citizenship=["canadian"]), CFG).eligible
    assert not evaluate(toronto, clearance, cand(citizenship=["india"]), CFG).eligible
    assert not evaluate(JOB, "Requirements:\n- Must be a U.S. citizen with an active secret clearance.", cand(), CFG).eligible


def test_a_date_before_the_deadline_words_is_not_the_deadline():
    """A careers-site layout that flattens to lines: the day it was posted, the heading, then the deadline on the
    next line. The posting date used to be read as a deadline that had passed."""
    layout = ("Posting Date:\n2026-10-26 Application Deadline:\n2026-11-20 Note : Applications will be accepted until "
              "11:59 PM on the day prior to the application deadline date above\n")
    assert evaluate(JOB, layout, cand(), CFG).eligible
    passed = evaluate(JOB, layout.replace("2026-11-20", "2026-11-02"), cand(), CFG)
    assert not passed.eligible and "Nov 02, 2026" in passed.blockers[0]["reason"]
    assert deadline("The application deadline is 5 p.m. Eastern Standard Time on Nov. 15, 2026.") == date(2026, 11, 15)
    whole = fit._sentences("About the role\nThe application deadline is 5 p.m. Eastern on Nov. 15, 2026. Apply early.")
    assert "The application deadline is 5 p.m. Eastern on Nov. 15, 2026." in whole     # not cut at "Nov."
    assert deadline("Deadline extended from Oct 1, 2026 to Oct 15, 2026") == date(2026, 10, 15)
    assert deadline("Able to meet tight deadlines and available from May 3, 2027") is None
    assert deadline("Posted 2026-10-01. Application deadline: Sunday, October 4th at 11:59pm") is None  # no year, no guess


def test_years_of_experience_count_only_when_asked_of_you():
    for sentence in ("Acme is an investment firm with over 22 years of experience serving institutional clients.",
                     "With more than 30 years of experience, Globex builds software for banks.",
                     "Must be at least 18 years old and have experience working in a team.",
                     "Less than 3 years of professional experience.",
                     "No more than 3 years of full-time work experience.",
                     "As a full-time employee we would require 3-5 years of experience, however interns learn on the job."):
        assert years_asked(sentence) is None, sentence
        assert evaluate(JOB, f"Requirements:\n- {sentence}", cand(), CFG).eligible, sentence
    assert years_asked("3+ years of professional software engineering experience.") == 3
    assert years_asked("Our company is looking for someone with 4 years of experience in Go.") == 4
    assert years_asked("Candidates with over 5 years of experience in distributed systems.") == 5


def test_degree_and_graduation_wording():
    ok = ["Currently enrolled in a BASc, MASc or PhD program.",
          "Pursuing a B.Eng, M.Eng or PhD in Computer Engineering.",
          "Must have an anticipated graduation date by May 2028 (Masters: May 2028-2029).",
          "You must not graduate before December 2027.",
          "Students can't graduate before May 2028.",
          "Graduating not later than December 2028."]
    for sentence in ok:
        r = evaluate(JOB, f"Requirements:\n- {sentence}", cand(), CFG)
        assert r.eligible, (sentence, r.blockers)
    for sentence in ("You must not graduate after June 2027.", "Graduating not later than December 2027."):
        r = evaluate(JOB, f"Requirements:\n- {sentence}", cand(), CFG)
        assert not r.eligible and "graduation window" in r.blockers[0]["reason"], sentence


def test_no_sponsorship_in_the_ways_postings_say_it():
    no = ["Acme is not currently sponsoring work permit applications.",
          "Visa sponsorship or support is unavailable for this role.",
          "This position is not eligible for employment-based immigration sponsorship.",
          "Acme does not provide employment visa sponsorship.",
          "Work visas will not be sponsored for this internship.",
          "Candidates who now or in the future require company sponsorship for a work visa are not eligible to apply.",
          "Acme is not currently hiring individuals who now or in the future require sponsorship for employment visa status.",
          "Must be legally authorized to work in the United States and will not require visa sponsorship now or in the future.",
          "Must be eligible to work in the United States without company sponsorship, now or in the future.",
          "Must possess unrestricted work authorization.",
          "Authorized to work in the United States for any employer."]
    for sentence in no:
        r = evaluate(JOB, f"Requirements:\n- {sentence}", cand(), CFG)
        assert not r.eligible and "sponsorship" in r.blockers[0]["reason"], sentence
    fine = ["We sponsor visas for placements of three months or longer.",
            "We do sponsor visas!",
            "You will present your work to executive sponsors.",
            "Interns join company sponsored events through the term.",
            "Selected candidates will be sponsored for the required background check.",
            "Will you now or in the future require sponsorship for employment visa status?",
            "Are you able to work in the United States without sponsorship?",
            "We aren't able to successfully sponsor visas for every role and every candidate."]
    for sentence in fine:
        r = evaluate(JOB, f"Requirements:\n- {sentence}", cand(), CFG)
        assert r.eligible, (sentence, r.blockers)
    unsure = evaluate(JOB, "Requirements:\n- We are not able to sponsor visas for every role.", cand(), CFG)
    assert unsure.eligible and "may not sponsor" in unsure.warnings[0]["reason"]
    # a line about the company's office in another country says nothing about this one
    london = evaluate(JOB, "Requirements:\n- You hold a permanent right to work in the UK.", cand(), CFG)
    assert london.eligible and not london.warnings
    # in Canada, for someone who needs no sponsorship there, none of it matters
    toronto = {**JOB, "locations": ["Toronto, ON, Canada"]}
    assert all(evaluate(toronto, f"Requirements:\n- {s}".replace("United States", "Canada"), cand(), CFG).eligible for s in no)


def test_citizenship_is_a_blocker_only_when_the_posting_requires_it():
    fine = ["U.S. citizenship is preferred.",
            "An active security clearance is beneficial but not required.",
            "U.S. citizenship is not required for this role.",
            "Clearance required for start: No",
            "Pay depends on experience, education, security clearances and market conditions.",
            "Learn more about the background check process for security clearances."]
    for sentence in fine:
        r = evaluate(JOB, f"About the role\n{sentence}", cand(), CFG)
        assert r.eligible, (sentence, r.blockers)
    # a heading says nothing until its value is read
    assert evaluate(JOB, "Security Clearance Requirement:\nNone for this internship.", cand(), CFG).eligible
    assert not evaluate(JOB, "Security Clearance Requirement:\nMust be a U.S. citizen.", cand(), CFG).eligible
    for sentence in ("Ability to obtain and maintain a Secret clearance.", "U.S. Person required.",
                     "Citizenship and clearance: Must be a U.S. citizen."):
        assert not evaluate(JOB, f"About the role\n{sentence}", cand(), CFG).eligible, sentence
    listed = evaluate(JOB, "About the role\nExport rules require citizenship of a country on the authorized list, which "
                           "includes Canada, or that you be a U.S. Person.", cand(), CFG)
    assert listed.eligible and "names Canada" in listed.warnings[0]["reason"]   # yours to read, not ruled out
    toronto = {**JOB, "locations": ["Toronto, ON, Canada"]}
    abroad = cand(citizenship=["india"])
    assert evaluate(toronto, "About the role\nCanadian citizens or permanent residents preferred.", abroad, CFG).eligible
    assert not evaluate(toronto, "About the role\nMust be a Canadian citizen or permanent resident.", abroad, CFG).eligible
