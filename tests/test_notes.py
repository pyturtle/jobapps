"""Extra notes from files and pages: read with background.md, fetched once a day, the last copy kept."""
from datetime import date

import pytest

from jobapps import drafts, net, notes
from jobapps.config import Profile

PAGE = "# Me\nI build robots, RC trucks and a split-flap display."
HOBBIES = ("# Hobbies\n\nI started figure skating in grade 10 and skate in black Jackson Freestyles on Aspire blades. "
           "I've played volleyball since high school, and it's probably my favourite sport. I took up golf in summer "
           "2026 and practised for three months straight, mostly working on my swing.")


def quiet(*args):
    pass


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "NOTES_DIR", tmp_path / "notes")
    monkeypatch.setattr(notes, "ROOT", tmp_path)
    (tmp_path / "more.md").write_text(HOBBIES, encoding="utf-8")
    return tmp_path


def test_files_are_read_as_they_are_and_a_missing_one_says_so(home):
    cfg = {"notes": ["more.md", "gone.md"]}
    a, b = notes.sources(cfg)
    assert a.kind == "file" and "Jackson Freestyles" in a.text and a.status.endswith("characters")
    assert b.text == "" and b.status == "file not found"
    assert notes.text(cfg).startswith("## More from the candidate: more.md\n\n# Hobbies")


def test_pages_are_fetched_once_a_day_and_a_failure_keeps_the_copy(home, monkeypatch):
    url = "https://example.test/llms.txt"
    cfg, calls, page = {"notes": [url]}, [], [PAGE]

    def request(u, **kw):
        calls.append(u)
        if page[0] is None:
            raise net.HttpError(404, u)
        return 200, page[0].encode("utf-8")

    monkeypatch.setattr(net, "request", request)
    assert notes.sources(cfg)[0].status.startswith("no copy yet") and calls == []   # reading never fetches
    notes.refresh(cfg, today=date(2026, 11, 12), log=quiet)
    s = notes.sources(cfg)[0]
    assert s.text == PAGE and s.status.startswith("copy from ") and calls == [url]
    notes.refresh(cfg, today=date(2026, 11, 12), log=quiet)                      # same day: not again
    assert calls == [url]
    page[0] = None
    notes.refresh(cfg, today=date(2026, 11, 13), log=quiet)                      # next day, the site is down
    s = notes.sources(cfg)[0]
    assert len(calls) == 2 and s.text == PAGE and "HTTP 404" in s.status
    page[0] = "<!doctype html><html>a 404 page served as 200</html>"
    notes.refresh(cfg, force=True, log=quiet)
    s = notes.sources(cfg)[0]
    assert s.text == PAGE and "HTML, not text" in s.status
    notes.store(url, "# Me\nA newer copy from a local build.", "a local build")
    s = notes.sources(cfg)[0]
    assert s.text.endswith("local build.") and "(a local build)" in s.status and s.error == ""


def test_background_and_material_include_the_sources(home, monkeypatch):
    monkeypatch.setattr(drafts, "load_config", lambda: {"notes": ["more.md"]})
    monkeypatch.setattr(drafts, "BACKGROUND_PATH", home / "background.md")   # no notes of their own yet
    monkeypatch.setattr(drafts, "resume_text", lambda profile: "")
    ada = Profile({"name": {"first": "Ada", "last": "Lovelace"}})
    assert drafts.has_material(ada)
    text = drafts.background(ada)
    assert text.index("## Profile") < text.index("## More from the candidate: more.md") < text.index("Jackson Freestyles")
    monkeypatch.setattr(drafts, "load_config", lambda: {"notes": []})
    assert not drafts.has_material(ada) and "More from the candidate" not in drafts.background(ada)
