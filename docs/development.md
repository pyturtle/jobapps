# Development

## Setting up

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

The suite has 174 tests. Two files drive a real browser and are skipped when Playwright has no browser.
`tests/test_scan_workday.py` scans `tests/fixtures/workday_mock.html`. `tests/test_extension_browser.py`
loads the extension zip the server builds into Chromium, on made-up job-board pages against a stand-in
server, and takes about 40 seconds. The rest runs in a few seconds. To get the browser:

```powershell
.venv\Scripts\python -m playwright install chromium
```

The tests never open your personal files or the real database. Four test files (`test_fit`,
`test_picker`, `test_pipeline` and `test_research`) do read the shipped `config.yaml`, so a change
to a filter or a score there can fail a test that has nothing to do with your code.

There is no build step. The dashboard and the extension are plain JavaScript files that the server
serves as they are. The extension zip is built on each download. It carries a fingerprint of
`jobapps/extension/` and `jobapps/filler.js`, which is how an installed copy knows it is out of date.

## Layout

```
jobapps/
  cli.py         the ja commands
  server.py      dashboard, API and the daily scheduler
  pipeline.py    the daily run: fetch, rank, check fit, screen, pick
  sources.py     readers for the public lists and employer boards
  picker.py      filtering and scoring from config.yaml
  fit.py         rule-outs and skill scoring, no model
  screen.py      the Claude screen and the check of its quotes
  research.py    finds the text of postings that can't be read directly
  discover.py    optional web search for postings the lists missed
  forms.py       reads postings and application forms from job-board APIs
  drafts.py      the drafting prompt and the checks on each answer
  grounding.py   decides whether a quote is in a text
  rules.py       turns a form question into a value from your profile
  plan.py        turns the fields the extension found into fill actions
  filler.js      finds and fills fields on a page; shared by the extension and ja apply
  filler.py      ja apply: fills forms in a browser on the PC with Playwright
  records.py     the Postings and History pages
  report.py      the daily HTML report
  llm.py         the only module that calls Claude
  net.py         HTTP helpers with retries
  db.py          the SQLite schema and queries
  config.py      paths, defaults, and loading config.yaml and profile.yaml
  web/           the dashboard
  extension/     the Chrome extension
scripts/         the Windows installer, the Mac extension updater, the README screenshot
tests/           the tests and their fixtures
```

Data flows one way. `sources` writes postings to the database, `pipeline` picks from them, `forms`
and `drafts` prepare each pick, and `server` shows the result. The extension holds no logic about
values: it sends the fields it found to the server, and `plan` and `rules` answer.

## Rules to keep

- Model output is only used after a check in code. A requirement, a piece of evidence or a fact
  counts when `grounding.py` finds its quote in the source text.
- Work authorization and sponsorship questions are answered from the profile, and only when they
  ask a plain yes or no about one country. Anything else is left for the user. Never add a default
  for them, and never answer them from a remembered answer.
- A consent box or declaration is never ticked.
- A wrong rule-out is worse than a missed one: a posting ruled out is never shown again. A fit rule
  should fire only on wording it is sure of. After loosening a rule, `ja recheck` puts back the
  postings it had stopped.
- Nothing presses a Submit button.
- The `events` table is a permanent log. Add rows; don't delete or rewrite them.
- Ranking and fit scoring use no model. The same postings, config and date must give the same
  scores.

## Common changes

### Add an employer board

Add a line under `boards` in `config.yaml`:

```yaml
- {company: "Example Co", ats: greenhouse, id: "exampleco"}
```

`ats` is one of `greenhouse`, `lever`, `ashby`, `workday` or `smartrecruiters`. The `id` is the
board's name in its public URL. For Workday it is `tenant.wdN/site`, as in `cibc.wd3/search`. Run
`.\ja fetch` afterwards. The "employer boards" line of its output names any board that failed.

### Add a public list

A list in the same JSON format as SimplifyJobs needs only a `name` and `url` under `sources`. For a
new format, write a `parse_*` function in `jobapps/sources.py` that returns the same dictionaries as
`parse_francklin`, add a `type` for it in `_load_source`, and add a test in `tests/test_sources.py`
with a small sample of the file.

### Add a form rule

Rules live in the `RULES` list in `jobapps/rules.py`. Each is a name, a regular expression matched
against the normalised question, a handler, and whether it may apply to a large text box. Order
matters, because the first match wins.

Add a test in `tests/test_rules.py` that uses the question as the site words it:

```python
assert resolve("Autofill from resume", type="file", required=False).kind == "skip"
```

If the question only matters to you, a `custom_answers` rule in `profile.yaml` does the same job
without a code change. See [Configuration](configuration.md#custom_answers).

### Change the drafting prompt

The prompt is `SYSTEM_RULES` in `jobapps/drafts.py`, and the checks are in `check_answer`. Their
tests are in `tests/test_drafts.py`. Drafts are cached, so use `.\ja redraft N` to see the effect of
a change on a real job.

### Retake the README screenshot

```powershell
.venv\Scripts\python scripts\readme-screenshot\make_shot.py jobapps\web docs\images\today.png 1226
```

The script serves the dashboard's files from disk and answers the API with the made-up data in
`scripts/readme-screenshot/demo_state.py`. It never contacts the real server, so nothing real can
appear in the picture. Keep it that way: this repository is public.
