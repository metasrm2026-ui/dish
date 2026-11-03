import argparse
import contextlib
import copy
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bore.publish import parse_busy, plan

ROOT = Path(__file__).resolve().parent.parent
IST = ZoneInfo("Asia/Kolkata")

NOW = 1_790_430_000  # fixed clock

# what the SocialPilot connector answers (shapes from real calls, trimmed)
ACCOUNTS = {"success": True, "message": "Accounts list displayed successfully.", "accounts": [
    {"platform": "Facebook", "isReconnect": False, "accountUsername": "Culture Rush", "loginId": 2662938},
    {"platform": "Facebook", "isReconnect": False, "accountUsername": "Huddle Room", "loginId": 2662936},
    {"platform": "Facebook", "isReconnect": False, "accountUsername": "Keaton Caleb", "loginId": 2662937}]}
USERINFO = {"success": True, "message": "User info retrieved successfully", "data": {"setting": {"timeZone": "Asia/Kolkata"}}}
LOCKED = {"success": False, "message": "Your Account is Locked", "accounts": [], "limit": 50, "page": 1, "totalAccount": 0, "totalPages": 0}
NAMES = {"huddle": "Huddle Room", "culture-rush": "Culture Rush"}


def _draft(draft_id, page="huddle", **kw):
    """An approved draft as /api/queue lists it (dashboard/routes/queue.js)."""
    now = int(time.time())
    return {"id": draft_id, "page": page, "page_name": NAMES.get(page, page), "status": "approved",
            "approved_at": now - 600, "first_approved_at": now - 600, "holds": 0, "held_error": "",
            "caption": f"Caption {draft_id}", "image_url": f"https://blob.example/{draft_id}.jpg",
            "image_urls": [f"https://blob.example/{draft_id}.jpg"], "requested_at": None, "mode": None, "studio": False,
            "socialpilot_login_id": "", "update_of": None, "created_at": None, "ever_sent": False, "prior_post_id": None, **kw}


class FakeDash:
    """/api/queue and /api/result as the publisher's CLI sees them."""
    def __init__(self):
        self.data = {"queue": [], "scheduled": [], "in_progress": [], "socialpilot": {}}
        self.sent, self.answers = [], {}

    def queue(self):
        return copy.deepcopy(self.data)

    def report(self, draft_id, status, **detail):
        self.sent.append((draft_id, status, detail))
        if status in self.answers:
            return self.answers[status](draft_id, detail)
        if status == "publishing":
            return {"ok": True, "id": draft_id, "status": status, "claim": "1791864091866"}
        if status == "held":
            return {"ok": True, "id": draft_id, "status": "held", "error": detail.get("error"), "holds": 1}
        return {"ok": True, "id": draft_id, "status": status}

    def statuses(self, status):
        return [(i, d) for i, s, d in self.sent if s == status]


class Pub:
    """Runs `bore publish ...` the way the routine does, against FakeDash; returns the exit code
    (0, an int, or the message of a sys.exit("..."))."""
    def __init__(self, root, dash):
        self.root, self.dash = root, dash

    def run(self, action, draft_id=None, **kw):
        from bore import cli
        ns = argparse.Namespace(action=action, id=draft_id, status=None, error=None, post_id=None, url=None,
                                scheduled_for=None, busy=None, queued=None, result=None)
        for k, v in kw.items():
            setattr(ns, k, v)
        try:
            cli.cmd_publish(ns, {"_root": str(self.root)})
        except SystemExit as e:
            return e.code
        return 0

    def save(self, name, value):
        p = self.root / "work" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value if isinstance(value, str) else json.dumps(value))
        return str(p)

    def preflight(self, accounts=None, userinfo=None):
        return self.run("preflight", result=[self.save("accounts.json", accounts or ACCOUNTS),
                                             self.save("userinfo.json", userinfo or USERINFO)])

    def plan(self, busy=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert self.run("plan", busy=busy) == 0
        return json.loads(buf.getvalue())

    def claim(self, draft_id, out):
        item = next(x for x in out["to_schedule"] if x["id"] == draft_id)
        return self.run("mark", draft_id, status="publishing", scheduled_for=item["schedule_utc"])

    def entry(self, draft_id, out, **kw):
        """The QueuedPosts entry CreatePost made for a planned item."""
        item = next(x for x in out["to_schedule"] if x["id"] == draft_id)
        return {"postId": f"sp-{draft_id}", "loginId": item["socialpilot_account"]["loginId"],
                "accountUsername": NAMES.get(item["page"], item["page"]), "postDesc": item["caption"], "postStatus": "N",
                "postTimeFormat": item["schedule_local"], "createdOn": datetime.now(IST).strftime("%b %d, %Y %I:%M %p"), **kw}

    def scheduled(self, draft_id, *entries, post_id="cp-1"):
        f = self.save("queued.json", {"success": True, "message": "Queued posts retrieved successfully", "posts": list(entries)})
        return self.run("mark", draft_id, status="scheduled", post_id=post_id, queued=f)


@pytest.fixture
def pub(tmp_path, monkeypatch):
    from bore import cli
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "socialpilot.yaml").write_text((ROOT / "config" / "socialpilot.yaml").read_text())
    dash = FakeDash()
    monkeypatch.setattr(cli, "_dashboard", lambda: dash)
    return Pub(tmp_path, dash)


def test_at_least_an_hour_after_approval():
    [p] = plan([{"id": "a", "page": "huddle", "approved_at": NOW}], {}, now=NOW)
    assert p["at"] >= NOW + 3600 and p["at"] % 300 == 0


def test_two_approved_together_are_45_minutes_apart():
    q = [{"id": "a", "page": "huddle", "approved_at": NOW}, {"id": "b", "page": "huddle", "approved_at": NOW + 5}]
    a, b = plan(q, {}, now=NOW)
    assert 2700 <= b["at"] - a["at"] < 3600


def test_other_pages_do_not_block_each_other():
    q = [{"id": "a", "page": "huddle", "approved_at": NOW}, {"id": "b", "page": "culture-rush", "approved_at": NOW}]
    a, b = plan(q, {}, now=NOW)
    assert a["at"] == b["at"]


def test_existing_scheduled_posts_are_respected_both_sides():
    t = NOW + 3600 + 600
    [p] = plan([{"id": "a", "page": "huddle", "approved_at": NOW}], {"huddle": [t]}, now=NOW)
    assert abs(p["at"] - t) >= 2700


def test_old_approval_still_waits_an_hour_from_now():
    [p] = plan([{"id": "a", "page": "huddle", "approved_at": NOW - 5 * 3600}], {}, now=NOW)
    assert p["at"] >= NOW + 3600


def test_busy_times_in_socialpilot_timezone():
    b = parse_busy(["huddle=2026-09-26 21:00"], "Asia/Kolkata")
    [p] = plan([{"id": "a", "page": "huddle", "approved_at": b["huddle"][0] - 3600}], b, now=b["huddle"][0] - 3600)
    assert p["local"] == "2026-09-26 21:45"   # 45 minutes after the post already at 21:00


def test_studio_posts_keep_the_editors_time():
    from bore.publish import plan
    now = 1_800_000_000
    q = [{"id": "auto", "page": "p", "approved_at": now - 60},
         {"id": "studio", "page": "p", "approved_at": now, "requested_at": (now + 3 * 3600) * 1000},
         {"id": "now", "page": "p", "approved_at": now, "requested_at": (now + 600) * 1000}]
    out = {x["id"]: x["at"] for x in plan(q, {}, now=now)}
    assert out["studio"] == now + 3 * 3600          # exactly as chosen
    assert out["now"] == now + 3600                 # never sooner than an hour from now (owner's rule)
    assert plan(q[2:], {}, now=now, min_lead_minutes=10)[0]["at"] == now + 600   # when the owner lowers it
    assert out["auto"] >= now + 3600 and all(abs(out["auto"] - out[k]) >= 45 * 60 for k in ("studio", "now"))


def test_studio_time_in_the_past_moves_to_now():
    from bore.publish import plan
    now = 1_800_000_000
    out = plan([{"id": "s", "page": "p", "approved_at": now - 900, "requested_at": (now - 300) * 1000}], {}, now=now)
    assert now + 60 * 60 <= out[0]["at"] <= now + 61 * 60


def test_studio_posts_whose_time_passed_keep_the_gap():
    """Held or retried Studio posts whose chosen time passed used to get now+60 each, without a look
    at the page's other posts: two in the same minute, minutes from a post already scheduled."""
    now = 1_800_000_000
    q = [{"id": "s1", "page": "huddle", "approved_at": now - 3 * 3600, "requested_at": (now - 3 * 3600) * 1000, "holds": 1},
         {"id": "s2", "page": "huddle", "approved_at": now - 2 * 3600, "requested_at": (now - 2 * 3600) * 1000, "holds": 1},
         {"id": "h1", "page": "huddle", "approved_at": now - 600}]
    busy = {"huddle": [now + 65 * 60]}
    out = {x["id"]: x["at"] for x in plan(q, busy, now=now)}
    times = sorted(list(out.values()) + busy["huddle"])
    assert all(b - a >= 45 * 60 for a, b in zip(times, times[1:])), times
    assert min(out.values()) >= now + 3600                          # never sooner than an hour from now
    assert out["s1"] < out["s2"]                                     # the older request first
    # "Post now" is the earliest free slot too, even while its time (an hour from the Send) is ahead
    nowq = [{"id": "n", "page": "huddle", "approved_at": now, "requested_at": (now + 3700) * 1000, "mode": "now"}]
    [n] = plan(nowq, {"huddle": [now + 3600]}, now=now)
    assert n["at"] >= now + 3600 + 45 * 60
    # an editor's own pick is kept as given, and the system's slots move around it
    pick = [{"id": "e", "page": "huddle", "approved_at": now, "requested_at": (now + 2 * 3600) * 1000, "mode": "schedule"},
            {"id": "n", "page": "huddle", "approved_at": now, "requested_at": (now - 60) * 1000, "mode": "now"}]
    out = {x["id"]: x["at"] for x in plan(pick, {"huddle": [now + 2 * 3600 + 600]}, now=now)}
    assert out["e"] == now + 2 * 3600 and abs(out["n"] - out["e"]) >= 45 * 60


def test_a_studio_post_planned_again_after_a_skip_keeps_the_gap_to_what_was_just_claimed(pub, monkeypatch):
    """The claim SKIPs a slot that is now under 55 minutes away: the item is planned again, and the
    new slot keeps 45 minutes from the hourly post this run claimed meanwhile."""
    now = int(time.time())
    pub.dash.data["queue"] = [_draft("h1", approved_at=now - 60), _draft("s1", studio=True, requested_at=(now + 60) * 1000, mode="now")]
    pub.preflight()
    out = pub.plan()
    assert pub.claim("h1", out) == 0
    h1 = next(x for x in out["to_schedule"] if x["id"] == "h1")
    pub.dash.data["queue"] = [x for x in pub.dash.data["queue"] if x["id"] != "h1"]
    pub.dash.data["in_progress"] = [{"id": "h1", "page": "huddle", "page_name": "Huddle Room", "scheduled_for": h1["schedule_utc"],
                                     "claimed_at": now * 1000, "caption": "Caption h1", "socialpilot_login_id": "", "update_of": None}]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 8 * 60)
    assert pub.claim("s1", out) == 3                                 # its slot is under 55 minutes away now
    again = next(x for x in pub.plan()["to_schedule"] if x["id"] == "s1")
    from bore.cli import _pub_utc
    assert abs(_pub_utc(again["schedule_utc"]) - _pub_utc(h1["schedule_utc"])) >= 45 * 60


def test_hourly_posts_wait_for_the_audiences_morning():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ny, ist = ZoneInfo("America/New_York"), ZoneInfo("Asia/Kolkata")
    win = ("America/New_York", 7, 23)
    # editors approve 4 posts at 07:00 IST = 21:30 New York (the morning review)
    approved = datetime(2026, 10, 13, 7, 0, tzinfo=ist).timestamp()
    q = [{"id": c, "page": "huddle", "approved_at": approved + i} for i, c in enumerate("abcd")]
    slots = plan(q, {}, now=approved, window=win)
    local = [datetime.fromtimestamp(s["at"], ny).strftime("%m-%d %H:%M") for s in slots]
    assert local == ["10-12 22:30", "10-13 07:00", "10-13 07:45", "10-13 08:30"]
    assert [s.get("held_for_audience_hours", False) for s in slots] == [False, True, True, True]
    for s in slots:                                         # never sooner than an hour after approval
        assert s["at"] >= approved + 3600
    # a post approved at 03:00 New York goes out at 07:00, and Studio times are never moved
    early = datetime(2026, 10, 13, 3, 0, tzinfo=ny).timestamp()
    st = datetime(2026, 10, 13, 4, 30, tzinfo=ny).timestamp()
    got = {x["id"]: x for x in plan([{"id": "a", "page": "huddle", "approved_at": early},
                                     {"id": "s", "page": "culture-rush", "approved_at": early, "requested_at": st * 1000}],
                                    {}, now=early, window=win)}
    a, s = got["a"], got["s"]
    assert datetime.fromtimestamp(a["at"], ny).strftime("%H:%M") == "07:00"
    assert s["at"] == st
    assert plan([{"id": "a", "page": "huddle", "approved_at": early}], {}, now=early)[0]["at"] < a["at"]   # no window: as before


def test_audience_window_edges():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    win = ("America/New_York", 7, 23)
    # the night US clocks change: 22:40 ET on Oct 31 + 1h -> held to 07:00 EST (12:00 UTC)
    t = datetime(2026, 10, 31, 22, 40, tzinfo=ny).timestamp()
    (p,) = plan([{"id": "a", "page": "huddle", "approved_at": t}], {}, now=t, window=win)
    assert p["iso_utc"] == "2026-11-01T12:00:00Z" and p.get("held_for_audience_hours")
    # posts already at 07:00 and 07:45 next morning: tonight's goes at 22:40, the next after them
    t = datetime(2026, 10, 13, 21, 40, tzinfo=ny).timestamp()
    busy = {"huddle": [datetime(2026, 10, 14, 7, 0, tzinfo=ny).timestamp(), datetime(2026, 10, 14, 7, 45, tzinfo=ny).timestamp()]}
    a, b = plan([{"id": "a", "page": "huddle", "approved_at": t}, {"id": "b", "page": "huddle", "approved_at": t + 1}],
                busy, now=t, window=win)
    assert datetime.fromtimestamp(a["at"], ny).strftime("%H:%M") == "22:40" and not a.get("held_for_audience_hours")
    assert datetime.fromtimestamp(b["at"], ny).strftime("%d %H:%M") == "14 08:30" and b.get("held_for_audience_hours")


def test_account_errors_hold_post_errors_fail():
    from bore.publish import outage_error
    for e in ("SocialPilot error: Your Account is Locked", "SocialPilot account is locked", "SocialPilot connector not attached to the routine",
              "Your trial has ended, upgrade your plan", "No such tool available: mcp__socialpilot__CreatePost",
              "Your trial period has ended", "Subscription is inactive", "Your plan has expired", "Account is currently locked"):
        assert outage_error(e), e
    # ambiguous (the post may exist, or it is this post / one page): fail for a person, as before
    for e in ("Error uploading media", "HTTP 503 Service Unavailable", "Request timed out", "Facebook session has expired, reconnect",
              "(#200) permissions error", "Page needs to be reconnected",
              "This account has been disabled by Facebook", "", None):
        assert not outage_error(e), e
    # the publisher's own messages, and words inside draft ids, never count
    for e in ("superseded by its update huddle-202610130209-account-locked-out-of-trial",
              "publisher run interrupted", "not delivered: missing from SocialPilot",
              "failed: see huddle-202610130209-bears-trial-has-ended-for-rookie"):
        assert not outage_error(e), e


def test_socialpilot_down_is_tried_about_hourly():
    from bore.publish import split_waiting
    now = 1_800_000_000
    q = [{"id": "a", "approved_at": now - 600, "holds": 1}, {"id": "b", "approved_at": now - 7200, "holds": 0},
         {"id": "c", "approved_at": now - 3 * 3600, "holds": 2}]
    ready, waiting = split_waiting(q, {"down": True, "last_try_at": (now - 600) * 1000}, now)
    assert ready == [] and len(waiting) == 3                       # tried 10 minutes ago: everything waits
    ready, waiting = split_waiting(q, {"down": True, "last_try_at": (now - 3600) * 1000}, now)
    assert [x["id"] for x in ready] == ["b", "c"] and [x["id"] for x in waiting] == ["a"]
    ready, waiting = split_waiting(q[1:], {"down": False}, now)
    assert len(ready) == 2 and waiting == []


def test_mark_failed_with_an_account_error_holds_the_draft(pub, capsys):
    pub.dash.data["queue"] = [_draft("huddle-1")]
    pub.preflight()
    out = pub.plan()
    pub.claim("huddle-1", out)
    claim = json.loads((pub.root / "work" / ".claims" / "huddle-1").read_text())
    assert claim["claim"] == "1791864091866" and claim["login_id"] == 2662936 and claim["caption"] == "Caption huddle-1"
    assert pub.run("mark", "huddle-1", status="failed", error="SocialPilot error: Your Account is Locked") == 4
    assert pub.dash.sent[-1][1] == "held" and pub.dash.sent[-1][2]["claim"] == "1791864091866"
    assert "HELD huddle-1" in capsys.readouterr().out
    assert pub.run("mark", "huddle-1", status="failed", error="Error uploading media") == 0     # one post's problem
    assert pub.dash.sent[-1][1] == "failed"


def test_a_blocked_session_call_is_held_not_failed(pub):
    """The routine's own permission check stopping CreatePost is not the post's fault (10-20 09:27)."""
    from bore.publish import outage_error, session_blocked
    for e in ("CreatePost blocked by auto mode classifier", "Permission to use mcp__socialpilot__CreatePost has been denied",
              "Tool use was rejected by the session"):
        assert session_blocked(e) and not outage_error(e), e
    for e in ("(#200) permissions error", "This account has been disabled by Facebook", "Error uploading media",
              "superseded by its update huddle-202610130209-classifier-blocked-again", None):
        assert not session_blocked(e), e
    pub.dash.data["queue"] = [_draft("huddle-1")]
    pub.preflight()
    pub.claim("huddle-1", pub.plan())
    assert pub.run("mark", "huddle-1", status="failed", error="CreatePost blocked by auto mode classifier") == 4
    assert pub.dash.sent[-1][1] == "held" and pub.dash.sent[-1][2]["claim"]
    assert pub.dash.sent[-1][2]["scope"] == "run"            # not SocialPilot: it is not marked down for everyone


def _old_dashboard_400(error="status must be publishing, scheduled, posted, failed or rejected"):
    """What a dashboard from before answers a status it does not take yet (or another 400)."""
    import requests
    r = requests.Response()
    r.status_code = 400
    r._content = json.dumps({"error": error}).encode()
    return requests.HTTPError(response=r)


def test_old_dashboard_without_holds_gets_a_failure(monkeypatch, tmp_path):
    from bore import cli
    sent = []

    class Dash:
        def report(self, draft_id, status, **detail):
            sent.append(status)
            if status == "held":
                raise _old_dashboard_400()
            return {"ok": True, "status": status}
    monkeypatch.setattr(cli, "_dashboard", lambda: Dash())
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "socialpilot.yaml").write_text("accounts: {}\n")
    cli.cmd_publish(argparse.Namespace(action="mark", id="h-1", status="failed", error="Your Account is Locked", post_id=None,
                                       url=None, scheduled_for=None, busy=None), {"_root": str(tmp_path)})
    assert sent == ["held", "failed"]


# ---- preflight: SocialPilot must answer before anything is claimed (finding 29) --------------------

def test_preflight_failure_holds_the_run_whatever_the_wording(pub, capsys):
    pub.dash.data["queue"] = [_draft("huddle-1"), _draft("culture-rush-1", page="culture-rush")]
    assert pub.preflight(accounts=LOCKED) == 4
    held = pub.dash.statuses("held")
    assert [i for i, _ in held] == ["huddle-1", "culture-rush-1"]
    assert all(d["error"] == "SocialPilot error: Your Account is Locked" for _, d in held)
    assert "go to step 6" in capsys.readouterr().out
    # nothing can be claimed in this run
    assert pub.claim("huddle-1", pub.plan()) == 4 and not pub.dash.statuses("publishing")


@pytest.mark.parametrize("how", ["error", "text", "timezone"])
def test_preflight_holds_on_an_unknown_outage_or_a_timezone_change(pub, how):
    pub.dash.data["queue"] = [_draft("huddle-1")]
    if how == "error":            # the call itself failed, worded in a way OUTAGE_RE does not know
        assert pub.run("preflight", error="MCP error -32001: Request timed out") == 4
    elif how == "text":           # what the model saved is the connector's error, not a result
        assert pub.run("preflight", result=[pub.save("accounts.json", "Error: 503 Service Unavailable")]) == 4
    else:                         # every scheduleDateTime would be read in another timezone
        assert pub.preflight(userinfo={"success": True, "data": {"setting": {"timeZone": "America/New_York"}}}) == 4
    [(draft_id, detail)] = pub.dash.statuses("held")
    assert draft_id == "huddle-1" and detail["error"]
    if how == "timezone":
        assert "America/New_York" in detail["error"]


def test_preflight_mistakes_hold_nothing(pub):
    pub.dash.data["queue"] = [_draft("huddle-1")]
    assert "--result" in pub.run("preflight")
    assert "empty" in pub.run("preflight", result=[pub.save("accounts.json", " ")])
    assert "AccountList" in pub.run("preflight", result=[pub.save("userinfo.json", USERINFO)])
    assert pub.dash.sent == []
    # the tool's own content wrapper is fine
    wrapped = [{"type": "text", "text": json.dumps(ACCOUNTS)}]
    assert pub.run("preflight", result=[pub.save("accounts.json", wrapped)]) == 0


def test_a_claim_needs_the_preflight_and_the_planned_slot(pub, monkeypatch, capsys):
    pub.dash.data["queue"] = [_draft("huddle-1")]
    out = pub.plan()
    assert "preflight" in pub.claim("huddle-1", out)
    assert pub.preflight() == 0
    out = pub.plan()
    assert "schedule_utc" in pub.run("mark", "huddle-1", status="publishing", scheduled_for="2026-01-01T00:00:00Z")
    assert "to_schedule" in pub.run("mark", "huddle-2", status="publishing", scheduled_for=out["to_schedule"][0]["schedule_utc"])
    assert not pub.dash.statuses("publishing")
    # an old plan: the slot is now less than an hour away, so it is planned again instead
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 20 * 60)
    assert pub.claim("huddle-1", out) == 3 and "SKIP huddle-1" in capsys.readouterr().out
    monkeypatch.setattr(time, "time", real)
    assert pub.claim("huddle-1", out) == 0
    assert pub.dash.statuses("publishing") == [("huddle-1", {"scheduled_for": out["to_schedule"][0]["schedule_utc"]})]


# ---- the per-run breaker (finding 29) ---------------------------------------------------------------

def test_a_second_unknown_failure_on_one_account_holds_its_drafts(pub, capsys):
    """3 drafts on a page, 2 different unrecognised errors: the second is held, and so is the first.
    Only that page's drafts wait (exit 3: the run goes on), and nothing is held twice."""
    pub.dash.data["queue"] = [_draft("huddle-1"), _draft("huddle-2"), _draft("huddle-3"),
                              _draft("culture-rush-1", page="culture-rush")]
    pub.preflight()
    out = pub.plan()
    assert pub.claim("huddle-1", out) == 0
    assert pub.run("mark", "huddle-1", status="failed", error="401 Unauthorized") == 0
    assert pub.dash.sent[-1][:2] == ("huddle-1", "failed")
    assert pub.claim("huddle-2", out) == 0
    assert pub.run("mark", "huddle-2", status="failed", error="MCP error -32001: Request timed out") == 3
    held = pub.dash.statuses("held")
    assert [i for i, _ in held] == ["huddle-2", "huddle-1"]          # the first failure waits as well
    assert all(d["claim"] == "1791864091866" and d["scope"] == "account" for _, d in held)
    assert held[1][1]["error"] == "401 Unauthorized"
    assert "SKIP huddle-2: held, not failed" in capsys.readouterr().out
    assert pub.claim("huddle-3", out) == 0                           # still tried: it may well work
    assert pub.run("mark", "huddle-3", status="failed", error="401 Unauthorized") == 3
    assert [i for i, _ in pub.dash.statuses("held")] == ["huddle-2", "huddle-1", "huddle-3"]
    assert pub.claim("culture-rush-1", out) == 0                     # the other page goes on
    assert pub.scheduled("culture-rush-1", pub.entry("culture-rush-1", out)) == 0


def test_a_success_on_the_same_account_between_two_failures_keeps_them_separate(pub):
    pub.dash.data["queue"] = [_draft("huddle-1"), _draft("huddle-2"), _draft("huddle-3"),
                              _draft("culture-rush-1", page="culture-rush"), _draft("huddle-4")]
    pub.preflight()
    out = pub.plan()
    pub.claim("huddle-1", out)
    assert pub.run("mark", "huddle-1", status="failed", error="(#200) permissions error") == 0
    pub.claim("huddle-2", out)
    assert pub.scheduled("huddle-2", pub.entry("huddle-2", out)) == 0
    pub.claim("huddle-3", out)
    assert pub.run("mark", "huddle-3", status="failed", error="(#200) permissions error") == 0
    assert [s for _, s, _ in pub.dash.sent if s in ("failed", "held")] == ["failed", "failed"]
    # a post on another page says nothing about this page's account
    pub.claim("culture-rush-1", out)
    assert pub.scheduled("culture-rush-1", pub.entry("culture-rush-1", out)) == 0
    pub.claim("huddle-4", out)
    assert pub.run("mark", "huddle-4", status="failed", error="(#200) permissions error") == 3
    # the publisher's own messages are never SocialPilot failing
    assert pub.run("mark", "huddle-3", status="failed", error="earlier copy in SocialPilot: unscheduled") == 0


def test_one_pages_broken_account_never_stops_the_other_pages(pub, capsys):
    """The reviewer's case: Huddle Room needs reconnecting in SocialPilot. Its drafts wait (held for
    that account only), Culture Rush's draft is scheduled in the same run, and the next run's plan
    shows no SocialPilot outage. It used to hold everything and stop every run for up to 12 hours."""
    reconnect = copy.deepcopy(ACCOUNTS)
    reconnect["accounts"][1]["isReconnect"] = True
    pub.dash.data["queue"] = [_draft("h1"), _draft("h2"), _draft("c1", page="culture-rush")]
    assert pub.preflight(accounts=reconnect) == 0
    said = capsys.readouterr().out
    assert "NOTE account Huddle Room (2662936) needs reconnecting" in said and "OK SocialPilot answers" in said
    assert not pub.dash.statuses("held")                     # nothing held before anything was tried
    out = pub.plan()
    assert [x["id"] for x in out["to_schedule"]] == ["h1", "h2", "c1"]
    for i in ("h1", "h2"):                                   # SocialPilot flags the account: the first failure waits
        assert pub.claim(i, out) == 0
        assert pub.run("mark", i, status="failed", error="Page needs to be reconnected") == 3
    assert [(i, d["scope"]) for i, d in pub.dash.statuses("held")] == [("h1", "account"), ("h2", "account")]
    assert not pub.dash.statuses("failed")
    assert pub.claim("c1", out) == 0
    assert pub.scheduled("c1", pub.entry("c1", out)) == 0
    assert pub.dash.sent[-1][:2] == ("c1", "scheduled")
    # the next run: the held drafts wait about an hour (they keep their place), SocialPilot is not "down"
    (pub.root / "work" / ".publish-run.json").unlink()
    now = int(time.time())
    pub.dash.data["queue"] = [_draft("h1", holds=1, held_scope="account", held_error="Page needs to be reconnected", approved_at=now - 60),
                              _draft("h2", holds=1, held_scope="account", held_error="Page needs to be reconnected", approved_at=now - 60),
                              _draft("h3"), _draft("c2", page="culture-rush")]
    assert pub.preflight() == 0                              # SocialPilot no longer flags it
    nxt = pub.plan()
    assert [x["id"] for x in nxt["to_schedule"]] == ["h3", "c2"]
    assert [x["id"] for x in nxt["waiting_for_socialpilot"]] == ["h1", "h2"]
    assert "socialpilot_down" not in nxt
    # the page's drafts are still waiting for its account: a new one that fails waits too, at once
    pub.claim("h3", nxt)
    assert pub.run("mark", "h3", status="failed", error="Page needs to be reconnected") == 3
    assert pub.dash.statuses("held")[-1][1]["scope"] == "account"
    pub.claim("c2", nxt)
    assert pub.scheduled("c2", pub.entry("c2", nxt)) == 0


def test_a_pages_failure_in_an_earlier_run_holds_its_next_failure(pub, capsys):
    """Every approval starts its own run, in a fresh container: the run's breaker alone let every
    post of an unknown CreatePost outage fail one by one (Oct 10-11, s4_unknown_outage.py). The
    dashboard keeps the page's last unknown failure for an hour (health/socialpilot failing_pages),
    so a later run holds the next failure there at once; the other pages are not affected."""
    import shutil
    unknown = "Something went wrong, please try again later (E5021)"
    pub.dash.data["queue"] = [_draft("h1")]
    pub.preflight()
    out = pub.plan()
    pub.claim("h1", out)
    assert pub.run("mark", "h1", status="failed", error=unknown) == 0          # the page's first: failed,
    assert pub.dash.sent[-1] == ("h1", "failed", {"error": unknown, "breaker": True})   # and the dashboard keeps it
    # the next approval's run, in a fresh container: only what the dashboard keeps
    shutil.rmtree(pub.root / "work")
    now_ms = time.time() * 1000
    pub.dash.data["queue"] = [_draft("h2"), _draft("c1", page="culture-rush")]
    pub.dash.data["socialpilot"] = {"failing_pages": {"huddle": {"id": "h1", "error": unknown, "at": now_ms - 10 * 60e3}}}
    pub.preflight()
    out = pub.plan()
    pub.claim("h2", out)
    assert pub.run("mark", "h2", status="failed", error=unknown) == 3
    assert pub.dash.sent[-1][:2] == ("h2", "held") and pub.dash.sent[-1][2]["scope"] == "account"
    assert "SKIP h2: held, not failed: a post on this page failed 10 min ago" in capsys.readouterr().out
    pub.claim("c1", out)
    assert pub.run("mark", "c1", status="failed", error=unknown) == 0          # another page: its own first
    assert pub.dash.sent[-1][:2] == ("c1", "failed")
    # over an hour ago it no longer counts
    shutil.rmtree(pub.root / "work")
    pub.dash.data["queue"] = [_draft("h3")]
    pub.dash.data["socialpilot"]["failing_pages"]["huddle"]["at"] = now_ms - 61 * 60e3
    pub.preflight()
    out = pub.plan()
    pub.claim("h3", out)
    assert pub.run("mark", "h3", status="failed", error=unknown) == 0
    assert pub.dash.sent[-1][:2] == ("h3", "failed")


def test_a_post_retried_after_its_own_failure_fails_again_not_held(pub, capsys):
    """The page's flag names the draft that failed. That same post failing again within the hour
    (an editor's Retry, or the Studio sending it again as a new draft) is that post's trouble (a bad
    image), not its account's: it fails with its real error, as before the breaker spanned runs. It
    used to be held as 'account' for its own failure, keep the page flagged for every run and move
    to Failed 12 hours later as a page whose account needs reconnecting. Another post is still held."""
    import shutil
    bad = "Error uploading media"
    pub.dash.data["queue"] = [_draft("h1")]
    pub.preflight()
    out = pub.plan()
    pub.claim("h1", out)
    assert pub.run("mark", "h1", status="failed", error=bad) == 0
    assert pub.dash.sent[-1] == ("h1", "failed", {"error": bad, "breaker": True})

    def run(queue, failed_id):               # the next approval's run, in a fresh container
        shutil.rmtree(pub.root / "work")
        pub.dash.data["queue"] = queue
        pub.dash.data["socialpilot"] = {"failing_pages": {"huddle": {"id": failed_id, "error": bad, "at": time.time() * 1000 - 5 * 60e3}}}
        pub.preflight()
        return pub.plan()
    # an editor retries h1 five minutes later: it fails the same way, and the page's flag is renewed
    out = run([_draft("h1")], "h1")
    pub.claim("h1", out)
    assert pub.run("mark", "h1", status="failed", error=bad) == 0
    assert pub.dash.sent[-1] == ("h1", "failed", {"error": bad, "breaker": True})
    assert "held" not in capsys.readouterr().out
    # sent again from the Studio as a new draft (resent_from): the same post
    out = run([_draft("s2", resent_from="s1", studio=True)], "s1")
    pub.claim("s2", out)
    assert pub.run("mark", "s2", status="failed", error=bad) == 0
    assert pub.dash.sent[-1][:2] == ("s2", "failed")
    # another post on the page failing within the hour still waits: two posts failed there
    out = run([_draft("h2")], "h1")
    pub.claim("h2", out)
    assert pub.run("mark", "h2", status="failed", error=bad) == 3
    assert pub.dash.sent[-1][:2] == ("h2", "held") and pub.dash.sent[-1][2]["scope"] == "account"
    # an account SocialPilot itself flags holds the post whose failure the page's flag is, too,
    # whether its preflight came before the plan or after it
    reconnect = copy.deepcopy(ACCOUNTS)
    reconnect["accounts"][1]["isReconnect"] = True
    for again in (False, True):
        out = run([_draft("h1")], "h1")
        if not again:
            shutil.rmtree(pub.root / "work")
            assert pub.preflight(accounts=reconnect) == 0
            out = pub.plan()
        else:
            assert pub.preflight(accounts=reconnect) == 0
        pub.claim("h1", out)
        assert pub.run("mark", "h1", status="failed", error=bad) == 3, again
        assert pub.dash.sent[-1][:2] == ("h1", "held")


def test_an_outage_wording_or_a_session_block_still_stops_the_run(pub, capsys):
    pub.dash.data["queue"] = [_draft("huddle-1"), _draft("culture-rush-1", page="culture-rush"), _draft("huddle-2")]
    pub.preflight()
    out = pub.plan()
    pub.claim("huddle-1", out)
    assert pub.run("mark", "huddle-1", status="failed", error="CreatePost blocked by auto mode classifier") == 4
    assert pub.dash.sent[-1][2]["scope"] == "run"            # this session: SocialPilot is not marked down
    assert pub.claim("culture-rush-1", out) == 4
    (pub.root / "work" / ".publish-run.json").unlink()
    pub.preflight()
    pub.claim("culture-rush-1", out)
    assert pub.run("mark", "culture-rush-1", status="failed", error="SocialPilot error: Your Account is Locked") == 4
    assert "scope" not in pub.dash.sent[-1][2]               # SocialPilot itself: every page waits
    assert pub.claim("huddle-2", out) == 4


def test_a_failed_lookup_holds_only_that_draft(pub, capsys):
    """PUBLISH.md 4.2: a lookup error for one draft is not SocialPilot being down: only that draft
    waits (scope draft), and the run goes on with the next item."""
    pub.dash.data["queue"] = [_draft("h-1", ever_sent=True, prior_post_id="sp-1"), _draft("c-1", page="culture-rush")]
    pub.preflight()
    out = pub.plan()
    pub.claim("h-1", out)
    assert pub.run("mark", "h-1", status="held", error="lookup failed: ViewPost: MCP error -32001: Request timed out") == 3
    _, status, detail = pub.dash.sent[-1]
    assert status == "held" and detail["scope"] == "draft" and detail["claim"] == "1791864091866"
    assert "SKIP h-1: held: a lookup failed" in capsys.readouterr().out
    assert pub.claim("c-1", out) == 0                       # the run was not stopped
    # the dashboard moves it to Failed once its lookups failed too often: the run still goes on
    pub.dash.answers["held"] = lambda i, d: {"ok": True, "id": i, "status": "failed", "error": "lookups failed: ..."}
    assert pub.run("mark", "h-1", status="held", error="lookup failed: DeliveredPosts: 502") == 0
    assert "FAILED h-1" in capsys.readouterr().out
    # held because SocialPilot itself says it is locked: every page waits, as before
    del pub.dash.answers["held"]
    assert pub.run("mark", "h-1", status="held", error="lookup failed: Your Account is Locked") == 4
    assert "scope" not in pub.dash.sent[-1][2]


@pytest.mark.parametrize("how", ["fenced", "label", "python", "text_between", "calcutta", "wrapped"])
def test_preflight_reads_results_however_they_were_saved(pub, how):
    """A slip in how the model wrote the file is not SocialPilot failing (it used to hold every page)."""
    a, u = json.dumps(ACCOUNTS), json.dumps(USERINFO)
    text = {"fenced": f"```json\n{a}\n```\n```json\n{u}\n```", "label": f"AccountList result:\n{a}\nUserInfo result:\n{u}",
            "python": repr(ACCOUNTS), "text_between": f"{a}\nUserInfo: {u}",
            "calcutta": a + json.dumps({"success": True, "data": {"setting": {"timeZone": "Asia/Calcutta"}}}),
            "wrapped": json.dumps([{"type": "text", "text": a}]) + "\n" + u}[how]
    pub.dash.data["queue"] = [_draft("huddle-1")]
    assert pub.run("preflight", result=[pub.save("accounts.json", text)]) == 0
    assert not pub.dash.statuses("held")
    # the timezone is still checked when the UserInfo result comes after other text
    if how == "text_between":
        ny = text.replace("Asia/Kolkata", "America/New_York")
        (pub.root / "work" / ".publish-run.json").unlink()
        assert pub.run("preflight", result=[pub.save("accounts.json", ny)]) == 4


def test_preflight_holds_only_on_what_reads_like_a_failed_call(pub, capsys):
    pub.dash.data["queue"] = [_draft("huddle-1")]
    # no result and nothing like an error in it: a mistake in the file, nothing is held
    assert "save the result exactly" in pub.run("preflight", result=[pub.save("accounts.json", "Here are the accounts:")])
    assert not pub.dash.sent
    assert pub.run("preflight", result=[pub.save("accounts.json", "Error: 503 Service Unavailable")]) == 4
    # no UserInfo: still OK, and the summary is told the timezone was not checked
    (pub.root / "work" / ".publish-run.json").unlink()
    assert pub.run("preflight", result=[pub.save("accounts.json", ACCOUNTS)]) == 0
    assert "timezone was not checked" in capsys.readouterr().out


def test_zones_compare_by_their_clocks():
    from bore.publish import same_zone
    for a in ("Asia/Kolkata", "Asia/Calcutta", "(GMT+05:30) Asia/Kolkata", "UTC+05:30", "GMT+5:30"):
        assert same_zone(a, "Asia/Kolkata"), a
    for a in ("America/New_York", "GMT-05:00", "IST", "", "Kolkata time"):
        assert not same_zone(a, "Asia/Kolkata"), a
    assert same_zone("US/Eastern", "America/New_York") and not same_zone("UTC-05:00", "America/New_York")


def test_a_second_preflight_keeps_the_runs_failures_and_its_stop(pub, capsys):
    pub.dash.data["queue"] = [_draft("huddle-1"), _draft("huddle-2"), _draft("culture-rush-1", page="culture-rush")]
    pub.preflight()
    out = pub.plan()
    pub.claim("huddle-1", out)
    assert pub.run("mark", "huddle-1", status="failed", error="401 Unauthorized") == 0
    assert pub.preflight() == 0                             # checking SocialPilot again forgets nothing
    pub.claim("huddle-2", out)
    assert pub.run("mark", "huddle-2", status="failed", error="401 Unauthorized") == 3
    pub.claim("culture-rush-1", out)
    assert pub.run("mark", "culture-rush-1", status="failed", error="Your Account is Locked") == 4
    capsys.readouterr()
    assert pub.preflight() == 4                             # a stopped run stays stopped
    assert "already stopped scheduling" in capsys.readouterr().out
    assert pub.claim("huddle-2", out) == 4


def test_publish_queue_changes_nothing(pub):
    """A look at the queue (e.g. from a diagnostics session) never records results; `plan` does."""
    pub.dash.data["queue"] = [_draft("h-1"), _draft("x-1", page="no-account")]
    assert pub.run("queue") == 0
    assert pub.dash.sent == [] and not (pub.root / "work" / ".plan").exists()
    pub.plan()
    assert [i for i, _ in pub.dash.statuses("failed")] == ["x-1"] and (pub.root / "work" / ".plan" / "h-1").exists()


def test_a_created_post_is_never_failed_when_the_queue_does_not_show_it(pub, capsys):
    """CreatePost worked but QueuedPosts misses it: one wider look, then it is recorded as scheduled
    at its slot with CreatePost's post id (the delivery check finds it), for a person to check.
    It used to be failed "created but not in SocialPilot's queue", and a Retry posted it twice."""
    pub.dash.data["queue"] = [_draft("huddle-1"), _draft("huddle-2"), _draft("huddle-3")]
    pub.preflight()
    out = pub.plan()
    item = out["to_schedule"][0]
    pub.claim("huddle-1", out)
    msg = pub.scheduled("huddle-1", post_id="0fa8d79d-653f-48f8-9682-1f9d0a220b14")
    assert "NOT FOUND" in msg and f"startDate = endDate = {item['schedule_local'][:10]}" in msg and "no q" in msg
    assert not pub.dash.statuses("scheduled")
    assert pub.scheduled("huddle-1", post_id="0fa8d79d-653f-48f8-9682-1f9d0a220b14") == 5
    _, status, detail = pub.dash.sent[-1]
    assert status == "scheduled" and detail["post_id"] == "0fa8d79d-653f-48f8-9682-1f9d0a220b14"
    assert detail["scheduled_for"] == item["schedule_utc"] and detail["sp_login_id"] == 2662936 and detail["copies"] == 0
    assert "did not show it" in detail["needs_action"] and "error" not in detail
    assert "STOP huddle-1" in capsys.readouterr().out
    # the old wording, from a model that remembers it, is recorded the same way
    (pub.root / "work" / ".publish-run.json").unlink()
    pub.preflight()
    pub.claim("huddle-2", out)
    assert pub.run("mark", "huddle-2", status="failed", error="created but not in SocialPilot's queue", post_id="cp-2") == 5
    assert pub.dash.sent[-1][1] == "scheduled" and not pub.dash.statuses("failed")
    # another run's claim (interrupted): never recorded as scheduled on a guess
    pub.dash.sent.clear()
    (pub.root / "work" / ".plan" / "huddle-3").write_text(json.dumps({"caption": "Caption huddle-3", "login_id": 2662936,
                                                                     "scheduled_for": item["schedule_utc"]}))
    assert "NOT FOUND" in pub.scheduled("huddle-3")
    assert "publisher run interrupted" in pub.scheduled("huddle-3") and pub.dash.sent == []


# ---- what `mark --status scheduled` checks (finding 31) -------------------------------------------

def test_a_scheduled_post_is_checked_against_its_claim(pub, capsys):
    pub.dash.data["queue"] = [_draft("huddle-1")]
    pub.preflight()
    out = pub.plan()
    pub.claim("huddle-1", out)
    assert "--queued" in pub.run("mark", "huddle-1", status="scheduled", post_id="cp-1")
    item = out["to_schedule"][0]
    other = pub.entry("huddle-1", out, postDesc="Another post", postTimeFormat=_shift(item["schedule_local"], 45))
    assert "NOT FOUND" in pub.scheduled("huddle-1", other)
    assert pub.scheduled("huddle-1", pub.entry("huddle-1", out, postDesc=item["caption"].replace(" ", "\n"))) == 0
    _, status, detail = pub.dash.sent[-1]
    assert status == "scheduled" and "needs_action" not in detail
    assert detail == {"post_id": "cp-1", "sp_post_id": "sp-huddle-1", "sp_login_id": 2662936, "copies": 1,
                      "scheduled_for": item["schedule_utc"]}


def _shift(local, minutes):
    t = datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp() + minutes * 60
    return datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d %H:%M")


@pytest.mark.parametrize("case", ["wrong_account", "copy_elsewhere", "utc_typed_as_ist", "two_copies", "caption_changed",
                                  "made_earlier"])
def test_a_broken_rule_is_recorded_for_a_person_and_stops_the_run(pub, capsys, case):
    pub.dash.data["queue"] = [_draft("huddle-1"), _draft("huddle-2")]
    pub.preflight()
    out = pub.plan()
    pub.claim("huddle-1", out)
    item = out["to_schedule"][0]
    e = pub.entry("huddle-1", out)
    entries = {"wrong_account": [{**e, "loginId": 2662937, "accountUsername": "Keaton Caleb"}],
               # QueuedPosts without an account filter: its own copy is right, a second one is on Keaton Caleb
               "copy_elsewhere": [e, {**e, "postId": "sp-kc", "loginId": 2662937, "accountUsername": "Keaton Caleb"}],
               "utc_typed_as_ist": [{**e, "postTimeFormat": _shift(item["schedule_local"], -330)}],
               "two_copies": [e, {**e, "postId": "sp-again"}],
               "caption_changed": [{**e, "postDesc": "Caption huddle-1, but shorter"}],
               "made_earlier": [{**e, "postTimeFormat": _shift(item["schedule_local"], 90), "createdOn": "Oct 01, 2026 09:31 AM"}]}[case]
    code = pub.scheduled("huddle-1", *entries)
    _, status, detail = pub.dash.sent[-1]
    if case == "made_earlier":      # a post an earlier run made (the duplicate guard found it): its own time is recorded
        assert code == 0 and status == "scheduled" and "needs_action" not in detail
        assert detail["scheduled_for"] != item["schedule_utc"]
        return
    if case == "caption_changed":   # recorded for a person, but the run goes on
        assert code == 0 and "caption" in detail["needs_action"]
        return
    assert code == 5 and "STOP huddle-1" in capsys.readouterr().out
    assert {"wrong_account": "wrong SocialPilot account", "copy_elsewhere": "Keaton Caleb, loginId 2662937",
            "utc_typed_as_ist": "not the planned", "two_copies": "2 copies"}[case] in detail["needs_action"]
    assert status == ("failed" if case == "wrong_account" else "scheduled")
    if case == "copy_elsewhere":    # its own copy goes out: recorded as scheduled (a Retry would post it twice)
        assert detail["sp_post_id"] == "sp-huddle-1" and detail["sp_login_id"] == 2662936 and detail["copies"] == 1
    if case == "utc_typed_as_ist":  # SocialPilot's real time, so later plans space posts around it
        assert detail["scheduled_for"] == datetime.fromtimestamp(
            datetime.strptime(item["schedule_local"], "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp() - 330 * 60,
            timezone.utc).isoformat().replace("+00:00", "Z")
    assert pub.claim("huddle-2", out) == 4              # nothing else is scheduled in this run


# ---- what the plan settles itself (findings 35, 76) -------------------------------------------------

def test_the_plan_fails_drafts_without_an_account_and_flags_outdated_originals(pub, capsys):
    soon = datetime.fromtimestamp(time.time() + 7200, timezone.utc).isoformat().replace("+00:00", "Z")
    pub.dash.data["scheduled"] = [{"id": "huddle-orig", "page": "huddle", "page_name": "Huddle Room", "scheduled_for": soon,
                                   "post_id": "cp-0", "sp_post_id": "sp-0", "sp_login_id": None, "socialpilot_login_id": "",
                                   "caption_start": "Caption huddle-orig", "update_of": None}]
    pub.dash.data["queue"] = [_draft("huddle-upd", update_of="huddle-orig"), _draft("new-1", page="new-page"),
                              _draft("blocked-1", page="other", socialpilot_login_id="2662937"),
                              _draft("unread-1", page="unread", login_error=True),
                              _draft("admin-1", page="admin-page", page_name="Admin Page", socialpilot_login_id="555")]
    out = pub.plan()
    assert [x["id"] for x in out["to_schedule"]] == ["huddle-upd", "admin-1"]
    assert out["to_schedule"][1]["socialpilot_account"] == {"name": "Admin Page", "loginId": 555}
    assert [x["id"] for x in out["no_account"]] == ["new-1", "blocked-1"]
    failed = dict(pub.dash.statuses("failed"))
    assert failed["new-1"]["error"].startswith("page has no SocialPilot account: set it in the admin panel")
    assert "2662937" in failed["blocked-1"]["error"] and "unread-1" not in failed      # unreadable: tried next run
    [(orig, act)] = pub.dash.statuses("needs_action")
    assert orig == "huddle-orig" and act["error"].startswith("delete it in SocialPilot: it is scheduled for ")
    assert "huddle-upd" in act["error"] and act["kind"] == "superseded_original"     # Done also retires it
    s = out["already_scheduled"][0]
    assert s["socialpilot_account"]["loginId"] == 2662936 and s["caption_start"] == "Caption huddle-orig"
    assert s["day"] == s["schedule_local"][:10] and s["sp_post_id"] == "sp-0"
    assert "no usable SocialPilot account" in capsys.readouterr().err       # the run report still sees it


def test_drafts_sent_before_are_looked_up_before_they_are_created(pub):
    first = int(time.time()) - 3 * 3600
    pub.dash.data["queue"] = [_draft("h-1", ever_sent=True, prior_post_id="sp-9", first_approved_at=first),
                              _draft("h-2", holds=2, approved_at=first), _draft("h-3")]
    items = {x["id"]: x for x in pub.plan()["to_schedule"]}
    assert [items[k]["check_existing"] for k in ("h-1", "h-2", "h-3")] == [True, True, False]
    assert items["h-1"]["prior_post_id"] == "sp-9"
    assert items["h-1"]["search_from"] == datetime.fromtimestamp(first, IST).strftime("%Y-%m-%d")


def test_a_retried_post_whose_old_copy_socialpilot_dropped_is_created_again(pub, capsys):
    """After a lock, editors retry the posts SocialPilot errored or dropped (Failed tab, Retry). The
    old copy, only in ErrorPosts or UnscheduledPosts, never goes out by itself: the retry creates the
    post again. PUBLISH.md 4.2 used to fail it as "earlier copy in SocialPilot", at every retry."""
    # scheduled, then 'unscheduled by SocialPilot' (step 5), then retried by an editor
    pub.dash.data["queue"] = [_draft("h-1", ever_sent=True, prior_post_id="sp-old")]
    pub.preflight()
    out = pub.plan()
    assert [out["to_schedule"][0][k] for k in ("check_existing", "prior_post_id")] == [True, "sp-old"]
    assert pub.claim("h-1", out) == 0
    # 4.2: ViewPost shows sp-old unscheduled, DeliveredPosts has nothing: CreatePost (4.3), then 4.4
    assert pub.scheduled("h-1", pub.entry("h-1", out)) == 0
    assert pub.dash.sent[-1][:2] == ("h-1", "scheduled")
    # the old copy is recorded for a person: rescheduled by hand in SocialPilot (as people do after
    # a lock) it would post the story twice, and nothing else said it is there
    note = pub.dash.sent[-1][2]["needs_action"]
    assert "SocialPilot post sp-old" in note and "never reschedule it" in note
    assert "NEEDS ACTION h-1" in capsys.readouterr().out
    # the lookups stop a create only for a copy that is queued or delivered, and never fail the draft
    step = re.search(r"\n2\. Duplicate guard:(.*?)\n3\. Call \*\*CreatePost\*\*", (ROOT / "PUBLISH.md").read_text(), re.S).group(1)
    assert "--status failed" not in step and "earlier copy" not in step
    assert "errored or unscheduled: that copy never goes out by itself" in " ".join(step.split())
    # ViewPost showed the old copy still scheduled: 4.4 records that copy itself, nothing to delete
    pub.dash.data["queue"] = [_draft("h-2", ever_sent=True, prior_post_id="sp-h-2")]
    out = pub.plan()
    pub.claim("h-2", out)
    assert pub.scheduled("h-2", pub.entry("h-2", out), post_id="") == 0
    assert pub.dash.sent[-1][:2] == ("h-2", "scheduled") and "needs_action" not in pub.dash.sent[-1][2]
    # a first send has no old copy
    pub.dash.data["queue"] = [_draft("h-3")]
    out = pub.plan()
    pub.claim("h-3", out)
    assert pub.scheduled("h-3", pub.entry("h-3", out)) == 0
    assert "needs_action" not in pub.dash.sent[-1][2]


def test_busy_times_name_the_page_by_slug_name_or_account(pub, capsys):
    pub.dash.data["queue"] = [_draft("h-1")]
    free = pub.plan()["to_schedule"][0]["schedule_local"]
    for name in ("huddle", "Huddle Room", "2662936"):
        got = pub.plan(busy=[f"{name}={free}"])["to_schedule"][0]["schedule_local"]
        assert got >= _shift(free, 45), name
    out = pub.plan(busy=[f"Keaton Caleb={free}"])
    assert out["busy_ignored"] == ["Keaton Caleb"] and out["to_schedule"][0]["schedule_local"] == free
    assert "ignored" in capsys.readouterr().err


# ---- delivery check: "not delivered" only after two runs (finding 30) --------------------------------

def test_a_missing_post_is_counted_not_failed_at_once(pub, capsys):
    import requests
    pub.dash.answers["missing"] = lambda i, d: {"ok": True, "id": i, "status": "scheduled", "misses": 1}
    assert pub.run("mark", "h-1", status="missing") == 0
    assert "MISSING h-1" in capsys.readouterr().out
    pub.dash.answers["missing"] = lambda i, d: {"ok": True, "id": i, "status": "failed", "error": "not delivered: ..."}
    assert pub.run("mark", "h-1", status="missing") == 0
    assert "FAILED h-1" in capsys.readouterr().out

    def old(i, d):                       # a dashboard from before: it must not fail the post instead
        raise _old_dashboard_400()
    pub.dash.answers["missing"] = old
    assert pub.run("mark", "h-1", status="missing") == 0
    assert "LEAVE h-1" in capsys.readouterr().out

    def refused(i, d):                   # any other 400 is about the request: never reported as "leave it"
        raise _old_dashboard_400("not 2 hours past its time yet")
    pub.dash.answers["missing"] = refused
    with pytest.raises(requests.HTTPError):
        pub.run("mark", "h-1", status="missing")


def test_needs_action_says_what_to_do(pub, capsys):
    """Without --error the note would be lost: the command refuses before calling the dashboard
    (it used to answer LEAVE ... 'needs deploying')."""
    assert "--error" in pub.run("mark", "h-1", status="needs_action")
    assert pub.dash.sent == []
    assert pub.run("mark", "h-1", status="needs_action", error="delete the copy scheduled for 2026-10-21 18:30") == 0
    assert pub.dash.sent == [("h-1", "needs_action", {"error": "delete the copy scheduled for 2026-10-21 18:30"})]
    assert "NEEDS ACTION h-1" in capsys.readouterr().out


# ---- PUBLISH.md names only what the plan gives (finding 30: the delivery check used absent fields) ------

DOC_WORDS = {"q", "type", "image", "loginIds", "shareType", "scheduleDateTime", "account",      # connector arguments
             "publishing", "scheduled", "posted", "failed", "held",                              # statuses
             "audience_hours", "held_for_audience_hours"}     # config, and its plan flag (only when it is set)


def _keys(v, prefix=""):
    out = set()
    if isinstance(v, dict):
        for k, x in v.items():
            out |= {k, f"{prefix}{k}"} | _keys(x, f"{k}.")
    elif isinstance(v, list):
        for x in v:
            out |= _keys(x, prefix)
    return out


def test_publish_md_names_only_fields_the_plan_gives(pub):
    now = time.time()
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")
    pub.dash.data = {
        "queue": [_draft("h-new", update_of="h-old"), _draft("h-old"), _draft("h-sent", ever_sent=True, prior_post_id="sp-1"),
                  _draft("h-wait", holds=1, approved_at=int(now) - 60), _draft("x-1", page="no-account")],
        "scheduled": [{"id": "h-sched", "page": "huddle", "scheduled_for": iso(now - 3 * 3600), "post_id": "cp", "sp_post_id": "sp",
                       "sp_login_id": 2662936, "socialpilot_login_id": "", "caption_start": "c", "update_of": None},
                      {"id": "h-was", "page": "huddle", "scheduled_for": iso(now + 3600), "post_id": "cp2", "sp_post_id": None,
                       "sp_login_id": None, "socialpilot_login_id": "", "caption_start": "c2", "update_of": None}],
        "in_progress": [{"id": "h-lost", "page": "huddle", "scheduled_for": iso(now + 7200), "claimed_at": (now - 3600) * 1000,
                         "caption": "c3", "socialpilot_login_id": "", "update_of": None}],
        "socialpilot": {"down": True, "error": "Your Account is Locked", "since": 1, "last_try_at": (now - 3600) * 1000}}
    pub.dash.data["queue"].append(_draft("h-upd", update_of="h-was"))
    out = pub.plan()
    have = _keys(out)
    words = set(re.findall(r"`([a-z][A-Za-z_]*(?:\.[A-Za-z_]+)?)`", (ROOT / "PUBLISH.md").read_text()))
    missing = sorted(w for w in words - DOC_WORDS if w not in have)
    assert not missing, f"PUBLISH.md names fields the plan does not give: {missing}"
    assert {"to_schedule", "superseded", "scheduled_but_superseded", "interrupted", "no_account",
            "waiting_for_socialpilot", "socialpilot_down", "check_existing", "prior_post_id", "search_from",
            "caption_start", "day", "sp_post_id", "socialpilot_account.loginId"} <= words
