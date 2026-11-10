# Privacy and security

JobApps holds your resume, your answers to legal questions and a record of everywhere you applied.
This page says where that data lives, what leaves your PC, and who can reach the server.

## What is stored

Everything is kept in the project folder on the PC that runs the server. None of it is tracked by
git.

| Where | What |
|---|---|
| `profile.yaml`, `background.md`, `learned_answers.yaml`, `resume.pdf` | your details, notes, remembered answers and resume |
| `data/jobapps.db` | postings, daily lists, application forms, drafted and edited answers, the event log and the per-application timer |
| `data/backups/` | a copy of the database, `profile.yaml`, `background.md` and `learned_answers.yaml` from each of the last 7 daily runs |
| `data/prompts/` | every prompt sent for drafting, which includes your resume and notes |
| `data/jobapps.log` | the server log |
| `data/browser-profile/` | the browser profile `ja apply` uses, including any sites you signed in to with it |
| `reports/` | the daily HTML reports |

The event log is never trimmed. It is the permanent record of what was picked, ruled out, opened and
applied to.

The backups are on the same disk as the originals, and your resume is not in them. Keep your own
copy of the resume, and of the whole folder if losing the PC would matter.

## What leaves your PC

JobApps has no telemetry and no account. It makes these outside requests:

- It downloads the internship lists and employer boards named in `config.yaml`, and the pages of
  postings it shortlists. Those requests say nothing about you.
- It sends text to Claude, described below.
- The dashboard page loads one font from Google Fonts, so your browser contacts Google when you open
  it.

The extension only talks to your own server. The server's address is written into the extension when
you download it.

### What is sent to Claude

Claude is called through the `claude` command or the Anthropic API, whichever you set up. Anthropic's
terms for that account apply to what is sent.

| Call | What it contains |
|---|---|
| Screening a posting | the posting text, your resume text, your notes, and the profile values listed below |
| Drafting answers | the same, plus the form's questions, your writing preferences, and answers already written for that application |
| Looking up a posting that can't be read | the company, job title, location and link. Nothing about you |
| `discover`, which is off by default | your school, major, year of study, graduation year, the skills found in your resume, and the term and city you want |

The profile values are your name, location, school, degree, major, minor, graduation date, year of
study, GPA, profile links, citizenship, work authorization, sponsorship, and the term and start date
you want.

Your resume text is sent as it is, so contact details printed on it are included. Your phone number,
address and EEO choices from `profile.yaml` are not put in any prompt.

To send nothing to Claude, set `drafts.backend: none` in `config.yaml`. JobApps then picks jobs
without the Claude screen and shows the drafting prompts for you to use yourself.

## Who can reach the server

The server listens on `127.0.0.1` only, so nothing outside the PC can connect to it directly.
`scripts/install-server.ps1` then runs `tailscale serve`, which publishes it over HTTPS on port 8443
to devices on your own tailnet.

There is no password. The server decides on each request:

- A request that arrived through Tailscale Funnel, which is the public internet, is refused.
- The `Host` header must be `localhost`, the PC's tailnet name or address, or a name listed under
  `server.allowed_hosts`.
- A request from a tailnet user must come from a login listed under `server.allowed_logins`. When
  that list is empty, only the Tailscale account the PC itself is signed in to is allowed. The
  server prints the allowed logins when it starts. If Tailscale can't tell it that account yet, no
  tailnet user is let in, and the server asks again every minute.
- A request that changes anything is refused when it comes from a web page other than the
  dashboard itself. The page's address must match the server's name and port.
- Data sent with a change must be JSON and labelled as JSON, which a form on another website can't
  do.

Pages are served with a content security policy that only allows scripts from the server itself.

### Known gaps

Most of these are open in the [issue tracker](https://github.com/pyturtle/jobapps/issues).

- Any program running on the PC can reach the server on port 8765 without signing in.
- The check on where a change comes from lets every browser extension through, not only the JobApps
  one.
- A tailnet device that has no user login, such as a tagged device, is treated like the PC itself and
  skips the `allowed_logins` check.
- A page the extension runs on can ask it to scan and fill again. The values it fills are visible to
  that page, as they are with any autofill. Check which page you are on before you fill.
- The server fetches whatever links the public lists contain. It does not yet refuse links that point
  at addresses on your own network.

Do not put the server behind Tailscale Funnel or forward its port. It is built for one person on
their own tailnet.

## Removing your data

To remove the scheduled task and stop publishing the port:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-server.ps1 -Remove
```

Then delete `data/`, `reports/` and the four personal files.
