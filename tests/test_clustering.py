import time

from bore.cluster import cluster_items
from bore.config import Page
from bore.fixture import evaluate, load
from bore.select import select_page

from conftest import FIXTURES


def test_regression_fixture_passes_at_configured_threshold(cfg):
    data = load(FIXTURES / "regression.json")
    assert evaluate(data, cfg) == []


def test_real_window_passes_at_configured_threshold(cfg):
    """Real Culture Rush window: 29 Olivia Rodrigo posts (hashtag and plain text, 6 sources) are
    one story; Kardashian/Hamilton and Deadliest Catch stay intact and separate."""
    data = load(FIXTURES / "real_culture_rush_20260926.json")
    assert evaluate(data, cfg) == []


def test_names_identify_stories_templates_do_not():
    from bore.textutil import entities
    assert {"olivia", "rodrigo", "unravel", "tour"} <= entities("Olivia Rodrigo on the #UnraveledTour")
    assert entities("Kim Kardashian and Lewis Hamilton Share Steamy Kiss in Romantic Paris Photos") == set()
    assert "thursday" not in entities("Ravens injury report: Zay Flowers limited Thursday")


def _clusters(cfg):
    now = time.time()
    data = load(FIXTURES / "regression.json", now)
    clusters, _ = cluster_items(data["items"], cfg, now)
    return data, clusters


def test_repost_counts_original_author_as_source_without_engagement(cfg):
    _, clusters = _clusters(cfg)
    hurts = next(c for c in clusters if any(m["id"] == "qa1" for m in c.members))
    assert "x:adamschefter" in hurts.sources and "x:eaglesbeat" in hurts.sources
    repost = next(m for m in hurts.members if m["id"] == "qa2")
    assert repost["engagement"] == 0


def test_boilerplate_and_stale_items_are_dropped(cfg):
    _, clusters = _clusters(cfg)
    ids = {m["id"] for c in clusters for m in c.members}
    assert "rd1" not in ids and "rd2" not in ids      # game thread, daily discussion
    assert "old1" not in ids                           # 30 h old: hard 24 h gate
    assert "rd3" in ids                                # a real Reddit story survives


def test_bridge_post_does_not_chain_two_qb_stories(cfg):
    _, clusters = _clusters(cfg)
    a = next(c for c in clusters if any(m["id"] == "qa1" for m in c.members))
    assert not any(m["id"].startswith("qb") for m in a.members)


def test_scoop_route_and_its_guards(cfg):
    now = time.time()
    data = load(FIXTURES / "regression.json", now)
    page = Page.from_dict(data["page"])
    res = select_page(page, data["items"], cfg, [], now)
    routes = {m["id"]: t.route for t in res.trends for m in t.cluster.members}
    assert routes.get("sc1") == "scoop"
    # the league feed is official AND high-volume: never a scoop
    assert not any(k.startswith("mlb") for k in routes)
    # even if listed as tier-one, >25 posts in the window disqualifies it
    page.tier_one.append("mlb")
    page.officials = []
    res = select_page(page, data["items"], cfg, [], now)
    assert not any(m["id"].startswith("mlb") for t in res.trends for m in t.cluster.members)


def test_min_shared_tokens(cfg):
    now = time.time()
    items = [{"id": "a", "platform": "x", "author": "u1", "text": "Please Bills fans", "created_at": now},
             {"id": "b", "platform": "x", "author": "u2", "text": "please Bills win", "created_at": now}]
    clusters, _ = cluster_items(items, cfg, now, threshold=0.1)
    assert len(clusters) == 2


def test_three_char_tokens_kept():
    from bore.textutil import tokens
    assert {"zay", "mcl", "usc"} <= set(tokens("Zay tore his MCL at USC"))
