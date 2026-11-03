import json

import pytest

from bore import routine as R
from bore.cards import THEMED, CardSpec, build_html
from bore.routine import krea_prompt, to_card_jpeg

THEME = {"accent": "#2266aa", "bg": "#101820", "ink": "#ffffff", "logo_text": "Demo Sports", "font": "Oswald",
         "logo_font": "Marker"}


def _brief(page, name, theme=None):
    return {"page": page, "page_name": name, "photos": [], "playbook": {}, **({"studio": {"theme": theme}} if theme else {})}


PLAYBOOK = {"name": "Demo Sports", "card": {"style": {"report": "Full-bleed action photo, stamp, huge headline",
                                                      "topic": "Striped backdrop, question boxes"}}}


def test_krea_prompt_has_exact_text_and_people_rules(monkeypatch):
    monkeypatch.setattr(R, "load_playbook", lambda slug: PLAYBOOK)
    copy = {"template": "report", "headline_lines": ["DEMO FC HUNTING", "FOR A KEEPER"], "label": "REPORT",
            "footer": "SAM EXAMPLE INJURED", "highlight": "KEEPER", "people_in_photo": ["Sam Example"]}
    p = krea_prompt(_brief("demo-sports", "Demo Sports", THEME), copy)
    assert 'EXACTLY these 5 text elements' in p      # the four lines and the page's logo text
    assert 'stamp/label: "REPORT"' in p and 'headline line 1: "DEMO FC HUNTING"' in p and 'headline line 2: "FOR A KEEPER"' in p
    assert 'page logo: "DEMO SPORTS"' in p and 'The page logo reads "DEMO SPORTS"' in p
    assert "Do NOT add any other words, numbers" in p
    assert "Sam Example" in p and "do not redraw" in p
    assert "Demo Sports" in p                        # page name comes from the playbook
    assert "#2266aa" in p and "#101820" in p         # the page's theme colours
    assert "Full-bleed action photo" in p            # only the chosen template's layout
    assert "Striped backdrop" not in p


def test_a_page_without_logo_text_gets_no_logo(monkeypatch):
    monkeypatch.setattr(R, "load_playbook", lambda slug: PLAYBOOK)
    p = krea_prompt(_brief("demo-sports", "Demo Sports", {"accent": "#2266aa"}), {"template": "report", "headline_lines": ["A"]})
    assert "page logo" not in p and "other than the page's own logo" not in p


def test_no_person_means_no_invented_person():
    copy = {"template": "topic", "headline_lines": ["DEMO LEAGUE ABROAD"], "people_in_photo": []}
    assert "do NOT add, invent or draw any person" in krea_prompt(_brief("demo-sports", "Demo Sports"), copy)


def test_card_html_escapes_and_highlights():
    html = build_html("demo-sports", CardSpec(template="speaker", headline_lines=["I don't think", "<b>NO WAY</b>"],
                                              highlight="NO WAY", label="Sam Example:", theme=THEME))
    assert "&lt;B&gt;" in html and '<span class="hl">NO WAY</span>' in html


@pytest.mark.parametrize("template", sorted(THEMED))
def test_every_themed_layout_is_drawn_in_the_pages_theme(template):
    """No layout belongs to one page: its colours, logo text and fonts are the page's theme."""
    html = build_html("demo-sports", CardSpec(template=template, headline_lines=["ONE", "TWO"], label="TAG", dek="A dek",
                                              stat_label="S", stat_value="9", footer="F", theme=THEME))
    assert "#2266aa" in html and "#101820" in html
    assert "Oswald" in html.split("</style>")[0]
    if template in ("side_headline", "spotlight", "casefile", "speaker"):      # the logo text
        assert "DEMO" in html and "SPORTS" in html
    if template in ("side_headline", "spotlight", "casefile"):                 # in the logo font
        assert "font-family: Marker" in html
    other = build_html("demo-page", CardSpec(template=template, headline_lines=["ONE"],
                                             theme={"accent": "#cc3300", "bg": "#fafafa", "ink": "#111111"}))
    assert "#cc3300" in other and "#2266aa" not in other and "DEMO" not in other


def test_an_unknown_font_or_colour_keeps_the_default():
    from bore.layouts import DEFAULT_THEME, theme_of
    t = theme_of({"font": "Comic Sans", "accent": "red", "logo_font": "marker"})
    assert t["font"] == DEFAULT_THEME["font"] and t["accent"] == DEFAULT_THEME["accent"] and t["logo_font"] == "Marker"


def test_to_card_jpeg_normalises_size():
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (928, 1152), "red").save(buf, "JPEG")
    out = Image.open(io.BytesIO(to_card_jpeg(buf.getvalue())))
    assert out.size == (1080, 1350)


def _jpeg(w, h):
    from PIL import Image
    import io
    b = io.BytesIO()
    Image.new("RGB", (w, h), "gray").save(b, "JPEG")
    return b.getvalue()


def test_report_moves_headline_below_a_high_face():
    from bore.cards import plan_layout
    spec = CardSpec(template="report", headline_lines=["DEMO FC HUNTING"], photos=[_jpeg(1536, 864)],
                    focus=(0.46, 0.29), face_box=(0.40, 0.14, 0.12, 0.30))
    assert plan_layout("demo-sports", spec) == [] and spec.text_at == "bottom"


def test_face_that_cannot_be_cleared_blocks_the_render():
    from bore.cards import FaceBlocked, render
    import pytest
    spec = CardSpec(template="report", headline_lines=["X"], photos=[_jpeg(1080, 1350)],
                    focus=(0.5, 0.5), face_box=(0.1, 0.05, 0.8, 0.9))    # face fills the whole photo
    with pytest.raises(FaceBlocked):
        render("demo-sports", spec)
