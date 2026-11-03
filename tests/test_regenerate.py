"""Reviewer feedback -> regeneration: brief contents, Krea prompt, upload payload and gates."""
import io
import json
import types

import pytest
from PIL import Image

from bore import routine as R


def _jpeg(w=1200, h=1500, color=(120, 40, 40)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "JPEG")
    return b.getvalue()


IMAGES = {"https://x/ref.jpg": _jpeg(), "https://x/src.jpg": _jpeg(color=(0, 90, 0)),
          "https://x/card.jpg": _jpeg(1080, 1350)}


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    import requests

    def get(url, **kw):
        data = IMAGES.get(url)
        return types.SimpleNamespace(ok=data is not None, content=data or b"")
    monkeypatch.setattr(requests, "get", get)


def _request(with_brief=True):
    req = {"id": "demo-celeb-202609271637-scarlett-colin", "page": "demo-celeb", "page_name": "Demo Celeb",
           "caption": "SCARLETT & COLIN ARE BACK\n\nFirst joint red carpet in more than a year.",
           "headline": "SCARLETT & COLIN RED CARPET RETURN", "sources": ["https://eonline.com/a"],
           "verification": {"route": "corroborated", "sources": ["x:enews", "x:variety"]},
           "source_photo": "https://x/src.jpg", "image_url": "https://x/card.jpg",
           "feedback": "Make the photo bigger, no polaroid frame", "reviewer_caption": "",
           "reference_image_url": "https://x/ref.jpg"}
    if with_brief:
        req["brief"] = {"posts": [{"platform": "x", "author": "enews", "text": "Scarlett Johansson and Colin Jost "
                                   "made their first joint red carpet appearance in more than a year", "url": "u"}],
                        "verification": req["verification"], "headline_hint": "Scarlett and Colin",
                        "photos": [{"url": "https://x/src.jpg", "source": "og_image", "credit": ""}]}
        req["copy"] = {"template": "headline", "headline_lines": ["SCARLETT & COLIN", "RED CARPET RETURN"]}
    return req


def test_regen_brief_carries_story_feedback_and_images(tmp_path, cfg):
    d = R.write_regen_brief(tmp_path, _request(), cfg)
    b = json.loads((d / "brief.json").read_text())
    rg = b["regeneration"]
    assert rg["replaces"] == "demo-celeb-202609271637-scarlett-colin" and not rg["legacy_sources"]
    assert b["id"].startswith("demo-celeb-") and b["id"].endswith("-scarlett-colin-v2") and b["id"] != rg["replaces"]
    assert rg["feedback"].startswith("Make the photo bigger")
    assert b["photos"][rg["reference_photo_index"]]["source"] == "reviewer upload"
    assert len(b["photos"]) == 2                    # source photo deduplicated with the snapshot
    assert (d / "previous_card.jpg").exists() and b["posts"][0]["author"] == "enews"
    assert "changes" in b["copy_schema"] and "design_notes" in b["copy_schema"]
    assert R.regen_id(b["id"]).endswith("-scarlett-colin-v2")   # no -v2-v2 on a second round


def test_legacy_draft_uses_previous_copy_as_fact_base(tmp_path, cfg):
    d = R.write_regen_brief(tmp_path, _request(with_brief=False), cfg)
    b = json.loads((d / "brief.json").read_text())
    assert b["regeneration"]["legacy_sources"]
    assert "more than a year" in b["posts"][0]["text"]


def test_design_notes_and_style_example_reach_krea(tmp_path, cfg):
    d = R.write_regen_brief(tmp_path, _request(), cfg)
    b = json.loads((d / "brief.json").read_text())
    copy = {"template": "headline", "headline_lines": ["SCARLETT & COLIN", "RED CARPET RETURN"],
            "photo_index": 1, "style_photo_index": b["regeneration"]["reference_photo_index"],
            "design_notes": "full-bleed photo, no polaroid frame", "caption": "x"}
    (d / "copy.json").write_text(json.dumps(copy))
    cfg["krea"]["mode"] = "art"
    art = R.krea_request(d, cfg, None)
    assert "full-bleed photo, no polaroid frame" in art["input"]["prompt"]
    assert art["input"]["image_style_references"][0]["url"] == "https://x/ref.jpg"
    cfg["krea"]["mode"] = "full"
    req = R.krea_request(d, cfg, None)
    p = req["input"]["prompt"]
    assert "full-bleed photo, no polaroid frame" in p and "editor's layout/style example" in p
    assert req["input"]["image_urls"][0] == "https://x/src.jpg"
    assert req["input"]["image_urls"][-1] == "https://x/ref.jpg"
    cfg["krea"]["mode"] = "premium"                          # (the default: owner, Nov 1)
    req = R.krea_request(d, cfg, None)
    p = req["input"]["prompt"]
    assert req["model"] == "google/nano-banana-2.1"
    assert "full-bleed photo, no polaroid frame" in p and "editor's layout/style example" in p
    assert req["input"]["image_urls"] == ["https://x/src.jpg", "https://x/ref.jpg"]   # no upload URL: no layout image


def test_payload_links_old_draft_and_keeps_brief(tmp_path, cfg):
    d = R.write_regen_brief(tmp_path, _request(), cfg)
    (d / "copy.json").write_text(json.dumps({"caption": "new caption", "headline_lines": ["A"], "photo_index": 1,
                                             "changes": "Bigger photo, removed the frame"}))
    (d / "card.jpg").write_bytes(_jpeg(1080, 1350))
    p = R.draft_payload(d)
    assert p["replaces"] == "demo-celeb-202609271637-scarlett-colin"
    assert p["feedback_applied"].startswith("Make the photo bigger") and p["changes"].startswith("Bigger")
    assert p["previous_image_url"] == "https://x/card.jpg"
    assert p["brief"]["posts"] and "caption" not in p["copy"]


def test_normal_payload_stores_brief_for_later_feedback(tmp_path, cfg):
    d = R.write_regen_brief(tmp_path, _request(), cfg)
    b = json.loads((d / "brief.json").read_text())
    b.pop("regeneration")
    (d / "brief.json").write_text(json.dumps(b))
    (d / "copy.json").write_text(json.dumps({"caption": "c", "headline_lines": ["A"], "photo_index": 0}))
    (d / "card.jpg").write_bytes(_jpeg(1080, 1350))
    p = R.draft_payload(d)
    assert "replaces" not in p and p["brief"]["verification"]["route"] == "corroborated"


def test_upload_refuses_regen_without_changes(tmp_path, cfg, monkeypatch):
    from bore import cli
    d = R.write_regen_brief(tmp_path, _request(), cfg)
    (d / "copy.json").write_text(json.dumps({"caption": "c", "headline_lines": ["A"], "photo_index": 0}))
    (d / "card.jpg").write_bytes(_jpeg(1080, 1350))
    for f, body in (("lint.json", {"pass": True}), ("ocr.json", {"pass": True}), ("verdict.json", {"pass": True})):
        (d / f).write_text(json.dumps(body))
    args = types.SimpleNamespace(dir=str(d))
    with pytest.raises(SystemExit, match="changes"):
        cli._routine_upload(args, cfg, R)
