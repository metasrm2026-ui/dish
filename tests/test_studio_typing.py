"""Post Studio in a real browser (dashboard/dev/server.mjs, headless Chromium): what an editor types
is never wiped by the page following a job. The page used to redraw every 10 s while a job or the
automatic check ran, which replaced the caption mid-word, threw away regenerate notes and moved the
focus to the page (Oct 2026). Every field of every step is typed into across a poll that brings a
change, and the text, the focus and the saved caption must all survive."""
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

DASH = Path(__file__).resolve().parent.parent / "dashboard"
PW = "dev password 123"
ROUTINE = "dev-routine"          # dev/server.mjs's routine secret (never a real one)
TINY_JPEG = ("/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////"
             "////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=")
LONG = ("Jalen Hurts threw for 312 yards and three touchdowns as the Eagles beat the Cowboys 31-17 on Sunday night. "
        "The editor rewrites the second line while the visual is still being checked, word by word, "
        "and nothing they type may disappear when the page follows the job.")


@pytest.fixture(scope="module")
def server():
    if not shutil.which("node") or not (DASH / "node_modules" / "@vercel" / "blob").exists():
        pytest.skip("node and the dashboard's packages are needed (npm ci in dashboard/)")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # its own dev secrets: bore.config loads the real .env into os.environ, and the dev server would take it
    env = {k: v for k, v in os.environ.items() if k not in ("ROUTINE_SECRET_NEXT", "DATA_KEY", "SESSION_SECRET")}
    proc = subprocess.Popen(["node", "dev/server.mjs", str(port)], cwd=DASH, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env={**env, "ROUTINE_SECRET": ROUTINE})
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            line = proc.stdout.readline()
            if "dev server" in line:
                break
            if not line and proc.poll() is not None:
                pytest.fail("dev server did not start")
        else:
            pytest.fail("dev server did not start in 30 s")
        yield f"http://localhost:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            from bore.cards import _chrome_path
            b = p.chromium.launch(**({"executable_path": _chrome_path()} if _chrome_path() else {}))
        except Exception as e:          # a machine without the browser: `python -m playwright install chromium`
            pytest.skip(f"no headless Chromium here: {e}")
        yield b
        b.close()


class Studio:
    """The editor's browser tab, and the API calls a routine session makes meanwhile."""

    def __init__(self, page, base):
        self.page, self.base = page, base

    def call(self, method, path, body=None, routine=False):
        r = self.page.evaluate("""async ([method, path, body, auth]) => {
            const r = await fetch(path, { method, credentials: 'same-origin', body: body === null ? undefined : JSON.stringify(body),
              headers: { 'content-type': 'application/json', ...(auth ? { authorization: 'Bearer ' + auth } : {}) } });
            return { status: r.status, body: await r.json().catch(() => null) };
        }""", [method, path, body, ROUTINE if routine else None])
        assert 200 <= r["status"] < 300, (method, path, r)
        return r["body"]

    def post(self, pid):
        return next(p for p in self.call("GET", f"/api/studio/batches/{self.bid}")["batch"]["posts"] if p["id"] == pid)

    def job(self, kind, pid, **result):
        """A job for the post, claimed by a session: the page follows it."""
        j = self.call("POST", f"/api/studio/batches/{self.bid}/run", {"kind": kind, "post_ids": [pid]})["job"]
        assert j, f"{kind} job for {pid} not queued"
        self.call("POST", f"/api/routine/jobs/{j}", {"status": "running"}, routine=True)
        return lambda: self.call("POST", f"/api/routine/jobs/{j}", {"status": "done", "posts": {pid: result}}, routine=True)

    def focused(self, selector):
        return self.page.evaluate("s => !!document.activeElement && document.activeElement.matches(s)", selector)

    def poll(self):
        """The page's 10-second poll comes (and its fetch answers)."""
        self.page.clock.run_for(11000)
        self.page.wait_for_timeout(700)


def signed_in(browser, server, email, health=None):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.route(re.compile(r"^https?://(?!localhost)"), lambda route: route.abort())   # the dummy feed's photos
    if health is not None:           # the health status (group 2's route), as it would answer
        ctx.route("**/api/health", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(health)))
    page = ctx.new_page()
    page.clock.install()
    st = Studio(page, server)
    page.goto(server + "/#/login")
    page.fill("input[name=email]", email)
    page.fill("input[name=password]", PW)
    page.click("button.btn.primary")
    page.wait_for_selector("text=Pick up to 5 stories")
    return ctx, page, st


def test_what_the_editor_types_survives_the_page_following_a_job(server, browser):
    ctx, page, st = signed_in(browser, server, "editor@example.com")

    # a batch of two: A (idea and photo set) and B (with its visual and caption, being checked automatically)
    slug = st.call("GET", "/api/studio/pages")["pages"][0]["slug"]
    bt = st.call("POST", "/api/studio/batches", {"page": slug, "stories": [{"id": "s1"}, {"id": "s2"}]})["batch"]
    st.bid = bt["id"]
    A, B = (p["id"] for p in bt["posts"])
    for p in bt["posts"]:
        st.call("PATCH", f"/api/studio/batches/{st.bid}/posts/{p['id']}",
                {"idea": {"choice": 1}, "base_image_url": p["story"]["images"][0]["url"]})
    st.job("compose", B, image_b64=TINY_JPEG, caption="Routine caption.", hashtags=["#Eagles"])()
    card = lambda pid: f'.card[data-p="{pid}"]'

    def across_a_poll(step, selector, first, rest, result):
        """Type `first`, a job on A finishes and the poll brings it, type `rest`: nothing is lost."""
        done = st.job("images", A, **result)
        page.evaluate(f"""h => {{ if (location.hash === h) window.dispatchEvent(new HashChangeEvent('hashchange'));
                                  else location.hash = h; }}""".replace("h => ", "h => ", 1), f"#/studio/b/{st.bid}/{step}")
        field = page.locator(selector)
        field.wait_for()
        if step != "visual":      # (the image search box starts with the story's subject: typed after it)
            field.fill("")
        field.click()
        page.keyboard.press("End")
        field.press_sequentially(first, delay=2)
        assert "Searching images" in page.locator(card(A)).inner_text()
        done()
        st.poll()
        # the job line changed in place, the field and the focus did not
        assert "Searching images" not in page.locator(card(A)).inner_text(), f"{step}: job line not updated"
        assert st.focused(selector), f"{step}: focus moved"
        field.press_sequentially(rest, delay=2)
        return field

    # 1. Ideas: My idea
    f = across_a_poll("ideas", f"{card(A)} [data-choice=own] textarea", "A ranked list of ", "the top five passers",
                      {"images": [{"url": "https://img.example.com/i1.jpg", "source": "web"}]})
    assert f.input_value() == "A ranked list of the top five passers"

    # 2. Visual: the regenerate note (never saved until it is sent) and the image search
    f = across_a_poll("visual", f"{card(B)} textarea[data-note]", "Make the headline ", "bigger and the photo brighter",
                      {"images": [{"url": "https://img.example.com/i2.jpg", "source": "web"}]})
    assert f.input_value() == "Make the headline bigger and the photo brighter"
    page.click("h3")                                  # they leave the field: the step is drawn again
    st.poll()
    assert page.locator(f'{card(A)} .cand[data-url="https://img.example.com/i2.jpg"]').count() == 1, "redrawn once they left"
    assert page.locator(f"{card(B)} textarea[data-note]").input_value() == "Make the headline bigger and the photo brighter"
    f = across_a_poll("visual", f"{card(A)} input[name=q]", " QB", " Philadelphia",
                      {"images": [{"url": "https://img.example.com/i3.jpg", "source": "web"}]})
    assert f.input_value().endswith(" QB Philadelphia")

    # 3. Caption: a long edit, typed across the poll, is saved whole as they type
    half = len(LONG) // 2
    f = across_a_poll("caption", f"{card(B)} textarea[data-cap]", LONG[:half], LONG[half:],
                      {"images": [{"url": "https://img.example.com/i4.jpg", "source": "web"}]})
    assert f.input_value() == LONG
    page.clock.run_for(1500)
    page.wait_for_timeout(700)
    assert st.post(B)["caption"] == LONG, "saved while typing, and not cut"
    f = across_a_poll("caption", f"{card(B)} input[data-tags]", "#Eagles #Hu", "rts",
                      {"images": [{"url": "https://img.example.com/i5.jpg", "source": "web"}]})
    page.click("h3")
    page.clock.run_for(1500)
    page.wait_for_timeout(700)
    assert st.post(B)["hashtags"] == ["#Eagles", "#Hurts"]

    # 4. Post: the page follows A while SocialPilot schedules it; the editor edits B's caption and time
    st.job("compose", A, image_b64=TINY_JPEG, caption="Caption A.")()
    st.call("POST", f"/api/studio/batches/{st.bid}/publish", {"post_id": A, "mode": "now"})
    draft = st.post(A)["publish"]["draft_id"]
    page.evaluate(f"location.hash = '#/studio/b/{st.bid}/post'")
    cap, at = f"{card(B)} textarea[data-cap]", f"{card(B)} input[data-at]"
    page.locator(at).wait_for()
    assert "Sending to SocialPilot" in page.locator(card(A)).inner_text()
    page.locator(at).fill("2030-01-02T10:30")
    page.click(cap)
    page.keyboard.press("End")
    page.locator(cap).press_sequentially(" One more", delay=2)
    st.call("POST", "/api/result", {"id": draft, "status": "publishing"}, routine=True)
    page.clock.run_for(21000)
    page.wait_for_timeout(700)
    assert st.focused(cap), "post: focus moved"
    page.locator(cap).press_sequentially(" line.", delay=2)
    assert page.locator(cap).input_value() == LONG + " One more line."
    page.clock.run_for(1500)
    page.wait_for_timeout(700)
    assert st.post(B)["caption"] == LONG + " One more line."
    page.click("h3")                                  # left the field: drawn again, with what they typed
    page.clock.run_for(2500)
    page.wait_for_timeout(700)
    assert "Sending to SocialPilot" in page.locator(card(A)).inner_text()
    assert page.locator(at).input_value() == "2030-01-02T10:30", "the time they picked is kept"
    assert page.locator(cap).input_value() == LONG + " One more line."

    # Make visual waits while the post's visual is being made (one click, one job); A was sent, so
    # it offers none
    page.evaluate(f"location.hash = '#/studio/b/{st.bid}/visual'")
    page.locator(f"{card(B)} [data-make]").wait_for()
    assert page.locator(f"{card(A)} [data-make]").is_disabled()
    page.click(f"{card(B)} [data-make]")
    # (the click disables it at once; the label changes when the server's answer is drawn)
    page.wait_for_function(f"document.querySelector('{card(B)} [data-make]').textContent === 'Making the visual…'")
    assert page.locator(f"{card(B)} [data-make]").is_disabled()
    assert st.post(B)["job"]["kind"] == "compose"
    ctx.close()


# the page's own requests, slowed on its clock: window.__slow[<part of the path>] = milliseconds
SLOW_FETCH = """() => { window.__slow = {}; const real = window.fetch;
  window.fetch = async (u, o) => { const k = Object.keys(window.__slow).find(k => String(u).includes(k));
    if (k) await new Promise(r => setTimeout(r, window.__slow[k])); return real(u, o); }; }"""


def test_the_page_never_draws_over_an_action_nor_floods_the_dashboard(server, browser):
    ctx, page, st = signed_in(browser, server, "editor@example.com")
    slug = st.call("GET", "/api/studio/pages")["pages"][0]["slug"]
    bt = st.call("POST", "/api/studio/batches", {"page": slug, "stories": [{"id": "s3"}, {"id": "s4"}]})["batch"]
    st.bid = bt["id"]
    A, B = (p["id"] for p in bt["posts"])
    for p in bt["posts"]:
        st.call("PATCH", f"/api/studio/batches/{st.bid}/posts/{p['id']}",
                {"idea": {"choice": 1}, "base_image_url": p["story"]["images"][0]["url"]})
    st.job("compose", B, image_b64=TINY_JPEG, caption="Routine caption B.")()
    card = lambda pid: f'.card[data-p="{pid}"]'
    gets, health = {"n": 0, "fail": False}, {"status": "green", "reasons": [], "checked_at": 0}

    def batch_get(route):            # the page's reads of the batch: counted, refused when storage is
        if route.request.method != "GET":
            return route.continue_()
        gets["n"] += 1
        if gets["fail"]:
            return route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "storage is not answering"}))
        route.continue_()
    page.route(re.compile(rf"/api/studio/batches/{re.escape(st.bid)}$"), batch_get)
    ctx.route("**/api/health", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(health)))

    def run_clock(seconds):
        """Time passes on the page, its requests answered as it goes."""
        for _ in range(int(seconds // 2)):
            page.clock.run_for(2000)
            page.wait_for_timeout(60)

    # 1. an action's answer comes while the editor types in another post's field: only the lines change
    page.evaluate(f"location.hash = '#/studio/b/{st.bid}/visual'")
    note = f"{card(B)} textarea[data-note]"
    page.locator(note).wait_for()
    page.evaluate(SLOW_FETCH)
    page.evaluate("window.__slow['/run'] = 1500")
    page.click(f"{card(A)} [data-make]")
    page.click(note)
    page.locator(note).press_sequentially("Make the headline bigger", delay=2)
    page.clock.run_for(2000)                          # the slow request goes out, and its answer comes
    page.wait_for_function(f"document.querySelector('{card(A)}').innerText.includes('waiting for the Studio routine')")
    assert st.focused(note), "the action's answer drew the step over the editor"
    page.locator(note).press_sequentially(" and brighter", delay=2)
    assert page.locator(note).input_value() == "Make the headline bigger and brighter"
    page.evaluate("delete window.__slow['/run']")

    # 2. an image search while A's visual is being made: the visual is still followed and waited for
    compose = st.post(A)["job"]["id"]
    st.call("POST", f"/api/routine/jobs/{compose}", {"status": "running"}, routine=True)
    search = st.job("images", A, images=[{"url": "https://img.example.com/i9.jpg", "source": "web"}])
    st.poll()
    page.click("h3")
    st.poll()
    assert "Searching images" in page.locator(card(A)).inner_text()
    assert "Making the visual and caption" in page.locator(card(A)).inner_text()
    mk = page.locator(f"{card(A)} [data-make]")
    assert mk.is_disabled() and mk.inner_text() == "Making the visual…", "Make visual waits for the visual behind the search"

    # 3. everything finishes while the cursor stays in a field: the page stops asking for the batch
    page.click(note)
    search()
    st.call("POST", f"/api/routine/jobs/{compose}", {"status": "done", "posts": {A: {"image_b64": TINY_JPEG, "caption": "Routine caption A."}}},
            routine=True)
    st.call("POST", f"/api/studio/batches/{st.bid}/skip-check", {})
    run_clock(12)
    page.wait_for_function(f"!document.querySelector('{card(A)}').innerText.includes('Making the visual')")
    assert st.focused(note)
    gets["n"] = 0
    run_clock(120)
    assert gets["n"] == 0, f"{gets['n']} reads of the batch while nothing was under way"

    # 4. the Post step after Send, the editor in the next caption, storage refusing reads: asked again
    # at most once per poll, never in a loop
    page.click("h3")
    page.evaluate(f"location.hash = '#/studio/b/{st.bid}/post'")
    cap = f"{card(B)} textarea[data-cap]"
    page.locator(cap).wait_for()
    page.evaluate("window.__slow['schedule-hint'] = 1500")
    page.click(f"{card(A)} [data-send]")
    page.wait_for_function("document.querySelector('#toast').textContent.startsWith('Sent')")
    page.click(cap)
    gets["fail"], gets["n"] = True, 0
    run_clock(120)
    assert 1 <= gets["n"] <= 120 // 10 + 1, f"{gets['n']} reads of the batch in 2 minutes"
    assert st.focused(cap)

    # 5. a tab that follows a post for minutes shows the pipeline's health as it changes
    health.update(status="red", reasons=[{"code": "publisher_stale", "level": "red", "text": "The publisher has not run for 3 hours.",
                                          "since": 0}])
    gets["fail"] = False
    page.evaluate("delete window.__slow['schedule-hint']")
    page.click("h3")
    run_clock(90)
    assert "The publisher has not run for 3 hours." in page.locator("#health").inner_text()
    ctx.close()


def test_health_banner_caption_offer_and_posted_lately_note(server, browser):
    now = int(time.time() * 1000)
    health = {"status": "red", "checked_at": now, "reasons": [
        {"code": "hourly_stale", "level": "red", "text": "No hourly post for Dummy Sports in 3h.", "since": now - 3 * 3600e3, "ids": []},
        {"code": "studio_queued", "level": "amber", "text": "A Studio job has waited 12 min.", "since": now - 12 * 60e3, "ids": ["job-1"]}]}
    ctx, page, st = signed_in(browser, server, "admin@example.com", health=health)
    # every signed-in page shows what is not working; admins see every reason in the Health tab
    banner = page.locator("#health")
    banner.wait_for()
    page.wait_for_function("document.querySelector('#health').innerText.includes('No hourly post')")
    assert "Part of the pipeline is not working" in banner.inner_text()
    assert "A Studio job has waited 12 min." in banner.inner_text()
    page.click("#health a")
    page.wait_for_selector("text=Pipeline health")
    rows = page.locator("#main table tr")
    assert rows.count() == 3 and "job-1" in rows.nth(2).inner_text() and "studio_queued" in rows.nth(2).inner_text()

    # a story the page already posted about (its words in the hourly draft), in a batch
    slug = st.call("GET", "/api/studio/pages")["pages"][0]["slug"]
    st.call("POST", "/api/routine/feed", {"page": slug, "stories": [{
        "id": "j1", "headline": "Josh Jacobs cleared to return for the Packers on Monday", "summary": "He is back.", "score": 9,
        "published_at": now, "url": "https://example.com/j1", "sources": [{"name": "Example News", "url": "https://example.com/j1"}],
        "ideas": [{"title": "Stat card", "template": "stat_card", "description": "x"}, {"title": "Quote", "template": "quote", "description": "y"}],
        "images": [{"url": "https://img.example.com/j1.jpg", "source": "web"}]}]}, routine=True)
    stamp = time.strftime("%Y%m%d%H%M", time.gmtime(time.time() - 5 * 3600))
    hourly = f"{slug}-{stamp}-exempt-jacob-josh-monday"
    st.call("POST", "/api/drafts", {"id": hourly, "page": slug, "page_name": "Dummy Sports", "headline": "JACOBS CLEARED",
                                    "caption": "Josh Jacobs is off the exempt list on Monday.", "image_b64": TINY_JPEG,
                                    "brief": {"headline_hint": "Jacobs removed from the Commissioner Exempt List on Monday"}},
            routine=True)
    st.call("POST", "/api/decide", {"id": hourly, "decision": "approve"})
    bt = st.call("POST", "/api/studio/batches", {"page": slug, "stories": [{"id": "j1"}]})["batch"]
    st.bid, P = bt["id"], bt["posts"][0]["id"]
    st.call("PATCH", f"/api/studio/batches/{st.bid}/posts/{P}", {"idea": {"choice": 1}, "base_image_url": "https://img.example.com/j1.jpg"})
    st.job("compose", P, image_b64=TINY_JPEG, caption="Routine caption.", hashtags=["#Packers"])()
    # the editor rewrites the caption, then has the visual made again: theirs stays, the new one is offered
    st.call("PATCH", f"/api/studio/batches/{st.bid}/posts/{P}", {"caption": "The editor's own words."})
    st.job("compose", P, image_b64=TINY_JPEG, caption="A fresh routine caption.", hashtags=["#GoPack"])()
    page.evaluate(f"location.hash = '#/studio/b/{st.bid}/caption'")
    page.locator("[data-use]").wait_for()
    assert page.locator("textarea[data-cap]").input_value() == "The editor's own words."
    assert "A fresh routine caption." in page.locator(".card").inner_text()
    page.click("[data-use]")
    page.wait_for_function("document.querySelector('textarea[data-cap]').value === 'A fresh routine caption.'")
    assert page.locator("[data-use]").count() == 0
    assert page.locator("input[data-tags]").input_value() == "#GoPack"
    # the Post step: a note that the page sent a similar story, and Send still works
    page.evaluate(f"location.hash = '#/studio/b/{st.bid}/post'")
    page.locator("[data-seen]").wait_for()
    assert "This page sent a similar story" in page.locator("[data-seen]").inner_text()
    assert "Commissioner Exempt List" in page.locator("[data-seen]").inner_text()
    page.click("[data-send]")
    page.wait_for_function("document.querySelector('#toast').textContent.startsWith('Sent: scheduled for')")
    assert st.post(P)["publish"]["state"] == "approved"
    # once sent, the Caption step shows it read-only (typing there could only be refused)
    page.evaluate(f"location.hash = '#/studio/b/{st.bid}/caption'")
    page.locator("text=it can no longer be changed here").wait_for()
    assert page.locator("textarea[data-cap]").get_attribute("readonly") is not None
    ctx.close()


def test_search_by_enter_stalled_visual_cleared_batch_post_now_and_retry(server, browser):
    ctx, page, st = signed_in(browser, server, "editor@example.com")
    slug = st.call("GET", "/api/studio/pages")["pages"][0]["slug"]
    now = int(time.time() * 1000)
    st.call("POST", "/api/routine/feed", {"page": slug, "stories": [{
        "id": f"e{i}", "headline": f"Story {i} for the editor", "summary": "S.", "score": 5, "published_at": now,
        "url": f"https://example.com/e{i}", "sources": [{"name": "Example News", "url": f"https://example.com/e{i}"}],
        "ideas": [{"title": "Stat card", "template": "stat_card", "description": "x"}],
        "images": [{"url": f"https://img.example.com/e{i}0.jpg", "source": "web"}]} for i in (1, 2)]}, routine=True)
    bt = st.call("POST", "/api/studio/batches", {"page": slug, "stories": [{"id": "e1"}, {"id": "e2"}]})["batch"]
    st.bid = bt["id"]
    A, B = (p["id"] for p in bt["posts"])
    for p in bt["posts"]:
        st.call("PATCH", f"/api/studio/batches/{st.bid}/posts/{p['id']}",
                {"idea": {"choice": 1}, "base_image_url": p["story"]["images"][0]["url"]})
    card = lambda pid: f'.card[data-p="{pid}"]'
    toast = lambda: page.locator("#toast").text_content()

    def show(step):
        """The step drawn afresh (its cards are new ones)."""
        page.evaluate("""h => { document.querySelectorAll('.card').forEach(c => { c.dataset.old = '1'; });
                                if (location.hash === h) window.dispatchEvent(new HashChangeEvent('hashchange'));
                                else location.hash = h; }""", f"#/studio/b/{st.bid}/{step}")
        page.locator(".card:not([data-old])").first.wait_for()

    def finish(pid, **result):
        """The routine session takes the post's newest job and finishes it."""
        j = st.post(pid)["job"]["id"]
        st.call("POST", f"/api/routine/jobs/{j}", {"status": "running"}, routine=True)
        st.call("POST", f"/api/routine/jobs/{j}", {"status": "done", "posts": {pid: result}}, routine=True)

    # 1. an image search sent with Enter: the photos it finds appear, with no click elsewhere
    show("visual")
    q = f"{card(A)} input[name=q]"
    page.locator(q).fill("Jalen Hurts")
    page.locator(q).press("Enter")
    page.wait_for_function("document.querySelector('#toast').textContent.includes('Studio routine')")
    finish(A, images=[{"url": "https://img.example.com/e1.jpg", "source": "web"}, {"url": "https://img.example.com/e2.jpg", "source": "web"}])
    st.poll()
    assert page.locator(f'{card(A)} .cand[data-url="https://img.example.com/e2.jpg"]').count() == 1, "the photos found are shown"
    assert page.locator(q).input_value() == "Jalen Hurts"

    # 2. a visual whose session stopped answering (the server marks it stale): Make visual is not held
    made_a = st.job("compose", A, image_b64=TINY_JPEG, caption="Caption A.")

    def stale(route):
        if route.request.method != "GET":
            return route.continue_()
        r = route.fetch()
        body = r.json()
        for p in body["batch"]["posts"]:
            if p["id"] == A and p["job"]["kind"] == "compose":
                p["job"]["stale"] = True
        route.fulfill(response=r, body=json.dumps(body))
    batch_url = re.compile(rf"/api/studio/batches/{re.escape(st.bid)}$")
    page.route(batch_url, stale)
    show("visual")
    mk = page.locator(f"{card(A)} [data-make]")
    assert mk.is_enabled() and mk.inner_text() == "Start again"
    assert "stopped answering" in page.locator(card(A)).inner_text()
    page.unroute(batch_url)
    show("visual")
    assert mk.is_disabled() and mk.inner_text() == "Making the visual…", "a session has it again: wait for it"

    # 3. a colleague clears the batch while the editor is in it: the page says who did (on its next
    # poll, or at once when a step is refused instead of being queued only to vanish)
    actx, apage, admin = signed_in(browser, server, "admin@example.com")
    restore = lambda: (page.click("[data-restore]"),
                       page.wait_for_function("document.querySelector('#toast').textContent.startsWith('Restored')"))
    warned = []                                       # (the Studio home warns that it is someone else's)
    apage.on("dialog", lambda d: (warned.append(d.message), d.accept()))
    apage.locator(f'[data-bid="{st.bid}"]').check()
    apage.click("#b-clear")
    apage.wait_for_function("document.querySelector('#toast').textContent === 'Cleared 1'")
    assert "It was started by editor@example.com, who may still be working on it" in warned[0], warned
    st.poll()
    assert "admin@example.com cleared this batch" in page.locator("#main").inner_text()
    # its editor also finds it on the Studio home (for a day: they may not have been on its page),
    # with who cleared it, and restores it there
    page.evaluate("location.hash = '#/studio'")
    row = page.locator(f'tr:has([data-restore-bid="{st.bid}"])')
    row.wait_for()
    assert "admin@example.com cleared it" in row.inner_text()
    row.locator("[data-restore-bid]").click()
    page.wait_for_function("document.querySelector('#toast').textContent === 'Restored'")
    show("visual")
    assert page.locator("[data-restore]").count() == 0
    st.call("POST", f"/api/studio/batches/{st.bid}/run", {"kind": "images", "post_ids": [B], "feedback": "Search for photos of: anyone"})
    assert admin.call("POST", "/api/studio/batches/clear", {"ids": [st.bid]})["cleared"] == 1
    actx.close()
    page.click(f"{card(B)} [data-make]")
    page.wait_for_function("document.querySelector('#toast').textContent.includes('cleared this batch')")
    assert page.locator("[data-restore]").count() == 1, "the refused step brings the note"
    st.poll()
    assert "Searching images was stopped when the batch was cleared" in page.locator(card(B)).inner_text()
    restore()
    assert page.locator("[data-restore]").count() == 0
    page.click(f"{card(B)} [data-make]")
    page.wait_for_function("document.querySelector('#toast').textContent.includes('Studio routine')")
    finish(B, image_b64=TINY_JPEG, caption="Caption B.")
    made_a()

    # 4. "Post now" says when it will really go out: the page's first free slot, from the server. A
    # label drawn before another post took that slot asks first, and shows the new time
    show("post")
    st.call("POST", f"/api/studio/batches/{st.bid}/publish", {"post_id": A, "mode": "now"})
    hint = st.call("GET", f"/api/studio/schedule-hint?page={slug}")
    assert hint["now_at"] >= st.post(A)["publish"]["requested_at"] + 45 * 60e3
    moved = []
    page.once("dialog", lambda d: (moved.append(d.message), d.dismiss()))
    page.check(f'{card(B)} input[value="now"]')
    page.click(f"{card(B)} [data-send]")
    page.wait_for_function(f"!document.querySelector('{card(B)} [data-send]').disabled")
    assert moved and moved[0].startswith("Post now goes out at"), moved
    assert st.post(B)["publish"] is None, "not sent: they did not agree to the later time"
    page.check(f'{card(B)} input[value="schedule"]')
    label = page.locator(f"{card(B)} [data-now]").inner_text()
    assert label == "(earliest free slot: %s)" % page.evaluate("ms => when(ms)", hint["now_at"]), label

    # 5. B's last try may be on Facebook and has a red note: Retry asks first, as the review page does
    st.call("POST", f"/api/studio/batches/{st.bid}/publish", {"post_id": B, "mode": "now"})
    draft = st.post(B)["publish"]["draft_id"]
    st.call("POST", "/api/result", {"id": draft, "status": "publishing"}, routine=True)
    st.call("POST", "/api/result", {"id": draft, "status": "failed", "error": "CreatePost: Request timed out"}, routine=True)
    st.call("POST", "/api/result", {"id": draft, "status": "needs_action", "error": "Delete the second copy in SocialPilot"}, routine=True)
    show("post")
    assert "It may already be on Facebook" in page.locator(card(B)).inner_text()
    asked, answers = [], []
    page.on("dialog", lambda d: (asked.append(d.message), d.accept() if answers.pop(0) else d.dismiss()))
    send = f"{card(B)} [data-send]"
    assert page.locator(send).inner_text() == "Retry"
    answers[:] = [False]                              # they have not looked yet: nothing is sent
    page.click(send)
    page.wait_for_function(f"!document.querySelector('{send}').disabled")
    assert len(asked) == 1 and "may already be on Facebook" in asked[0], asked
    assert "look in SocialPilot (Queue and Unscheduled)" in asked[0], "a copy made then waits there, not on the page yet"
    assert st.post(B)["publish"]["state"] == "failed"
    # they fix the caption too: the publisher looks for the earlier copy by its caption, so they are told
    page.fill(f"{card(B)} [data-cap]", "Caption B, fixed.")
    answers[:] = [True, True]                         # they looked, and deleted the copy
    page.click(send)
    page.wait_for_function("document.querySelector('#toast').textContent.startsWith('Sent: scheduled for')")
    assert "You changed the caption since" in asked[1] and "“Caption B.…”" in asked[1], asked
    assert "Delete the second copy in SocialPilot" in asked[2], asked
    assert st.post(B)["publish"]["state"] == "approved"

    # 6. A fails, and is retried on the review page while the Studio still shows Retry: the Studio's
    # Retry is refused (one post, one draft with the publisher) and the card shows where it is
    st.call("POST", "/api/result", {"id": draft, "status": "publishing"}, routine=True)
    st.call("POST", "/api/result", {"id": draft, "status": "scheduled", "scheduled_for": "2030-01-01T10:00:00Z"}, routine=True)
    a_draft = st.post(A)["publish"]["draft_id"]
    st.call("POST", "/api/result", {"id": a_draft, "status": "publishing"}, routine=True)
    st.call("POST", "/api/result", {"id": a_draft, "status": "failed", "error": "page has no SocialPilot account: set it in the admin panel, then retry"},
            routine=True)
    show("post")                                      # (nothing is being sent now: the step does not poll)
    assert page.locator(f"{card(A)} [data-send]").inner_text() == "Retry"
    assert st.call("POST", "/api/decide", {"ids": [a_draft], "decision": "retry"})["retried"] == 1
    page.click(f"{card(A)} [data-send]")
    page.wait_for_function("document.querySelector('#toast').textContent.startsWith('already sent')")
    page.wait_for_function(f"document.querySelector('{card(A)}').innerText.includes('Sending to SocialPilot')", timeout=5000)
    ctx.close()
