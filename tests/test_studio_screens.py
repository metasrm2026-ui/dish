"""Post Studio in a real browser (dashboard/dev/server.mjs, headless Chromium): what the editor sees on
each step is what is saved and sent (Oct 2026 audit). The Post step follows a visual being made again
and refuses one the editor never saw; a photo already chosen keeps its visual; a re-check keeps the
findings; a sent post offers nothing to change; slow or lost answers, a lost connection and a session
that ran out never draw the wrong screen, lose typed text or hide that a Send went through; phones
get no sideways scrolling; and a tab left open over a deploy says to reload."""
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
# the page's own requests, held or broken on the way: window.__net = [[part of the path, method, ms,
# how]], how: 'slow' (answered ms later), 'offline' (never sent), 'lost' (sent, the answer lost)
NET = """() => { window.__net = []; const real = window.fetch;
  window.fetch = async (u, o) => {
    const m = (o && o.method) || 'GET', r = window.__net.find(x => String(u).includes(x[0]) && (!x[1] || x[1] === m));
    if (!r) return real(u, o);
    if (r[3] === 'offline') throw new TypeError('Failed to fetch');
    if (r[3] === 'lost') { await real(u, o); throw new TypeError('Failed to fetch'); }
    const answer = await real(u, o); await new Promise(ok => setTimeout(ok, r[2])); return answer; }; }"""


def free_port():
    for port in range(4111, 4120):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    pytest.skip("no free port in 4111-4119")


@pytest.fixture(scope="module")
def server():
    if not shutil.which("node") or not (DASH / "node_modules" / "@vercel" / "blob").exists():
        pytest.skip("node and the dashboard's packages are needed (npm ci in dashboard/)")
    port = free_port()
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
        except Exception as e:
            pytest.skip(f"no headless Chromium here: {e}")
        yield b
        b.close()


class Tab:
    """One browser tab signed in, with the API calls a routine session makes meanwhile."""

    def __init__(self, browser, server, email="editor@example.com", width=1280):
        self.ctx = browser.new_context(viewport={"width": width, "height": 900})
        self.ctx.route(re.compile(r"^https?://(?!localhost)"), lambda route: route.abort())   # the dummy feed's photos
        self.page = self.ctx.new_page()
        self.page.add_init_script(f"({NET})()")
        self.page.on("dialog", lambda d: (self.dialogs.append(d.message), d.accept() if self.accept else d.dismiss()))
        self.dialogs, self.accept, self.server = [], False, server
        self.page.goto(server + "/#/login")
        self.sign_in(email)

    def sign_in(self, email):
        self.page.fill("input[name=email]", email)
        self.page.fill("input[name=password]", PW)
        self.page.click("button.btn.primary")
        self.page.wait_for_selector("text=Pick up to 5 stories")

    def call(self, method, path, body=None, routine=False, ok=True):
        r = self.page.evaluate("""async ([method, path, body, auth]) => {
            const r = await fetch(path, { method, credentials: 'same-origin', body: body === null ? undefined : JSON.stringify(body),
              headers: { 'content-type': 'application/json', ...(auth ? { authorization: 'Bearer ' + auth } : {}) } });
            return { status: r.status, body: await r.json().catch(() => null) };
        }""", [method, path, body, ROUTINE if routine else None])
        if ok:
            assert 200 <= r["status"] < 300, (method, path, r)
            return r["body"]
        return r

    def batch(self, stories=("s1",), made=False):
        slug = self.call("GET", "/api/studio/pages")["pages"][0]["slug"]
        bt = self.call("POST", "/api/studio/batches", {"page": slug, "stories": [{"id": s} for s in stories]})["batch"]
        self.bid = bt["id"]
        for p in bt["posts"]:
            self.patch(p["id"], {"idea": {"choice": 1}, "base_image_url": p["story"]["images"][0]["url"]})
            if made:
                self.job("compose", p["id"], image_b64=TINY_JPEG, caption=f"Routine caption {p['id']}.", hashtags=["#Dummy"])()
        return [p["id"] for p in bt["posts"]]

    def patch(self, pid, body):
        return self.call("PATCH", f"/api/studio/batches/{self.bid}/posts/{pid}", body)

    def post(self, pid):
        return next(p for p in self.call("GET", f"/api/studio/batches/{self.bid}")["batch"]["posts"] if p["id"] == pid)

    def job(self, kind, pid, feedback="", **result):
        """A job for the post, claimed by a session: returns how to finish it."""
        j = self.call("POST", f"/api/studio/batches/{self.bid}/run", {"kind": kind, "post_ids": [pid], "feedback": feedback})["job"]
        assert j, f"{kind} job for {pid} not queued"
        self.call("POST", f"/api/routine/jobs/{j}", {"status": "running"}, routine=True)
        return lambda status="done", **more: self.call("POST", f"/api/routine/jobs/{j}", {"status": status, "posts": {pid: {**result, **more}}}, routine=True)

    def open(self, step, wait=".card"):
        """The step drawn afresh (what was on the screen before is marked, and waited past)."""
        self.page.evaluate("() => document.querySelectorAll('#main *').forEach(e => { e.dataset.stale = '1'; })")
        self.page.evaluate(f"""h => {{ if (location.hash === h) window.dispatchEvent(new HashChangeEvent('hashchange'));
                                       else location.hash = h; }}""", f"#/studio/b/{self.bid}/{step}")
        self.page.wait_for_function("s => { const e = document.querySelector(`#main ${s}`); return !!e && !e.dataset.stale; }", arg=wait)

    def card(self, pid):
        return f'.card[data-p="{pid}"]'

    def text(self, sel):
        return self.page.locator(sel).inner_text()

    def until(self, js, timeout=15000):
        self.page.wait_for_function(js, timeout=timeout)


def test_the_post_step_follows_a_visual_made_again_and_never_sends_one_unseen(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("post")
    shown = t.page.locator(f"{t.card(A)} .preview img").get_attribute("src")
    # the editor asked for a new visual on the Visual step, then came here while it is made
    finish = t.job("compose", A, feedback="bigger headline", image_b64=TINY_JPEG, caption="NEW caption written for card v3", hashtags=["#New"])
    t.open("post")
    assert "Making the visual and caption" in t.text(t.card(A)), "the Post step says a visual is being made"
    assert t.page.locator(f"{t.card(A)} [data-send]").is_disabled(), "Send waits for it"
    finish()
    # the step follows the job (every 10 s): the new card and its caption are drawn
    t.until(f"document.querySelector('{t.card(A)} .preview img').src !== {json.dumps(shown)}", timeout=25000)
    assert "NEW caption written for card v3" in t.text(f"{t.card(A)} .preview .txt")
    assert t.page.locator(f"{t.card(A)} [data-cap]").input_value() == "NEW caption written for card v3"
    assert not t.page.locator(f"{t.card(A)} [data-send]").is_disabled()

    # a visual made again behind the card's back (the page drawn before it, not redrawn yet): refused
    seen = t.page.locator(f"{t.card(A)} .preview img").get_attribute("src")
    t.page.evaluate("() => { clearTimeout(S.poll); S.follow = null; }")       # (no poll comes before Send)
    t.job("compose", A, image_b64=TINY_JPEG, caption="Caption for card v4")()
    t.page.click(f"{t.card(A)} [data-send]")
    t.until("document.querySelector('#toast').textContent.includes('visual changed since this page was drawn')")
    assert t.post(A)["publish"] is None, "nothing sent"
    t.until(f"document.querySelector('{t.card(A)} .preview img').src !== {json.dumps(seen)}")
    assert "Caption for card v4" in t.text(f"{t.card(A)} .preview .txt"), "drawn again as it is now"
    t.page.click(f"{t.card(A)} [data-send]")
    t.until(f"document.querySelector('{t.card(A)}').innerText.includes('Sending to SocialPilot')")
    assert t.post(A)["publish"]["state"] == "approved"
    t.ctx.close()


def test_send_in_an_older_tab_never_replaces_a_caption_fixed_in_another(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("post")
    # another tab fixes the caption (its autosave); this one still shows the routine's
    t.patch(A, {"caption": "Fixed in tab A: the deal is $102M."})
    t.page.click(f"{t.card(A)} [data-send]")
    t.until("document.querySelector('#toast').textContent.includes('caption was changed elsewhere')")
    p = t.post(A)
    assert (p["caption"], p["publish"]) == ("Fixed in tab A: the deal is $102M.", None)
    t.until(f"document.querySelector('{t.card(A)} [data-cap]').value.startsWith('Fixed in tab A')")
    # the preview follows what the editor types
    t.page.locator(f"{t.card(A)} [data-cap]").fill("Fixed in tab A: the deal is $102M. Four years.")
    assert "Four years." in t.text(f"{t.card(A)} .preview .txt")
    t.page.click(f"{t.card(A)} [data-send]")
    t.until(f"document.querySelector('{t.card(A)}').innerText.includes('Sending to SocialPilot')")
    assert t.post(A)["caption"] == "Fixed in tab A: the deal is $102M. Four years."
    t.ctx.close()


def test_the_photo_already_chosen_keeps_its_visual_and_another_asks_first(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.job("images", A, images=[{"url": "https://img.example.com/other.jpg", "source": "web"}])()
    before = t.post(A)
    t.open("visual")
    t.page.click(f"{t.card(A)} .cand.sel")
    t.page.wait_for_timeout(600)
    assert t.dialogs == []
    assert t.post(A)["final_image_url"] == before["final_image_url"]
    # another photo: the page asks first; saying no changes nothing
    t.page.click(f'{t.card(A)} .cand[data-url="https://img.example.com/other.jpg"]')
    t.page.wait_for_timeout(600)
    assert t.dialogs and "replaces the finished visual" in t.dialogs[-1]
    assert t.post(A)["final_image_url"] == before["final_image_url"]
    assert t.page.locator(f"{t.card(A)} .cand.sel").get_attribute("data-url") == before["base_image"]["url"]
    t.ctx.close()


def test_two_quick_photo_taps_answered_out_of_order_show_and_save_the_same_photo(server, browser):
    t = Tab(browser, server)
    A, = t.batch()
    t.job("images", A, images=[{"url": "https://img.example.com/b.jpg", "source": "web"}, {"url": "https://img.example.com/c.jpg", "source": "web"}])()
    t.open("visual")
    t.page.evaluate("pid => { window.__net = [[`/posts/${pid}`, 'PATCH', 3000, 'slow']]; }", A)
    t.page.click(f'{t.card(A)} .cand[data-url="https://img.example.com/b.jpg"]')
    assert t.page.locator(f"{t.card(A)} .cand.sel").get_attribute("data-url") == "https://img.example.com/b.jpg", "shown at once"
    t.page.click(f'{t.card(A)} .cand[data-url="https://img.example.com/c.jpg"]')     # (while the first is on its way: ignored)
    t.page.wait_for_timeout(3800)
    assert t.page.locator(f"{t.card(A)} .cand.sel").get_attribute("data-url") == t.post(A)["base_image"]["url"] == "https://img.example.com/b.jpg"
    t.ctx.close()


def test_rerun_check_keeps_the_findings_on_screen(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.job("check", A, check={"factual": ["The deal is $102M, not $120M"]})()
    t.open("check")
    t.page.click(f"{t.card(A)} [data-one]")
    t.until(f"document.querySelector('{t.card(A)}').innerText.includes('waiting for the Studio routine')")
    card = t.text(t.card(A))
    assert "Previous check" in card and "The deal is $102M, not $120M" in card
    t.ctx.close()


def test_a_sent_post_offers_no_new_visual_search_or_check(server, browser):
    t = Tab(browser, server)
    A, B = t.batch(stories=("s1", "s2"), made=True)
    t.call("POST", f"/api/studio/batches/{t.bid}/publish", {"post_id": A, "mode": "now"})
    t.open("visual")
    c = t.card(A)
    assert "Sent to SocialPilot: it can no longer be changed here" in t.text(c)
    for sel in ("[data-make]", "[data-search]", "input[name=q]", "[data-file]"):
        assert t.page.locator(f"{c} {sel}").is_disabled(), sel
    assert t.page.locator(f"{c} textarea[data-note]").count() == 0
    t.open("check")
    assert t.page.locator(f"{c} [data-one]").count() == 0
    # Run check checks the others only
    t.job("check", B, check={"factual": []})()
    t.open("check")
    t.page.click("#runall")
    t.until(f"document.querySelector('{t.card(B)}').innerText.includes('waiting for the Studio routine')")
    assert t.post(A)["check"]["status"] != "queued"
    t.ctx.close()


def test_add_link_twice_or_a_pick_from_another_window_is_seen_once(server, browser):
    t = Tab(browser, server)
    url = server + "/review.html"                                      # (a page the dev server serves itself)
    t.page.evaluate("() => { window.__net = [['/api/studio/url', 'POST', 1500, 'slow']]; }")
    t.page.fill("#urlf input[name=url]", url)
    t.page.click("#urlf button")
    assert t.page.locator("#urlf button").inner_text() == "Reading…"
    t.page.locator("#urlf button").click(force=True)
    t.page.wait_for_function("document.querySelector('#urlf button').textContent === 'Add link'")
    t.page.fill("#urlf input[name=url]", url)
    t.page.click("#urlf button")
    t.page.wait_for_timeout(300)
    assert t.text("#count") == "1 of 5 selected"
    assert t.page.locator("#list .story").filter(has_text="pasted link").count() == 1
    # a story picked in the 24-hour window stays listed (ticked) in the 3-hour one
    t.page.click("[data-h='24']")
    t.page.wait_for_selector("#list .story")
    old = t.page.locator("#list .story").last
    head = old.locator(".h").inner_text()
    old.locator("input").check()
    t.page.click("[data-h='3']")
    t.page.wait_for_selector("#list .story")
    assert t.page.locator("#list .story.sel").filter(has_text=head).count() == 1
    assert t.text("#count") == "2 of 5 selected"
    t.ctx.close()


def test_job_failures_read_as_plain_words_and_cleared_batches_say_restore_first(server, browser):
    t = Tab(browser, server)
    A, B = t.batch(stories=("s1", "s2"))
    j = t.call("POST", f"/api/studio/batches/{t.bid}/run", {"kind": "compose", "post_ids": [A]})["job"]
    for n in range(3):
        t.call("POST", f"/api/routine/jobs/{j}", {"status": "running"}, routine=True)
        t.call("POST", f"/api/routine/jobs/{j}", {"status": "queued", "release": True, "error": "KeyError: 'base_image'"}, routine=True)
    t.open("visual")
    line = t.text(f"{t.card(A)} [data-line]")
    assert "could not be prepared" in line and "KeyError" not in line
    admin = Tab(browser, server, "admin@example.com")
    admin.bid = t.bid
    admin.open("visual")
    assert "For the admin: KeyError: 'base_image'" in admin.text(f"{admin.card(A)} [data-line]")
    # a colleague clears the editor's batch while a visual waits
    t.call("POST", f"/api/studio/batches/{t.bid}/run", {"kind": "compose", "post_ids": [B]})
    admin.call("POST", "/api/studio/batches/clear", {"ids": [t.bid]})
    t.open("visual")
    assert "restore the batch (above), then press the button again" in t.text(f"{t.card(B)} [data-line]")
    assert "no longer listed for your colleagues (your Studio home shows it for a day)" in t.text("#main")
    admin.ctx.close()
    t.ctx.close()


def test_a_caption_too_long_is_not_saved_and_says_so(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("caption")
    cap = f"{t.card(A)} [data-cap]"
    t.page.locator(cap).fill("Paragraph. " * 600)
    t.page.click("h3")
    t.until(f"!!document.querySelector('{t.card(A)} [data-unsaved]')")
    assert "at most 5,000 characters" in t.text(f"{t.card(A)} [data-unsaved]")
    assert "shorten it" in t.text(f"{t.card(A)} [data-count]")
    assert t.post(A)["caption"] == f"Routine caption {A}."
    t.ctx.close()


def test_a_save_that_fails_is_marked_and_tried_again_until_it_lands(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("caption")
    t.page.evaluate("pid => { window.__net = [[`/posts/${pid}`, 'PATCH', 0, 'offline']]; }", A)
    t.page.locator(f"{t.card(A)} [data-cap]").fill("Typed on the train.")
    t.page.click("h3")
    t.until(f"!!document.querySelector('{t.card(A)} [data-unsaved]')")
    assert "Not saved yet" in t.text(f"{t.card(A)} [data-unsaved]")
    assert t.page.evaluate("() => unsaved()"), "leaving the page asks first"
    t.page.evaluate("() => { window.__net = []; window.dispatchEvent(new Event('online')); }")
    t.until(f"!document.querySelector('{t.card(A)} [data-unsaved]')")
    assert t.post(A)["caption"] == "Typed on the train."
    t.ctx.close()


def test_a_step_that_cannot_load_keeps_the_step_bar_and_tries_again(server, browser):
    t = Tab(browser, server)
    t.batch(made=True)
    t.open("caption")
    t.page.evaluate("bid => { window.__net = [[`/api/studio/batches/${bid}`, 'GET', 0, 'offline']]; }", t.bid)
    t.page.click("text=Continue to check")
    t.page.wait_for_selector("#tryagain")
    assert "No connection" in t.text("#main") and "Failed to fetch" not in t.text("#main")
    assert t.page.locator(".steps a").count() == 5
    t.page.evaluate("() => { window.__net = []; }")
    t.page.click("#tryagain")
    t.page.wait_for_selector("text=Continue to posting")
    t.ctx.close()


def test_a_slow_answer_for_a_screen_the_editor_left_is_not_drawn(server, browser):
    t = Tab(browser, server)
    t.batch(made=True)
    t.open("ideas")
    # 2. Visual answers in 2.5 s; the editor goes back to Ideas meanwhile (answered at once)
    t.page.evaluate("bid => { window.__net = [[`/api/studio/batches/${bid}`, 'GET', 2500, 'slow']]; }", t.bid)
    t.page.click(".steps a:nth-child(2)")
    t.page.wait_for_timeout(200)
    t.page.evaluate("() => { window.__net = []; }")
    t.page.go_back()
    t.page.wait_for_timeout(3500)
    assert t.page.locator(".steps a.on").inner_text() == "1. Ideas"
    # the story list answering after the editor went back to a step
    t.page.evaluate("() => { window.__net = [['/api/studio/feed', 'GET', 2500, 'slow']]; }")
    t.page.click(".nav a[href='#/studio']")
    t.page.wait_for_timeout(200)
    t.page.go_back()
    t.page.wait_for_timeout(3500)
    assert t.page.locator(".steps a.on").inner_text() == "1. Ideas"
    assert "Cannot set properties" not in t.text("#main")
    t.ctx.close()


def test_a_late_answer_from_another_batch_never_takes_over(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    first = t.bid
    t.open("caption")
    t.page.evaluate("pid => { window.__net = [[`/posts/${pid}`, 'PATCH', 3000, 'slow']]; }", A)
    t.page.locator(f"{t.card(A)} [data-cap]").fill("Edited in batch A.")
    t.page.click("h3")
    B, = t.batch(stories=("s2",))
    t.job("images", B, images=[{"url": "https://img.example.com/b2.jpg", "source": "web"}])()
    t.open("visual")
    t.page.wait_for_timeout(3500)                                      # (A's save answers now)
    assert t.page.evaluate("() => S.batch.id") == t.bid
    t.page.click(f'{t.card(B)} .cand[data-url="https://img.example.com/b2.jpg"]')
    t.page.wait_for_timeout(800)
    assert t.post(B)["base_image"]["url"] == "https://img.example.com/b2.jpg"
    t.bid = first
    assert t.post(A)["caption"] == "Edited in batch A."
    t.ctx.close()


def test_a_send_whose_answer_was_lost_finds_out_it_was_sent(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("post")
    t.page.evaluate("() => { window.__net = [['/publish', 'POST', 0, 'lost']]; }")
    t.page.click(f"{t.card(A)} [data-send]")
    t.until(f"document.querySelector('{t.card(A)}').innerText.includes('Sending to SocialPilot')")
    assert t.page.locator(f"{t.card(A)} [data-send]").count() == 0
    t.ctx.close()


def test_signed_out_while_typing_the_editor_is_brought_back_with_their_text_saved(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("caption")
    t.ctx.clear_cookies()                                              # (the session ran out)
    t.page.locator(f"{t.card(A)} [data-cap]").fill("Written before the session ran out.")
    t.page.click("h3")
    t.page.wait_for_selector("input[name=email]")
    t.sign_in_to_step = None
    t.page.fill("input[name=email]", "editor@example.com")
    t.page.fill("input[name=password]", PW)
    t.page.click("button.btn.primary")
    t.page.wait_for_selector(f"{t.card(A)} [data-cap]")
    assert t.page.evaluate("() => location.hash") == f"#/studio/b/{t.bid}/caption"
    t.until(f"document.querySelector('{t.card(A)} [data-cap]').value === 'Written before the session ran out.'")
    deadline = time.time() + 15
    while time.time() < deadline and t.post(A)["caption"] != "Written before the session ran out.":
        t.page.wait_for_timeout(500)
    assert t.post(A)["caption"] == "Written before the session ran out."
    t.ctx.close()


def test_my_idea_and_one_tap_on_continue(server, browser):
    t = Tab(browser, server)
    slug = t.call("GET", "/api/studio/pages")["pages"][0]["slug"]
    t.bid = t.call("POST", "/api/studio/batches", {"page": slug, "stories": [{"id": "s1"}]})["batch"]["id"]
    t.open("ideas")
    t.page.locator("[data-choice=own] textarea").fill("A ranked list of the top five passers")
    t.page.click("#cont")
    t.page.wait_for_selector("text=Continue to captions")
    assert t.page.evaluate("() => location.hash").endswith("/visual")
    t.ctx.close()


def test_withdrawn_on_the_review_page_the_post_step_says_so(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.call("POST", f"/api/studio/batches/{t.bid}/publish", {"post_id": A, "mode": "now"})
    t.call("POST", "/api/decide", {"id": t.post(A)["publish"]["draft_id"], "decision": "withdraw"})
    t.open("post")
    assert "Withdrawn by editor@example.com on the review page, so it was not posted. Send posts it again." in t.text(t.card(A))
    t.ctx.close()


def test_a_tab_open_over_a_deploy_says_to_reload(server, browser):
    t = Tab(browser, server)
    commit = {"v": "aaaaaaa"}
    t.page.route("**/version.json*", lambda route: route.fulfill(status=200, content_type="application/json",
                                                                 body=json.dumps({"commit": commit["v"], "api": 1})))
    t.page.evaluate("() => { S.versionAt = 0; route(); }")
    t.page.wait_for_timeout(500)
    assert t.page.locator("#newversion").count() == 0
    commit["v"] = "bbbbbbb"                                           # deployed meanwhile
    t.page.evaluate("() => { S.versionAt = 0; route(); }")
    t.page.wait_for_selector("#newversion")
    assert "A new version is available: reload" in t.text("#newversion")
    t.ctx.close()


@pytest.mark.parametrize("width", [360, 375])
def test_phone_screens_never_scroll_sideways(server, browser, width):
    t = Tab(browser, server, width=width)
    A, = t.batch(made=True)
    long_url = "https://www.example.com/sports/basketball/2026/10/22/lakers-celtics-overtime-thriller-recap-player-ratings"
    t.patch(A, {"caption": f"Read more: {long_url}", "hashtags": "#LakersCelticsOvertimeThrillerRecapPlayerRatings"})
    t.job("check", A, check={"factual": [f"See {long_url}"]})()
    t.job("images", A, images=[{"url": "https://img.example.com/w.jpg", "source": "web", "flags": ["violent incident: do not use without checking"]}])()
    t.page.evaluate("() => { if (location.hash === '#/studio') route(); else location.hash = '#/studio'; }")
    t.page.wait_for_selector("table.batches")
    wide = "() => document.documentElement.scrollWidth"
    assert t.page.evaluate(wide) <= width, "Studio home"
    for step in ("check", "post", "visual"):
        t.open(step)
        assert t.page.evaluate(wide) <= width, step
    # nothing cut off in the preview, the photo warnings readable whole
    t.open("post")
    assert t.page.evaluate(f"() => {{ const e = document.querySelector('{t.card(A)} .preview .txt'); return e.scrollWidth <= e.clientWidth; }}")
    t.open("visual")
    assert t.page.evaluate("() => [...document.querySelectorAll('.cand .src.warn')].every(e => e.scrollWidth <= e.clientWidth)")
    # the hint in each box fits it
    fits = """sel => { const el = document.querySelector(sel), c = document.createElement('canvas').getContext('2d');
      const st = getComputedStyle(el); c.font = `${st.fontSize} ${st.fontFamily}`;
      return c.measureText(el.placeholder).width <= el.clientWidth - parseFloat(st.paddingLeft) - parseFloat(st.paddingRight); }"""
    assert t.page.evaluate(fits, f"{t.card(A)} input[name=q]")
    t.page.evaluate("() => location.hash = '#/studio'")
    t.page.wait_for_selector("#urlf")
    assert t.page.evaluate(fits, "#urlf input[name=url]")
    t.ctx.close()


def until_server(t, pid, test, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline and not test(t.post(pid)):
        t.page.wait_for_timeout(250)
    return test(t.post(pid))


def test_typing_over_a_caption_a_colleague_fixed_shows_both_and_saves_neither_unseen(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("post")
    cap = f"{t.card(A)} [data-cap]"
    t.patch(A, {"caption": "Fixed by colleague: the deal is $102M."})   # (on another device)
    t.page.locator(cap).focus()
    t.page.keyboard.press("Control+End")
    t.page.keyboard.type(" Wow.")
    t.until(f"!!document.querySelector('{t.card(A)} [data-unsaved] [data-theirs]')")
    assert "Fixed by colleague: the deal is $102M." in t.text(f"{t.card(A)} [data-unsaved]")
    assert t.page.locator(cap).input_value() == f"Routine caption {A}. Wow.", "the editor's text stays in the box"
    assert t.post(A)["caption"] == "Fixed by colleague: the deal is $102M."
    t.page.click(f"{t.card(A)} [data-send]")
    t.until("document.querySelector('#toast').textContent.includes('choose yours or theirs')")
    assert t.post(A)["publish"] is None, "nothing sent"
    t.page.click(f"{t.card(A)} [data-theirs]")
    assert t.page.locator(cap).input_value() == "Fixed by colleague: the deal is $102M."
    assert "Fixed by colleague" in t.text(f"{t.card(A)} .preview .txt")
    t.page.click(f"{t.card(A)} [data-send]")
    t.until(f"document.querySelector('{t.card(A)}').innerText.includes('Sending to SocialPilot')")
    assert t.post(A)["caption"] == "Fixed by colleague: the deal is $102M."

    # on the Caption step, Keep mine saves the editor's text once they chose it
    B, = t.batch(stories=("s2",), made=True)
    t.open("caption")
    cap = f"{t.card(B)} [data-cap]"
    t.patch(B, {"caption": "Second fix."})
    t.page.locator(cap).focus()
    t.page.keyboard.press("Control+End")
    t.page.keyboard.type(" Mine.")
    t.until(f"!!document.querySelector('{t.card(B)} [data-unsaved] [data-mine]')")
    assert t.post(B)["caption"] == "Second fix."
    t.page.click(f"{t.card(B)} [data-mine]")
    assert until_server(t, B, lambda p: p["caption"] == f"Routine caption {B}. Mine.")
    t.until(f"!document.querySelector('{t.card(B)} [data-unsaved]')")
    t.ctx.close()


def test_a_poll_answered_from_before_a_photo_tap_never_undoes_it(server, browser):
    t = Tab(browser, server)
    A, = t.batch()
    t.job("images", A, images=[{"url": "https://img.example.com/b.jpg", "source": "web"}])()
    t.job("images", A, feedback="Search for photos of: the coach")        # (under way: the step polls)
    t.open("visual")
    t.page.evaluate("pid => { window.__net = [[`/posts/${pid}`, 'PATCH', 2500, 'slow']]; }", A)
    t.page.click(f'{t.card(A)} .cand[data-url="https://img.example.com/b.jpg"]')
    t.page.evaluate("() => tick()")                                    # a poll, answered before the tap is saved
    assert t.page.locator(f"{t.card(A)} [data-make]").is_disabled(), "Make visual waits for the photo"
    t.page.wait_for_timeout(3500)
    assert t.post(A)["base_image"]["url"] == "https://img.example.com/b.jpg"
    assert t.page.locator(f"{t.card(A)} .cand.sel").get_attribute("data-url") == "https://img.example.com/b.jpg"
    assert t.page.evaluate("pid => S.batch.posts.find(p => p.id === pid).base_image.url", A) == "https://img.example.com/b.jpg"
    assert not t.page.locator(f"{t.card(A)} [data-make]").is_disabled()
    t.ctx.close()


def test_an_upload_cancelled_can_be_chosen_again(server, browser, tmp_path):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("visual")
    photo = tmp_path / "p3.jpg"
    photo.write_bytes(__import__("base64").b64decode(TINY_JPEG))
    t.page.set_input_files(f"{t.card(A)} [data-file]", str(photo))   # (Cancel: t.accept is False)
    t.page.wait_for_timeout(600)
    assert len(t.dialogs) == 1 and t.post(A)["base_image"]["source"] == "web"
    t.accept = True
    t.page.set_input_files(f"{t.card(A)} [data-file]", str(photo))
    assert until_server(t, A, lambda p: p["base_image"]["source"] == "upload")
    assert len(t.dialogs) == 2
    t.ctx.close()


def test_a_save_waiting_for_sign_in_lands_as_soon_as_they_sign_in(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("caption")
    t.ctx.clear_cookies()                                              # (the session ran out)
    t.page.locator(f"{t.card(A)} [data-cap]").fill("Typed while signed out.")
    t.page.click("h3")
    t.page.wait_for_selector("input[name=email]")
    t.page.wait_for_timeout(9000)                                      # (the tries meanwhile wait longer and longer)
    t.page.fill("input[name=email]", "editor@example.com")
    t.page.fill("input[name=password]", PW)
    t.page.click("button.btn.primary")
    t.page.wait_for_selector(f"{t.card(A)} [data-cap]")
    assert until_server(t, A, lambda p: p["caption"] == "Typed while signed out.", seconds=3)
    t.ctx.close()


def test_a_check_waiting_again_says_the_caption_was_edited_since_the_one_shown(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.job("check", A, check={"factual": ["The deal is $102M, not $120M"]})()
    t.call("POST", f"/api/studio/batches/{t.bid}/run", {"kind": "check", "post_ids": [A]})
    t.patch(A, {"caption": "Fixed: the deal is $102M"})
    t.open("check")
    card = t.text(t.card(A))
    assert "Previous check" in card and "Edited since that check." in card
    t.ctx.close()


TOAST = "document.querySelector('#toast').textContent"


def test_signed_out_mid_word_the_sign_in_screen_says_why_and_keeps_the_way_back(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("caption")
    t.ctx.clear_cookies()                                              # (the session ran out)
    # no click elsewhere: the save goes a moment after the last key, and again as the field goes
    t.page.locator(f"{t.card(A)} [data-cap]").fill("Typed as the session ran out.")
    t.page.wait_for_selector("input[name=email]")
    t.page.wait_for_timeout(1500)
    assert t.page.evaluate("() => location.hash") == "#/login?next=" + f"/studio/b/{t.bid}/caption".replace("/", "%2F") + "&why=idle"
    assert "Signed out after a break" in t.text("#main [data-why]")
    assert "Signed out after a break: sign in to carry on (your text is kept)" in t.page.evaluate(f"() => {TOAST}")
    t.page.fill("input[name=email]", "editor@example.com")
    t.page.fill("input[name=password]", PW)
    t.page.click("button.btn.primary")
    t.page.wait_for_selector(f"{t.card(A)} [data-cap]")
    assert t.page.evaluate("() => location.hash") == f"#/studio/b/{t.bid}/caption"
    assert until_server(t, A, lambda p: p["caption"] == "Typed as the session ran out.")
    t.ctx.close()


def test_a_new_visual_landing_while_the_editor_types_says_it_brought_its_own_caption(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    finish = t.job("compose", A, feedback="bigger headline", image_b64=TINY_JPEG, caption="Regenerated caption", hashtags=["#Dummy"])
    t.open("caption")
    cap = f"{t.card(A)} [data-cap]"
    t.page.locator(cap).focus()
    t.page.keyboard.press("Control+End")
    t.page.keyboard.type(" Mine.")
    finish()                                                           # (lands before the save goes)
    t.until(f"!!document.querySelector('{t.card(A)} [data-unsaved] [data-theirs]')")
    note = t.text(f"{t.card(A)} [data-unsaved]")
    assert "the new visual came with its own caption while you typed" in note and "Regenerated caption" in note
    assert "another window" not in note and "colleague" not in note
    assert t.page.locator(cap).input_value() == f"Routine caption {A}. Mine."
    t.ctx.close()


def test_send_waits_for_a_visual_and_says_where_to_make_it(server, browser):
    t = Tab(browser, server)
    A, B = t.batch(stories=("s1", "s2"))
    t.job("compose", A, image_b64=TINY_JPEG, caption="Routine caption.", hashtags=["#Dummy"])()
    t.open("post")
    assert t.page.locator(f"{t.card(B)} [data-send]").is_disabled()
    assert "No visual yet: make it in step 2 (Visual) first." in t.text(f"{t.card(B)} [data-novisual]")
    assert not t.page.locator(f"{t.card(A)} [data-send]").is_disabled()
    assert t.text(f"{t.card(A)} [data-novisual]") == ""
    t.page.evaluate("() => drawLines()")                               # (what a poll does)
    assert t.page.locator(f"{t.card(B)} [data-send]").is_disabled()
    t.ctx.close()


def test_a_send_with_no_connection_says_it_was_not_sent_once_it_knows(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("post")
    t.page.evaluate("() => { window.__net = [['/publish', 'POST', 0, 'offline']]; }")
    t.page.click(f"{t.card(A)} [data-send]")
    t.until(f"{TOAST}.includes('It was not sent: press Send again')")
    assert t.post(A)["publish"] is None
    assert not t.page.locator(f"{t.card(A)} [data-send]").is_disabled()
    t.ctx.close()


def test_a_send_cut_off_by_the_platform_finds_out_it_was_sent(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("post")

    def cut_off(route):                                                # (it reached the dashboard; the platform's own page came back)
        route.fetch()
        route.fulfill(status=504, content_type="text/plain", body="An error occurred with your deployment\n\nFUNCTION_INVOCATION_TIMEOUT")
    t.page.route("**/publish", cut_off)
    t.page.click(f"{t.card(A)} [data-send]")
    t.until(f"document.querySelector('{t.card(A)}').innerText.includes('Sending to SocialPilot')")
    t.until(f"{TOAST}.includes('It was sent')")
    assert t.page.locator(f"{t.card(A)} [data-send]").count() == 0
    # the review page: plain words too
    t.page.goto(server + "/review.html")
    t.page.wait_for_load_state("networkidle")
    t.page.route("**/api/decide", lambda route: route.fulfill(status=504, content_type="text/plain", body="FUNCTION_INVOCATION_TIMEOUT"))
    msg = t.page.evaluate("() => api('/api/decide', { method: 'POST', body: '{}' }).then(() => 'ok', e => e.message)")
    assert "did not answer in time" in msg and "504" not in msg
    t.ctx.close()


def test_moving_to_another_step_takes_the_old_one_off_the_screen_at_once(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("caption")
    t.page.evaluate("bid => { window.__net = [[`/api/studio/batches/${bid}`, 'GET', 3000, 'slow']]; }", t.bid)
    t.page.click("text=Continue to check")
    t.page.wait_for_timeout(300)
    assert t.page.locator(".steps a.on").inner_text() == "4. Check"
    assert t.page.locator("#main [data-cap]").count() == 0 and "Loading" in t.text("#main")
    t.page.wait_for_selector("#skip")
    t.ctx.close()


def test_skip_check_pressed_twice_is_sent_once(server, browser):
    t = Tab(browser, server)
    t.batch(made=True)
    t.open("check")
    skips = []
    t.page.on("request", lambda r: "/skip-check" in r.url and skips.append(r.url))
    t.page.evaluate("() => { window.__net = [['/skip-check', 'POST', 2000, 'slow']]; }")
    t.page.click("#skip")
    assert t.page.locator("#skip").is_disabled()
    t.page.locator("#skip").click(force=True)
    t.page.wait_for_function("location.hash.endsWith('/post')")
    assert len(skips) == 1
    t.ctx.close()


def test_the_studio_home_says_when_unfinished_batches_could_not_load(server, browser):
    t = Tab(browser, server)
    t.batch(made=True)
    t.page.evaluate("() => { window.__net = [['/api/studio/batches?', 'GET', 0, 'offline']]; location.hash = '#/studio/topics'; }")
    t.page.wait_for_selector("#list .story")
    t.page.evaluate("() => { location.hash = '#/studio'; }")
    t.page.wait_for_selector("#main [data-failed]")
    assert "Could not load your unfinished batches right now: No connection" in t.text("#main [data-failed]")
    t.page.evaluate("() => { window.__net = []; }")
    t.page.click("#b-again")
    t.page.wait_for_selector("table.batches")
    t.ctx.close()


def test_a_post_sent_before_the_editors_change_was_saved_shows_what_was_sent_and_says_so(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("caption")
    cap = f"{t.card(A)} [data-cap]"
    t.page.evaluate("pid => { window.__net = [[`/posts/${pid}`, 'PATCH', 0, 'offline']]; }", A)
    t.page.locator(cap).fill(f"Routine caption {A}. (fixed a typo)")
    t.page.click("h3")
    t.until(f"!!document.querySelector('{t.card(A)} [data-unsaved]')")
    t.call("POST", f"/api/studio/batches/{t.bid}/publish", {"post_id": A, "mode": "now"})   # (a colleague's Send)
    t.page.evaluate("() => { window.__net = []; }")
    t.open("caption")
    assert t.page.locator(cap).input_value() == f"Routine caption {A}."
    assert "was not sent: the post had already been sent" in t.page.evaluate(f"() => {TOAST}")
    assert not t.page.evaluate("() => unsaved()"), "leaving the page does not ask"
    t.ctx.close()


def test_a_link_headline_never_makes_a_phone_scroll_sideways(server, browser):
    t = Tab(browser, server, width=360)
    # a link the dashboard cannot read: its headline is the link itself
    url = server + "/r/nba/comments/1g9xk2p/lebron_james_scores_40_points_in_overtime_win_over_the_celtics/"
    t.page.fill("#urlf input[name=url]", url)
    t.page.click("#urlf button")
    t.page.wait_for_function("document.querySelector('#urlf button').textContent === 'Add link'")
    wide = "() => document.documentElement.scrollWidth"
    assert t.page.locator("#list .story").filter(has_text="lebron_james").count() == 1
    assert t.page.evaluate(wide) <= 360, "Studio home"
    t.page.click("#cont")
    t.page.wait_for_selector("text=Continue to visuals")
    t.bid = t.page.evaluate("() => location.hash.split('/')[3]")
    assert t.page.evaluate(wide) <= 360, "ideas"
    A = t.call("GET", f"/api/studio/batches/{t.bid}")["batch"]["posts"][0]["id"]
    t.patch(A, {"idea": {"choice": "own", "text": "A stat card"}, "base_image_b64": TINY_JPEG, "base_image_type": "image/jpeg"})
    t.job("compose", A, image_b64=TINY_JPEG, caption="Routine caption.", hashtags=["#Dummy"])()
    for step in ("visual", "caption", "check", "post"):
        t.open(step)
        assert t.page.evaluate(wide) <= 360, step
    t.ctx.close()


def test_a_send_lost_while_its_draft_is_made_never_says_it_was_not_sent(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("post")
    faking = {"on": True}

    def mid_send(route):                                               # (the server still making the draft: 'sending')
        r = route.fetch()
        body = r.json()
        if faking["on"]:
            for p in body["batch"]["posts"]:
                if p["id"] == A:
                    p["publish"] = {"state": "sending", "sending_at": int(time.time() * 1000), "draft_id": "d1"}
        route.fulfill(response=r, json=body)
    t.page.route(re.compile(r"/api/studio/batches/[^/?]+$"), mid_send)
    t.page.route("**/publish", lambda route: route.abort())           # (the answer lost: a dropped connection)
    seen = []
    t.page.expose_function("seenToast", lambda m: seen.append(m))
    t.page.evaluate("() => new MutationObserver(() => seenToast(document.querySelector('#toast').textContent))"
                    ".observe(document.querySelector('#toast'), { childList: true, characterData: true, subtree: true })")
    t.page.click(f"{t.card(A)} [data-send]")
    t.until(f"{TOAST}.includes('It may still be on its way')")
    assert "Do not make this story again" in t.text(f"{t.card(A)} [data-lost]")
    t.page.unroute("**/publish")
    t.call("POST", f"/api/studio/batches/{t.bid}/publish", {"post_id": A, "mode": "now"})   # (the Send lands)
    faking["on"] = False
    t.until(f"{TOAST}.includes('It was sent')", timeout=20000)
    assert not any("It was not sent" in m for m in seen), seen
    t.until(f"document.querySelector('{t.card(A)}').innerText.includes('Sending to SocialPilot')")
    t.ctx.close()


def test_a_lost_send_says_on_the_card_that_it_was_not_sent_until_sent(server, browser):
    t = Tab(browser, server)
    A, = t.batch(made=True)
    t.open("post")
    t.page.evaluate("() => { window.__net = [['/publish', 'POST', 0, 'offline']]; }")
    t.page.click(f"{t.card(A)} [data-send]")
    t.until(f"!!document.querySelector('{t.card(A)} [data-lost]')")
    t.page.wait_for_timeout(4000)                                      # (the toast is long gone)
    assert "Not sent: the connection dropped. Press Send again." in t.text(f"{t.card(A)} [data-lost]")
    t.open("caption")
    t.open("post")
    assert "Not sent: the connection dropped" in t.text(f"{t.card(A)} [data-lost]"), "kept while the batch is open"
    t.page.evaluate("() => { window.__net = []; }")
    t.page.click(f"{t.card(A)} [data-send]")
    t.until(f"document.querySelector('{t.card(A)}').innerText.includes('Sending to SocialPilot')")
    assert t.page.locator("#main [data-lost]").count() == 0
    t.ctx.close()


def test_signed_out_a_buttons_own_message_says_why_too(server, browser):
    t = Tab(browser, server)
    t.batch(made=True)
    t.open("check")
    t.ctx.clear_cookies()
    t.page.click("#skip")
    t.page.wait_for_selector("input[name=email]")
    t.page.wait_for_timeout(300)
    assert "Signed out after a break: sign in to carry on" in t.page.evaluate(f"() => {TOAST}")
    assert "Please sign in" not in t.page.evaluate(f"() => {TOAST}")
    t.ctx.close()


def test_signed_out_on_the_review_page_the_sign_in_screen_says_why(server, browser):
    t = Tab(browser, server)
    t.page.goto(server + "/review.html")
    t.page.wait_for_load_state("networkidle")
    t.ctx.clear_cookies()
    t.page.evaluate("() => api('/api/decide', { method: 'POST', body: '{}' }).catch(() => {})")
    t.page.wait_for_selector("input[name=email]")
    assert "why=idle" in t.page.evaluate("() => location.hash")
    assert "Signed out after a break: sign in to carry on" in t.text("#main [data-why]")
    t.page.fill("input[name=email]", "editor@example.com")
    t.page.fill("input[name=password]", PW)
    t.page.click("button.btn.primary")
    t.page.wait_for_url("**/review.html")
    t.ctx.close()


def test_the_post_step_lets_send_go_once_a_visual_is_made_elsewhere(server, browser):
    t = Tab(browser, server)
    A, = t.batch()
    t.open("post")
    assert t.page.locator(f"{t.card(A)} [data-send]").is_disabled()
    assert "No visual yet" in t.text(f"{t.card(A)} [data-novisual]")
    t.job("compose", A, image_b64=TINY_JPEG, caption="Made in another tab.", hashtags=["#Dummy"])()
    t.until(f"!document.querySelector('{t.card(A)} [data-send]').disabled", timeout=25000)
    assert t.text(f"{t.card(A)} [data-novisual]") == ""
    assert t.page.locator(f"{t.card(A)} .preview img").count() == 1
    t.ctx.close()


def test_the_studio_home_says_plainly_when_storage_does_not_answer(server, browser):
    t = Tab(browser, server)
    t.batch(made=True)
    t.page.route(re.compile(r"/api/studio/batches\?"), lambda route: route.fulfill(status=503, json={
        "error": "storage is not answering (503): nothing was changed, try again in a few minutes"}))
    t.page.evaluate("() => { location.hash = '#/studio/topics'; }")
    t.page.wait_for_selector("#list .story")
    t.page.evaluate("() => { location.hash = '#/studio'; }")
    t.page.wait_for_selector("#main [data-failed]")
    msg = t.text("#main [data-failed]")
    assert "Could not load your unfinished batches right now: they are still there. Try again in a minute." in msg
    assert "503" not in msg and "nothing was changed" not in msg
    t.ctx.close()
