# Usage

## The dashboard

The dashboard has four pages.

**Today** lists the day's new picks, followed by jobs still open from earlier days. Opening a job
shows:

- the drafted answers, which you edit in place and which save as you type
- any problems the checks found with an answer, such as a fact that isn't in your resume or a
  `[placeholder]` to fill in
- why the job fits: skills you have and lack, and each requirement with the resume line that
  supports it
- what the extension will fill in, and which fields it will leave for you
- the prompt used for the drafts

Under each answer, "Ask Claude to change this answer" redrafts it with your instruction, for example
"use my internship here, not the hackathon project". Tick "Remember this for future answers" to turn
the instruction into a standing writing preference.

"Add 1, 3 or 5 more" picks extra jobs for the day. The button with the previous day's date opens
earlier lists.

**Postings** is every posting JobApps has collected and what happened to it: never picked, on your
list, applied, skipped, ruled out (with the reason), closed, or swapped out by a re-pick. You can
search, filter, download a CSV, and add any posting to today's list.

**History** shows time per application (average, median, total, and by site), each day's list, and
every finished application with its links, a saved copy of the posting, your answers and a timeline.

**Setup** holds your resume, details, work history and skills, questions waiting for an answer,
remembered answers, notes for answers, writing preferences, the extension download, and a System
panel with the state of the server, the daily run, the feeds, Claude and backups.

## The extension

### Installing it

Download the zip from Setup, unzip it, open `chrome://extensions`, switch on Developer mode, press
"Load unpacked" and choose the folder. Download it again to update.

On a Mac there is a script that keeps the extension updated. Setup shows the command with your
server's address filled in:

```bash
curl -fsSL https://<your-pc>.<your-tailnet>.ts.net:8443/mac-setup.sh | bash
```

It puts the extension in `~/JobApps/jobapps-extension` and adds a LaunchAgent that asks your server
every 15 minutes whether there is a new build. Load that folder in `chrome://extensions` as above.
The extension reloads itself when the files change. If a new version is waiting, the toolbar popup
shows "Reload JobApps". To remove everything, run `bash ~/JobApps/uninstall.sh`.

The extension only talks to your own server. The address is written into it when it is downloaded.

### Filling a form

Press "Open application" on the dashboard. When the form loads, the extension fills it and shows a
panel listing what it filled, what it drafted, and what it left for you. Fill in what is left. Your
answer is saved and used the next time a form asks the same question.

On an application page you opened some other way, press Alt+J or use "Fill this page" in the toolbar
popup.

Press the site's own Submit button when you have checked the page. The job is marked applied when
the confirmation page appears. If it isn't, press "I submitted it" in the panel.

The extension handles text boxes, dropdowns, search-as-you-type boxes, radio and checkbox groups,
dates split across several boxes, and file uploads. On Workday it also presses Add for each job,
school and link in your profile, and replaces what Workday guessed from your resume.

## Supported sites

| Site | Read before you open it | Filled by the extension |
|---|---|---|
| Greenhouse, Lever, Ashby | description and questions | yes |
| Workday | description | yes, including the multi-page application |
| Oracle Cloud, Workable | description | yes |
| SmartRecruiters, iCIMS, Taleo, SuccessFactors, Jobvite, BambooHR, Rippling, Dayforce, UKG, Paylocity, ADP, Eightfold, Phenom, Avature and others | description, when the page can be read without running JavaScript | yes |

The extension runs on the 54 host patterns listed in `jobapps/extension/manifest.json`. On any other
site, use "Fill this page" in the popup. When an application continues on a company's own career site,
the popup also offers "Run JobApps on" that site. Chrome asks you to allow that one site, and from then
on the extension fills and times applications there by itself. Remove a site under the extension's
details in `chrome://extensions`.

Time on an application is counted while the extension runs on its pages. When it barely saw an
application, History uses the time from pressing "Open application" to "applied" instead, if that was
under two hours, counted as at most 45 minutes, and marks it as an estimate.

Written answers are only drafted ahead of time where the questions can be read ahead, which is
Greenhouse, Lever and Ashby. On other sites the extension asks the server for drafts when it finds
written questions on the page. The server writes them in the background, which takes about a minute.
The extension fills the rest of the page meanwhile and puts the drafts in when they arrive. If drafting
fails, the panel says so, and "Fill again" asks once more.

## Command line

Run these from the project folder with `.\ja` on Windows, or `python -m jobapps` anywhere.

| Command | What it does |
|---|---|
| `ja serve [--port N]` | run the dashboard and the daily scheduler |
| `ja today [--more N] [--refresh] [--no-fetch] [--no-draft] [--open]` | build or show today's list, draft answers, write the HTML report |
| `ja status` | today's list, backlog and totals |
| `ja explain N` | every point and quote behind pick N, and how each field of its form will be filled |
| `ja apply [N ...]` | fill today's jobs in a browser on this PC, one at a time |
| `ja done N` / `ja skip N` | mark job N applied, or drop it from the list |
| `ja redraft N` | throw away job N's drafted answers and draft them again |
| `ja skills` | the skills the fit check found in your resume |
| `ja recheck [--dry-run]` | put back postings ruled out on an earlier day that today's rules would let through |
| `ja fetch` | refresh the listings now |
| `ja report` | rebuild and open today's HTML report |
| `ja history [--limit N]` | recent applications |
| `ja doctor` | check your profile, resume, Claude access and scheduled task |

`--date YYYY-MM-DD` before the command runs it as if it were that day.

`ja apply` is the only command that drives a browser itself, and it needs Playwright, which the
normal install leaves out:

```powershell
.venv\Scripts\pip install -e ".[browser]"
```

It uses Microsoft Edge by default. Set `browser.channel` in `config.yaml` to use Chrome, or leave it
empty and run `python -m playwright install chromium` to use Playwright's own browser.

## Troubleshooting

| Problem | What to try |
|---|---|
| The dashboard won't load from another computer | Check that Tailscale is running on both machines. `tailscale serve status` on the PC should list port 8443. |
| Another computer is refused just after the PC starts | The server lets tailnet devices in once Tailscale tells it which account the PC is signed in to. It asks every minute, so wait a minute and reload. `data/jobapps.log` says when it has the answer. |
| "Can't listen on 127.0.0.1:8765" | Another copy of the server is running. Stop the "JobApps Server" scheduled task, or close the other `ja serve`, before starting a new one. |
| "Claude calls are failing" | Run `claude` once on the PC to sign in again, or check `ANTHROPIC_API_KEY`. |
| No list this morning | The PC was off or offline. The list is built when it is back, with a retry every hour. Setup → System shows the last run. |
| The extension doesn't fill a page | Press Alt+J or use "Fill this page". Check that the popup says Connected. |
| "JobApps just updated. Reload this page" | The extension updated while the page was open. Reload the page. |
| A question keeps being left for you | Answer it under Setup → Answers JobApps remembers, or add a `custom_answers` rule to your profile. |
| Anything else | Run `.\ja doctor` and read `data/jobapps.log`. |
