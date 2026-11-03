"""Krea art mode: Krea 2 Turbo paints the backdrop (~15 credits); the renderer lays the real photo
and exact text on top. The artwork must never contain a person or text."""
import io
import json

import pytest
from PIL import Image, ImageDraw, ImageFont

from bore import routine as R
from bore.cards import CardSpec, build_html
from conftest import FIXTURES

ART = (FIXTURES / "krea_art_culture_rush.png").read_bytes()   # a real Krea 2 Turbo backdrop


def _png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def test_real_backdrop_passes_the_art_check():
    assert R.check_art(ART) == []


def test_art_with_text_is_rejected():
    im = Image.open(io.BytesIO(ART)).convert("RGB")
    ImageDraw.Draw(im).text((120, 500), "BREAKING NEWS", fill="white",
                            font=ImageFont.truetype(str(__import__("bore.cards", fromlist=["FONTS"]).FONTS / "Anton-Regular.ttf"), 90))
    assert any("text" in p for p in R.check_art(_png(im)))


def test_art_replaces_backdrop_and_stays_out_of_the_photo_middle():
    spec = CardSpec(template="headline", headline_lines=["A"], art=ART)
    h = build_html("culture-rush", spec)
    assert ".bg { background: linear-gradient" in h and "data:image/png;base64" in h
    rep = build_html("huddle", CardSpec(template="report", headline_lines=["A"], art=ART))
    assert 'class="artband"' in rep and "transparent 38%, transparent 74%" in rep


def test_art_prompt_uses_page_style_team_colours_and_no_people(cfg):
    brief = {"page": "huddle", "page_name": "Huddle Room", "photos": []}
    p = R.art_prompt(brief, {"template": "report", "team": "eagles", "art_mood": "rainy night"})
    assert "#004C54" in p and "rainy night" in p and "no people" in p and "no text" in p
    # premium cards (owner, Nov 1); art mode stays the template layer's model for a switch back
    assert cfg["krea"]["mode"] == "premium" and cfg["krea"]["premium"]["model"] == "google/nano-banana-2.1"
    assert cfg["krea"]["model"] == "krea/krea-2/medium-turbo"
    # the month's cards fit the plan's credits, and leave room for ~10 hourly posts a page a day
    assert cfg["krea"]["monthly_max_images"] * cfg["krea"]["credits_per_image"] <= cfg["krea"]["monthly_credits"]
    assert cfg["krea"]["monthly_max_images"] >= 10 * 2 * 30


def test_rejected_art_is_counted_and_not_used(tmp_path, cfg, store, monkeypatch):
    d = tmp_path / "p"
    (d / "photos").mkdir(parents=True)
    (d / "brief.json").write_text(json.dumps({"id": "p", "page": "culture-rush", "photos": []}))
    (d / "copy.json").write_text(json.dumps({"template": "headline", "headline_lines": ["A"]}))
    monkeypatch.setattr(R, "check_art", lambda img: ["artwork contains a face/person"])
    with pytest.raises(R.ArtRejected):
        R._art_result(d, cfg, store, ART, "u", "")
    assert not (d / "card.jpg").exists()
    assert R.krea_daily_allowance(store, 1320)[0] == 1


def test_same_artwork_rerun_is_not_counted_again(tmp_path, cfg, store, monkeypatch):
    d = tmp_path / "p"
    (d / "photos").mkdir(parents=True)
    (d / "brief.json").write_text(json.dumps({"id": "p", "page": "culture-rush", "photos": []}))
    (d / "copy.json").write_text(json.dumps({"template": "headline", "headline_lines": ["A"]}))
    monkeypatch.setattr(R, "render_fallback", lambda d, cfg, art=None, engine="": {"engine": engine})
    R._art_result(d, cfg, store, ART, "u1", "")
    R._art_result(d, cfg, store, ART, "u1", "")      # layout changed, same art
    assert R.krea_daily_allowance(store, 1320)[0] == 1


def test_headline_is_distressed_and_clean_twin_is_not():
    worn = build_html("culture-rush", CardSpec(template="headline", headline_lines=["A"]))
    clean = build_html("culture-rush", CardSpec(template="headline", headline_lines=["A"], distress=False))
    from bore.cards import DISTRESS_CSS
    assert DISTRESS_CSS in worn and DISTRESS_CSS not in clean


def test_verify_reads_text_on_the_clean_twin(tmp_path):
    from bore.guard import verify
    d = tmp_path
    Image.new("RGB", (1080, 1350)).save(d / "card.jpg")
    Image.new("RGB", (1080, 1350)).save(d / "card_clean.jpg")
    seen = []

    def fake_ocr(path):
        seen.append(path)
        return []
    import bore.guard as G
    orig, G._ocr = G._ocr, fake_ocr
    try:
        res = verify(str(d / "card.jpg"), {"page": "culture-rush", "photos": []}, {"headline_lines": []},
                     faces_override=[], source_faces_override=[])
    finally:
        G._ocr = orig
    assert seen == [str(d / "card_clean.jpg")] and res["text_checked_on"] == "card_clean.jpg"
