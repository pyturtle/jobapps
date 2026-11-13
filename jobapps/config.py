"""Paths plus loading of config.yaml (search settings) and profile.yaml (your values)."""
from __future__ import annotations

import copy
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
PROMPTS_DIR = DATA_DIR / "prompts"
BROWSER_PROFILE_DIR = DATA_DIR / "browser-profile"
DB_PATH = DATA_DIR / "jobapps.db"
CONFIG_PATH = ROOT / "config.yaml"
PROFILE_PATH = ROOT / "profile.yaml"
LEARNED_PATH = ROOT / "learned_answers.yaml"
BACKGROUND_PATH = ROOT / "background.md"
NOTES_DIR = DATA_DIR / "notes"   # the last copy of each page listed under `notes` in config.yaml

# Built-in defaults; config.yaml overrides any of these keys.
DEFAULTS: dict[str, Any] = {
    "daily_count": 10,
    "carry_over": True,
    "carry_days": 3,
    "carry_max": 5,
    "company_cooldown_days": 30,
    "max_per_company": 1,
    "sources": [],
    "filters": {
        "max_age_days": 90,
        "stale_after_days": 21,
        "categories": [],
        "title_include": [],
        "title_exclude": [],
        "exclude_terms": [],
        "require_degree": "",
        "exclude_sponsorship": [],
        "location_include": [],
        "location_exclude": [],
        "exclude_companies": [],
    },
    "scoring": {
        "recency_max": 30,
        "recency_per_day": 1,
        "ats": {},
        "categories": {},
        "terms": {},
        "locations": [],
        "title_keywords": [],
        "favorite_companies": {"points": 20, "names": []},
    },
    "reach_companies": [],
    "max_reach_per_day": 1,
    "max_outside_canada_per_day": None,
    "fit": {"shortlist": 40, "skill_points": 30, "unknown_points": 8, "max_years_required": 2},
    "screen": {"enabled": True, "top": 12, "weight": 40, "min_fit": 30, "effort": "low", "parallel": 3},
    "research": {"enabled": True, "model": "claude-sonnet-5-5", "effort": "low", "budget_usd": 0.3,
                 "max_per_day": 6, "parallel": 3},
    "discover": {"enabled": False, "budget_usd": 1.0, "max_results": 15, "effort": "low", "roles": [], "locations": []},
    "drafts": {
        "backend": "auto",
        "model": "claude-opus-5-5",
        "effort": "medium",
        "parallel": 3,
        "timeout_seconds": 600,
    },
    "server": {"port": 8765, "daily_at": "07:30", "tailscale_https_port": 8443, "allowed_logins": [], "allowed_hosts": []},
    "browser": {"channel": "msedge"},
    "notes": [],   # files on this PC or pages on the web, read with background.md (jobapps/notes.py)
}


def deep_merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_config(path: Path = CONFIG_PATH) -> dict:
    return deep_merge(DEFAULTS, load_yaml(path) or {})


_PLACEHOLDER = re.compile(r"^\s*(todo|changeme|xxx+)\b", re.I)


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or bool(_PLACEHOLDER.match(value))
    if isinstance(value, (list, tuple)):
        return all(is_blank(v) for v in value)
    return False


class Profile:
    """Read-only view of profile.yaml. Blank or TODO values count as missing."""

    REQUIRED = [
        "name.first", "name.last", "email", "phone", "location",
        "education.school", "education.major", "education.graduation_year",
        "work_authorization.canada", "work_authorization.us",
        "needs_sponsorship.canada", "needs_sponsorship.us",
        "files.resume",
    ]

    def __init__(self, data: dict | None, root: Path = ROOT):
        self.data = data or {}
        self.root = root

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        if isinstance(node, list):
            node = [v for v in node if not is_blank(v)]
        return default if is_blank(node) else node

    def file(self, dotted: str) -> Path | None:
        value = self.get(dotted)
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        return path if path.exists() else None

    @property
    def full_name(self) -> str:
        return self.get("name.full") or " ".join(
            p for p in (self.get("name.first"), self.get("name.last")) if p
        )

    @property
    def custom_answers(self) -> list[dict]:
        return [c for c in (self.data.get("custom_answers") or []) if isinstance(c, dict) and c.get("match")]

    def missing(self) -> list[str]:
        out = [key for key in self.REQUIRED if self.get(key) is None]
        if self.get("files.resume") and not self.file("files.resume"):
            out.append("files.resume (file not found)")
        return out


def ensure_user_files() -> None:
    """First run after cloning: start your own profile.yaml and background.md from the templates. Those two
    (and learned_answers.yaml, your resume and data/) hold your personal details and are never committed."""
    import shutil
    for path in (PROFILE_PATH, BACKGROUND_PATH):
        example = path.with_name(f"{path.stem}.example{path.suffix}")
        if not path.exists() and example.exists():
            shutil.copyfile(example, path)


def load_profile(path: Path = PROFILE_PATH) -> Profile:
    return Profile(load_yaml(path) or {})


def _scalar(value: Any) -> str:
    """A YAML value that round-trips exactly (JSON is valid YAML flow syntax)."""
    import json
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) or value is None:
        return json.dumps(value)
    return json.dumps(value, ensure_ascii=False)


def _split_comment(rest: str) -> tuple[str, str]:
    """Split 'value   # comment' without being fooled by '#' inside quotes."""
    quote = None
    for i, ch in enumerate(rest):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            return rest[:i].rstrip(), "  " + rest[i:]
    return rest.rstrip(), ""


class _BlockDumper(yaml.SafeDumper):
    """Lists of records (work history) as readable YAML: multi-line text as | blocks."""


_BlockDumper.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))


def set_yaml_value(text: str, dotted: str, value: Any) -> str:
    """Replace one `key: value` in YAML text, keeping every comment and the layout intact."""
    lines = text.splitlines(keepends=True)
    start, end, indent = 0, len(lines), 0
    parts = dotted.split(".")
    for depth, key in enumerate(parts):
        found = None
        for i in range(start, end):
            m = re.match(r"^(\s*)([\w-]+):(.*)$", lines[i].rstrip("\r\n"))
            if m and len(m.group(1)) == indent and m.group(2) == key:
                found = i
                break
        if found is None:
            if depth == 0 and len(parts) == 1:  # new top-level key: append it
                return text.rstrip("\n") + f"\n{key}: {_scalar(value)}\n"
            if depth == 0 and len(parts) == 2:  # new top-level block with one key
                return text.rstrip("\n") + f"\n{key}:\n  {parts[1]}: {_scalar(value)}\n"
            raise KeyError(dotted)
        if depth == len(parts) - 1:
            m = re.match(r"^(\s*[\w-]+:)(.*)$", lines[found].rstrip("\r\n"))
            _, comment = _split_comment(m.group(2).strip())
            newline = "\r\n" if lines[found].endswith("\r\n") else "\n"
            # The old value may continue on more-indented lines (a block list, a wrapped flow list).
            stop = found + 1
            while stop < end and (not lines[stop].strip() or len(lines[stop]) - len(lines[stop].lstrip()) > indent):
                stop += 1
            while stop > found + 1 and not lines[stop - 1].strip():
                stop -= 1  # blank lines before the next key stay
            if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                dumped = yaml.dump(value, Dumper=_BlockDumper, sort_keys=False, allow_unicode=True, width=1000)
                body = "".join(f"{' ' * (indent + 2)}{line}{newline}" for line in dumped.splitlines())
                lines[found:stop] = [f"{m.group(1)}{comment}{newline}{body}"]
            else:
                lines[found:stop] = [f"{m.group(1)} {_scalar(value)}{comment}{newline}"]
            return "".join(lines)
        start = found + 1
        block_end = start
        while block_end < end and (not lines[block_end].strip() or lines[block_end].lstrip().startswith("#")
                                   or len(lines[block_end]) - len(lines[block_end].lstrip()) > indent):
            block_end += 1
        end = block_end
        child = [l for l in lines[start:end] if l.strip() and not l.lstrip().startswith("#")]
        if not child:
            raise KeyError(dotted)
        indent = len(child[0]) - len(child[0].lstrip())
    raise KeyError(dotted)


# One writer at a time for your files (the server answers requests on several threads at once).
WRITE_LOCK = threading.RLock()


def atomic_write(path: Path, content: str | bytes) -> None:
    """Write a whole new file, then swap it in: anything reading it at that moment sees the old or the new
    file, never half of one."""
    tmp = path.with_name(f".{path.name}.tmp")
    if isinstance(content, bytes):
        tmp.write_bytes(content)
    else:
        tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def update_profile(updates: dict[str, Any], path: Path = PROFILE_PATH) -> None:
    """Write several dotted keys into profile.yaml, verifying the result parses to what was asked."""
    with WRITE_LOCK:
        _update_profile(updates, path)


def _update_profile(updates: dict[str, Any], path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for dotted, value in updates.items():
        text = set_yaml_value(text, dotted, value)
    data = yaml.safe_load(text)
    for dotted, value in updates.items():
        node = data
        for part in dotted.split("."):
            node = node[part]
        if node != value:
            raise ValueError(f"profile.yaml edit for {dotted} did not round-trip")
    atomic_write(path, text)


def load_learned(path: Path = LEARNED_PATH) -> list[dict]:
    data = load_yaml(path) or []
    return [d for d in data if isinstance(d, dict) and d.get("label")]


def save_learned(entries: list[dict], path: Path = LEARNED_PATH) -> None:
    header = (
        "# Answers learned from forms you filled by hand. Matched by exact (normalized) question text.\n"
        "# Edit or delete freely; profile.yaml custom_answers take priority over these.\n"
    )
    body = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True, width=120)
    with WRITE_LOCK:
        atomic_write(path, header + body)
