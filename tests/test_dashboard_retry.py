"""Dashboard calls survive the network blips the run log shows (Oct 20: an SSLEOFError on the first
call ended a topic run; "transient SSL error on first state pull"), against a local HTTPS server that
drops connections, and never send twice what the dashboard may already have acted on. Every case runs
on a direct connection and through a CONNECT proxy, as the routines connect (HTTPS_PROXY): there the
TLS handshake happens in a different part of urllib3."""
import gzip
import json
import os
import select
import shutil
import socket
import ssl
import subprocess
import threading
import time

import pytest
import requests

from bore import dashboard as D
from bore import studio
from bore.store import Store

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl for a test certificate")


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(d / "key.pem"), "-out", str(d / "cert.pem")],
                   check=True, capture_output=True)
    return d / "cert.pem", d / "key.pem"


class Flaky:
    """A local HTTPS server doing, connection by connection, what `plan` says, then answering 200:
    "drop" closes before the TLS handshake (what the proxy did), "eof" reads the request and closes
    without an answer, "badtls" answers with a broken TLS record (an SSL error after the request),
    a number answers with that status, (status, {header: value}) with these headers too. `seen` lists
    the requests it read: what the dashboard would have acted on."""

    def __init__(self, cert, key):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.url = f"https://127.0.0.1:{self.sock.getsockname()[1]}"
        self.plan, self.seen, self.bodies = [], [], {}
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c, self.plan.pop(0) if self.plan else 200), daemon=True).start()

    def _one(self, c, what):
        if what == "drop":
            c.close()
            return
        try:
            s = self.ctx.wrap_socket(c, server_side=True)
            data = b""
            while b"\r\n\r\n" not in data:
                data += s.recv(65536)
            head, body = data.split(b"\r\n\r\n", 1)
            n = int(next((ln.split(b":")[1] for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")), 0))
            while len(body) < n:
                body += s.recv(65536)
            method, path = head.split(b" ")[:2]
            path = path.decode().split("?")[0]
            self.seen.append((method.decode(), path))
            if what in ("eof", "badtls"):
                if what == "badtls":
                    os.write(s.fileno(), b"\x17\x03\x03\x00\x05hello")   # application data that fails its check
                s.close()
                return
            out = self.bodies.get(path, json.dumps({"ok": True, "jobs": [], "links": [], "feed": None}).encode())
            status, extra = what if isinstance(what, tuple) else (what, {})
            head = "".join(f"{k}: {v}\r\n" for k, v in extra.items())
            s.sendall(f"HTTP/1.1 {status} X\r\n{head}Content-Length: {len(out)}\r\nConnection: close\r\n\r\n".encode() + out)
            s.close()
        except (OSError, ssl.SSLError):
            pass


class Tunnel:
    """A CONNECT proxy like the containers' agent proxy: it opens a tunnel to `port` and passes bytes
    both ways, so the server's "drop" reaches the client in the middle of its TLS handshake."""

    def __init__(self, port):
        self.port = port
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        up = None
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = c.recv(4096)
                if not chunk:
                    return
                data += chunk
            up = socket.create_connection(("127.0.0.1", self.port))
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            while True:
                ready, _, _ = select.select([c, up], [], [], 10)
                if not ready:
                    return
                for s in ready:
                    b = s.recv(65536)
                    if not b:
                        return
                    (up if s is c else c).sendall(b)
        except OSError:
            pass
        finally:
            for s in (c, up):
                if s is not None:
                    s.close()


@pytest.fixture
def server(cert, monkeypatch):
    monkeypatch.setattr(D, "RETRY_BACKOFF", 0)
    srv = Flaky(*cert)
    yield srv
    srv.sock.close()


def connect(server, cert, via: str):
    d = D.Dashboard(server.url, "secret")
    d.s.trust_env, d.s.verify = False, str(cert[0])       # not this machine's proxy; trust the test certificate
    if via == "proxy":
        d.s.proxies = {"https": Tunnel(int(server.url.rsplit(":", 1)[1])).url}
    return d


@pytest.fixture(params=["direct", "proxy"])
def dash(server, cert, request):
    return connect(server, cert, request.param)


def test_dropped_connections_are_tried_again_and_each_request_acts_once(server, dash, tmp_path, monkeypatch):
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    st = Store(tmp_path / "server.db")
    st.log_run("routine-prepare", time.time(), "ok")
    st.db.close()
    server.bodies["/api/state"] = gzip.compress((tmp_path / "server.db").read_bytes())
    calls = {
        "sync": lambda: studio._get(dash, "config"),
        "pull": lambda: dash.pull_state(tmp_path / "data" / "bore.db"),
        "jobs": lambda: studio._get(dash, "jobs"),
        "claim": lambda: studio._post(dash, "jobs/j1", {"status": "running"}),     # a POST: nothing was sent yet
        "links": lambda: dash.link_requests(),
    }
    for name, call in calls.items():
        server.plan[:], server.seen[:] = ["drop", "drop"], []
        call()
        assert len(server.seen) == 1, name
    assert D.restored(tmp_path / "data" / "bore.db")["counts"]["runs"] == 1


def test_a_post_the_dashboard_may_have_acted_on_is_never_sent_again(server, dash):
    for call in (lambda: studio._post(dash, "jobs/j1", {"status": "running"}),       # a second claim: 409, "SKIP"
                 lambda: dash.create_draft({"page": "demo-sports"})):                     # a second draft
        for cut in ("eof", "badtls"):
            server.plan[:], server.seen[:] = [cut], []
            with pytest.raises(requests.RequestException):
                call()
            assert len(server.seen) == 1, cut
    server.plan[:], server.seen[:] = [503], []
    with pytest.raises(RuntimeError, match="HTTP 503"):
        studio._post(dash, "jobs/j1", {"status": "done"})
    assert len(server.seen) == 1


def test_reads_and_whole_record_writes_are_tried_again_after_the_request_was_cut(server, dash):
    for cut in ("eof", "badtls"):
        server.plan[:], server.seen[:] = [cut], []
        assert studio._get(dash, "feed", page="demo-sports") == {"ok": True, "jobs": [], "links": [], "feed": None}
        assert len(server.seen) == 2, cut
    server.plan[:], server.seen[:] = ["eof"], []
    studio._post(dash, "feed", {"page": "demo-sports", "stories": []})        # replaces the record: same result twice
    assert server.seen == [("POST", "/api/routine/feed")] * 2


def test_server_errors_are_tried_again_but_a_security_check_shows_at_once(server, dash):
    server.plan[:], server.seen[:] = [503, 502], []
    assert dash.link_requests() == []
    assert len(server.seen) == 3
    for status in (403, 500):        # a security check; an error the dashboard already retried its reads for
        server.plan[:], server.seen[:] = [status], []
        with pytest.raises(requests.HTTPError, match=str(status)):
            dash.link_requests()
        assert len(server.seen) == 1


def test_only_the_exact_record_replacing_paths_resend_a_post(server, dash):
    for path in ("feed-publish", "lessons/reset", "feedback"):     # the same prefix as an idempotent POST
        server.plan[:], server.seen[:] = ["eof"], []
        with pytest.raises(requests.RequestException):
            studio._post(dash, path, {"page": "demo-sports"})
        assert len(server.seen) == 1, path
    server.plan[:], server.seen[:] = ["eof"], []
    dash.save_lessons("demo-sports", {})
    assert server.seen == [("POST", "/api/routine/lessons")] * 2


def test_heavy_reads_are_tried_again_once_and_not_after_a_cut_reply(server, dash):
    """GET /api/drafts reads hundreds of records: on Oct 21 bursts of them set off the storage check."""
    server.plan[:], server.seen[:] = [503, 503], []
    with pytest.raises(requests.HTTPError, match="503"):
        dash.recent_drafts(24)
    assert len(server.seen) == 2
    server.plan[:], server.seen[:] = ["eof"], []
    with pytest.raises(requests.RequestException):
        dash.editor_feedback()
    assert len(server.seen) == 1
    server.plan[:], server.seen[:] = ["drop", "drop"], []         # nothing was sent: tried again as always
    assert dash.recent_drafts(24) == []
    assert len(server.seen) == 1


def test_a_busy_dashboard_is_given_time_before_the_first_retry(server, cert, monkeypatch):
    monkeypatch.setattr(D, "RETRY_BACKOFF", 1.0)
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)
    dash = connect(server, cert, "direct")
    server.plan[:], server.seen[:] = [503, 502], []
    assert dash.link_requests() == []
    assert len(waits) == 2 and 3 <= waits[0] < 4 and 6 <= waits[1] < 7      # not urllib3's instant first retry
    waits[:], server.plan[:] = [], ["drop"]
    dash.link_requests()
    assert waits == []                                       # a cut connection is tried again at once
    server.plan[:] = [(429, {"Retry-After": "7"}), (503, {"Retry-After": "600"})]
    assert dash.link_requests() == []
    assert waits == [7, D.RETRY_AFTER_MAX]                   # the dashboard's wait, up to a cap


def test_a_feed_that_cannot_be_read_stops_the_run_instead_of_publishing_it_stripped(monkeypatch, tmp_path):
    from types import SimpleNamespace as NS

    import bore.select
    from bore.config import Page
    monkeypatch.setattr(studio, "load_pages", lambda: [Page.from_dict({"slug": "demo", "name": "Demo", "status": "live"})])
    monkeypatch.setattr(studio, "studio_meta", lambda: {"demo": {}})
    monkeypatch.setattr(bore.select, "select_page", lambda *a, **k: NS(trends=[], watchlist=[]))

    def cut(dash, path, **k):
        raise requests.ConnectionError("SSLEOFError(8, 'UNEXPECTED_EOF_WHILE_READING')")
    monkeypatch.setattr(studio, "_get", cut)
    cfg = {"clustering": {"window_hours": 6}}
    with pytest.raises(SystemExit, match="cannot read demo's current feed.*written stories are kept.*fails twice"):
        studio.prepare_feed(tmp_path, cfg, NS(page_items=lambda *a: []), dash=object())
    assert not (tmp_path / "feed").exists()               # nothing for feed-publish to send
