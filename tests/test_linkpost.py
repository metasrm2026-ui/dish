"""'Post from a link': an editor pastes a URL on the dashboard; the next run makes the post."""
import io
import json
import types

import pytest
from PIL import Image

from bore import linkpost as L
from bore import routine as R
from bore.guard import lint

ARTICLE = """<html><head><title>Site title</title>
<meta property="og:title" content="Presley Gerber Death: Sober House Knew He Was High">
<meta property="og:description" content="Staff at the sober living home knew, sources tell TMZ.">
<meta property="og:image" content="https://img.example.com/lead.jpg">
<meta property="article:published_time" content="2026-09-28T18:00:00Z"></head>
<body><nav><p>Home News Sports Photos Videos Newsletters and more links here</p></nav>
<article><p>Presley Gerber was allowed to stay at the sober living facility even though staff knew he had relapsed, sources say.</p>
<p>Short.</p><script>var x = "<p>not text</p>";</script>
<p>The 26-year-old model was found on September 21, according to the coroner's office report.</p></article>
<footer><p>Copyright 2026 all rights reserved by the publisher of this site</p></footer></body></html>"""


def _jpeg(w=1400, h=1000):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (90, 40, 40)).save(b, "JPEG")
    return b.getvalue()


class _Resp:
    def __init__(self, text="", content=b"", status=200, js=None):
        self.text, self.content, self.status_code, self._js = text, content, status, js
        self.headers = {"content-type": "text/html"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._js


@pytest.fixture
def web(monkeypatch):
    import requests
    pages = {"https://www.tmz.com/2026/09/28/presley-gerber/": _Resp(ARTICLE),
             "https://img.example.com/lead.jpg": _Resp(content=_jpeg())}

    def get(url, **kw):
        if "publish.twitter.com" in url:
            return _Resp(js={"html": '<blockquote><p>Bears QB Tyson Bagent is OUT for Monday night. '
                                     '<a href="https://t.co/x">pic.twitter.com/abc</a></p>&mdash; Adam Schefter</blockquote>'})
        return pages.get(url, _Resp(status=403))
    monkeypatch.setattr(requests, "get", get)


def test_article_is_read_without_nav_scripts_or_footer(web):
    a = L.read_link("https://www.tmz.com/2026/09/28/presley-gerber/")
    assert a["title"].startswith("Presley Gerber Death") and a["author"] == "tmz.com"
    assert "relapsed" in a["text"] and "26-year-old" in a["text"]
    assert "Newsletters" not in a["text"] and "not text" not in a["text"] and "Copyright" not in a["text"]
    assert a["images"] == ["https://img.example.com/lead.jpg"]


def test_x_post_via_oembed(web):
    a = L.read_link("https://x.com/AdamSchefter/status/123")
    assert a["author"] == "adamschefter" and "Bagent is OUT" in a["text"] and "pic.twitter" not in a["text"]


def test_reddit_falls_back_to_apify(monkeypatch, cfg):
    calls = []
    ap = types.SimpleNamespace(run=lambda actor, inp, t: (calls.append(inp) or
        ([{"title": "Selena Gomez announces tour", "selftext": "Dates below", "subreddit": "popculturechat",
           "url": "https://reddit.com/r/popculturechat/comments/1/x/", "createdAt": "2026-09-29T10:00:00Z"}], "SUCCEEDED")))
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(status=403, js=None))
    a = L.read_link("https://www.reddit.com/r/popculturechat/comments/1/x/", ap, cfg)
    assert a["platform"] == "reddit" and calls[0][cfg["apify"]["reddit"]["urls_key"]][0].endswith("/1/x/")


def test_link_brief_payload_and_lint(tmp_path, cfg, web):
    req = {"id": "culture-rush-1-abc", "url": "https://www.tmz.com/2026/09/28/presley-gerber/",
           "page": "culture-rush", "note": "use the family photo", "at": 1}
    d = L.write_link_brief(tmp_path, req, cfg)
    b = json.loads((d / "brief.json").read_text())
    assert b["verification"]["route"] == "manual" and b["link"]["note"] == "use the family photo"
    assert len(b["photos"]) == 1 and b["photos"][0]["source"] == "link" and not b["link"]["read_error"]
    copy = {"template": "headline", "headline_lines": ["SOBER HOUSE", "KNEW"], "photo_index": 0,
            "caption": "Staff at Presley Gerber's sober living home knew he had relapsed.\n\nWhat do you think? 👇\n\n"
                       "#PresleyGerber #CultureRush #entertainmentnews"}
    res = lint(b, copy, R.load_playbook("culture-rush"), 800)
    assert any("attribute" in e for e in res["errors"])          # one article: say whose report it is
    copy["caption"] = copy["caption"].replace("knew he had relapsed", "knew he had relapsed, per TMZ")
    assert not any("attribute" in e for e in lint(b, copy, R.load_playbook("culture-rush"), 800)["errors"])
    (d / "copy.json").write_text(json.dumps(copy))
    (d / "card.jpg").write_bytes(_jpeg(1080, 1350))
    p = R.draft_payload(d)
    assert p["link_request"] == "culture-rush-1-abc" and p["link_url"] == req["url"]


def test_unreadable_link_needs_text_or_a_reason(tmp_path, cfg, web):
    req = {"id": "huddle-1-x", "url": "https://paywalled.example.com/story", "page": "huddle", "at": 1}
    d = L.write_link_brief(tmp_path, req, cfg)
    b = json.loads((d / "brief.json").read_text())
    assert b["link"]["read_error"] and b["posts"] == []
    res = lint(b, {"template": "report", "headline_lines": ["X"], "caption": "x per ESPN?"}, R.load_playbook("huddle"), 800)
    assert any("link-text" in e for e in res["errors"])
    L.add_source_text(d, "The Bears ruled Tyson Bagent out for Monday night, per ESPN.", "https://espn.com/s")
    assert json.loads((d / "brief.json").read_text())["posts"][0]["read_by"] == "routine"


def test_an_editors_link_is_never_blocked_for_niche_fit():
    from bore.guard import lint
    b = {"page": "demo", "verification": {"route": "manual"},
         "posts": [{"text": "A boy noticed his butterflies were hatching, per Reddit. 3 of them flew off."}],
         "photos": [{"index": 0, "file": "photos/0.jpg", "size": "1200x1200"}]}
    copy = {"template": "headline", "headline_lines": ["BUTTERFLY BOY"], "niche_fit": 3, "photo_index": 0,
            "people_in_photo": ["the boy"], "caption": "A boy noticed his butterflies hatching, per Reddit. Cute?"}
    assert not any("niche_fit" in e for e in lint(b, copy, {})["errors"])
    b["verification"]["route"] = "corroborated"
    assert any("niche_fit" in e for e in lint(b, copy, {})["errors"])


def test_a_photo_without_people_names_what_it_shows():
    from bore.guard import lint
    b = {"page": "demo", "verification": {"route": "manual"}, "posts": [{"text": "A boy studied his butterflies, per Reddit."}],
         "photos": [{"index": 0, "file": "photos/0.jpg", "size": "1200x1200"}]}
    copy = {"template": "headline", "headline_lines": ["BUTTERFLY MEMORY"], "photo_index": 0,
            "caption": "A boy studied his butterflies, per Reddit. Wild?"}
    assert any("people_in_photo" in e for e in lint(b, copy, {})["errors"])
    assert not any("people_in_photo" in e for e in lint(b, {**copy, "photo_subject": "a monarch butterfly"}, {})["errors"])


GALLERY = {"title": "NASA has released these new photos from the surface of Mars", "subreddit": "interestingasfuck",
           "selftext": "", "created_utc": 1789891868, "url": "https://www.reddit.com/gallery/1wlb5ct",
           "gallery_data": {"items": [{"media_id": "bbbbbbbbbbbbb"}, {"media_id": "aaaaaaaaaaaaa"}]},
           "media_metadata": {"aaaaaaaaaaaaa": {"status": "valid", "e": "Image", "m": "image/jpg",
                                                "s": {"u": "https://preview.redd.it/aaaaaaaaaaaaa.jpg?width=1170&amp;s=x"}},
                              "bbbbbbbbbbbbb": {"status": "valid", "e": "Image", "m": "image/png",
                                                "s": {"u": "https://preview.redd.it/bbbbbbbbbbbbb.png?width=1955&amp;s=y"}}}}


def test_reddit_gallery_through_the_archive(monkeypatch, tmp_path, cfg):
    """Reddit blocks cloud servers: the public archive gives the text and every gallery image, in order."""
    import requests
    seen = []

    def get(url, **kw):
        seen.append(url)
        if "arctic-shift" in url:
            assert kw["params"]["ids"] == "1wlb5ct"
            return _Resp(js={"data": [GALLERY]})
        if url.startswith("https://i.redd.it/"):
            return _Resp(content=_jpeg(1170, 1463))
        return _Resp(status=403)
    monkeypatch.setattr(requests, "get", get)
    a = L.read_link("https://www.reddit.com/r/interestingasfuck/comments/1wlb5ct/nasa_has_released/")
    assert a["title"].startswith("NASA") and a["images"] == ["https://i.redd.it/bbbbbbbbbbbbb.png",
                                                             "https://i.redd.it/aaaaaaaaaaaaa.jpg"]
    req = {"id": "culture-rush-2-x", "url": "https://www.reddit.com/r/interestingasfuck/comments/1wlb5ct/nasa_has_released/",
           "page": "culture-rush", "note": "No edits post images as is", "at": 1}
    b = json.loads((L.write_link_brief(tmp_path, req, cfg) / "brief.json").read_text())
    assert not b["link"]["read_error"] and [p["url"] for p in b["photos"]] == a["images"]


def test_reddit_embed_page_when_the_archive_has_nothing(monkeypatch):
    import requests
    embed = ('<meta property="og:title" content="The tree perfectly fitting into the sunset">'
             '<img src="https://preview.redd.it/the-tree-v0-cccccccccccc1.jpg?width=320&amp;s=a">'
             '<img src="https://preview.redd.it/the-tree-v0-cccccccccccc1.jpg?width=1080&amp;s=b">')

    def get(url, **kw):
        if "arctic-shift" in url:
            return _Resp(js={"data": []})
        if url.startswith("https://embed.reddit.com/"):
            return _Resp(embed)
        return _Resp(status=403)
    monkeypatch.setattr(requests, "get", get)
    a = L.read_link("https://www.reddit.com/r/interestingasfuck/comments/1wy0abt/the_tree/")
    assert a["title"] == "The tree perfectly fitting into the sunset"
    assert a["images"] == ["https://i.redd.it/cccccccccccc1.jpg"]          # the original, not the 1080 preview

    # a share link has no post id: a clear reason, so the routine reads it with its own tools
    with pytest.raises(ValueError, match="share link"):
        L.read_link("https://www.reddit.com/r/privacy/s/XaJ5kGdbva")


def test_as_is_posts_the_editors_images_unedited(tmp_path):
    from bore import routine as R
    d = tmp_path / "culture-rush-202610160900-link-mars"
    (d / "photos").mkdir(parents=True)
    photos = []
    for i, (w, h) in enumerate([(1170, 1463), (3000, 2000), (1955, 1019)]):
        Image.new("RGB", (w, h), (20 * i, 40, 60)).save(d / f"photos/{i}.jpg", "JPEG")
        photos.append({"index": i, "file": f"photos/{i}.jpg", "url": f"https://i.redd.it/{i}.jpg", "source": "link",
                       "credit": "reddit.com", "size": f"{w}x{h}"})
    brief = {"id": d.name, "page": "culture-rush", "page_name": "Culture Rush", "photos": photos, "posts": [],
             "verification": {"route": "manual"}, "link": {"request_id": "r1", "url": "https://reddit.com/x", "note": "as is"}}
    copy = {"as_is": True, "photos": [2, 0, 1], "photo_index": 2, "caption": "x", "headline_lines": ["MARS"]}
    (d / "brief.json").write_text(json.dumps(brief))
    (d / "copy.json").write_text(json.dumps(copy))
    res = R.render_as_is(d)
    assert res["images"] == 3
    assert Image.open(d / "card.jpg").size == (1955, 1019)            # the first chosen, not cropped or carded
    assert Image.open(d / "extra/2.jpg").size == (2048, 1365)         # only scaled to what Facebook shows
    p = R.draft_payload(d)
    assert p["as_is"] and len(p["extra_images_b64"]) == 2 and p["engine"] == "as-is"
    # only an editor's request can ask for it
    errs = lint({**brief, "link": None}, copy, R.load_playbook("culture-rush"), 800)["errors"]
    assert any("as_is is only" in e for e in errs)
    errs = lint({**brief, "link": {**brief["link"], "note": "make it high ctr"}}, copy, R.load_playbook("culture-rush"), 800)["errors"]
    assert any("as_is is only" in e for e in errs)
    for note in ("No edits post images as is", "dont do any branding on the image whatsoever"):
        errs = lint({**brief, "link": {**brief["link"], "note": note}}, copy, R.load_playbook("culture-rush"), 800)["errors"]
        assert not any("as_is" in e for e in errs), (note, errs)


def test_the_helpline_line_is_not_a_fact_to_check():
    from bore.guard import HELPLINE_RX
    cap = "Final line.\n\nIf you or someone you know is struggling, call or text 988 (US).\n\n#News"
    assert "988" not in HELPLINE_RX.sub("", cap)


def test_feedback_on_an_as_is_post_keeps_its_images_in_order(tmp_path, cfg, monkeypatch):
    import requests

    from bore import routine as R
    jpg = _jpeg(1200, 900)
    monkeypatch.setattr(requests, "get", lambda url, **k: types.SimpleNamespace(ok=True, content=jpg + url.encode()))
    snap = {"posts": [{"text": "NASA photos"}], "photos": [{"url": f"https://i.redd.it/{n}.jpg", "source": "link"} for n in "abc"]}
    req = {"id": "culture-rush-202610160900-link-mars", "page": "culture-rush", "feedback": "punchier caption",
           "brief": snap, "copy": {"as_is": True, "photos": [2, 0], "photo_index": 2}, "link_note": "post images as is",
           "source_photo": "https://i.redd.it/c.jpg", "reference_image_url": "https://img.example.com/upload.jpg"}
    b = json.loads((R.write_regen_brief(tmp_path, req, cfg) / "brief.json").read_text())
    reg = b["regeneration"]
    assert reg["as_is"] and [b["photos"][i]["url"] for i in reg["as_is_photos"]] == ["https://i.redd.it/c.jpg", "https://i.redd.it/a.jpg"]
    copy = {"as_is": True, "photos": reg["as_is_photos"], "photo_index": reg["as_is_photos"][0], "caption": "x"}
    assert not any("as_is" in e for e in lint(b, copy, R.load_playbook("culture-rush"), 800)["errors"])
