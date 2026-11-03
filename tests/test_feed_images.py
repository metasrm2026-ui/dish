"""Topic feed images: checked lists are reused, the check fields reach the dashboard, a story's text
only carries over to the same posts, and the placeholder and text rules spare real photos."""
import json
from types import SimpleNamespace as NS

from bore import stills, studio
from bore.config import Page
from bore.media import Photo

PAGE = Page.from_dict({"slug": "demo", "name": "Demo"})
NOW = 1_800_000_000.0


def trend(key, urls):
    members = [{"platform": "x", "url": u, "engagement": 1, "created_at": NOW - 600} for u in urls]
    return NS(key=key, score=1.0, kind="", headline=key, route="", cluster=NS(last_seen=NOW - 600, members=members))


def story(t, *_a, **_k):
    urls = [m["url"] for m in t.cluster.members]
    return {"id": t.key, "headline_hint": t.key, "score": 1.0, "published_at": 0, "url": urls[0],
            "sources": [{"name": "a", "url": u} for u in urls], "subject": "", "kind": "", "learned": "",
            "images": [{"url": "https://img.example.org/raw.jpg", "source": "x", "credit": "@a"}]}


def run_feed(monkeypatch, tmp_path, trends, old, checked=None):
    import bore.select
    monkeypatch.setattr(studio, "load_pages", lambda: [PAGE])
    monkeypatch.setattr(studio, "studio_meta", lambda: {"demo": {}})
    monkeypatch.setattr(studio, "_page_templates", lambda slug: [])
    monkeypatch.setattr(studio, "_get", lambda dash, path, **k: {"feed": {"stories": old}})
    monkeypatch.setattr(studio, "_story_from_trend", story)
    monkeypatch.setattr(stills, "video_policy", lambda page: "never")
    monkeypatch.setattr(bore.select, "select_page", lambda *a, **k: NS(trends=trends, watchlist=[]))
    calls = []

    def feed_images(t, policy, images, same=None, searches=None, learned=None):
        calls.append(t.key)
        return checked if checked is not None else [{"url": "https://img.example.org/ok.jpg", "source": "x"}]
    monkeypatch.setattr(studio, "_feed_images", feed_images)
    cfg = {"clustering": {"window_hours": 6}}
    (f,) = studio.prepare_feed(tmp_path, cfg, NS(page_items=lambda *a: []), dash=object(), now=NOW)
    return {s["id"]: s for s in json.loads(f.read_text())["stories"]}, calls


def test_checked_empty_list_is_reused_not_replaced_by_raw_candidates(monkeypatch, tmp_path):
    t = trend("k1", ["https://x.com/a/status/1", "https://x.com/b/status/2"])
    old = [{**story(t), "images": [], "images_v": studio.IMAGES_V, "images_at": NOW - 600, "summary": "s",
            "ideas": [{}, {}], "headline": "Written"}]
    got, calls = run_feed(monkeypatch, tmp_path, [t], old)
    assert got["k1"]["images"] == [] and calls == [] and got["k1"]["headline"] == "Written"


def test_thin_lists_are_checked_again_after_three_hours(monkeypatch, tmp_path):
    t = trend("k1", ["https://x.com/a/status/1", "https://x.com/b/status/2"])
    old = [{**story(t), "images": [], "images_v": studio.IMAGES_V, "images_at": NOW - 4 * 3600}]
    got, calls = run_feed(monkeypatch, tmp_path, [t], old)
    assert calls == ["k1"] and got["k1"]["images_at"] == NOW


def test_same_id_with_different_posts_is_rewritten(monkeypatch, tmp_path):
    old_t = trend("k1", [f"https://x.com/u/status/{i}" for i in range(4)])
    new_t = trend("k1", [f"https://x.com/v/status/{i}" for i in range(4)])
    old = [{**story(old_t), "headline": "Commanders end-of-game mistake", "summary": "old", "ideas": [{}, {}],
            "images_v": studio.IMAGES_V, "images_at": NOW, "images": [{"url": "https://img.example.org/old.jpg"}]}]
    got, calls = run_feed(monkeypatch, tmp_path, [new_t], old)
    s = got["k1"]
    assert s.get("needs_writing") and "headline" not in s and calls == ["k1"]


def test_images_checked_before_face_matching_are_kept_and_only_get_faces(monkeypatch, tmp_path):
    t = trend("k1", ["https://x.com/a/status/1", "https://x.com/b/status/2"])
    kept = [{"url": "https://img.example.org/a.jpg", "source": "x"}, {"url": "https://img.example.org/b.jpg", "source": "x"}]
    old = [{**story(t), "images": kept, "images_v": studio.FACELESS_V, "images_at": NOW - 600}]
    faced = []
    monkeypatch.setattr(studio, "_feed_faces", lambda t, policy, images: faced.append(t.key) or
                        [{**images[1], "id_match": "match"}, images[0]])
    got, calls = run_feed(monkeypatch, tmp_path, [t], old)
    s = got["k1"]
    assert calls == [] and faced == ["k1"]                         # no full re-check
    assert s["images_v"] == studio.IMAGES_V and s["images_at"] == NOW - 600 and s["images"][0]["id_match"] == "match"
    # out of time this hour: kept as checked (not 'not checked yet'), faces next hour
    monkeypatch.setattr(studio, "FEED_IMAGE_BUDGET_S", 0)
    got, calls = run_feed(monkeypatch, tmp_path, [t], old)
    assert got["k1"]["images"] == kept and got["k1"]["images_v"] == studio.FACELESS_V and calls == []


def test_past_the_budget_images_are_marked_unchecked(monkeypatch, tmp_path):
    monkeypatch.setattr(studio, "FEED_IMAGE_BUDGET_S", 0)
    got, calls = run_feed(monkeypatch, tmp_path, [trend("k1", ["https://x.com/a/status/1"])], [])
    assert calls == [] and got["k1"]["images"][0]["flags"][0] == "not checked yet"


def test_publish_feed_sends_the_check_fields(monkeypatch, tmp_path):
    (tmp_path / "feed").mkdir()
    s = {"id": "k1", "headline": "H", "summary": "", "sources": [], "published_at": 0, "url": "", "score": 1,
         "images": [], "images_v": studio.IMAGES_V, "images_at": NOW, "subject": "Dre Greenlaw",
         "kind": "off_field", "learned": "Off-field: editors approved 2 of 3"}
    (tmp_path / "feed" / "demo.json").write_text(json.dumps({"page": "demo", "stories": [s]}))
    sent = []
    monkeypatch.setattr(studio, "_post", lambda dash, path, body: sent.append(body) or {})
    studio.publish_feed(tmp_path, dash=object())
    (out,) = sent[0]["stories"]
    assert {k: out[k] for k in ("images_v", "images_at", "subject", "kind", "learned")} == \
        {k: s[k] for k in ("images_v", "images_at", "subject", "kind", "learned")}


def img(h, subject=None, **k):
    return {"url": "https://img.example.org/p.jpg", "source": "web", "_h": h, **k}


def test_placeholder_rule_spares_stories_about_one_person():
    shared = img(7)
    a = {"subject": "Marvin Harrison Jr.", "images": [shared]}
    b = {"subject": "Marvin Harrison Jr.", "images": [img(7)]}
    assert not studio._placeholder([a, b], shared)
    b["subject"] = "Dre Greenlaw"
    assert studio._placeholder([a, b], shared)
    c = {"subject": "Marvin Harrison Jr.", "images": [img(7)]}
    b["subject"] = "Marvin Harrison Jr."
    assert studio._placeholder([a, b, c], shared)                     # three stories: a placeholder
    assert not studio._placeholder([a, b, c], img(7, consensus=3))   # the picture outlets shared


def test_rank_puts_shared_clean_named_photos_first():
    members = [{"url": "https://x.com/a/status/1", "text": "Greenlaw suspended two games"},
               {"url": "https://x.com/b/status/2", "text": "Week 6 notes"}]
    unnamed = Photo("u1", "x", "@b", 1600, 1200, post_url="https://x.com/b/status/2")
    named = Photo("u2", "x", "@a", 1600, 1200, post_url="https://x.com/a/status/1")
    soft = Photo("u3", "x", "@a", 1600, 1200, post_url="https://x.com/a/status/1", flags=["soft (sharpness 20)"])
    shared = Photo("u4", "news", "outlet", 1200, 800, consensus=3, flags=["soft (likely an upscaled video frame)"])
    ranked = sorted([unnamed, soft, named, shared], key=lambda p: studio._image_rank(p, "Dre Greenlaw", members, set()))
    assert [p.url for p in ranked] == ["u4", "u2", "u1", "u3"]


def fake_ocr(monkeypatch, lines):
    import bore.guard
    monkeypatch.setattr(bore.guard, "_ocr", lambda path: [(t, 0.9, b) for t, b in lines])
    monkeypatch.setattr(bore.guard, "detect_faces", lambda bgr: [])


def photo():
    import io

    from PIL import Image
    im = Image.new("RGB", (960, 640))
    px = im.load()
    for x in range(960):
        for y in range(640):
            px[x, y] = ((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return Photo("u", "x", "@a", 960, 640, data=buf.getvalue())


def test_quote_card_is_text_heavy_but_a_backdrop_is_not(monkeypatch):
    card = [("I DON'T KNOW WHAT", (100, 100, 860, 160)), ("HAPPENED ON THAT PLAY", (100, 170, 860, 230)),
            ("- JIM HARBAUGH", (300, 240, 660, 280))]
    fake_ocr(monkeypatch, card)
    assert any(f.startswith("text-heavy") for f in stills.inspect_still(photo()).flags)
    backdrop = [("CREATIVE ARTS", (10, 10, 200, 40)), ("EMMYS", (220, 10, 320, 40)), ("Television Academy", (10, 50, 260, 80)),
                ("CREATIVE ARTS", (500, 10, 690, 40)), ("EMMYS", (710, 10, 810, 40)), ("Television Academy", (500, 50, 750, 80))]
    fake_ocr(monkeypatch, backdrop)
    flags = stills.inspect_still(photo()).flags
    assert not any(f.startswith(("text-heavy", "burned-in")) for f in flags)


def test_jersey_numbers_and_network_names_are_not_flagged(monkeypatch):
    fake_ocr(monkeypatch, [("18", (400, 300, 520, 420)), ("NBCU", (10, 10, 120, 50)), ("FOX", (800, 10, 900, 50))])
    flags = stills.inspect_still(photo()).flags
    assert not any(f.startswith(("watermark", "burned-in", "text-heavy")) for f in flags)
    fake_ocr(monkeypatch, [("@clipsguy", (10, 600, 200, 630))])
    assert any(f.startswith("watermark") for f in stills.inspect_still(photo()).flags)


def test_roundup_articles_count_as_list_posts():
    from bore.media import list_post
    assert list_post({"platform": "web", "title": "Week 6 winners and losers: Bears rally"})
    assert list_post({"platform": "web", "title": "Live updates: Chiefs vs. Bills"})
    assert not list_post({"platform": "web", "title": "Dre Greenlaw suspended two games"})


def test_final_score_card_is_a_scoreboard(monkeypatch):
    fake_ocr(monkeypatch, [("5", (100, 100, 140, 140)), ("32", (200, 100, 260, 140)), ("29", (300, 100, 360, 140)),
                           ("FINAL", (400, 100, 520, 140))])
    assert "scoreboard" in stills.inspect_still(photo(), sports=True).flags


def test_downloads_stopped_at_the_deadline_do_not_throw_the_story_away(monkeypatch):
    """Oct 22-23: a download cancelled at the deadline raised CancelledError when asked for its error,
    so the story lost the photos already found and every topic run was marked degraded."""
    import time as _t

    from bore import consensus
    from bore import ops
    now = _t.time()
    arts = lambda *a, **k: [NS(url=f"https://o{i}.example.com/a", domain=f"o{i}.example.com", ts=now, title="Bears beat Patriots",
                               rank=i, img="", thumb="", maxw=0, maxh=0, flags=[], h=None) for i in range(30)]
    monkeypatch.setattr(consensus, "_engine", arts)                 # 30 articles: more downloads than workers
    monkeypatch.setattr(consensus, "_load", lambda a: _t.sleep(3))  # none finishes before the deadline
    marked = []
    monkeypatch.setattr(ops, "degraded", lambda why: marked.append(why))
    try:
        consensus.consensus_photos("Bears beat Patriots", now - 3600, deadline_s=0.5)
    except Exception as e:                          # whatever the search does here, never a CancelledError
        assert "Cancelled" not in type(e).__name__
    assert not any("unavailable" in m for m in marked)
