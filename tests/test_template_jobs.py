"""A page's templates as template specs, converted by a Studio job and used once approved (Oct 23:
editors' new templates were only ever text for the ideas step, and Studio drew the nearest coded
layout instead of the template they chose). The conversion job's files (STUDIO.md TEMPLATES), the
approved specs `studio sync` writes, the ideas that name them, and the card drawn with the chosen one
on the editor's photo. Every spec here is made up (tests/fixtures/tspec/); the pages' own are
confidential."""
import base64
import io
import json
import os
import time
from pathlib import Path

import pytest
from PIL import Image

from bore import cli, ops, tspec
from bore import studio as S

FIX = Path(__file__).parent / "fixtures" / "tspec"
RAW = {p.stem: json.loads(p.read_text()) for p in FIX.glob("*.json")}
THEME = {"accent": "#ffcc00", "bg": "#101a2c", "ink": "#ffffff", "logo_text": "Demo Page"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The repository's private/ and work/ folders in tmp_path (S, tspec and the CLI all see them)."""
    import bore.routine as R
    monkeypatch.setattr(S, "ROOT", tmp_path)
    monkeypatch.setattr(S, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(S, "WORK", tmp_path / "work" / "studio")
    monkeypatch.setattr(R, "WORK", tmp_path / "work")
    monkeypatch.setattr(tspec, "SPEC_DIR", tmp_path / "private" / "templates")
    monkeypatch.setattr(ops, "_DEGRADED", [])
    monkeypatch.setenv("BORE_ROUTINE", "manual")
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    monkeypatch.setattr(S, "_dash", lambda: object())       # the commands' dashboard: the Dash below answers
    (tmp_path / "private").mkdir()
    return tmp_path


def sync_with(monkeypatch, templates, **page):
    """`studio sync` against a config whose page has these approved templates (None: an older dashboard)."""
    from bore import lessons
    monkeypatch.setattr(lessons, "save", lambda slug, rec: None)
    p = {"slug": "demo", "name": "Demo Page", "sources": [], "pipeline": {"hourly": False}, "theme": THEME, "docs": {}, **page}
    if templates is not None:
        p["templates"] = templates
    monkeypatch.setattr(S, "_get", lambda d, path, **k: {"pages": [p]} if path == "config" else {})
    S.sync(object(), quiet=True)
    return S.studio_meta()["demo"]


def approved(name, v=1, **over):
    return {"id": name, "v": v, "doc_version": 3, "spec": {**RAW[name], **over}}


# ------------------------------------------------------------------------------------------- sync
def test_sync_writes_only_the_approved_specs_and_lists_them_for_the_briefs(home, monkeypatch):
    meta = sync_with(monkeypatch, [approved("tally-tower"), approved("mega-figure", 2)])
    store = home / "private" / "templates" / "demo"
    assert sorted(f.name for f in store.glob("*.json")) == ["mega-figure.json", "tally-tower.json"]
    assert json.loads((store / "tally-tower.json").read_text()) == RAW["tally-tower"]
    listed = {t["id"]: t for t in meta["templates"]}
    assert [t["name"] for t in meta["templates"]] == ["Mega Figure", "Tally Tower"]
    tt = listed["tally-tower"]
    assert (tt["visual_lane"], tt["art_layer"], tt["photos"], tt["file"], tt["v"], tt["doc_version"]) == \
           ("clean", False, 1, "private/templates/demo/tally-tower.json", 1, 3)
    assert tt["slots"]["stats"] == {"type": "list", "required": True, "min_items": 2, "max_items": 4,
                                    "hint": "two to four plates; the last is drawn in the accent colour"}
    assert listed["mega-figure"]["art_layer"] is True
    assert tspec.find("tally-tower", {"page": "demo"})["name"] == "Tally Tower", "lint and the renderer find it"

    # taken out of use: removed, so it can never be drawn; one that does not validate is left out
    meta = sync_with(monkeypatch, [approved("mega-figure", 2), approved("said-it", text=[{"slot": "nope", "box": [0, 0, 1, 1]}])])
    assert [t["id"] for t in meta["templates"]] == ["mega-figure"]
    assert sorted(f.name for f in store.glob("*.json")) == ["mega-figure.json"]
    assert ops._DEGRADED == ["approved template spec not drawable"]
    # an older dashboard sends no templates at all: the store is left as it is
    assert sync_with(monkeypatch, None)["templates"] == [] and (store / "mega-figure.json").exists()


# ------------------------------------------------------------------------------- the conversion job
def page_images(home, n=3):
    folder = home / "private" / "docs" / "demo" / "templates"
    folder.mkdir(parents=True)
    out = []
    for i in range(1, n + 1):
        Image.new("RGB", (927, 1200), (40 * i, 90, 140)).save(folder / f"page-{i:02d}.png")
        out.append(f"private/docs/demo/templates/page-{i:02d}.png")
    return out


def meta_with_doc(home, monkeypatch, images):
    meta = {"demo": {"name": "Demo Page", "theme": THEME, "templates": [],
                     "docs": {"templates": {"version": 3, "file": "private/docs/demo/templates.txt", "pages": len(images),
                                            "images": images, "image_pages": len(images)}}}}
    (home / "private" / "studio.json").write_text(json.dumps(meta))
    return meta


def templates_job(**over):
    return {"id": "demo-1761200000000-abcd", "kind": "templates", "page": "demo", "batch_id": None, "post_ids": [],
            "user": "admin@example.com", "status": "queued", "doc_version": 3, "only": None, "notes": {}, "reason": "new_document",
            "posts": [], "templates": [], **over}


class Dash:
    """The dashboard's routine API as `studio` calls it (_get / _post)."""

    def __init__(self, jobs):
        self.jobs, self.posted = jobs, []

    def get(self, dash, path, **params):
        assert path == "jobs"
        return {"jobs": self.jobs}

    def post(self, dash, path, body):
        self.posted.append((path, body))
        if path == "templates":
            return {"id": body["spec"]["id"], "v": sum(1 for p, _ in self.posted if p == "templates"), "status": "pending"}
        return {"status": body.get("status")}


def test_a_templates_job_gives_the_session_the_document_and_takes_back_a_checked_spec_preview_and_page(home, monkeypatch, capsys):
    images = page_images(home)
    meta_with_doc(home, monkeypatch, images)
    rejected = {"id": "said-it", "name": "Said It", "status": "rejected", "in_use": None, "note": "The quote sits too low.",
                "v": 2, "doc_version": 3, "doc_pages": [2], "spec": RAW["said-it"]}
    dash = Dash([templates_job(templates=[rejected])])
    monkeypatch.setattr(S, "_get", dash.get)
    monkeypatch.setattr(S, "_post", dash.post)
    [jd] = S.pull_jobs({}, dash=object())
    said = capsys.readouterr().out
    assert dash.posted == [(f"jobs/{jd.name}", {"status": "running"})], "claimed like every job"
    assert "TEMPLATES  page demo" in said and "convert every template of the document" in said
    assert "Said It" not in said, "the document's template names stay out of the session's output"
    brief = json.loads((jd / "brief.json").read_text())
    assert (brief["kind"], brief["convert"], brief["doc"]["version"], brief["doc"]["images"], brief["notes"]) == \
           ("templates", "all", 3, images, {})
    assert brief["templates"][0]["note"] == "The quote sits too low." and brief["templates"][0]["spec"] == f"work/studio/{jd.name}/_current/said-it.json"
    assert json.loads((jd / "_current" / "said-it.json").read_text()) == RAW["said-it"], "a rejected one is converted again from its spec"
    assert brief["preview"] == (f"python -m bore.cli routine tspec-render work/studio/{jd.name}/<id>/spec.json --sample --page demo "
                                f"--out work/studio/{jd.name}/<id>/preview.png")
    assert "templates" not in json.loads((jd / "job.json").read_text()), "the job record only"
    assert brief["new_id"] == "t1"
    # a new template takes the next id none had, never its place in the document (t2 is t1's and t3's neighbour)
    later = S.write_templates_job({**templates_job(templates=[{**rejected, "id": i} for i in ("t1", "t3", "said-it")]), "id": "demo-2-cd"})
    assert json.loads((later / "brief.json").read_text())["new_id"] == "t4"

    # the session writes a template's folder and draws its preview on the stand-in photo
    d = jd / "tally-tower"
    d.mkdir()
    (d / "spec.json").write_text(json.dumps(RAW["tally-tower"]))
    (d / "source.json").write_text(json.dumps({"pages": [2, 3]}))
    with pytest.raises(ValueError, match="draw the preview from the current spec.json"):
        S.send_template(d)
    monkeypatch.chdir(home)
    cli.main(["routine", "tspec-render", str(d / "spec.json"), "--sample", "--page", "demo", "--out", str(d / "preview.png")])
    assert Image.open(d / "preview.png").size == (1080, 1350)
    r = S.send_template(d)
    path, body = dash.posted[-1]
    assert path == "templates" and r["v"] == 1
    assert (body["page"], body["job"], body["doc_version"], body["doc_pages"], body["spec"]) == ("demo", jd.name, 3, [2, 3], RAW["tally-tower"])
    pics = [base64.b64decode(body["preview_b64"])] + [base64.b64decode(x["image_b64"]) for x in body["pages"]]
    assert [x["n"] for x in body["pages"]] == [2, 3]
    assert all(p[:3] == b"\xff\xd8\xff" and len(p) < 1_400_000 for p in pics), "JPEGs the dashboard takes"
    assert len(json.dumps(body)) < 4.5e6, "one request"
    assert Image.open(io.BytesIO(pics[1])).size == (927, 1200)
    n = len(dash.posted)
    S.send_template(d)
    assert len(dash.posted) == n, "sent again unchanged: nothing sent"

    # what is refused before anything is sent
    def refused(match, **files):
        bad = jd / "bad-one"
        bad.mkdir(exist_ok=True)
        for k, v in {"spec.json": json.dumps({**RAW["tally-tower"], "id": "bad-one"}), "source.json": '{"pages": [1]}', **files}.items():
            (bad / k).write_text(v)
        os.utime(bad / "spec.json", (time.time() - 60, time.time() - 60))
        Image.new("RGB", (10, 10)).save(bad / "preview.png")
        with pytest.raises(ValueError, match=match):
            S.send_template(bad)
    refused("the folder must be named after it", **{"spec.json": json.dumps(RAW["tally-tower"])})
    refused("unknown key 'colour'", **{"spec.json": json.dumps({**RAW["tally-tower"], "id": "bad-one", "colour": "red"})})
    refused(r"source.json must be .* \(page-NN.png: 1-3\)", **{"source.json": '{"pages": [7]}'})
    assert len(dash.posted) == n


def test_studio_done_sends_what_is_left_and_refuses_while_a_rejected_template_is_not_converted_again(home, monkeypatch, capsys):
    images = page_images(home, 2)
    meta_with_doc(home, monkeypatch, images)
    dash = Dash([templates_job(only=["said-it", "mega-figure"], notes={"said-it": "Lower the quote.", "mega-figure": "Bigger number."},
                               reason="rejected")])
    monkeypatch.setattr(S, "_get", dash.get)
    monkeypatch.setattr(S, "_post", dash.post)
    [jd] = S.pull_jobs({}, dash=object())
    assert "convert 2 rejected template(s) again" in capsys.readouterr().out
    assert json.loads((jd / "brief.json").read_text())["convert"] == ["said-it", "mega-figure"]
    d = jd / "said-it"
    d.mkdir()
    (d / "spec.json").write_text(json.dumps(RAW["said-it"]))
    (d / "source.json").write_text('{"pages": [1]}')
    monkeypatch.chdir(home)
    cli.main(["routine", "tspec-render", str(d / "spec.json"), "--sample", "--out", str(d / "preview.png")])
    with pytest.raises(SystemExit, match=r"NOT DONE:\n  mega-figure: rejected by the editors, not converted again"):
        S.finish_job(jd, dash=object())
    assert [p for p, _ in dash.posted if p == "templates"] == ["templates"], "the finished one went to the editors meanwhile"
    cli.main(["studio", "fail", str(jd / "mega-figure"), "--reason", "Its photo grid has no zone in the format yet"])
    S.finish_job(jd, dash=object())
    path, body = dash.posted[-2]
    assert path == f"jobs/{jd.name}"
    assert (body["status"], body["posts"], body["templates"]) == \
           ("done", {}, {"sent": ["said-it"], "failed": {"mega-figure": "Its photo grid has no zone in the format yet"}})
    assert dash.posted[-1][0] == "usage" and dash.posted[-1][1]["entries"][0]["unit"] == "template"
    assert [p for p, _ in dash.posted].count("templates") == 1, "sent once"


def test_a_template_folder_to_fix_is_an_answer_and_a_whole_templates_job_can_fail(home, monkeypatch):
    meta_with_doc(home, monkeypatch, page_images(home, 1))
    dash = Dash([templates_job()])
    monkeypatch.setattr(S, "_get", dash.get)
    monkeypatch.setattr(S, "_post", dash.post)
    [jd] = S.pull_jobs({}, dash=object())
    (jd / "said-it").mkdir()
    (jd / "said-it" / "spec.json").write_text(json.dumps(RAW["said-it"]))
    with pytest.raises(SystemExit, match="NOT DONE: the template folder needs fixing first:\n  draw the preview from the current spec.json"):
        cli.main(["studio", "template-send", str(jd / "said-it")])
    # the job folder holds the job's brief too: `studio fail` on it fails the whole job
    cli.main(["studio", "fail", str(jd), "--reason", "The page has no templates document any more"])
    assert dash.posted[-1] == (f"jobs/{jd.name}", {"status": "failed", "error": "The page has no templates document any more", "posts": {}})
    assert not (jd / "fail.txt").exists()


def test_a_templates_job_whose_pages_cannot_be_drawn_here_is_handed_back(home, monkeypatch):
    meta_with_doc(home, monkeypatch, [])

    def no_module(*a):
        raise ModuleNotFoundError("No module named 'pypdfium2'")
    monkeypatch.setattr(S, "doc_images", no_module)
    with pytest.raises(RuntimeError, match="pages could not be drawn here .*pypdfium2.*run `studio jobs` again"):
        S.write_job(templates_job(), {})
    from bore.deps import NEEDS
    assert {"pypdfium2", "chromium"} <= set(NEEDS["templates"]), "checked before it is claimed"


# ------------------------------------------------------------------------------- using the templates
def ideas(*specs):
    return [{"title": f"Idea {i}", "template": name, "description": "d", **({"template_spec": sid} if sid else {}),
             **({"layout": layout} if layout else {})} for i, (sid, name, layout) in enumerate(specs, 1)]


def test_ideas_name_the_pages_approved_templates_or_a_layout(home, monkeypatch):
    monkeypatch.setattr(S, "_page_templates", lambda page: [])
    sync_with(monkeypatch, [approved("tally-tower"), approved("said-it")])
    assert S.check_ideas(ideas(("tally-tower", "Tally Tower", ""), ("said-it", "Said It", "")), "demo") == []
    # one template approved early in the rollout fits no story of this kind: a layout is still an idea
    # (approving one template forced every idea of the page into the approved ones, Oct 23 audit)
    assert S.check_ideas(ideas(("tally-tower", "Tally Tower", ""), ("", "headline", "headline")), "demo") == []
    assert S.check_ideas(ideas(("", "headline", "headline"), ("", "quote", "quote")), "demo") == []
    assert S.check_ideas(ideas(("giant", "Giant", ""), ("said-it", "Wobbly Lantern", "")), "demo") == [
        "idea 1: template_spec must be one of the page's approved templates (said-it, tally-tower), or leave it out and pick a layout",
        "idea 2: template must be the name of the template_spec it names (its name in `templates`)"]
    assert S.check_ideas(ideas(("", "headline", ""), ("", "quote", "collage")), "demo") == [
        "idea 1 is missing layout", "idea 2: layout 'collage' must be one of " + str(sorted(S.GENERIC))]
    # a page with none approved: layouts, as before
    sync_with(monkeypatch, [])
    assert S.check_ideas(ideas(("", "headline", "headline"), ("", "quote", "quote")), "demo") == []
    assert S.check_ideas(ideas(("tally-tower", "Tally Tower", ""), ("", "quote", "quote")), "demo") == [
        "idea 1: the page has no approved templates yet: leave template_spec out and pick a layout"]


def test_briefs_offer_the_approved_templates(home, monkeypatch):
    import bore.routine as R
    monkeypatch.setattr(S, "_page_templates", lambda page: [])
    monkeypatch.setattr(R, "load_playbook", lambda page: {})
    sync_with(monkeypatch, [approved("tally-tower")])
    job = {"id": "j1", "kind": "compose", "page": "demo", "batch_id": "b",
           "posts": [{"id": "p1", "story": {"headline": "H", "summary": "S.", "sources": []}}]}
    brief = json.loads((S.write_job(job, {}) / "p1" / "brief.json").read_text())
    assert [t["id"] for t in brief["templates"]] == ["tally-tower"] and "template_spec" in brief["copy_schema"]
    sync_with(monkeypatch, [])
    brief = json.loads((S.write_job({**job, "id": "j2"}, {}) / "p1" / "brief.json").read_text())
    assert "templates" not in brief and "template_spec" not in brief["copy_schema"]


def _photo(face=(0.42, 0.14, 0.16, 0.14)):
    im = Image.new("RGB", (1200, 1500), (60, 90, 120))
    x, y, w, h = face
    Image.Image.paste(im, (230, 180, 200), (int(x * 1200), int(y * 1500), int((x + w) * 1200), int((y + h) * 1500)))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=92)
    return b.getvalue()


def compose_dir(home, idea, copy, spec="tally-tower"):
    d = home / "work" / "studio" / "j1" / "p1"
    (d / "photos").mkdir(parents=True)
    (d / "photos" / "0.jpg").write_bytes(_photo())
    (d / "photos" / "1.jpg").write_bytes(_photo((0.1, 0.1, 0.1, 0.1)))
    brief = {"id": "studio-p1", "page": "demo", "page_name": "Demo Page", "verification": {"route": "studio", "sources": ["x"]},
             "posts": [{"text": "Demo Player scored 31 points with 12 rebounds and 9 assists in 34 minutes, a record."}],
             "photos": [{"index": 0, "file": "photos/0.jpg", "url": "https://img.example.com/editor.jpg", "size": "1200x1500"},
                        {"index": 1, "file": "photos/1.jpg", "url": "https://img.example.com/other.jpg", "size": "1200x1500"}],
             "playbook": {}, "studio": {"kind": "compose", "idea": idea, "theme": THEME,
                                         "base_image": {"url": "https://img.example.com/editor.jpg"}}}
    (d / "brief.json").write_text(json.dumps(brief))
    (d / "copy.json").write_text(json.dumps(copy))
    return d


SPEC_COPY = {"template_spec": "tally-tower", "headline_lines": ["DEMO PLAYER'S BIG NIGHT"], "photo_index": 0,
             "people_in_photo": ["Demo Player"], "caption": "Demo Player was everywhere. Who stops him?", "hashtags": ["#Demo"],
             "slots": {"verdict": "UNSTOPPABLE", "stats": [{"value": "31", "label": "POINTS"}, {"value": "12", "label": "REBOUNDS"}]}}


def test_the_chosen_template_is_drawn_on_the_editors_photo_and_only_that_one_is_accepted(home, monkeypatch):
    import bore.routine as R
    sync_with(monkeypatch, [approved("tally-tower"), approved("said-it")])
    idea = {"choice": 1, "title": "The numbers", "template": "Tally Tower", "template_spec": "tally-tower", "text": "d"}
    d = compose_dir(home, idea, SPEC_COPY)
    seen = {}
    real = tspec.card_spec

    def spy(spec, copy, photos, **kw):
        seen.update(spec=spec["id"], photos=photos, theme=kw.get("theme"), art=kw.get("art"))
        return real(spec, copy, photos, **kw)
    monkeypatch.setattr(tspec, "card_spec", spy)
    assert R.render_fallback(d, {}) == {"engine": "template", "template_spec": "tally-tower"}
    assert seen["spec"] == "tally-tower" and seen["photos"] == [(d / "photos" / "0.jpg").read_bytes()], "the editor's photo, only"
    assert seen["theme"] == THEME and seen["art"] is None
    assert Image.open(d / "card.jpg").size == (1080, 1350) and (d / "card_clean.jpg").exists()
    assert json.loads((d / "attempts.json").read_text())[-1]["template_spec"] == "tally-tower"

    # studio done takes the card only when it is drawn with the template the editor chose
    for f, body in (("lint.json", {"pass": True}), ("ocr.json", {"pass": True}), ("verdict.json", {"pass": True})):
        (d / f).write_text(json.dumps(body))
    out = S.post_result(d, "compose")
    assert (out["card"]["template_spec"], out["card"]["slots"]["verdict"], out["photo"]) == \
           ("tally-tower", "UNSTOPPABLE", "https://img.example.com/editor.jpg")
    (d / "copy.json").write_text(json.dumps({**SPEC_COPY, "template_spec": "said-it"}))
    for f in ("lint.json", "ocr.json", "verdict.json"):
        os.utime(d / f)
    with pytest.raises(ValueError, match="the editor chose another of the page's templates: copy.json must draw it"):
        S.post_result(d, "compose")


def test_the_cli_refuses_what_a_spec_card_cannot_be(home, monkeypatch):
    sync_with(monkeypatch, [approved("tally-tower"), approved("mega-figure")])
    idea = {"choice": 1, "template": "Tally Tower", "template_spec": "tally-tower"}
    d = compose_dir(home, idea, {**SPEC_COPY, "slots": {**SPEC_COPY["slots"], "verdict": "UNSTOPPABLE"}})
    cfg = {"krea": {"mode": "art"}}
    with pytest.raises(SystemExit, match=r"REFUSED: the template you chose has no AI-art layer: draw it with `routine fallback"):
        cli._spec_art_or_exit(d, cfg)
    (d / "copy.json").write_text(json.dumps({**SPEC_COPY, "template_spec": "mega-figure"}))
    cli._spec_art_or_exit(d, cfg)                       # an art layer, art mode: Krea may paint it
    with pytest.raises(SystemExit, match="Krea paints only its art layer"):
        cli._spec_art_or_exit(d, {"krea": {"mode": "full"}})
    # text that does not fit its zone at the smallest size: refused, never clipped (a gate, not a crash)
    from bore.cards import TextOverflow
    import bore.routine as R

    def overflow(*a, **k):
        raise TextOverflow("slot 'verdict' does not fit its zone even at 52px: shorten it")
    monkeypatch.setattr(R, "render_fallback", overflow)
    monkeypatch.setattr(cli, "_lint_or_exit", lambda d, cfg, R: {"pass": True})
    with pytest.raises(SystemExit, match="REFUSED: slot 'verdict' does not fit"):
        cli._routine_fallback(type("A", (), {"dir": str(d)})(), cfg, R)


def test_a_studio_spec_card_without_an_art_layer_reserves_no_krea_image(home, monkeypatch):
    """Krea is one allowance for the hourly routine and Studio: a template drawn on the photo alone is
    refused before an image of it is reserved."""
    import bore.routine as R
    sync_with(monkeypatch, [approved("tally-tower")])
    d = compose_dir(home, {"choice": 1, "template": "Tally Tower", "template_spec": "tally-tower"}, SPEC_COPY)
    reserved = []
    monkeypatch.setattr(cli, "_dashboard", lambda: type("D", (), {"krea_reserve": lambda self, *a, **k: reserved.append(a) or {}})())
    monkeypatch.setattr(cli, "_lint_or_exit", lambda d, cfg, R: {"pass": True})
    args = type("A", (), {"dir": str(d), "upload_url": None, "feedback": None})()
    with pytest.raises(SystemExit, match="no AI-art layer"):
        cli._routine_krea_request(args, {"krea": {"mode": "art", "monthly_max_images": 1320}}, R)
    assert reserved == [] and not (d / "krea_request.json").exists()


def test_the_sample_photos_are_neutral_and_their_face_is_kept_clear(home):
    for name in RAW:
        spec = tspec.load(RAW[name])
        photos, faces = tspec.sample_photos(tspec.photo_count(spec))
        assert len(photos) == tspec.photo_count(spec) and faces == [[tspec.SAMPLE_FACE]] * len(photos)
        card = tspec.card_spec(spec, {"template_spec": spec["id"], "slots": tspec.example_slots(spec)}, photos,
                               theme=THEME, faces=faces, keep=[[] for _ in photos])
        plan = tspec.plan(card)
        x0, y0, x1, y1 = tspec.face_on_card(card, plan, 0, tspec.SAMPLE_FACE)
        head = (x0 - 0.18 * (x1 - x0), y0 - 0.45 * (y1 - y0), x1 + 0.18 * (x1 - x0), y1 + 0.08 * (y1 - y0))
        for zone, (a0, b0, a1, b1) in tspec.covering_boxes(card, plan):
            assert not (min(a1, head[2]) > max(a0, head[0]) and min(b1, head[3]) > max(b0, head[1])), (name, zone)


def test_a_spec_cards_art_prompt_is_the_specs_own_and_its_preview_marks_the_layer(home, monkeypatch, tmp_path):
    """Krea painted the page's playbook backdrop (another template's) into a spec's art layer, and the
    editors approved a preview drawn with no art at all (Oct 23 audit)."""
    import bore.routine as R
    monkeypatch.setattr(R, "load_playbook", lambda page: {"card": {"art": {"quote": "Gritty poster backdrop in {c1}"}}})
    painted = {**RAW["mega-figure"], "art": {**RAW["mega-figure"]["art"], "prompt": "Soft spotlight beams over a dark stage floor"}}
    sync_with(monkeypatch, [{"id": "mega-figure", "v": 1, "doc_version": 3, "spec": painted}])
    p = R.art_prompt({"page": "demo"}, {"template_spec": "mega-figure", "template": "Mega Figure"})
    assert p.startswith("Soft spotlight beams over a dark stage floor, in the page's colours #101a2c and #ffcc00.")
    assert "Gritty poster" not in p and R.NO_PEOPLE in p
    assert tspec.validate({**painted, "art": {"prompt": 5}}) == ["art: prompt must be 10-600 characters: the artwork the layer shows (no people, no text)"]
    sync_with(monkeypatch, [approved("mega-figure")])                    # no prompt: plain artwork, never the playbook's
    p = R.art_prompt({"page": "demo"}, {"template_spec": "mega-figure"})
    assert p.startswith("Abstract textured background artwork") and "Gritty" not in p
    # the preview the editors approve shows where the artwork goes
    out = tmp_path / "preview.png"
    cli.main(["routine", "tspec-render", str(FIX / "mega-figure.json"), "--sample", "--out", str(out)])
    from bore.cards import render
    spec = tspec.load(RAW["mega-figure"])
    photos, faces = tspec.sample_photos(1)
    bare = tspec.card_spec(spec, {"slots": tspec.example_slots(spec)}, photos, faces=faces, keep=[[]])
    plain = Image.open(io.BytesIO(render("preview", bare))).convert("RGB")
    band = [(x, y) for x in range(0, 1080, 40) for y in range(895, 940, 5)]     # the art layer below the photo
    marked = Image.open(out).convert("RGB")
    assert sum(abs(a - b) for p in band for a, b in zip(marked.getpixel(p), plain.getpixel(p))) / len(band) > 20
    assert tspec.art_stand_in(tspec.load(RAW["tally-tower"])) is None
    from bore.guard import ocr_lines
    assert "AIARTHERE" in "".join(ocr_lines(tspec.art_stand_in(spec))).replace(" ", "")


def test_no_template_name_fixed_text_or_id_reaches_a_run_report(home, monkeypatch):
    """A refusal's first line is what the run report keeps (ops.outcome): the template's name (as the
    confidential document names it) and its fixed words reached the dashboard's run log (Oct 23 audit)."""
    from bore.cards import FaceBlocked, TextOverflow, render
    secret = {**RAW["tally-tower"], "id": "t7", "name": "Velvet Hammer",
              "text": RAW["tally-tower"]["text"] + [{"text": "ONLY HERE", "box": [0.7, 0.86, 0.25, 0.03], "size": [40, 40]}]}
    sync_with(monkeypatch, [approved("tally-tower"), {"id": "t7", "v": 1, "doc_version": 3, "spec": secret}])
    idea = {"choice": 1, "template": "Velvet Hammer", "template_spec": "t7"}
    d = compose_dir(home, idea, {**SPEC_COPY, "template_spec": "t7"})
    reports = []

    def report(command, call):
        try:
            call()
        except (SystemExit, FaceBlocked, TextOverflow) as e:
            err = e if isinstance(e, SystemExit) else SystemExit(f"{'BLOCKED' if isinstance(e, FaceBlocked) else 'REFUSED'}: {e}")
            reports.append((str(err.code), ops.outcome(command, err)[1]))
    report("routine krea-request", lambda: cli._spec_art_or_exit(d, {"krea": {"mode": "art"}}))
    spec = tspec.load(secret)
    big = (0.1, 0.05, 0.8, 0.85)
    strict = tspec.load({**secret, "photos": [{"box": [0, 0, 1, 1], "contain_fallback": False}]})
    card = tspec.card_spec(strict, SPEC_COPY, [_photo()], theme=THEME, faces=[[big]], keep=[[]])
    report("routine fallback", lambda: render("demo", card))
    card = tspec.card_spec(spec, SPEC_COPY, [_photo()], theme=THEME, faces=[[]], keep=[[]])
    report("routine fallback", lambda: render("demo", card))           # 'ONLY HERE' at 40px does not fit its box
    jd = home / "work" / "studio" / "demo-1-ab"            # a templates job: the session's folder for t7
    (jd / "t7").mkdir(parents=True)
    (jd / "job.json").write_text(json.dumps({"id": "demo-1-ab", "kind": "templates", "page": "demo"}))
    (jd / "brief.json").write_text(json.dumps({"preview": "tspec-render <id>"}))
    (jd / "t7" / "spec.json").write_text(json.dumps({**secret, "use_when": ""}))
    report("studio template-send", lambda: cli.main(["studio", "template-send", str(jd / "t7")]))
    assert [r["refused"].split(":")[0] for _, r in reports] == ["REFUSED", "BLOCKED", "REFUSED", "NOT DONE"], reports
    for full, r in reports:
        assert "Velvet Hammer" not in r["refused"] and "ONLY HERE" not in r["refused"] and "t7" not in r["refused"], r
    assert "ONLY HERE" not in reports[2][0] and "fixed text 3" in reports[2][0], "the zone named by its place, never its words"
    # a template's folder is named by its id: the report names the job's folder
    args = type("A", (), {"cmd": "studio", "step": "template-send", "dir": str(jd / "t7")})()
    assert ops.target_of(args) == str(jd)
    # so does each preview drawn from its spec.json (run several times a template), absolute or not
    monkeypatch.chdir(home)                                # (the routine runs from the repository)
    for spec_path in (str(jd / "t7" / "spec.json"), "work/studio/demo-1-ab/t7/spec.json"):
        args = type("A", (), {"cmd": "routine", "step": "tspec-render", "dir": spec_path})()
        assert ops.target_of(args) == str(Path(spec_path).parent.parent), spec_path
    args = type("A", (), {"cmd": "routine", "step": "tspec-render", "dir": "tests/fixtures/tspec/tally-tower.json"})()
    assert ops.target_of(args) == "tests/fixtures/tspec/tally-tower.json", "a spec outside a templates job: as it is"
