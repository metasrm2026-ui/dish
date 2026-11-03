"""Editors' links and regenerations are never left behind: `routine check` refuses to finish while
one is neither made nor reported back (3 of the 4 links editors ever sent waited 7-23 hours, Oct
7-16, while every hourly check said OK), `routine skip` refuses them, an Apify problem does not stop
them, and a link's own image is usable at article size; X links bring their photos."""
import json
import types

import pytest

from bore import cli
from bore.config import Page

from conftest import FIXTURES


def _brief(d, page, **extra):
    d.mkdir(parents=True)
    (d / "brief.json").write_text(json.dumps({"id": d.name, "page": page, "rank": 1, "backup": False,
                                              "headline_hint": "h", "photos": [], **extra}))
    return d


def _run(tmp_path, quota):
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"quota": quota}))
    return run


def _check(run):
    cli._routine_check(types.SimpleNamespace(dir=str(run)), {}, None)


def test_an_open_link_or_regeneration_stops_the_check_whatever_the_quota(tmp_path, capsys):
    run = _run(tmp_path, {"culture-rush": 0, "huddle": 1})
    link = _brief(run / "culture-rush-link-x", "culture-rush", link={"request_id": "r1", "url": "https://x.com/a/status/1"})
    regen = _brief(run / "huddle-regen-y", "huddle", regeneration={"replaces": "huddle-old"})
    (_brief(run / "huddle-pick-z", "huddle") / "uploaded").write_text("{}")
    with pytest.raises(SystemExit, match="NOT DONE") as e:
        _check(run)
    assert "culture-rush-link-x" in str(e.value) and "huddle-regen-y" in str(e.value)
    (link / "uploaded").write_text("{}")                       # made
    (regen / cli.REQUEST_FAILED).write_text("{}")             # reported back to the reviewer
    _check(run)
    out = capsys.readouterr().out
    assert "check: OK" in out and "1 reported back to the editor" in out


def test_skip_refuses_an_editors_request(tmp_path, cfg):
    d = _brief(tmp_path / "culture-rush-link-x", "culture-rush", link={"request_id": "r1", "url": "https://x.com/a/status/1"})
    with pytest.raises(SystemExit, match="REFUSED.*link-fail"):
        cli._routine_skip(types.SimpleNamespace(dir=str(d), reason="off-niche", photo=False), cfg, None)
    assert not (d / "skip.json").exists()


def test_a_decision_skip_in_copy_is_refused_for_an_editors_request(tmp_path, cfg, monkeypatch):
    from bore import routine as R
    d = _brief(tmp_path / "huddle-regen-y", "huddle", regeneration={"replaces": "huddle-old"})
    (d / "copy.json").write_text(json.dumps({"decision": "skip", "reason": "weak story"}))
    monkeypatch.setattr(R, "load_playbook", lambda slug: {})
    with pytest.raises(SystemExit, match="REFUSED"):
        cli._lint_or_exit(d, cfg, R)
    assert not (d / "skip.json").exists()


def test_link_fail_tells_the_editor_and_leaves_a_marker(tmp_path, cfg, monkeypatch):
    d = _brief(tmp_path / "culture-rush-link-x", "culture-rush", link={"request_id": "r1", "url": "https://x.com/a/status/1"})
    told = []
    monkeypatch.setattr(cli, "_dashboard", lambda: types.SimpleNamespace(link_failed=lambda rid, why: told.append((rid, why)) or {"ok": True}))
    cli._routine_link_fail(types.SimpleNamespace(dir=str(d), reason="the page cannot be read"), cfg, None)
    assert told == [("r1", "the page cannot be read")] and json.loads((d / cli.REQUEST_FAILED).read_text())["reason"]


def _prepare_env(tmp_path, cfg, store, monkeypatch):
    """prepare with one link and one regeneration waiting, no Apify token, a dashboard stub."""
    import bore.config
    import bore.linkpost
    from bore import routine as R
    page = Page.from_dict({"slug": "demo", "name": "Demo", "hourly": True})
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.setattr(bore.config, "load_env", lambda *a, **k: None)
    monkeypatch.setattr(cli, "load_pages", lambda *a: [page])
    monkeypatch.setattr(bore.config, "load_pages", lambda *a: [page])
    monkeypatch.setattr(cli, "_store", lambda c: store)
    monkeypatch.setattr(R, "WORK", tmp_path / "work")
    monkeypatch.setattr(R, "load_playbook", lambda slug: {})
    monkeypatch.setattr(bore.linkpost, "read_link", lambda url, apify=None, cfg=None: {
        "platform": "web", "author": "news.example.org", "title": "A story", "text": "What happened.", "images": []})

    def regen_brief(run, req, cfg):
        d = run / f"demo-regen-{req['id']}"
        (d / "photos").mkdir(parents=True, exist_ok=True)
        (d / "brief.json").write_text(json.dumps({"id": d.name, "page": "demo", "regeneration": {
            "replaces": req["id"], "feedback": "bigger face", "reference_photo_index": None}}))
        return d
    monkeypatch.setattr(R, "write_regen_brief", regen_brief)
    dash = types.SimpleNamespace(
        created_today=lambda c: {}, recent_drafts=lambda h: [], editor_feedback=lambda days, cap=12: {},
        regen_requests=lambda: [{"id": "demo-old"}],
        link_requests=lambda: [{"id": "r1", "page": "demo", "url": "https://news.example.org/a"}])
    monkeypatch.setattr(cli, "_dashboard", lambda: dash)
    return types.SimpleNamespace(no_collect=False, page=None, fresh=True), R


def _briefs(tmp_path):
    (run,) = (tmp_path / "work").iterdir()
    return run, sorted(p.name for p in run.iterdir() if (p / "brief.json").exists())


def test_prepare_without_an_apify_token_still_writes_the_editors_briefs(tmp_path, cfg, store, monkeypatch, capsys):
    args, R = _prepare_env(tmp_path, cfg, store, monkeypatch)
    cli._routine_prepare(args, cfg, R)
    out = capsys.readouterr().out
    # this line's shape is what the run report reads as degraded (bore/ops.py DEGRADED_LINES)
    assert any(ln.startswith("Apify unavailable (APIFY_TOKEN is not set") and "collection skipped" in ln
               for ln in out.splitlines())
    run, briefs = _briefs(tmp_path)
    assert len(briefs) == 2 and "demo-regen-demo-old" in briefs and any("-link-" in b for b in briefs)
    assert json.loads((run / "run.json").read_text())["links"] == ["r1"]


def test_prepare_reports_the_hourly_ledger_to_the_shared_krea_count(tmp_path, cfg, store, monkeypatch, capsys):
    """Studio uses Krea only once the hourly routine has counted the month: every prepare reports."""
    args, R = _prepare_env(tmp_path, cfg, store, monkeypatch)
    cfg["db_path"] = str(tmp_path / "t.db")
    (tmp_path / ".restored").write_text(json.dumps({"counts": {}}))   # this session's pull restored it
    asked = []
    cli._dashboard().krea_reserve = lambda who, cap, key=None, ledger=None, report_only=False: asked.append(
        (who, report_only, ledger)) or {"used": 3, "today": 1, "allowed": 41, "studio": 1}
    cli._routine_prepare(args, cfg, R)
    assert [(w, r) for w, r, _ in asked] == [("hourly", True)] and asked[0][2]["hourly_used"] == 0
    assert "Krea images this month (hourly routine and Studio): 3/" in capsys.readouterr().out


def test_the_editors_briefs_are_written_before_collection_and_selection_can_fail(tmp_path, cfg, store, monkeypatch):
    """Whatever breaks in collection or selection, the editors' links and regenerations are briefed."""
    import bore.select
    args, R = _prepare_env(tmp_path, cfg, store, monkeypatch)

    def broken(*a, **k):
        raise RuntimeError("selection crashed")
    monkeypatch.setattr(bore.select, "select_page", broken)
    with pytest.raises(RuntimeError, match="selection crashed"):
        cli._routine_prepare(args, cfg, R)
    _, briefs = _briefs(tmp_path)
    assert "demo-regen-demo-old" in briefs and any("-link-" in b for b in briefs)


def test_a_rejected_apify_token_skips_collection_but_a_bug_is_not_hidden(tmp_path, cfg, store, monkeypatch, capsys):
    import requests

    from bore import budget
    args, R = _prepare_env(tmp_path, cfg, store, monkeypatch)
    monkeypatch.setenv("APIFY_TOKEN", "rotated")

    def rejected(apify, store, now=None):
        raise requests.HTTPError("401 Client Error: Unauthorized for url: https://api.apify.com/v2/users/me/limits")
    monkeypatch.setattr(budget, "make_pacer", rejected)
    cli._routine_prepare(args, cfg, R)
    assert "Apify unavailable (HTTPError: 401 Client Error" in capsys.readouterr().out

    def bug(apify, store, now=None):
        raise TypeError("unsupported operand type(s)")
    monkeypatch.setattr(budget, "make_pacer", bug)
    with pytest.raises(TypeError):
        cli._routine_prepare(args, cfg, R)


def _lint(size, source):
    from bore.guard import lint
    brief = {"id": "culture-rush-link-x", "page": "culture-rush", "posts": [{"platform": "web", "text": "A story."}],
             "photos": [{"index": 0, "file": "photos/0.jpg", "size": size, "source": source}],
             "link": {"request_id": "r1", "url": "https://news.example.org/a"}}
    copy = {"headline_lines": ["A STORY"], "caption": "A story.", "photo_index": 0, "template": "headline"}
    return lint(brief, copy, {}, 800)


def test_a_links_own_article_image_only_warns():
    res = _lint("1200x630", "link")
    assert not [e for e in res["errors"] if "floor" in e]
    assert any("link's own image is 1200x630" in w for w in res["warnings"])
    assert any("below the 800px floor" in e for e in _lint("640x360", "link")["errors"])     # too soft for a card
    assert any("below the 800px floor" in e for e in _lint("1200x630", "web")["errors"])     # any other photo


def test_an_x_link_brings_its_photos(monkeypatch):
    from bore import linkpost
    post = json.loads((FIXTURES / "x_syndication_20261021.json").read_text())
    calls = []

    class R:
        def __init__(self, url, data):
            self.url, self.data, self.status_code = url, data, 200

        def raise_for_status(self):
            if self.data is None:
                raise linkpost.requests.HTTPError("403")

        def json(self):
            return self.data

    def get(url, params=None, **k):
        calls.append(url)
        if "syndication" in url:
            assert params["id"] == "1000000000000000001" and params["token"]
            return R(url, post)
        return R(url, None)                 # oEmbed refused: the syndication text is used
    monkeypatch.setattr(linkpost.requests, "get", get)
    got = linkpost.read_x("https://x.com/someoutlet/status/1000000000000000001")
    assert got["images"] == ["https://pbs.twimg.com/media/AAAAAAAAAAAAAA0?format=jpg&name=orig",
                             "https://pbs.twimg.com/media/AAAAAAAAAAAAAA1?format=jpg&name=orig"]
    assert got["text"].startswith("A post with two photos") and "flags" not in got
    assert linkpost.x_media({"mediaDetails": [{"type": "video", "media_url_https": "https://pbs.twimg.com/x.jpg"}]})[1] == \
        ["video: only a still of it can be posted"]
