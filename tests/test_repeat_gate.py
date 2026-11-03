"""The upload repeat gate on real drafts (Oct 11-21): it threw away finished posts about different
stories (12 skips in 7 days, 9 of them called false positives by the session) because page hashtags
(#TrueCrime #Justice) and sentence-initial words ("Police said...") counted as shared names. The
fixture pairs were labelled by reading both captions: different stories must pass, real repeats
(the same news again) must still be stopped."""
import json
import types

import pytest

from bore.config import Page
from bore.dupes import Deduper, draft_text, sentence_capitals, story_text

from conftest import FIXTURES

PAIRS = json.loads((FIXTURES / "repeat_pairs_20261021.json").read_text())
# the reporters and outlets quoted in these drafts (as in tests/test_repeats.py)
PAGES = {
    "huddle": Page.from_dict({"slug": "huddle", "name": "Huddle Room", "leagues": ["nfl"], "x_handles": [
        "adamschefter", "rapsheet", "tompelissero", "mikegarafolo", "jfowlerespn", "diannaespn", "nflnetwork",
        "profootballtalk", "aroundthenfl", "espnnfl", "nfl", "brgridiron"]}),
    "culture-rush": Page.from_dict({"slug": "culture-rush", "name": "Culture Rush", "leagues": ["entertainment"],
                                    "x_handles": [
        "popcrave", "popbase", "tmz", "variety", "thr", "deadline", "people", "pagesix", "enews", "culturecrave",
        "discussingfilm", "etnow", "thewrap", "billboard", "dexerto", "courttv"]}),
}
# The live Huddle follows about 30 accounts, most of them reporters whose handles hold common first
# names: with those, 'Jordan Elliott' lost 'jordan' and a real repeat got through (Oct 21 review).
# Made-up reporters and outlets of the same shape (never the real source list).
LIVE_SHAPED = {
    "huddle": Page.from_dict({"slug": "huddle", "name": "Huddle Room", "leagues": ["nfl"], "x_handles": [
        *PAGES["huddle"].x_handles, "jordanpike", "mikehollis", "mikebrandt", "jeffbarlow", "danquinlan",
        "cameronreed", "tomkessler", "joshtrent", "jaycolby", "adamvance", "briankeogh", "kalebmoss",
        "jalenreports", "justinweekly", "dkessler", "gridironwire", "footballdesk", "sundayfilmroom", "pigskinreport"]}),
    "culture-rush": Page.from_dict({"slug": "culture-rush", "name": "Culture Rush", "leagues": ["entertainment"]}),
}


def _live_pages():
    from bore.config import PRIVATE, load_pages
    if not (PRIVATE / "pages.yaml").exists():
        pytest.skip("needs the pages' private config (run `python -m bore.cli studio sync`)")
    return {p.slug: p for p in load_pages(PRIVATE / "pages.yaml")}


CONFIGS = {"sep29": lambda: PAGES, "live-shaped": lambda: LIVE_SHAPED, "live": _live_pages}


def _id(pair):
    return f"{pair[0]['headline'][:30]} ~ {pair[1]['headline'][:30]}"


@pytest.mark.parametrize("config", CONFIGS)
@pytest.mark.parametrize("pair", PAIRS["different"], ids=_id)
def test_different_stories_pass(pair, config):
    new, old = pair
    assert not Deduper(CONFIGS[config]()[new["page"]], [old]).match(draft_text(new))


@pytest.mark.parametrize("config", CONFIGS)
@pytest.mark.parametrize("pair", PAIRS["same"], ids=_id)
def test_real_repeats_are_still_stopped(pair, config):
    new, old = pair
    assert Deduper(CONFIGS[config]()[new["page"]], [old]).match(draft_text(new))


def test_a_first_name_inside_a_reporters_handle_is_still_a_name():
    """'jordan' is inside @jordanpike: Jordan Elliott keeps it, Jordan Pike (the reporter) does not
    count, nor do reporters named in full without 'per' (Mike Hollis, Dan Quinlan) or by handle."""
    dd = Deduper(LIVE_SHAPED["huddle"], [])
    f = dd.features_of("The Titans have released DT Jordan Elliott, Jordan Pike reports.\n\n"
                       "Mike Hollis and Dan Quinlan say a teammate was involved. @JordanPike has more.")
    assert {"jordan", "elliott"} <= f.names and not ({"pike", "mike", "hollis", "dan", "quinlan", "jordanpike"} & f.names)
    new, old = next(p for p in PAIRS["same"] if p[0]["headline"] == "FIREARM BEHIND ELLIOTT RELEASE")
    assert Deduper(LIVE_SHAPED["huddle"], [old]).match(draft_text(new)).names == ["elliott", "jordan"]


def test_team_cities_are_names_off_sports_pages_unless_a_team_follows():
    """Culture Rush: Whitney Houston, Draymond Green and a Tampa crime story keep their names; a
    'Green Bay Packers' is a team anywhere. On a sports page the cities say nothing."""
    cr, hr = Deduper(LIVE_SHAPED["culture-rush"], []), Deduper(LIVE_SHAPED["huddle"], [])
    assert "houston" in cr.features_of("The estate of Whitney Houston sued the label on Monday.").names
    assert {"draymond", "green", "seth"} <= cr.features_of("Draymond Green and Seth Green met at the show.").names
    assert "tampa" in cr.features_of("The man who shot a deputy in Tampa was charged, and Tampa police said more.").names
    love = "The Green Bay Packers star Jordan Love threw three touchdowns."
    assert not ({"green", "bay"} & cr.features_of(love).names) and {"jordan", "love"} <= cr.features_of(love).names
    assert not ({"houston", "dallas"} & hr.features_of("The Cowboys beat the Texans in Houston, as Dallas hoped.").names)


def test_hashtags_reporters_and_sentence_starts_are_not_story_names():
    dd = Deduper(PAGES["huddle"], [])
    f = dd.features_of("Police said the man was arrested. Officers found a gun, per Adam Schefter and Ian Rapoport.\n\n"
                       "#TrueCrime #Justice #NFL")
    assert not ({"police", "officer", "crime", "true", "justice", "adam", "ian", "rapoport"} & f.names)
    assert "Schefter" not in story_text("Bears are 3-0, per Adam Schefter.")
    assert sentence_capitals("Jalen Hurts is out. Hurts said the Eagles told him.") == frozenset()   # a name, not a start
    assert "police" in sentence_capitals("Police said he fled.")


def test_a_story_mentioning_an_earlier_one_is_not_that_story():
    """Both headlines name someone, and the stories share only the other headline's person."""
    new, old = next(p for p in PAIRS["different"] if p[0]["headline"] == "FRANKLIN REPORTEDLY SUSPENDED")
    assert "Gardner" in new["caption"] and not Deduper(PAGES["huddle"], [old]).match(draft_text(new))


def test_the_upload_gate_passes_a_different_crime_story_and_stops_a_repeat(cfg, monkeypatch):
    from bore import cli
    monkeypatch.setattr("bore.config.load_pages", lambda *a: list(PAGES.values()))
    deputy, tupac = next(p for p in PAIRS["different"] if p[1]["headline"].startswith("TUPAC"))
    dash = types.SimpleNamespace(recent_drafts=lambda h: [tupac])
    brief = {"id": "culture-rush-202610211310-x", "page": "culture-rush"}
    lines, _, caption = draft_text(deputy).partition("\n")
    cli._repeat_gate(brief, {"headline_lines": [lines], "caption": caption}, dash, cfg, None)     # made
    new, old = next(p for p in PAIRS["same"] if p[0]["headline"].startswith("FRANKLIN"))
    dash = types.SimpleNamespace(recent_drafts=lambda h: [old])
    lines, _, caption = draft_text(new).partition("\n")
    with pytest.raises(SystemExit, match="repeats a recent post"):
        cli._repeat_gate({"id": "huddle-202610211600-x", "page": "huddle"}, {"headline_lines": [lines], "caption": caption},
                         dash, cfg, None)
