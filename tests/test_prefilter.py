"""Sep 30: the routine made no posts because its shortlist was birthdays, anniversaries, promo
and site section pages; Claude rightly skipped them all. The page playbook's prefilter drops those
before selection and boosts the kinds editors approve."""
import time

import pytest

from bore.collect import section_page
from bore.config import Page
from bore.select import select_page

NOW = time.time()
# made-up playbooks' prefilters, of the shape the onboarding routine writes (playbook.prefilter)
PLAYBOOKS = {
    "demo-celeb": {"prefilter": {
        "skip": [r"\b\d+ years ago today\b", r"\bhappy \d+\w* birthday\b", r"\bnational [\w ]{1,30} day\b",
                 r"\b(looking|looks) (breathtaking|stunning) on the red carpet\b", r"\bwill star in\b"],
        "boost": [r"\b(arrested|charged|sentenced)\b"]}},
    "demo-sports": {"prefilter": {
        "skip": [r"\bon their way to\b", "🎧", r"\bpodcast\b", r"\bpractice squad\b"],
        "boost": [r"\b(ruled out|injur(y|ed)|sprain)\b"]}},
}


@pytest.fixture(autouse=True)
def _playbooks(monkeypatch):
    monkeypatch.setattr("bore.routine.load_playbook", lambda slug: PLAYBOOKS.get(slug, {}))


def _items(texts, page):
    out = []
    for i, t in enumerate(texts):
        for a in ("starwire", "popdesk") if page == "demo-celeb" else ("samreporter", "gridironwire"):
            out.append({"id": f"{i}{a}", "platform": "x", "author": a, "text": t, "created_at": NOW - 1800,
                        "engagement": 5000})
    return out


def test_demo_celeb_junk_is_dropped_and_approved_kinds_rise(cfg):
    page = Page.from_dict({"slug": "demo-celeb", "name": "Demo Celeb"})
    texts = ["12 years ago today, Ariana Grande and The Weeknd released Love Me Harder, a huge single then",
             "Happy 29th birthday to the talented Max Verstappen, one of the fastest drivers ever",
             "Today is National Love People Day, so share some love with the people around you",
             "Anne Hathaway looking breathtaking on the red carpet at the VERITY premiere in New York City",
             "Jennifer Lawrence will star in Zach Cregger's sci-fi thriller The Flood from Universal",
             "Rapper Big Star arrested on felony charges in Miami after a traffic stop on Tuesday night"]
    res = select_page(page, _items(texts, "demo-celeb"), cfg, [], NOW, quota=2)
    heads = [t.headline for t in res.shortlist]
    assert len(heads) == 1 and "arrested" in heads[0]
    assert sum(1 for x in res.suppressed if x.get("why", "").startswith("prefilter")) == 5


def test_attending_court_is_news_not_a_red_carpet(cfg):
    page = Page.from_dict({"slug": "demo-celeb", "name": "Demo Celeb"})
    res = select_page(page, _items(["Singer Big Star attends court hearing as the fraud trial opens in Los Angeles"],
                                   "demo-celeb"), cfg, [], NOW, quota=2)
    assert len(res.shortlist) == 1


def test_demo_sports_logistics_and_podcasts_are_dropped(cfg):
    page = Page.from_dict({"slug": "demo-sports", "name": "Demo Sports"})
    texts = ["The Commanders are on their way to London for Sunday's game against the Colts",
             "It is time to push the panic button on Patriots QB Drake Maye? With Ty Schmit 🎧",
             "Jets are signing LB Nick Dawkins to the practice squad, per sources familiar with it",
             "Bears QB Caleb Williams has been ruled out for Sunday with a high ankle sprain, per sources"]
    res = select_page(page, _items(texts, "demo-sports"), cfg, [], NOW, quota=2)
    assert [t.headline[:30] for t in res.shortlist] == ["Bears QB Caleb Williams has be"]


def test_section_pages_are_not_stories():
    assert section_page({"title": "NFL Scores, News & Stats | Latest Super Bowl News"})
    assert section_page({"title": "Articles filed under NFL"})
    assert not section_page({"title": "Christian McCaffrey Injured! Season In Jeopardy After PCL Injury"})


def test_service_pieces_from_websites_are_not_news():
    from bore.collect import service_piece
    web = lambda t: {"platform": "web", "title": t}
    for t in ("Where to watch Under the Skin? Scarlett Johansson's thriller is streaming free",
              "Chicago Fire season 15 episode 2: Release date and time, where to watch and more",
              "Meet the cast of The Marlow Murder Club season 3", "Severance season 2 ending, explained",
              "SNL season 52 premiere recap"):
        assert service_piece(web(t)), t
    for t in ("Blueface proposes to Chrisean Rock at a nightclub", "Tobey Maguire officiates ex-wife's wedding",
              "Kelly Clarkson slams Brooklyn shop", "How old was Valkyrae's late nephew?"):
        assert not service_piece(web(t)), t
    assert not service_piece({"platform": "x", "title": "where to watch the game"})


def test_more_service_pieces_and_daily_almanacs_are_not_news():
    from bore.collect import service_piece
    web = lambda t: {"platform": "web", "title": t}
    for t in ("Law & Order: SVU season 28 episode 2: Release schedule and more",
              "Is there going to be The Example Club season 4? Renewal status and more explored",
              "When will The New Example Experiment premiere on Netflix?",
              "When is the next rocket launching in Florida? Dates, times and details to know",
              "What time is The Example Club season 3 finale arriving on TV? Global release time",
              "Love Island season 8: Meet the singles, bombshells and returning couples",
              "Example 2026 soundtrack: A complete guide to songs featured in the series",
              "What does the Moon look like today? Phase, visibility and viewing details for October 12",
              "Is Example Island season 1 based on a novel? What the adaptation changes",
              "What is The Example Trap all about? Inside the mystery drama",
              "Does the detective survive the shooting? The shocking twist behind his fate explained",
              "Example Town season 4's ending makes one storyline much more important than it seemed",
              "10 best Halloween episodes from popular sitcoms of all times",
              "Orionid meteor shower 2026: When to watch and how to see it",
              "5 movies to watch if you like The Example Hypothesis"):
        assert service_piece(web(t)), t
    for t in ("Actor's new Netflix movie faces mixed reception as its audience score hits a career low",
              "Reality star dies at 46 as the show's host honors her with an emotional tribute",
              "Actress arrested after allegedly trying to open a plane door mid-flight",
              "What happened to the hiker found dead in the mountains? Inside the case",
              "Who is the billionaire who married the actor's ex-wife? All about him",
              "RHOP season 11 episode 1: Wendy says the arrest aftermath has been the darkest time"):
        assert not service_piece(web(t)), t


CELEB_PB = {"niche": "Celebs, entertainment, viral moments and true crime",
            "worth_posting": "YES: a named celebrity moment people will react to. NO: press-release promos, sports "
                             "results, politics/policy, disasters without a human-interest angle, anything stale. "
                             "LEARNED FROM EDITOR REVIEWS: APPROVED KINDS: a celebrity clashing with a politician."}
NFL_PB = {"niche": "NFL news and conversation",
          "worth_posting": "YES: injury news for notable players. NO: college football, betting lines, pure "
                           "promotional content, stale news."}


def _web(titles, hours_old=1.0):
    return [{"id": f"w{i}", "platform": "web", "author": "example.com", "url": f"https://example.com/{i}",
             "title": t, "text": "", "created_at": NOW - hours_old * 3600, "engagement": 0}
            for i, t in enumerate(titles)]


def test_topic_rules_follow_the_playbooks_no_list():
    from bore.select import topic_rules
    assert set(topic_rules(CELEB_PB)) == {"politic", "disaster"}
    assert set(topic_rules(NFL_PB)) == {"promotional"}
    assert topic_rules({"worth_posting": "YES: politics and policy debates. NO: stale news."}) == {}
    assert topic_rules({}) == {}


def test_politics_and_storms_leave_but_stars_people_and_personal_moments_stay(cfg, monkeypatch):
    """Oct 9-13 the routine skipped every politics and storm story it was briefed on (Demo Celeb:
    a third of its briefs with the service pieces). A star clashing with a politician is an
    approved kind, so a politician's name alone, or any entertainment or human angle, keeps it."""
    monkeypatch.setattr("bore.routine.load_playbook", lambda slug: CELEB_PB)
    page = Page.from_dict({"slug": "demo-celeb", "name": "Demo Celeb"})
    dropped = ["Federal judge orders halt to taxpayer-funded campaign ads ahead of the midterms",
               "Immigration operation launched at a Democratic congresswoman's home on Tuesday",
               "Hurricane Delta makes landfall as a Category 2 storm on the Gulf Coast"]
    kept = ["Writer Joe Example slams the President's new press secretary over deleted posts",
            "Comedian Sam Example mocks the Senate during his late-night monologue",
            "Former Senate leader marks his 34th wedding anniversary with a sweet message",
            "Video shows a firefighter rescuing a dog as the hurricane floods a Texas town"]
    res = select_page(page, _web(dropped + kept), cfg, [], NOW, quota=3)
    assert {t.headline for t in res.shortlist} == set(kept)
    why = {x["headline"][:40]: x["why"] for x in res.suppressed}
    assert why[dropped[0][:40]] == why[dropped[1][:40]] == "prefilter: politic (the playbook's NO list)"
    assert why[dropped[2][:40]] == "prefilter: disaster (the playbook's NO list)"


def test_no_topic_filter_without_the_playbook_saying_so(cfg, monkeypatch):
    monkeypatch.setattr("bore.routine.load_playbook", lambda slug: {"niche": "News"})
    page = Page.from_dict({"slug": "demo-news", "name": "Demo News"})
    res = select_page(page, _web(["Federal judge orders halt to taxpayer-funded campaign ads ahead of the midterms"]),
                      cfg, [], NOW, quota=2)
    assert len(res.shortlist) == 1


def test_a_quote_further_down_a_post_is_not_its_topic():
    from bore.select import TOPIC_RX, excluded_topic
    topics = {"politic": TOPIC_RX["politic"]}
    post = 'Joe Example says the CEO seemed great before he played him:\n\n"I remember him testifying to Congress"'
    assert excluded_topic(post, post, topics, []) is None
    assert excluded_topic(post.replace("\n", " "), "", topics, []) == "politic"


def test_nfl_page_drops_web_results_without_football_and_show_plugs(cfg, monkeypatch):
    monkeypatch.setattr("bore.routine.load_playbook", lambda slug: NFL_PB)
    page = Page.from_dict({"slug": "demo-nfl", "name": "Demo NFL"})
    items = _web(["A software company now expects 70 billion dollars in revenue by the end of next year",
                  "Water cannon and teargas used on protesters in a European capital overnight",
                  "Jeanty limited in practice Wednesday with an ankle issue, per the team",
                  "Ravens add a veteran receiver after a strong workout on Tuesday afternoon"])
    items += [{"id": "x1", "platform": "x", "author": "reporter", "created_at": NOW - 1800, "engagement": 40,
               "text": "Big night ahead for the whole city, can't wait for kickoff on Sunday everyone"},
              {"id": "x2", "platform": "x", "author": "network", "created_at": NOW - 1800, "engagement": 40,
               "text": "New edition of The Example Report streams tonight at 7p ET with the whole crew"}]
    res = select_page(page, items, cfg, [], NOW, quota=3)
    assert {t.headline[:20] for t in res.shortlist} == {"Jeanty limited in pr", "Ravens add a veteran",
                                                        "Big night ahead for "}
    whys = sorted(x["why"] for x in res.suppressed)
    assert whys == ["prefilter: off-niche web result (no football in it)"] * 2 + \
        ["prefilter: promotional (the playbook's NO list)"]


def test_true_crime_courts_and_white_house_clashes_stay(cfg, monkeypatch):
    """A verdict or sentence is the true-crime kind editors approve even in a federal court, and the
    White House trading shots with a star is the star's clash: both reach the routine."""
    monkeypatch.setattr("bore.routine.load_playbook", lambda slug: CELEB_PB)
    page = Page.from_dict({"slug": "demo-celeb", "name": "Demo Celeb"})
    kept = ["Federal judge sentences Joe Example to 50 months in prison",
            "Supreme Court rejects Sam Example's appeal of his conviction",
            "White House fires back at Joe Example after his gala speech",
            "Rock & Roll Hall of Fame 2027 ballot revealed"]
    res = select_page(page, _web(kept + ["Federal judge orders halt to taxpayer-funded campaign ads"]), cfg, [], NOW,
                      quota=3)
    assert {t.headline for t in res.shortlist} == set(kept)
    assert [x["why"] for x in res.suppressed] == ["prefilter: politic (the playbook's NO list)"]


def test_nfl_web_results_naming_only_the_people_stay(cfg, monkeypatch):
    """A play or a coach's quote from a web search often names only the people: with no sign of
    another subject it is NFL news, not off-niche."""
    monkeypatch.setattr("bore.routine.load_playbook", lambda slug: NFL_PB)
    page = Page.from_dict({"slug": "demo-nfl", "name": "Demo NFL"})
    kept = ["Joe Example dots Sam Example from DEEP to get the drive going",
            "Joe Example: 'Really proud of our group getting to 5-0'",
            "Sam Example's 53-yard kick return sets up the game-winning FG"]
    res = select_page(page, _web(kept + ["Coastal flooding turns streets into rivers in a beach town"]), cfg, [], NOW,
                      quota=3)
    assert {t.headline for t in res.shortlist} == set(kept)
    assert [x["why"] for x in res.suppressed] == ["prefilter: off-niche web result (no football in it)"]
