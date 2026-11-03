"""The templates end to end, in a real browser against the dev dashboard (dashboard/dev/server.mjs,
headless Chromium): a templates document made active queues a conversion, the routine's own code
(`studio sync`, `studio jobs`, `routine tspec-render --sample`, `studio template-send`, `studio done`)
sends a template, the page's editor sees the preview under Templates (an admin also the document's
page beside it) and approves it, the next `studio sync` writes it where the renderer finds it, and a
rejection with a note queues it again. Every document and template here is made up."""
import base64
import json
import re
import threading
from pathlib import Path

import pytest

from bore import cli, lessons, ops, tspec
from bore import studio as S
from bore.dashboard import Dashboard
from test_doc_pages import made_up_pdf
from test_studio_typing import PW, ROUTINE, browser, server  # noqa: F401  (the dev dashboard and Chromium)

pytest.importorskip("pypdfium2")
SLUG = "dummy-sports"          # dev/server.mjs's page
SPEC = json.loads((Path(__file__).parent / "fixtures" / "tspec" / "tally-tower.json").read_text())


def signed_in(browser, server, email):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1280, "height": 1000})
    ctx.route(re.compile(r"^https?://(?!localhost)"), lambda route: route.abort())
    page = ctx.new_page()
    page.goto(server + "/#/login")
    page.fill("input[name=email]", email)
    page.fill("input[name=password]", PW)
    page.click("button.btn.primary")
    page.wait_for_selector("text=Pick up to 5 stories")
    return page


def text(page, sel):
    return " ".join(page.locator(sel).inner_text().split())


def loaded(page, sel):
    return page.eval_on_selector(sel, "img => img.complete && img.naturalWidth > 0")


def off_thread(fn, *args):
    """A step that draws (Playwright's sync API) while this test's browser runs: in a thread of its own."""
    out = {}

    def run():
        try:
            out["r"] = fn(*args)
        except BaseException as e:      # (SystemExit too: the CLI's gates)
            out["e"] = e
    t = threading.Thread(target=run)
    t.start()
    t.join(120)
    if "e" in out:
        raise out["e"]
    return out.get("r")


@pytest.fixture
def routine(server, tmp_path, monkeypatch):  # noqa: F811
    """The routine's machine: private/ and work/ in tmp_path, the dev dashboard as its dashboard."""
    import bore.routine as R
    for mod, k, v in ((S, "ROOT", tmp_path), (S, "PRIVATE", tmp_path / "private"), (S, "WORK", tmp_path / "work" / "studio"),
                      (R, "WORK", tmp_path / "work"), (tspec, "SPEC_DIR", tmp_path / "private" / "templates")):
        monkeypatch.setattr(mod, k, v)
    monkeypatch.setattr(lessons, "save", lambda slug, rec: None)
    monkeypatch.setattr(ops, "_DEGRADED", [])
    monkeypatch.setenv("BORE_ROUTINE", "manual")
    monkeypatch.chdir(tmp_path)
    dash = Dashboard(server, ROUTINE)
    dash.s.trust_env = False                 # localhost, not through this machine's proxy
    monkeypatch.setattr(S, "_dash", lambda: dash)
    # this machine's packages are checked elsewhere (tests/test_deps_machine.py); the browser self-test
    # cannot start a second Playwright inside this test's
    monkeypatch.setattr("bore.deps.problems", lambda names=None: {})
    return dash


def test_an_editor_approves_a_converted_template_and_rejects_it_with_a_note(browser, server, routine, tmp_path):  # noqa: F811
    admin = signed_in(browser, server, "admin@example.com")
    pdf = base64.b64encode(made_up_pdf(2)).decode()
    up = admin.evaluate("""async ([slug, b64]) => (await fetch(`/api/admin/pages/${slug}/docs/templates`, { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ filename: 'made-up-templates.pdf', data_b64: b64 }) })).json()""",
                        [SLUG, pdf])
    assert up["conversion"]["job"]

    # the Studio routine: the job is claimed and draws the document's pages, one template is converted and sent
    S.sync(routine, quiet=True)
    [jd] = S.pull_jobs({}, dash=routine)
    brief = json.loads((jd / "brief.json").read_text())
    assert brief["kind"] == "templates" and len(brief["doc"]["images"]) == 2
    d = jd / "tally-tower"
    d.mkdir()
    (d / "spec.json").write_text(json.dumps(SPEC))
    (d / "source.json").write_text('{"pages": [2]}')
    off_thread(cli.main, ["routine", "tspec-render", str(d / "spec.json"), "--sample", "--page", SLUG, "--out", str(d / "preview.png")])
    S.finish_job(jd)

    # the page's editor: the preview, waiting for approval (the document's own page is an admin's)
    ed = signed_in(browser, server, "editor@example.com")
    ed.click("nav >> text=Templates")
    card = '[data-tpl="tally-tower"]'
    ed.wait_for_selector(card)
    head = text(ed, f"{card} .row >> nth=0")
    assert "Tally Tower" in head and "waiting for approval" in head and "not in use" in head
    assert "From templates document v1, page 2" in text(ed, card)
    ed.wait_for_function("() => [...document.querySelectorAll('[data-tpl] img')].every(i => i.complete && i.naturalWidth > 0)")
    assert loaded(ed, f"{card} img[alt=preview]") and ed.locator(f'{card} img[alt="document page 2"]').count() == 0
    assert "Convert templates again" not in text(ed, "#main"), "only an admin converts the whole document again"
    ed.click(f"{card} [data-approve]")
    ed.wait_for_selector(f"{card} .pill:has-text('in use: version 1')")
    assert "approved by editor@example.com" in text(ed, card)
    assert ed.locator(f"{card} [data-retire]").count() == 1, "taken out of use without converting it again"

    # the routines draw it from their next sync: where lint and the renderer find it
    S.sync(routine, quiet=True)
    assert json.loads((tmp_path / "private" / "templates" / SLUG / "tally-tower.json").read_text()) == SPEC
    assert [t["id"] for t in S.approved_templates(SLUG)] == ["tally-tower"]

    # something is wrong after all: out of use, converted again with the note
    ed.click(f"{card} [data-reject]")
    assert "not in use" not in text(ed, card), "nothing happens without a note"
    ed.fill(f"{card} [data-note]", "The plates belong on the right, under the logo.")
    asked = []
    ed.once("dialog", lambda d: (asked.append(d.message), d.accept()))
    ed.click(f"{card} [data-reject]")
    ed.wait_for_selector(f"{card} .job.err")
    assert asked and "stops being used now" in asked[0], "the one in use: it says so before it stops"
    assert text(ed, f"{card} .job.err") == "Rejected: The plates belong on the right, under the logo."
    assert "not in use" in text(ed, card)
    assert "waiting for the Studio routine to start" in text(ed, "#main .panel")
    S.sync(routine, quiet=True)
    assert S.approved_templates(SLUG) == [] and not (tmp_path / "private" / "templates" / SLUG / "tally-tower.json").exists()
    [again] = S.pull_jobs({}, dash=routine)
    redo = json.loads((again / "brief.json").read_text())
    assert (redo["convert"], redo["notes"]) == (["tally-tower"], {"tally-tower": "The plates belong on the right, under the logo."})

    # an admin's Templates tab: the same screen, and the button to convert everything again
    admin.goto(server + "/#/admin/templates")
    admin.wait_for_selector(card)
    assert admin.locator("#tpl-convert").is_disabled(), "a conversion is under way"
    admin.wait_for_function("() => [...document.querySelectorAll('[data-tpl] img')].every(i => i.complete && i.naturalWidth > 0)")
    assert loaded(admin, f'{card} img[alt="document page 2"]'), "an admin compares it with the document's page"
    ed.context.close()
    admin.context.close()


THUMB = "() => { const i = document.querySelector('%s .tplthumb'); return !!i && i.complete && i.naturalWidth > 0; }"


def test_the_ideas_and_visual_steps_show_the_chosen_templates_preview_without_fetching_it_at_every_redraw(browser, server):  # noqa: F811
    """The idea cards' thumbnails were no-store, so every click, regenerate and poll fetched and
    decrypted them again (and they flickered); the Visual step showed none."""
    # no ctx.route here: Playwright turns the browser's cache off while it intercepts requests. Other
    # hosts (the dummy feed's photos) go to a proxy that answers nothing.
    ctx = browser.new_context(viewport={"width": 1280, "height": 1000}, proxy={"server": "http://127.0.0.1:9", "bypass": "localhost"})
    page = ctx.new_page()
    page.goto(server + "/#/login")
    page.fill("input[name=email]", "editor@example.com")
    page.fill("input[name=password]", PW)
    page.click("button.btn.primary")
    page.wait_for_selector("text=Pick up to 5 stories")
    call = lambda method, path, body=None, routine=False: page.evaluate("""async ([method, path, body, auth]) => {
        const r = await fetch(path, { method, body: body === null ? undefined : JSON.stringify(body),
          headers: { 'content-type': 'application/json', ...(auth ? { authorization: 'Bearer ' + auth } : {}) } });
        return { status: r.status, body: await r.json().catch(() => null) }; }""", [method, path, body, ROUTINE if routine else None])
    pic = base64.b64encode((Path(__file__).parent.parent / "docs" / "samples" / "huddle-giants-qb-template.jpg").read_bytes()).decode()
    r = call("POST", "/api/routine/templates", {"page": SLUG, "job": "", "doc_version": 1, "doc_pages": [1],
                                               "spec": {**SPEC, "id": "t9", "name": "Made-up Thumb"}, "preview_b64": pic,
                                               "pages": [{"n": 1, "image_b64": pic}]}, True)
    assert r["status"] == 200, r
    assert call("POST", f"/api/templates/{SLUG}/t9/{r['body']['v']}/approve", {})["status"] == 200
    story = {"id": "th1", "headline": "A made-up story with a template idea", "summary": "S.", "score": 9, "published_at": 1,
             "url": "https://example.com/th1", "sources": [{"name": "Example News", "url": "https://example.com/th1"}],
             "ideas": [{"title": "Big number", "template": "Made-up Thumb", "template_spec": "t9", "description": "One big number."},
                       {"title": "Quote", "template": "quote", "layout": "quote", "description": "A quote."}],
             "images": [{"url": "https://img.example.com/th1.jpg", "source": "web"}]}
    assert call("POST", "/api/routine/feed", {"page": SLUG, "stories": [story]}, True)["status"] == 200
    bid = call("POST", "/api/studio/batches", {"page": SLUG, "stories": [{"id": "th1"}]})["body"]["batch"]["id"]
    fetched, cached = [], set()                              # what reached the dashboard, not the browser's cache
    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.on("Network.requestServedFromCache", lambda e: cached.add(e["requestId"]))
    cdp.on("Network.responseReceived", lambda e: "/api/templates/image" in e["response"]["url"] and not e["response"].get("fromDiskCache")
           and fetched.append(e["requestId"]))
    sent = lambda: [x for x in fetched if x not in cached]
    page.evaluate(f"location.hash = '#/studio/b/{bid}/ideas'")
    page.wait_for_function(THUMB % ".idea")
    assert len(sent()) == 1
    for c in ("2", "1"):                                     # each click draws the screen again
        page.click(f'.idea[data-choice="{c}"]')
        page.wait_for_selector(f'.idea.sel[data-choice="{c}"]')
    page.wait_for_function(THUMB % ".idea")
    page.wait_for_timeout(300)
    assert len(sent()) == 1, "fetched again at a redraw"
    page.click("#cont")                                      # the Visual step shows the chosen idea's template too
    page.wait_for_function(THUMB % ".idealine")
    ctx.close()
