"""The onboarding routine's commands (bore/onboard.py, ONBOARD.md), against a fake dashboard: one job
per session, every step reported to the tester, the scheduler account only when exactly one Facebook
account matches and it is neither denied nor another page's, sources active this month and within
the page's budget, a playbook the engine can read, and a first run that never touches the saved
state. Every page, account and source here is made up."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

from bore import cli, ops
from bore import onboard as O
from bore import studio as S

ROOT = Path(__file__).resolve().parent.parent
PAGE = {"slug": "demo-page", "name": "Demo Page", "workspace": "w1", "fb_page_url": "https://www.facebook.com/demopage",
        "niche": "Made-up league news.", "language": "en", "timezone": "America/New_York", "posts_per_day": 10,
        "premium_per_day": 6, "status": "going_live"}
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Reply:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class FakeDash:
    """The dashboard's /api/routine/onboard/* as docs/PRODUCT.md and README describe them."""

    def __init__(self, jobs=(), taken=()):
        self.url, self.calls, self.jobs, self.taken = "https://dash.test", [], list(jobs), set(taken)
        self.stopped = False
        self.s = NS(get=self._get, post=self._post)

    def _get(self, url, timeout=None, params=None):
        path = url.split("/api/routine/onboard/")[1]
        self.calls.append(("GET", path, None))
        return Reply(200, {"jobs": self.jobs})

    def _post(self, url, json=None, timeout=None):
        path = url.split("/api/routine/onboard/")[1]
        self.calls.append(("POST", path, json))
        if self.stopped:
            return Reply(409, {"error": "the page was paused"})
        if path == "claim":
            if json["id"] in self.taken:
                return Reply(409, {"error": "another session has it"})
            return Reply(200, {"job": {"id": json["id"]}, "page": PAGE, "used_login_ids": [777]})
        if path == "result" and json.get("templates"):
            return Reply(200, {"studio_job": "tpl-1"})
        if path == "done":
            return Reply(200, {"status": "live"})
        return Reply(200, {"ok": True})

    def posted(self, path):
        return [b for m, p, b in self.calls if m == "POST" and p == path]


@pytest.fixture
def ob(monkeypatch, tmp_path):
    monkeypatch.setattr(O, "WORK", tmp_path / "work" / "onboard")
    monkeypatch.setattr(S, "sync", lambda dash=None, quiet=False: [])
    meta = {"demo-page": {"name": "Demo Page", "docs": {"tone": {"file": "private/docs/demo-page/tone.txt", "version": 1},
                                                        "templates": {"file": "private/docs/demo-page/templates.txt", "version": 2}},
                          "socialpilot_login_id": None},
            "other-page": {"name": "Other Page", "docs": {}, "socialpilot_login_id": "888"}}
    monkeypatch.setattr(S, "studio_meta", lambda: meta)
    monkeypatch.setattr(S, "doc_pages", lambda slug, t, dash=None: {"images": [f"private/docs/{slug}/{t}/page-01.png"]})
    sp = tmp_path / "socialpilot.yaml"
    sp.write_text("deny: [999]\naccounts: {}\n")
    real = O.denied_login_ids
    monkeypatch.setattr(O, "denied_login_ids", lambda path=None: real(sp))
    dash = FakeDash(jobs=[{"id": "ob-1", "page": "demo-page"}])
    monkeypatch.setattr(O, "_dash", lambda: dash)
    return NS(dash=dash, meta=meta, tmp=tmp_path)


def _claimed(ob):
    assert O.pull_job(ob.dash) is not None
    return json.loads((O.WORK / "demo-page" / "brief.json").read_text())


# ------------------------------------------------------------------ jobs and steps
def test_one_job_is_claimed_and_its_brief_says_what_to_produce(ob, capsys):
    ob.dash.jobs = [{"id": "ob-0", "page": "taken-page"}, {"id": "ob-1", "page": "demo-page"}]
    ob.dash.taken = {"ob-0"}
    b = _claimed(ob)
    out = capsys.readouterr().out
    assert "SKIP   ob-0" in out and "JOB" in out and "DOCS MISSING" not in out
    assert [p for m, p, _ in ob.dash.calls if m == "POST"] == ["claim", "claim"]      # one job per session
    assert b["job"] == "ob-1" and b["page"]["name"] == "Demo Page" and b["page"]["posts_per_day"] == 10
    assert b["docs"]["templates"]["images"] == ["private/docs/demo-page/templates/page-01.png"]
    assert "images" not in b["docs"]["tone"] and b["missing_docs"] == []
    assert b["files"]["sources"].endswith("demo-page/sources.json") and b["source_limits"] == O.SOURCE_LIMITS
    assert set(b["playbook_keys"]) == set(O.PLAYBOOK_TYPES) and b["used_login_ids"] == ["777"]


def test_a_job_this_machine_could_not_prepare_goes_back_and_a_new_job_starts_its_steps_over(ob, monkeypatch):
    def broken(dash=None, quiet=False):
        raise ConnectionError("the network went away")
    monkeypatch.setattr(S, "sync", broken)
    with pytest.raises(ConnectionError):
        O.pull_job(ob.dash)
    claim, back = ob.dash.posted("claim")
    assert back["release"] is True and back["id"] == "ob-1" and "ConnectionError" in back["error"]
    monkeypatch.setattr(S, "sync", lambda dash=None, quiet=False: [])
    _claimed(ob)
    O._record("demo-page", "link", True)
    _claimed(ob)                                                 # the same job again (this session): kept
    assert O._done_steps("demo-page") == {"link": True}
    ob.dash.jobs = [{"id": "ob-2", "page": "demo-page"}]         # Try again: a new job
    _claimed(ob)
    assert O._done_steps("demo-page") == {}


def test_a_missing_document_is_said_and_no_job_is_a_line(ob, capsys):
    del ob.meta["demo-page"]["docs"]["tone"]
    assert _claimed(ob)["missing_docs"] == ["tone"]
    assert "DOCS MISSING: tone" in capsys.readouterr().out
    ob.dash.jobs = []
    assert O.pull_job(ob.dash) is None and "no onboard jobs waiting" in capsys.readouterr().out


def test_steps_are_reported_with_the_job_and_a_short_plain_detail(ob):
    with pytest.raises(O.Refused, match="onboard jobs"):
        O.step("demo-page", "link", True, "x")                   # nothing claimed here
    _claimed(ob)
    O.step("demo-page", "research", True, "  found   12 sources " + "x" * 400)
    body, = ob.dash.posted("step")
    assert body["id"] == "ob-1" and body["page"] == "demo-page" and body["step"] == "research" and body["ok"] is True
    assert body["detail"].startswith("found 12 sources") and len(body["detail"]) == O.DETAIL_MAX
    with pytest.raises(O.Refused, match="unknown step"):
        O.step("demo-page", "publish", True)
    with pytest.raises(O.Refused, match="slug"):
        O.step("../etc", "link", True)


def test_a_stopped_onboarding_stops_the_session(ob):
    _claimed(ob)
    ob.dash.stopped = True                                      # the tester paused the page meanwhile
    with pytest.raises(SystemExit, match="STOPPED.*paused.*Do nothing more"):
        O.run(NS(step="step", id="demo-page", what="research", ok=True, fail=False, detail="x"), {})


# ------------------------------------------------------------------ the SocialPilot account
def _accounts(tmp, *accts):
    f = tmp / "accounts.json"
    f.write_text(json.dumps([{"type": "text", "text": json.dumps({"success": True, "accounts": list(accts)})}]))
    return f


FB = lambda lid, name, **k: {"platform": "Facebook", "isReconnect": False, "accountUsername": name, "loginId": lid, **k}


def test_the_one_matching_account_is_linked_and_the_step_reported(ob):
    _claimed(ob)
    f = _accounts(ob.tmp, FB(101, "Demo Page"), FB(102, "Someone Else"), {"platform": "Instagram", "accountUsername": "Demo Page", "loginId": 103})
    O.link("demo-page", "101", "demo page", f)
    res, = ob.dash.posted("result")
    assert res == {"id": "ob-1", "page": "demo-page", "socialpilot_login_id": "101", "socialpilot_name": "Demo Page"}
    assert ob.dash.posted("step")[-1]["step"] == "link" and O._done_steps("demo-page")["link"] is True
    # matched by the page's own Facebook address when the names differ
    f = _accounts(ob.tmp, FB(104, "DP Official", profileUrl="https://www.facebook.com/demopage/"))
    O.link("demo-page", "104", "DP Official", f)
    assert ob.dash.posted("result")[-1]["socialpilot_login_id"] == "104"


@pytest.mark.parametrize("accts, lid, name, why", [
    ([FB(102, "Someone Else")], "102", "Someone Else", "NO MATCH: connect Demo Page in SocialPilot .*Try again"),
    ([FB(101, "Demo Page"), FB(105, "Demo-Page")], "101", "Demo Page", "SEVERAL MATCHES"),
    ([FB(999, "Demo Page")], "999", "Demo Page", "DENIED"),                    # config/socialpilot.yaml deny
    ([FB(888, "Demo Page")], "888", "Demo Page", "IN USE"),                    # another page's account
    ([FB(777, "Demo Page")], "777", "Demo Page", "IN USE"),                    # the dashboard says so too
    ([FB(101, "Demo Page")], "106", "Demo Page", "matching Demo Page is 101"),
    ([FB(101, "Demo Page")], "101", "Other Name", "--name must be"),
    ([FB(101, "Demo Page", isReconnect=True)], "101", "Demo Page", "needs reconnecting"),
    ([FB(101, "Demo Page")], "abc", "Demo Page", "numeric loginId"),
])
def test_an_account_that_is_not_exactly_the_pages_own_is_never_saved(ob, accts, lid, name, why):
    _claimed(ob)
    with pytest.raises(O.Refused, match=why):
        O.link("demo-page", lid, name, _accounts(ob.tmp, *accts))
    assert ob.dash.posted("result") == [] and "link" not in O._done_steps("demo-page")


def test_the_deny_list_reads_the_products_config(tmp_path):
    f = tmp_path / "sp.yaml"
    f.write_text("deny: [11]\nblocked_login_ids: [12]\n")
    assert {"11", "12"} <= O.denied_login_ids(f)
    assert O.denied_login_ids(tmp_path / "none.yaml") >= set()


def test_an_accountlist_file_with_no_result_is_refused(ob):
    _claimed(ob)
    f = ob.tmp / "a.json"
    f.write_text("I could not call it")
    with pytest.raises(O.Refused, match="no AccountList result"):
        O.link("demo-page", "101", "Demo Page", f)


# ------------------------------------------------------------------ sources
GOOD = {"x": [{"handle": "@DemoReporter", "role": "reporter", "last_post": TODAY},
              {"handle": "demoleague", "role": "official", "last_post": TODAY}],
        "subreddits": [{"name": "r/DemoLeague", "last_post": TODAY}],
        "web_searches": [{"query": "Demo League  news today"}],
        "websites": [{"url": "https://demo-news.example/league", "last_post": TODAY}]}


def test_sources_become_the_dashboards_source_records(ob, tmp_path):
    _claimed(ob)
    f = tmp_path / "sources.json"
    f.write_text(json.dumps(GOOD))
    O.save_sources("demo-page", f, {})
    res, = ob.dash.posted("result")
    got = {(s["type"], s["url"], s["label"]) for s in res["sources"]}
    assert got == {("social", "https://x.com/demoreporter", "reporter"), ("social", "https://x.com/demoleague", "official"),
                   ("reddit", "https://www.reddit.com/r/demoleague/", "Reddit"), ("search", "Demo League news today", "web search"),
                   ("website", "https://demo-news.example/league", "website")}
    assert res["pipeline"] == {"tier_one": ["demoreporter"], "officials": ["demoleague"]}
    assert all(s["active"] and s["added_by"] == "onboarding" for s in res["sources"])
    # what the collector makes of them (the same records an owner adds by hand)
    page = S.page_from_config({"slug": "demo-page", "sources": res["sources"], "pipeline": res["pipeline"]})
    assert page.x_handles == ["demoreporter", "demoleague"] and page.tier_one == ["demoreporter"]
    assert page.subreddits == ["demoleague"] and "site:demo-news.example/league" in page.web_searches
    assert ob.dash.posted("step")[-1]["detail"] == "2 X accounts, 1 subreddits, 1 web searches, 1 websites"


def test_a_source_quiet_this_month_or_over_the_budget_is_refused(ob):
    old = datetime.fromtimestamp(time.time() - 40 * 86400, timezone.utc).strftime("%Y-%m-%d")
    with pytest.raises(O.Refused, match=r"over 31 days ago: a source must be active this month"):
        O.check_sources({**GOOD, "subreddits": [{"name": "demoleague", "last_post": old}]})
    with pytest.raises(O.Refused, match="last_post must be the date"):
        O.check_sources({**GOOD, "websites": [{"url": "https://demo-news.example"}]})
    many = [{"handle": f"demo{i}", "role": "other", "last_post": TODAY} for i in range(31)]
    with pytest.raises(O.Refused, match="31 x: the page's budget allows at most 30"):
        O.check_sources({**GOOD, "x": many})
    with pytest.raises(O.Refused, match="allows at most 2"):            # the product's config may set lower limits
        O.check_sources({**GOOD, "subreddits": [{"name": f"demo{i}", "last_post": TODAY} for i in range(3)]},
                        {"onboard": {"source_limits": {"subreddits": 2}}})
    assert O.limits({"onboard": {"source_limits": {"x": 99}}})["x"] == 30, "never higher than the defaults"


@pytest.mark.parametrize("bad, why", [
    ({"x": [{"handle": "not a handle!", "last_post": TODAY}]}, "not a handle"),
    ({"x": [{"handle": "demo", "role": "boss", "last_post": TODAY}]}, "role must be one of"),
    ({"x": [{"handle": "demo", "last_post": TODAY}, {"handle": "@Demo", "last_post": TODAY}]}, "listed twice"),
    ({"websites": [{"url": "https://x.com/demo", "last_post": TODAY}]}, "go in their own lists"),
    ({"websites": [{"url": "demo-news", "last_post": TODAY}]}, "not a web address"),
    ({"web_searches": [{"query": "site:demo-news.example"}], "x": [{"handle": "demo", "last_post": TODAY}]}, "search words"),
    ({"web_searches": [{"query": "Demo League"}]}, "nothing to read"),
    ({"instagram": [], "x": [{"handle": "demo", "last_post": TODAY}]}, "unknown keys"),
])
def test_sources_that_cannot_be_collected_are_refused(bad, why):
    with pytest.raises(O.Refused, match=why):
        O.check_sources(bad)


# ------------------------------------------------------------------ playbook
def test_the_example_playbook_is_one_the_engine_reads():
    pb = yaml.safe_load((ROOT / "config" / "playbooks" / "example.yaml").read_text())
    assert O.check_playbook({**pb, "page": "demo-page"}, "demo-page")


@pytest.mark.parametrize("change, why", [
    ({"tone_of_voice": "x"}, "unknown key 'tone_of_voice'"),
    ({"voice": ""}, "voice is required"),
    ({"caption_rules": "one rule"}, "caption_rules must be a list"),
    ({"page": "other-page"}, "must be this page's slug"),
    ({"prefilter": {"skip": ["(unclosed"]}}, "not a valid pattern"),
    ({"lint": {"hashtags": {"min": 5, "max": 2}}}, "0 <= min <= max"),
    ({"lint": {"hashtags": {"required": "DemoPage"}}}, "one hashtag"),
    ({"card": {"templates": ["made_up_layout"]}}, "card.templates: only layouts the renderer draws"),
    ({"kinds": [{"label": "No id"}]}, "needs an id"),
    ({"style_refs": ["http://demo.example/a.jpg"]}, "https://"),
])
def test_a_playbook_the_engine_would_misread_is_refused(change, why):
    pb = yaml.safe_load((ROOT / "config" / "playbooks" / "example.yaml").read_text())
    pb.pop("page")
    with pytest.raises(O.Refused, match=why):
        O.check_playbook({**pb, **change}, "demo-page")


def test_a_good_playbook_is_saved_and_reported(ob, tmp_path):
    _claimed(ob)
    pb = yaml.safe_load((ROOT / "config" / "playbooks" / "example.yaml").read_text())
    pb.pop("page")
    f = tmp_path / "playbook.yaml"
    f.write_text(yaml.safe_dump(pb))
    O.save_playbook("demo-page", f)
    assert ob.dash.posted("result")[0]["playbook"]["voice"] == pb["voice"]
    assert ob.dash.posted("step")[-1]["step"] == "playbook"


# ------------------------------------------------------------------ templates, first run, done, fail
def test_templates_are_queued_through_the_result_route(ob, capsys):
    _claimed(ob)
    O.run(NS(step="templates", id="demo-page", what=None), {})
    assert ob.dash.posted("result")[-1] == {"id": "ob-1", "page": "demo-page", "templates": True}
    assert "Studio job tpl-1" in capsys.readouterr().out


def test_the_first_run_is_this_page_only_on_a_fresh_database(ob, monkeypatch, tmp_path, cfg):
    _claimed(ob)
    with pytest.raises(O.Refused, match="needs the link, sources, playbook"):
        O.first_run("demo-page", cfg, ob.dash)
    for s in ("link", "sources", "playbook"):
        O._record("demo-page", s, True)
    private = tmp_path / "private"
    (private / "playbooks").mkdir(parents=True)
    (private / "playbooks" / "demo-page.yaml").write_text("voice: Short.\n")
    import bore.config as C
    import bore.routine as R
    monkeypatch.setattr(C, "PRIVATE", private)
    monkeypatch.setattr(R, "load_playbook", lambda slug: {"voice": "Short."} if slug == "demo-page" else {})
    pages = [C.Page(slug="other-page", name="Other Page"), C.Page(slug="demo-page", name="Demo Page", hourly=False)]
    monkeypatch.setattr(C, "load_pages", lambda path=None: pages)
    saved = {}
    monkeypatch.setattr(C, "save_pages", lambda ps, path=None: saved.update(pages=ps, path=path))
    cfg["db_path"] = str(tmp_path / "data" / "bore.db")
    (tmp_path / "data").mkdir()
    marker = tmp_path / "data" / ".restored"
    marker.write_text("{}")                                    # a pull ran in this session somehow
    real = NS(link_requests=lambda: [{"id": "l1", "page": "demo-page"}, {"id": "l2", "page": "other-page"}],
              regen_requests=lambda: [{"id": "r1", "page": "other-page"}])
    seen = {}

    run = tmp_path / "run"
    for i in (1, 2):
        (run / f"demo-page-{i}").mkdir(parents=True)
        (run / f"demo-page-{i}" / "brief.json").write_text("{}")
    monkeypatch.setattr(R, "latest_run_dir", lambda: run)

    def prepare(args, cfg, R):
        d = cli._dashboard()
        seen.update(page=args.page, fresh=args.fresh, collect=not args.no_collect,
                    links=[x["id"] for x in d.link_requests()], regens=d.regen_requests(),
                    reported_before=[b["step"] for b in ob.dash.posted("step")])
    monkeypatch.setattr(cli, "_routine_prepare", prepare)
    before = cli._dashboard
    O.first_run("demo-page", cfg, NS(url=ob.dash.url, s=ob.dash.s, **vars(real)))
    assert seen == {"page": "demo-page", "fresh": True, "collect": True, "links": ["l1"], "regens": [], "reported_before": []}
    assert [p.slug for p in saved["pages"]] == ["demo-page"] and saved["pages"][0].hourly is True
    assert saved["path"] == private / "pages.yaml"
    assert not marker.exists(), "never a push over the saved state from the first run"
    assert cli._dashboard is before
    last = ob.dash.posted("step")[-1]
    assert last["step"] == "first_run" and last["detail"] == "2 stories picked for the first posts", "reported once briefed"


def test_done_waits_for_every_step_and_fail_needs_a_reason_the_tester_can_act_on(ob, capsys):
    _claimed(ob)
    with pytest.raises(O.Refused, match="not done yet: link, sources, playbook, templates, first_run"):
        O.done("demo-page")
    with pytest.raises(O.Refused, match="reason they can act on"):
        O.fail("demo-page", "no")
    O._record("demo-page", "link", True)
    O.fail("demo-page", "connect Demo Page in SocialPilot, then press Try again")
    body, = ob.dash.posted("fail")
    assert body["step"] == "documents" and body["reason"].startswith("connect Demo Page") and body["id"] == "ob-1"
    for s in O.NEEDED:
        O._record("demo-page", s, True)
    O.run(NS(step="done", id="demo-page", what=None), {})
    assert ob.dash.posted("done") == [{"id": "ob-1", "page": "demo-page"}] and "demo-page: live" in capsys.readouterr().out


def test_onboarding_commands_report_as_their_own_routine():
    assert ops.routine_for("onboard link", "demo-page") == "onboarding"
    assert ops.routine_for("onboard first-run", "demo-page") == "onboarding"


def test_a_session_without_the_restored_database_never_spends_krea_unchecked(monkeypatch, tmp_path, store, cfg):
    """The first run works on a fresh database: when the shared count cannot be asked, it has no
    ledger of its own to count on, so it draws the template card (as Studio does)."""
    from bore import routine as R
    d = tmp_path / "work" / "run1" / "demo-page-1"
    d.mkdir(parents=True)
    (d / "copy.json").write_text("{}")
    (d / "brief.json").write_text("{}")
    monkeypatch.setattr(R, "WORK", tmp_path / "work")
    monkeypatch.setattr(cli, "_lint_or_exit", lambda d, cfg, R: None)
    monkeypatch.setattr(cli, "_store", lambda cfg: store)
    monkeypatch.setattr(R, "krea_request", lambda d, cfg, url, fb: {"prompt": "p"})

    class Down:
        def krea_reserve(self, *a, **k):
            raise ConnectionError("down")
    monkeypatch.setattr(cli, "_dashboard", lambda: Down())
    cfg["db_path"] = str(tmp_path / "t.db")
    args = NS(dir=str(d), upload_url="", feedback="")
    with pytest.raises(SystemExit, match="KREA UNCHECKED.*fallback"):
        cli._routine_krea_request(args, cfg, R)
    (tmp_path / ".restored").write_text(json.dumps({"counts": {}}))     # the hourly routine's restored database
    cli._routine_krea_request(args, cfg, R)                             # goes on, on its own ledger
    ops._DEGRADED.clear()
