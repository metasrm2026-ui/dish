"""Template specs (bore/tspec.py, docs/TEMPLATE_SPEC.md): a post template as data that the card
renderer draws, lint checks and verify reads back, so a page's new template needs no code (Oct 23:
editors' new templates were only ever text for the ideas step; the card fell back to the nearest
coded layout). Every spec here is made up (tests/data/tspec/)."""
import io
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from bore import tspec
from bore.cards import FaceBlocked, TextOverflow, render
from bore.guard import brand_words, card_strings, lint, verify

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "data" / "tspec"
SPECS = sorted(FIX.glob("*.json"))
THEME = {"accent": "#ffcc00", "bg": "#101a2c", "ink": "#ffffff", "logo_text": "Demo Page"}
FACE = (0.42, 0.12, 0.16, 0.14)          # high in the frame: where a headline over the photo would sit


def _photo(w=1200, h=1500, face=FACE, text=None):
    """A gradient 'photo' with the face painted magenta (so the card shows whether anything covers it)."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=(40 + y * 80 // h, 90, 140 - y * 60 // h))
    if face:
        x, y, fw, fh = face
        d.rectangle([x * w, y * h, (x + fw) * w, (y + fh) * h], fill=(255, 0, 255))
    if text:
        words, (tx, ty) = text
        d.text((tx * w, ty * h), words, fill=(255, 255, 255), font=ImageFont.truetype(str(ROOT / "assets/fonts/Inter.ttf"), 44))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=92)
    return b.getvalue()


def _magenta_share(img: bytes, rect) -> float:
    from PIL import Image
    im = Image.open(io.BytesIO(img)).convert("RGB")
    x0, y0, x1, y1 = (int(rect[0]) + 4, int(rect[1]) + 4, int(rect[2]) - 4, int(rect[3]) - 4)
    px = [im.getpixel((x, y)) for x in range(x0, x1, 6) for y in range(y0, y1, 6)]
    return sum(1 for r, g, b in px if r > 100 and b > 100 and g < 90 and abs(r - b) < 60) / max(1, len(px))


def _head(rect):
    """verify's head zone around a face given as card pixels (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    return (x0 - 0.18 * w, y0 - 0.45 * h, x1 + 0.18 * w, y1 + 0.08 * h)


def _hits(a, b) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _card(spec, slots=None, photos=None, faces=None, keep=None, **kw):
    copy = {"template_spec": spec["id"], "slots": tspec.example_slots(spec) if slots is None else slots, **kw}
    photos = photos or [_photo()]
    return tspec.card_spec(spec, copy, photos, theme=THEME, faces=faces if faces is not None else [[FACE]],
                           keep=keep if keep is not None else [[]])


def _mini(**over):
    """A one-photo, one-text spec for the planner's own tests."""
    raw = {"id": "mini", "name": "Mini", "use_when": "tests",
           "photos": [{"box": [0, 0, 1, 1]}],
           "text": [{"slot": "head", "box": [0.05, 0.3, 0.9, 0.2], "size": [30, 80]}],
           "slots": {"head": {"type": "text", "max_words": 6}}}
    for k, v in over.items():
        raw[k] = v
    return tspec.load(raw)


# ----------------------------------------------------------------------------------- the schema
def test_the_example_specs_load_and_loading_again_changes_nothing():
    specs = [tspec.load(p) for p in SPECS]
    assert {s["id"] for s in specs} == {"mega-figure", "tally-tower", "said-it"}
    for s in specs:
        assert tspec.load(s) == s                                   # a normalised spec is a valid spec
        assert s["canvas"] == {"w": 1080, "h": 1350} and s["photos"][0]["photo"] == 0
    stats = next(s for s in specs if s["id"] == "tally-tower")["slots"]["stats"]
    assert stats["type"] == "list" and (stats["min_items"], stats["max_items"]) == (2, 4)


def test_a_broken_spec_names_every_problem():
    raw = {"id": "Bad Id", "name": "", "use_when": "x", "visual_lane": "neon", "canvas": {"w": 100},
           "photos": [{"box": [0, 0, 1.2, 1], "photo": 1}],
           "text": [{"slot": "headline", "box": [0, 0, 1, 0.2], "rotation": 12, "colour": "accent"},
                    {"slot": "ghost", "box": [0, 0.5, 1, 0.2], "color": "purple"},
                    {"slot": "headline", "text": "VS", "box": [0, 0.8, 1, 0.1]}],
           "slots": {"headline": {"type": "text"}, "unused": {"type": "text"}, "stats": {"type": "list", "max_items": 12}}}
    errs = "\n".join(tspec.validate(raw))
    for want in ("id must be", "name is required", "visual_lane 'neon'", "canvas: w and h", "photos[0]: box",
                 "one photo zone must show photo 0", "rotation must be 0", "unknown key 'colour'",
                 "slot 'ghost' is not defined", "colour 'purple'", "either slot (text from the copy) or text",
                 "slots.unused: no zone draws", "a list holds 1-8 items", "slots.stats: no zone draws"):
        assert want in errs, want
    with pytest.raises(tspec.SpecError, match="rotation must be 0"):
        tspec.load(raw)
    assert any("limits must be text" in e for e in tspec.validate({**raw, "limits": ["a cut-out"]}))
    with pytest.raises(tspec.SpecError, match="not valid JSON"):
        tspec.load('{"id": ')


def test_a_slot_is_drawn_by_one_zone_of_its_kind():
    base = json.loads((FIX / "tally-tower.json").read_text())
    twice = {**base, "text": base["text"] + [{"slot": "verdict", "box": [0, 0.5, 1, 0.1]}]}
    assert any("already drawn by" in e for e in tspec.validate(twice))
    wrong = {**base, "text": base["text"] + [{"slot": "stats", "box": [0, 0.5, 1, 0.1]}], "lists": []}
    assert any("'stats' is a list slot" in e for e in tspec.validate(wrong))


def test_specs_are_found_in_the_brief_then_the_pages_store(tmp_path, monkeypatch):
    raw = json.loads((FIX / "said-it.json").read_text())
    assert tspec.find("said-it", {"template_specs": {"said-it": raw}})["name"] == "Said It"
    assert tspec.find("said-it", {"template_specs": [raw]})["id"] == "said-it"
    monkeypatch.setattr(tspec, "SPEC_DIR", tmp_path)
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "said-it.json").write_text(json.dumps(raw))
    (tmp_path / "demo" / "broken.json").write_text("{}")
    assert tspec.find("said-it", {"page": "demo"})["id"] == "said-it"
    assert list(tspec.page_specs("demo")) == ["said-it"]          # one that does not validate is left out
    with pytest.raises(tspec.SpecError, match=r"not one of this page's template specs \(said-it\)"):
        tspec.find("nope", {"page": "demo"})
    with pytest.raises(tspec.SpecError, match="id must be"):       # named, so its problems are shown
        tspec.find("broken", {"page": "demo"})


def test_the_doc_describes_every_key_the_validator_accepts():
    doc = (ROOT / "docs" / "TEMPLATE_SPEC.md").read_text()
    for kind, keys in tspec.KEYS.items():
        for k in keys:
            assert f"`{k}`" in doc, f"docs/TEMPLATE_SPEC.md does not describe {kind} key '{k}'"
    for opt in (*tspec.LANES, *tspec.TEXTURES, *tspec.FONTS, *tspec.ROLES):
        assert f"`{opt}`" in doc, opt
    example = json.loads(re.search(r"```json\n(.*?)```", doc, re.S).group(1))
    assert tspec.lint_slots(tspec.load(FIX / f"{example['template_spec']}.json"), example) == []


# ------------------------------------------------------------------------------ the face-safe plan
def test_a_face_under_a_text_zone_moves_the_photo_first():
    spec = _mini()
    face = (0.4, 0.3, 0.2, 0.1)             # a tall photo: the crop has room to move
    card = _card(spec, {"head": "A HEADLINE"}, photos=[_photo(1000, 2000, face)], faces=[[face]])
    plan = tspec.plan(card)
    assert plan.problems == [] and plan.photos[0].mode == "cover"
    assert plan.text_boxes[0] == tspec.px(spec["text"][0]["box"], spec["canvas"])
    on_card = tspec.face_on_card(card, plan, 0, face)
    assert not any(_hits(_head(on_card), r) for _, r in tspec.covering_boxes(card, plan))
    assert on_card[1] >= 0 and on_card[3] <= 1350                    # the face itself stays on the card


def test_then_the_text_takes_its_alt_box_then_the_whole_photo_shows():
    spec = tspec.load(FIX / "mega-figure.json")
    card = _card(spec)
    plan = tspec.plan(card)
    assert plan.problems == [] and plan.text_boxes[0] == tspec.px(spec["text"][0]["alt_box"], spec["canvas"])

    big = (0.1, 0.05, 0.8, 0.85)            # a face that fills the frame: no crop clears the text below it
    mini = _mini(text=[{"slot": "head", "box": [0.05, 0.9, 0.9, 0.08]}])
    card = _card(mini, {"head": "A HEADLINE"}, photos=[_photo(1080, 1080, big)], faces=[[big]])
    plan = tspec.plan(card)
    assert plan.problems == [] and plan.photos[0].mode == "contain"
    assert not any(_hits(_head(tspec.face_on_card(card, plan, 0, big)), r) for _, r in tspec.covering_boxes(card, plan))

    strict = _mini(photos=[{"box": [0, 0, 1, 1], "contain_fallback": False}],
                   text=[{"slot": "head", "box": [0.05, 0.4, 0.9, 0.12]}])
    card = _card(strict, {"head": "A HEADLINE"}, photos=[_photo(1080, 1080, big)], faces=[[big]])
    with pytest.raises(FaceBlocked, match="face cannot be kept clear of the text in the template you chose"):
        render("demo", card)             # refused before any browser starts


def test_every_face_in_the_photo_is_protected_and_shades_count():
    spec = _mini(photos=[{"box": [0, 0, 1, 1], "shade": [{"edge": "bottom", "size": 0.5}]}],
                 text=[{"slot": "head", "box": [0.05, 0.8, 0.9, 0.15]}])
    second = (0.7, 0.55, 0.1, 0.08)         # a second, smaller face low in the frame
    card = _card(spec, {"head": "A HEADLINE"}, photos=[_photo(1000, 1600)], faces=[[FACE, second]])
    plan = tspec.plan(card)
    assert plan.problems == [] and plan.photos[0].top < 0           # the photo moved up for the second face
    for f in (FACE, second):
        head = _clip_to(_head(tspec.face_on_card(card, plan, 0, f)), (0, 0, 1080, 1350))
        assert not any(_hits(head, r) for _, r in tspec.covering_boxes(card, plan))
        assert not _hits(head, (0, 1350 * 0.75, 1080, 1350))         # the darker half of the bottom shade
    lone = _card(spec, {"head": "A HEADLINE"}, photos=[_photo(1000, 1600)], faces=[[FACE]])
    assert tspec.plan(lone).photos[0].top == 0                        # without it, the photo sits at its focus


def _clip_to(a, b):
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return r if r[2] > r[0] and r[3] > r[1] else None


def test_a_watermark_is_never_cropped_or_covered():
    spec = _mini(text=[{"slot": "head", "box": [0.05, 0.05, 0.9, 0.15]}])
    mark = (0.7, 0.9, 0.25, 0.04)           # a credit in the bottom-right corner of a wide photo
    card = _card(spec, {"head": "A HEADLINE"}, photos=[_photo(2400, 1200, None)], faces=[[]], keep=[[mark]])
    plan = tspec.plan(card)
    assert plan.problems == []
    r = tspec.face_on_card(card, plan, 0, mark)
    assert r[0] >= -1 and r[2] <= 1081 and r[1] >= -1 and r[3] <= 1351
    assert not any(_hits(r, c) for _, c in tspec.covering_boxes(card, plan))
    # and the renderer finds one by itself: a handle printed on the photo
    found = tspec.watermark_boxes(_photo(1200, 800, None, text=("@demo.photos", (0.6, 0.88))))
    assert any(abs(x - 0.6) < 0.05 and abs(y - 0.88) < 0.05 for x, y, _, _ in found), found


# --------------------------------------------------------------------------------- the renderer
@pytest.mark.parametrize("path", SPECS, ids=lambda p: p.stem)
def test_each_example_renders_with_its_text_readable_and_the_face_clear(path, tmp_path):
    spec = tspec.load(path)
    card = _card(spec)
    plan = tspec.plan(card)
    img = render("demo", replace(card, distress=False))       # verify reads the clean twin
    from PIL import Image
    assert Image.open(io.BytesIO(img)).size == (spec["canvas"]["w"], spec["canvas"]["h"])
    face = tspec.face_on_card(card, plan, 0, FACE)
    assert _magenta_share(img, face) > 0.95                  # nothing is drawn over the face
    (tmp_path / "card.jpg").write_bytes(img)
    brief = {"page": "demo", "photos": [], "studio": {"theme": THEME},
             "template_specs": {spec["id"]: json.loads(path.read_text())}}
    copy = {"template_spec": spec["id"], "slots": card.slots, "people_in_photo": ["Demo Person"]}
    res = verify(str(tmp_path / "card.jpg"), brief, copy,
                 faces_override=[(face[0], face[1], face[2] - face[0], face[3] - face[1])], source_faces_override=[])
    assert res["pass"], (res["errors"], res["ocr"])


def _fitted(card) -> dict:
    """Each zone's fitted font sizes, read from the page the renderer draws."""
    from playwright.sync_api import sync_playwright

    from bore.cards import _chrome_path
    with sync_playwright() as pw:
        b = pw.chromium.launch(**({"executable_path": _chrome_path()} if _chrome_path() else {}))
        pg = b.new_page(viewport={"width": 1080, "height": 1350})
        pg.set_content(tspec.build_html(card))
        pg.wait_for_function("document.body.dataset.ready === '1'", timeout=20000)
        out = pg.evaluate("""Object.fromEntries([...document.querySelectorAll('.tz')].map(z => [z.dataset.name,
            [...z.querySelectorAll('.ln')].map(l => parseFloat(getComputedStyle(l).fontSize))]))""")
        b.close()
    return out


def test_text_is_auto_fitted_to_its_zone_and_too_much_text_is_refused():
    spec = tspec.load(FIX / "said-it.json")
    short = _fitted(_card(spec, {"quote": ["SO", "GOOD"], "speaker": "ONE COACH"}))
    long = _fitted(_card(spec, {"quote": "WE KNEW IT WOULD BE HARD BUT NOBODY IN THAT ROOM WAS READY FOR THAT",
                                "speaker": "ONE COACH"}))
    lo, hi = spec["text"][1]["size"]
    assert short["quote"] == [hi, hi]                                  # short lines take the largest size
    assert lo <= long["quote"][0] < hi                                 # a long quote shrinks to fit 4 lines
    stacked = _fitted(_card(tspec.load(FIX / "mega-figure.json"), {"headline": ["HUGE", "A LONGER SECOND LINE"], "figure": "61"},
                            faces=[[]]))
    assert stacked["headline"][0] > stacked["headline"][1]              # each_line: a short line is set larger
    tight = _mini(text=[{"slot": "head", "box": [0.05, 0.85, 0.3, 0.05], "size": [60, 80], "max_lines": 1}])
    with pytest.raises(TextOverflow, match="head"):
        render("demo", _card(tight, {"head": "FAR TOO MANY WORDS HERE"}, faces=[[]]))


def test_theme_colours_duotone_and_art_layer():
    assert tspec.color("auto", {**tspec._theme(_card(_mini(), {"head": "X"})), "accent": "#ffcc00"}, on="accent") == "#111111"
    th = tspec._theme(_card(_mini(), {"head": "X"}))
    assert tspec.color("bg/50", th).startswith("rgba(") and tspec.color("#123456", th) == "#123456"
    spec = _mini(photos=[{"box": [0, 0, 1, 1], "duotone": ["bg", "accent"]}], art={"box": [0, 0, 1, 0.5], "blend": "screen"})
    card = _card(spec, {"head": "X"}, faces=[[]])
    html = tspec.build_html(card)
    assert '<filter id="duo0"' in html and "filter:url(#duo0)" in html and 'class="art"' not in html
    html = tspec.build_html(replace(card, art=_photo(800, 400, None)))
    assert 'class="art"' in html and "mix-blend-mode:screen" in html
    whole = _mini(photos=[{"box": [0, 0, 1, 1], "fit": "contain", "duotone": ["bg", "accent"]}])
    html = tspec.build_html(_card(whole, {"head": "X"}, faces=[[]]))
    assert "filter:url(#duo0) blur(36px)" in html                     # the blurred fill keeps its blur


# ----------------------------------------------------------------------------------- lint, verify
def _brief(spec_name="tally-tower", **kw):
    return {"page": "demo", "page_name": "Demo", "verification": {"route": "studio"},
            "posts": [{"text": "Demo Player scored 31 points with 12 rebounds and 9 assists in 34 minutes, a record."}],
            "photos": [{"index": 0, "file": "photos/0.jpg", "size": "1200x1500", "source": "upload"},
                       {"index": 1, "file": "photos/1.jpg", "size": "1200x1500"}],
            "template_specs": {spec_name: json.loads((FIX / f"{spec_name}.json").read_text())}, **kw}


def _copy(**kw):
    return {"template_spec": "tally-tower", "headline_lines": ["DEMO PLAYER'S BIG NIGHT"], "photo_index": 0,
            "people_in_photo": ["Demo Player"], "caption": "Demo Player was everywhere. Who stops him?",
            "hashtags": ["#Demo"],
            "slots": {"verdict": "UNSTOPPABLE", "stats": [{"value": "31", "label": "POINTS"}, {"value": "12", "label": "REBOUNDS"},
                                                           {"value": "9", "label": "ASSISTS"}],
                      "footnote": "In 34 minutes"}, **kw}


def test_lint_passes_a_spec_copy_and_checks_its_slot_rules():
    assert lint(_brief(), _copy(), {})["pass"], lint(_brief(), _copy(), {})["errors"]
    assert card_strings(_copy()) == ["UNSTOPPABLE", "31", "POINTS", "12", "REBOUNDS", "9", "ASSISTS", "In 34 minutes"]
    cases = {
        "slot 'verdict' is required": {"verdict": ""},
        "slot 'verdict' has 4 words (max 3)": {"verdict": "THE BEST ONE EVER"},
        "has 5 items; the template takes 2-4": {"stats": [{"value": "31", "label": "POINTS"}] * 5},
        "item 1 label too long": {"stats": [{"value": "31", "label": "POINTS IN THE SECOND HALF"}, {"value": "12", "label": "R"}]},
        "slot 'colour' is not in the template": {"colour": "red"},
        "'40' does not appear in the sources": {"stats": [{"value": "40", "label": "POINTS"}, {"value": "12", "label": "R"}]},
    }
    for want, slots in cases.items():
        errs = lint(_brief(), _copy(slots={**_copy()["slots"], **slots}), {})["errors"]
        assert any(want in e for e in errs), (want, errs)


def test_lint_keeps_the_rest_of_a_spec_copy_honest():
    def errs(brief=None, **kw):
        return " | ".join(lint(brief or _brief(), _copy(**kw), {})["errors"])
    assert "a template spec draws only its slots" in errs(dek="ALSO THIS")
    assert "the post's title in the dashboard" in errs(headline_lines=[])
    assert "not one of this page's template specs (tally-tower)" in errs(template_spec="giant")
    assert "extra_photo_indexes has 1" in errs(extra_photo_indexes=[1])
    assert "template" not in errs()                                       # no coded template needed
    hedged = _brief(posts=[{"text": "Demo Player reportedly could be traded after scoring 31 points."}])
    assert "states ['TRADED'] as fact" in errs(hedged, slots={"verdict": "TRADED", "stats": _copy()["slots"]["stats"][:2]})
    mega = _brief("mega-figure")
    bad = lint(mega, _copy(template_spec="mega-figure", slots={"headline": ["MOST POINTS"], "figure": "THIRTY"}), {})["errors"]
    assert any("must contain digits" in e for e in bad)


def test_verify_reads_the_slots_and_knows_the_layouts_own_words():
    brief = _brief("said-it", studio={"theme": THEME})
    copy = {"template_spec": "said-it", "slots": {"quote": ["WE WERE NOT", "READY FOR THAT"], "speaker": "THE COACH"}}
    assert {"ON", "THE", "RECORD", "DEMO", "PAGE"} <= brand_words(brief, copy)
    box = (0, 0, 10, 10)
    read = [("ON THE RECORD", 0.99, box), ("WE WERE NOT", 0.99, box), ("READY FOR THAT", 0.99, box),
            ("THE COACH", 0.98, box), ("DEMO PAGE", 0.97, box)]
    res = verify("card.jpg", brief, copy, ocr=lambda p: read)
    assert res["pass"] and not res["warnings"], res
    res = verify("card.jpg", brief, copy, ocr=lambda p: read[:2] + read[3:])
    assert any("READY FOR THAT" in e for e in res["errors"])


def test_an_hourly_spec_cards_logo_is_the_pages_own_text(monkeypatch):
    """The hourly brief has no studio theme, but its spec card's logo zone draws the page's logo text
    (studio sync's theme): verify called it invented text on every such card (Oct 23 audit)."""
    from bore import studio
    monkeypatch.setattr(studio, "studio_meta", lambda: {"demo": {"theme": THEME}})
    brief = {k: v for k, v in _brief("said-it").items() if k != "studio"}
    copy = {"template_spec": "said-it", "slots": {"quote": ["WE WERE NOT", "READY FOR THAT"], "speaker": "THE COACH"}}
    assert {"DEMO", "PAGE"} <= brand_words(brief, copy)
    box = (0, 0, 10, 10)
    read = [("ON THE RECORD", 0.99, box), ("WE WERE NOT", 0.99, box), ("READY FOR THAT", 0.99, box),
            ("THE COACH", 0.98, box), ("DEMOPAGE", 0.97, box)]
    res = verify("card.jpg", brief, copy, ocr=lambda p: read)
    assert res["pass"] and not res["warnings"], res
    # every layout is drawn in the page's theme, its logo text too (bore/cards.py, bore/layouts.py)
    assert {"DEMO", "PAGE"} <= brand_words(brief, {"template": "headline"})
    monkeypatch.setattr(studio, "studio_meta", lambda: {})
    assert not brand_words(brief, {"template": "headline"}), "a page with no theme draws no logo"


def test_a_logo_of_several_words_reads_as_the_pages_own_on_any_card():
    """OCR reads a two-word logo as one line; matched a word at a time it was flagged as invented text
    (a warning on every generic Studio card of a page whose logo text has a space)."""
    from bore.guard import brand_phrases
    brief = {"page": "demo", "photos": [], "studio": {"theme": {**THEME, "logo_text": "Demo Page Now"}}}
    copy = {"template": "headline", "headline_lines": ["BIG NEWS TODAY"]}
    assert brand_phrases(brief, copy) == {"DEMO PAGE NOW"}
    read = [("BIG NEWS TODAY", 0.99, (0, 0, 9, 9)), ("DEMO PAGE NOW", 0.97, (0, 0, 9, 9))]
    assert verify("card.jpg", brief, copy, ocr=lambda p: read)["warnings"] == []


def test_a_coded_template_named_next_to_a_spec_does_not_apply_its_rules():
    errs = lint(_brief(), _copy(template="comparison"), {"card": {"templates": ["report"]}})["errors"]
    assert errs == [], errs


# ------------------------------------------------------------------------------------------- CLI
def test_the_preview_command_draws_a_spec(tmp_path, monkeypatch, capsys):
    from PIL import Image

    from bore import cli
    monkeypatch.setenv("BORE_ROUTINE", "manual")
    photo = tmp_path / "p.jpg"
    photo.write_bytes(_photo())
    out = tmp_path / "card.png"
    cli.main(["routine", "tspec-render", str(FIX / "tally-tower.json"), str(photo), "--out", str(out)])
    assert Image.open(out).size == (1080, 1350) and Image.open(out).format == "PNG"
    assert "tspec-render:" in capsys.readouterr().out
    copy = tmp_path / "copy.json"
    copy.write_text(json.dumps({"template_spec": "tally-tower", "slots": {"verdict": "WAY TOO MANY WORDS", "stats": []}}))
    cli.main(["routine", "tspec-render", str(FIX / "tally-tower.json"), str(photo), "--copy", str(copy), "--out", str(out)])
    said = capsys.readouterr().out
    assert "slot rule: slot 'verdict' has 4 words" in said and "lint would refuse this copy" in said
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"id": "x"}))
    with pytest.raises(SystemExit, match="REFUSED: the template spec is not valid"):
        cli.main(["routine", "tspec-render", str(bad), str(photo), "--out", str(out)])


# ------------------------------------------------------------- everything drawn keeps faces clear
def _green_rows(img: bytes, x0: int, x1: int) -> list[int]:
    """Card rows where a pure-green plate shows between x0 and x1."""
    from PIL import Image
    im = Image.open(io.BytesIO(img)).convert("RGB")
    return [y for y in range(im.height)
            if any((lambda p: p[1] > 200 and p[0] < 60 and p[2] < 60)(im.getpixel((x, y))) for x in range(x0, x1, 3))]


@pytest.mark.parametrize("pad", [14, 40, 80])
def test_a_plate_behind_each_line_stays_inside_its_zone_under_a_face(pad):
    """A 'lines' plate painted its padding and the font's full height above its zone, over the chin
    of a face just above it, while the plan and verify both passed (Oct 23 audit)."""
    photos, faces = tspec.sample_photos(1)               # the face's head zone ends at card y ~589
    spec = _mini(text=[{"slot": "head", "box": [0.05, 600 / 1350, 0.9, 0.25], "size": [40, 90], "max_lines": 2,
                        "plate": {"color": "#00ff00", "style": "lines", "pad": [pad, 14]}}])
    card = _card(spec, {"head": ["A BIG", "HEADLINE"]}, photos=photos, faces=faces)
    plan = tspec.plan(card)
    assert plan.problems == []
    head = _head(tspec.face_on_card(card, plan, 0, tspec.SAMPLE_FACE))
    img = render("demo", replace(card, distress=False))
    rows = _green_rows(img, 60, 1020)
    assert rows and min(rows) >= 600 and max(rows) <= 600 + 0.25 * 1350, (min(rows), max(rows))
    assert min(rows) > head[3]                           # nothing of the plate on the face or head


def test_a_credit_under_a_photos_border_or_rounded_corner_moves_the_photo_or_is_refused():
    spec = _mini(photos=[{"box": [0, 0, 1, 1], "radius": 200, "border": {"width": 40}}],
                 text=[{"slot": "head", "box": [0.05, 0.05, 0.9, 0.1]}])
    mark = (0.66, 0.965, 0.32, 0.03)                     # a credit along the bottom edge, into the corner
    card = _card(spec, {"head": "A HEADLINE"}, photos=[_photo(1080, 1350, None)], faces=[[]], keep=[[mark]])
    plan = tspec.plan(card)
    frame = tspec._frame_rects(spec["photos"][0], spec["canvas"])
    if plan.problems:
        assert "watermark or credit" in plan.problems[0]
    else:                                                # moved (the whole photo shown) inside the frame
        assert not any(_hits(tspec.face_on_card(card, plan, 0, mark), f) for f in frame)
    plain = _mini(text=[{"slot": "head", "box": [0.05, 0.05, 0.9, 0.1]}])
    lone = tspec.plan(_card(plain, {"head": "A HEADLINE"}, photos=[_photo(1080, 1350, None)], faces=[[]], keep=[[mark]]))
    assert lone.problems == [] and lone.photos[0].mode == "cover"         # without the frame it was fine where it was
    # a face under the border band counts as covered too
    edge = (0.42, 0.0, 0.16, 0.12)
    framed = _mini(photos=[{"box": [0, 0, 1, 1], "border": {"width": 40}}], text=[{"slot": "head", "box": [0.05, 0.8, 0.9, 0.1]}])
    card = _card(framed, {"head": "A HEADLINE"}, photos=[_photo(1080, 1350, edge)], faces=[[edge]])
    plan = tspec.plan(card)
    assert plan.problems or not any(_hits(_head(tspec.face_on_card(card, plan, 0, edge)), f)
                                    for f in tspec._frame_rects(framed["photos"][0], framed["canvas"]))
    assert tspec.plan(_card(_mini(text=framed["text"]), {"head": "A HEADLINE"}, photos=[_photo(1080, 1350, edge)],
                            faces=[[edge]])).photos[0].mode == "cover"


def _big_closeup() -> bytes:
    """A real face (tests/data/faces) in a 4050 px photo: the detector misses it at that size, verify
    finds it on the card."""
    import cv2
    import numpy as np

    from conftest import face_photo
    im = cv2.imdecode(np.frombuffer(face_photo("closeup.jpg"), np.uint8), cv2.IMREAD_COLOR)   # 1350 px, face ~40% down
    return cv2.imencode(".jpg", cv2.resize(im, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC))[1].tobytes()


def test_the_plan_finds_faces_at_the_size_verify_looks_and_the_render_keeps_them_clear(tmp_path):
    import cv2
    import numpy as np

    from bore.guard import detect_faces
    big = _big_closeup()
    assert detect_faces(cv2.imdecode(np.frombuffer(big, np.uint8), cv2.IMREAD_COLOR)) == []   # all the plan saw before
    faces = tspec.detect(big)
    assert faces and 0.3 < faces[0][1] < 0.45                           # the face, found at the card's size
    spec = _mini(text=[{"slot": "head", "box": [0.05, 0.4, 0.9, 0.12], "alt_box": [0.05, 0.86, 0.9, 0.1],
                        "size": [40, 90], "max_lines": 1}])
    copy = {"template_spec": "mini", "slots": {"head": "A HEADLINE"}, "people_in_photo": ["Demo Person"]}
    card = tspec.card_spec(spec, copy, [big], theme=THEME, keep=[[]])
    plan = tspec.plan(card)
    assert plan.problems == [] and plan.text_boxes[0] == tspec.px(spec["text"][0]["alt_box"], spec["canvas"])
    (tmp_path / "card.jpg").write_bytes(render("demo", replace(card, distress=False)))
    res = verify(str(tmp_path / "card.jpg"), {"page": "demo", "photos": [], "template_specs": {"mini": spec}}, copy,
                 source_faces_override=[])
    assert res["faces"] and not any("overlaps a face" in e for e in res["errors"]), res


def test_the_grit_texture_has_no_seam_where_its_tile_repeats():
    """The 900 px grit tile did not wrap: a hard line at x=900 (and y=900) on every textured card."""
    import numpy as np
    from PIL import Image
    spec = _mini(background={"fill": "#808080", "texture": "grit"}, photos=[{"box": [0.9, 0.9, 0.1, 0.1]}],
                 text=[{"slot": "head", "box": [0.9, 0.0, 0.1, 0.05], "size": [10, 12]}])
    img = np.asarray(Image.open(io.BytesIO(render("demo", _card(spec, {"head": "X"}, faces=[[]])))).convert("L"), float)
    rows = img[100:1100]
    step = np.abs(np.diff(rows, axis=1)).mean(axis=0)            # how much each column differs from the next
    assert step[899] < 3 * np.median(step[600:880]), (step[899], np.median(step[600:880]))
    cols = img[:, 100:900]
    down = np.abs(np.diff(cols, axis=0)).mean(axis=1)
    assert down[899] < 3 * np.median(down[600:880]), (down[899], np.median(down[600:880]))


# --------------------------------------------------------------------------- lone digits read back
def _spec_dir(d, fid, slots, photo=None):
    from bore import routine
    (d / "photos").mkdir(parents=True)
    (d / "photos/0.jpg").write_bytes(photo or tspec.sample_photos(1)[0][0])
    brief = {"page": "demo", "photos": [{"index": 0, "file": "photos/0.jpg"}], "studio": {"theme": THEME},
             "template_specs": {fid: json.loads((FIX / f"{fid}.json").read_text())}}
    copy = {"template_spec": fid, "slots": slots, "photo_index": 0}
    routine._render_spec(d, brief, copy, None, "template")
    return brief, copy


def _busy_photo(w: int = 1500, h: int = 1875) -> bytes:
    """A made-up photo with a real photo's clutter (stadium-like colour blocks, light and dark
    patches, grain): what a figure or a plate is drawn over."""
    import numpy as np
    from PIL import Image, ImageDraw
    rng = np.random.default_rng(7)
    y, x = np.mgrid[0:h, 0:w]
    base = np.stack([60 + 80 * x / w, 70 + 60 * y / h, 110 - 50 * x / w], -1)
    im = Image.fromarray(np.clip(base + rng.normal(0, 14, (h, w, 3)), 0, 255).astype("uint8"))
    dr = ImageDraw.Draw(im)
    for _ in range(60):
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        dr.rectangle([x0, y0, x0 + int(rng.integers(20, 300)), y0 + int(rng.integers(20, 300))],
                     fill=tuple(int(c) for c in rng.integers(0, 255, 3)))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90)
    return b.getvalue()


def test_a_lone_digit_on_a_plate_or_as_the_giant_figure_is_read_where_it_is_drawn(tmp_path):
    """OCR of the whole card dropped a lone '1' (and read '2' as '7'): a correct card failed verify, and
    a '1' anywhere else on the card (SAMPLE PHOTO 1) could stand in for a missing one."""
    real = _busy_photo()
    plates = [{"value": "1", "label": "SACKS"}, {"value": "7", "label": "TACKLES"}]
    cases = (("mega-figure", {"headline": ["MOST POINTS", "IN ONE NIGHT"], "figure": "1"}, real),
             ("tally-tower", {"verdict": "BIG NIGHT", "stats": plates}, real),
             ("mega-figure", {"headline": ["MOST POINTS", "IN ONE NIGHT"], "figure": "2"}, real),  # Anton's '2' reads '7'
             ("mega-figure", {"headline": ["MOST POINTS", "IN ONE NIGHT"], "figure": "2"}, None),
             ("tally-tower", {"verdict": "BIG NIGHT", "stats": [plates[0], {"value": "2", "label": "TACKLES"}]}, None))
    for n, (fid, slots, photo) in enumerate(cases):
        d = tmp_path / str(n)
        brief, copy = _spec_dir(d, fid, slots, photo)
        res = verify(str(d / "card.jpg"), brief, copy, faces_override=[], source_faces_override=[])
        assert res["pass"], (fid, slots, res["errors"], res["ocr"])
    # the plate shows '2' where the copy says '9' (as if drawn so): refused
    rec = json.loads((d / "render.json").read_text())
    rec["lines"] = [{**x, "text": "9" if x["text"] == "2" else x["text"]} for x in rec["lines"]]
    (d / "render.json").write_text(json.dumps(rec))
    res = verify(str(d / "card.jpg"), brief, {**copy, "slots": {**copy["slots"], "stats": [plates[0], {"value": "9", "label": "TACKLES"}]}},
                 faces_override=[], source_faces_override=[])
    assert any("'9' (it could not be read where it is drawn)" in e for e in res["errors"]), res["errors"]
