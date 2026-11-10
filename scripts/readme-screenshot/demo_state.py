"""Made-up dashboard state for the README screenshot: no real companies, postings or answers.

Same shape as the server's /api/state response. If the dashboard stops rendering it, compare with
server.py's state handler and add the missing keys here."""
import json
import sys

TODAY = "2026-11-09"


def checks(**kw):
    base = {"unsupported_facts": [], "unsupported_numbers": [], "placeholders": [], "generic_phrases": [], "em_dashes": 0,
            "addresses": [], "unsourced_points": [], "question_type": "project", "fit_closer": "", "self_verdicts": [],
            "meta": [], "stat_stacks": [], "number_count": 1, "many_numbers": False, "repeats": [], "trimmed_closer": ""}
    return {**base, **kw}


def pick(fit, verdict, matched, missing=(), summary="", reqs=(), score=100):
    return {"score": score, "stage1": ["+28 posted 2d ago", "+15 greenhouse form", "+30 Toronto area", "+5 term Summer 2027"],
            "fit": {"eligible": True, "score": 30, "points": [], "blockers": [], "warnings": [], "matched": list(matched),
                    "missing": list(missing), "coverage": 0.8},
            "screen": {"requirements": [{"requirement": r, "kind": k, "status": s, "evidence": e} for r, k, s, e in reqs],
                       "blockers": [], "summary": summary, "fit": fit, "dropped": 0, "downgraded": 0, "verdict": verdict},
            "source": {"kind": "posting", "url": ""}, "verdict": verdict, "reach": False, "screen_points": 30}


def job(n, company, title, place, ats, age, fit, verdict, status="pending", **kw):
    base = {"id": f"demo{n:02d}", "position": n, "status": status, "company": company, "title": title, "locations": [place],
            "ats": ats, "source": "demo", "url": "#", "posting_url": "#", "age_days": age, "first_assigned": TODAY,
            "carried": False, "pick": pick(fit, verdict, ["Python", "Git"]), "readable": ats in ("greenhouse", "lever", "ashby"),
            "questions": [], "plan": [], "prompt": None, "seconds": 0, "how": "picked", "extra": False}
    return {**base, **kw}


Q1 = ("Last winter two friends and I built a tracker for the campus shuttle, because the posted timetable was wrong most "
      "days and people were waiting outside in January. I wrote the part that estimates arrival times from the drivers' "
      "phone GPS. My first version averaged past trips and was off by several minutes whenever traffic changed, so I "
      "switched to updating the estimate from the last three stops. About 400 students were using it by the end of term. "
      "[what you would change if you built it again]")
OPEN = job(
    2, "Northbeam Robotics", "Software Engineering Intern, Perception (Summer 2027)", "Toronto, ON", "greenhouse", 2, 88, "strong",
    pick=pick(88, "strong", ["Python", "PyTorch", "OpenCV", "Docker", "Git"], ["ROS 2"],
              "Your resume covers the Python, PyTorch and camera work this posting asks for. "
              "It also lists ROS 2, which your resume doesn't mention.",
              [("Currently enrolled in a Computer Science or Engineering degree", "required", "met", "Honours Bachelor of Science, Computer Science"),
               ("Experience with Python and a deep learning framework", "required", "met", "trained a PyTorch model to follow a taped line from camera frames"),
               ("Experience with ROS 2", "preferred", "missing", "")]),
    questions=[
        {"qkey": "q-project", "label": "Tell us about a project you're proud of.", "required": True, "limit": "max 150 words",
         "answer": Q1, "edited": False, "prompt": None,
         "checks": checks(placeholders=["[what you would change if you built it again]"],
                          addresses=["build and ship features end to end"],
                          trimmed_closer="That is close to the real-time estimation your team does.")},
    ],
    plan=[{"label": "First Name", "required": True, "kind": "fill", "value": "Sam", "note": "name.first"},
          {"label": "Last Name", "required": True, "kind": "fill", "value": "Rivera", "note": "name.last"},
          {"label": "Email", "required": True, "kind": "fill", "value": "sam.rivera@example.com", "note": "email"},
          {"label": "Resume/CV", "required": True, "kind": "file", "value": "resume.pdf", "note": "files.resume"},
          {"label": "Are you legally authorized to work in Canada?", "required": True, "kind": "fill", "value": "Yes", "note": "work_authorization.canada"},
          {"label": "Tell us about a project you're proud of.", "required": True, "kind": "draft", "value": "", "note": "draft"},
          {"label": "How did you hear about this role?", "required": True, "kind": "manual", "value": "", "note": "no rule - answer it once and it will be learned"}])

JOBS = [
    job(1, "Brightline Transit", "Software Engineering Intern (Summer 2027)", "Toronto, ON", "greenhouse", 1, 91, "strong",
        status="applied", seconds=540),
    OPEN,
    job(3, "Larkspur Health", "Software Developer Co-op (Summer 2027)", "Toronto, ON", "workday", 4, 81, "good"),
    job(4, "Tidewater Payments", "Backend Engineering Intern", "Waterloo, ON", "lever", 3, 79, "good"),
    job(5, "Quillon Labs", "Machine Learning Intern", "Remote in Canada", "ashby", 6, 74, "good"),
    job(6, "Fernhill Systems", "Full-Stack Developer Intern", "Mississauga, ON", "greenhouse", 9, 72, "good"),
    job(7, "Orchard Street AI", "Applied AI Intern (Summer 2027)", "Montreal, QC", "ashby", 5, 63, "stretch"),
    job(8, "Kestrel Analytics", "Data Engineering Intern", "Vancouver, BC", "workday", 12, 70, "good"),
    job(9, "Pinecrest Software", "Software Developer Intern", "Ottawa, ON", "smartrecruiters", 8, 68, "good"),
    job(10, "Hollowbrook Games", "Gameplay Programming Intern", "Toronto, ON", "lever", 2, 58, "stretch"),
]

STATE = {
    "date": TODAY, "today": TODAY, "preview": False,
    "days": [{"date": d, "total": 10, "done": 6, "applied": 5} for d in ("2026-11-06", "2026-11-07", "2026-11-08")]
            + [{"date": TODAY, "total": 10, "done": 1, "applied": 1}],
    "daily_count": 10, "next_run": "2026-11-10T07:30", "jobs": JOBS, "done": 1, "unanswered": 0, "excluded": [],
    "totals": {"applied": 16, "closed": 1, "pending": 9, "skipped": 4}, "backlog": 0, "last_fetch": f"{TODAY}T07:31:02",
    "run": {"running": False, "task": "daily run", "lines": [], "last_finished": f"{TODAY}T07:38:40", "last_error": None},
    "sources": None, "setup": {"missing": [], "has_material": True, "backend": "claude-cli"}, "claude_failing": None,
}

if __name__ == "__main__":
    json.dump(STATE, sys.stdout, ensure_ascii=False)
