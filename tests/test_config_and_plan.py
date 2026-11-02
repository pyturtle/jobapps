import yaml

from jobapps.config import Profile, set_yaml_value
from jobapps.plan import action, actions
from jobapps.rules import MonthYear, Resolution, Resolver

YAML = """# my profile
name:
  first: Ada               # given name
  last: TODO
phone: TODO                # e.g. 416
education:
  school: "UofT # main campus"   # quoted hash
  graduation_year: TODO
work_authorization:        # legal
  canada: TODO
  us: TODO
"""


def test_set_yaml_value_keeps_comments():
    out = set_yaml_value(YAML, "work_authorization.us", False)
    out = set_yaml_value(out, "education.graduation_year", 2028)
    out = set_yaml_value(out, "phone", "416-555-0100")
    out = set_yaml_value(out, "name.last", "Lovelace")
    out = set_yaml_value(out, "citizenship", ["Canada"])
    data = yaml.safe_load(out)
    assert data["work_authorization"] == {"canada": "TODO", "us": False}
    assert data["education"] == {"school": "UofT # main campus", "graduation_year": 2028}
    assert data["phone"] == "416-555-0100" and data["name"]["last"] == "Lovelace" and data["citizenship"] == ["Canada"]
    assert "# e.g. 416" in out and "# given name" in out and "# legal" in out and out.startswith("# my profile")


def test_plan_actions():
    f_text = {"id": "1", "type": "text", "label": "Phone", "inputType": "number", "required": True, "value": ""}
    assert action(f_text, Resolution("fill", "416-555-0100"))["text"] == "4165550100"
    f_date = {"id": "2", "type": "text", "label": "Grad", "placeholder": "MM/DD/YYYY", "value": ""}
    assert action(f_date, Resolution("fill", MonthYear(2028, 5)))["text"] == "05/15/2028"
    f_sel = {"id": "3", "type": "select", "label": "Gender", "options": ["Male", "Decline to self-identify"],
             "optionValues": ["m", "d"], "value": ""}
    a = action(f_sel, Resolution("fill", "decline"))
    assert (a["op"], a["value"], a["state"]) == ("select", "d", "filled")
    f_radio = {"id": "4", "type": "radio", "label": "Auth?", "options": ["Yes", "No"], "value": []}
    assert action(f_radio, Resolution("fill", True))["indexes"] == [0]
    assert action({**f_text, "value": "already"}, Resolution("fill", "x"))["op"] is None
    assert action(f_text, Resolution("manual", note="set phone"))["state"] == "missing"
    combo = action({"id": "5", "type": "combobox", "label": "Location", "value": ""},
                   Resolution("fill", ["Toronto, Ontario, Canada", "Toronto"], "location"))
    assert combo["op"] == "combobox" and combo["pick_first"] and combo["candidates"][1] == "Toronto"


def test_plan_uses_the_shared_resolver():
    p = Profile({"name": {"first": "Ada", "last": "L"}, "email": "a@x.com"})
    out = actions(Resolver(p, {"locations": []}), [
        {"id": "1", "type": "text", "label": "First Name *", "value": ""},
        {"id": "2", "type": "textarea", "label": "Why do you want this job?", "value": ""},
    ])
    assert out[0]["text"] == "Ada" and out[1]["state"] == "manual"   # optional + no draft yet -> left for you
