from jobapps import forms
from jobapps.forms import _board_guesses, html_to_text, parse_lever_form


def test_board_guesses():
    assert _board_guesses("MongoDB")[0] == "mongodb"
    assert _board_guesses("Figure AI") == ["figureai", "figure-ai", "figure"]
    assert _board_guesses("Acme Technologies Inc.")[0] == "acme"

LEVER = """
<form>
<li class="application-question resume"><label><div class="application-label">Resume/CV <span class="required">✱</span></div>
<div class="application-field"><input type="file" name="resume"></div></label></li>
<li class="application-question"><label><div class="application-label">Full name<span class="required">✱</span></div>
<div class="application-field"><input type="text" name="name" required></div></label></li>
<li class="application-question custom-question"><div class="application-label full-width">Are you legally authorized to work in the United States?<span class="required">✱</span></div>
<div class="application-field full-width"><ul data-qa="multiple-choice">
<li><label><input type="radio" name="cards[abc][field0]" value="Yes"><span class="application-answer-alternative">Yes</span></label></li>
<li><label><input type="radio" name="cards[abc][field0]" value="No"><span class="application-answer-alternative">No</span></label></li>
</ul></div></li>
<li class="application-question"><div class="application-label">I identify my ethnicity as</div>
<select name="surveysResponses[x][responses][field1]"><option value="">Select ...</option>
<option value="Asian">Asian</option><option value="Decline to self-identify">Decline to self-identify</option></select></li>
<li class="application-question"><div class="application-label">Why us?</div><textarea name="cards[def][field0]"></textarea></li>
</form>
"""


def test_parse_lever_form():
    fields = {f.label: f for f in parse_lever_form(LEVER)}
    assert fields["Resume/CV"].type == "file" and fields["Resume/CV"].required
    assert fields["Full name"].type == "text" and fields["Full name"].key == "name"
    auth = fields["Are you legally authorized to work in the United States?"]
    assert auth.type == "radio" and auth.options == ["Yes", "No"] and auth.required
    eth = fields["I identify my ethnicity as"]
    assert eth.type == "select" and eth.options == ["Asian", "Decline to self-identify"]
    assert fields["Why us?"].type == "textarea"


def test_html_to_text():
    html = "&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;One&lt;/li&gt;&lt;li&gt;Two&lt;/li&gt;&lt;/ul&gt;"
    text = html_to_text(html)
    assert "Hello & welcome" in text and "- One" in text and "- Two" in text
    # an escaped closing tag inside a script doesn't end it early, and escaped scripts go too
    page = ('<p>Before</p><script>var s = "&lt;/script&gt;"; var hidden = 1;</script><p>After</p>'
            "&lt;script&gt;alert(2)&lt;/script&gt;")
    assert html_to_text(page) == "Before\nAfter"
    assert html_to_text("<ul><li><i></i></li><li> </li><li>Real\r\n item</li></ul>") == "- Real\nitem"


POSTING = ("About the role\nYou will build internal tools with our platform team.\n"
           "Qualifications\n- Pursuing a bachelor's degree in Computer Science\n- Experience with Python and React\n"
           "Responsibilities\n- Ship features end to end with your mentor during the internship.\n") * 3


def test_a_page_is_read_as_one_posting_or_not_at_all(monkeypatch):
    html = lambda body: "<html><body><nav><p>Jobs</p></nav>" + body + "</body></html>"
    as_html = "".join(f"<p>{line}</p>" for line in POSTING.split("\n"))
    blob = '<code style="display:none">{"themeOptions": {' + '"k": "requirements", ' * 9000 + '"z": 1}}</code>'
    # data cut up by the tags inside it: a line that starts mid-string, and a string's text with its escapes
    tail = ('<code style="display:none">", "navbarConfig": {"enable": false, "links": ["a", "b"]}, "consent": '
            '"Applicants must be citizens."</code>')
    escaped = "<p>You must hold a security clearance." + chr(92) + "u200b " + chr(92) + "n</p>"
    pages = {"https://acme.example/job/1": html(as_html + blob + tail + escaped),
             "https://acme.example/careers": html(as_html * 60)}  # every opening on one page
    monkeypatch.setattr(forms.net, "get_text", lambda url, **kw: pages[url])
    text = forms.page_description("https://acme.example/job/1")
    assert "Qualifications" in text and "themeOptions" not in text and len(text) < 2000
    assert not any(word in text for word in ("navbarConfig", "citizens", "clearance"))
    assert forms.page_description("https://acme.example/careers") == ""
    assert not forms.looks_like_posting(POSTING * 80)


def test_a_stored_careers_page_is_dropped_when_the_posting_cannot_be_read(tmp_path, monkeypatch):
    from jobapps import db, pipeline
    conn = db.connect(tmp_path / "t.db")
    job = {"id": "j1", "ats": "other", "url": "https://acme.example/job/1"}
    db.save_form(conn, "j1", "page", POSTING * 80, [], "")
    monkeypatch.setattr(forms, "page_description", lambda url: "")
    assert pipeline.description_for(conn, job) == ""
    assert db.get_form(conn, "j1")["description"] == ""
    db.save_form(conn, "j1", "page", "Menu - Sign in", [], "")  # short page text is kept: the extension may have saved it
    assert pipeline.description_for(conn, job) == "" and db.get_form(conn, "j1")["description"] == "Menu - Sign in"
