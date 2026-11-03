"""The review page (dashboard/public/review.html) in Chromium, every dashboard call answered here
(third audit): an emptied caption is not approved, a second tap on a list that just moved approves
nothing, a lost connection is said in plain words, a posted post links to Facebook, a link in the
list stays readable on a phone, the server's own words when the posts could not load, bulk actions
say which posts were not changed, notes that could not be loaded are said so. The Studio start
screen tries again by itself while storage is not answering."""
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

PUBLIC = Path(__file__).resolve().parent.parent / "dashboard" / "public"


def _draft(slug, status="pending", **kw):
    return {"id": f"dummy-sports-202610211200-{slug}", "page": "dummy-sports", "page_name": "Dummy Sports",
            "caption": f"Caption of {slug}", "status": status, "headline": slug, "created_at_unix": 1_792_600_000,
            "image_url": "/img.jpg", **kw}


class Site:
    """The dashboard's answers: drafts by tab, and what the page asked for (path, method, body)."""

    def __init__(self, drafts=(), stats=None, links=(), overrides=None):
        self.drafts, self.asked = list(drafts), []
        self.stats = stats or {"counts": {}, "totals": {}, "publisher": {}, "lessons": []}
        self.links, self.overrides = list(links), overrides or {}

    def answer(self, route):
        req = route.request
        path = urlsplit(req.url).path
        body = json.loads(req.post_data) if req.post_data else None
        self.asked.append((path, req.method, body))
        if path in ("/review.html", "/index.html", "/app.js", "/app.css"):
            types = {"js": "text/javascript", "css": "text/css"}
            return route.fulfill(body=(PUBLIC / path[1:]).read_text(), content_type=types.get(path.rsplit(".", 1)[1], "text/html"))
        if path in self.overrides:
            return self.overrides[path](route, body)
        if path == "/api/drafts" and req.method == "GET":
            return self.json(route, {"drafts": [d for d in self.drafts if d["status"] == "pending" or "status=all" in req.url
                                                or f"status={d['status']}" in req.url]})
        if path == "/api/decide":
            self.drafts = [d for d in self.drafts if d["id"] != body.get("id")]
            return self.json(route, {"ok": True, "queued": True})
        if path == "/api/stats":
            return self.json(route, self.stats)
        if path == "/api/links":
            return self.json(route, {"links": self.links})
        if path == "/api/auth/me":
            return self.json(route, {"user": {"email": "ed@example.com", "role": "editor"}, "pages": [{"slug": "dummy-sports", "name": "Dummy Sports"}]})
        return self.json(route, {})

    @staticmethod
    def json(route, obj, status=200):
        return route.fulfill(status=status, body=json.dumps(obj), content_type="application/json")

    def decided(self):
        return [b["id"] for p, m, b in self.asked if p == "/api/decide" and b and "id" in b]


def _open(pw, site, path="/review.html", phone=False):
    from bore.cards import _chrome_path
    browser = pw.chromium.launch(**({"executable_path": _chrome_path()} if _chrome_path() else {}))
    ctx = browser.new_context(**({"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True} if phone else {}))
    page = ctx.new_page()
    page.on("dialog", lambda d: d.accept())
    page.route("**/*", site.answer)
    page.goto("http://bore.test" + path)
    return browser, page


def _toast(page):
    page.wait_for_selector("#toast.show")
    return page.inner_text("#toast")


def test_an_emptied_caption_is_not_approved():
    site = Site([_draft("blank")])
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site)
        try:
            page.wait_for_selector(".post")
            page.wait_for_timeout(900)
            page.fill(".post .side > textarea", "  \n ")
            page.click(".post .okay")
            assert _toast(page) == "The caption is empty: write one, or Undo your edit"
            assert site.decided() == []
        finally:
            browser.close()


@pytest.mark.parametrize("gap", [300, 700, 900, 1300])
def test_a_double_tap_on_okay_approves_only_the_post_the_editor_read(gap):
    site = Site([_draft("alpha"), _draft("bravo"), _draft("charlie")])
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site, phone=True)
        try:
            page.wait_for_selector(".post")
            page.wait_for_timeout(900)
            page.locator(".post .okay").first.scroll_into_view_if_needed()
            box = page.locator(".post .okay").first.bounding_box()
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            page.touchscreen.tap(x, y)
            page.wait_for_timeout(gap)
            page.touchscreen.tap(x, y)
            # (900 ms: the list redrew about 100 ms after the first tap, long before the second)
            assert _toast(page) == "The list just changed: check this post, then tap again"
            page.wait_for_timeout(1200)
            assert site.decided() == ["dummy-sports-202610211200-alpha"]
            # once the list has stood still, the next post is decided as usual
            page.touchscreen.tap(x, y)
            page.wait_for_timeout(600)
            assert len(site.decided()) == 2
        finally:
            browser.close()


def test_a_tap_right_after_the_page_or_a_tab_loads_is_taken():
    site = Site([_draft("alpha"), _draft("bravo")])
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site)
        try:
            page.wait_for_selector(".post")
            page.locator(".post .okay").first.click()                 # at once: nothing moved under the finger
            page.wait_for_timeout(300)
            assert site.decided() == ["dummy-sports-202610211200-alpha"]
            page.wait_for_timeout(1500)
            page.click(".tabs button:has-text('All')")
            page.wait_for_selector(".post")
            page.locator(".post .okay").first.click()
            page.wait_for_timeout(300)
            assert site.decided() == ["dummy-sports-202610211200-alpha", "dummy-sports-202610211200-bravo"]
        finally:
            browser.close()


def test_okay_with_no_signal_says_nothing_was_sent():
    # (the page's routes answer even offline: the phone's own state, and the request not getting out)
    site = Site([_draft("nosignal")], overrides={"/api/decide": lambda route, body: route.abort("internetdisconnected")})
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site)
        try:
            page.wait_for_selector(".post")
            page.evaluate("Object.defineProperty(navigator, 'onLine', { get: () => false })")
            page.locator(".post .okay").first.click()
            assert _toast(page) == "No connection: nothing was sent, try again once you are back online"
        finally:
            browser.close()


def test_a_caption_of_only_zero_width_characters_is_not_approved():
    site = Site([_draft("invisible")])
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site)
        try:
            page.wait_for_selector(".post")
            page.fill(".post .side > textarea", "\u200b\u200b \ufeff")
            page.click(".post .okay")
            assert _toast(page) == "The caption is empty: write one, or Undo your edit"
            assert site.decided() == []
        finally:
            browser.close()


def test_a_lost_connection_is_said_in_plain_words():
    site = Site([_draft("offline"), _draft("lost")], overrides={"/api/decide": lambda route, body: route.abort(),
                                                               "/api/feedback": lambda route, body: route.abort()})
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site)
        try:
            page.wait_for_selector(".post")
            page.wait_for_timeout(900)
            page.locator(".post .okay").first.click()
            text = _toast(page)
            assert "Failed to fetch" not in text
            assert text == "Connection lost: it may have been approved, see the list once you are back online"
            page.eval_on_selector("#toast", "t => t.classList.remove('show')")
            page.locator(".post .fb summary").first.click()
            page.locator(".post .fbtext").first.fill("other photo")
            page.locator(".post .regen").first.click()
            assert _toast(page) == "No connection: check the internet connection, then try again"
        finally:
            browser.close()


def test_a_posted_post_links_to_facebook():
    url = "https://www.facebook.com/123/posts/456"
    site = Site([_draft("posted", "posted", status_info={"result": {"url": url}}),
                 _draft("odd", "posted", status_info={"result": {"url": "javascript:alert(1)"}})])
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site, "/review.html")
        try:
            page.wait_for_selector(".tabs")
            page.click(".tabs button:has-text('Posted')")
            page.wait_for_selector(".post .status")
            links = page.locator(".post .status a")
            assert links.count() == 1
            assert links.first.get_attribute("href") == url and links.first.inner_text() == "View on Facebook"
            assert url not in page.inner_text("main")
        finally:
            browser.close()


def test_a_link_in_the_list_stays_readable_on_a_phone():
    link = {"id": "l1", "url": "https://www.example.com/nba/story/_/id/123456/a-long-story-about-the-trade-deadline",
            "page": "dummy-sports", "page_name": "Dummy Sports", "at": int(time.time() * 1000), "status": "waiting"}   # "just now"
    site = Site([], links=[link])
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site, phone=True)
        try:
            page.wait_for_selector(".linkbox summary")
            page.click(".linkbox summary")
            a = page.locator(".links a").first.bounding_box()
            assert a["width"] > 200, a
            assert page.evaluate("document.documentElement.scrollWidth") <= 390
        finally:
            browser.close()


def test_a_failed_load_says_storage_is_not_answering_in_plain_words():
    said = "storage is not answering (503): nothing was changed, try again in a few minutes"
    site = Site([], overrides={"/api/drafts": lambda route, body: Site.json(route, {"error": said}, 503)})
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site)
        try:
            page.wait_for_selector(".loaderr")
            text = page.inner_text(".loaderr")
            assert text.startswith("Could not load the posts: the dashboard's storage is not answering just now. This is not an empty queue. "
                                   "Trying again in a few seconds."), text
            assert "503" not in text and "nothing was changed" not in text
        finally:
            browser.close()
    # a failed post approved again while storage is not answering: its words, without the status code
    site = Site([_draft("failed", "failed")], overrides={"/api/decide": lambda route, body: Site.json(route, {"error": said}, 503)})
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site)
        try:
            page.click(".tabs button:has-text('Failed')")
            page.wait_for_selector(".post")
            page.locator(".post .okay").first.click()
            assert _toast(page) == "storage is not answering: nothing was changed, try again in a few minutes"
        finally:
            browser.close()
    site = Site([], overrides={"/api/drafts": lambda route, body: route.fulfill(status=502, body="<html>Bad gateway</html>")})
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site)
        try:
            page.wait_for_selector(".loaderr")
            assert "the connection or the server did not answer" in page.inner_text(".loaderr")
        finally:
            browser.close()


def test_bulk_clear_and_not_okay_say_which_posts_were_not_changed():
    why = "Someone already approved this post (see Queued)"
    sk = [{"id": "dummy-sports-202610211200-taken", "why": why}]
    site = Site([_draft("taken")], overrides={
        "/api/drafts": lambda route, body: (Site.json(route, {"ok": True, "cleared": 0, "ids": [], "skipped": sk}) if body is not None
                                            else Site.json(route, {"drafts": [_draft("taken")]})),
        "/api/decide": lambda route, body: Site.json(route, {"ok": True, "rejected": 0, "ids": [], "skipped": sk})})
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site)
        try:
            page.wait_for_selector(".post")
            page.check(".post .pick input")
            page.click("text=Clear selected")
            assert _toast(page) == f"Cleared 0 · 1 not changed: {why}"
            page.eval_on_selector("#toast", "t => t.classList.remove('show')")
            page.wait_for_selector(".post")
            page.check(".post .pick input")
            page.select_option(".clearbar select", "Duplicate")
            assert _toast(page) == f"0 marked Not okay · 1 not changed: {why}"
        finally:
            browser.close()


def test_notes_that_could_not_be_loaded_are_said_so():
    site = Site([_draft("failed", "failed")], stats={"counts": {}, "totals": {}, "lessons": [],
                                                     "publisher": {"needs_action": {}, "needs_action_unknown": True}})
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site)
        try:
            page.wait_for_selector("main")
            page.wait_for_selector(".banner")
            assert "could not be loaded just now" in page.inner_text("#app")
        finally:
            browser.close()


def test_the_studio_start_screen_tries_again_by_itself_while_storage_is_not_answering():
    said = "storage is not answering (503): nothing was changed, try again in a few minutes"
    site = Site([], overrides={"/api/auth/me": lambda route, body: Site.json(route, {"error": said}, 503)})
    with sync_api.sync_playwright() as pw:
        browser, page = _open(pw, site, "/index.html")
        try:
            page.wait_for_selector("#main [data-failed]")
            text = page.inner_text("#main")
            assert "The dashboard's storage is not answering just now: this screen tries again by itself." in text, text
            assert "503" not in text and "in a few minutes" not in text
            page.wait_for_timeout(6500)                       # the first try again comes after 5 s
            assert sum(1 for p, m, b in site.asked if p == "/api/auth/me") >= 2
        finally:
            browser.close()
