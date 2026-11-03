"""Run reports (bore/ops.py): every CLI command reports its start (long ones) and its end; ok and
degraded come from the code, never from the session; a report never breaks a command or shows the
secret; machine faults are not swallowed by the photo catch-alls."""
import argparse
import json

import pytest

from bore import cli, ops

SECRET = "s3cret-routine-value-0123456789"


@pytest.fixture
def sent(monkeypatch, tmp_path):
    """The dashboard, mocked: every record bore.ops sends."""
    out = []
    monkeypatch.setenv("DASHBOARD_URL", "https://dash.test")
    monkeypatch.setenv("ROUTINE_SECRET", SECRET)
    monkeypatch.delenv("BORE_ROUTINE", raising=False)
    monkeypatch.setattr(ops, "SESSION", tmp_path / ".ops-session.json")
    monkeypatch.setattr(ops, "_post", lambda url, secret, rec, **k: out.append(json.loads(json.dumps(rec))) or 200)
    return out


def _argvs():
    """One command line per CLI subcommand (and per step of routine / studio / publish)."""
    ap = cli.build_parser()
    sub = next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction))
    for name, p in sub.choices.items():
        pos = [a for a in p._actions if not a.option_strings]
        req = [a for a in p._actions if a.option_strings and a.required]
        steps = pos[0].choices if pos and pos[0].choices else [None]
        for step in steps:
            argv = [name] + ([step] if step else [])
            argv += ["x" for a in (pos[1:] if step else pos) if a.nargs in (None, "+")]
            for a in req:
                argv += [a.option_strings[0], "x"]
            yield argv
            if step == "state":
                yield argv + ["--action", "push"]


def test_every_cli_command_reports_its_end_and_long_ones_their_start(sent, monkeypatch):
    for k in [k for k in vars(cli) if k.startswith("cmd_")]:
        monkeypatch.setattr(cli, k, lambda args, cfg: None)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    argvs = list(_argvs())
    assert len(argvs) > 40
    for argv in argvs:
        sent.clear()
        cli.main(argv)
        ends = [r for r in sent if r["ended_at"]]
        assert len(ends) == 1, argv
        end = ends[0]
        assert end["ok"] is True and end["degraded"] == [] and end["summary"]["exit"] == 0, argv
        assert end["started_at"] <= end["ended_at"]
        starts = [r for r in sent if not r["ended_at"]]
        assert (len(starts) == 1) == (end["command"] in ops.LONG), argv
        assert all(s["run_id"] == end["run_id"] and s["ok"] is None for s in starts)
    names = {r["command"] for r in sent} | {ops.command_name(cli.build_parser().parse_args(a)) for a in argvs}
    assert {"routine prepare", "routine state pull", "routine state push", "studio feed-publish", "publish mark", "health"} <= names


def _run(argv, fn, monkeypatch, cmd="cmd_routine"):
    monkeypatch.setattr(cli, cmd, fn)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    cli.main(argv)


def test_ok_is_worked_out_from_how_the_command_ended(sent, monkeypatch, capsys):
    def crash(args, cfg):
        raise ValueError("boom")
    with pytest.raises(ValueError):
        _run(["routine", "upload", "d"], crash, monkeypatch)
    assert sent[-1]["ok"] is False and sent[-1]["summary"]["error"] == "ValueError: boom"

    def blocked(args, cfg):
        raise SystemExit("BLOCKED: run `routine verify` on the current card.jpg; it must PASS")
    with pytest.raises(SystemExit):
        _run(["routine", "upload", "d"], blocked, monkeypatch)
    assert sent[-1]["ok"] is True and sent[-1]["summary"]["refused"].startswith("BLOCKED"), "a gate saying no is not a failure"

    def no_secret(args, cfg):
        raise SystemExit("DASHBOARD_URL / ROUTINE_SECRET not set")
    with pytest.raises(SystemExit):
        _run(["routine", "upload", "d"], no_secret, monkeypatch)
    assert sent[-1]["ok"] is False and sent[-1]["summary"]["error"] == "DASHBOARD_URL / ROUTINE_SECRET not set"

    for code, ok in ((4, True), (3, True), (1, False)):          # publish mark: HELD, SKIP; anything else fails
        def mark(args, cfg, code=code):
            raise SystemExit(code)
        with pytest.raises(SystemExit):
            _run(["publish", "mark", "huddle-1", "--status", "failed"], mark, monkeypatch, "cmd_publish")
        assert sent[-1]["ok"] is ok and sent[-1]["summary"]["exit"] == code
    assert sent[-1]["routine"] == "publisher" and sent[-1]["summary"]["target"] == "huddle-1"

    monkeypatch.setenv("APIFY_TOKEN", "apify_api_VerySecretValue123")

    def apify_down(args, cfg):                          # an error quoting a URL with the token in it
        raise SystemExit("cannot read Apify usage: 403 for url: https://api.apify.com/v2/users/me?token=apify_api_VerySecretValue123")
    with pytest.raises(SystemExit):
        _run(["routine", "prepare"], apify_down, monkeypatch)
    assert sent[-1]["ok"] is False and "token=<secret>" in sent[-1]["summary"]["error"]
    assert "VerySecretValue" not in json.dumps(sent)

    def health_errors(args, cfg):
        print("ERROR x: last success 50.0h ago, beyond the 48h backfill cap: data is being lost")
        raise SystemExit(1)
    with pytest.raises(SystemExit):
        _run(["health"], health_errors, monkeypatch, "cmd_health")
    assert sent[-1]["ok"] is False and sent[-1]["summary"]["errors"] == ["x: last success Nh ago, beyond the Nh backfill cap"]
    assert SECRET not in capsys.readouterr().out + json.dumps(sent)


def test_the_documented_gates_are_answers_and_a_stop_is_not(sent, monkeypatch):
    """What ROUTINE.md and PUBLISH.md call expected (a refused skip of an editor's link, NOT FOUND
    asking for one more QueuedPosts call, a preflight that held the run while SocialPilot is down)
    is not a failed run: it used to turn the health status red after two sessions. The state push
    refusing, and a publisher STOP, still fail."""
    def exits(msg):
        def fn(args, cfg):
            raise SystemExit(msg)
        return fn
    for argv, cmd, msg, ok in (
            (["routine", "skip", "d", "--reason", "r"], "cmd_routine",
             "REFUSED: this is an editor's link, never skipped (ROUTINE.md 2b/2c). Make the post", True),
            (["routine", "lint", "d"], "cmd_routine", "REFUSED: an editor's link or regeneration is never skipped", True),
            (["publish", "mark", "huddle-1", "--status", "scheduled"], "cmd_publish",
             "NOT FOUND huddle-1: the QueuedPosts result has no post with this caption. Call QueuedPosts again", True),
            (["publish", "preflight", "--error", "locked"], "cmd_publish", 4, True),
            (["publish", "preflight", "--error", "locked"], "cmd_publish", 1, False),
            (["publish", "mark", "huddle-1", "--status", "scheduled"], "cmd_publish", 5, False),
            (["routine", "state", "--action", "push"], "cmd_routine",
             "STATE NOT SAVED: the push was refused. The dashboard keeps its copy: report it and stop.", False),
            (["routine", "state", "--action", "pull"], "cmd_routine", "STATE NOT RESTORED: not a gzip file", False)):
        with pytest.raises(SystemExit):
            _run(argv, exits(msg), monkeypatch, cmd)
        s = sent[-1]["summary"]
        assert sent[-1]["ok"] is ok, (argv, msg)
        assert ("refused" in s) is ok and ("error" in s) is not ok, (argv, s)
    assert sent[-1]["summary"]["error"].startswith("STATE NOT RESTORED")

    # how a `publish mark` ended, from its last outcome line (a STOP after its NEEDS ACTION note)
    def stop(args, cfg):
        print("NEEDS ACTION huddle-1: recorded for a person: 2 copies are queued in SocialPilot: delete all but one")
        print("STOP huddle-1: 2 copies are queued in SocialPilot: delete all but one. Do not schedule anything else")
        raise SystemExit(5)
    with pytest.raises(SystemExit):
        _run(["publish", "mark", "huddle-1", "--status", "scheduled"], stop, monkeypatch, "cmd_publish")
    assert sent[-1]["ok"] is False and sent[-1]["summary"]["outcome"] == "stop"
    _run(["publish", "mark", "huddle-1", "--status", "missing"],
         lambda a, c: print("MISSING huddle-1: not found by this run (1 of 2): a later run looks again"), monkeypatch, "cmd_publish")
    assert sent[-1]["ok"] is True and sent[-1]["summary"]["outcome"] == "missing"


def test_one_degraded_call_or_catch_all_line_makes_the_run_amber(sent, monkeypatch, capsys, tmp_path):
    run = tmp_path / "20261021-1405"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"regenerations": ["huddle-1"], "links": [], "created_today": {"huddle": 3}}))

    def prepare(args, cfg):
        print("budget: $1.00 of $16.00 used this cycle; today $0.10 of $0.53 allowance (30.0 days left)")
        print("recent posts unavailable (HTTPSConnectionPool(host='bore-dashboard.vercel.app'): SSLEOFError)")
        print("Huddle Room: quota 2 this run, 5 candidate(s)")
        print("  skipped repeat: Some story -> earlier")
        print("    faces not compared (ImportError: cannot import name '_imaging')")
        print("reddit: skipped by pacer (needs ~$0.120; $0.500/$0.530 used today)")
        ops.degraded("other outlets' photos unavailable")
        ops.degraded("other outlets' photos unavailable")           # the same reason once
        for i in range(500):                                          # a long run: its last line still counts
            print(f"  PICK   {i}")
        print(f"\n1 brief(s) in {run}")
    _run(["routine", "prepare"], prepare, monkeypatch)
    end = sent[-1]
    assert end["ok"] is True
    assert end["degraded"] == ["recent posts unavailable", "faces not compared (ImportError)", "other outlets' photos unavailable"]
    s = end["summary"]
    assert s["pages"] == {"Huddle Room": {"quota": 2, "candidates": 5, "repeats": 1, "filtered": 0}}
    assert s["pacer_skipped"] == ["reddit"] and s["budget"].startswith("$1.00 of $16.00")
    assert (s["run"], s["regenerations"], s["links"], s["created_today"]) == (run.name, 1, 0, {"huddle": 3})
    assert "run report: degraded (recent posts unavailable" in capsys.readouterr().err
    _run(["routine", "status"], lambda a, c: print("fine"), monkeypatch)
    assert sent[-1]["degraded"] == [], "each command starts clean"


def test_a_report_never_breaks_the_command_or_prints_the_secret(sent, monkeypatch, capsys):
    import requests

    def down(url, secret, rec, **k):
        raise requests.ConnectionError(f"cannot reach {url} with Bearer {secret}")
    monkeypatch.setattr(ops, "_post", down)
    done = []
    _run(["routine", "status"], lambda a, c: done.append(1), monkeypatch)
    assert done == [1]
    err = capsys.readouterr().err
    assert "run report not sent (ConnectionError): carry on" in err and SECRET not in err
    monkeypatch.setattr(ops, "_post", lambda url, secret, rec, **k: 404)
    _run(["routine", "status"], lambda a, c: None, monkeypatch)
    assert "run report not saved (HTTP 404): carry on" in capsys.readouterr().err, "never an instruction to the model"
    # no dashboard configured: nothing is sent at all
    calls = []
    monkeypatch.setattr(ops, "_post", lambda *a, **k: calls.append(a) or 200)
    monkeypatch.delenv("DASHBOARD_URL")
    monkeypatch.setattr("bore.config.load_env", lambda path=None: None)
    _run(["routine", "status"], lambda a, c: None, monkeypatch)
    assert calls == []


def test_a_dashboard_that_does_not_answer_costs_seconds_not_minutes(sent, monkeypatch, capsys):
    """Oct 21: the dashboard was slow; every command waited on its report. One miss: the next reports
    wait 1-2 s; a second miss: none for 10 minutes (the session file remembers it)."""
    import requests
    waits = []

    def slow(url, secret, rec, timeout=(4, 6)):
        waits.append(timeout)
        raise requests.Timeout("read timed out")
    monkeypatch.setattr(ops, "_post", slow)
    for _ in range(5):
        _run(["routine", "status"], lambda a, c: None, monkeypatch)
    assert waits == [(4, 6), (1, 2)], "one normal wait, one short one, then none"
    assert capsys.readouterr().err.count("run report not sent (Timeout): carry on") == 2
    s = json.loads(ops.SESSION.read_text())
    s["report_down"]["at"] -= ops.DOWN_S                         # 10 minutes later it tries again; an answer ends it
    ops.SESSION.write_text(json.dumps(s))
    monkeypatch.setattr(ops, "_post", lambda url, secret, rec, timeout=(4, 6): waits.append(timeout) or 503)
    _run(["routine", "status"], lambda a, c: None, monkeypatch)
    assert "run report not sent (HTTP 503): carry on" in capsys.readouterr().err, "a server error is not an answer"
    monkeypatch.setattr(ops, "_post", lambda url, secret, rec, timeout=(4, 6): waits.append(timeout) or 200)
    _run(["routine", "status"], lambda a, c: None, monkeypatch)
    _run(["routine", "status"], lambda a, c: None, monkeypatch)
    assert waits[2:] == [(4, 6), (1, 2), (4, 6)]
    assert "report_down" not in json.loads(ops.SESSION.read_text())


def test_only_a_session_that_says_so_reports_as_manual(sent, monkeypatch):
    """A developer's or diagnostic session sets BORE_ROUTINE=manual. Nothing is guessed from the
    checkout: a guess labelled routine sessions 'manual' (Oct 22-23) and hid their failures."""
    monkeypatch.delenv("BORE_ROUTINE", raising=False)
    _run(["routine", "prepare"], lambda a, c: None, monkeypatch)
    assert sent[-1]["routine"] == "hourly"
    monkeypatch.setenv("BORE_ROUTINE", "manual")
    _run(["routine", "prepare"], lambda a, c: None, monkeypatch)
    assert sent[-1]["routine"] == "manual"
    assert ops._routine_branch() == "main", "from routines/triggers.yaml"


def test_the_routine_is_named_by_the_command_or_the_session(sent, monkeypatch):
    for argv, cmd in ((["studio", "sync"], "cmd_studio"), (["routine", "state"], "cmd_routine"),
                      (["studio", "feed"], "cmd_studio"), (["routine", "state", "--action", "pull"], "cmd_routine")):
        _run(argv, lambda a, c: None, monkeypatch, cmd)
    assert [r["routine"] for r in sent if r["ended_at"]] == ["setup", "setup", "topics", "topics"]
    assert len({r["summary"]["session"] for r in sent}) == 1
    sent.clear()
    _run(["routine", "lint", "studio/job-1/p1"], lambda a, c: None, monkeypatch)
    _run(["studio", "jobs"], lambda a, c: None, monkeypatch, "cmd_studio")
    _run(["publish", "plan"], lambda a, c: None, monkeypatch, "cmd_publish")
    assert [r["routine"] for r in sent if r["ended_at"]] == ["studio", "studio", "publisher"]
    monkeypatch.setenv("BORE_ROUTINE", "Hourly")
    _run(["studio", "sync"], lambda a, c: None, monkeypatch, "cmd_studio")
    assert sent[-1]["routine"] == "hourly"


def test_routine_check_reports_pages_and_open_editor_requests(sent, monkeypatch, tmp_path):
    run = tmp_path / "20261021-1405"
    for name, brief, marker in (("1-a", {"page": "huddle", "rank": 1}, "uploaded"), ("2-b", {"page": "huddle", "rank": 2}, None),
                                ("3-link", {"page": "huddle", "link": {"request_id": "r1"}}, None)):
        (run / name).mkdir(parents=True)
        (run / name / "brief.json").write_text(json.dumps(brief))
        if marker:
            (run / name / marker).write_text("")
    (run / "run.json").write_text(json.dumps({"quota": {"huddle": 1}}))
    with pytest.raises(SystemExit, match="NOT DONE"):        # an open link keeps the check from passing
        cli.main(["routine", "check", str(run)])
    end = sent[-1]
    s = end["summary"]
    assert end["ok"] is True and s["refused"].startswith("NOT DONE")        # a gate, not a crash
    assert s["check"] == "NOT DONE" and s["pages"]["huddle"]["quota"] == 1 and s["pages"]["huddle"]["uploaded"] == 1
    assert s["requests_open"] == ["3-link"], "an editor's link with no upload and no fail is still owed"

    # reported back to the editor with link-fail / regen-fail: settled (this session ran it, or its marker is there)
    real = cli.cmd_routine
    _run(["routine", "link-fail", str(run / "3-link"), "--reason", "the page cannot be read"], lambda a, c: None, monkeypatch)
    monkeypatch.setattr(cli, "cmd_routine", real)
    for name, brief in (("4-regen", {"page": "huddle", "regeneration": {"replaces": "huddle-x"}}),
                        ("5-link", {"page": "huddle", "link": {"request_id": "r2"}})):
        (run / name).mkdir()
        (run / name / "brief.json").write_text(json.dumps(brief))
    (run / "4-regen" / "request_failed.json").write_text("{}")
    (run / "3-link" / "request_failed.json").write_text("{}")      # what the real link-fail leaves
    with pytest.raises(SystemExit, match="NOT DONE"):
        cli.main(["routine", "check", str(run)])
    s = sent[-1]["summary"]
    assert s["requests_failed"] == ["3-link", "4-regen"] and s["requests_open"] == ["5-link"]
    assert s["pages"]["huddle"]["failed"] == 2
    (run / "5-link" / "uploaded").write_text("")
    cli.main(["routine", "check", str(run)])
    assert sent[-1]["summary"]["check"] == "OK" and sent[-1]["summary"]["requests_open"] == []


def test_prepare_without_apify_is_a_degraded_run(sent, monkeypatch):
    """No APIFY_TOKEN, a rotated one, an outage: prepare briefs the editors' requests and picks from
    the stored posts (bore/cli.py _hourly_select); the report says so, amber, not green."""
    def prepare(args, cfg):
        print("Apify unavailable (APIFY_TOKEN is not set. Put it in .env (see .env.example).): collection skipped, "
              "picking from the stored posts")
    _run(["routine", "prepare"], prepare, monkeypatch)
    assert sent[-1]["ok"] is True and sent[-1]["degraded"] == ["apify unavailable: collection skipped"]


def test_publish_plan_reports_what_needs_a_person(sent, monkeypatch):
    import time

    now = time.time()
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 7200))

    class Dash:
        def queue(self):
            return {"queue": [{"id": "huddle-upd", "page": "huddle", "approved_at": now - 60, "update_of": "huddle-orig"},
                              {"id": "newpage-1", "page": "newpage", "approved_at": now - 60}],
                    "scheduled": [{"id": "huddle-orig", "page": "huddle", "scheduled_for": when}], "in_progress": [],
                    "socialpilot": {}}
    monkeypatch.setattr(cli, "_dashboard", lambda: Dash())
    cli.main(["publish", "plan"])
    end = sent[-1]
    s = end["summary"]
    assert s["to_schedule"] == 1 and s["delete_in_socialpilot"] == ["huddle-orig"] and s["no_account"] == ["newpage-1"]
    # the plan ran fine: what a person must do is the health status's own reason (from these lists),
    # never a degraded run that turns red after 3 plans
    assert end["ok"] is True and end["degraded"] == []


def test_the_health_status_reads_the_publishers_notes_as_urgent_unless_they_only_ask_to_look():
    """dashboard/routes/health.js turns the publisher's notes red unless they are the code's own notes
    asking a person to look (CHECK_NOTE, in their words): what bore/publish.py verify_queued finds and
    what a session records itself (PUBLISH.md's example) go out unless a person fixes or deletes them.
    (When only verify_queued's words were red, a session's own note about a second copy stayed amber
    until after that copy had gone out.) Each look note is matched in the words its code writes."""
    import re
    import time
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bore.publish import verify_queued
    src = (ops.ROOT / "dashboard" / "routes" / "health.js").read_text()
    words = re.search(r"export const CHECK_NOTE = /\^\((.+)\)/;", src)[1]
    look = re.compile(f"^({words})")
    urgent = lambda note: not look.search(note)      # noqa: E731
    writers = (ops.ROOT / "bore" / "cli.py").read_text() + (ops.ROOT / "dashboard" / "routes" / "result.js").read_text()
    for w in words.split("|"):                  # reworded where it is written: health.js must follow (else red)
        assert w.replace("\\", "") in writers, w
    publish_md = (ops.ROOT / "PUBLISH.md").read_text()
    example = re.search(r'--status needs_action --error "<what to do, e\.g\. ([^>]+)>"', publish_md)[1]
    assert urgent(example), example
    tz, ours, never = "Asia/Kolkata", "2662938", "2662937"
    now = int(time.time()) // 60 * 60
    local = lambda t, f="%Y-%m-%d %H:%M": datetime.fromtimestamp(t, ZoneInfo(tz)).strftime(f)   # noqa: E731
    post = lambda t, login=ours, caption="The approved caption, word for word.": {        # noqa: E731
        "postId": "p1", "postDesc": caption, "loginId": login, "accountUsername": "x", "postTimeFormat": local(t) if t else "",
        "createdOn": local(now, "%b %d, %Y %I:%M %p")}
    expect = lambda slot: {"caption": "The approved caption, word for word.", "login_id": ours, "slot": slot,   # noqa: E731
                           "claimed_at": now}
    slot = now + 2 * 3600
    for entries, s in (([post(slot), post(slot, never)], slot),            # a copy on the account it must never use
                       ([post(slot), post(slot)], slot),                   # two copies
                       ([post(None)], slot),                               # no time in SocialPilot
                       ([post(slot + 3600)], slot),                        # not its slot
                       ([post(now + 1800)], now + 1800)):                  # under an hour out
        v = verify_queued(entries, expect(s), tz, {ours}, {never})
        assert v["stop"] and v["problems"] and all(urgent(p) for p in v["problems"]), v["problems"]
    v = verify_queued([post(slot, caption="Edited on the way")], expect(slot), tz, {ours}, {never})
    assert not v["stop"] and v["problems"] and all(urgent(p) for p in v["problems"]), "a changed caption goes out unapproved"
    for note in ("SocialPilot's queue did not show it after CreatePost: check in SocialPilot that it is scheduled once, on "
                 f"account {ours}, for {local(slot)} ({tz})",
                 "look on the Facebook page: SocialPilot has no record of this post, it may or may not have gone out",
                 "look in SocialPilot and on the Facebook page: the publisher could not check whether this post is there already"):
        assert not urgent(note), note


def test_the_setup_docs_stop_on_each_way_ensure_deps_gives_up():
    """ROUTINE.md says when to stop after scripts/ensure-deps.sh, quoting each final line it gives up
    with (exit 1): the packages, and the card browser. It used to quote only the first, so on a machine
    whose browser does not start a session wrote the posts and asked Krea for images before every card
    failed. TOPICS.md quotes both too: the topic feed stops on the first and carries on on the second."""
    import re
    lines = (ops.ROOT / "scripts" / "ensure-deps.sh").read_text().splitlines()
    gives_up = [next(x for x in reversed(lines[max(0, i - 3):i]) if x.strip().startswith('echo "packages:'))
                for i, line in enumerate(lines) if line.strip() == "exit 1"]
    assert len(gives_up) == 2, gives_up
    for doc in ("ROUTINE.md", "TOPICS.md"):
        para = next(" ".join(p.split()) for p in (ops.ROOT / doc).read_text().split("\n\n") if "`ensure-deps.sh` ends with" in p)
        quoted = re.findall(r'"([^"]+)"', para[para.index("`ensure-deps.sh` ends with"):])
        assert all(any(q in echo for q in quoted) for echo in gives_up), (doc, quoted, gives_up)
        assert all(any(q in echo for echo in gives_up) for q in quoted), (doc, quoted)
    topics = " ".join((ops.ROOT / "TOPICS.md").read_text().split())
    assert 'If it ends with "the card browser still does not start", carry on' in topics


def test_studio_jobs_and_feed_publish_summaries(sent, monkeypatch, tmp_path):
    def jobs(args, cfg):
        print("LEAVE  huddle-1-ab (compose): packages not loading on this machine")
        print("JOB    work/studio/huddle-2-cd  IDEAS  page huddle")
    _run(["studio", "jobs"], jobs, monkeypatch, "cmd_studio")
    s = sent[-1]["summary"]
    assert s["taken"] == [{"id": "huddle-2-cd", "kind": "ideas", "page": "huddle"}] and s["left"] == ["huddle-1-ab"]
    assert sent[-1]["degraded"] == ["left a Studio job: packages not loading on this machine"]

    import time
    now = time.time()
    ms = lambda ago: int((now - ago) * 1000)                    # noqa: E731 - the feed's times, as prepare_feed writes them
    (tmp_path / "feed").mkdir()
    (tmp_path / "feed" / "huddle.json").write_text(json.dumps({"page": "huddle", "stories": [
        {"id": "a", "published_at": ms(3600), "summary": "Whole sentence.", "images": [{"id_match": "match"}], "images_v": 5},
        {"id": "b", "published_at": ms(6 * 3600), "summary": "Cut off due to a", "images": [], "images_v": 0},
        {"id": "c", "published_at": ms(20 * 3600), "needs_writing": True, "images": [{"id_match": "mismatch"}], "images_v": 5}]}))
    (tmp_path / "feed" / "culture-rush.json").write_text(json.dumps({"stories": [{"id": "d", "published_at": now - 600}]}))
    _run(["studio", "feed-publish", "--run", str(tmp_path)], lambda a, c: None, monkeypatch, "cmd_studio")
    assert sent[-1]["routine"] == "topics"
    pages = sent[-1]["summary"]["pages"]
    assert pages["huddle"] == {"stories": 3, "newest_min": 60, "in_3h": 1, "in_12h": 2, "written": 2,
                               "unwritten": 1, "cut_summaries": 1, "no_images": 1, "unchecked": 1,
                               "face_match": 1, "face_mismatch": 1}
    assert pages["huddle"]["in_3h"] < pages["huddle"]["stories"]
    assert (pages["culture-rush"]["newest_min"], pages["culture-rush"]["in_3h"]) == (10, 1), "seconds are read too"


def test_health_warnings_never_carry_source_names(sent, monkeypatch):
    def health(args, cfg):
        print("WARN  huddle: 5 handle(s) produced nothing in 72h (renamed/suspended?): SecretHandleOne, SecretHandleTwo")
        print("WARN  x: last error: HTTPSConnectionPool(host='api.apify.com')")
    _run(["health"], health, monkeypatch, "cmd_health")
    assert sent[-1]["summary"]["warnings"] == ["huddle: N handle(s) produced nothing in Nh", "x: last error"]
    assert "Secret" not in json.dumps(sent)


def test_machine_faults_are_told_apart_from_bad_content():
    from PIL import UnidentifiedImageError
    assert ops.machine_fault(ImportError("cannot import name '_imaging' from 'PIL'"))
    assert ops.machine_fault(ModuleNotFoundError("No module named 'cv2'"))
    assert ops.machine_fault(Exception("OpenCV(5.0.0) error: (-5:Bad argument) Can't read ONNX file: /x/face.onnx"))
    assert ops.machine_fault(Exception("BrowserType.launch: Executable doesn't exist at /opt/pw-browsers/chromium-1194"))
    assert not ops.machine_fault(UnidentifiedImageError("cannot identify image file"))
    assert not ops.machine_fault(RuntimeError("HTTP 403"))


def test_photo_catch_alls_re_raise_machine_faults_and_mark_the_rest_degraded(monkeypatch):
    from bore import consensus, photosearch, stills
    ops._DEGRADED.clear()

    def broken(*a, **k):
        raise ImportError("cannot import name '_imaging' from 'PIL'")
    monkeypatch.setattr(consensus, "_consensus", broken)
    with pytest.raises(ImportError):                              # Oct 21: 'Photo checks hit ImportError', reported ok
        consensus.consensus_photos("A headline", 0)

    def flaky(*a, **k):
        raise RuntimeError("bing answered 500")
    monkeypatch.setattr(consensus, "_consensus", flaky)
    assert consensus.consensus_photos("A headline", 0) == []
    assert ops._DEGRADED == ["machine fault: ImportError (other outlets' photos)", "other outlets' photos unavailable"]

    # a still whose OCR does not load is not a still with no text on it
    import io

    from PIL import Image

    from bore import guard
    from bore.media import Photo
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (120, 90, 60)).save(buf, "JPEG")
    monkeypatch.setattr(guard, "_ocr", broken)
    with pytest.raises(ImportError):
        stills.inspect_still(Photo("https://x.test/a.jpg", "web", "x", 64, 48, buf.getvalue()))
    monkeypatch.setattr(guard, "_ocr", flaky)
    p = stills.inspect_still(Photo("https://x.test/a.jpg", "web", "x", 64, 48, buf.getvalue()))
    assert "not text-checked" in p.flags and "still text check failed" in ops._DEGRADED

    # a photo search source that hit a broken machine fails the search instead of shrinking it
    for k in ("subject_news_photos", "bing_images", "wikimedia"):
        monkeypatch.setattr(photosearch, k, lambda *a, **kw: ([], {"source": "x"}))
    monkeypatch.setattr(photosearch, "ddg_images", broken)
    monkeypatch.setattr("bore.faceid.available", lambda: False)
    with pytest.raises(ImportError):
        photosearch.search_subject("Jane Example", deadline_s=2)
    ops._DEGRADED.clear()


def test_a_machine_fault_that_a_caller_swallows_still_makes_the_run_amber(sent, monkeypatch):
    """Callers further up catch everything (cli's photo pre-filter, the consensus futures): the
    re-raised fault never reaches the report, so it is recorded where it happens."""
    import io

    from PIL import Image

    from bore import guard, stills
    from bore.media import Photo
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (120, 90, 60)).save(buf, "JPEG")

    def broken(*a, **k):
        raise ImportError("cannot import name '_imaging' from 'PIL'")
    monkeypatch.setattr(guard, "_ocr", broken)

    def photos(args, cfg):                                         # e.g. `except Exception: clean = True`
        try:
            stills.inspect_still(Photo("https://x.test/a.jpg", "web", "x", 64, 48, buf.getvalue()))
        except Exception:
            pass
    _run(["routine", "photos", "d"], photos, monkeypatch)
    assert sent[-1]["ok"] is True and sent[-1]["degraded"] == ["machine fault: ImportError (still text check)"]
    assert not ops.fault(RuntimeError("HTTP 403"), "x") and ops._DEGRADED == ["machine fault: ImportError (still text check)"]


def test_reports_reach_the_dev_dashboard_and_its_health_shows_a_failed_run(tmp_path):
    """The real CLI against the real routes (dashboard/dev/server.mjs, in-memory store): the
    contract between bore/ops.py and POST /api/routine/runs, end to end."""
    import os
    import shutil
    import socket
    import subprocess
    import sys

    import requests
    root = ops.ROOT
    if not shutil.which("node") or not (root / "dashboard" / "node_modules" / "@vercel" / "blob").exists():
        # CI installs them (test.yml): there the contract is always checked
        (pytest.fail if os.environ.get("CI") else pytest.skip)("node and the dashboard's packages are needed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://localhost:{port}"
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}   # the CLI may send: to the dev server only
    env.update(DASHBOARD_URL=base, ROUTINE_SECRET="dev-routine", BORE_ROUTINE="hourly", NO_PROXY="localhost,127.0.0.1",
               no_proxy="localhost,127.0.0.1")
    server = subprocess.Popen(["node", str(root / "dashboard" / "dev" / "server.mjs"), str(port)], cwd=root / "dashboard",
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    session = ops.SESSION.read_text() if ops.SESSION.exists() else None
    auth = {"Authorization": "Bearer dev-routine"}
    try:
        for _ in range(100):
            line = server.stdout.readline()
            if "dev server http" in line or server.poll() is not None:
                break
        assert server.poll() is None, "the dev server did not start"
        p = subprocess.run([sys.executable, "-m", "bore.cli", "routine", "check", str(tmp_path / "missing-run")], cwd=root, env=env,
                           capture_output=True, text=True, timeout=120)
        assert p.returncode != 0 and "run report not" not in p.stderr, p.stderr
        runs = requests.get(f"{base}/api/routine/runs?hours=1", headers=auth, timeout=10).json()
        assert [(r["routine"], r["command"], r["state"]) for r in runs["runs"]] == [("hourly", "routine check", "fail")]
        assert runs["reports"][0]["summary"]["error"].startswith("FileNotFoundError")
        health = requests.post(f"{base}/api/routine/watchdog", json={}, headers=auth, timeout=30).json()["health"]
        assert any(r["code"] == "run_failed" and r["level"] == "amber" for r in health["reasons"]), health
    finally:
        server.terminate()
        server.wait(10)
        if session is None:
            ops.SESSION.unlink(missing_ok=True)
        else:
            ops.SESSION.write_text(session)
