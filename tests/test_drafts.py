"""Answer quality checks and the single automatic rewrite (no model calls: _run is faked)."""
from jobapps import drafts
from jobapps.forms import Field

SOURCE = "- Name: Ada\n## Resume\nSoftware Developer at Initech. Built 12 Flask API endpoints on Postgres."
POSTING = "You will build internal tools in TypeScript. Experience with Postgres is an asset. We ship weekly."


def answer(text, facts=("Built 12 Flask API endpoints on Postgres",), points=("build internal tools in TypeScript",)):
    return {"answer": text, "facts_used": list(facts), "posting_points": list(points), "question_type": "why_role"}


def test_checks_flag_generic_wording_and_unsourced_company_claims():
    c = drafts.check_answer("I'm passionate about leveraging Postgres — not only fast but also reliable.",
                            [], SOURCE, ["we ship weekly", "we are the leading AI company"], POSTING, "fit")
    assert c["generic_phrases"][:2] == ["passionate", "leveraging"] and any("not only" in g for g in c["generic_phrases"])
    assert c["em_dashes"] == 1
    assert c["addresses"] == ["we ship weekly"] and c["unsourced_points"] == ["we are the leading AI company"]
    clean = drafts.check_answer("At Initech I built 12 Flask API endpoints on Postgres.", [], SOURCE, [], POSTING)
    assert clean["generic_phrases"] == [] and clean["em_dashes"] == 0


def test_problems_cover_length_facts_and_style():
    ok = answer("At Initech I built 12 Flask API endpoints on Postgres, so internal tools are familiar ground.")
    assert drafts.problems(ok, "max 40 words", SOURCE, POSTING) == []
    long = answer("word " * 50)
    assert "over the limit of 40" in drafts.problems(long, "max 40 words", SOURCE, POSTING)[0]
    made_up = answer("I led a team of 12 at Google.", facts=["led a team of 12 at Google"])
    p = drafts.problems(made_up, "", SOURCE, POSTING)
    assert any("not in the candidate background" in x for x in p) and any("12" in x for x in p)


def test_rewrite_is_kept_only_when_better(monkeypatch):
    q = [("q1", Field("k", "Why this role?", "textarea"), "")]
    bad = {"q1": answer("I am passionate about this role and eager to leverage my skills.")}
    better = {"q1": answer("At Initech I built 12 Flask API endpoints on Postgres; your internal tools are that kind of work.")}
    seen = []
    monkeypatch.setattr(drafts, "_run", lambda system, user, cfg: seen.append(user) or better)
    out = drafts._improve("sys", lambda qs: "PROMPT", q, dict(bad), {}, SOURCE, POSTING)
    assert out["q1"] is better["q1"] and "stock phrases" in seen[0] and "<previous_draft>" in seen[0]
    worse = {"q1": answer("I am passionate, thrilled and eager — truly.", facts=["made up fact"])}
    monkeypatch.setattr(drafts, "_run", lambda system, user, cfg: worse)
    assert drafts._improve("sys", lambda qs: "PROMPT", q, dict(bad), {}, SOURCE, POSTING)["q1"] is bad["q1"]
    monkeypatch.setattr(drafts, "_run", lambda *a: (_ for _ in ()).throw(AssertionError("no rewrite needed")))
    assert drafts._improve("sys", lambda qs: "PROMPT", q, dict(better), {}, SOURCE, POSTING) == better
