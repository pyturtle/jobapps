"""filler.js on a Workday-style page (tests/fixtures/workday_mock.html mirrors real 2026 Workday markup):
split dates, button dropdowns, search boxes with chips and numbered blocks come out as the right fields."""
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parent.parent
FILLER = (ROOT / "jobapps" / "filler.js").read_text(encoding="utf-8")
MOCK = (Path(__file__).parent / "fixtures" / "workday_mock.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser installed on this machine
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


def scan(browser, step):
    page = browser.new_page()
    page.set_content(MOCK)
    for _ in range(step):
        page.click("#next")
    if step == 1:  # My Experience: one block each, as after pressing Add
        for b in page.locator("[data-automation-id=add-button]").all():
            b.click()
        page.wait_for_timeout(600)
    page.add_script_tag(content=FILLER)
    fields = page.evaluate("window.__jobapps.scan()")
    page.close()
    return {(f["section"], f["label"]): f for f in fields}


def test_my_information(browser):
    f = scan(browser, 0)
    assert f[("", "Country")]["type"] == "dropdown" and f[("", "Country")]["required"]
    assert f[("", "Phone Device Type")]["value"] == ""                 # "Select One" is no value
    assert f[("", "Country Phone Code")]["type"] == "multiselect"
    assert f[("", "How Did You Hear About Us?")]["value"] == []
    assert f[("", "I have a preferred name")]["type"] == "boolean"
    assert f[("", "Have you previously worked for MockCo?")]["options"] == ["Yes", "No"]
    assert not any(k[1] == "" for k in f)                             # the hidden mirror inputs are not fields


def test_my_experience_blocks(browser):
    f = scan(browser, 1)
    assert f[("Work Experience 1", "Job Title")]["type"] == "text"
    assert f[("Work Experience 1", "I currently work here")]["type"] == "boolean"
    assert f[("Work Experience 1", "From")]["type"] == "date" and f[("Work Experience 1", "From")]["parts"] == ["month", "year"]
    assert f[("Education 1", "From")]["parts"] == ["year"]
    assert f[("Education 1", "Degree")]["type"] == "dropdown"
    assert f[("Education 1", "Field of Study")]["type"] == "multiselect"
    assert f[("Websites 1", "URL")]["required"]
    assert f[("", "Type to Add Skills")]["type"] == "multiselect"
    assert f[("", "Resume/CV")]["type"] == "file"


def test_question_dropdowns_take_their_legend(browser):
    f = scan(browser, 2)
    assert ("", "Are you legally authorized to work in Canada?") in f
    assert all(x["type"] == "dropdown" and x["required"] for x in f.values())


PLAIN = """<form>
  <label for="n">Full name</label><input id="n">
  <label for="m">First major Question First major</label><input id="m">
  <fieldset><legend>References</legend>
    <label for="rn">Name</label><input id="rn">
    <label for="re">Email</label><input id="re" type="email">
  </fieldset>
  <fieldset><legend>Do you have references we may contact?</legend>
    <label><input type="radio" name="r" value="y">Yes</label><label><input type="radio" name="r" value="n">No</label>
  </fieldset>
</form>"""


def test_blocks_about_someone_else_and_doubled_labels(browser):
    page = browser.new_page()
    page.set_content(PLAIN)
    page.add_script_tag(content=FILLER)
    f = {(x["section"], x["label"]) for x in page.evaluate("window.__jobapps.scan()")}
    page.close()
    assert {("", "Full name"), ("", "First major")} <= f               # the screen-reader copy is dropped
    assert {("References", "Name"), ("References", "Email")} <= f      # whose name: the heading says
    assert ("", "Do you have references we may contact?") in f         # a question, not a block
