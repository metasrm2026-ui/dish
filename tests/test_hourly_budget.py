import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bore.budget import Pacer
from bore.cli import page_quota
from bore.config import Page
from bore.select import select_page


def test_pacer_spreads_remaining_budget():
    now = time.time()
    p = Pacer(ceiling=16.0, used=6.0, cycle_end=now + 10 * 86400, spent_today=0.5, now=now)
    assert abs(p.daily_allowance - 1.0) < 1e-6
    assert p.allows(0.4) and not p.allows(0.6)
    over = Pacer(ceiling=16.0, used=16.5, cycle_end=now + 5 * 86400, spent_today=0, now=now)
    assert over.daily_allowance == 0 and not over.allows(0.001)


def test_the_pacer_counts_apify_spend_only(store):
    from types import SimpleNamespace as NS

    from bore.budget import make_pacer
    now = time.time()
    store.record_spend("krea:flux", 0.45)               # card art: its own monthly cap
    store.record_spend("apify/reddit-scraper", 0.15)
    apify = NS(usage=lambda: (6.0, 16.0), ceiling=lambda cap: cap, cycle={"endAt": None})
    p = make_pacer(apify, store, now)
    assert abs(p.spent_today - 0.15) < 1e-9 and p.allows(0.15)     # the afternoon Reddit pass still runs ($0.33/day)


def _at(hour):
    tz = ZoneInfo("America/New_York")
    return datetime.now(tz).replace(hour=hour, minute=10, second=0, microsecond=0).timestamp()


def test_page_quota_paces_to_daily_cap(cfg, store):
    page = Page.from_dict({"slug": "p", "name": "P", "timezone": "America/New_York", "posts_per_day": 20})
    assert page_quota(page, store, cfg, _at(3)) == 0             # quiet hours
    assert page_quota(page, store, cfg, _at(9)) == 2             # 20 over 14 hours left
    assert page_quota(page, store, cfg, _at(20)) == 3            # behind: catch up, max 3
    for k in range(20):                                          # briefed or skipped stories are not posts
        store.record_sent("p", f"k{k}", ["a"], 0, 1, 0, "[skipped] h", _at(8))
    store.commit()
    assert page_quota(page, store, cfg, _at(20)) == 3
    for k in range(20):                                          # 20 uploads: daily cap reached
        store.db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?)", ("run", "p", f"b{k}", "uploaded", "", "h", _at(8)))
    store.commit()
    assert page_quota(page, store, cfg, _at(20)) == 0


def test_exclude_terms_and_contentless_picks(cfg):
    now = time.time()
    page = Page.from_dict({"slug": "h", "name": "Demo Sports", "exclude_terms": ["high school"]})
    items = [
        {"id": "a", "platform": "web", "author": "ktal.com", "created_at": now - 600,
         "title": "Week 5: Texas high school football scores & highlights"},
        {"id": "b", "platform": "web", "author": "wjhl.com", "created_at": now - 600,
         "title": "Week 5 Texas high school football scores and highlights"},
        {"id": "c", "platform": "x", "author": "albertbreer", "created_at": now - 600, "text": "Unforgettable.",
         "engagement": 90000},
        {"id": "d", "platform": "x", "author": "adamschefter", "created_at": now - 600, "engagement": 2000,
         "text": "Players ruled out for Sunday include Devin Lloyd, Rico Dowdle and Jadeveon Clowney"},
    ]
    res = select_page(page, items, cfg, [], now, quota=2)
    ids = {m["id"] for t in res.shortlist for m in t.cluster.members}
    assert ids == {"d"}


def test_photo_only_skip_comes_back_after_a_few_hours(cfg, store, monkeypatch):
    from bore import cli
    monkeypatch.setattr(cli, "_store", lambda c: store)
    brief = {"page": "p", "key": "k1", "signature": ["a", "b"], "verification": {}, "headline_hint": "Jeanty limited"}
    now = time.time()
    cli._remember_skip(brief, cfg, photo=True)
    hrs = cfg["dedup"]["suppress_hours"]
    assert any(r["key"] == "k1" for r in store.sent_for_page("p", now - hrs * 3600))          # blocked for now
    assert not any(r["key"] == "k1" for r in store.sent_for_page("p", now + (cfg["posts"]["photo_retry_hours"] + 0.1) * 3600 - hrs * 3600))
    cli._remember_skip(brief, cfg, photo=True)
    cli._remember_skip(brief, cfg, photo=True)                   # third time: blocked like any skip
    assert any(r["key"] == "k1" for r in store.sent_for_page("p", now + 5 * 3600 - hrs * 3600))
    cli._remember_skip({**brief, "key": "k2"}, cfg)              # an ordinary skip stays blocked
    assert any(r["key"] == "k2" for r in store.sent_for_page("p", now + 5 * 3600 - hrs * 3600))


def test_photo_retries_count_per_story_even_when_its_key_drifts(cfg, store, monkeypatch):
    from bore import cli
    monkeypatch.setattr(cli, "_store", lambda c: store)
    now = time.time()
    hrs = cfg["dedup"]["suppress_hours"]
    sig = ["jeanty", "limited", "practice", "raider", "ankle", "wednesday"]
    for i, key in enumerate(("k1", "k2", "k3")):            # new coverage shifts the key each time
        cli._remember_skip({"page": "p", "key": key, "signature": sig[:5] + [f"x{i}"], "verification": {},
                            "headline_hint": "Jeanty limited"}, cfg, photo=True)
    rows = {r["key"]: r for r in store.sent_for_page("p", now - (hrs + 24) * 3600)}
    assert rows["k1"]["headline"].startswith("[photo-skip]") and rows["k2"]["headline"].startswith("[photo-skip]")
    assert rows["k3"]["headline"].startswith("[skipped]")     # third time for the same story: blocked


def test_website_only_pages_keep_the_days_unbriefed_articles(cfg):
    """Demo Celeb (one website's feed) ran out of candidates by evening on Oct 12 while articles
    from that morning had aged out of the 12-hour window unbriefed."""
    from bore.select import website_only, window_hours
    now = time.time()
    site = Page.from_dict({"slug": "w", "name": "W", "web_searches": ["site:example.com"],
                           "feeds": ["https://example.com/feed"]})
    mixed = Page.from_dict({"slug": "m", "name": "M", "x_handles": ["reporter"], "web_searches": ["site:example.com"]})
    assert website_only(site) and not website_only(mixed)
    assert window_hours(site, cfg) == 24 and window_hours(mixed, cfg) == 12
    item = {"id": "a", "platform": "web", "author": "example.com", "url": "https://example.com/a", "engagement": 0,
            "title": "Singer Ann Example announces split from her husband after ten years", "text": "",
            "created_at": now - 18 * 3600}
    assert len(select_page(site, [item], cfg, [], now, quota=2).shortlist) == 1
    assert select_page(mixed, [item], cfg, [], now, quota=2).shortlist == []
    old = {**item, "created_at": now - 30 * 3600}                # never past the freshness gate
    assert select_page(site, [old], cfg, [], now, quota=2).shortlist == []


def test_a_reason_only_about_the_photo_counts_as_a_photo_skip(cfg, store, monkeypatch, tmp_path):
    """No story was ever retried for its photo (Oct 9-13): the routine wrote "no sharp photo of X"
    about 14 times a day on Demo Sports but never passed --photo."""
    import json
    from argparse import Namespace
    from bore import cli, routine as R
    monkeypatch.setattr(cli, "_store", lambda c: store)
    for r in ("No sharp photo of Jake Example found despite multiple searches",
              "Only candidate photo is a wide stadium shot; the face renders at 59px on the card",
              "Good injury story but every available photo has a watermark tiled across it",
              "No usable photo found for Jake Example after repeated searches"):
        assert cli.photo_only(r), r
    for r in ("Weak story: single source, and no photo either", "Same story as a pending post; no photo",
              "Off-niche weather story, zero usable photos", "Pure cast-preview guide with no photo and no hook",
              "Politics/policy story, explicitly NO in the playbook", "Thin one-line blurb, no photo",
              "All photos show him in a college uniform; cannot confirm this is NFL content. The page covers NFL only",
              "Cryptic teaser tweet with no clear postable fact; the attached image is a graphic",
              "Generic viewership-numbers story, not about a specific player, no photo",
              "The single tweet names no one, leaving no way to find a photo of the twins"):
        assert not cli.photo_only(r), r
    assert cli.photo_only("Only photos are old college-uniform shots; no current NFL photo of him exists yet")

    def brief_dir(name, key):
        d = tmp_path / "run" / name
        d.mkdir(parents=True)
        (d / "brief.json").write_text(json.dumps({"page": "p", "key": key, "signature": ["a", key],
                                                  "verification": {}, "headline_hint": "Example limited"}))
        return d
    cli._routine_skip(Namespace(dir=str(brief_dir("b1", "k1")), reason="No sharp photo of Jake Example found",
                                photo=False), cfg, R)
    cli._routine_skip(Namespace(dir=str(brief_dir("b2", "k2")), reason="Weak story, single source", photo=False), cfg, R)
    d3 = brief_dir("b3", "k3")                                   # a skip written in copy.json, then lint
    (d3 / "copy.json").write_text(json.dumps({"decision": "skip", "reason": "No usable photo of Jake Example"}))
    cli._lint_or_exit(d3, cfg, R)
    rows = {r["key"]: r["headline"] for r in store.sent_for_page("p", 0)}
    assert rows["k1"].startswith("[photo-skip]") and rows["k3"].startswith("[photo-skip]")
    assert rows["k2"].startswith("[skipped]")
    # linting or skipping the same brief again is one skip, not three: it must not use up the retries
    cli._lint_or_exit(d3, cfg, R)
    cli._lint_or_exit(d3, cfg, R)
    cli._routine_skip(Namespace(dir=str(d3), reason="No usable photo of Jake Example", photo=True), cfg, R)
    assert store.get_kv("photo_retry:p:k3") == 1
    assert {r["key"]: r["headline"] for r in store.sent_for_page("p", 0)}["k3"].startswith("[photo-skip]")


# ------------------------------------------------------------------ only live pages run
def _statuses():
    return [Page.from_dict({"slug": s, "name": s, "status": s}) for s in ("live", "paused", "draft", "going_live", "failed")]


def test_only_live_pages_run_unless_a_page_going_live_is_named(monkeypatch):
    """The hourly run, its collection and the refresh work on live pages only. A page going live runs
    when it is named (the onboarding routine's first run); a paused one never does."""
    from types import SimpleNamespace as NS

    import pytest

    from bore import cli
    monkeypatch.setattr(cli, "load_pages", _statuses)
    assert [p.slug for p in cli._pages(NS(page=None), {}, running=True)] == ["live"]
    assert len(cli._pages(NS(page=None), {})) == 5                       # listing and settings see them all
    assert [p.slug for p in cli._pages(NS(page="going_live"), {}, running=True)] == ["going_live"]
    for slug in ("paused", "draft", "failed"):
        with pytest.raises(SystemExit, match=f"page {slug} is {slug}, not live: it does not run"):
            cli._pages(NS(page=slug), {}, running=True)


def test_with_no_live_page_the_hourly_run_collects_nothing(monkeypatch, cfg, store, capsys):
    from types import SimpleNamespace as NS

    from bore import cli
    monkeypatch.setattr(cli, "load_pages", lambda: [p for p in _statuses() if p.slug != "live"])
    monkeypatch.setattr(cli, "_store", lambda c: store)
    monkeypatch.setattr(cli, "_apify", lambda *a: (_ for _ in ()).throw(AssertionError("Apify was asked")))
    _, apify, pacer, results, log, _ = cli._hourly_select(NS(page=None), cfg)
    assert apify is None and results == [] and "nothing collected" in pacer.summary()
    assert "not running for 4 page(s) that are not live: draft, failed, going_live, paused" in capsys.readouterr().out
