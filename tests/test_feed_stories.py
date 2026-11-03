"""Topic feed stories (Oct 21 live feeds): written text and the writer's 'not news' verdict stay with
the same posts only, subjects are names of people, a photo search by name only finds that face,
duplicates are merged, and a crashed photo check is checked again."""
import json
from types import SimpleNamespace as NS

import pytest

from bore import faceid, studio
from bore.cluster import Cluster
from bore.config import Page
from bore.media import Photo, names_in, subject_of
from bore.select import Trend

from conftest import FIXTURES
from test_feed_images import NOW, run_feed, story, trend

FEED = json.loads((FIXTURES / "feed_stories_20261021.json").read_text())
BY_HEAD = {s["headline"]: s for s in FEED["stories"]}


def head(start: str) -> dict:
    return next(s for h, s in BY_HEAD.items() if h.startswith(start))


def written(t, headline, **k):
    return {**story(t), "headline": headline, "summary": "s.", "ideas": [{}, {}], "images_v": studio.IMAGES_V,
            "images_at": NOW, "images": [{"url": "https://img.example.org/a.jpg"}, {"url": "https://img.example.org/b.jpg"}], **k}


def texts(t, *lines):
    for m, line in zip(t.cluster.members, lines):
        m["text"] = line
    return t


# ------------------------------------------------------------------ text carryover
def test_the_smaller_half_of_a_split_story_does_not_keep_the_other_storys_text(monkeypatch, tmp_path):
    # Oct 21: a Purdy hot-mic post (the lead) clustered with two Naquan Jones posts; the writer wrote
    # about Purdy; the next hour the Naquan Jones posts alone kept the Purdy headline
    urls = ["https://x.com/purdy/status/1", "https://x.com/tom/status/2", "https://x.com/pft/status/3"]
    old = [written(trend("49er-purdy-mic", urls), "Purdy caught on hot mic yelling 'HEY KYLE!'")]
    new = texts(trend("49er-agent-jone-naquan", urls[1:]), "The 49ers are releasing veteran DT Naquan Jones",
                "49ers release DL Naquan Jones.")
    got, _ = run_feed(monkeypatch, tmp_path, [new], old)
    s = got["49er-agent-jone-naquan"]
    assert s.get("needs_writing") and "headline" not in s


def test_a_grown_story_keeps_its_text_and_where_it_was_written_from(monkeypatch, tmp_path):
    urls = [f"https://x.com/u/status/{i}" for i in range(3)]
    old = [written(trend("k-old", urls[:2]), "Written")]
    got, calls = run_feed(monkeypatch, tmp_path, [trend("k-new", urls)], old)
    s = got["k-new"]
    assert s["headline"] == "Written" and not s.get("needs_writing") and calls == []
    assert s["written_from"] == urls                   # an older feed has none: the posts it was matched on


def test_an_earlier_story_is_continued_once_and_only_where_its_lead_is():
    urls = [f"https://x.com/u/status/{i}" for i in range(4)]
    old = {"id": "o", "url": urls[0], "written_from": urls}
    halves = [{"id": "a", "written_from": urls[:2]}, {"id": "b", "written_from": urls[2:]}]
    assert {k: v["id"] for k, v in studio._inherit(halves, [old]).items()} == {"a": "o"}
    assert studio._inherit([{"id": "c", "written_from": urls[1:3]}], [old]) == {}       # no lead: new text
    more = [f"https://x.com/v/status/{i}" for i in range(8)]
    grown = [{"id": "d", "written_from": urls + more}]           # three times the posts, the same lead
    assert {k: v["id"] for k, v in studio._inherit(grown, [old]).items()} == {"d": "o"}
    absorbed = [{"id": "e", "written_from": more[:1] + urls + more[1:]}]    # part of a bigger story now
    assert studio._inherit(absorbed, [old]) == {}
    assert studio._inherit([{"id": "f", "written_from": urls[:2] + more}], [old]) == {}   # half its posts gone


def test_a_headline_naming_people_the_posts_do_not_is_written_again(monkeypatch, tmp_path):
    urls = ["https://x.com/a/status/1", "https://x.com/b/status/2"]
    t = texts(trend("k1", urls), "The 49ers are releasing veteran DT Naquan Jones", "49ers release DL Naquan Jones.")
    got, _ = run_feed(monkeypatch, tmp_path, [t], [written(trend("k1", urls), "Purdy caught on hot mic yelling 'HEY KYLE!'")])
    assert got["k1"].get("needs_writing")
    got, _ = run_feed(monkeypatch, tmp_path, [t], [written(trend("k1", urls), "49ers release Naquan Jones")])
    assert got["k1"]["headline"] == "49ers release Naquan Jones"


# ------------------------------------------------------------------ the writer's 'not news' verdict
def test_not_news_is_remembered_and_the_writing_slots_go_to_the_news(monkeypatch, tmp_path):
    memes = [trend(f"meme{i}", [f"https://reddit.com/m/{i}a", f"https://reddit.com/m/{i}b"]) for i in range(15)]
    news = [trend(f"news{i}", [f"https://x.com/n/status/{i}a", f"https://x.com/n/status/{i}b"]) for i in range(5)]
    old = [{**story(t), "images_v": studio.IMAGES_V, "images_at": NOW, "images": [{"url": "u1"}, {"url": "u2"}],
            "not_news": "a meme"} for t in memes] + [{**story(t), "images_v": studio.IMAGES_V, "images_at": NOW,
                                                      "images": [{"url": "u1"}, {"url": "u2"}]} for t in news]
    got, calls = run_feed(monkeypatch, tmp_path, memes + news, old)
    assert sorted(k for k, s in got.items() if s.get("needs_writing")) == [f"news{i}" for i in range(5)]
    assert all(got[f"meme{i}"]["not_news"] == "a meme" for i in range(15)) and calls == []


def test_publish_feed_lists_not_news_last_and_sends_what_the_next_hour_compares(monkeypatch, tmp_path):
    (tmp_path / "feed").mkdir()
    base = {"sources": [], "published_at": 0, "url": "u", "score": 3.0, "images": [], "headline_hint": "h"}
    stories = [{**base, "id": "meme", "needs_writing": True, "not_news": "a personal anecdote", "written_from": ["u"]},
               {**base, "id": "news", "needs_writing": True, "written_from": ["v"]}]
    (tmp_path / "feed" / "demo.json").write_text(json.dumps({"page": "demo", "stories": stories}))
    sent = []
    monkeypatch.setattr(studio, "_post", lambda dash, path, body: sent.append(body) or {})
    studio.publish_feed(tmp_path, dash=object())
    meme, news = sent[0]["stories"]
    assert meme["not_news"] == "a personal anecdote" and meme["score"] == 0 and meme["written_from"] == ["u"]
    assert "not_news" not in news and news["score"] == 3.0


# ------------------------------------------------------------------ a failed photo check
def test_a_crashed_photo_check_stays_unchecked_and_is_checked_next_run(monkeypatch, tmp_path):
    import requests

    import bore.media
    t = trend("k1", ["https://x.com/a/status/1", "https://x.com/b/status/2"])

    def boom(*a, **k):
        raise requests.ConnectionError("Connection aborted: RemoteDisconnected")
    monkeypatch.setattr(bore.media, "story_photos", boom)
    monkeypatch.setattr(faceid, "available", lambda: False)
    with pytest.raises(studio.PhotoCheckFailed) as e:
        studio._feed_images(t, "never", [{"url": "https://img.example.org/raw.jpg", "flags": []}])
    assert e.value.images[0]["flags"][0] == "not checked: ConnectionError"

    def broken(*a, **k):     # a package that does not load is the machine, not this story: the run fails
        raise ImportError("cannot import name '_imaging' from 'PIL'")
    monkeypatch.setattr(bore.media, "story_photos", broken)
    with pytest.raises(ImportError):
        studio._feed_images(t, "never", [{"url": "https://img.example.org/raw.jpg", "flags": []}])
    # prepare_feed: unchecked (images_v 0), and the next run checks the story again
    monkeypatch.setattr(studio, "_feed_images", lambda *a, **k: (_ for _ in ()).throw(studio.PhotoCheckFailed([], "OSError")))
    import bore.select
    monkeypatch.setattr(studio, "load_pages", lambda: [Page.from_dict({"slug": "demo", "name": "Demo"})])
    monkeypatch.setattr(studio, "studio_meta", lambda: {"demo": {}})
    monkeypatch.setattr(studio, "_page_templates", lambda slug: [])
    monkeypatch.setattr(studio, "_get", lambda dash, path, **k: {"feed": {"stories": []}})
    monkeypatch.setattr(studio, "_story_from_trend", story)
    monkeypatch.setattr(bore.select, "select_page", lambda *a, **k: NS(trends=[t], watchlist=[]))
    (f,) = studio.prepare_feed(tmp_path, {"clustering": {"window_hours": 6}}, NS(page_items=lambda *a: []), dash=object(), now=NOW)
    (s,) = json.loads(f.read_text())["stories"]
    assert s["images_v"] == 0
    got, calls = run_feed(monkeypatch, tmp_path, [t], [{**s, "headline": ""}])
    assert calls == ["k1"]


# ------------------------------------------------------------------ subjects
def test_live_subjects_name_a_person_or_nobody():
    """Oct 21: 'On March', 'He COULD', 'Packers RB Josh', 'Riley Patterson.' were searched by name.
    Each is now nobody or the right person, or a phrase the person lookup has no face for (so it is
    never searched by name or compared with faces)."""
    for s in FEED["stories"]:
        got = subject_of(s["members"])
        assert got in s["subject_ok"] or got in FEED["not_people"], (s["headline"], got)
    for bad in ("On March", "He COULD", "Packers RB Josh", "Saints WR Jordyn", "Bills DB Sam", "Former Cornell",
                "Riley Patterson.", "Power Rankings", "From The Insiders", "Saints GM Mickey", "Los Angeles"):
        assert bad not in {subject_of(s["members"]) for s in FEED["stories"]}


@pytest.mark.parametrize("start,name", [("NFLPA calls", "Jalen Hurts"), ("Kelly Soo Park", "Kelly Soo Park"),
                                        ("Bills DB Sam Franklin", "Sam Franklin Jr."), ("Steve Tisch gave up", "Steve Tisch"),
                                        ("Packers RB Josh Jacobs", "Josh Jacobs"), ("Rams signing veteran kicker", "Brandon McManus")])
def test_real_names_are_kept(start, name):
    assert subject_of(head(start)["members"]) == name


def test_names_are_read_from_sentences():
    assert names_in("Following his emails with Jeffrey Epstein, Steve Tisch gave up his stake") == \
        [("Jeffrey Epstein", "object"), ("Steve Tisch", "")]
    assert [n for n, _ in names_in("Sean ‘Diddy’ Combs and A.J. Brown met Amon-Ra St. Brown and CJ Stroud")] == \
        ["Sean ‘Diddy’ Combs", "A.J. Brown", "Amon-Ra St. Brown", "CJ Stroud"]
    assert names_in("On March 25, 1990, He COULD have. Former Cornell president sued by Jane Doe") == []
    assert names_in("A long talk with Terry McLaurin, Baker Mayfield, and Lamar Jackson.") == \
        [("Terry McLaurin", "object"), ("Baker Mayfield", "object"), ("Lamar Jackson", "object")]
    # the review of Oct 21: team cities, a coach who speaks, a possessive after 'of', a quoted word
    assert names_in("Green Bay Packers QB Jordan Love and Kansas City Chiefs QB Patrick Mahomes. Los Angeles is 0-5") == \
        [("Jordan Love", ""), ("Patrick Mahomes", "")]
    assert names_in('Eagles HC Nick Sirianni "hopeful" WR DeVonta Smith, RB Saquon Barkley will return') == \
        [("Nick Sirianni", "staff"), ("DeVonta Smith", ""), ("Saquon Barkley", "")]
    assert names_in("He has watched every snap of Justin Herbert's NFL career") == [("Justin Herbert", "")]
    assert names_in("Dan Campbell: I expect Kerby Joseph to play") == [("Dan Campbell", "speaker"), ("Kerby Joseph", "")]
    assert names_in("The Daily Show takes a swipe at WH press secretary Katie Zacharia") == [("Katie Zacharia", "")]


@pytest.mark.parametrize("posts,subject", [
    # Oct 14-21 drafts whose photo shows the player while the coach or speaker became the subject
    (["Texans HC DeMeco Ryans announced that WR Tank Dell is going to play Sunday vs. the Giants.",
      "Coach DeMeco Ryans just announced that WR Tank Dell is going to play on Sunday vs the #Giants."], "Tank Dell"),
    (["Bills coach Joe Brady says the team won't be cutting S C.J. Gardner-Johnson after his spitting incident."],
     "C.J. Gardner-Johnson"),
    (["Ben Johnson: Drake Maye is an elite deep ball thrower, can crush you with his legs."], "Drake Maye"),
    (["Ryan Fitzpatrick on Broncos QB Bo Nix: “Bo Nix is not playing competitively.”",
      "The legendary Ryan Fitzpatrick goes OFF on Broncos QB Bo Nix after tonight’s game."], "Bo Nix"),
    # ...and the ones where the speaker or the coach is the story
    (["Dak Prescott: Cowboys offense \"didn't hold our own\" in loss to Packers"], "Dak Prescott"),
    (["49ers principal owner Jed York has been suspended for six games for violating the NFL’s Personal Conduct "
      "Policy."] * 3 + ["Per the NFL: Jed York has stayed away from the 49ers first three games."], "Jed York"),
    (["Green Bay Packers QB Jordan Love threw 3 TDs", "Green Bay Packers win again"], "Jordan Love"),
    ([".@danielrpopper has watched every snap of Justin Herbert's NFL career. Los Angeles sits at 0-5."], "Justin Herbert"),
    (["“(You Drive Me) Crazy” by Britney Spears has the right tempo for Hands-Only CPR."], "Britney Spears"),
    (["TIL: the Old Copper complex, in North America, copper production began around 6500BCE"], ""),
])
def test_the_subject_is_whom_the_news_is_about(posts, subject):
    assert subject_of([{"text": t, "engagement": 10 - i} for i, t in enumerate(posts)]) == subject


def test_the_no_name_search_rule_reads_the_crime_kinds_words_and_abuse():
    """One word list (bore/kinds.py), not a third copy: every crime_court story is a crime story to
    the feed, and so is an abuse story the kind does not name."""
    from bore.kinds import CELEBRITY, kind_of
    for text in ("Rapper arrested after a fight outside a club", "Singer sued by her former manager",
                 "Actor sentenced to 10 years", "The cult leader's trial opens", "Reality star jailed over DUI"):
        assert kind_of(text, CELEBRITY) == "crime_court" and studio.crime_story(NS(headline=text, kind=""), []), text
    for text in ("Former Cornell fraternity president dropped from Jane Doe lawsuit",
                 "Victims describe years of abuse by the coach", "Man who shot dead his neighbour"):
        assert studio.crime_story(NS(headline=text, kind=""), []), text
    assert not studio.crime_story(NS(headline="Taylor Swift's album breaks a streaming record", kind=""), [])


# ------------------------------------------------------------------ the photo search by name
def _photo(i, match=""):
    ph = Photo(f"https://img.example.org/{i}.jpg", "search", "s.example.org", 1600, 1200, b"x", post_url=f"https://s/{i}")
    ph.id_match = match
    return ph


def _feed_trend(text, kind=""):
    members = [{"platform": "x", "url": f"https://x.com/a/status/{i}", "engagement": 5, "created_at": NOW, "text": text}
               for i in range(2)]
    return NS(key="k1", headline=text, kind=kind, cluster=NS(members=members))


@pytest.mark.parametrize("text,kind,face,searched", [
    ("Packers RB Josh Jacobs is back on the field today", "", True, True),
    ("Packers RB Josh Jacobs is back on the field today", "", False, False),        # no known face: no search
    ("On March 25, 1990, Julio González set the Happy Land fire, police said", "", True, False),  # a crime story
    ("Kelly Soo Park, Munir Uwaydah trial", "crime_court", True, False),
])
def test_a_name_search_needs_a_known_face_and_keeps_only_that_face(monkeypatch, text, kind, face, searched):
    import bore.media
    import bore.photosearch
    monkeypatch.setattr(bore.media, "story_photos", lambda members, **k: [])
    monkeypatch.setattr(faceid, "available", lambda: True)
    monkeypatch.setattr(faceid, "reference", lambda name, sports=False, player=None, timeout=6: ((object(), "espn") if face else (None, "")))
    calls = []

    def search(query, name, **k):
        calls.append(name)
        return [_photo(1, "match"), _photo(2, ""), _photo(3, "mismatch")], []
    monkeypatch.setattr(bore.photosearch, "search_subject", search)
    monkeypatch.setattr(bore.media, "dhash", lambda data: hash(data) + len(calls))
    out = studio._feed_images(_feed_trend(text, kind), "never", [], same=[], searches={"left": 3})
    assert bool(calls) is searched
    if searched:
        assert calls == ["Josh Jacobs"] and [i["url"] for i in out] == ["https://img.example.org/1.jpg"]
        assert out[0]["flags"] == ["photo of Josh Jacobs (search)"]


# ------------------------------------------------------------------ duplicates
def _trends(page):
    out = []
    for i, s in enumerate(x for x in FEED["stories"] if x["page"] == page):
        ms = [{**m, "id": f"{i}-{j}", "url": f"https://example.org/{i}/{j}", "created_at": NOW} for j, m in enumerate(s["members"])]
        out.append(Trend(Cluster(members=ms), 10.0 - i * 0.1, "watchlist", s["headline"], []))
    return out


def test_duplicate_stories_are_merged_and_different_ones_kept():
    kept = []
    for page in ("culture-rush", "huddle"):
        kept += [t.key for t in studio.merge_same_stories(Page.from_dict({"slug": page, "name": page}), _trends(page))]

    def listed(start):
        return any(k.startswith(start) for k in kept)
    for a, b in FEED["same_story"]:          # the lower-ranked copy joined the higher-ranked one
        assert listed(a) and not listed(b), (a, b)
    for a, b in FEED["different_stories"]:
        assert listed(a) and listed(b), (a, b)
    assert len(kept) == len(FEED["stories"]) - 5      # three pairs, and the Hurts dispute's three stories


def test_a_story_joins_through_any_story_already_merged():
    """'NFL finds Eagles handled... appropriately' is the story of 'NFL backs Eagles' handling', which
    had joined the NFLPA's call (a statement naming several people): compared with the first story
    alone, it stayed apart."""
    page = Page.from_dict({"slug": "huddle", "name": "huddle"})
    ts = {t.key: t for t in _trends("huddle")}
    keys = [k for k in ts if k.startswith(("NFLPA calls", "NFL backs Eagles", "NFL finds Eagles"))]
    n = sum(len(ts[k].cluster.members) for k in keys)
    assert len(studio.merge_same_stories(page, [ts[keys[0]], ts[keys[2]]])) == 2      # the finding alone: apart
    (kept,) = studio.merge_same_stories(page, [ts[k] for k in keys])
    assert kept.key.startswith("NFLPA calls") and len(kept.cluster.members) == n
