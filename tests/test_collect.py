import time

from bore.collect import batch_from_queries, collection_window, normalise_x, routes_for, route_map
from bore.config import Page
from bore.textutil import identity


def test_search_batches_stay_under_limit():
    handles = [f"handle_number_{i}" for i in range(80)]
    batches = batch_from_queries(handles, 460)
    assert all(len(b) <= 460 for b in batches)
    joined = " OR ".join(batches)
    assert all(f"from:{h}" in joined for h in handles)


def test_gap_backfill_widens_window_and_caps(cfg, store):
    now = time.time()
    assert abs(collection_window(store, "x", now, cfg) - (now - 3600)) < 1
    store.mark_attempt("x", success_at=now - 5 * 3600)       # missed four hours
    start = collection_window(store, "x", now, cfg)
    assert start <= now - 5 * 3600 and start >= now - 5 * 3600 - 11 * 60
    store.mark_attempt("x", success_at=now - 72 * 3600)      # long outage: capped at 24 h
    assert abs(collection_window(store, "x", now, cfg) - (now - 24 * 3600)) < 1


def test_repost_normalisation(cfg):
    raw = {"id": "9", "text": "RT @schefter: Big trade done", "author": {"userName": "BeatWriter"},
           "isRetweet": True, "likeCount": 500, "retweetCount": 40, "createdAt": "2026-09-24T10:00:00Z"}
    n = normalise_x(raw, cfg["fields"]["x"], time.time())
    assert n["author"] == "beatwriter" and n["original_author"] == "schefter"
    assert n["text"] == "Big trade done" and n["engagement"] == 0


def test_routing_by_author_and_original_author():
    pages = [Page.from_dict({"slug": "a", "name": "A", "x_handles": ["Schefter"]}),
             Page.from_dict({"slug": "b", "name": "B", "x_handles": ["beatwriter"]})]
    bh, bs = route_map(pages)
    item = {"platform": "x", "author": "beatwriter", "original_author": "schefter"}
    assert [p for p, _ in routes_for(item, bh, bs)] == ["a", "b"]


def test_web_identity_keeps_urls_distinct():
    a = identity("web", url="https://espn.com/nfl/story/_/id/1/a")
    b = identity("web", url="https://espn.com/nfl/story/_/id/2/b")
    assert a != b
    assert identity("web", url="https://www.espn.com/nfl/story/_/id/1/a/") == a


def test_live_x_payload_shape(cfg):
    """Shape observed from the live actor (2026-09-26): author empty, handle only in the URL."""
    raw = {"id": "2103819834626826387", "createdAt": "Sat Sep 26 12:11:50 +0000 2026",
           "text": "RT @espn: 20 years ago, Tennessee retired Peyton Manning's No. 16.",
           "isRetweet": True, "likeCount": 0, "retweetCount": 42,
           "url": "https://x.com/AdamSchefter/status/2103819834626826387"}
    n = normalise_x(raw, cfg["fields"]["x"], time.time())
    assert n["author"] == "adamschefter" and n["original_author"] == "espn"
    assert n["engagement"] == 0 and n["created_at"] > 0


def test_web_filter(cfg):
    from bore.collect import keep_web
    wc = cfg["apify"]["web"]
    assert keep_web("https://www.cbssports.com/nfl/news/nfl-injury-report-week-3/", wc)
    assert not keep_web("https://www.profootballrumors.com/", wc)
    assert not keep_web("https://www.reddit.com/r/nfl/new/", wc)
    assert not keep_web("https://www.youtube.com/watch?v=abc", wc)


def test_reddit_pinned_and_free_talk_dropped(cfg):
    from bore.cluster import is_boilerplate
    pats = cfg["clustering"]["boilerplate_title_patterns"]
    assert is_boilerplate({"platform": "reddit", "title": "Friday Free Talk"}, pats)
    assert is_boilerplate({"platform": "reddit", "title": "Official r/NFL Week 2 Power Rankings"}, pats)
    assert not is_boilerplate({"platform": "reddit", "title": "[Schefter] Darnold off the injury report"}, pats)
