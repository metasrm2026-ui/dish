"""The routines see a reference document's pages, not only its text (Oct 23: the templates document
is zone diagrams, and its text alone loses where each box sits). The job that needs them (a
templates job: S.doc_pages) downloads the active templates or visual references PDF part by part (one
dashboard response carries at most 4.5 MB), draws each page as private/docs/<page>/<type>/page-NN.png,
again only when the version changed, and lists them in the brief (studio.docs). `studio sync`, at
the start of every hourly, topics and Studio run, downloads none (Oct 23 audit: a 40 MB document was
17 requests and 17 audit entries a sync). Every document here is made up."""
import hashlib
import io
import json
import sys

import pytest
from PIL import Image

from bore import ops
from bore import studio as S

pytest.importorskip("pypdfium2")


def made_up_pdf(pages: int, size=(612, 792), label="MADE-UP CARD", text=True, photo_px=0) -> bytes:
    """A templates document like the editors' (a zone diagram a page), with made-up names. text=False:
    the pages are pictures only (text flattened when exported); photo_px: an example photo of that
    width on each page (noise: about 1 MB per 600 px, which makes a document of a realistic size)."""
    import numpy as np
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    rng = np.random.default_rng(7)
    for i in range(pages):
        if photo_px:
            im = io.BytesIO()
            Image.fromarray((rng.random((photo_px, photo_px, 3)) * 255).astype("uint8")).save(im, "JPEG", quality=95)
            c.drawImage(ImageReader(io.BytesIO(im.getvalue())), 40, 40, size[0] - 80, size[1] * 0.4)
        c.setFillColorRGB(0.9, 0.9, 0.9)
        c.rect(40, size[1] * 0.45, size[0] - 80, size[1] * 0.45, fill=1)
        c.setFillColorRGB(0, 0, 0)
        if text:
            c.setFont("Helvetica-Bold", 18)
            c.drawString(60, size[1] * 0.8, f"{label} {i + 1}: TITLE AREA (up to 8 words)")
            c.drawString(60, size[1] * 0.5, "BIG FIGURE AREA | TAG LINE (up to 30 characters)")
        else:
            c.rect(60, size[1] * 0.75, size[0] * 0.6, 30, fill=1)
        c.showPage()
    c.save()
    return buf.getvalue()


class FakeDash:
    """The dashboard's GET /api/routine/doc: one part per request (X-Doc-Parts), as routes/routine.js."""

    url = "https://dash.example"

    def __init__(self):
        self.docs, self.calls, self.s = {}, [], self

    def serve(self, typ, pdf, parts=2, old=False):
        cut = -(-len(pdf) // parts)
        self.docs[typ] = [pdf] if old else [pdf[i:i + cut] for i in range(0, len(pdf), cut)]
        entry = {"version": 1, "filename": f"{typ}.pdf", "pages": 3, "text": f"--- page 1 ---\nmade-up {typ} text"}
        if not old:       # an older dashboard's config has no parts, size or checksum: it sends the PDF whole
            entry.update(size=len(pdf), parts=len(self.docs[typ]), sha256=hashlib.sha256(pdf).hexdigest())
        return entry

    def get(self, url, params=None, timeout=None):
        assert url == f"{self.url}/api/routine/doc" and timeout
        self.calls.append((params["type"], params["part"]))
        body = self.docs[params["type"]][params["part"]]
        return type("R", (), {"content": body, "status_code": 200, "raise_for_status": lambda self: None})()


@pytest.fixture
def synced(tmp_path, monkeypatch):
    """sync() against a fake dashboard, writing into tmp_path/private; returns (dash, run(docs) -> meta)."""
    from bore import lessons
    monkeypatch.setattr(S, "ROOT", tmp_path)
    monkeypatch.setattr(S, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(lessons, "save", lambda slug, rec: None)
    monkeypatch.setattr(ops, "_DEGRADED", [])
    dash = FakeDash()
    page = {"slug": "dummy", "name": "Dummy Page", "sources": [], "pipeline": {"hourly": False}, "theme": {}}

    def run(docs, draw=True):
        """sync, then (draw) what a templates job does: each document's page images drawn here."""
        monkeypatch.setattr(S, "_get", lambda d, path, **k: {"pages": [{**page, "docs": docs}]} if path == "config" else {})
        S.sync(dash, quiet=True)
        for t in S.DOC_IMAGE_TYPES if draw else ():
            if t in docs:
                S.doc_pages("dummy", t, dash)
        return json.loads((tmp_path / "private" / "studio.json").read_text())["dummy"]["docs"]
    return dash, run


def test_sync_downloads_no_document(synced, tmp_path):
    dash, run = synced
    t = dash.serve("templates", made_up_pdf(2), parts=2)
    docs = run({"templates": t, "visual": dash.serve("visual", made_up_pdf(1), parts=1)}, draw=False)
    assert dash.calls == [] and docs["templates"]["images"] == [] and docs["visual"]["images"] == []
    assert docs["templates"]["ref"] == {k: t[k] for k in ("version", "sha256", "size", "parts")}
    assert not ops._DEGRADED
    run({"templates": t})                                     # a templates job draws them here
    dash.calls.clear()
    assert len(run({"templates": t}, draw=False)["templates"]["images"]) == 2, "drawn here before: listed"
    assert dash.calls == []


def test_sync_draws_each_page_of_the_templates_once_per_version(synced, tmp_path):
    dash, run = synced
    pdf = made_up_pdf(3)
    t = dash.serve("templates", pdf, parts=2)
    docs = run({"templates": t, "tone": dash.serve("tone", made_up_pdf(1), parts=1)})
    folder = tmp_path / "private" / "docs" / "dummy" / "templates"
    assert docs["templates"]["images"] == [f"private/docs/dummy/templates/page-0{i}.png" for i in (1, 2, 3)]
    assert docs["templates"]["image_pages"] == 3
    assert docs["templates"]["file"] == "private/docs/dummy/templates.txt"
    assert "images" not in docs["tone"], "the tone document is words: no page images"
    assert dash.calls == [("templates", 0), ("templates", 1)], "downloaded part by part"
    for f in folder.glob("page-*.png"):
        im = Image.open(f)
        assert im.height == S.DOC_PAGE_PX and abs(im.width - 612 * S.DOC_PAGE_PX / 792) < 1, "tall enough to read a zone diagram"
    assert not ops._DEGRADED

    # the next run, same version: nothing downloaded or drawn again
    before = {f.name: f.stat().st_mtime_ns for f in folder.glob("page-*.png")}
    dash.calls.clear()
    assert run({"templates": t})["templates"]["images"] == docs["templates"]["images"]
    assert dash.calls == [] and before == {f.name: f.stat().st_mtime_ns for f in folder.glob("page-*.png")}

    # a new version (or a rollback) with fewer pages: drawn again, the old pages gone
    t2 = {**dash.serve("templates", made_up_pdf(2, label="MADE-UP CARD V2"), parts=3), "version": 2}
    assert run({"templates": t2})["templates"]["images"] == [f"private/docs/dummy/templates/page-0{i}.png" for i in (1, 2)]
    assert dash.calls == [("templates", 0), ("templates", 1), ("templates", 2)]
    assert sorted(f.name for f in folder.glob("page-*.png")) == ["page-01.png", "page-02.png"]

    # the document removed: its pages go too
    run({})
    assert not folder.exists()


def test_an_older_dashboard_sends_the_pdf_whole_and_a_wide_page_keeps_its_long_side(synced, tmp_path):
    dash, run = synced
    docs = run({"visual": dash.serve("visual", made_up_pdf(1, size=(2400, 600)), old=True)})
    assert dash.calls == [("visual", 0)]
    im = Image.open(tmp_path / docs["visual"]["images"][0])
    assert im.width == S.DOC_PAGE_MAX_PX and im.height == 500


def test_a_document_that_cannot_be_drawn_still_syncs_its_text_and_says_why(synced, tmp_path, monkeypatch):
    dash, run = synced
    good = dash.serve("templates", made_up_pdf(2))
    run({"templates": good})
    folder = tmp_path / "private" / "docs" / "dummy" / "templates"
    assert len(list(folder.glob("page-*.png"))) == 2

    # the parts that came back are not the document (a cut download): no pages of any version listed
    bad = {**good, "version": 2, "sha256": "0" * 64}
    with pytest.raises(ValueError, match="do not add up"):
        run({"templates": bad})
    assert run({"templates": bad}, draw=False)["templates"]["images"] == []
    assert not folder.exists(), "the pages of version 1 are not shown as version 2"
    assert (tmp_path / "private" / "docs" / "dummy" / "templates.txt").read_text() == good["text"]
    assert not ops._DEGRADED, "the text synced; the job that needed the pages says why"

    # the package does not load on this machine: the templates job is handed back, the run amber
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    dash.calls.clear()
    run({"templates": {**good, "version": 3}}, draw=False)
    monkeypatch.setattr(S, "_dash", lambda: dash)
    with pytest.raises(RuntimeError, match="pages could not be drawn here .*ModuleNotFoundError.*run `studio jobs` again"):
        S.write_templates_job({"id": "j1", "kind": "templates", "page": "dummy"})
    assert ops._DEGRADED == ["machine fault: ModuleNotFoundError (reference page images)"]
    assert dash.calls == [], "nothing downloaded that could not be drawn"


def test_a_long_document_is_drawn_up_to_the_limit_and_the_brief_says_how_many_pages_it_has(synced, tmp_path, monkeypatch):
    dash, run = synced
    monkeypatch.setattr(S, "DOC_MAX_PAGES", 2)
    docs = run({"templates": dash.serve("templates", made_up_pdf(4))})
    assert len(docs["templates"]["images"]) == 2 and docs["templates"]["image_pages"] == 4


def test_an_ideas_or_compose_job_draws_the_templates_and_visual_pages_it_opens(synced, tmp_path, monkeypatch):
    """Sync draws nothing and Studio containers start fresh: the IDEAS and COMPOSE briefs lost the
    visual references and templates pictures STUDIO.md tells them to open."""
    import bore.routine as R
    dash, run = synced
    docs = {"templates": dash.serve("templates", made_up_pdf(2)), "visual": dash.serve("visual", made_up_pdf(1), parts=1)}
    run(docs, draw=False)
    monkeypatch.setattr(S, "WORK", tmp_path / "work")
    monkeypatch.setattr(S, "_dash", lambda: dash)
    monkeypatch.setattr(S, "_page_templates", lambda page: [])
    monkeypatch.setattr(R, "load_playbook", lambda page: {})
    job = {"id": "j1", "kind": "ideas", "page": "dummy", "batch_id": "b",
           "posts": [{"id": "p1", "story": {"headline": "H", "summary": "S.", "sources": []}}]}
    brief = json.loads((S.write_job(job, {}) / "p1" / "brief.json").read_text())
    assert len(brief["studio"]["docs"]["templates"]["images"]) == 2 and len(brief["studio"]["docs"]["visual"]["images"]) == 1
    assert all((tmp_path / f).exists() for t in ("templates", "visual") for f in brief["studio"]["docs"][t]["images"])

    # the pictures cannot be drawn on this machine: the job is still prepared (the text is there), the run not amber
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    run({t: {**d, "version": 2} for t, d in docs.items()}, draw=False)
    brief = json.loads((S.write_job({**job, "id": "j2", "kind": "compose"}, {}) / "p1" / "brief.json").read_text())
    assert brief["studio"]["docs"]["visual"]["images"] == [] and brief["studio"]["docs"]["visual"]["file"]
    assert not ops._DEGRADED


def test_the_brief_lists_the_page_images(monkeypatch, tmp_path):
    import bore.routine as R
    images = [f"private/docs/huddle/templates/page-0{i}.png" for i in (1, 2)]
    meta = {"huddle": {"name": "Huddle", "docs": {"templates": {"version": 4, "file": "private/docs/huddle/templates.txt",
                                                                "pages": 2, "images": images, "image_pages": 2}}}}
    monkeypatch.setattr(S, "WORK", tmp_path)
    monkeypatch.setattr(S, "studio_meta", lambda: meta)
    monkeypatch.setattr(S, "_page_templates", lambda page: [])
    monkeypatch.setattr(R, "load_playbook", lambda page: {})
    job = {"id": "j1", "kind": "ideas", "page": "huddle", "batch_id": "b",
           "posts": [{"id": "p1", "story": {"headline": "H", "summary": "S.", "sources": []}}]}
    brief = json.loads((S.write_job(job, {}) / "p1" / "brief.json").read_text())
    assert brief["studio"]["docs"]["templates"]["images"] == images
