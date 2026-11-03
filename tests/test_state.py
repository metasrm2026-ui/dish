"""The routine's database between runs: a pull that did not restore it stops the run, and a push can
never put an empty or shrunken database over the dashboard's copy (Oct 21: the state endpoint served
a security-check page as the saved state)."""
import gzip
import json
import time

import pytest
import requests

from bore import cli
from bore import dashboard as D
from bore.store import Store

CHECKPOINT = (b"<!DOCTYPE html><html lang=en><head><title>Vercel Security Checkpoint</title></head>"
              b"<body>We're verifying your browser</body></html>")


class Resp:
    def __init__(self, status, content=b""):
        self.status_code, self.content, self.text = status, content, content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def database(path, rows=30):
    st = Store(path)
    for i in range(rows):
        st.log_run("routine-prepare", time.time(), "ok")
        st.record_sent("demo-sports", f"k{i}", ["eagles", "hurts"], 10, 2, 1.0, f"story {i}")
        st.record_spend("apify/x", 0.01)
    st.commit()
    st.db.close()
    return path


def saved_state(tmp_path, rows=30) -> bytes:
    return gzip.compress(database(tmp_path / "server" / "bore.db", rows).read_bytes())


def answering(monkeypatch, *answers):
    """A Dashboard whose GETs return these answers in turn (the last one repeats)."""
    d = D.Dashboard("https://dash.example", "s")
    calls = []

    def get(url, **k):
        calls.append(url)
        a = answers[min(len(calls), len(answers)) - 1]
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(d.s, "get", get)
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    return d, calls


def test_a_security_check_page_restores_nothing_and_stops(monkeypatch, tmp_path):
    db = tmp_path / "data" / "bore.db"
    db.parent.mkdir()
    db.write_bytes(b"what this machine had")
    d, calls = answering(monkeypatch, Resp(200, CHECKPOINT))
    with pytest.raises(D.StateError, match="tried 3 times.*security-check page"):
        d.pull_state(db)
    assert len(calls) == 3                                          # tried again before giving up
    assert db.read_bytes() == b"what this machine had" and D.restored(db) is None
    assert not list(db.parent.glob("*.pulled"))


def test_a_pull_that_works_on_a_later_try_restores_and_records_what_it_brought(monkeypatch, tmp_path):
    db = tmp_path / "data" / "bore.db"
    d, calls = answering(monkeypatch, requests.ConnectionError("SSLEOFError"), Resp(503, b'{"error":"state unreadable"}'),
                         Resp(200, saved_state(tmp_path)))
    out = d.pull_state(db)
    assert len(calls) == 3 and "30 runs" in out
    assert D.restored(db)["counts"] == {"runs": 30, "sent_index": 30, "spend": 30}
    assert len(Store(db).sent_for_page("demo-sports", 0)) == 30


@pytest.mark.parametrize("body, why", [
    (gzip.compress(b"<html>not a database</html>"), "not a SQLite database"),
    (gzip.compress(b"SQLite format 3\x00" + b"\x00" * 50), "damaged"),
    (b"\x1f\x8b\x08\x00garbage", "broken gzip"),
])
def test_only_a_sound_database_is_restored(monkeypatch, tmp_path, body, why):
    db = tmp_path / "bore.db"
    d, _ = answering(monkeypatch, Resp(200, body))
    with pytest.raises(D.StateError, match=why):
        d.pull_state(db, waits=())
    assert not db.exists() and D.restored(db) is None and not list(tmp_path.glob("*.pulled"))


def test_a_refused_or_missing_state_is_not_tried_again(monkeypatch, tmp_path):
    d, calls = answering(monkeypatch, Resp(403, b'{"error":"forbidden"}'))
    with pytest.raises(D.StateError, match="refused the pull: HTTP 403"):
        d.pull_state(tmp_path / "bore.db")
    assert len(calls) == 1
    d, calls = answering(monkeypatch, Resp(404, b'{"error":"no state yet"}'))
    with pytest.raises(D.StateError, match="no saved state.*--fresh"):        # never "carry on with an empty one"
        d.pull_state(tmp_path / "bore.db")


def test_a_failed_pull_takes_back_an_earlier_restore(monkeypatch, tmp_path):
    db = tmp_path / "bore.db"
    d, _ = answering(monkeypatch, Resp(200, saved_state(tmp_path)), Resp(200, CHECKPOINT))
    d.pull_state(db, waits=())
    assert D.restored(db)
    with pytest.raises(D.StateError):
        d.pull_state(db, waits=())
    assert D.restored(db) is None


def pushing(monkeypatch, tmp_path):
    d, _ = answering(monkeypatch, Resp(200, saved_state(tmp_path)))
    put = []
    monkeypatch.setattr(d.s, "put", lambda url, data=None, **k: put.append(data) or Resp(200))
    return d, put


def test_push_needs_a_restored_database_unless_fresh(monkeypatch, tmp_path):
    d, put = pushing(monkeypatch, tmp_path)
    db = database(tmp_path / "data" / "bore.db", rows=3)            # what an empty start makes
    with pytest.raises(D.StateError, match="did not restore the database"):
        d.push_state(db)
    assert put == []
    assert d.push_state(db, fresh=True).startswith("saved state")  # the owner's new database
    assert gzip.decompress(put[0]).startswith(b"SQLite format 3")


def test_push_refuses_a_database_that_lost_most_of_what_was_pulled(monkeypatch, tmp_path):
    d, put = pushing(monkeypatch, tmp_path)
    db = tmp_path / "data" / "bore.db"
    d.pull_state(db, waits=())
    st = Store(db)
    st.log_run("routine-prepare", time.time(), "ok")                # an ordinary run: saved
    st.db.close()
    assert d.push_state(db).startswith("saved state") and len(put) == 1
    st = Store(db)
    st.db.execute("DELETE FROM sent_index WHERE rowid % 3 != 0")    # 30 -> 10: the repeat memory is gone
    st.commit()
    st.db.close()
    with pytest.raises(D.StateError, match=r"lost more than half.*sent_index 30 -> 10"):
        d.push_state(db)
    db.unlink()
    Store(db).db.close()                                            # a database emptied for good
    with pytest.raises(D.StateError, match=r"lost more than half.*runs 30 -> 0, sent_index 30 -> 0, spend 30 -> 0"):
        d.push_state(db)
    assert len(put) == 1
    db.write_bytes(b"not a database")
    with pytest.raises(D.StateError, match="no local database|damaged"):
        d.push_state(db, fresh=True)


@pytest.mark.parametrize("paused_days", [5, 8, 30])
def test_the_first_run_after_a_pause_prunes_old_rows_and_still_saves(monkeypatch, tmp_path, paused_days):
    """The saved state as hourly runs left it before the routine was paused: each table full to its
    window then. The first run after the pause prunes most of it (all of sent_index after 8 days):
    rows that age out are not rows lost, or the state would never be saved again."""
    stop = time.time() - paused_days * 86400
    st = Store(tmp_path / "server" / "bore.db")
    for h in range(14 * 24):                                        # an hourly run, a story sent an hour
        st.db.execute("INSERT INTO runs (kind, started, finished, status) VALUES ('routine-prepare', ?, ?, 'ok')",
                      (stop - h * 3600, stop - h * 3600))
        if h < 8 * 24:
            st.record_sent("demo-sports", f"k{h}", ["eagles"], 1, 1, 1.0, f"story {h}", now=stop - h * 3600)
    for h in range(45 * 6):
        st.db.execute("INSERT INTO spend (at, actor, usd) VALUES (?, 'apify/x', 0.01)", (stop - h * 4 * 3600,))
    st.commit()
    st.db.close()
    d, _ = answering(monkeypatch, Resp(200, gzip.compress((tmp_path / "server" / "bore.db").read_bytes())))
    put = []
    monkeypatch.setattr(d.s, "put", lambda url, data=None, **k: put.append(data) or Resp(200))
    db = tmp_path / "data" / "bore.db"
    d.pull_state(db, waits=())
    st = Store(db)
    st.log_run("routine-prepare", time.time(), "ok")
    st.prune()                                                      # what every `routine prepare` does
    st.db.close()
    assert d.push_state(db).startswith("saved state") and len(put) == 1


@pytest.fixture
def routine(monkeypatch, tmp_path, cfg):
    """`python -m bore.cli ...` against a scratch database, with the dashboard `dash`."""
    cfg["db_path"] = str(tmp_path / "data" / "bore.db")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    def run(argv, dash):
        monkeypatch.setattr(cli, "_dashboard", lambda: dash)
        cli.main(argv)
    return run


def test_the_state_command_exits_non_zero_and_says_stop(monkeypatch, tmp_path, routine):
    d, _ = answering(monkeypatch, Resp(200, CHECKPOINT))
    with pytest.raises(SystemExit, match="STATE NOT RESTORED.*Do not run prepare and never push"):
        routine(["routine", "state", "--action", "pull"], d)
    database(tmp_path / "data" / "bore.db", rows=3)
    with pytest.raises(SystemExit, match="STATE NOT SAVED.*did not restore.*Do not retry with --fresh.*owner's step"):
        routine(["routine", "state", "--action", "push"], d)


class Stop(BaseException):      # past the checks: the run's first real step (no `except Exception` catches it)
    pass


class Dash:
    def created_today(self, cfg):
        raise Stop


def test_prepare_and_the_feed_refuse_an_unrestored_database(monkeypatch, tmp_path, routine):
    from bore import deps, studio
    monkeypatch.setattr(deps, "problems", lambda names=None: {})
    monkeypatch.setattr(studio, "prepare_feed", lambda *a, **k: (_ for _ in ()).throw(Stop()))
    for argv in (["routine", "prepare"], ["studio", "feed", "--run", str(tmp_path / "run")]):
        with pytest.raises(SystemExit, match="database was not restored.*state --action pull.*fails twice"):
            routine(argv, Dash())
        with pytest.raises(Stop):                                   # --fresh: the owner's explicit start
            routine(argv + ["--fresh"], Dash())
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / D.RESTORED).write_text(json.dumps({"counts": {}}))
    with pytest.raises(Stop):                                       # restored: carries on
        routine(["routine", "prepare"], Dash())


def test_prepare_feed_and_image_search_stop_when_the_image_packages_fail(monkeypatch, tmp_path, routine):
    from bore import deps, studio
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / D.RESTORED).write_text(json.dumps({"counts": {}}))
    asked = []
    monkeypatch.setattr(deps, "problems", lambda names=None: asked.append(names) or {"Pillow": "ImportError: _imaging"})
    monkeypatch.setattr(studio, "image_search", lambda *a, **k: pytest.fail("searched with the photo checks off"))
    for argv in (["routine", "prepare"], ["studio", "feed", "--run", str(tmp_path / "run")], ["studio", "images", str(tmp_path), "--query", "Jalen Hurts"]):
        with pytest.raises(SystemExit, match="not working on this machine.*Pillow.*ensure-deps.*report it and stop"):
            routine(argv, Dash())
    assert all("opencv-python" in names and "image files" in names for names in asked)


def test_prepare_stops_when_the_card_browser_does_not_start(monkeypatch, tmp_path, routine):
    """Every hourly post is drawn in Chromium: where it does not start, prepare refuses before a post is
    written or Krea is asked for an image (it checked only the image packages, so a session that missed
    ROUTINE.md's stop line spent Krea images before every card failed). The topic feed draws no cards."""
    from bore import deps, studio
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / D.RESTORED).write_text(json.dumps({"counts": {}}))
    no_browser = "Error: BrowserType.launch: Executable doesn't exist at /opt/pw-browsers/chromium/headless_shell"
    monkeypatch.setattr(deps, "problems", lambda names=None: {"chromium": no_browser} if "chromium" in names else {})
    with pytest.raises(SystemExit, match="routine prepare: not working on this machine .chromium: Error: BrowserType"):
        routine(["routine", "prepare"], Dash())
    monkeypatch.setattr(studio, "prepare_feed", lambda *a, **k: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        routine(["studio", "feed", "--run", str(tmp_path / "run")], Dash())
