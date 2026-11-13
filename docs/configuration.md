# Configuration

There are two kinds of settings. `config.yaml` says what to look for and is tracked by git. Your
personal files hold who you are and are git-ignored.

## config.yaml

The file is commented, so this table is only a map. Values in brackets are what the repository
ships with.

| Key | What it controls |
|---|---|
| `daily_count` | new jobs picked per day (10) |
| `carry_over`, `carry_days`, `carry_max` | whether unfinished jobs stay on the list, for how many days (3), and how many at most (5) |
| `company_cooldown_days` | companies you have had in this many days (30) are skipped while there are enough others |
| `max_per_company` | jobs from one company per day (1) |
| `sources` | public internship lists to fetch |
| `boards` | employers' own Greenhouse, Lever, Ashby, Workday and SmartRecruiters boards to poll |
| `filters` | which postings are considered at all: `max_age_days` (90), `stale_after_days` (21), categories, title regexes, terms, degree, locations, companies |
| `scoring` | points for recency, readable forms, category, term, location, title keywords and favourite companies |
| `fit` | how many postings are read (`shortlist`, 80), points for skill overlap (`skill_points`, 30), `max_years_required` (2) |
| `screen` | the Claude screen: `enabled`, how many postings (`top`, 25), its `weight` (40) and the lowest fit kept (`min_fit`, 30) |
| `reach_companies`, `max_reach_per_day` | companies to count as stretch roles, and how many stretch roles a day (2) |
| `max_outside_canada_per_day` | jobs outside Canada per day (1); `null` removes the limit |
| `research` | Claude lookups for postings JobApps can't read: `enabled`, `model`, `budget_usd` per lookup (0.3), `max_per_day` (6) |
| `discover` | optional Claude web search for postings the lists missed (off) |
| `server` | `daily_at` ("07:30"), `port` (8765), `allowed_logins` (the tailnet accounts let in; empty means the account this PC is signed in to), `allowed_hosts` (extra names the dashboard may be opened under) |
| `drafts` | `backend` (`auto`, `api`, `claude-cli` or `none`), `model`, `effort`, `parallel`, `timeout_seconds` |
| `browser` | the browser `ja apply` drives (`msedge`) |
| `notes` | files on this PC or pages on the web read with `background.md` for every draft and screen (none) |

### Aiming it at a different search

The shipped settings look for Summer 2027 software and AI internships in Canada. To change that,
edit:

- `sources` and `boards`, for where postings come from
- `filters.title_include`, `filters.title_exclude`, `filters.categories` and
  `filters.exclude_terms`, for which roles are considered
- `scoring.locations`, `scoring.terms` and `scoring.title_keywords`, for what is preferred
- `max_outside_canada_per_day`, which assumes you are applying from Canada

### Reading more than background.md

`notes` lists files and pages whose text joins your notes in every draft and screen, under a heading
that names each one:

```yaml
notes:
  - more-notes.md                             # a file in this folder, read as it is each time
  - https://www.nathan-nguyen.dev/llms.txt    # a page: fetched once a day, the last copy kept
```

A page is fetched by the daily run, or now with `ja notes --refresh`, and its copy is kept under
`data/notes/`, so a day the page can't be reached still has the last one. A page must be plain text or
Markdown; an HTML page is refused and the copy kept. `ja notes` and the Setup page say where each
source stands. Before a page is online, `ja notes --copy FILE` keeps a copy from a file instead, and the
page replaces it as soon as it can be fetched.

The shipped entry is the author's portfolio, which publishes itself as one text file. A site built the
same way can do the same, and anything else about you that lives in a file can simply be listed.

### Choosing how Claude is called

`drafts.backend` is `auto` by default: it uses `ANTHROPIC_API_KEY` if that is set, then the `claude`
command if it is installed, and otherwise nothing. With no backend, postings are not screened and
the dashboard shows the prompts in place of drafts. Looking up unreadable postings (`research`) and
`discover` need the `claude` command, because they use its web search.

## Your files

These are created from templates the first time JobApps runs. The dashboard's Setup page edits all
of them, so you rarely need to open them.

| File | What it holds |
|---|---|
| `profile.yaml` | everything typed into forms: name, contact details, address, links, education, work history, skills, citizenship, work authorization, sponsorship, preferences, EEO choices (decline by default), file paths, `writing_preferences` and `custom_answers` |
| `background.md` | notes the drafts may use as facts: what you are looking for, projects, experience, short true stories, things to always or never mention |
| `learned_answers.yaml` | answers you gave on forms, reused when the same question comes up |
| `resume.pdf` | your resume |

The more `background.md` says about why you did things and what you learned, the fewer
`[placeholders]` the drafts contain. The drafter is not allowed to make those up.

### custom_answers

A rule answers any question whose text matches a regular expression. Rules are checked before
remembered answers and the built-in rules.

```yaml
custom_answers:
  - match: "how many hours (per|a) week"
    answer: "40"
  - match: "security clearance"
    answer: null        # always leave this one for me
```

### writing_preferences

Standing instructions for every draft, in your own words:

```yaml
writing_preferences:
  - "Use my internship before any hackathon project."
```

They change how answers are written. They cannot add facts.
