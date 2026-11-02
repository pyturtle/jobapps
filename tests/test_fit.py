from jobapps.config import load_config
from jobapps.fit import Candidate, evaluate, sections, skills_in

CFG = load_config()
JOB = {"title": "Software Engineer Intern", "company": "Acme", "locations": ["Austin, TX"]}


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
