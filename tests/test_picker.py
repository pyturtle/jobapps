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
    # a bare season next to a past dated one is that same past term (feeds tag ["Summer 2026", "Summer"])
    assert not evaluate(listing(9, terms=["Summer 2026", "Summer"]), cfg, TODAY).eligible
    assert not evaluate(listing(10, terms=["Fall 2025"]), cfg, TODAY).eligible
    assert evaluate(listing(11, terms=["Summer 2026", "Summer 2027"]), cfg, TODAY).eligible
    assert evaluate(listing(12, terms=["Summer"]), cfg, TODAY).eligible


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


def test_titles_in_french_and_without_the_word_engineer_pass_the_title_filter():
    cfg = load_config()
    for title in ("Stagiaire en développement logiciel", "Stagiaire developpeur(se) de logiciels",
                  "Stagiaire en programmation UI", "Stagiaire - Intelligence artificielle",
                  "Artificial Intelligence Scientist Intern", "Computer Science Co-op (Winter 2027)",
                  "Computer Vision Intern", "Robotics Co-op", "Embedded Systems Intern"):
        assert evaluate(listing(1, title=title), cfg, TODAY).eligible, title
    for title in ("Business Development Intern", "Finance Co-op", "Stagiaire en conception de formation",
                  "Program Manager Intern", "Financial Analyst Intern"):
        assert evaluate(listing(1, title=title), cfg, TODAY).reason == "title matches no include pattern", title
    # the exclusions still apply to a title the new patterns let in
    assert not evaluate(listing(1, title="Stage - Ingenieur developpement FPGA"), cfg, TODAY).eligible


def test_a_place_abroad_is_excluded_by_its_name_not_by_a_lookalike():
    cfg = load_config()
    for place in ("Indianapolis, IN", "Albuquerque, New Mexico", "London, ON", "London, Ontario, Canada"):
        assert evaluate(listing(1, locations=[place]), cfg, TODAY).eligible, place
    for place in ("Bangalore, India", "London, UK", "London", "Mexico City, Mexico"):
        assert evaluate(listing(1, locations=[place]), cfg, TODAY).reason == "all locations excluded", place


def test_every_canadian_place_scores_as_canada():
    cfg = load_config()  # the same test as the cap on postings outside Canada (rules.is_canadian)
    abroad = evaluate(listing(1, locations=["Austin, TX"]), cfg, TODAY).score
    for place in ("Vancouver, British Columbia", "Montreal", "CA-BC-VANCOUVER", "Ottawa", "Calgary, Alberta",
                  "Remote, Canada"):
        v = evaluate(listing(1, locations=[place]), cfg, TODAY)
        assert v.score == abroad + 24 and any("in Canada" in r for r in v.reasons), place
    for place in ("Markham", "Waterloo, ON", "Richmond Hill, Ontario"):
        v = evaluate(listing(1, locations=[place]), cfg, TODAY)
        assert v.score == abroad + 30 and any("Toronto area" in r for r in v.reasons), place
    for place in ("Markham, IL", "Waterloo, IA", "Vancouver, WA", "Ontario, CA"):
        assert evaluate(listing(1, locations=[place]), cfg, TODAY).score == abroad, place
    remote = evaluate(listing(1, locations=["Remote in USA"]), cfg, TODAY)
    assert remote.score == abroad + 4 and any("remote" in r for r in remote.reasons)


def test_postings_from_the_start_of_the_cycle_stay_eligible():
    cfg = load_config()  # big employers post in Aug-Sept and keep postings open for months
    old = listing(1, days_ago=0, date_posted=int(datetime(2026, 9, 5, 12).timestamp()))
    assert evaluate(old, cfg, TODAY).eligible


def test_company_names_match_divisions_and_initials_not_lookalikes():
    from jobapps.picker import named
    names = ["Amazon", "RBC", "Meta", "Apple"]
    assert named("Amazon.com Services LLC", names)
    assert named("Royal Bank of Canada", names)
    assert named("Meta", names)
    assert not named("Metabase", names)
    assert not named("Applied Intuition", names)

    cfg = load_config()
    cfg["scoring"]["favorite_companies"] = {"points": 20, "names": ["Amazon"]}
    fav = evaluate(listing(1, company="Amazon Web Services"), cfg, TODAY)
    other = evaluate(listing(2, company="Amazonia Tech"), cfg, TODAY)
    assert fav.score == other.score + 20
    assert any("favorite company" in r for r in fav.reasons)
