"""Post Studio: page settings from the admin's sources, ideas/caption checks, generic layouts."""
import io

from bore.config import Page
from bore.guard import card_strings, lint
from bore.studio import _split_caption, check_ideas, classify, page_from_config, sources_from_page

PAGE = Page.from_dict({"slug": "demo", "name": "Demo", "x_handles": ["reporter1", "outlet1"], "tier_one": ["reporter1"],
                       "officials": ["outlet1"], "x_handles_secondary": ["minor1"], "subreddits": ["demo"],
                       "subreddits_rotation": ["demo2"], "web_searches": ["demo news today"], "web_rotation": ["demo rumours"]})


def test_sources_round_trip_and_disable():
    pl = {k: v for k, v in PAGE.to_dict().items() if k not in ("slug", "name")}
    src = sources_from_page(PAGE)
    back = page_from_config({"slug": "demo", "name": "Demo", "pipeline": pl, "sources": src})
    for k in ("x_handles", "x_handles_secondary", "tier_one", "officials", "subreddits", "subreddits_rotation",
              "web_searches", "web_rotation"):
        assert getattr(back, k) == getattr(PAGE, k), k
    # an admin disables the reporter and adds a website, a subreddit and a search
    for s in src:
        if s["url"].endswith("/reporter1"):
            s["active"] = False
    src += [{"url": "https://www.example.com/sport", "type": "website"}, {"url": "https://reddit.com/r/New/", "type": "reddit"},
            {"url": "demo injuries", "type": "search"}]
    p = page_from_config({"slug": "demo", "name": "Demo", "pipeline": pl, "sources": src})
    assert "reporter1" not in p.x_handles and "reporter1" not in p.tier_one
    assert "new" in p.subreddits and "site:example.com/sport" in p.web_searches and "demo injuries" in p.web_searches


def test_new_admin_page_without_pipeline_is_studio_only():
    p = page_from_config({"slug": "n", "name": "N", "sources": [{"url": "https://x.com/SomeOne", "type": "social"}]})
    assert p.x_handles == ["someone"] and p.hourly is False
    assert classify({"url": "https://www.instagram.com/x", "type": "social"})[0] == "other"


def test_ideas_need_two_with_a_drawable_layout():
    ok = [{"title": "A", "template": "Big number", "layout": "stat_card", "description": "d"},
          {"title": "B", "template": "Versus", "layout": "comparison", "description": "d"}]
    assert check_ideas(ok, "demo") == []
    assert check_ideas(ok[:1], "demo")
    assert any("layout" in e for e in check_ideas([ok[0], {**ok[1], "layout": "collage"}], "demo"))


def test_caption_and_hashtags_split():
    assert _split_caption({"caption": "News.\n\nThoughts?\n#A #B"}) == ("News.\n\nThoughts?", ["#A", "#B"])
    assert _split_caption({"caption": "News?", "hashtags": ["A", "#B"]}) == ("News?", ["#A", "#B"])


def _brief(**kw):
    return {"page": "demo", "page_name": "Demo", "verification": {"route": "studio"},
            "posts": [{"text": "Player One signed for 3 years. Player One and Player Two compared: 31 and 35 touchdowns."}],
            "photos": [{"index": 0, "file": "photos/0.jpg", "size": "700x600", "source": "upload"},
                       {"index": 1, "file": "photos/1.jpg", "size": "1200x1200"}], **kw}


def test_studio_lint_rules():
    copy = {"template": "comparison", "headline_lines": ["WHO WAS BETTER?"], "photo_index": 0, "extra_photo_indexes": [1],
            "people_in_photo": ["Player One"], "caption": "Player One or Player Two?", "hashtags": ["#Demo"],
            "items": [{"label": "Player One"}, {"label": "Player Two"}, {"label": "TDs", "left": "31", "right": "35"}]}
    res = lint(_brief(), copy, {})
    assert res["pass"], res          # no niche_fit needed, small editor image is only a warning
    assert any("soft" in w for w in res["warnings"])
    assert "35" in " ".join(card_strings(copy))
    bad = lint(_brief(), {**copy, "items": [{"label": "Player One"}]}, {})
    assert any("needs items" in e for e in bad["errors"])
    wrong = lint(_brief(), {**copy, "items": copy["items"][:2] + [{"label": "TDs", "left": "40", "right": "35"}]}, {})
    assert any("'40'" in e for e in wrong["errors"])
    pb = {"name": "Demo", "lint": {"hashtags": {"min": 2, "max": 3, "required": "#Demo"}}}
    assert any("2-3 hashtags" in e for e in lint(_brief(), copy, pb)["errors"])


def _jpeg(w, h, color=(90, 120, 160)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "JPEG")
    return b.getvalue()


def test_generic_layouts_render_in_the_page_colours():
    from PIL import Image
    from bore.cards import CardSpec, render
    theme = {"accent": "#e63946", "bg": "#0b1d3a", "ink": "#ffffff", "logo_text": "Demo"}
    for tpl, kw in (("comparison", {"items": [{"label": "A"}, {"label": "B"}, {"label": "TDs", "left": "31", "right": "35"}]}),
                    ("timeline", {"items": [{"date": "Mar 3", "text": "Talks"}, {"date": "Mar 9", "text": "Deal"}]})):
        spec = CardSpec(template=tpl, headline_lines=["HEADLINE"], photos=[_jpeg(1200, 900), _jpeg(900, 1200)], theme=theme, **kw)
        im = Image.open(io.BytesIO(render("demo", spec))).convert("RGB")
        assert im.size == (1080, 1350)
        assert im.getpixel((540, 1340)) == (11, 29, 58) or sum(abs(a - b) for a, b in zip(im.getpixel((540, 1340)), (11, 29, 58))) < 12


def test_generic_layout_zooms_onto_a_small_face():
    from bore.layouts import _placement
    img = _jpeg(1600, 1000)
    wide = _placement(img, (0, 0, 1080, 720), (0.5, 0.3), (0.45, 0.2, 0.05, 0.08))
    plain = _placement(img, (0, 0, 1080, 720), (0.5, 0.3), (0.3, 0.1, 0.3, 0.5))
    w = lambda s: int(s.split("width:")[1].split("px")[0])
    assert w(wide) > w(plain) == 1152


def test_compose_must_use_the_editors_photo(tmp_path):
    import json
    import os
    import pytest
    from bore.studio import post_result
    d = tmp_path / "p"
    d.mkdir()
    brief = {"page": "demo", "studio": {"base_image": {"url": "https://x/chosen.jpg"}},
             "photos": [{"index": 0, "url": "https://x/chosen.jpg"}, {"index": 1, "url": "https://x/other.jpg"}]}
    (d / "brief.json").write_text(json.dumps(brief))
    for f, v in (("lint.json", {"pass": True}), ("card.jpg", None), ("ocr.json", {"pass": True}), ("verdict.json", {"pass": True})):
        (d / f).write_bytes(b"jpg") if v is None else (d / f).write_text(json.dumps(v))
    os.utime(d / "card.jpg", (1, 1))
    (d / "copy.json").write_text(json.dumps({"photo_index": 1, "caption": "x", "hashtags": []}))
    with pytest.raises(ValueError, match="editor chose"):
        post_result(d, "compose")
    (d / "copy.json").write_text(json.dumps({"photo_index": 0, "caption": "x", "hashtags": []}))
    assert post_result(d, "compose")["caption"] == "x"
