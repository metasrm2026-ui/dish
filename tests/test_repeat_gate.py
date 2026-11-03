"""The upload repeat gate: page hashtags (#TrueCrime #Justice), sentence-initial words ("Police
said...") and the reporters a page follows never count as shared story names, so a different story
is never thrown away as a repeat, while the same news again still is."""
import pytest

from bore.config import Page
from bore.dupes import Deduper, draft_text, sentence_capitals, story_text

# made-up reporters and outlets (handles holding common first names, as real ones do)
PAGES = {
    "demo-sports": Page.from_dict({"slug": "demo-sports", "name": "Demo Sports", "leagues": ["nfl"], "x_handles": [
        "samreporter", "ianinsider", "gridironwire", "footballdesk"]}),
    "demo-celeb": Page.from_dict({"slug": "demo-celeb", "name": "Demo Celeb", "leagues": ["entertainment"],
                                  "x_handles": ["starwire", "popdesk"]}),
}
LIVE_SHAPED = {
    "demo-sports": Page.from_dict({"slug": "demo-sports", "name": "Demo Sports", "leagues": ["nfl"], "x_handles": [
        *PAGES["demo-sports"].x_handles, "jordanpike", "mikehollis", "mikebrandt", "jeffbarlow", "danquinlan",
        "cameronreed", "tomkessler", "joshtrent", "jaycolby", "adamvance", "briankeogh", "kalebmoss",
        "jalenreports", "justinweekly", "dkessler", "sundayfilmroom", "pigskinreport"]}),
    "demo-celeb": Page.from_dict({"slug": "demo-celeb", "name": "Demo Celeb", "leagues": ["entertainment"]}),
}


def test_a_first_name_inside_a_reporters_handle_is_still_a_name():
    """'jordan' is inside @jordanpike: Jordan Elliott keeps it, Jordan Pike (the reporter) does not
    count, nor do reporters named in full without 'per' (Mike Hollis, Dan Quinlan) or by handle."""
    dd = Deduper(LIVE_SHAPED["demo-sports"], [])
    f = dd.features_of("The Titans have released DT Jordan Elliott, Jordan Pike reports.\n\n"
                       "Mike Hollis and Dan Quinlan say a teammate was involved. @JordanPike has more.")
    assert {"jordan", "elliott"} <= f.names and not ({"pike", "mike", "hollis", "dan", "quinlan", "jordanpike"} & f.names)
    old = {"page": "demo-sports", "headline": "TITANS RELEASE JORDAN ELLIOTT",
           "caption": "The Titans have released defensive tackle Jordan Elliott after a firearm incident at the "
                      "team facility, per @jordanpike.\n\nThoughts?\n#Titans #NFL"}
    new = {"page": "demo-sports", "headline": "FIREARM BEHIND ELLIOTT RELEASE",
           "caption": "A firearm incident at the team facility is behind the Titans releasing defensive tackle "
                      "Jordan Elliott, Jordan Pike reports.\n\nYour take?\n#Titans"}
    assert Deduper(LIVE_SHAPED["demo-sports"], [old]).match(draft_text(new)).names == ["elliott", "jordan"]


def test_team_cities_are_names_off_sports_pages_unless_a_team_follows():
    """Demo Celeb: Whitney Houston, Draymond Green and a Tampa crime story keep their names; a
    'Green Bay Packers' is a team anywhere. On a sports page the cities say nothing."""
    cr, hr = Deduper(LIVE_SHAPED["demo-celeb"], []), Deduper(LIVE_SHAPED["demo-sports"], [])
    assert "houston" in cr.features_of("The estate of Whitney Houston sued the label on Monday.").names
    assert {"draymond", "green", "seth"} <= cr.features_of("Draymond Green and Seth Green met at the show.").names
    assert "tampa" in cr.features_of("The man who shot a deputy in Tampa was charged, and Tampa police said more.").names
    love = "The Green Bay Packers star Jordan Love threw three touchdowns."
    assert not ({"green", "bay"} & cr.features_of(love).names) and {"jordan", "love"} <= cr.features_of(love).names
    assert not ({"houston", "dallas"} & hr.features_of("The Cowboys beat the Texans in Houston, as Dallas hoped.").names)


def test_hashtags_reporters_and_sentence_starts_are_not_story_names():
    dd = Deduper(PAGES["demo-sports"], [])
    f = dd.features_of("Police said the man was arrested. Officers found a gun, per Sam Reporter and Ian Insider.\n\n"
                       "#TrueCrime #Justice #NFL")
    assert not ({"police", "officer", "crime", "true", "justice", "sam", "ian", "insider"} & f.names)
    assert "Reporter" not in story_text("Bears are 3-0, per Sam Reporter.")
    assert sentence_capitals("Jalen Hurts is out. Hurts said the Eagles told him.") == frozenset()   # a name, not a start
    assert "police" in sentence_capitals("Police said he fled.")
