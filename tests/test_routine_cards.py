import json

from bore.cards import CardSpec, build_html, team_colors
from bore.routine import krea_prompt, to_card_jpeg


def _brief(page, name):
    return {"page": page, "page_name": name, "photos": [], "playbook": {}}


def test_krea_prompt_has_exact_text_and_people_rules():
    copy = {"template": "report", "headline_lines": ["GIANTS HUNTING", "FOR A QB"], "label": "REPORT",
            "footer": "JAXSON DART INJURED", "highlight": "QB", "team": "giants", "people_in_photo": ["Jaxson Dart"]}
    p = krea_prompt(_brief("huddle", "Huddle"), copy)
    assert 'EXACTLY these 4 text elements' in p
    assert 'stamp/label: "REPORT"' in p and 'headline line 1: "GIANTS HUNTING"' in p and 'headline line 2: "FOR A QB"' in p
    assert "Do NOT add any other words, numbers" in p
    assert "Jaxson Dart" in p and "do not redraw" in p
    assert "Huddle Room" in p                       # page name comes from the playbook
    assert "#0B2265" in p                           # team colours resolved to hex
    assert "Full-bleed action photo" in p           # only the chosen template's layout
    assert "field-green" not in p


def test_no_person_means_no_invented_person():
    copy = {"template": "topic", "headline_lines": ["NFL IN BRAZIL"], "people_in_photo": []}
    assert "do NOT add, invent or draw any person" in krea_prompt(_brief("huddle", "Huddle"), copy)


def test_card_html_escapes_and_highlights():
    html = build_html("huddle", CardSpec(template="quote", headline_lines=["I don't think", "<b>F-BOMBS</b>"],
                                         highlight="F-BOMBS", label="Kevin Stefanski:", team="falcons"))
    assert "&lt;B&gt;" in html and '<span class="hl">F-BOMBS</span>' in html
    assert team_colors("Atlanta Falcons")[0] == "#A71930"


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
    spec = CardSpec(template="report", headline_lines=["GIANTS HUNTING"], photos=[_jpeg(1536, 864)],
                    focus=(0.46, 0.29), face_box=(0.40, 0.14, 0.12, 0.30))
    assert plan_layout("huddle", spec) == [] and spec.text_at == "bottom"


def test_face_that_cannot_be_cleared_blocks_the_render():
    from bore.cards import FaceBlocked, render
    import pytest
    spec = CardSpec(template="report", headline_lines=["X"], photos=[_jpeg(1080, 1350)],
                    focus=(0.5, 0.5), face_box=(0.1, 0.05, 0.8, 0.9))    # face fills the whole photo
    with pytest.raises(FaceBlocked):
        render("huddle", spec)
