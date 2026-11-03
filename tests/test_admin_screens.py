"""The admin screens and the account screens in a real browser (dashboard/dev/server.mjs, headless
Chromium), Oct 2026 audit: an admin's save sends only what that admin changed, so two admins (or two
tabs) never undo each other's settings or page grants; a slow screen is never drawn over the one the
admin moved to; text typed when the session ran out comes back after signing in; access given later
shows without a reload; and a double tap on any form sends one request."""
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

DASH = Path(__file__).resolve().parent.parent / "dashboard"
PW = "dev password 123"
ROUTINE = "dev-routine"          # dev/server.mjs's routine secret (never a real one)
SLUG = "dummy-sports"            # dev/server.mjs's page
# the page's own requests answered late: window.__slow = [[part of the path, ms]]
SLOW = """() => { window.__slow = []; const real = window.fetch;
  window.fetch = async (u, o) => { const r = window.__slow.find(x => String(u).includes(x[0]));
    const answer = await real(u, o); if (r) await new Promise(ok => setTimeout(ok, r[1])); return answer; }; }"""


def free_port():
    for port in range(4121, 4130):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    pytest.skip("no free port in 4121-4129")


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
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"no headless Chromium here: {e}")
        yield b
        b.close()


class Tab:
    """One browser tab (signed in when an email is given), with the requests and page errors it saw."""

    def __init__(self, browser, server, email="admin@example.com", at="#/login"):
        self.ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        self.ctx.route(re.compile(r"^https?://(?!localhost)"), lambda route: route.abort())   # the dummy feed's photos
        self.page = self.ctx.new_page()
        self.page.add_init_script(f"({SLOW})()")
        self.requests, self.errors, self.server = [], [], server
        self.page.on("request", lambda r: self.requests.append((r.method, r.url)))
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("dialog", lambda d: d.accept())
        self.page.goto(server + "/" + at)
        if email:
            self.sign_in(email)

    def sign_in(self, email):
        self.page.fill("input[name=email]", email)
        self.page.fill("input[name=password]", PW)
        self.page.click("button.btn.primary")
        self.page.wait_for_selector("#top .me")

    def call(self, method, path, body=None, routine=False):
        r = self.page.evaluate("""async ([method, path, body, auth]) => {
            const r = await fetch(path, { method, credentials: 'same-origin', body: body === null ? undefined : JSON.stringify(body),
              headers: { 'content-type': 'application/json', ...(auth ? { authorization: 'Bearer ' + auth } : {}) } });
            return { status: r.status, body: await r.json().catch(() => null) };
        }""", [method, path, body, ROUTINE if routine else None])
        assert 200 <= r["status"] < 300, (method, path, r)
        return r["body"]

    def go(self, hash_, wait):
        self.page.evaluate("h => { location.hash = h; }", hash_)
        self.page.wait_for_selector(wait)

    def sent(self, method, part):
        return [u for m, u in self.requests if m == method and part in u]

    def toast(self):
        return self.page.locator("#toast").text_content()


def page_now(t, slug=SLUG):
    return next(p for p in t.call("GET", "/api/admin/pages")["pages"] if p["slug"] == slug)


def invite(t, email, **extra):
    """A new user with a password set; returns their id."""
    r = t.call("POST", "/api/admin/users", {"email": email, "name": "", "role": "editor", **extra})
    body = json.dumps({"token": r["setup_link"].split("#reset=")[1], "password": PW}).encode()
    req = urllib.request.Request(t.server + "/api/auth/reset", data=body, headers={"content-type": "application/json"})
    assert urllib.request.urlopen(req).status == 200          # (not from the admin's tab: it would sign it in as them)
    return r["user"]["id"]


def test_page_settings_send_only_what_this_admin_changed_and_show_what_is_stored(server, browser):
    a, b = Tab(browser, server), Tab(browser, server)
    a.go(f"#/admin/page/{SLUG}", "#set")
    # the other tab moves the page to another SocialPilot account and renames it
    b.call("PATCH", f"/api/admin/pages/{SLUG}", {"socialpilot_login_id": "777", "name": "Dummy Sports HQ"})
    a.page.fill("#set [name=timezone]", "Europe/London")
    a.page.click("#set button.primary")
    a.page.wait_for_function("() => document.querySelector('#toast').textContent === 'Saved'")
    assert a.page.input_value("#set [name=socialpilot_login_id]") == "777", "the form shows what is stored now"
    assert a.page.input_value("#set [name=name]") == "Dummy Sports HQ"
    a.page.fill("#set [name=logo_text]", "DS")
    a.page.evaluate("() => { document.querySelector('#toast').textContent = ''; }")
    a.page.click("#set button.primary")
    a.page.wait_for_function("() => document.querySelector('#toast').textContent === 'Saved'")
    p = page_now(a)
    assert (p["name"], p["socialpilot_login_id"], p["timezone"], p["theme"].get("logo_text")) == ("Dummy Sports HQ", "777", "Europe/London", "DS")
    changes = [sorted(e["fields"]) for e in a.call("GET", "/api/admin/audit?days=1")["entries"] if e["action"] == "page_changed"]
    assert ["timezone"] in changes and ["theme"] in changes, "each save sent the one field changed"
    assert not any("socialpilot_login_id" in f and "theme" in f for f in changes), "A never sent the account back"
    # a name longer than kept: the form shows the name as stored, and the next save does not send it again
    a.page.fill("#set [name=name]", "N" * 150)
    a.page.click("#set button.primary")
    a.page.wait_for_function("() => document.querySelector('#set [name=name]').value.length === 100")
    b.call("PATCH", f"/api/admin/pages/{SLUG}", {"name": "Dummy Sports"})
    a.page.fill("#set [name=fb_page_id]", "124")
    a.page.click("#set button.primary")
    a.page.wait_for_function("() => document.querySelector('#set [name=name]').value === 'Dummy Sports'")
    assert (page_now(a)["name"], page_now(a)["fb_page_id"]) == ("Dummy Sports", "124")
    a.call("PATCH", f"/api/admin/pages/{SLUG}", {"socialpilot_login_id": "999", "timezone": "America/New_York", "fb_page_id": "123"})
    a.ctx.close(); b.ctx.close()


def test_two_admins_giving_an_editor_pages_keep_both_grants(server, browser):
    a, b = Tab(browser, server), Tab(browser, server)
    second = a.call("POST", "/api/admin/pages", {"name": "Grant Second"})["page"]["slug"]
    third = a.call("POST", "/api/admin/pages", {"name": "Grant Third"})["page"]["slug"]
    uid = invite(a, "grants@example.com", pages=[SLUG])
    a.go("#/admin/users", f'tr[data-u="{uid}"]')
    b.call("PATCH", f"/api/admin/users/{uid}", {"pages": [SLUG, second]})
    row = f'tr[data-u="{uid}"]'
    a.page.check(f'{row} [data-pages] input[value="{third}"]')
    a.page.click(f"{row} [data-save]")
    a.page.wait_for_function("() => document.querySelector('#toast').textContent === 'Saved'")
    users = a.call("GET", "/api/admin/users")["users"]
    assert sorted(next(u for u in users if u["id"] == uid)["pages"]) == sorted([SLUG, second, third])
    ticked = a.page.eval_on_selector_all(f"{row} [data-pages] input:checked", "els => els.map(e => e.value)")
    assert sorted(ticked) == sorted([SLUG, second, third]), "the row shows the other admin's grant too"
    # unticking one removes that one only
    a.page.uncheck(f'{row} [data-pages] input[value="{SLUG}"]')
    a.page.click(f"{row} [data-save]")
    a.page.wait_for_function(f"() => !document.querySelector('{row} [data-pages] input[value=\"{SLUG}\"]').checked && document.querySelector('#toast').textContent === 'Saved'")
    users = a.call("GET", "/api/admin/users")["users"]
    assert sorted(next(u for u in users if u["id"] == uid)["pages"]) == sorted([second, third])
    a.ctx.close(); b.ctx.close()


def test_a_slow_manage_screen_is_not_drawn_over_the_tab_the_admin_moved_to(server, browser):
    a = Tab(browser, server)
    a.go("#/admin/pages", "#add")
    a.page.evaluate("() => { window.__slow.push(['/sources', 2000]); }")
    a.page.click(f'a[href="#/admin/page/{SLUG}"]')
    a.page.wait_for_timeout(200)
    a.page.click('.steps a[href="#/admin/users"]')
    a.page.wait_for_selector("#inv")
    a.page.wait_for_timeout(2500)
    assert a.page.locator("#inv").count() == 1, "the Users tab stays"
    assert a.page.locator("#set").count() == 0
    assert a.page.locator(".steps a.on").text_content() == "Users"
    a.ctx.close()


def test_signed_out_while_typing_an_admin_gets_their_changes_back(server, browser):
    a = Tab(browser, server)
    a.go(f"#/admin/page/{SLUG}", "#ls-notes")
    a.ctx.clear_cookies()                                              # (the session ran out)
    a.page.fill("#ls-notes", "Practice reports are too minor.")
    a.page.fill("#set [name=socialpilot_login_id]", "4242")
    a.page.click("#ls-save")
    a.page.wait_for_selector("input[name=email]")
    a.sign_in("admin@example.com")
    a.page.wait_for_selector("#ls-notes")
    a.page.wait_for_function("() => document.querySelector('#ls-notes').value === 'Practice reports are too minor.'")
    assert a.page.input_value("#set [name=socialpilot_login_id]") == "4242"
    assert "your changes are back" in a.toast()
    a.page.click("#ls-save")
    a.page.wait_for_function("() => document.querySelector('#toast').textContent.startsWith('Saved: used from')")
    assert a.call("GET", f"/api/admin/pages/{SLUG}/lessons")["lessons"]["admin_notes"] == "Practice reports are too minor."
    a.page.click("#set button.primary")
    a.page.wait_for_function("() => document.querySelector('#toast').textContent === 'Saved'")
    assert page_now(a)["socialpilot_login_id"] == "4242"
    a.call("PATCH", f"/api/admin/pages/{SLUG}", {"socialpilot_login_id": "999"})
    a.ctx.close()


def test_access_given_later_shows_without_a_reload(server, browser):
    a = Tab(browser, server)
    uid = invite(a, "later@example.com", pages=[])
    e = Tab(browser, server, email="later@example.com")
    e.page.wait_for_selector("text=No pages are assigned to you yet")
    a.call("PATCH", f"/api/admin/users/{uid}", {"pages": [SLUG]})
    e.go("#/templates", "#main .empty")
    e.page.click('#top a[href="#/studio"]')
    e.page.wait_for_selector("text=Pick up to 5 stories")
    # made an admin: the Admin tab appears once the next screen is drawn (asked at most once a minute)
    a.call("PATCH", f"/api/admin/users/{uid}", {"role": "admin"})
    e.page.evaluate("() => { S.meAt = Date.now() - 61e3; }")
    e.go("#/templates", "#main .panel")
    e.page.wait_for_selector('#top a[href="#/admin/users"]')
    a.ctx.close(); e.ctx.close()


def test_a_double_tap_on_set_password_sends_once_and_a_dead_link_says_what_to_do(server, browser):
    a = Tab(browser, server)
    r = a.call("POST", "/api/admin/users", {"email": "newbie@example.com", "role": "editor", "pages": [SLUG]})
    link = "#reset=" + r["setup_link"].split("#reset=")[1]
    n = Tab(browser, server, email=None, at=link)
    n.page.wait_for_selector("input[name=pw]")
    assert n.page.locator('#f a[href="#/forgot"]').count() == 1 and n.page.locator('#f a[href="#/login"]').count() == 1
    n.page.fill("input[name=pw]", "a new password 42")
    n.page.fill("input[name=pw2]", "a new password 42")
    n.page.dblclick("#f button")
    n.page.wait_for_selector("text=Pick up to 5 stories")
    n.page.wait_for_timeout(500)
    assert len(n.sent("POST", "/api/auth/reset")) == 1
    assert n.toast() == "Password saved"
    # the same link opened again: a clear message with the ways on, not the same form
    m = Tab(browser, server, email=None, at=link)
    m.page.fill("input[name=pw]", "another password 42")
    m.page.fill("input[name=pw2]", "another password 42")
    m.page.click("#f button")
    m.page.wait_for_selector("text=This link no longer works")
    assert m.page.locator("input[name=pw]").count() == 0
    m.page.click('#main a[href="#/forgot"]')
    m.page.wait_for_selector("text=Reset password")
    a.ctx.close(); n.ctx.close(); m.ctx.close()


def test_a_double_tap_on_forgot_password_sends_one_link(server, browser):
    t = Tab(browser, server, email=None, at="#/forgot")
    t.page.wait_for_selector("input[name=email]")
    t.page.fill("input[name=email]", "editor@example.com")
    t.page.dblclick("#f button")
    t.page.wait_for_function("() => document.querySelector('#toast').textContent.includes('reset link')")
    t.page.wait_for_timeout(500)
    assert len(t.sent("POST", "/api/auth/forgot")) == 1
    t.ctx.close()


def test_a_double_click_on_add_source_adds_it_once(server, browser):
    a = Tab(browser, server)
    a.go(f"#/admin/page/{SLUG}", "#addsrc")
    before = len(a.call("GET", f"/api/admin/pages/{SLUG}/sources")["sources"])
    a.page.fill("#addsrc [name=url]", "https://example.com/once")
    a.page.dblclick("#addsrc button")
    a.page.wait_for_selector("text=https://example.com/once")
    a.page.wait_for_timeout(800)
    srcs = a.call("GET", f"/api/admin/pages/{SLUG}/sources")["sources"]
    assert len(srcs) == before + 1
    assert a.page.locator("#main tr[data-s]").count() == before + 1
    assert a.page.locator("#lessons").count() == 1 and a.page.locator("#sheet").count() == 1
    assert not a.errors, a.errors
    a.ctx.close()


def test_a_setting_typed_in_and_put_back_is_not_sent(server, browser):
    a, b = Tab(browser, server), Tab(browser, server)
    a.go(f"#/admin/page/{SLUG}", "#set")
    was = a.page.input_value("#set [name=socialpilot_login_id]")
    a.page.fill("#set [name=socialpilot_login_id]", was + "0")
    a.page.fill("#set [name=socialpilot_login_id]", was)               # (put back as it was drawn)
    b.call("PATCH", f"/api/admin/pages/{SLUG}", {"socialpilot_login_id": "777"})
    a.page.fill("#set [name=timezone]", "Europe/London")
    with a.page.expect_request(lambda r: r.method == "PATCH" and f"/api/admin/pages/{SLUG}" in r.url) as req:
        a.page.click("#set button.primary")
    assert json.loads(req.value.post_data) == {"timezone": "Europe/London"}
    a.page.wait_for_function("() => document.querySelector('#toast').textContent === 'Saved'")
    assert page_now(a)["socialpilot_login_id"] == "777", "the other admin's account is kept"
    assert a.page.input_value("#set [name=socialpilot_login_id]") == "777"
    a.call("PATCH", f"/api/admin/pages/{SLUG}", {"socialpilot_login_id": "999", "timezone": "America/New_York"})
    a.ctx.close(); b.ctx.close()


def test_a_page_box_or_role_changed_and_put_back_is_not_sent(server, browser):
    a, b = Tab(browser, server), Tab(browser, server)
    second = a.call("POST", "/api/admin/pages", {"name": "Back Second"})["page"]["slug"]
    third = a.call("POST", "/api/admin/pages", {"name": "Back Third"})["page"]["slug"]
    uid = invite(a, "putback@example.com", pages=[SLUG])
    row = f'tr[data-u="{uid}"]'
    a.go("#/admin/users", row)
    a.page.check(f'{row} [data-pages] input[value="{third}"]')
    a.page.uncheck(f'{row} [data-pages] input[value="{third}"]')
    a.page.select_option(f"{row} [data-role]", "admin")
    a.page.select_option(f"{row} [data-role]", "editor")
    b.call("PATCH", f"/api/admin/users/{uid}", {"pages_add": [third]})
    a.page.check(f'{row} [data-pages] input[value="{second}"]')
    with a.page.expect_request(lambda r: r.method == "PATCH" and uid in r.url) as req:
        a.page.click(f"{row} [data-save]")
    assert json.loads(req.value.post_data) == {"pages_add": [second]}
    a.page.wait_for_function("() => document.querySelector('#toast').textContent === 'Saved'")
    users = a.call("GET", "/api/admin/users")["users"]
    assert sorted(next(u for u in users if u["id"] == uid)["pages"]) == sorted([SLUG, second, third]), "the other admin's grant is kept"
    a.ctx.close(); b.ctx.close()


def test_a_page_ticked_while_a_row_is_saved_stays_ticked_to_be_saved(server, browser):
    a = Tab(browser, server)
    second = a.call("POST", "/api/admin/pages", {"name": "Held Second"})["page"]["slug"]
    third = a.call("POST", "/api/admin/pages", {"name": "Held Third"})["page"]["slug"]
    uid = invite(a, "heldrow@example.com", pages=[SLUG])
    row = f'tr[data-u="{uid}"]'
    a.go("#/admin/users", row)
    held = []
    a.page.route(f"**/api/admin/users/{uid}", lambda rt: held.append(rt) if rt.request.method == "PATCH" else rt.continue_())
    a.page.check(f'{row} [data-pages] input[value="{second}"]')
    a.page.click(f"{row} [data-save]")
    deadline = time.time() + 10
    while not held and time.time() < deadline:
        a.page.wait_for_timeout(50)
    a.page.check(f'{row} [data-pages] input[value="{third}"]')        # (while the save is under way)
    held[0].continue_()
    a.page.wait_for_function("() => document.querySelector('#toast').textContent === 'Saved'")
    a.page.unroute(f"**/api/admin/users/{uid}")
    assert a.page.is_checked(f'{row} [data-pages] input[value="{third}"]'), "still ticked, to be saved"
    users = a.call("GET", "/api/admin/users")["users"]
    assert sorted(next(u for u in users if u["id"] == uid)["pages"]) == sorted([SLUG, second])
    a.page.evaluate("() => { document.querySelector('#toast').textContent = ''; }")
    a.page.click(f"{row} [data-save]")
    a.page.wait_for_function("() => document.querySelector('#toast').textContent === 'Saved'")
    users = a.call("GET", "/api/admin/users")["users"]
    assert sorted(next(u for u in users if u["id"] == uid)["pages"]) == sorted([SLUG, second, third])
    a.ctx.close()


def test_access_taken_away_is_said_when_the_details_are_asked_again(server, browser):
    a = Tab(browser, server)
    other = a.call("POST", "/api/admin/pages", {"name": "Taken Other"})["page"]["slug"]
    a.call("PATCH", f"/api/admin/pages/{SLUG}", {"name": "Dummy Sports"})
    did = invite(a, "demoted@example.com", role="admin", pages=[SLUG])
    d = Tab(browser, server, email="demoted@example.com")
    d.go("#/admin/users", "#inv")
    a.call("PATCH", f"/api/admin/users/{did}", {"role": "editor"})
    d.page.evaluate("() => { S.meAt = Date.now() - 61e3; }")          # (a minute later)
    d.page.evaluate("() => { location.hash = '#/admin/pages'; }")
    d.page.wait_for_function("() => document.querySelector('#toast').textContent.includes('no longer an admin')")
    assert d.toast() == "You are no longer an admin: ask an admin if you still need it"
    eid = invite(a, "lostpage@example.com", pages=[SLUG, other])
    e = Tab(browser, server, email="lostpage@example.com")
    e.page.wait_for_selector("text=Pick up to 5 stories")
    a.call("PATCH", f"/api/admin/users/{eid}", {"pages_remove": [SLUG]})
    e.page.evaluate("() => { S.meAt = Date.now() - 61e3; }")
    e.page.evaluate("() => { location.hash = '#/templates'; }")
    e.page.wait_for_function("() => document.querySelector('#toast').textContent.includes('no longer have access')")
    assert e.toast() == "You no longer have access to Dummy Sports: ask an admin if you still need it"
    a.ctx.close(); d.ctx.close(); e.ctx.close()


def test_changes_kept_at_sign_out_are_not_given_to_another_admin(server, browser):
    a = Tab(browser, server)
    invite(a, "admin2@example.com", role="admin")
    a.go(f"#/admin/page/{SLUG}", "#set")
    a.ctx.clear_cookies()                                              # (the session ran out)
    a.page.fill("#set [name=socialpilot_login_id]", "5555")
    a.page.click("#set button.primary")
    a.page.wait_for_selector("input[name=email]")
    a.sign_in("admin2@example.com")
    a.page.wait_for_selector("#set")
    a.page.wait_for_timeout(300)
    stored = page_now(a)["socialpilot_login_id"]
    assert stored != "5555" and a.page.input_value("#set [name=socialpilot_login_id]") == stored
    assert "your changes are back" not in (a.toast() or "")
    assert a.page.evaluate("() => sessionStorage.getItem('drafts')") is None
    a.ctx.close()


def test_the_manage_screen_opens_from_an_address_ending_in_a_slash(server, browser):
    a = Tab(browser, server)
    a.go("#/admin/users", "#inv")
    a.go(f"#/admin/page/{SLUG}/", "#set")
    assert a.page.locator("#inv").count() == 0
    a.ctx.close()


def test_an_editor_made_admin_gets_the_admin_screens_at_once(server, browser):
    a = Tab(browser, server)
    uid = invite(a, "promoted@example.com", pages=[SLUG])
    e = Tab(browser, server, email="promoted@example.com")
    e.page.wait_for_selector("text=Pick up to 5 stories")
    a.call("PATCH", f"/api/admin/users/{uid}", {"role": "admin"})
    e.go("#/admin/users", "#inv")                                      # (within the minute)
    e.page.wait_for_selector('#top a[href="#/admin/users"]')
    a.ctx.close(); e.ctx.close()
