"""Krea art mode: Krea 2 Turbo paints the backdrop (~15 credits); the renderer lays the real photo
and exact text on top. The artwork must never contain a person or text."""
import io
import json

import pytest
from PIL import Image, ImageDraw, ImageFont

from bore import routine as R
from bore.cards import CardSpec, build_html


def _backdrop() -> bytes:
    """A made-up textured backdrop like the ones Krea paints: a dark glow with grain, no person, no text."""
    import numpy as np
    rng = np.random.default_rng(7)
    y, x = np.mgrid[0:1350, 0:1080]
    glow = np.exp(-(((x - 700) / 520) ** 2 + ((y - 500) / 640) ** 2))
    base = np.stack([30 + 150 * glow, 20 + 110 * glow, 60 + 60 * glow], -1) + rng.normal(0, 9, (1350, 1080, 3))
    b = io.BytesIO()
    Image.fromarray(base.clip(0, 255).astype("uint8")).save(b, "PNG")
    return b.getvalue()


ART = _backdrop()


def _png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def test_a_backdrop_passes_the_art_check():
    assert R.check_art(ART) == []


def test_art_with_text_is_rejected():
    im = Image.open(io.BytesIO(ART)).convert("RGB")
    ImageDraw.Draw(im).text((120, 500), "BREAKING NEWS", fill="white",
                            font=ImageFont.truetype(str(__import__("bore.cards", fromlist=["FONTS"]).FONTS / "Anton-Regular.ttf"), 90))
    assert any("text" in p for p in R.check_art(_png(im)))


def test_art_replaces_backdrop_and_stays_out_of_the_photo_middle():
    spec = CardSpec(template="side_headline", headline_lines=["A"], art=ART)
    h = build_html("demo-page", spec)
    assert ".bg { background: linear-gradient" in h and "data:image/png;base64" in h
    rep = build_html("demo-sports", CardSpec(template="report", headline_lines=["A"], art=ART))
    assert 'class="artband"' in rep and "transparent 38%, transparent 74%" in rep


def test_art_prompt_uses_the_page_theme_colours_and_no_people(cfg, monkeypatch):
    """The playbook's art text names the page's colours ({c1}, {c2}): they come from its theme."""
    monkeypatch.setattr(R, "load_playbook", lambda slug: {"card": {"art": {"report": "Stadium lights in {c1} and {c2}"}}})
    brief = {"page": "demo-sports", "page_name": "Demo Sports", "photos": [],
             "studio": {"theme": {"accent": "#2266aa", "bg": "#101820"}}}
    p = R.art_prompt(brief, {"template": "report", "art_mood": "rainy night"})
    assert "#2266aa" in p and "#101820" in p and "rainy night" in p and "no people" in p and "no text" in p
    # premium cards; art mode stays the template layer's model for a switch back
    assert cfg["krea"]["mode"] == "premium" and cfg["krea"]["premium"]["model"] == "google/nano-banana-2.1"
    assert cfg["krea"]["model"] == "krea/krea-2/medium-turbo"
    # the product's own monthly cap (docs/PRODUCT.md Budgets): 150 cards, within the credits it names
    assert cfg["krea"]["monthly_max_images"] == 150
    assert cfg["krea"]["monthly_max_images"] * cfg["krea"]["credits_per_image"] <= cfg["krea"]["monthly_credits"]


def test_rejected_art_is_counted_and_not_used(tmp_path, cfg, store, monkeypatch):
    d = tmp_path / "p"
    (d / "photos").mkdir(parents=True)
    (d / "brief.json").write_text(json.dumps({"id": "p", "page": "demo-page", "photos": []}))
    (d / "copy.json").write_text(json.dumps({"template": "headline", "headline_lines": ["A"]}))
    monkeypatch.setattr(R, "check_art", lambda img: ["artwork contains a face/person"])
    with pytest.raises(R.ArtRejected):
        R._art_result(d, cfg, store, ART, "u", "")
    assert not (d / "card.jpg").exists()
    assert R.krea_daily_allowance(store, 1320)[0] == 1


def test_same_artwork_rerun_is_not_counted_again(tmp_path, cfg, store, monkeypatch):
    d = tmp_path / "p"
    (d / "photos").mkdir(parents=True)
    (d / "brief.json").write_text(json.dumps({"id": "p", "page": "demo-page", "photos": []}))
    (d / "copy.json").write_text(json.dumps({"template": "headline", "headline_lines": ["A"]}))
    monkeypatch.setattr(R, "render_fallback", lambda d, cfg, art=None, engine="": {"engine": engine})
    R._art_result(d, cfg, store, ART, "u1", "")
    R._art_result(d, cfg, store, ART, "u1", "")      # layout changed, same art
    assert R.krea_daily_allowance(store, 1320)[0] == 1


def test_headline_is_distressed_and_clean_twin_is_not():
    worn = build_html("demo-page", CardSpec(template="side_headline", headline_lines=["A"]))
    clean = build_html("demo-page", CardSpec(template="side_headline", headline_lines=["A"], distress=False))
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
        res = verify(str(d / "card.jpg"), {"page": "demo-page", "photos": []}, {"headline_lines": []},
                     faces_override=[], source_faces_override=[])
    finally:
        G._ocr = orig
    assert seen == [str(d / "card_clean.jpg")] and res["text_checked_on"] == "card_clean.jpg"
