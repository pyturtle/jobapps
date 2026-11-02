import random
from datetime import date, datetime

from jobapps.config import load_config
from jobapps.picker import evaluate, rank

TODAY = date(2026, 10, 25)


def listing(i, company="Acme", title="Software Engineer Intern", days_ago=1, ats="greenhouse",
            locations=("Toronto, ON, Canada",), **kw):
    posted = int(datetime(2026, 10, 25 - days_ago, 12).timestamp()) if days_ago < 25 else 0
    base = {"id": f"id{i:03d}", "source": "t", "company": company, "title": title, "url": f"https://x/{i}",
            "ats": ats, "locations": list(locations), "terms": ["Summer 2027"], "category": "Software",
            "sponsorship": "Other", "degrees": ["Bachelor's"], "date_posted": posted, "active": True}
    base.update(kw)
    return base


def test_ranking_is_deterministic_regardless_of_input_order():
    cfg = load_config()
    items = [listing(i, company=f"C{i % 7}", days_ago=i % 5, ats=["greenhouse", "workday", "other"][i % 3])
             for i in range(40)]
    first = [l["id"] for l, _ in rank(items, cfg, TODAY)]
    for seed in range(5):
        shuffled = items[:]
        random.Random(seed).shuffle(shuffled)
        assert [l["id"] for l, _ in rank(shuffled, cfg, TODAY)] == first


def test_filters():
    cfg = load_config()
    assert not evaluate(listing(1, days_ago=30), cfg, TODAY).eligible
    assert not evaluate(listing(2, title="PhD Research Intern"), cfg, TODAY).eligible
    assert not evaluate(listing(3, title="Mechanical Engineering Intern"), cfg, TODAY).eligible
    assert not evaluate(listing(4, sponsorship="U.S. Citizenship is Required"), cfg, TODAY).eligible
    assert not evaluate(listing(5, locations=["London, UK"]), cfg, TODAY).eligible
    assert evaluate(listing(6, locations=["London, UK", "NYC"]), cfg, TODAY).eligible
    assert not evaluate(listing(7, terms=["Summer 2026"]), cfg, TODAY).eligible
    assert not evaluate(listing(8, degrees=["Master's", "PhD"]), cfg, TODAY).eligible


def test_scoring_prefers_fresh_readable_toronto_roles():
    cfg = load_config()
    toronto = evaluate(listing(1), cfg, TODAY)
    us = evaluate(listing(2, locations=["Austin, TX"]), cfg, TODAY)
    workday = evaluate(listing(3, ats="workday"), cfg, TODAY)
    old = evaluate(listing(4, days_ago=10), cfg, TODAY)
    assert toronto.score > us.score
    assert toronto.score > workday.score
    assert toronto.score > old.score
    assert any("Toronto area" in r for r in toronto.reasons)
