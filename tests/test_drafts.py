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


def test_prompt_no_longer_asks_for_the_lines_the_candidate_deletes():
    for asked in ("skim", "85-95%", "if there's room", "44 Next.js", "shipped, used results"):
        assert asked not in drafts.SYSTEM_RULES


def test_shape_checks_catch_closers_verdicts_meta_and_stacked_numbers():
    text = ("I already ship full-stack features at Initech. I built 12 Flask API endpoints for 300 users. "
            "A resume skim might miss that. That's close to what Acme does, which is why this role fits me.")
    c = drafts.check_answer(text, [], SOURCE, [], POSTING, "fit", question="Why should we hire you?", company="Acme")
    assert c["fit_closer"].startswith("That's close to what Acme")
    assert c["self_verdicts"] == ["I already ship"]
    assert c["meta"] == ["resume", "skim"]
    assert c["stat_stacks"] == ["I built 12 Flask API endpoints for 300 users."]
    # a question about results may have numbers, and one about your resume may mention it
    c2 = drafts.check_answer(text, [], SOURCE, [], POSTING, "fit", question="What results did your resume project get?")
    assert c2["stat_stacks"] == [] and c2["meta"] == []
    # plain factual answers aren't held to any of this
    plain = drafts.check_answer("12 years, 300 users. JavaScript is where I'm strongest.", [], SOURCE, [], POSTING,
                                "short_factual", others=["12 years of it"])
    assert plain["stat_stacks"] == [] and plain["self_verdicts"] == [] and plain["repeats"] == []
    # a closer has to tie the work to theirs, and a one-sentence answer has none
    assert drafts.fit_closer("We shipped it in May. Now I write the migration note before the code.") == ""
    assert drafts.fit_closer("That is close to what your team does.") == ""


def test_closing_fit_line_is_cut_without_a_rewrite(monkeypatch):
    q = [("q1", Field("k", "Why this role?", "textarea"), "")]
    text = ("At Initech I built 12 Flask API endpoints on Postgres. The hard part was the auth layer. "
            "That work lines up with what your team builds.")
    monkeypatch.setattr(drafts, "_run", lambda *a: (_ for _ in ()).throw(AssertionError("no rewrite needed")))
    out = drafts._improve("sys", lambda qs: "PROMPT", q, {"q1": answer(text)}, {}, SOURCE, POSTING, "Acme")
    assert out["q1"]["answer"] == "At Initech I built 12 Flask API endpoints on Postgres. The hard part was the auth layer."
    assert out["q1"]["trimmed_closer"] == "That work lines up with what your team builds."
    assert drafts._checks(out["q1"], SOURCE, POSTING)["trimmed_closer"] == out["q1"]["trimmed_closer"]
    # with only one sentence left it isn't cut: it goes back for a rewrite that may only cut
    short = "I built 12 Flask API endpoints on Postgres. That's close to what your team does."
    seen = []
    monkeypatch.setattr(drafts, "_run", lambda system, user, cfg: seen.append(user) or {})
    out = drafts._improve("sys", lambda qs: "PROMPT", q, {"q1": answer(short)}, {}, SOURCE, POSTING, "Acme")
    assert out["q1"]["answer"] == short and "ties the candidate's work" in seen[0] and "Add nothing" in seen[0]


def test_answers_for_one_application_dont_repeat_each_other(monkeypatch):
    first = "At Initech I built 12 Flask API endpoints on Postgres for the billing team."
    second = "I built 12 Flask API endpoints on Postgres, and the tests."
    assert drafts.repeats(second, [first]) == ["12", "I built 12 Flask API endpoints on Postgres"]
    assert drafts.repeats(second, []) == []
    q = [("q1", Field("a", "Why this role?", "textarea"), ""), ("q2", Field("b", "Proudest project?", "textarea"), "")]
    seen = []
    monkeypatch.setattr(drafts, "_run", lambda system, user, cfg: seen.append(user) or {})
    drafts._improve("sys", lambda qs: "PROMPT " + ",".join(x[0] for x in qs), q,
                    {"q1": answer(first), "q2": answer(second)}, {}, SOURCE, POSTING)
    assert seen[0].startswith("PROMPT q2\n") and "from another answer" in seen[0]
    # an answer already written for the application counts too
    seen.clear()
    drafts._improve("sys", lambda qs: "PROMPT " + ",".join(x[0] for x in qs), q[:1], {"q1": answer(second)}, {},
                    SOURCE, POSTING, earlier=[first])
    assert "from another answer" in seen[0]


def test_prompt_lists_answers_already_written_for_the_application():
    existing = {"a": {"question": "Why us?", "answer": "word " * 20, "final_answer": None},
                "b": {"question": "City?", "answer": "Toronto", "final_answer": None},
                "c": {"question": "Proudest project?", "answer": "draft " * 20, "final_answer": "mine " * 20}}
    answered = drafts.answered_elsewhere(existing, {"a"})
    assert answered == [("Proudest project?", ("mine " * 20).strip())]
    job = {"company": "Acme", "title": "Intern", "url": "u"}
    p = drafts.user_prompt(job, POSTING, [("q1", Field("k", "Why this role?", "textarea"), "")], answered)
    assert "<already_answered>" in p and "mine mine" in p
    assert "<already_answered>" not in drafts.user_prompt(job, POSTING, [("q1", Field("k", "Why?", "textarea"), "")])
    # an answer you emptied isn't quoted from its old draft
    existing["c"]["final_answer"] = ""
    assert drafts.answered_elsewhere(existing, {"a"}) == []


def test_a_question_you_answered_yourself_is_not_drafted(tmp_path):
    from jobapps import db
    conn = db.connect(tmp_path / "t.db")
    f = Field("k", "Why this role?", "textarea")
    db.save_final_answer(conn, "a", drafts.qkey(f.label), "My own words.", f.label)
    job = {"id": "a", "company": "Acme", "title": "Intern", "url": "u"}
    assert drafts.draft_job(conn, {}, None, job, POSTING, [f], None)["status"] == "cached"
