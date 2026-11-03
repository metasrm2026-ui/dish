"""Repeat posts: the same story made twice (a backup the routine used was never recorded, a grown
story was let back in) is found, and different stories about the same people stay apart. The
drafts are made up, in the shape /api/drafts gives them."""
import json
import time
import types

import pytest

from bore.config import Page, load_pages
from bore.dupes import Deduper
from bore.select import select_page, shortlist, PageResult, Trend
from bore.cluster import Cluster

# made-up pages: the reporters and outlets they follow must not count as story names
PAGES = {
    "demo-sports": Page.from_dict({"slug": "demo-sports", "name": "Demo Sports", "x_handles": [
        "samreporter", "tomwire", "gridironwire", "footballdesk"]}),
    "demo-celeb": Page.from_dict({"slug": "demo-celeb", "name": "Demo Celeb", "x_handles": ["starwire", "popdesk"]}),
}


def _d(id_, page, headline, caption, at="2026-09-28T12:00:00Z"):
    return {"id": id_, "page": page, "status": "approved", "headline": headline, "caption": caption, "created_at": at}


DRAFTS = [
    _d("demo-celeb-202609262207-carpet-vance-dray", "demo-celeb", "MARA VANCE & COLIN DRAY HIT THE CARPET",
       "Mara Vance and Colin Dray made their first joint red carpet appearance in a year at the film festival "
       "premiere on Friday night.\n\nCute couple?\n#MaraVance #RedCarpet"),
    _d("demo-celeb-202609271637-vance-dray", "demo-celeb", "VANCE AND DRAY TOGETHER AGAIN",
       "Colin Dray and Mara Vance stepped out together at the film festival premiere, their first joint red "
       "carpet appearance in a year.\n\nThoughts?\n#RedCarpet"),
    _d("demo-celeb-202609251200-lina-rose-chart", "demo-celeb", "LINA ROSE TOPS THE CHART",
       "Lina Rose's new album debuted at number one on the album chart with record first-week sales.\n\n#LinaRose"),
    _d("demo-celeb-202609261500-lina-rose-trip", "demo-celeb", "LINA ROSE'S ROAD TRIP",
       "Lina Rose shared photos from a road trip through the desert with her sister.\n\nWould you go?\n#LinaRose"),
    _d("demo-sports-202609261000-brandt-cleared", "demo-sports", "THEO BRANDT CLEARED",
       "Bears QB Theo Brandt has cleared the concussion protocol and returned to practice, per @samreporter."
       "\n\nReady for Monday?\n#Bears #NFL"),
    _d("demo-sports-202609271000-brandt-back", "demo-sports", "BRANDT BACK AT PRACTICE",
       "Theo Brandt is back at Bears practice after clearing the concussion protocol.\n\n#Bears"),
    _d("demo-sports-202609271400-okafor-ready", "demo-sports", "OKAFOR GOOD TO GO",
       "Eagles RB Dante Okafor is good to go for Sunday after practicing fully with a stinger, off the injury "
       "report.\n\n#Eagles"),
]
@pytest.fixture(autouse=True)
def _pages(monkeypatch):
    monkeypatch.setattr("bore.config.load_pages", lambda *a: list(PAGES.values()))


REPEATS = {frozenset(d["id"] for d in DRAFTS if "vance-dray" in d["id"]),
           frozenset(d["id"] for d in DRAFTS if "brandt" in d["id"])}


def _pairs(slug):
    dd = Deduper(PAGES[slug], DRAFTS)
    items = list(zip(dd.recent, dd.features))
    for i, (a, fa) in enumerate(items):
        for b, fb in items[i + 1:]:
            yield a, b, dd.same_story(fa, fb)[0]


def test_real_repeats_found_and_nothing_else():
    found = {frozenset((a["id"], b["id"])) for slug in PAGES for a, b, dup in _pairs(slug) if dup}
    assert found == REPEATS    # the chart and the road trip stay distinct


@pytest.mark.parametrize("text,repeat", [
    ("Bears QB Theo Brandt has cleared the concussion protocol and returned to practice, per @TomWire", True),
    ("Theo Brandt throws 3 TDs as Bears beat the Eagles on Monday night", False),
    ("Dante Okafor Is Good To Go For Sunday After Practicing Fully With Stinger, Off Injury Report", True),
    ("Dante Okafor signs a record extension with the Eagles", False),
])
def test_tweets_against_recent_posts(text, repeat):
    assert bool(Deduper(PAGES["demo-sports"], DRAFTS).match(text)) is repeat


def test_reporter_names_are_not_story_names():
    dd = Deduper(PAGES["demo-sports"], [])
    f = dd.features_of("Per Sam Reporter and Tom Wire, the Bears are 3-0. #NFL #Bears")
    assert not ({"sam", "reporter", "tom", "wire", "bear", "nfl"} & f.names)


def _trend(text, key):
    now = time.time()
    m = {"id": key, "platform": "x", "author": "a", "text": text, "created_at": now, "engagement": 10}
    return Trend(Cluster(members=[m]), 5.0, "corroborated", key, [key])


def test_shortlist_skips_repeats_and_same_story_backups(cfg):
    res = PageResult("demo-celeb")
    res.trends = [
        _trend("Mara Vance and Colin Dray make first joint red carpet appearance in a year at the festival", "s1"),
        _trend("Zendaya and Tom Holland spotted at the Spider-Man premiere red carpet in London tonight", "z1"),
        _trend("Tom Holland and Zendaya walk the Spider-Man premiere red carpet in London, fans react", "z2"),
        _trend("Pedro Pascal cast as the lead in a new Western drama from A24 directed by Ethan Coen", "p1"),
    ]
    shortlist(res, cfg, quota=2, dedupe=Deduper(PAGES["demo-celeb"], DRAFTS))
    assert [t.key for t in res.shortlist] == ["z1", "p1"]
    assert any(s["key"] == "s1" and "vance" in s["why"] for s in res.suppressed)


def test_upload_blocks_repeat_unless_declared_update(tmp_path, cfg, monkeypatch):
    from bore import cli
    brief = {"id": "demo-sports-202609291000-brandt-x", "page": "demo-sports", "key": "k", "signature": []}
    dash = types.SimpleNamespace(recent_drafts=lambda h: DRAFTS)
    copy = {"headline_lines": ["BRANDT CLEARED"], "caption": "Theo Brandt cleared the concussion protocol "
            "and practiced fully, the Bears said."}
    with pytest.raises(SystemExit, match="repeats a recent post"):
        cli._repeat_gate(brief, copy, dash, cfg, None)
    m = Deduper(PAGES["demo-sports"], DRAFTS).match(copy["caption"])
    cli._repeat_gate(brief, {**copy, "update_of": m.draft["id"], "what_is_new": "Ruled OUT for Monday"},
                     dash, cfg, None)
    with pytest.raises(SystemExit):   # an update needs the new fact
        cli._repeat_gate(brief, {**copy, "update_of": m.draft["id"]}, dash, cfg, None)


def test_regeneration_is_not_a_repeat_of_the_post_it_replaces(cfg):
    from bore import cli
    old = next(d for d in DRAFTS if d["id"].startswith("demo-celeb-202609262207"))
    brief = {"id": "demo-celeb-202609291000-vance-dray-v2", "page": "demo-celeb",
             "regeneration": {"replaces": old["id"]}}
    dash = types.SimpleNamespace(recent_drafts=lambda h: [old])
    cli._repeat_gate(brief, {"headline_lines": [old["headline"]], "caption": old["caption"]}, dash, cfg, None)


def test_uploaded_backup_is_recorded(cfg, store, monkeypatch):
    from bore import cli
    monkeypatch.setattr(cli, "_store", lambda c: store)
    cli._record_uploaded({"page": "demo-celeb", "key": "carpet-colin", "signature": ["colin", "carpet"],
                          "verification": {"sources": ["a", "b"], "engagement": 5}},
                         {"headline_lines": ["VANCE & DRAY"]}, cfg)
    assert [r["key"] for r in store.sent_for_page("demo-celeb", 0)] == ["carpet-colin"]


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


def test_each_page_makes_its_own_posts_a_day(cfg):
    """The page record's posts_per_day (default 10), never above the product's ceiling (daily_cap)."""
    from bore.cli import daily_posts, page_quota
    from datetime import datetime
    from zoneinfo import ZoneInfo
    assert cfg["posting"]["daily_cap"] == 20
    page = Page.from_dict({"slug": "p", "name": "P", "timezone": "America/New_York", "posts_per_day": 20})
    at7 = datetime(2026, 9, 29, 7, 5, tzinfo=ZoneInfo("America/New_York")).timestamp()
    at22 = datetime(2026, 9, 29, 22, 5, tzinfo=ZoneInfo("America/New_York")).timestamp()
    assert page_quota(page, None, cfg, at7, created_today=0) == 2          # 20 over 16 hours
    assert page_quota(page, None, cfg, at22, created_today=15) == 3        # catching up, capped
    assert page_quota(page, None, cfg, at22, created_today=20) == 0
    ten = Page.from_dict({"slug": "q", "name": "Q", "timezone": "America/New_York"})
    assert ten.posts_per_day == 10 and daily_posts(ten, cfg) == 10
    assert page_quota(ten, None, cfg, at7, created_today=0) == 1           # 10 over 16 hours
    assert page_quota(ten, None, cfg, at22, created_today=8) == 2 and page_quota(ten, None, cfg, at22, created_today=10) == 0
    big = Page.from_dict({"slug": "r", "name": "R", "posts_per_day": 50})
    assert daily_posts(big, cfg) == 20                                     # the ceiling holds
    assert daily_posts(Page.from_dict({"slug": "s", "name": "S", "posts_per_day": 0}), cfg) == 0
    assert Page.from_dict({"slug": "t", "name": "T", "posts_per_day": "lots"}).posts_per_day == 10


def test_an_editors_link_that_repeats_a_post_is_made_and_noted(cfg):
    """Editors decide what to cover: their link is never blocked as a repeat, the post says so."""
    from bore import cli
    dash = types.SimpleNamespace(recent_drafts=lambda h: DRAFTS)
    brief = {"id": "demo-sports-202609291900-link-brandt", "page": "demo-sports", "link": {"request_id": "r1", "url": "https://x.com/a/status/1"}}
    copy = {"headline_lines": ["BRANDT CLEARED"], "caption": "Theo Brandt cleared the concussion protocol "
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
