"""Premium cards (owner, Nov 1): the image model makes the whole card from the real photo, in the
layout of the page's own card, with a photo credit; verify reads it back, confirms the hero number by
eye and refuses a card whose person is not the photo's."""
import io
import json

import pytest
from PIL import Image

from bore import guard as G
from bore import routine as R


def _jpeg(w=800, h=1000, color=(30, 60, 160)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "JPEG")
    return b.getvalue()


@pytest.fixture
def post(tmp_path):
    d = tmp_path / "p"
    (d / "photos").mkdir(parents=True)
    (d / "photos" / "0.jpg").write_bytes(_jpeg())
    brief = {"id": "p", "page": "demo-sports", "page_name": "Demo Sports",
             "photos": [{"index": 0, "file": "photos/0.jpg", "url": "https://cdn.example.com/a.jpg",
                         "source": "news", "credit": "www.sportsdesk.example"}]}
    copy = {"template": "report", "headline_lines": ["ALLEN'S WIN CHASE"], "stat_value": "102",
            "stat_label": "ONE WIN FROM PASSING BRADY", "photo_index": 0, "people_in_photo": ["Josh Allen"],
            "caption": "x"}
    (d / "brief.json").write_text(json.dumps(brief))
    (d / "copy.json").write_text(json.dumps(copy))
    return d, brief, copy


def test_the_credit_names_where_the_photo_came_from(post):
    _, brief, copy = post
    assert R.photo_credit(brief, copy) == "Photo: sportsdesk.example"
    brief["photos"][0]["credit"] = "@teamphotog"
    assert R.photo_credit(brief, copy) == "Photo: teamphotog"
    brief["photos"][0].update(credit="", url="https://pbs.example.com/media/x.jpg")
    assert R.photo_credit(brief, copy) == "Photo: example.com"
    assert R.photo_credit(brief, {**copy, "photo_index": -1}) == ""


def test_the_prompt_lists_exactly_the_cards_text_the_credit_and_keeps_the_person(post):
    _, brief, copy = post
    p = R.premium_prompt(brief, copy, "Photo: sportsdesk.example", layout=True)
    assert 'EXACTLY these 4 text elements' in p
    for t in ("ALLEN'S WIN CHASE", "102", "ONE WIN FROM PASSING BRADY", "Photo: sportsdesk.example"):
        assert f'"{t}"' in p
    assert "Josh Allen" in p and "never redraw" in p and "SECOND image" in p and "over any face" in p
    assert "SECOND image" not in R.premium_prompt(brief, copy, "", layout=False)


def test_the_request_sends_the_photo_and_the_pages_own_card_as_its_layout(post, cfg, monkeypatch):
    d, brief, copy = post
    sent = []

    class Answer:
        text = '"https://krea.example/asset"'

        def raise_for_status(self):
            pass
    monkeypatch.setattr("requests.post", lambda url, files=None, timeout=None: sent.append(files["file"][0]) or Answer())
    drawn = []
    monkeypatch.setattr(R, "render_fallback", lambda d, cfg, art=None, engine="template": drawn.append(engine)
                        or (d / "card.jpg").write_bytes(_jpeg(1080, 1350)) or {"engine": engine})
    cfg["krea"]["mode"] = "premium"
    req = R.krea_request(d, cfg, "https://upload.example")
    assert req["model"] == "google/nano-banana-2.1" and req["input"]["thinking_level"] == "high"
    assert sent == ["0.jpg", "layout.jpg"] and req["input"]["image_urls"] == ["https://krea.example/asset"] * 2
    assert drawn == ["layout"] and (d / "layout.jpg").exists() and not (d / "card.jpg").exists()
    assert not (d / "attempts.json").exists()          # drawing the layout is not an attempt
    assert json.loads((d / "copy.json").read_text())["photo_credit"] == "Photo: sportsdesk.example"


def test_a_premium_card_is_counted_as_its_model_and_marked_premium(post, cfg, store, monkeypatch):
    d, _, _ = post
    cfg["krea"]["mode"] = "premium"

    class Got:
        content = _jpeg(928, 1152)
    monkeypatch.setattr("requests.get", lambda url, timeout=None: Got())
    assert R.krea_result(d, cfg, store, "https://krea.example/card.png")["engine"] == "krea-premium"
    a = json.loads((d / "attempts.json").read_text())
    assert a[-1]["engine"] == "krea-premium" and a[-1]["model"] == "google/nano-banana-2.1"
    assert store.db.execute("SELECT actor FROM spend").fetchone()[0] == "krea:google/nano-banana-2.1"
    assert Image.open(d / "card.jpg").size == (1080, 1350)
    assert G._premium(d / "card.jpg")


def test_verify_asks_for_the_hero_number_by_eye_but_refuses_a_misspelled_line(post, monkeypatch):
    d, brief, copy = post
    (d / "card.jpg").write_bytes(_jpeg(1080, 1350))
    (d / "attempts.json").write_text(json.dumps([{"engine": "krea-premium"}]))
    copy["photo_credit"] = "Photo: sportsdesk.example"
    read = [("ALLEN'S WIN CHASE", 0.95, [10, 10, 500, 80]), ("ONE WIN FROM PASSING BRADY", 0.95, [10, 900, 500, 940]),
            ("Photo: sportsdesk.example", 0.9, [10, 1300, 200, 1320])]
    r = G.verify(str(d / "card.jpg"), brief, copy, ocr=lambda p: read, faces_override=[], source_faces_override=[])
    assert r["pass"], r["errors"]
    assert any(w.startswith("CONFIRM BY EYE: '102'") for w in r["warnings"])
    assert not any("not in the copy" in w for w in r["warnings"])          # the credit line is ours
    read[1] = ("ONE WIN FRON PAPSSING BRADY", 0.95, [10, 900, 500, 940])
    r = G.verify(str(d / "card.jpg"), brief, copy, ocr=lambda p: read, faces_override=[], source_faces_override=[])
    assert not r["pass"] and any("ONE WIN FROM PASSING BRADY" in e for e in r["errors"])
    # the template renderer's card: the number must be read as before
    (d / "attempts.json").write_text(json.dumps([{"engine": "template"}]))
    r = G.verify(str(d / "card.jpg"), brief, copy, ocr=lambda p: read[:1], faces_override=[], source_faces_override=[])
    assert any("'102'" in e for e in r["errors"])


def test_a_card_whose_person_is_not_the_photos_is_refused(post, monkeypatch):
    d, brief, copy = post
    (d / "card.jpg").write_bytes(_jpeg(1080, 1350))
    (d / "attempts.json").write_text(json.dumps([{"engine": "krea-premium"}]))
    from bore import faceid
    feats = {"photo": [(80.0, 8.0, "A")], "card": [(90.0, 8.0, "B")]}
    photo = (d / "photos/0.jpg").read_bytes()
    monkeypatch.setattr(faceid, "faces", lambda data: feats["photo"] if data == photo else feats["card"])
    monkeypatch.setattr(faceid, "clear", lambda f: True)
    monkeypatch.setattr(faceid, "judge", lambda ref, fs: ("mismatch", 0.12) if ref == "A" and fs[0][2] == "B" else ("match", 0.7))
    assert "does not look like the person in the chosen photo" in G._premium_face(d / "card.jpg", brief, copy)
    feats["card"] = [(90.0, 8.0, "A")]
    assert G._premium_face(d / "card.jpg", brief, copy) == ""
    assert G._premium_face(d / "card.jpg", brief, {**copy, "people_in_photo": []}) == ""   # nobody to compare
