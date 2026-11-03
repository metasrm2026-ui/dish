"""One Krea allowance for the hourly routine and Studio sessions (Oct 23): a Studio session keeps no
ledger, so its images were never counted against the month's cap. The dashboard keeps the count; the
hourly routine also sends what its own ledger counted (with the UTC month and day it counted it in,
and only from a database the session restored); a Studio session that cannot reach the count uses the
template card, and the run says so on the admin's Health tab."""
import calendar
import json
import time
from types import SimpleNamespace as NS

import pytest
import requests

from bore import cli, ops
from bore import routine as R
from bore.dashboard import Dashboard, KreaRefused


@pytest.fixture
def wired(monkeypatch, tmp_path, store, cfg):
    asked, built = [], []
    work = tmp_path / "work"
    for d in ("studio/job1/p1", "run1/huddle-1", "run1/studio-fans"):
        (work / d).mkdir(parents=True)
        (work / d / "brief.json").write_text("{}")
        (work / d / "copy.json").write_text("{}")              # a coded layout: no template spec
    monkeypatch.setattr(R, "WORK", work)
    monkeypatch.setattr(cli, "_lint_or_exit", lambda d, cfg, R: None)
    monkeypatch.setattr(cli, "_store", lambda cfg: store)
    monkeypatch.setattr(R, "krea_request", lambda d, cfg, url, fb: built.append(d.name) or {"prompt": "p"})
    store.record_spend("krea:m", 0.015)                      # one image in the hourly routine's own ledger
    cfg["db_path"] = str(tmp_path / "t.db")
    (tmp_path / ".restored").write_text(json.dumps({"counts": {}}))   # this session's pull restored it

    class Dash:
        answer = {"used": 2, "cap": 10, "today": 1, "allowed": 5, "studio": 1}

        def krea_reserve(self, who, cap, key=None, ledger=None, report_only=False, made=False):
            asked.append({"who": who, "cap": cap, "key": key, "ledger": ledger, "report_only": report_only,
                          **({"made": True} if made else {})})
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
    dash = Dash()
    monkeypatch.setattr(cli, "_dashboard", lambda: dash)
    ops._DEGRADED.clear()
    yield NS(asked=asked, built=built, dash=dash, work=work, cfg=cfg, root=tmp_path)
    ops._DEGRADED.clear()


def run(w, d, cap=10):
    cli._routine_krea_request(NS(dir=d, upload_url="", feedback=""), {**w.cfg, "krea": {**w.cfg["krea"], "monthly_max_images": cap}}, R)


def test_studio_and_hourly_reserve_from_one_allowance(wired, capsys):
    run(wired, "studio/job1/p1")
    run(wired, "run1/huddle-1")
    studio, hourly = wired.asked
    assert (studio["who"], studio["ledger"]) == ("studio", None)
    assert hourly["who"] == "hourly"
    assert hourly["ledger"] == {"hourly_used": 1, "hourly_today": 1, "month": time.strftime("%Y-%m", time.gmtime()),
                                "day": time.strftime("%Y-%m-%d", time.gmtime())}   # its ledger, and when it counted it
    assert "Krea images this month: 2/10" in capsys.readouterr().out
    wired.dash.answer = KreaRefused("KREA CAP: 10/10 images this month")
    with pytest.raises(SystemExit, match="KREA CAP: 10/10.*shared by the hourly routine and Studio.*fallback"):
        run(wired, "studio/job1/p1")


def test_the_request_is_built_before_anything_is_reserved(wired, monkeypatch):
    def broken(d, cfg, url, fb):
        raise requests.ConnectionError("photo upload failed")
    monkeypatch.setattr(R, "krea_request", broken)
    with pytest.raises(requests.ConnectionError):
        run(wired, "run1/huddle-1")
    assert wired.asked == []                                   # nothing taken from the allowance


def test_the_same_attempt_asks_with_the_same_key(wired):
    run(wired, "studio/job1/p1")
    run(wired, "studio/job1/p1")                               # its output was lost: the same image
    d = wired.work / "studio/job1/p1"
    (d / "attempts.json").write_text(json.dumps([{"engine": "krea-art", "url": "u1"}]))
    run(wired, "studio/job1/p1")                               # after an image: a new one
    a, b, c = (x["key"] for x in wired.asked)
    assert a == b and a.endswith("-0") and c != a and c.endswith("-1")


def test_studio_is_known_by_where_its_folder_is(wired, monkeypatch):
    monkeypatch.chdir(wired.work / "studio/job1")
    run(wired, "p1")                                           # a path relative to the job's own folder
    run(wired, str(wired.work / "run1/studio-fans"))           # "studio" in an hourly post's name
    assert [x["who"] for x in wired.asked] == ["studio", "hourly"]


def test_a_database_this_session_did_not_restore_sends_no_ledger(wired):
    (wired.root / ".restored").unlink()                        # a local checkout: its count is not the routine's
    run(wired, "run1/huddle-1")
    assert wired.asked[0]["ledger"] is None


def test_without_the_shared_count_studio_uses_the_card_and_hourly_its_ledger(wired, monkeypatch, capsys, store):
    run(wired, "run1/huddle-1")                                # the dashboard said: Studio made 1 this month
    wired.dash.answer = requests.ConnectionError("down")
    with pytest.raises(SystemExit, match="KREA UNCHECKED.*fallback"):
        run(wired, "studio/job1/p1")
    assert ops._DEGRADED == ["shared Krea allowance unavailable (ConnectionError)"]   # amber, on the admin's tab
    capsys.readouterr()
    run(wired, "run1/huddle-1", cap=1000)                      # 1 of its own + Studio's 1 of 1000: it may go on
    out = capsys.readouterr().out
    assert "shared Krea allowance unavailable (ConnectionError): counting this routine's own images and Studio's last known 1" in out
    assert "Krea images this month: 2/1000" in out
    with pytest.raises(SystemExit, match="KREA CAP: 2/2"):     # Studio's images still count against the cap
        run(wired, "run1/huddle-1", cap=2)
    monkeypatch.setattr(cli, "_dashboard", lambda: None)
    with pytest.raises(SystemExit, match="KREA UNCHECKED"):
        run(wired, "studio/job1/p1")


def test_every_hourly_run_reports_its_ledger(wired, capsys, store):
    wired.dash.answer = {"used": 7, "cap": 1260, "today": 2, "allowed": 41, "studio": 3}
    cli._krea_report(wired.dash, wired.cfg)
    assert wired.asked[0]["report_only"] and wired.asked[0]["ledger"]["hourly_used"] == 1
    cap = wired.cfg["krea"]["monthly_max_images"]
    assert f"Krea images this month (hourly routine and Studio): 7/{cap}, today 2/41 (3 by Studio)" in capsys.readouterr().out
    assert store.get_kv(f"krea_studio:{time.strftime('%Y-%m', time.gmtime())}") == 3   # for when it does not answer
    wired.dash.answer = requests.HTTPError("500")
    cli._krea_report(wired.dash, wired.cfg)                    # never stops prepare
    assert "Studio uses template cards until it answers" in capsys.readouterr().out
    assert ops._DEGRADED == ["shared Krea allowance unavailable (HTTPError)"]


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


def client(answers):
    sent = []
    dash = Dashboard("https://dash.test", "s")

    def post(url, json=None, timeout=None):
        sent.append(json)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    dash.s.post = post
    return dash, sent


def test_the_dashboard_client_says_no_as_a_refusal_and_asks_again_only_when_it_is_safe(monkeypatch):
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)
    dash, sent = client([FakeResponse(409, {"error": "KREA PACE: 41/41 images today"})])
    with pytest.raises(KreaRefused, match="KREA PACE: 41/41"):
        dash.krea_reserve("studio", 1260, "k-0")
    assert sent == [{"who": "studio", "cap": 1260, "key": "k-0", "confirms": True}]   # Studio sends no ledger
    dash, sent = client([FakeResponse(409, ValueError("not json"))])
    with pytest.raises(KreaRefused, match="KREA CAP"):
        dash.krea_reserve("studio", 1260, "k-0")
    ledger = {"hourly_used": 3, "hourly_today": 1, "month": "2026-10", "day": "2026-10-23"}
    dash, sent = client([requests.ConnectionError("reset"), FakeResponse(200, {"used": 4})])
    assert dash.krea_reserve("hourly", 1260, "k-1", ledger) == {"used": 4}  # a key: the same ask, sent again
    assert sent[0] == sent[1] == {"who": "hourly", "cap": 1260, "key": "k-1", **ledger, "confirms": True}
    assert waits == [3]                                        # a busy dashboard is given time
    dash, sent = client([requests.ReadTimeout("slow"), FakeResponse(200, {"used": 4})])
    assert dash.krea_reserve("studio", 1260, "k-0") == {"used": 4}   # the answer lost: asked again
    assert len(sent) == 2 and sent[0] == sent[1]
    dash, sent = client([FakeResponse(200, {"used": 4})])
    dash.krea_reserve("hourly", 1260, "k-1", made=True)
    assert sent == [{"who": "hourly", "cap": 1260, "key": "k-1", "made": True}]
    dash, sent = client([FakeResponse(503, {}), FakeResponse(200, {"used": 4})])
    assert dash.krea_reserve("hourly", 1260, ledger=ledger, report_only=True) == {"used": 4}
    assert sent[0]["report_only"] is True and "confirms" not in sent[0]
    dash, sent = client([FakeResponse(500, {}), FakeResponse(200, {})])
    with pytest.raises(requests.HTTPError):                    # no key: an ask sent twice could take two
        dash.krea_reserve("studio", 1260)
    dash, sent = client([FakeResponse(400, {}), FakeResponse(200, {})])
    with pytest.raises(requests.HTTPError):                    # a wrong ask is not asked again
        dash.krea_reserve("studio", 1260, "k-0")
    assert len(sent) == 1
    assert waits == [3, 3, 3]                                  # neither the keyless ask nor the wrong one waited


def test_an_unchecked_allowance_is_a_refusal_not_an_error():
    ok, rep = ops.outcome("routine krea-request", SystemExit("KREA UNCHECKED: the shared Krea allowance could not be read (HTTPError)."))
    assert ok and rep["refused"].startswith("KREA UNCHECKED")


def test_without_the_shared_count_studio_s_images_today_count_against_today(wired, capsys):
    t = time.gmtime()
    dl = calendar.monthrange(t.tm_year, t.tm_mon)[1] - t.tm_mday + 1
    # the dashboard said: Studio made 10 this month, 6 of them today; the routine made 1 (today)
    wired.dash.answer = {"used": 11, "cap": 1000, "today": 7, "allowed": 40, "studio": 10, "studio_today": 6}
    run(wired, "run1/huddle-1", cap=1000)
    wired.dash.answer = requests.ConnectionError("down")
    capsys.readouterr()
    run(wired, "run1/huddle-1", cap=1000)
    assert f"today 7/{(1000 - 4) // dl}" in capsys.readouterr().out   # Studio's 4 before today, its 6 today
    if dl > 1:     # (on the month's last day the share is the cap, and KREA CAP says so first)
        cap = 7 + 4 + (dl - 1) * 7                             # a share of 7 a day: 1 + 6 made today
        with pytest.raises(SystemExit, match="KREA PACE: 7/7"):
            run(wired, "run1/huddle-1", cap=cap)


def test_without_the_shared_count_the_daily_share_leaves_studio_its_images(wired):
    t = time.gmtime()
    dl = calendar.monthrange(t.tm_year, t.tm_mon)[1] - t.tm_mday + 1
    if dl == 1:
        pytest.skip("last day of the month: the share is the cap")
    S, C = dl + 1, 3 * dl
    wired.dash.answer = {"used": 2, "cap": C, "today": 1, "allowed": 5, "studio": S, "studio_today": 0}
    run(wired, "run1/huddle-1", cap=C)                         # the dashboard says Studio made S before today
    wired.dash.answer = requests.ConnectionError("down")
    with pytest.raises(SystemExit, match="KREA PACE: 1/1"):    # (C - S) // dl = 1, and 1 image today
        run(wired, "run1/huddle-1", cap=C)


def test_last_months_studio_count_does_not_count_this_month(wired, monkeypatch, capsys):
    real, then = time.gmtime, time.time() - 40 * 86400
    monkeypatch.setattr(time, "gmtime", lambda t=None: real(then if t is None else t))
    wired.dash.answer = {"used": 501, "cap": 1000, "today": 1, "allowed": 40, "studio": 500, "studio_today": 0}
    run(wired, "run1/huddle-1", cap=1000)                      # last month: Studio made 500
    monkeypatch.setattr(time, "gmtime", real)
    wired.dash.answer = requests.ConnectionError("down")
    capsys.readouterr()
    run(wired, "run1/huddle-1", cap=1000)                      # this month, the dashboard down
    assert "Krea images this month: 1/1000" in capsys.readouterr().out


def result(w, d, url, made=lambda d, url: None):
    """krea-result with the image step replaced: `made` stands for it (the attempt kept, or an error)."""
    def krea_result(dd, cfg, store, u, fb):
        attempts = json.loads((dd / "attempts.json").read_text()) if (dd / "attempts.json").exists() else []
        if not any(a["url"] == u for a in attempts):
            (dd / "attempts.json").write_text(json.dumps(attempts + [{"engine": "krea-art", "url": u}]))
        made(dd, u)
        return {"engine": "krea-art"}
    import bore.routine
    old, bore.routine.krea_result = bore.routine.krea_result, krea_result
    try:
        cli._routine_krea_result(NS(dir=d, url=url, feedback=""), w.cfg, R)
    finally:
        bore.routine.krea_result = old


def test_a_made_image_is_confirmed_with_its_reservation_s_key(wired, capsys):
    run(wired, "run1/huddle-1")
    result(wired, "run1/huddle-1", "https://img/1")
    asked, confirmed = wired.asked
    assert confirmed == {**asked, "cap": wired.cfg["krea"]["monthly_max_images"], "ledger": None, "made": True}   # the same key, said made
    result(wired, "run1/huddle-1", "https://img/1")            # the same image again (a new layout): not a new one
    assert len(wired.asked) == 2

    def rejected(d, url):
        raise R.ArtRejected("artwork contains text: X")
    run(wired, "studio/job1/p1")
    with pytest.raises(SystemExit, match="ART REJECTED"):      # a rejected artwork was made too
        result(wired, "studio/job1/p1", "https://img/2", rejected)
    assert wired.asked[-1]["who"] == "studio" and wired.asked[-1]["made"] and wired.asked[-1]["key"] == wired.asked[-2]["key"]
    run(wired, "studio/job1/p1")                               # the next attempt: a new key
    assert wired.asked[-1]["key"] != wired.asked[-2]["key"]


def test_a_confirmation_that_fails_never_stops_the_card(wired, capsys):
    run(wired, "run1/huddle-1")
    wired.dash.answer = requests.HTTPError("500")
    capsys.readouterr()
    result(wired, "run1/huddle-1", "https://img/1")
    out = capsys.readouterr().out
    assert "the shared count was not told this image was made. Carry on with the card." in out
    assert out.strip().endswith("card.jpg")
    assert ops._DEGRADED == ["shared Krea allowance unavailable (HTTPError)"]   # the admin's Health tab
