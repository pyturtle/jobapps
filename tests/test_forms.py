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
