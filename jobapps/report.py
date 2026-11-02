"""Render today's batch as one self-contained HTML page (reports/YYYY-MM-DD.html)."""
from __future__ import annotations

from datetime import date
from html import escape
from pathlib import Path

from jobapps import db, picker
from jobapps.config import REPORTS_DIR, Profile, load_learned
from jobapps.drafts import length_limit
from jobapps.forms import Field
from jobapps.rules import Resolver, display, qkey

STATUS_LABEL = {"pending": "To do", "applied": "Applied", "skipped": "Skipped", "closed": "Closed"}
KIND_LABEL = {"fill": "auto", "file": "upload", "draft": "draft", "manual": "you", "skip": "skip"}

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1c1c1a;--muted:#6b6b66;--line:#e4e3de;--accent:#2f5bd3;--ok:#1f8a4c;--warn:#b26a00;
--bad:#c23a32;--chip:#efeee9;--code:#f1f0ec;--on-accent:#fff}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--card:#1d1d1b;--ink:#ecebe6;--muted:#9c9b94;
--line:#2e2d2a;--accent:#7f9cf5;--ok:#4cc382;--warn:#e0a240;--bad:#f07067;--chip:#2a2926;--code:#252422;--on-accent:#10131c}}
:root[data-theme="dark"]{--bg:#141413;--card:#1d1d1b;--ink:#ecebe6;--muted:#9c9b94;--line:#2e2d2a;--accent:#7f9cf5;--ok:#4cc382;
--warn:#e0a240;--bad:#f07067;--chip:#2a2926;--code:#252422;--on-accent:#10131c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:880px;margin:0 auto;padding:28px 16px 64px}
header h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}header p{margin:0;color:var(--muted)}
.progress{display:flex;gap:6px;margin:16px 0 8px}.progress span{flex:1;height:6px;border-radius:3px;background:var(--line)}
.progress span.done{background:var(--ok)}.progress span.skip{background:var(--muted)}
.how{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:18px 0 26px;color:var(--muted)}
.how code,code{background:var(--code);padding:1px 6px;border-radius:5px;font:13px ui-monospace,Consolas,monospace;color:var(--ink)}
article{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:0 0 18px}
article.applied,article.skipped,article.closed{opacity:.6}
.head{display:flex;gap:14px;align-items:flex-start}.num{flex:none;width:30px;height:30px;border-radius:50%;background:var(--ink);
color:var(--card);display:grid;place-items:center;font-weight:700}
.head h2{margin:0;font-size:19px}.head .title{margin:2px 0 0;font-weight:500}.meta{margin:4px 0 0;color:var(--muted);font-size:13.5px}
.status{margin-left:auto;flex:none;font-size:12.5px;font-weight:600;padding:3px 10px;border-radius:99px;background:var(--chip)}
.status.applied{color:var(--ok)}.status.pending{color:var(--accent)}.status.skipped,.status.closed{color:var(--muted)}
.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:14px 0 4px}
a.btn{background:var(--accent);color:var(--on-accent);text-decoration:none;padding:6px 12px;border-radius:7px;font-weight:600;font-size:14px}
button.copy{font:inherit;font-size:12.5px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:6px;
padding:2px 9px;cursor:pointer}button.copy:hover{border-color:var(--accent)}
h3{font-size:14px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:20px 0 8px}
.qa{border-top:1px solid var(--line);padding:12px 0}.qa:first-of-type{border-top:0}
.q{margin:0 0 6px;font-weight:600}.q small{font-weight:400;color:var(--muted);margin-left:6px}
.a{white-space:pre-wrap;margin:0 0 6px;background:var(--code);border-radius:8px;padding:10px 12px;font-size:14.5px}
.a.empty{color:var(--muted);font-style:italic}
details{margin-top:10px}summary{cursor:pointer;color:var(--muted);font-size:14px}
details pre{white-space:pre-wrap;background:var(--code);border-radius:8px;padding:10px 12px;font:12.5px/1.5 ui-monospace,Consolas,monospace;
max-height:420px;overflow:auto}
ul.why{margin:6px 0 0;padding-left:20px;color:var(--muted);font-size:13.5px}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:6px}td{border-top:1px solid var(--line);padding:6px 6px;
vertical-align:top}td.k{width:60px}td.v{color:var(--muted)}
.kind{font-size:11.5px;font-weight:700;padding:1px 7px;border-radius:99px;background:var(--chip)}
.kind.auto,.kind.upload{color:var(--ok)}.kind.draft{color:var(--accent)}.kind.you{color:var(--warn)}.kind.skip{color:var(--muted)}
.note{color:var(--muted);font-size:13.5px}
@media (max-width:560px){.head{flex-wrap:wrap}.status{margin-left:44px}article{padding:16px}}
"""

JS = """
document.addEventListener('click', e => {
  const b = e.target.closest('[data-copy]'); if (!b) return;
  const text = document.getElementById(b.dataset.copy).innerText;
  const done = () => { b.textContent = 'Copied'; setTimeout(() => { b.textContent = 'Copy'; }, 1200); };
  const fallback = () => { const t = document.createElement('textarea'); t.value = text; document.body.append(t); t.select();
    document.execCommand('copy'); t.remove(); done(); };
  (navigator.clipboard ? navigator.clipboard.writeText(text).then(done) : Promise.reject()).catch(fallback);
});
"""


def _copy(target: str) -> str:
    return f'<button class="copy" data-copy="{target}">Copy</button>'


def _job_html(conn, pos: int, row: dict, job: dict, form: dict | None, answers: dict, profile: Profile,
              cfg: dict, today: date, learned: list) -> str:
    e = escape
    status = row["status"]
    pick = db.get_pick(conn, job["id"])
    verdict = picker.evaluate(job, cfg, today)
    if pick:
        verdict.score = pick["score"]
        verdict.reasons = pick["stage1"] + pick["fit"]["points"] + (
            [f"+{pick['screen_points']} verified fit {pick['screen']['fit']}/100 (Claude screen)"] if pick.get("screen") else [])
    if form and form.get("ats") == "page":
        form = None
    age = picker.age_days(job, today)
    meta = " &middot; ".join(x for x in (
        e(", ".join(job["locations"][:3]) + (" +more" if len(job["locations"]) > 3 else "")) if job["locations"] else "",
        f"posted {'today' if age == 0 else f'{age}d ago'}" if age < 999 else "",
        f"{e(job['ats'])} form",
    ) if x)
    out = [f'<article class="{e(status)}" id="job-{pos}">',
           '<div class="head">', f'<span class="num">{pos}</span>',
           f'<div><h2>{e(job["company"])}</h2><p class="title">{e(job["title"])}</p><p class="meta">{meta}</p></div>',
           f'<span class="status {e(status)}">{STATUS_LABEL.get(status, e(status))}</span>', '</div>',
           '<div class="actions">',
           f'<a class="btn" href="{e((form or {}).get("apply_url") or job["url"])}" target="_blank" rel="noopener">Open posting</a>',
           f'<code id="cmd-{pos}">ja apply {pos}</code>{_copy(f"cmd-{pos}")}', '</div>']
    if verdict.reasons:
        out.append(f'<details><summary>Why this one &middot; score {verdict.score}</summary><ul class="why">'
                   + "".join(f"<li>{e(r)}</li>" for r in verdict.reasons) + "</ul></details>")

    fields = [Field.from_dict(f) for f in (form or {}).get("fields", [])]
    drafts = {k: v["final_answer"] or v["answer"] for k, v in answers.items() if v["final_answer"] or v["answer"]}
    resolver = Resolver(profile, job, learned, drafts)
    plan = [(f, resolver.resolve(f)) for f in fields]
    questions = [(f, r) for f, r in plan if r.kind == "draft"]

    if form is None:
        out.append('<p class="note">This ATS can\'t be read ahead of time. Questions are read and drafted when you run '
                   f'<code>ja apply {pos}</code>.</p>')
    if questions:
        out.append(f"<h3>Questions to answer &middot; {len(questions)}</h3>")
        prompt_file = None
        for i, (f, r) in enumerate(questions, 1):
            meta_q = " &middot; ".join(x for x in (e(length_limit(f)), "required" if f.required else "optional") if x)
            aid = f"a-{pos}-{i}"
            out.append(f'<div class="qa"><p class="q">{e(f.label)}<small>{meta_q}</small></p>')
            if r.value:
                out.append(f'<p class="a" id="{aid}">{e(r.value)}</p>{_copy(aid)}')
            else:
                out.append(f'<p class="a empty" id="{aid}">No draft yet. Copy the prompt below into Claude, '
                           'or set up a drafting backend (see README).</p>')
            out.append("</div>")
            prompt_file = prompt_file or (answers.get(qkey(f.label)) or {}).get("prompt_file")
        if prompt_file and Path(prompt_file).exists():
            text = Path(prompt_file).read_text(encoding="utf-8")
            user_part = text.split("# User prompt", 1)[-1].strip()
            pid = f"p-{pos}"
            out.append(f'<details><summary>Prompt used (system prompt + background are in '
                       f'{e(Path(prompt_file).name)})</summary>{_copy(pid)}<pre id="{pid}">{e(user_part)}</pre></details>')
    elif form is not None:
        out.append('<p class="note">No free-text questions on this form.</p>')

    if plan:
        rows = []
        for f, r in plan:
            kind = KIND_LABEL[r.kind]
            value = {"draft": "drafted answer", "skip": r.note or "left blank", "manual": r.note or "you decide"}.get(
                r.kind, display(r.value) if r.kind == "fill" else Path(str(r.value)).name)
            rows.append(f'<tr><td class="k"><span class="kind {kind}">{kind}</span></td><td>{e(f.label[:140])}'
                        f'{" *" if f.required else ""}</td><td class="v">{e(str(value))}</td></tr>')
        n_auto = sum(1 for _, r in plan if r.kind in ("fill", "file"))
        n_you = sum(1 for _, r in plan if r.kind == "manual")
        out.append(f'<details><summary>Autofill plan &middot; {n_auto} automatic, {len(questions)} drafted, '
                   f'{n_you} for you</summary><table>{"".join(rows)}</table></details>')
    out.append("</article>")
    return "\n".join(out)


def build(conn, cfg: dict, profile: Profile, today: date) -> Path:
    rows = db.batch(conn, today.isoformat())
    learned = load_learned()
    jobs_html = []
    for row in rows:
        job = db.get_listing(conn, row["job_id"])
        if job is None:
            continue
        jobs_html.append(_job_html(conn, row["position"], row, job, db.get_form(conn, job["id"]),
                                   db.answers_for(conn, job["id"]), profile, cfg, today, learned))
    done = sum(1 for r in rows if r["status"] in ("applied", "skipped", "closed"))
    bar = "".join(f'<span class="{"done" if r["status"] == "applied" else "skip" if r["status"] != "pending" else ""}"></span>'
                  for r in rows)
    totals = db.counts(conn)
    missing = profile.missing()
    warn = (f'<p class="note">Profile incomplete: {escape(", ".join(missing))}. Fields that need these are left for you.</p>'
            if missing else "")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Apps {today.isoformat()}</title><style>{CSS}</style></head>
<body><main>
<header><h1>Today's {len(rows)} applications</h1>
<p>{today.strftime('%A, %B %d, %Y').replace(' 0', ' ')} &middot; {done} of {len(rows)} done &middot;
{totals.get('applied', 0)} applied all time</p></header>
<div class="progress">{bar}</div>
<div class="how">Run <code>ja apply</code> to open each one in the browser, already filled in.
Blue outlines are drafted answers to read and edit; red ones need you. You submit; it never does.
Mark one done by hand with <code>ja done 2</code>, or drop it with <code>ja skip 2</code>.{warn}</div>
{"".join(jobs_html) or '<p class="note">No jobs in today\'s batch. Check config.yaml filters and run <code>ja today</code>.</p>'}
</main><script>{JS}</script></body></html>"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{today.isoformat()}.html"
    path.write_text(html, encoding="utf-8")
    return path
