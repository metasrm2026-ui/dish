"""Lessons from the Sep 29-30 editor reviews (37 of 61 posts marked Not okay).
Culture Rush: casefile used for celebrity news (small faces, dead space, unrelated polaroids).
Huddle Room: wide shots, a card with no photo, the wrong player pictured. Both: weak stories."""
import io
import json
import types

import pytest
from PIL import Image

from bore import routine as R
from bore.cards import CardSpec, plan_layout
from bore.guard import lint, verify

PB = {p: R.load_playbook(p) for p in ("culture-rush", "huddle")}


def _brief(page, text="Charles Kelley shared he has multiple myeloma, a rare blood cancer, in an Instagram video."):
    return {"page": page, "posts": [{"text": text}], "verification": {"route": "corroborated"},
            "photos": [{"size": "1600x1200", "file": "photos/0.jpg"}, {"size": "1600x1200", "file": "photos/1.jpg"}]}


def _copy(**kw):
    c = {"niche_fit": 9, "template": "headline", "headline_lines": ["CHARLES KELLEY", "HAS CANCER"],
         "photo_index": 0, "people_in_photo": ["Charles Kelley"],
         "caption": "CHARLES KELLEY REVEALS CANCER DIAGNOSIS 💛\n\nLady A's Charles Kelley shared he has a rare "
                    "blood cancer.\n\nSending him strength? 👇\n\n#CharlesKelley #LadyA #CultureRush #entertainmentnews #celebritynews"}
    c.update(kw)
    return c


def errors(**kw):
    return lint(_brief("culture-rush"), _copy(**kw), PB["culture-rush"])["errors"]


def test_clean_copy_passes():
    assert errors() == []


def test_casefile_is_only_for_true_crime():
    assert any("casefile template is only for true crime" in e for e in errors(template="casefile"))
    assert not any("casefile" in e for e in errors(template="casefile", story_type="true_crime"))


def test_every_card_needs_a_photo():
    assert any("no photo chosen" in e for e in errors(photo_index=-1))


def test_people_in_the_photo_must_be_in_the_story():
    assert any("'Kevin Cotter' is in the photo but not in the story" in e
               for e in errors(people_in_photo=["Charles Kelley", "Kevin Cotter"]))
    assert errors(people_in_photo=["Charles Kelley Jr."]) == []


def test_extra_polaroids_only_when_picked_for_casefile():
    assert any("only for the casefile" in e for e in errors(extra_photo_indexes=[1]))


def test_low_niche_fit_is_refused():
    assert any("niche_fit 7 is below 8" in e for e in errors(niche_fit=7))


def test_small_face_on_card_fails(tmp_path):
    img = tmp_path / "card.jpg"
    Image.new("RGB", (1080, 1350)).save(img)
    c = _copy()
    res = verify(str(img), _brief("culture-rush"), c, ocr=lambda p: [],
                 faces_override=[(400, 300, 60, 70, 0.9)], source_faces_override=[])
    assert any("face is too small" in e for e in res["errors"])
    ok = verify(str(img), _brief("culture-rush"), c, ocr=lambda p: [],
                faces_override=[(400, 300, 200, 260, 0.9)], source_faces_override=[])
    assert not any("too small" in e for e in ok["errors"])


def test_photo_text_on_a_helmet_is_not_card_text_on_a_face(tmp_path):
    img = tmp_path / "card.jpg"
    Image.new("RGB", (1080, 1350)).save(img)
    c = _copy(template="report", headline_lines=["GIANTS LAND", "MCCARTHY"], people_in_photo=["J.J. McCarthy"],
              caption="The Giants landed McCarthy.\n\nGood move? 👇\n\n#Giants #NFL #McCarthy #NYG")
    face = [(479, 654, 119, 165, 0.9)]
    visor = lambda p: [("GIANTS LAND", .99, (40, 40, 700, 140)), ("MCCARTHY", .99, (40, 150, 600, 250)),
                       ("OAKLEY", .95, (500, 690, 580, 710))]
    assert not any("overlaps a face" in e for e in verify(str(img), _brief("huddle"), c, visor, faces_override=face,
                                                             source_faces_override=face)["errors"])
    ours = lambda p: [("GIANTS LAND", .99, (480, 660, 700, 700)), ("MCCARTHY", .99, (40, 150, 600, 250))]
    assert any("overlaps a face" in e for e in verify(str(img), _brief("huddle"), c, ours, faces_override=face,
                                                         source_faces_override=face)["errors"])


def _photo(w, h):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (120, 90, 80)).save(b, "JPEG")
    return b.getvalue()


def test_tight_closeup_falls_back_to_spotlight_then_whole_photo():
    # the Haskins case: portrait photo, face a third of the frame
    spec = CardSpec(template="spotlight", headline_lines=["A"], photos=[_photo(1242, 1754)],
                    focus=(0.49, 0.31), face_size=0.34, face_box=(0.31, 0.14, 0.36, 0.34))
    assert plan_layout("culture-rush", spec) == [] and spec.fit == "contain"
    easy = CardSpec(template="spotlight", headline_lines=["A"], photos=[_photo(1600, 1200)],
                    focus=(0.5, 0.35), face_size=0.2, face_box=(0.44, 0.25, 0.12, 0.2))
    assert plan_layout("culture-rush", easy) == [] and easy.fit == "cover"


def test_renderer_switches_blocked_headline_to_spotlight(tmp_path, cfg, monkeypatch):
    from bore import cards
    d = tmp_path / "p"
    (d / "photos").mkdir(parents=True)
    (d / "photos/0.jpg").write_bytes(_photo(1242, 1754))
    (d / "brief.json").write_text(json.dumps({"id": "p", "page": "culture-rush", "photos": [{"file": "photos/0.jpg"}]}))
    (d / "copy.json").write_text(json.dumps(_copy()))
    calls = []

    def fake_render(page, spec, quality=92):
        calls.append(spec.template)
        if spec.template == "headline":
            raise cards.FaceBlocked("face")
        return _photo(1080, 1350)
    monkeypatch.setattr(cards, "render", fake_render)
    R.render_fallback(d, cfg)
    assert calls[:2] == ["headline", "spotlight"]
    assert json.loads((d / "copy.json").read_text())["template"] == "spotlight"


def test_playbooks_carry_the_learned_kinds():
    for slug in ("culture-rush", "huddle"):
        wp = PB[slug]["worth_posting"]
        assert "REJECTED KINDS" in wp and "APPROVED KINDS" in wp
        assert PB[slug]["card"]["template_rules"]
    assert "spotlight" in PB["culture-rush"]["card"]["templates"]
    assert "spotlight" in PB["culture-rush"]["card"]["art"]


def test_editor_feedback_groups_reasons(monkeypatch):
    from bore.dashboard import Dashboard
    dash = Dashboard("https://x", "s")
    drafts = [{"page": "huddle", "status": "rejected", "headline": "MCCARTHY GETS NO. 18",
               "status_info": {"reason": "Weak story", "note": "jersey numbers are trivia"}},
              {"page": "huddle", "status": "rejected", "headline": "LEWIS OUT", "status_info": {"reason": "Image quality"}},
              {"page": "huddle", "status": "posted", "headline": "HURTS WANTS BACK IN?", "status_info": {}},
              {"page": "huddle", "status": "failed", "headline": "LAMB OUT", "approved_at": 1791700000,
               "status_info": {"error": "SocialPilot account is locked"}},
              {"page": "huddle", "status": "failed", "headline": "NEVER APPROVED", "status_info": {}}]
    monkeypatch.setattr(dash.s, "get", lambda *a, **k: types.SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"drafts": drafts}))
    fb = dash.editor_feedback()["huddle"]
    assert fb["reasons"] == {"Weak story": 1, "Image quality": 1}
    assert fb["rejected"][0]["editor_reason"] == "Weak story" and fb["approved"][0]["headline"].startswith("HURTS")
    assert fb["rejected"][0]["editor_note"] == "jersey numbers are trivia" and "editor_note" not in fb["rejected"][1]
    # approved by an editor, then failed in SocialPilot: still an approval of the story
    assert [a["headline"] for a in fb["approved"]] == ["HURTS WANTS BACK IN?", "LAMB OUT"]


def test_check_refuses_under_quota_with_backups_left(tmp_path, cfg, store, monkeypatch):
    from bore import cli
    monkeypatch.setattr(cli, "_store", lambda c: store)
    run = tmp_path / "20260930-1212"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"quota": {"culture-rush": 2}}))
    for i in range(4):
        d = run / f"b{i}"
        d.mkdir()
        (d / "brief.json").write_text(json.dumps({"id": f"b{i}", "page": "culture-rush", "rank": i + 1,
                                                  "headline_hint": f"story {i}"}))
    (run / "b0" / "skip.json").write_text("{}")
    (run / "b1" / "uploaded").write_text("{}")
    args = types.SimpleNamespace(dir=str(run))
    with pytest.raises(SystemExit, match="under quota .*b2"):
        cli._routine_check(args, cfg, R)
    cli._routine_skip(types.SimpleNamespace(dir=str(run / "b2"), reason="red-carpet fluff"), cfg, R)
    with pytest.raises(SystemExit, match="b3"):
        cli._routine_check(args, cfg, R)
    (run / "b3" / "uploaded").write_text("{}")
    cli._routine_check(args, cfg, R)          # quota met
    assert [x["reason"] for x in store.decisions(0)] == ["red-carpet fluff"]


def test_skipped_backup_does_not_come_back(tmp_path, cfg, store, monkeypatch):
    from bore import cli
    monkeypatch.setattr(cli, "_store", lambda c: store)
    d = tmp_path / "run" / "b1"
    d.mkdir(parents=True)
    (d / "brief.json").write_text(json.dumps({"id": "b1", "page": "huddle", "key": "eagle-fire-sirianni",
                                              "signature": ["eagle", "fire", "sirianni"], "headline_hint": "Eagles fired"}))
    cli._routine_skip(types.SimpleNamespace(dir=str(d), reason="unverified rumour"), cfg, R)
    assert [r["key"] for r in store.sent_for_page("huddle", 0)] == ["eagle-fire-sirianni"]


def test_trade_only_industry_news_is_dropped_unless_big(cfg):
    import time as _t
    from bore.config import Page
    from bore.select import select_page
    now = _t.time()
    page = Page.from_dict({"slug": "culture-rush", "name": "Culture Rush"})

    def items(text, authors):
        return [{"id": f"{a}{hash(text)}", "platform": "x", "author": a, "text": text, "created_at": now - 900,
                 "engagement": 3000} for a in authors]
    deal = items("Mattel hires Roger Lynch as its new chief of film and television operations this week", ["variety", "deadline"])
    death = items("Veteran actor John Example has died at 81, his family confirmed on Tuesday evening", ["variety", "thr"])
    fan = items("Taylor Swift's new single reached 200 million streams on Spotify in record time", ["popcrave", "variety"])
    res = select_page(page, deal + death + fan, cfg, [], now, quota=3)
    heads = " | ".join(t.headline for t in res.shortlist)
    assert "Mattel" not in heads and "died" in heads and "Taylor Swift" in heads


@pytest.mark.parametrize("page,tpl,box", [
    ("culture-rush", "headline", (60, 300, 360, 700)),
    ("culture-rush", "spotlight", (390, 250, 690, 600)),
    ("huddle", "report", (390, 620, 690, 900)),
])
def test_photos_are_never_textured_or_tinted(page, tpl, box):
    """Editors saw grit/scratch lines across a face (Sep 30): the texture layers must stay under
    the photo, and the photo must not be graded. A flat grey photo must render flat."""
    import numpy as np
    from bore.cards import render
    flat = io.BytesIO()
    Image.new("RGB", (1600, 2000), (128, 128, 128)).save(flat, "JPEG", quality=98)
    spec = CardSpec(template=tpl, headline_lines=["TEST"], photos=[flat.getvalue()], distress=False)
    card = np.asarray(Image.open(io.BytesIO(render(page, spec))).convert("L")).astype(float)
    x0, y0, x1, y1 = box
    region = card[y0:y1, x0:x1]
    grain = region.std(axis=1).mean()          # across each row: textures, not the top/bottom shade
    assert grain < 2.0, f"texture over the photo (row std {grain:.1f})"
    assert abs(region.mean() - 128) < 12, f"photo tinted/darkened (mean {region.mean():.0f})"


def test_spotlight_subtitle_band_is_visible():
    """The yellow subtitle band rendered behind the backdrop (dark text on black, Quavo card)."""
    import numpy as np
    from bore.cards import render
    photo = io.BytesIO()
    Image.new("RGB", (1600, 1200), (90, 90, 90)).save(photo, "JPEG")
    spec = CardSpec(template="spotlight", headline_lines=["QUAVO", "IS A DAD"],
                    dek="The Migos rapper says he's welcomed his first child.", photos=[photo.getvalue()])
    card = np.asarray(Image.open(io.BytesIO(render("culture-rush", spec))).convert("RGB")).astype(int)
    band = card[1255:1300, 300:780]
    yellow = ((band[..., 0] > 200) & (band[..., 1] > 180) & (band[..., 2] < 90)).mean()
    assert yellow > 0.4, f"subtitle band not visible ({yellow:.0%} yellow)"
