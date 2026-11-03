"""The tester's journey and the product's look in a real browser (dashboard/dev/server.mjs, headless
Chromium), at 1280 px and on a 390 px phone (docs/PRODUCT.md "A page's life", "The look"): an owner
with no page is asked to connect their first one; the four steps save as they go; the red Go live
button stays off, saying what is missing, until the page is ready; the onboarding's steps show as the
routine (played here through its API) reports them, then Live with the ways on, or the reason and Try
again; Pause and Resume; the super's testers, budgets, pages and SocialPilot text; open access never
shows a sign-in screen. Every page, account and text here is made up."""
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
PDF = DASH / "test" / "fixtures" / "dummy_templates.pdf"
PW = "dev password 123"
ROUTINE = "dev-routine"          # dev/server.mjs's routine secret (never a real one)
CONNECT = "Open https://example.com/connect-demo and sign in with the account we sent you, then add your Facebook page."
WIDTHS = (1280, 390)


def free_port(lo):
    for port in range(lo, lo + 9):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    pytest.skip(f"no free port in {lo}-{lo + 8}")


def start(lo, **env_extra):
    if not shutil.which("node") or not (DASH / "node_modules" / "@vercel" / "blob").exists():
        pytest.skip("node and the dashboard's packages are needed (npm ci in dashboard/)")
    port = free_port(lo)
    env = {k: v for k, v in os.environ.items() if k not in ("ROUTINE_SECRET_NEXT", "DATA_KEY", "SESSION_SECRET", "OPEN_ACCESS")}
    proc = subprocess.Popen(["node", "dev/server.mjs", str(port)], cwd=DASH, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**env, "ROUTINE_SECRET": ROUTINE, **env_extra})
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if "dev server" in line:
            return proc, f"http://localhost:{port}"
        if not line and proc.poll() is not None:
            pytest.fail("dev server did not start")
    proc.terminate()
    pytest.fail("dev server did not start in 30 s")


@pytest.fixture(scope="module")
def server():
    proc, url = start(4131)
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def open_server():
    proc, url = start(4141, OPEN_ACCESS="1")
    yield url
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


def routine(server, method, path, body=None):
    """What the onboarding routine sends (ONBOARD.md), with the dev server's routine secret."""
    req = urllib.request.Request(server + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"content-type": "application/json", "authorization": f"Bearer {ROUTINE}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b"{}")


def set_password(server, link):
    body = json.dumps({"token": link.split("#reset=")[1], "password": PW}).encode()
    req = urllib.request.Request(server + "/api/auth/reset", data=body, headers={"content-type": "application/json"})
    assert urllib.request.urlopen(req).status == 200


class Tab:
    """One browser tab (signed in when an email is given), with the requests and page errors it saw."""

    def __init__(self, browser, server, email="admin@example.com", width=1280, at="#/login"):
        self.ctx = browser.new_context(viewport={"width": width, "height": 900})
        self.ctx.route(re.compile(r"^https?://(?!localhost)"), lambda route: route.abort())   # the dummy feed's photos
        self.page = self.ctx.new_page()
        self.width, self.server, self.requests, self.errors = width, server, [], []
        self.page.on("request", lambda r: self.requests.append((r.method, r.url, r.post_data)))
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("dialog", lambda d: d.accept())
        self.page.goto(server + "/" + at)
        if email:
            self.page.fill("input[name=email]", email)
            self.page.fill("input[name=password]", PW)
            self.page.click("button.btn.primary")
            self.page.wait_for_selector("#top .me")

    def call(self, method, path, body=None):
        r = self.page.evaluate("""async ([method, path, body]) => {
            const r = await fetch(path, { method, credentials: 'same-origin', body: body === null ? undefined : JSON.stringify(body),
              headers: { 'content-type': 'application/json' } });
            return { status: r.status, body: await r.json().catch(() => null) };
        }""", [method, path, body])
        assert 200 <= r["status"] < 300, (method, path, r)
        return r["body"]

    def go(self, hash_, wait):
        self.page.evaluate("h => { location.hash = h; }", hash_)
        self.page.wait_for_selector(wait)

    def text(self, sel):
        return self.page.locator(sel).inner_text()

    def toast_is(self, text):
        self.page.wait_for_function("t => document.querySelector('#toast').textContent.startsWith(t)", arg=text)

    def fits(self, where):
        """Nothing wider than the screen: no sideways scrolling on a phone."""
        w = self.page.evaluate("() => document.documentElement.scrollWidth")
        assert w <= self.width, f"{where}: the page is {w} px wide on a {self.width} px screen"

    def sent(self, method, part, since=0):
        """What was sent to paths holding `part` (ending in it, when it ends with $), after request number `since`."""
        hit = (lambda u: u.split("?")[0].endswith(part[:-1])) if part.endswith("$") else (lambda u: part in u)
        return [json.loads(d) if d else None for m, u, d in self.requests[since:] if m == method and hit(u)]


def new_tester(admin, server, email, workspace):
    r = admin.call("POST", "/api/admin/testers", {"email": email, "name": "Demo Tester", "workspace_name": workspace})
    set_password(server, r["setup_link"])
    return r["workspace"]["id"]


@pytest.mark.parametrize("width", WIDTHS)
def test_a_tester_connects_their_first_page_and_watches_it_go_live(server, browser, width):
    admin = Tab(browser, server)
    admin.call("PATCH", "/api/admin/settings", {"socialpilot_connect": CONNECT})
    new_tester(admin, server, f"journey{width}@example.com", f"Journey Workspace {width}")
    t = Tab(browser, server, email=f"journey{width}@example.com", width=width)
    # no page yet: the first screen asks for one
    t.page.wait_for_selector("#firstpage")
    assert "Connect your first page" in t.text("#firstpage")
    assert t.page.evaluate("() => location.hash") == "#/pages"
    t.fits("first page")
    t.page.click("#firstpage a.btn.primary")

    # 1. Page details
    t.page.wait_for_selector("#pdet")
    name, fb = f"Demo Journey {width}", f"facebook.com/demojourney{width}"
    t.page.fill("#pdet [name=name]", name)
    t.page.fill("#pdet [name=fb_page_url]", fb)
    t.page.fill("#pdet [name=niche]", "Made-up news about a made-up hobby.")
    t.page.fill("#pdet [name=timezone]", "Europe/London")
    t.fits("page details")
    t.page.dblclick("#pdet button.primary")                            # (a double tap makes one page)
    t.page.wait_for_selector("#spdone")
    assert len(t.sent("POST", "/api/pages")) == 1
    slug = re.match(r"#/pages/([^/]+)/connect", t.page.evaluate("() => location.hash")).group(1)
    p = t.call("GET", f"/api/pages/{slug}")["page"]
    assert (p["name"], p["fb_page_url"], p["timezone"], p["language"], p["status"]) == (name, f"https://{fb}", "Europe/London", "English", "draft")

    # 2. Connect in SocialPilot: the product owner's text, its link clickable; the red button is off until ready
    assert t.text("#connecthelp").startswith("Open https://example.com/connect-demo")
    assert t.page.locator('#connecthelp a[href="https://example.com/connect-demo"]').count() == 1
    t.fits("connect")
    t.page.click('.wizard a[href$="/review"]')
    t.page.wait_for_selector("#golive")
    assert t.page.locator("#golive").is_disabled()
    why = t.text("#golive-why")
    assert "connect the page in SocialPilot" in why and "upload the post templates document" in why and "upload the caption language and tone document" in why
    assert "Facebook address" not in why, "what is there is not asked for"
    t.page.click('.wizard a[href$="/connect"]')
    t.page.wait_for_selector("#spdone")
    t.page.check("#spdone")
    t.toast_is("Saved")
    assert t.call("GET", f"/api/pages/{slug}")["page"]["socialpilot_connected"] is True

    # 3. Upload documents: sent in parts, with what happened on the document's card
    t.page.click('#main a[href$="/documents"].btn')
    t.page.wait_for_selector('.card[data-t="templates"]')
    for kind in ("templates", "tone"):
        t.page.set_input_files(f'.card[data-t="{kind}"] [data-up]', str(PDF))
        t.page.wait_for_function("k => (document.querySelector(`.card[data-t='${k}'] [data-docnote]`) || {}).textContent?.includes('is uploaded as v1')", arg=kind)
        t.page.wait_for_selector(f'.card[data-t="{kind}"] .docnow')
    assert t.sent("POST", f"/api/pages/{slug}/docs/templates/part"), "the owner's own route, in parts"
    t.fits("documents")

    # 4. Go live
    t.page.click('#main a[href$="/review"].btn')
    t.page.wait_for_selector("#golive:not([disabled])")
    assert t.text("#golive-why") == ""
    t.fits("go live")
    t.page.dblclick("#golive")
    t.page.wait_for_selector('.statushead[data-st="going_live"]')
    assert len(t.sent("POST", "/go-live")) == 1, "one tap, one Go live"
    assert "is going live" in t.text(".statushead") and "Waiting for the onboarding routine to start" in t.text(".statushead")
    assert t.page.locator(".progress li").count() == 6

    # the onboarding routine claims the job and reports each step: the screen follows
    job = next(j for j in routine(server, "GET", "/api/routine/onboard/jobs")["jobs"] if j["page"]["slug"] == slug)["id"]
    on = lambda act, body=None: routine(server, "POST", f"/api/routine/onboard/jobs/{job}/{act}", body or {})
    on("claim", {"session": "test"})
    on("step", {"step": "link_account", "detail": f"Found {name} in SocialPilot"})
    on("result", {"socialpilot_login_id": f"555{width}"})
    t.page.wait_for_selector('li[data-step="link_account"].done', timeout=15000)
    assert f"Found {name} in SocialPilot" in t.text('li[data-step="link_account"]')
    assert t.page.locator('li[data-step="read_documents"].now').count() == 1, "the next one is under way"
    assert "Waiting for the onboarding routine" not in t.text(".statushead")
    for step in ("read_documents", "research_sources", "write_playbook", "queue_templates", "first_run"):
        on("step", {"step": step, "detail": "done"})
    on("done")
    t.page.wait_for_selector('.statushead[data-st="live"]', timeout=15000)
    assert f"{name} is live" in t.text(".statushead")
    assert t.page.locator('.statushead a[href="/review.html"]').count() == 1
    assert t.page.locator(".progress li.done").count() == 6
    t.fits("live")
    assert t.page.locator(f'#pagesel option[value="{slug}"]').inner_text() == name, "live in the page picker, without a reload"

    # Live: its Studio opens on it
    t.page.click('.statushead a[href="#/studio"]')
    t.page.wait_for_selector(f"text=Pick up to 5 stories · {name}")
    assert t.page.locator("[data-closed]").count() == 0

    # the pages list: Pause (said first), then Resume
    t.go("#/pages", f'.pagecard[data-page="{slug}"]')
    card = f'.pagecard[data-page="{slug}"]'
    assert t.text(f"{card} .pill") == "live"
    t.fits("pages")
    t.page.click(f"{card} [data-pause]")
    t.page.wait_for_function("s => document.querySelector(`${s} .pill`).textContent === 'paused'", arg=card)
    assert t.call("GET", f"/api/pages/{slug}")["page"]["status"] == "paused"
    t.page.click(f"{card} [data-resume]")
    t.page.wait_for_function("s => document.querySelector(`${s} .pill`).textContent === 'live'", arg=card)
    assert not t.errors, t.errors
    admin.ctx.close(); t.ctx.close()


def ready_page(t, name):
    """A page with what Go live needs, made through the owner's routes."""
    import base64
    slug = t.call("POST", "/api/pages", {"name": name, "fb_page_url": f"facebook.com/{name.lower().replace(' ', '')}",
                                         "niche": "Made-up news about a made-up hobby."})["page"]["slug"]
    data = base64.b64encode(PDF.read_bytes()).decode()
    for kind in ("templates", "tone"):
        t.call("POST", f"/api/pages/{slug}/docs/{kind}", {"filename": f"{kind}.pdf", "data_b64": data})
    t.call("PATCH", f"/api/pages/{slug}", {"socialpilot_connected": True})
    return slug


@pytest.mark.parametrize("width", WIDTHS)
def test_a_failed_onboarding_says_why_and_try_again_starts_it_again(server, browser, width):
    admin = Tab(browser, server)
    new_tester(admin, server, f"failing{width}@example.com", f"Failing Workspace {width}")
    t = Tab(browser, server, email=f"failing{width}@example.com", width=width)
    slug = ready_page(t, f"Demo Fail {width}")
    first = t.call("POST", f"/api/pages/{slug}/go-live", {})["onboarding"]["job"]["id"]
    routine(server, "POST", f"/api/routine/onboard/jobs/{first}/claim", {})
    routine(server, "POST", f"/api/routine/onboard/jobs/{first}/step", {"step": "link_account", "ok": False, "detail": "no account matched"})
    routine(server, "POST", f"/api/routine/onboard/jobs/{first}/fail", {"error": f"connect Demo Fail {width} in SocialPilot (instructions on the page), then press Try again"})
    t.go(f"#/pages/{slug}", '.statushead[data-st="failed"]')
    assert f"connect Demo Fail {width} in SocialPilot (instructions on the page), then press Try again" in t.text(".statushead .job.err")
    assert t.page.locator('li[data-step="link_account"].failed').count() == 1
    t.fits("failed")
    t.page.dblclick("#retry")
    t.page.wait_for_selector('.statushead[data-st="going_live"]')
    assert len(t.sent("POST", "/retry")) == 1
    again = t.call("GET", f"/api/pages/{slug}/onboarding")["onboarding"]
    assert again["status"] == "going_live" and again["job"]["id"] != first and again["error"] == ""
    # the pages list says it too
    t.go("#/pages", f'.pagecard[data-page="{slug}"]')
    assert t.text(f'.pagecard[data-page="{slug}"] .pill') == "going live"
    admin.ctx.close(); t.ctx.close()


def test_the_studio_of_a_page_not_live_says_so_and_starts_nothing(server, browser):
    t = Tab(browser, server, email="tester@example.com")
    t.page.wait_for_selector("text=Pick up to 5 stories")
    t.page.select_option("#pagesel", "demo-page")
    t.page.wait_for_selector("[data-closed]")
    assert "Demo Page is not live yet: its Studio opens once it has gone live." in t.text("[data-closed]")
    assert t.page.locator('[data-closed] a[href="#/pages/demo-page"]').count() == 1, "the owner is led to it"
    assert t.page.locator("#cont").is_disabled()
    assert "(draft)" in t.page.locator('#pagesel option[value="demo-page"]').inner_text()
    t.page.select_option("#pagesel", "dummy-sports")
    t.page.wait_for_selector("text=Pick up to 5 stories · Dummy Sports")
    assert t.page.locator("[data-closed]").count() == 0
    t.ctx.close()


def test_an_owner_invites_an_editor_and_changes_only_what_they_ticked(server, browser):
    t = Tab(browser, server, email="tester@example.com")
    t.go("#/workspace", "#winv")
    assert "Editors: Demo Workspace" in t.text("#main h2")
    t.page.fill("#winv [name=email]", "helper@example.com")
    t.page.check('#winv input[value="dummy-sports"]')
    t.page.dblclick("#winv button.primary")
    t.page.wait_for_selector("#winvout .linkbox")
    assert "#reset=" in t.text("#winvout .linkbox")
    assert len(t.sent("POST", "/api/workspace/editors")) == 1
    uid = t.page.locator("#editors tr[data-u]").last.get_attribute("data-u")
    row = f'#editors tr[data-u="{uid}"]'
    assert t.page.is_checked(f'{row} input[value="dummy-sports"]')
    t.page.check(f'{row} input[value="demo-page"]')
    t.page.click(f"{row} [data-save]")
    t.toast_is("Saved")
    assert t.sent("PATCH", f"/api/workspace/editors/{uid}")[-1] == {"pages_add": ["demo-page"], "pages_remove": []}
    users = t.call("GET", "/api/workspace")["users"]
    assert sorted(next(u for u in users if u["id"] == uid)["pages"]) == ["demo-page", "dummy-sports"]
    t.ctx.close()


def test_the_super_invites_a_tester_sets_budgets_pauses_and_writes_the_socialpilot_text(server, browser):
    a = Tab(browser, server)
    a.go("#/admin/testers", "#tinv")
    assert a.page.locator('#top a.on[href="#/admin/testers"]').count() == 1
    a.page.fill("#tinv [name=email]", "newtester@example.com")
    a.page.fill("#tinv [name=workspace_name]", "Fresh Workspace")
    a.page.fill("#tinv [name=posts_per_day]", "8")
    a.page.dblclick("#tinv button.primary")
    a.page.wait_for_selector("#tinvout .linkbox")
    assert len(a.sent("POST", "/api/admin/testers")) == 1
    assert a.sent("POST", "/api/admin/testers")[0]["posts_per_day"] == 8
    assert a.page.locator('#testers tr[data-w="fresh-workspace"]').count() == 1
    # budgets: only what was changed in the row is sent
    row = '#testers tr[data-w="demo-workspace"]'
    a.page.fill(f"{row} [name=premium_per_day]", "4")
    a.page.click(f"{row} [data-save]")
    a.toast_is("Saved")
    assert a.sent("PATCH", "/api/admin/workspaces/demo-workspace")[-1] == {"limits": {"premium_per_day": 4}}
    ws = next(w for w in a.call("GET", "/api/admin/workspaces")["workspaces"] if w["id"] == "demo-workspace")
    assert (ws["limits"]["premium_per_day"], ws["limits"]["posts_per_day"]) == (4, 10)
    a.call("PATCH", "/api/admin/workspaces/demo-workspace", {"limits": {"premium_per_day": 6}})
    # every page across the workspaces, with its status
    a.go("#/admin/pages", "#allpages")
    assert a.text('#allpages tr[data-page="dummy-sports"] [data-status]') == "live"
    assert a.text('#allpages tr[data-page="demo-page"] [data-status]') == "draft"
    assert "Demo Workspace" in a.text('#allpages tr[data-page="dummy-sports"]')
    # pause a tester's pages: the tester cannot resume what the product owner paused
    a.go("#/admin/testers", f"{row} [data-pauseall]")
    a.page.click(f"{row} [data-pauseall]")
    a.page.wait_for_selector(f'{row} .pill[data-status="paused"]')
    t = Tab(browser, server, email="tester@example.com")
    r = t.page.evaluate("async () => (await fetch('/api/pages/dummy-sports/resume', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' })).status")
    assert r == 403
    a.call("POST", "/api/pages/dummy-sports/resume", {})
    # the SocialPilot text owners read on the connect step
    a.go("#/admin/settings", "#pset")
    a.page.fill("#pset [name=socialpilot_connect]", CONNECT)
    a.page.click("#pset button.primary")
    a.toast_is("Saved")
    assert t.call("GET", "/api/pages/demo-page")["connect_help"] == CONNECT
    assert not a.errors, a.errors
    a.ctx.close(); t.ctx.close()


def test_the_approvals_page_counts_against_the_page_s_own_budget(server, browser):
    t = Tab(browser, server, email="tester@example.com")
    t.call("PATCH", "/api/pages/dummy-sports", {"posts_per_day": 4})
    t.page.goto(server + "/review.html")
    t.page.wait_for_selector(".tile")
    assert re.search(r"Dummy Sports: \d+/4 today", t.text(".today")), t.text(".today")
    assert "Dish" in t.text("header .brand")
    t.call("PATCH", "/api/pages/dummy-sports", {"posts_per_day": 10})
    t.ctx.close()


def test_open_access_never_shows_a_sign_in_screen(open_server, browser):
    t = Tab(browser, open_server, email=None, at="")
    t.page.wait_for_selector("#top .me")
    t.page.wait_for_selector("text=Pick up to 5 stories")
    assert "product owner" in t.text("#top .me")
    for h in ("#/login", "#/forgot", "#/password", "#/login?next=review&why=idle"):
        t.page.evaluate("h => { location.hash = h; }", h)
        t.page.wait_for_function("h => location.hash !== h", arg=h)
        t.page.wait_for_selector("#top .me")
        t.page.wait_for_timeout(200)
        assert t.page.locator("input[type=password], input[name=email]").count() == 0, h
    assert t.page.locator("#logout").count() == 0 and t.page.locator('#top a[href="#/password"]').count() == 0
    # the open-access owner has a workspace of their own: Pages, Editors and Admin
    for href in ("#/pages", "#/workspace", "#/admin/testers"):
        assert t.page.locator(f'#top a[href="{href}"]').count() == 1, href
    t.page.goto(open_server + "/review.html")
    t.page.wait_for_selector(".tile")
    assert "/#/login" not in t.page.url
    t.ctx.close()


SCREENS = {"admin@example.com": ["#/admin/testers", "#/admin/pages", "#/admin/users", "#/admin/settings", "#/admin/health", "#/admin/audit",
                                 "#/admin/page/dummy-sports", "#/templates", "#/studio"],
           "tester@example.com": ["#/pages", "#/workspace", "#/pages/demo-page/details", "#/pages/demo-page/connect",
                                  "#/pages/demo-page/documents", "#/pages/demo-page/review", "#/pages/dummy-sports/status"]}


@pytest.mark.parametrize("width", WIDTHS)
def test_every_screen_has_the_look_and_fits_the_screen(server, browser, width):
    t = Tab(browser, server, email=None, width=width)
    t.page.wait_for_selector("input[name=email]")
    t.fits("sign in")
    assert t.text("#top .brand") == "Dish"
    look = t.page.evaluate("""() => { const b = getComputedStyle(document.body), h = getComputedStyle(document.querySelector('.login h1'));
      return [b.backgroundColor, b.color, h.color, getComputedStyle(document.querySelector('.login .btn.primary')).backgroundColor]; }""")
    assert look[:3] == ["rgb(255, 255, 255)", "rgb(15, 61, 46)", "rgb(15, 61, 46)"], look
    assert re.match(r"rgb\((\d+), (\d+), (\d+)\)", look[3]) and (lambda r, g, b: g > r and g > b)(*map(int, re.findall(r"\d+", look[3]))), f"a green button, not {look[3]}"
    t.ctx.close()
    for email, screens in SCREENS.items():
        t = Tab(browser, server, email=email, width=width)
        for h in screens:
            t.go(h, "#main .panel, #main .card")
            t.page.wait_for_timeout(300)
            t.fits(h)
            assert t.text("#top .brand") == "Dish"
        if email.startswith("tester"):
            t.go("#/pages/demo-page/review", "#golive")
            red = t.page.evaluate("() => getComputedStyle(document.querySelector('#golive')).backgroundColor")
            r, g, b = map(int, re.findall(r"\d+", red)[:3])
            assert r > 150 and g < 80 and b < 80, f"the Go live button is red, not {red}"
        t.page.goto(server + "/review.html")
        t.page.wait_for_selector("header .brand")
        t.fits("approvals")
        assert not t.errors, (email, t.errors)
        t.ctx.close()


def test_the_owner_changes_the_sources_and_playbook_the_routine_proposed(server, browser):
    admin = Tab(browser, server)
    new_tester(admin, server, "proposed@example.com", "Proposed Workspace")
    t = Tab(browser, server, email="proposed@example.com", width=390)
    slug = ready_page(t, "Demo Proposed")
    job = t.call("POST", f"/api/pages/{slug}/go-live", {})["onboarding"]["job"]["id"]
    on = lambda act, body=None: routine(server, "POST", f"/api/routine/onboard/jobs/{job}/{act}", body or {})
    on("claim")
    on("result", {"socialpilot_login_id": "5559001", "playbook": {"voice": "calm", "never": ["rumours"]},
                  "sources": [{"type": "website", "url": "https://example.com/hobby-news", "label": "Hobby news"},
                              {"type": "search", "url": "made-up hobby news today"}]})
    on("done")
    t.go(f"#/pages/{slug}/sources", "#osrc")
    assert t.page.locator("#osrc tr[data-s]").count() == 2
    t.fits("sources")
    # one switched off: only that one changes
    t.page.locator('#osrc tr[data-s] [data-active]').first.uncheck()
    t.toast_is("Saved")
    srcs = t.call("GET", f"/api/pages/{slug}/sources")["sources"]
    assert [x["active"] for x in srcs] == [False, True]
    # the playbook: broken JSON is never sent, valid JSON is
    assert json.loads(t.page.input_value("#opb [name=playbook]")) == {"voice": "calm", "never": ["rumours"]}
    n = len(t.requests)
    t.page.fill("#opb [name=playbook]", '{"voice": "calm",')
    t.page.click("#opb button.primary")
    t.toast_is("The playbook is not valid JSON")
    assert not t.sent("PATCH", f"/api/pages/{slug}$", n)
    t.page.fill("#opb [name=playbook]", '{"voice": "warm", "never": ["rumours"]}')
    t.page.dblclick("#opb button.primary")
    t.toast_is("Saved: used from the next hourly run")
    assert t.sent("PATCH", f"/api/pages/{slug}$", n) == [{"playbook": {"voice": "warm", "never": ["rumours"]}}]
    assert t.call("GET", f"/api/pages/{slug}")["page"]["playbook"] == {"voice": "warm", "never": ["rumours"]}
    assert not t.errors, t.errors
    admin.ctx.close(); t.ctx.close()


def test_text_typed_when_the_session_ran_out_comes_back_after_signing_in(server, browser):
    t = Tab(browser, server, email="tester@example.com")
    t.go("#/pages/demo-page/details", "#pdet")
    was = t.page.input_value("#pdet [name=niche]")
    t.ctx.clear_cookies()                                              # (the session ran out)
    t.page.fill("#pdet [name=niche]", "Made-up news about a made-up hobby, told kindly.")
    t.page.click("#pdet button.primary")
    t.page.wait_for_selector("input[name=email]")
    t.page.fill("input[name=email]", "tester@example.com")
    t.page.fill("input[name=password]", PW)
    t.page.click("button.btn.primary")
    t.page.wait_for_selector("#pdet")
    t.page.wait_for_function("() => document.querySelector('#pdet [name=niche]').value.endsWith('told kindly.')")
    t.toast_is("You were signed out: your changes are back")
    assert t.call("GET", "/api/pages/demo-page")["page"]["niche"] == was, "nothing saved while signed out"
    n = len(t.requests)
    t.page.click("#pdet button.primary")
    t.page.wait_for_selector("#spdone")                                # (a draft page: on to the next step)
    assert t.sent("PATCH", "/api/pages/demo-page$", n) == [{"niche": "Made-up news about a made-up hobby, told kindly."}]
    t.call("PATCH", "/api/pages/demo-page", {"niche": was})
    t.ctx.close()
