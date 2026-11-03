"""The review page shows the pipeline's health (GET /api/health) as a banner: red or amber with the
reasons, nothing when it is green, fetched on its own so the queue never waits for it. Drawn in
Chromium with every dashboard call answered here."""
import json
from pathlib import Path
from urllib.parse import urlsplit

HTML = Path(__file__).resolve().parent.parent / "dashboard" / "public" / "review.html"


def _app_text(health: dict, publisher: dict | None = None) -> str:
    from playwright.sync_api import sync_playwright

    from bore.cards import _chrome_path
    replies = {"/api/drafts": {"drafts": []}, "/api/stats": {"counts": {}, "publisher": publisher or {}, "lessons": []},
               "/api/links": {"links": []}, "/api/health": health}

    def answer(route):
        path = urlsplit(route.request.url).path
        if path == "/review.html":
            return route.fulfill(body=HTML.read_text(), content_type="text/html")
        return route.fulfill(body=json.dumps(replies.get(path, {})), content_type="application/json")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**({"executable_path": _chrome_path()} if _chrome_path() else {}))
        try:
            page = browser.new_page()
            page.route("**/*", answer)
            page.goto("http://bore.test/review.html")
            page.wait_for_selector("main")
            page.wait_for_selector("#health[data-at]", state="attached")     # drawn when it arrives
            return page.inner_text("#app")
        finally:
            browser.close()


def test_red_and_amber_health_show_their_reasons_and_green_shows_nothing():
    now = 1_792_600_000_000
    red = {"status": "red", "checked_at": now, "reasons": [
        {"code": "hourly_stale", "level": "red", "text": "No hourly post run seen for 3h.", "since": now, "ids": []},
        {"code": "topics_stale", "level": "amber", "text": "The Studio story list for Demo Sports was last refreshed 2h ago.",
         "since": now, "ids": ["demo-sports"]}]}
    text = _app_text(red)
    assert "Part of the pipeline is not working" in text
    assert "No hourly post run seen for 3h." in text and "Demo Sports was last refreshed 2h ago" in text
    amber = {"status": "amber", "checked_at": now, "reasons": red["reasons"][1:]}
    assert "running with problems" in _app_text(amber)
    green = _app_text({"status": "green", "checked_at": now, "reasons": []})
    assert "pipeline" not in green
    # SocialPilot's own outage keeps its own banner: not said twice
    held = {"status": "amber", "checked_at": now, "reasons": [
        {"code": "publisher_held", "level": "amber", "text": "2 approved post(s) have waited more than 2 hours for SocialPilot.",
         "since": now, "ids": ["a", "b"]}]}
    text = _app_text(held, {"blocked": True, "held": True, "waiting": 2, "error": "locked"})
    assert "waited more than 2 hours" not in text and "SocialPilot" in text


def test_the_queue_is_drawn_without_waiting_for_the_health_status():
    from playwright.sync_api import sync_playwright

    from bore.cards import _chrome_path
    draft = {"id": "demo-sports-1", "page": "demo-sports", "page_name": "Demo Sports", "caption": "A caption", "status": "pending",
             "headline": "A headline", "created_at_unix": 1_792_600_000}
    replies = {"/api/drafts": {"drafts": [draft]}, "/api/stats": {"counts": {}, "publisher": {}, "lessons": []},
               "/api/links": {"links": []}}
    held, asked = [], []

    def answer(route):
        path = urlsplit(route.request.url).path
        asked.append(path)
        if path == "/review.html":
            return route.fulfill(body=HTML.read_text(), content_type="text/html")
        if path == "/api/health":                    # working it out takes seconds: not answered yet
            return held.append(route)
        return route.fulfill(body=json.dumps(replies.get(path, {})), content_type="application/json")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**({"executable_path": _chrome_path()} if _chrome_path() else {}))
        try:
            page = browser.new_page()
            page.route("**/*", answer)
            page.goto("http://bore.test/review.html")
            page.wait_for_selector(".post")
            assert held and "pipeline" not in page.inner_text("#app"), "the queue is there while health is still coming"
            held[0].fulfill(body=json.dumps({"status": "red", "checked_at": 1_792_600_000_000, "reasons": [
                {"code": "hourly_stale", "level": "red", "text": "No hourly post run seen for 3h.", "since": 1, "ids": []}]}),
                content_type="application/json")
            page.wait_for_selector("#health[data-at]", state="attached")
            assert "No hourly post run seen for 3h." in page.inner_text("#app") and page.locator(".post").count() == 1
            with page.expect_response("**/api/drafts*"):  # approving or rejecting reloads the queue, not the health
                page.evaluate("load()")
            assert asked.count("/api/health") == 1 and asked.count("/api/drafts") == 2
        finally:
            browser.close()
