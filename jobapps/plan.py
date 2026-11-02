"""Turn fields found on a live page into fill actions.

Used by the browser extension (via the server): the extension scans the page with filler.js and
sends the fields here; every value is decided by the same Resolver as everywhere else, so the
extension is only a hands-on executor and never decides anything itself.
"""
from __future__ import annotations

import re
from datetime import date

from jobapps.forms import Field
from jobapps.rules import MonthYear, Resolution, Resolver, candidates, choose_option, display, norm


def to_field(f: dict) -> Field:
    return Field(key=str(f["id"]), label=f.get("label") or "", type=f["type"], required=bool(f.get("required")),
                 options=f.get("options") or [], section=f.get("section") or "")


def has_value(value) -> bool:
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, bool):
        return value
    return bool(str(value or "").strip())


def text_for(f: dict, value) -> str:
    """The exact text to type into a text-like field."""
    if isinstance(value, MonthYear):
        return value.format_for(f.get("inputType", ""), f.get("placeholder", ""))
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d" if f.get("inputType") == "date" else "%m/%d/%Y")
    text = display(value)
    if f.get("inputType") == "number":
        text = re.sub(r"[^\d.]", "", text)
    return text


def date_parts(f: dict, value) -> dict | None:
    """Digits for each box of a split date (Workday: month / day / year spin boxes)."""
    if isinstance(value, MonthYear):
        y, m, d = value.year, value.month, MonthYear.DAY
    elif isinstance(value, date):
        y, m, d = value.year, value.month, value.day
    elif re.fullmatch(r"\d{4}", str(value or "")):
        y, m, d = int(value), None, None
    else:
        return None
    wanted = {"month": m, "day": d, "year": y}
    parts = {p: wanted[p] for p in f.get("parts") or []}
    return parts if parts and all(v is not None for v in parts.values()) else None


def shown_date(parts: dict) -> str:
    order = [p for p in ("month", "day", "year") if p in parts]
    return "/".join(f"{parts[p]:02d}" if p != "year" else str(parts[p]) for p in order)


def choose_exact(value: str, options: list[str]) -> str | None:
    """Strict match for search-box picks such as skills: the option is the value itself, maybe with a
    qualifier ('Python (Programming Language)', 'Amazon Web Services (AWS)', 'React.js')."""
    compact = lambda s: re.sub(r"[^a-z0-9+#]", "", s)  # noqa: E731
    v = norm(value)
    for o in options:
        n = norm(o)
        base = re.sub(r"\s*\([^)]*\)$", "", n)
        inner = re.search(r"\(([^)]*)\)$", n)
        if v in (n, base) or (inner and inner.group(1) == v) or compact(base) in (compact(v), compact(v) + "js"):
            return o
    return None


def _same(current, want) -> bool:
    if isinstance(want, bool) or isinstance(current, bool):
        return bool(current) == bool(want)
    return norm(display(current)) == norm(str(want))


def action(f: dict, res: Resolution) -> dict:
    base = {"id": f["id"], "label": f.get("label") or "", "required": bool(f.get("required")),
            "note": res.note or res.source, "op": None, "rule": res.source}
    missing = "missing" if base["required"] else "manual"
    current = f.get("value")
    if res.kind == "skip":
        return {**base, "state": "skip"}
    if has_value(current) and not res.overwrite and f["type"] != "multiselect":
        return {**base, "state": "filled", "note": "already filled"}
    if res.kind == "manual" or (res.kind == "draft" and not res.value):
        return {**base, "state": missing}
    state = "draft" if res.kind == "draft" else "filled"
    kind, value = f["type"], res.value
    if res.kind == "file":
        return {**base, "state": state, "op": "file", "file": res.source}
    if kind == "date" and f.get("parts"):
        parts = date_parts(f, value)
        if not parts:
            return {**base, "state": missing, "note": f"can't put '{display(value)}' into this date"}
        if _same(current, shown_date(parts)):
            return {**base, "state": "filled", "note": "already filled"}
        return {**base, "state": state, "op": "dateparts", "parts": parts, "shown": shown_date(parts)}
    if kind in ("text", "textarea", "date"):
        text = text_for(f, value)
        if res.overwrite and _same(current, text):
            return {**base, "state": "filled", "note": "already filled"}
        return {**base, "state": state, "op": "text", "text": text}
    if kind == "select":
        opts = f.get("options") or []
        chosen = value if value in opts else choose_option(value, opts)
        if chosen is None:
            return {**base, "state": missing, "note": f"no option matches '{display(value)}'"}
        return {**base, "state": state, "op": "select", "value": (f.get("optionValues") or opts)[opts.index(chosen)],
                "shown": chosen}
    if kind in ("radio", "checkbox", "buttons"):
        opts, idx = f.get("options") or [], []
        for want in (value if isinstance(value, list) else [value]):
            opt = want if want in opts else choose_option(want, opts)
            if opt is None:
                return {**base, "state": missing, "note": f"no option matches '{display(want)}'"}
            idx.append(opts.index(opt))
        return {**base, "state": state, "op": "click", "indexes": idx, "shown": [opts[i] for i in idx]}
    if kind == "boolean":
        if res.overwrite:
            return {**base, "state": state, "op": None if _same(current, value) else "check", "on": value is True}
        return {**base, "state": state, "op": "check" if value is True else None, "on": True}
    if kind == "combobox":
        return {**base, "state": state, "op": "combobox", "candidates": candidates(value),
                "pick_first": res.source == "location"}
    if kind == "dropdown":  # e.g. Workday's button + listbox; its options only exist once it's open
        cands = candidates(value)
        if has_value(current) and choose_option(cands, [str(current)]) is not None:
            return {**base, "state": "filled", "note": "already filled"}
        return {**base, "state": state, "op": "dropdown", "candidates": cands,
                "pick_first": res.source == "location"}
    if kind == "multiselect":  # a search box that collects picks as chips (skills, school, how you heard)
        chips = current if isinstance(current, list) else []
        if res.many:
            todo = [v for v in candidates(value) if not choose_exact(v, chips)]
            if not todo:
                return {**base, "state": "filled", "note": "already filled"}
            return {**base, "state": state, "op": "multiselect", "items": [[v] for v in todo], "exact": True,
                    "clear": False, "shown": todo}
        cands = candidates(value)
        if chips and choose_option(cands, chips) is not None:
            return {**base, "state": "filled", "note": "already filled"}
        if chips and not res.overwrite:
            return {**base, "state": "filled", "note": "already filled"}
        return {**base, "state": state, "op": "multiselect", "items": [cands], "exact": False, "clear": bool(chips),
                "pick_first": res.source in ("location", "preferences.heard_about"), "shown": cands[:1]}
    return {**base, "state": missing, "note": f"unsupported field type {kind}"}


def actions(resolver: Resolver, fields: list[dict]) -> list[dict]:
    return [action(f, resolver.resolve(to_field(f))) for f in fields]


def sections(resolver: Resolver) -> list[dict]:
    """How many blocks each repeated section should have, so the extension can press 'Add' for the rest."""
    p = resolver.profile
    links = [k for k in ("linkedin", "github", "website", "twitter") if p.get(f"links.{k}")]
    return [{"kind": "work", "want": len(p.get("experience.history") or [])},
            {"kind": "education", "want": 1 if p.get("education.school") else 0},
            {"kind": "website", "want": len(links)}]


def needs_draft(resolver: Resolver, fields: list[dict]) -> list[dict]:
    out = []
    for f in fields:
        res = resolver.resolve(to_field(f))
        if res.kind == "draft" and not res.value and not has_value(f.get("value")):
            out.append(f)
    return out
