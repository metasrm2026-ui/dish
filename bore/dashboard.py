"""Client for the approval dashboard (Vercel app in dashboard/)."""
from __future__ import annotations

import gzip
import json
import os
import random
import sqlite3
import time
import zlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import load_env, load_pages
from .store import check_file

RETRY_BACKOFF = 1.0      # seconds: after a cut connection retries wait 0, 2, 4, 8 s; after a busy reply 3-4, then 6-7 s
RETRY_AFTER_MAX = 30     # seconds: the longest Retry-After waited for (a run has its own time budget)
# POSTs that replace a whole record: sent twice, the result is the same, so they are retried like a GET.
# Exact paths only: a route added later under one of them (say /api/routine/feed-publish) must not inherit it.
IDEMPOTENT_POSTS = ("/api/routine/feed", "/api/routine/lessons")
# the heaviest reads (hundreds of stored records each). On Oct 21 bursts of them set off the storage's
# security check, which no retry gets through and every retry prolongs: one retry after a busy reply,
# none after a timeout or a cut reply (only a connection that never opened is tried again).
HEAVY_READS = ("/api/drafts", "/api/routine/reviews")
RESTORED = ".restored"   # next to the database: what this session's pull restored (only a pull that worked writes it)
MIN_ROWS = 10            # a table this small says little: a few rows less is not counted as lost
PUSH_WITHIN = 6 * 3600   # a run pushes within hours of its pull: rows pruned by then are not counted at the pull


class StateError(RuntimeError):
    """The database was not restored, or must not be saved over the dashboard's copy."""


def _while_connecting(err: BaseException) -> bool:
    """The error came from opening the connection (TCP, the proxy tunnel, the TLS handshake), before
    any of the request was sent. urllib3 only says so for a refused or timed-out connect, not for a
    handshake the proxy cut (the run log's SSLEOFError). Its traceback passes through _validate_conn
    (a direct connection) or _prepare_proxy (through HTTPS_PROXY, as the routines connect: the tunnel
    and the handshake inside it are made there). (If urllib3 ever renames these, this says no and
    such errors are retried less, never more.)"""
    seen, todo = set(), [err]
    while todo:
        e = todo.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        tb = e.__traceback__
        while tb is not None:
            if tb.tb_frame.f_code.co_name in ("_validate_conn", "_prepare_proxy"):
                return True
            tb = tb.tb_next
        todo += [x for x in (e.__context__, e.__cause__, getattr(e, "original_error", None), *e.args)
                 if isinstance(x, BaseException)]
    return False


class _Retry(Retry):
    """urllib3's Retry, deciding by whether the request left: an error while connecting is retried
    for every method, POST included (nothing was sent); after the request was sent, only the methods
    in allowed_methods are tried again (urllib3 itself would resend a POST after an SSL error, and the
    dashboard may already have created the draft or taken the claim)."""

    def _is_connection_error(self, err: Exception) -> bool:
        return super()._is_connection_error(err) or _while_connecting(err)

    def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
        if (error is not None and method and not self._is_method_retryable(method)
                and not self._is_connection_error(error)):
            raise error
        return super().increment(method, url, response, error, _pool, _stacktrace)

    def get_backoff_time(self) -> float:
        """urllib3 retries at once the first time: right after a cut connection, but not after the
        dashboard answered that it is busy (429/5xx), which an instant retry only adds to. Jittered,
        so sessions that failed together do not come back together."""
        if not (self.history and self.history[-1].status):
            return super().get_backoff_time()
        replies = sum(1 for h in self.history if h.status)
        return self.backoff_factor * (3 * 2 ** (replies - 1) + random.random())

    def get_retry_after(self, response) -> float | None:
        after = super().get_retry_after(response)
        return None if after is None else min(after, RETRY_AFTER_MAX)


def _retries(post: bool = False, heavy: bool = False) -> Retry:
    """Network blips through the container proxy are routine (an SSL EOF on the first call ended a
    topic run): retried here, not left to the session. Busy replies (429, 502-504) are tried again
    after a wait (Retry-After when given), at most twice, heavy reads once. Never on 403 (a security
    check shows at once) or 500 (the dashboard already retried the storage reads behind it)."""
    # (through the proxy a reply cut after sending counts as "other", not "read": urllib3 blames the proxy)
    return _Retry(total=6, connect=4, read=0 if heavy else 1, other=0 if heavy else 3, status=1 if heavy else 2,
                  backoff_factor=RETRY_BACKOFF, status_forcelist=(429, 502, 503, 504), raise_on_status=False,
                  allowed_methods=frozenset({"GET", "HEAD", "PUT"} | ({"POST"} if post else set())))


class _Session(requests.Session):
    """requests picks an adapter by URL prefix, so a route added later under /api/routine/feed would
    silently get "a POST may be sent twice". Adapters in `exact` serve one path only."""

    def __init__(self):
        super().__init__()
        self.exact: dict[str, HTTPAdapter] = {}

    def get_adapter(self, url):
        return self.exact.get(urlsplit(url).path) or super().get_adapter(url)

    def close(self):
        super().close()
        for a in self.exact.values():
            a.close()


def restored(db_path: Path) -> dict | None:
    """What this session's `routine state --action pull` restored ({counts: rows per table}), or
    None: no pull worked here, so the local database is empty or someone else's."""
    try:
        return json.loads(db_path.with_name(RESTORED).read_text())
    except (OSError, ValueError):
        return None


def _restore(body: bytes, path: Path) -> dict[str, int]:
    """Write the gzipped SQLite database in `body` to `path`, only if it is one and SQLite finds it
    sound; its row counts (of the rows the push will still find unpruned). ValueError says what the
    body is instead."""
    if body[:2] != b"\x1f\x8b":
        what = "a security-check page" if b"Vercel Security Checkpoint" in body else f"{body[:60]!r}"
        raise ValueError(f"not a gzipped database: {what} ({len(body)} bytes)")
    try:
        data = gzip.decompress(body)
    except (OSError, EOFError, zlib.error) as e:
        raise ValueError(f"broken gzip ({e})") from None
    if not data.startswith(b"SQLite format 3\x00"):
        raise ValueError(f"not a SQLite database ({data[:30]!r})")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".pulled")
    tmp.write_bytes(data)
    try:
        check, counts = check_file(tmp, at=time.time() + PUSH_WITHIN)
    except sqlite3.DatabaseError as e:
        check = f"cannot read it: {e}"
    if check != "ok":
        tmp.unlink()
        raise ValueError(f"SQLite finds the database damaged ({check})")
    os.replace(tmp, path)
    return counts


class KreaRefused(Exception):
    """The shared Krea allowance has no image left this month (CAP) or today (PACE: the month's
    share for today, or the page's own premium cards a day)."""


class Dashboard:
    def __init__(self, url: str, secret: str):
        self.url = url.rstrip("/")
        self.s = _Session()
        self.s.headers["Authorization"] = f"Bearer {secret}"
        retrying = HTTPAdapter(max_retries=_retries())
        self.s.mount("https://", retrying)
        self.s.mount("http://", retrying)
        posts, heavy = HTTPAdapter(max_retries=_retries(post=True)), HTTPAdapter(max_retries=_retries(heavy=True))
        base = urlsplit(self.url).path
        self.s.exact = {**{base + p: posts for p in IDEMPOTENT_POSTS}, **{base + p: heavy for p in HEAVY_READS}}

    @classmethod
    def from_env(cls) -> "Dashboard | None":
        load_env()
        url, secret = os.environ.get("DASHBOARD_URL"), os.environ.get("ROUTINE_SECRET")
        return cls(url, secret) if url and secret else None

    def created_today(self, cfg: dict) -> dict[str, int]:
        """Posts already created today per page (pending + approved + posted), page-local day.
        This is what the daily cap counts, so rejected drafts free up a slot."""
        out = {}
        for p in load_pages():
            local = datetime.now(ZoneInfo(p.timezone))
            since = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            r = self.s.get(f"{self.url}/api/stats", params={"since": int(since), "page": p.slug}, timeout=30)
            r.raise_for_status()
            c = r.json().get("counts", {}).get(p.slug, {})
            out[p.slug] = sum(c.get(k, 0) for k in ("pending", "approved", "posted", "publishing", "scheduled",
                                                   "regenerate", "cleared"))
        return out

    def recent_drafts(self, hours: float) -> list[dict]:
        """Every post made in the last `hours` (any status except superseded), for the
        same-story check. The dashboard is the record of what was made, whatever the local state."""
        import time
        r = self.s.get(f"{self.url}/api/drafts", params={"status": "all", "since": int(time.time() - hours * 3600),
                                                        "limit": 300}, timeout=45)
        r.raise_for_status()
        return [{k: d.get(k) for k in ("id", "page", "status", "headline", "caption", "created_at_unix")}
                | {"caption": (d.get("status_info") or {}).get("caption") or d.get("caption") or ""}
                for d in r.json().get("drafts", []) if d.get("status") != "regenerated"]

    def editor_feedback(self, days: float = 14, cap: int = 40) -> dict[str, dict]:
        """What editors decided lately, per page: rejected posts with their reason and approved
        ones. Put into every brief so each run learns from the reviews."""
        import time
        r = self.s.get(f"{self.url}/api/drafts", params={"status": "all", "since": int(time.time() - days * 86400),
                                                        "limit": 300}, timeout=45)
        r.raise_for_status()
        out: dict[str, dict] = {}
        for d in r.json().get("drafts", []):
            page = out.setdefault(d.get("page") or "", {"rejected": [], "approved": [], "reasons": {}})
            si = d.get("status_info") or {}
            item = {"headline": d.get("headline"), "template": (d.get("copy") or {}).get("template"),
                    "reason": d.get("reason") or ""}
            if d.get("status") == "rejected":
                why = si.get("reason") or "no reason given"
                page["rejected"].append({**item, "editor_reason": why, **({"editor_note": si["note"]} if si.get("note") else {})})
                page["reasons"][why] = page["reasons"].get(why, 0) + 1
            elif d.get("status") in ("approved", "publishing", "scheduled", "posted") or (
                    d.get("status") == "failed" and d.get("approved_at")):   # approved; SocialPilot failed
                page["approved"].append(item)
        for page in out.values():
            page["rejected"], page["approved"] = page["rejected"][:cap], page["approved"][:cap]
        return out

    def krea_reserve(self, who: str, cap: int, key: str | None = None, ledger: dict | None = None,
                     report_only: bool = False, made: bool = False, page: str | None = None,
                     page_cap: int | None = None) -> dict:
        """Reserve one Krea image in the month's shared allowance (hourly routine and Studio), or with
        report_only only send the hourly routine's ledger: {hourly_used, hourly_today, month, day}, as
        counted; with made, say the image reserved with this key was made (a reservation never said to
        be made stops counting after 45 minutes; a reservation says `confirms`, or it counts as made
        whatever happens to it). Raises KreaRefused with the dashboard's KREA
        CAP/PACE/UNCHECKED message when it says no. An ask with a key (or a report) means the same
        however often it arrives: sent once more when its answer is lost or the dashboard is busy.
        page / page_cap: the page the image is for and its premium cards a UTC day (premium_per_day):
        past it the dashboard says KREA PACE for that page, whatever the month has left."""
        body = {"who": who, "cap": cap, **({"key": key} if key else {}), **(ledger or {}),
                **({"page": page} if page else {}), **({"page_cap": int(page_cap)} if page_cap is not None else {}),
                **({"report_only": True} if report_only else {}), **({"made": True} if made else {}),
                **({} if report_only or made else {"confirms": True})}
        for wait in (3, None):
            try:
                r = self.s.post(f"{self.url}/api/routine/krea", json=body, timeout=30)
                if r.status_code == 409:
                    try:
                        why = r.json().get("error")
                    except ValueError:
                        why = None
                    raise KreaRefused(why or "KREA CAP")
                r.raise_for_status()
                return r.json()
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                status = e.response.status_code if getattr(e, "response", None) is not None else None
                if wait is None or not (key or report_only) or (status is not None and status < 500):
                    raise
                time.sleep(wait)

    def create_draft(self, payload: dict) -> dict:
        r = self.s.post(f"{self.url}/api/drafts", json=payload, timeout=90)
        r.raise_for_status()
        return r.json()

    def pull_state(self, path: Path, waits: tuple[float, ...] = (5, 20)) -> str:
        """Restore the routine's database. Only a real gzipped SQLite database that SQLite finds sound
        replaces the local one, and only then is the RESTORED marker written: prepare, `studio feed`
        and push refuse to run without it (a run on an empty database would push it over the saved
        state). Tried again after each wait; StateError when it never worked."""
        marker = path.with_name(RESTORED)
        marker.unlink(missing_ok=True)
        why = ""
        for wait in (0, *waits):
            time.sleep(wait)
            try:
                r = self.s.get(f"{self.url}/api/state", timeout=45)
            except requests.RequestException as e:
                why = " ".join(f"{type(e).__name__}: {e}".split())[:300]
                continue
            if r.status_code == 404:
                raise StateError("the dashboard has no saved state (404). Only on the very first run, "
                                 "when the owner says so, carry on with --fresh")
            if r.status_code in (401, 403):      # a wrong secret or a security check: trying again won't help
                raise StateError(f"the dashboard refused the pull: HTTP {r.status_code} {r.text[:200]}")
            if r.status_code != 200:
                why = f"HTTP {r.status_code} {r.text[:200]}"
                continue
            try:
                counts = _restore(r.content, path)
            except ValueError as e:
                why = str(e)
                continue
            marker.write_text(json.dumps({"at": time.time(), "bytes": len(r.content), "counts": counts}))
            return (f"restored {path} ({path.stat().st_size // 1024} KB: "
                    + ", ".join(f"{n} {t}" for t, n in counts.items()) + ")")
        raise StateError(f"tried {1 + len(waits)} times, last: {why}")

    def push_state(self, path: Path, fresh: bool = False) -> str:
        """Save the database as the dashboard's copy. Refused when SQLite finds it damaged, when this
        session's pull did not restore it (fresh: a new database the owner asked for), or when it lost
        more than half of the runs, sent stories or spend the pull brought (an empty database). Both
        sides count only the rows pruning keeps: the first run after a pause prunes most of them."""
        try:
            check, counts = check_file(path)
        except (OSError, sqlite3.DatabaseError) as e:
            raise StateError(f"no local database to save ({e})") from None
        if check != "ok":
            raise StateError(f"SQLite finds the local database damaged ({check})")
        if not fresh:
            pulled = restored(path)
            if pulled is None:
                raise StateError("this session's `routine state --action pull` did not restore the database, "
                                 "so it would replace the saved state")
            lost = [f"{t} {n} -> {counts.get(t, 0)}" for t, n in pulled.get("counts", {}).items()
                    if n >= MIN_ROWS and counts.get(t, 0) < n / 2]
            if lost:
                raise StateError("the database lost more than half of the recent rows the pull restored ("
                                 + ", ".join(lost) + ")")
        data = gzip.compress(path.read_bytes(), 9)
        if len(data) > 4_000_000:
            raise RuntimeError(f"state is {len(data) // 1024} KB compressed; prune the database")
        r = self.s.put(f"{self.url}/api/state", data=data, timeout=120,
                       headers={"Content-Type": "application/gzip"})
        r.raise_for_status()
        return f"saved state ({len(data) // 1024} KB)"

    # ---- reviewer feedback -> regeneration
    def regen_requests(self) -> list[dict]:
        r = self.s.get(f"{self.url}/api/feedback", timeout=60)
        r.raise_for_status()
        return r.json().get("requests", [])

    def regen_failed(self, draft_id: str, error: str) -> dict:
        r = self.s.post(f"{self.url}/api/feedback", json={"id": draft_id, "action": "failed", "error": error},
                        timeout=60)
        r.raise_for_status()
        return r.json()

    # ---- "URL to post" requests from editors
    def link_requests(self) -> list[dict]:
        r = self.s.get(f"{self.url}/api/links", params={"status": "waiting"}, timeout=60)
        r.raise_for_status()
        return sorted(r.json().get("links", []), key=lambda l: l.get("at", 0))

    def link_failed(self, request_id: str, error: str) -> dict:
        r = self.s.post(f"{self.url}/api/links", json={"id": request_id, "action": "failed", "error": error},
                        timeout=60)
        r.raise_for_status()
        return r.json()

    # ---- learning from reviews
    def reviews(self, days: int = 180) -> list[dict]:
        r = self.s.get(f"{self.url}/api/routine/reviews", params={"days": days}, timeout=120)
        r.raise_for_status()
        return r.json().get("entries", [])

    def backfill_reviews(self, entries: list[dict], replace: bool = False) -> int:
        added = 0
        for i in range(0, len(entries), 400):
            r = self.s.post(f"{self.url}/api/routine/reviews", json={"entries": entries[i:i + 400], "replace": replace},
                            timeout=300)
            r.raise_for_status()
            added += r.json().get("added", 0)
        return added

    def lessons(self, page: str) -> dict | None:
        r = self.s.get(f"{self.url}/api/routine/lessons", params={"page": page}, timeout=60)
        r.raise_for_status()
        return r.json().get("lessons")

    def save_lessons(self, page: str, record: dict) -> None:
        r = self.s.post(f"{self.url}/api/routine/lessons", json={"page": page, "record": record}, timeout=60)
        r.raise_for_status()

    def made_since(self, since: float) -> dict[str, int]:
        """Drafts made per page since `since` (any status)."""
        r = self.s.get(f"{self.url}/api/stats", params={"since": int(since)}, timeout=60)
        r.raise_for_status()
        return {pg: sum(c.values()) for pg, c in r.json().get("counts", {}).items()}

    # ---- SocialPilot publisher routine
    def queue(self) -> dict:
        r = self.s.get(f"{self.url}/api/queue", timeout=60)
        r.raise_for_status()
        return r.json()

    def report(self, draft_id: str, status: str, **detail) -> dict:
        r = self.s.post(f"{self.url}/api/result", json={"id": draft_id, "status": status, **detail}, timeout=60)
        r.raise_for_status()
        return r.json()
