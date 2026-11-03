"""Reference documents end to end: an admin uploads them in a real browser (dashboard/dev/server.mjs,
headless Chromium), then `studio sync` draws their pages from what the dashboard stored. On Oct 23
the editors' new templates document never arrived (the browser refused files over 3 MB with a toast
that vanished after 3 seconds; Vercel refuses request bodies over 4.5 MB) and nobody could tell.
Every document here is made up."""
import json
import re

import pytest
from PIL import Image

from bore import lessons, ops
from bore import studio as S
from bore.dashboard import Dashboard
from test_doc_pages import made_up_pdf
from test_studio_typing import PW, ROUTINE, browser, server  # noqa: F401  (the dev dashboard and Chromium)

pytest.importorskip("pypdfium2")
SLUG = "dummy-sports"          # dev/server.mjs's page
LIMIT = 4.5e6                  # Vercel's request body limit


def card(t):
    return f'[data-doc="{SLUG}/{t}"]'


@pytest.fixture(scope="module")
def admin(browser, server):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.route(re.compile(r"^https?://(?!localhost)"), lambda route: route.abort())
    page = ctx.new_page()
    page.clock.install()
    page.goto(server + "/#/login")
    page.fill("input[name=email]", "admin@example.com")
    page.fill("input[name=password]", PW)
    page.click("button.btn.primary")
    page.wait_for_selector("text=Pick up to 5 stories")
    page.goto(server + f"/#/admin/page/{SLUG}")
    page.wait_for_selector(card("templates"))
    # what the document's card said, in order (the progress lines go by quickly)
    page.evaluate("""() => { window.__notes = []; new MutationObserver(() => {
        for (const n of document.querySelectorAll('[data-docnote]')) { const t = n.textContent.replace(/\\s+/g, ' ').trim();
          if (t && window.__notes[window.__notes.length - 1] !== t) window.__notes.push(t); } })
      .observe(document.body, { subtree: true, childList: true, characterData: true }); }""")
    yield page
    ctx.close()


def upload(page, t, name, data):
    page.set_input_files(f"{card(t)} input[data-up]", files=[{"name": name, "mimeType": "application/pdf", "buffer": data}])


def note(page, t):
    return " ".join(page.locator(f"{card(t)} [data-docnote]").inner_text().split())


def wait_note(page, t, text, timeout=30000):
    page.locator(f"{card(t)} [data-docnote]", has_text=text).wait_for(timeout=timeout)
    return note(page, t)


def test_a_document_over_the_request_limit_goes_up_in_parts_and_the_routine_draws_its_pages(admin, server, tmp_path, monkeypatch):  # noqa: F811
    page = admin
    pdf = made_up_pdf(4, photo_px=1000)
    assert len(pdf) > LIMIT
    sent = []
    page.on("request", lambda r: sent.append(len(r.post_data_buffer or b"")) if "/docs/templates/part" in r.url else None)
    upload(page, "templates", "made-up-templates.pdf", pdf)
    done = wait_note(page, "templates", "is now active")
    mb = f"{len(pdf) / 1e6:.1f} MB"
    assert done.startswith(f"✓ made-up-templates.pdf is uploaded as v1 and is now active: 4 pages, {mb},")
    assert "characters of text read" in done
    assert len(sent) == 3 and all(0 < n < LIMIT for n in sent), sent
    seen = page.evaluate("window.__notes")
    assert any("part 2 of 3" in n for n in seen) and any("joining the parts and reading the text" in n for n in seen), seen
    page.clock.run_for(10000)
    assert note(page, "templates") == done, "the result stays on the card"
    row = " ".join(page.locator(f"{card('templates')} tr", has_text="made-up-templates.pdf").inner_text().split())
    assert row.startswith(f"v1 active made-up-templates.pdf {mb} 4 ") and "characters" in row and "almost no text" not in row

    # the routine's side: the parts come back whole, each page drawn tall enough to read
    monkeypatch.setattr(S, "ROOT", tmp_path)
    monkeypatch.setattr(S, "PRIVATE", tmp_path / "private")
    monkeypatch.setattr(lessons, "save", lambda slug, rec: None)
    monkeypatch.setattr(ops, "_DEGRADED", [])
    dash = Dashboard(server, ROUTINE)
    dash.s.trust_env = False                 # localhost, not through this machine's proxy
    S.sync(dash, quiet=True)
    docs = json.loads((tmp_path / "private" / "studio.json").read_text())[SLUG]["docs"]
    assert docs["templates"]["images"] == [], "sync downloads no document: the templates job draws its pages"
    S.doc_pages(SLUG, "templates", dash)
    docs = json.loads((tmp_path / "private" / "studio.json").read_text())[SLUG]["docs"]
    assert docs["templates"]["images"] == [f"private/docs/{SLUG}/templates/page-0{i}.png" for i in range(1, 5)]
    assert all(Image.open(tmp_path / f).height == S.DOC_PAGE_PX for f in docs["templates"]["images"])
    assert "MADE-UP CARD 1" in (tmp_path / docs["templates"]["file"]).read_text()
    assert not ops._DEGRADED


def test_a_document_of_pictures_is_flagged_and_says_what_to_do(admin):
    page = admin
    upload(page, "visual", "made-up-visual.pdf", made_up_pdf(3, text=False))
    warn = wait_note(page, "visual", "is now active")
    assert warn.startswith("⚠ made-up-visual.pdf is uploaded as v1 and is now active: 3 pages")
    assert "almost no text could be read from it (0 characters)" in warn and "export it again as a PDF with live text" in warn
    page.clock.run_for(10000)
    assert note(page, "visual") == warn
    assert page.locator(f"{card('visual')} .pill", has_text="almost no text").count() == 1


def test_an_upload_that_fails_says_why_and_what_to_do_until_closed(admin):
    page = admin
    upload(page, "tone", "notes.pdf", b"PK\x03\x04 a word file, not a PDF")
    err = wait_note(page, "tone", "was not uploaded")
    assert err.startswith("⚠ notes.pdf was not uploaded: that file is not a PDF: export the document as a PDF and upload that. "
                          "Nothing was changed.")
    page.clock.run_for(10000)
    assert note(page, "tone") == err, "an error stays on screen (the old toast went after 3 seconds)"
    page.click(f"{card('tone')} [data-docclose]")
    assert note(page, "tone") == ""


def test_a_dropped_connection_is_tried_again_and_a_lasting_one_is_reported(admin):
    page = admin
    drops = {"n": 0}

    def flaky(route):                       # the first two tries of the part are cut
        drops["n"] += 1
        route.abort() if drops["n"] <= 2 else route.continue_()
    page.route("**/docs/tone/part", flaky)
    upload(page, "tone", "made-up-tone.pdf", made_up_pdf(1))
    for _ in range(10):
        if "is now active" in note(page, "tone"):
            break
        page.clock.run_for(4000)
        page.wait_for_timeout(300)
    assert note(page, "tone").startswith("✓ made-up-tone.pdf is uploaded as v1"), note(page, "tone")
    assert drops["n"] == 3
    page.unroute("**/docs/tone/part")

    page.route("**/docs/visual/part", lambda route: route.abort())
    upload(page, "visual", "made-up-visual-2.pdf", made_up_pdf(1))
    for _ in range(10):
        if "was not uploaded" in note(page, "visual"):
            break
        page.clock.run_for(4000)
        page.wait_for_timeout(300)
    assert note(page, "visual").startswith("⚠ made-up-visual-2.pdf was not uploaded: sending part 1 of 1 failed 4 times (")
    assert note(page, "visual").endswith("check the internet connection, then upload the file again. "
                                         "Nothing was changed: v1 is still the active version. Close")
    page.unroute("**/docs/visual/part")
