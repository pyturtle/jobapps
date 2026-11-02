from jobapps import screen
from jobapps.drafts import check_answer
from jobapps.grounding import placeholders, quote_in, unsupported_numbers

RESUME = """Teaching assistant, intro programming (2025-26): ran weekly labs for 30 students.
Campus food-waste tracker (TypeScript, React, FastAPI, Postgres): 400 students used it in its first month."""
POSTING = """Requirements:
- Experience building web applications with React.
- Familiarity with Kubernetes and cloud infrastructure.
Nice to have: experience mentoring or teaching others."""


def test_quote_in_exact_and_near_exact():
    assert quote_in("ran weekly labs for 30 students", RESUME)
    assert quote_in("Ran weekly labs for 30 students.", RESUME)               # case/punctuation
    assert quote_in("400 students used it in the first month", RESUME)       # one word slipped
    assert not quote_in("led a team of 12 engineers at Google", RESUME)       # invented
    assert not quote_in("React", RESUME)                                      # too short to prove anything


def test_numbers_and_placeholders():
    answer = "My tracker reached 400 students and cut waste by 35%. I ran labs for 30 students. [Add a metric]"
    assert unsupported_numbers(answer, RESUME) == ["35%"]
    assert placeholders(answer) == ["[Add a metric]"]
    assert unsupported_numbers("In 2027 I want to", RESUME) == []


def test_check_answer_flags_unsupported_facts():
    c = check_answer("I ran weekly labs.", ["ran weekly labs for 30 students", "won the ICPC world finals"], RESUME)
    assert c["unsupported_facts"] == ["won the ICPC world finals"]


def test_screen_verify_drops_and_downgrades():
    raw = {
        "requirements": [
            {"requirement": "Experience building web applications with React.", "kind": "required", "status": "met",
             "evidence": "Campus food-waste tracker (TypeScript, React, FastAPI, Postgres)"},
            {"requirement": "Familiarity with Kubernetes and cloud infrastructure.", "kind": "required", "status": "met",
             "evidence": "deployed services on Kubernetes at a startup"},   # not in the resume -> downgraded
            {"requirement": "Five years of Rust in production.", "kind": "required", "status": "missing",
             "evidence": ""},                                              # not in the posting -> dropped
            {"requirement": "experience mentoring or teaching others", "kind": "preferred", "status": "met",
             "evidence": "Teaching assistant, intro programming"},
        ],
        "blockers": [{"quote": "Must be a U.S. citizen", "reason": "citizenship"}],  # not in posting -> ignored
        "summary": "Solid web match.",
    }
    v = screen.verify(raw, POSTING, RESUME)
    statuses = {r["requirement"][:21]: r["status"] for r in v["requirements"]}
    assert statuses["Experience building w"] == "met"
    assert statuses["Familiarity with Kube"] == "missing"
    assert len(v["requirements"]) == 3 and v["dropped"] == 1 and v["downgraded"] == 1
    assert v["blockers"] == []
    assert v["fit"] == round(100 * (1 + 0 + 0.4) / 2.4)
    assert screen.verdict(v["fit"]) == "good"  # 58/100
