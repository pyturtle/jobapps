# JobApps

A home server (a Windows PC) picks **5 internships a day that fit your resume**, reads their
application forms, drafts the essay answers with your Claude login, and serves a dashboard you open
from your laptop over Tailscale. A Chrome extension on the laptop fills each application for you.
You review and press Submit yourself; nothing is ever submitted for you.

It's tuned for Canadian software/AI internships (Toronto first) out of the box; `config.yaml` sets
the feeds, filters and scoring.

## Setup

**On the PC** (Windows, Python 3.11+, [Tailscale](https://tailscale.com), and the
[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) signed in with `claude`):

```powershell
git clone https://github.com/pyturtle/jobapps; cd jobapps
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\python -m playwright install chromium
powershell -ExecutionPolicy Bypass -File scripts\install-server.ps1
```

The script registers a **JobApps Server** task that starts the server at login, with a 5-minute
watchdog that relaunches it if it ever stops, and publishes the dashboard to your tailnet on port
8443 (tailnet only, never public). It prints the dashboard address. Run it with `-Remove` to undo.
(Or just run `ja serve` and open http://127.0.0.1:8765.)

Your personal files are created from templates on first run and are never committed (see
`.gitignore`): `profile.yaml` (from `profile.example.yaml`), `background.md` (notes for answers),
`learned_answers.yaml` (answers it remembers), your resume PDF, and everything in `data/`.

**Keeping it healthy (all automatic).** If the PC is asleep or off at 07:30, the daily list is built
when it's back. A run that fails (no network, a feed down) retries every hour. A feed that fails
never marks jobs closed. The database is backed up daily (`dataackups`, last 7 kept), and the log
rotates at 5 MB (`data\jobapps.log`). **Setup → System** on the dashboard shows all of this, plus
whether Claude calls are working. If they stop (e.g. an expired login), run `claude` once on the PC.

**On your laptop**, open the dashboard (Safari on a Mac: File → Add to Dock, for an app window) → **Setup**:
1. Drop in your **resume PDF**: picks, fit checks and answers all read from it.
2. Fill in the highlighted **details** (graduation date, citizenship, work authorization/sponsorship…).
   Legal questions are never guessed.
3. Check **work history and skills** (copied from your resume; typed into Workday's My Experience page).
   Add **notes** for answers: projects, experience, short true stories.
4. **Install the extension so it updates itself:** on a Mac (Tailscale on) run
   `curl -fsSL https://<your-pc>.<tailnet>.ts.net:8443/mac-setup.sh | bash` (Setup shows it with a Copy
   button), then `chrome://extensions` → Developer mode → Load unpacked → `~/JobApps/jobapps-extension`.
   A LaunchAgent re-syncs that folder from the server every 15 minutes and the extension reloads
   itself when the build changes. (Or download the zip from Setup and reload by hand. Chrome, Arc,
   Brave or Edge; Safari isn't supported. Stop updates: `bash ~/JobApps/uninstall.sh`.)
5. **Re-pick today's jobs** so today's list reflects your resume too.

## Every day

Open the dashboard: today's 5, each with **why it fits** (the skills you have and lack, and each
requirement checked against a quote from your resume), drafted answers you can edit in place,
and an **Open application** button. Today's jobs fill themselves; on any other application page
(about 60 job sites: Greenhouse, Lever, Ashby, Workday, iCIMS, SmartRecruiters, Taleo, SuccessFactors…)
the extension offers **Fill this page**, or press ⌥J. Green = filled, blue = drafted (read it),
red = needs you. Submit, and the job is marked applied on the dashboard.

**Answer once, filled forever:** fill in a red field on the page and it's remembered as soon as you
leave the field (postal code, phone and similar go straight into your profile; anything else into
`learned_answers.yaml`, also matching the same question from another company). Questions it met
but couldn't answer are listed in **Setup → Answers JobApps remembers** (Today shows a heads-up),
so you can answer them before you see the form; everything remembered is editable there too.

On **Workday** it walks the whole wizard with you: dropdowns, search boxes (skills, field of study,
how you heard), month/year dates, and it presses **Add** for each job, school and link, replacing
whatever Workday guessed from your resume. You still press Save and Continue yourself on each page.

Don't like a drafted answer? **Ask Claude to change this answer** under it ("lead with my internship,
not the hackathon project"); tick *Remember this* to make it a standing instruction for every future draft.

**No repeats:** every morning brings 5 *new* jobs. A posting you've had on any list (applied, skipped,
swapped out) or that was ruled out is never picked again, nor the same role under another link
(another career site, a Workday repost, another feed) or the same company under another name
(HPE = Hewlett Packard Enterprise); companies you've had in the last week (`company_cooldown_days`)
only come back when there aren't enough others. Unfinished jobs show apart, under *Still open from
earlier days*, for up to `carry_days` (3) days; **‹ previous day**
and History open any earlier day's list. The day's main list stays at 5; **Add 1 / 3 / 5 more** adds
the next best fits as extras, and **Postings → Add to today** puts any posting you like on the list.

## Your record

**Postings** lists every posting ever collected (none are deleted) with what happened to it: never
picked, on your list, applied, skipped, ruled out (with the reason) or swapped out by a re-pick.
Search, filter (status, Canada, still open) and **Download CSV**. Every change is also kept in an
event log (picked, carried over, added, opened, applied, skipped, undone).

**Timer:** the extension counts the time you actually spend working on each application page (tab
visible and you typing, clicking or scrolling; an idle tab doesn't count). The panel shows it while
you work, Today shows it on each job, and **History** shows your average, median, total and time by
site (Workday vs Greenhouse...).

## Where postings come from (checked every morning)

Chosen after downloading and comparing about 40 candidate lists:

| Source | What it adds |
|---|---|
| SimplifyJobs Summer 2027 | the big one: ~4,600 open roles, direct job-board links |
| vanshb03 Summer 2027 | hand-curated; its short links are resolved to the real board and cached |
| Francklin9999 canada-internship | Canada-only, refreshed 2-4×/day, real posting dates, PEY (12/16-month) tags |
| dreamworkhq Open-Tech-Internships | Canadian slice of a verified-open index |
| speedyapply SWE + AI (international) | Canadian rows the others miss |
| employer boards | Canadian employers' own Greenhouse/Lever/Ashby/Workday boards, polled directly (`boards:` in config.yaml) |

Skipped: lists that just mirror Simplify (negarprh, frogg, Bobwillrule…), ones whose links need a login or go through
aggregators (csjobs.ca, zapply, firstjobby), and the federal Job Bank feed (no internships in it). The same posting
from several sources becomes one entry; the dashboard's *Sources checked* shows what each contributed today.

## How picks fit your resume

1. **Metadata rank** (no model): fresh postings, readable forms, Toronto/Canada first. Top 40 are
   checked live.
2. **Resume fit + eligibility** (no model): each posting is read and ruled out if it's graduate-only,
   asks for 3+ years, has a graduation window or "returning to school" date that excludes you, needs
   U.S. citizenship/clearance or a citizenship you don't have, won't sponsor when you need it, or has
   a GPA floor above yours. Every rule-out shows the posting's own sentence (Today → *Ruled out*).
   Skill overlap with your resume is scored.
3. **Claude screen** (your login, top 12): lists the key requirements. The server only accepts a
   requirement it can find **word for word in the posting**, and only counts it as met if Claude
   quoted **your resume** word for word. Everything else is dropped or downgraded, and the score comes
   only from what survived. Results are cached, so each posting is judged once.
4. **Pick 5**: best total, one per company, at most one stretch role a day.

Drafted answers follow the same rule. Each lists the resume facts it used; the dashboard flags
any fact or number that isn't in your resume, plus every `[placeholder]` left for you.

## Deterministic

Same listings + same files + same day → same 5, same fields, same answers. Ranking uses fixed
integer scores and a stable tie-break. Model output only enters through verified quotes and is
cached, and why each job was picked is frozen when it's picked. Field values come from:
your `custom_answers` → `learned_answers.yaml` → built-in rules → drafted answer → you.

## On the PC (CLI)

`ja serve` · `ja today` · `ja status` · `ja explain 2` (every point and quote behind a pick) ·
`ja skills` (what's read from your resume) · `ja apply` (fill in a browser on this PC) · `ja doctor`

## Settings

`config.yaml`: feeds, filters, scoring, `fit`, `screen`, `reach_companies`, `discover` (Claude
web search for postings the feeds miss; off by default because in testing the feeds already had
10 of the 11 it found), `server.daily_at`. `profile.yaml`: everything typed into forms (the
dashboard edits it without losing comments).

## Security

The server listens on 127.0.0.1 only, and Tailscale Serve exposes it to your tailnet. It refuses
anything arriving via Funnel (the public internet), allows only your tailnet login, and checks the
Host header. Your data stays on your PC, in this folder (`data/`, `profile.yaml`, `resume.pdf`), and
none of it is tracked by git.

Tests: `.venv\Scripts\python -m pytest`
