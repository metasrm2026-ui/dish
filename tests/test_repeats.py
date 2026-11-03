"""Repeat posts (Sep 26 incident): Gottmik x2, Tyson Bagent x4, Scarlett & Colin x2 were each made
more than once, because backups used by the routine were never recorded and a grown story was
let back in. The dashboard's real drafts are the fixture."""
import json
import time
import types

import pytest

from bore.config import Page, load_pages
from bore.dupes import Deduper
from bore.select import select_page, shortlist, PageResult, Trend
from bore.cluster import Cluster

from conftest import FIXTURES

DRAFTS = json.loads((FIXTURES / "dashboard_drafts_20260929.json").read_text())
# the pages as they were when these drafts were made (the live source lists change; the reporters
# and outlets quoted in these drafts must not count as story names)
PAGES = {
    "huddle": Page.from_dict({"slug": "huddle", "name": "Huddle Room", "x_handles": [
        "adamschefter", "rapsheet", "tompelissero", "mikegarafolo", "jfowlerespn", "diannaespn", "nflnetwork",
        "profootballtalk", "aroundthenfl", "espnnfl", "nfl", "brgridiron"]}),
    "culture-rush": Page.from_dict({"slug": "culture-rush", "name": "Culture Rush", "x_handles": [
        "popcrave", "popbase", "tmz", "variety", "thr", "deadline", "people", "pagesix", "enews", "culturecrave",
        "discussingfilm", "etnow", "thewrap", "billboard", "dexerto", "courttv"]}),
}
REPEATS = {frozenset(d["id"] for d in DRAFTS if "gottmik" in d["id"]),
           frozenset(d["id"] for d in DRAFTS if "scarlett" in d["id"])}


def _pairs(slug):
    dd = Deduper(PAGES[slug], DRAFTS)
    items = list(zip(dd.recent, dd.features))
    for i, (a, fa) in enumerate(items):
        for b, fb in items[i + 1:]:
            yield a, b, dd.same_story(fa, fb)[0]


def test_real_repeats_found_and_nothing_else():
    found = {frozenset((a["id"], b["id"])) for slug in PAGES for a, b, dup in _pairs(slug) if dup}
    bagent = {d["id"] for d in DRAFTS if "BAGENT" in (d["headline"] or "")}
    expected = REPEATS | {frozenset((a, b)) for a in bagent for b in bagent if a < b}
    assert found == expected    # 8 repeat pairs; Taylor Swift chart vs road trip etc. stay distinct


@pytest.mark.parametrize("text,repeat", [
    ("Bears QB Tyson Bagent has cleared the concussion protocol and returned to practice, per @AdamSchefter", True),
    ("Tyson Bagent throws 3 TDs as Bears beat the Eagles on Monday night", False),
    ("Saquon Barkley Is Good To Go For Sunday After Practicing Fully With Stinger, Off Injury Report", True),
    ("Saquon Barkley signs a record extension with the Eagles", False),
])
def test_tweets_against_recent_posts(text, repeat):
    assert bool(Deduper(PAGES["huddle"], DRAFTS).match(text)) is repeat


def test_reporter_names_are_not_story_names():
    dd = Deduper(PAGES["huddle"], [])
    f = dd.features_of("Per Adam Schefter and Tom Pelissero, the Bears are 3-0. #NFL #Bears")
    assert not ({"adam", "schefter", "tom", "pelissero", "bear", "nfl"} & f.names)


def _trend(text, key):
    now = time.time()
    m = {"id": key, "platform": "x", "author": "a", "text": text, "created_at": now, "engagement": 10}
    return Trend(Cluster(members=[m]), 5.0, "corroborated", key, [key])


def test_shortlist_skips_repeats_and_same_story_backups(cfg):
    res = PageResult("culture-rush")
    res.trends = [
        _trend("Scarlett Johansson and Colin Jost make first joint red carpet appearance in a year at NYFF", "s1"),
        _trend("Zendaya and Tom Holland spotted at the Spider-Man premiere red carpet in London tonight", "z1"),
        _trend("Tom Holland and Zendaya walk the Spider-Man premiere red carpet in London, fans react", "z2"),
        _trend("Pedro Pascal cast as the lead in a new Western drama from A24 directed by Ethan Coen", "p1"),
    ]
    shortlist(res, cfg, quota=2, dedupe=Deduper(PAGES["culture-rush"], DRAFTS))
    assert [t.key for t in res.shortlist] == ["z1", "p1"]
    assert any(s["key"] == "s1" and "scarlett" in s["why"] for s in res.suppressed)


def test_upload_blocks_repeat_unless_declared_update(tmp_path, cfg, monkeypatch):
    from bore import cli
    brief = {"id": "huddle-202609291000-bagent-x", "page": "huddle", "key": "k", "signature": []}
    dash = types.SimpleNamespace(recent_drafts=lambda h: DRAFTS)
    copy = {"headline_lines": ["BAGENT CLEARED"], "caption": "Tyson Bagent cleared the concussion protocol "
            "and practiced fully, the Bears said."}
    with pytest.raises(SystemExit, match="repeats a recent post"):
        cli._repeat_gate(brief, copy, dash, cfg, None)
    m = Deduper(PAGES["huddle"], DRAFTS).match(copy["caption"])
    cli._repeat_gate(brief, {**copy, "update_of": m.draft["id"], "what_is_new": "Ruled OUT for Monday"},
                     dash, cfg, None)
    with pytest.raises(SystemExit):   # an update needs the new fact
        cli._repeat_gate(brief, {**copy, "update_of": m.draft["id"]}, dash, cfg, None)


def test_regeneration_is_not_a_repeat_of_the_post_it_replaces(cfg):
    from bore import cli
    old = next(d for d in DRAFTS if d["id"].startswith("culture-rush-202609262207"))
    brief = {"id": "culture-rush-202609291000-scarlett-v2", "page": "culture-rush",
             "regeneration": {"replaces": old["id"]}}
    dash = types.SimpleNamespace(recent_drafts=lambda h: [old])
    cli._repeat_gate(brief, {"headline_lines": [old["headline"]], "caption": old["caption"]}, dash, cfg, None)


def test_uploaded_backup_is_recorded(cfg, store, monkeypatch):
    from bore import cli
    monkeypatch.setattr(cli, "_store", lambda c: store)
    cli._record_uploaded({"page": "culture-rush", "key": "carpet-colin", "signature": ["colin", "carpet"],
                          "verification": {"sources": ["a", "b"], "engagement": 5}},
                         {"headline_lines": ["SCARLETT & COLIN"]}, cfg)
    assert [r["key"] for r in store.sent_for_page("culture-rush", 0)] == ["carpet-colin"]


def test_krea_daily_pacing(store):
    from datetime import datetime, timezone
    from bore.routine import krea_daily_allowance
    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc).timestamp()
    for i in range(400):     # 400 used before today (Sep 1-28)
        store.db.execute("INSERT INTO spend (at, actor, usd) VALUES (?,?,?)",
                         (datetime(2026, 9, 1 + i % 28, tzinfo=timezone.utc).timestamp(), "krea:m", 0.03))
    assert krea_daily_allowance(store, 600, now) == (0, 100)   # 200 left over 2 days
    for _ in range(5):
        store.db.execute("INSERT INTO spend (at, actor, usd) VALUES (?,?,?)", (now - 60, "krea:m", 0.03))
    assert krea_daily_allowance(store, 600, now) == (5, 100)


def test_twenty_posts_per_page(cfg):
    from bore.cli import page_quota
    from datetime import datetime
    from zoneinfo import ZoneInfo
    assert cfg["posting"]["daily_cap"] == 20
    page = Page.from_dict({"slug": "p", "name": "P", "timezone": "America/New_York"})
    at7 = datetime(2026, 9, 29, 7, 5, tzinfo=ZoneInfo("America/New_York")).timestamp()
    at22 = datetime(2026, 9, 29, 22, 5, tzinfo=ZoneInfo("America/New_York")).timestamp()
    assert page_quota(page, None, cfg, at7, created_today=0) == 2          # 20 over 16 hours
    assert page_quota(page, None, cfg, at22, created_today=15) == 3        # catching up, capped
    assert page_quota(page, None, cfg, at22, created_today=20) == 0


def test_an_editors_link_that_repeats_a_post_is_made_and_noted(cfg):
    """Editors decide what to cover: their link is never blocked as a repeat, the post says so."""
    from bore import cli
    dash = types.SimpleNamespace(recent_drafts=lambda h: DRAFTS)
    brief = {"id": "huddle-202609291900-link-bagent", "page": "huddle", "link": {"request_id": "r1", "url": "https://x.com/a/status/1"}}
    copy = {"headline_lines": ["BAGENT CLEARED"], "caption": "Tyson Bagent cleared the concussion protocol "
            "and practiced fully, the Bears said."}
    cli._repeat_gate(brief, copy, dash, cfg, None)
    assert "Similar to a recent post" in copy["note_to_editor"]


def test_a_network_blip_while_checking_repeats_does_not_lose_the_post(monkeypatch, tmp_path):
    """Oct 23 11:21: the repeat check's read of recent drafts hit a network error and the upload failed with a
    traceback. It is tried again after a pause; if the dashboard still does not answer, nothing is sent and
    the message says to run the same command again."""
    import requests

    from bore import cli
    calls = []

    class Dash:
        def recent_drafts(self, hours):
            calls.append(hours)
            raise requests.ConnectionError("Max retries exceeded")
    monkeypatch.setattr(cli.time, "sleep", lambda s: calls.append(("sleep", s)))
    import bore.config
    monkeypatch.setattr(bore.config, "load_pages", lambda: [type("P", (), {"slug": "p"})()])
    from types import SimpleNamespace as NS
    with pytest.raises(SystemExit, match="UPLOAD NOT SENT.*nothing was sent.*routine upload d1"):
        cli._repeat_gate({"id": "x", "page": "p"}, {"caption": "c"}, Dash(), {"dedup": {"suppress_hours": 96}}, NS(dir="d1"))
    assert calls == [96, ("sleep", 15), 96]
