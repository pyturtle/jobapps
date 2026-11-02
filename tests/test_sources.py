from jobapps.sources import canonical_url, detect_ats, from_feed_item, listing_id, url_key


def test_canonical_url_strips_tracking():
    url = "https://jobs.lever.co/acme/2bad3e47-6c00-4ec2-94b5-feb4009619c9/apply?utm_source=Simplify&ref=Simplify"
    assert canonical_url(url) == "https://jobs.lever.co/acme/2bad3e47-6c00-4ec2-94b5-feb4009619c9/apply"


def test_url_key_unifies_greenhouse_variants():
    a = "https://job-boards.greenhouse.io/acme/jobs/5151569007?gh_jid=5151569007"
    b = "https://boards.greenhouse.io/acme/jobs/5151569007"
    c = "https://careers.acme.com/jobs?gh_jid=5151569007"
    assert url_key(a) == url_key(b) == url_key(c) == "greenhouse:5151569007"
    assert listing_id(a) == listing_id(c)


def test_url_key_ignores_apply_suffix():
    assert url_key("https://example.com/careers/123/apply") == url_key("https://www.example.com/careers/123")


def test_detect_ats():
    assert detect_ats("https://jobs.ashbyhq.com/1password/abc/application?embed=true") == "ashby"
    assert detect_ats("https://nvidia.wd5.myworkdayjobs.com/x") == "workday"
    assert detect_ats("https://www.psiquantum.com/apply?gh_jid=7761881003") == "greenhouse"
    assert detect_ats("https://example.com/jobs/1") == "other"


def test_from_feed_item_normalizes():
    item = {"company_name": " Dell ", "title": "Data  Science Intern", "url": "https://x.com/j/1?utm_source=Simplify",
            "locations": ["Round Rock, TX"], "terms": ["N/A"], "active": True, "is_visible": True,
            "date_posted": 1792909347, "category": "AI/ML/Data", "sponsorship": "Other", "degrees": ["Bachelor's"]}
    l = from_feed_item(item, "simplify")
    assert l["company"] == "Dell" and l["title"] == "Data Science Intern"
    assert l["url"] == "https://x.com/j/1" and l["terms"] == [] and l["active"]
    assert from_feed_item({"title": "x"}, "s") is None


def test_workday_identity_ignores_locale():
    a = "https://clio.wd3.myworkdayjobs.com/en-US/ClioCareerSite/job/Toronto/Software-Developer--Co-op_REQ-1577"
    b = "https://clio.wd3.myworkdayjobs.com/ClioCareerSite/job/Toronto/Software-Developer--Co-op_REQ-1577"
    assert url_key(a) == url_key(b) == "workday:clio:req-1577"
    assert detect_ats(a) == "workday"
    # the application wizard lives under /apply/... and must still be recognised as the same job
    assert url_key(f"{a}/apply/applyManually?source=LinkedIn") == "workday:clio:req-1577"
    assert url_key(f"{b}/apply/autofillWithResume") == url_key(f"{b}/apply") == "workday:clio:req-1577"


def test_board_helpers():
    from jobapps.sources import STUDENT_TITLE, _ago, _terms_from_title
    assert _terms_from_title("Software Engineer Intern - Summer 2027 (4 months)") == ["Summer 2027"]
    assert _terms_from_title("Co-op, Fall/Winter 2027") == ["Winter 2027"]
    assert _terms_from_title("Autumn 2027 Placement") == ["Fall 2027"]
    import time
    assert abs(_ago("Posted 3 Days Ago") - (time.time() - 3 * 86400)) < 5
    assert abs(_ago("Posted Today") - time.time()) < 5
    for t in ("Software Developer, Co-op", "ML Intern", "Summer Analyst", "PEY Student - Data", "Software Engineering Internship"):
        assert STUDENT_TITLE.search(t), t
    for t in ("Senior Software Engineer", "Internal Tools Engineer", "International Sales", "Battery Replacement Tech"):
        assert not STUDENT_TITLE.search(t), t


FRANCKLIN = """# Intern Radar
## Tech internships
<details><summary>By company</summary>

| Company | Open roles |
|---|---|
| Clio | 2 |
</details>

| Company | Role | Location | Details | Posted | Apply |
|---|---|---|---|---|---|
| Clio | NEW Software Developer, Co-op | Toronto, Ontario | Winter 2027 - 4 month term - $30/hr | Oct 23, 2026<br><sub>2 days old</sub> | <a href="https://clio.wd3.myworkdayjobs.com/en-US/ClioCareerSite/job/Toronto/Software-Developer--Co-op_REQ-1577"><strong>Apply</strong></a> | <!--id:322296-->
| Ciena | Software Intern (PEY) | CAN Kanata (2), ON - WR | 16 month term - Co-op enrollment | Oct 20, 2026<br><sub>5 days old</sub> | <a href="https://ciena.wd5.myworkdayjobs.com/Careers/job/Ottawa/Software-Intern_R123"><strong>Apply</strong></a> | <!--id:1-->
| Acme | Data Intern | Toronto | | Oct 20, 2026 | <a href="https://www.linkedin.com/jobs/view/1"><strong>Apply</strong></a> | <!--id:2-->
## Other internships
| Bank | Business Intern | Toronto | | Oct 20, 2026 | <a href="https://x.com/1">Apply</a> | <!--id:3-->
"""

SPEEDY = """<!-- TABLE_START -->
| Company | Position | Location | Posting | Age |
|---|---|---|---|---|
| <a href="https://www.nvidia.com"><strong>NVIDIA</strong></a> | Software Intern - Summer 2027 | Toronto, Canada | <a href="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/Toronto/SW-Intern_JR1"><img alt="Apply"/></a> | 2d |
| <a href="https://x.com"><strong>X</strong></a> | ML Intern | London, UK | <a href="https://x.com/j/2"><img alt="Apply"/></a> | 1d |
| <a href="https://y.com"><strong>Y</strong></a> | AI Intern | Vancouver, Canada +1 | <a href="https://jobs.ashbyhq.com/y/11111111-2222-3333-4444-555555555555"><img alt="Apply"/></a> | 10d |
<!-- TABLE_END -->
"""


def test_parse_francklin():
    from jobapps.sources import parse_francklin
    rows = parse_francklin(FRANCKLIN)
    assert [r["company_name"] for r in rows] == ["Clio", "Ciena"]            # summary table, LinkedIn row, non-tech skipped
    clio, ciena = rows
    assert clio["title"] == "Software Developer, Co-op" and clio["terms"] == ["Winter 2027", "4 months"]
    assert clio["locations"] == ["Toronto, Ontario"] and clio["date_posted"] > 0
    assert ciena["terms"] == ["16 months"] and ciena["locations"][0].startswith("CAN Kanata")


def test_parse_speedyapply_canada_only():
    from jobapps.sources import parse_speedyapply
    rows = parse_speedyapply(SPEEDY, "Canada")
    assert [r["company_name"] for r in rows] == ["NVIDIA", "Y"]
    assert rows[0]["terms"] == ["Summer 2027"] and rows[1]["locations"] == ["Vancouver, Canada"]


def test_parse_dreamwork_country_filter():
    from jobapps.sources import parse_dreamwork
    data = {"listings": [
        {"title": "SWE Intern", "company": "Rivianvw.Tech", "location": "Vancouver, BC", "locationCountryCode": "CA",
         "url": "https://jobs.ashbyhq.com/rivianvw.tech/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "postedAt": "2026-10-20T00:00:00Z"},
        {"title": "SWE Intern", "company": "Foo", "location": "Berlin", "locationCountryCode": "DE", "url": "https://x/1"},
    ]}
    rows = parse_dreamwork(data, "CA")
    assert len(rows) == 1 and rows[0]["company_name"] == "Rivianvw" and rows[0]["date_posted"] > 0


def test_canada_lookalikes():
    from jobapps.rules import is_canadian
    assert is_canadian("Waterloo, ON") and is_canadian("Vancouver, BC, Canada") and is_canadian("CAN, QC, Montreal")
    assert is_canadian("London, ON")
    assert not is_canadian("Waterloo, IA") and not is_canadian("Vancouver, WA") and not is_canadian("London, UK")
