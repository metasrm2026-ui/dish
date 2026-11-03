"""Run reports: every `python -m bore.cli` command reports itself to the dashboard.

One record per command run (POST /api/routine/runs):
    {run_id, routine, command, started_at, ended_at, ok, degraded: [str], summary: {...}}
`ok` is worked out here from how the command ended, never typed by the model: a crash or an
unexpected exit is ok=false; a gate saying no (lint, BLOCKED, NOT DONE, REFUSED, publish SKIP/HELD)
is the command doing its job. `degraded` lists what ran with less than it should (a dependency or a
check that was unavailable, a machine problem worked around): the run is amber. Long commands also
report their start, so one whose session died shows as a start without an end.

Best effort: a report adds a few seconds at most, never breaks a command and never prints the
secret (read from the environment or .env, like the rest of bore).

    from . import ops
    ops.degraded("other outlets' photos unavailable")    # a catch-all that carried on with less
    if ops.fault(e, "still text check"):                  # a broken machine is not a bad photo: said, and raised
        raise
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSION = ROOT / "work" / ".ops-session.json"
SESSION_HOURS = 3                 # one routine session per fresh container; a dev machine's ends after 3 idle hours
DOWN_S = 600                      # after the dashboard did not answer a report: short waits, then none, for 10 minutes

# Commands whose start is reported too (they can run for minutes, or are a routine's first step):
# a session that dies in one leaves a start without an end.
LONG = {"routine prepare", "routine state pull", "routine state push", "studio sync", "studio feed",
        "studio feed-publish", "studio jobs", "studio done", "publish plan", "hourly", "brief", "collect",
        "refresh", "escalate"}

# A gate saying no is an answer, not a failure: the model fixes what it says and runs it again.
# REFUSED: an editor's link or regeneration is never skipped; NOT FOUND: `publish mark` asks for one
# more QueuedPosts call. (STATE NOT SAVED / NOT RESTORED are not gates: the session stops on them.)
GATE_RE = re.compile(r"^(BLOCKED|NOT DONE|REFUSED:|NOT FOUND |KREA (CAP|PACE|UNCHECKED)|ART REJECTED|copy\.json failed the lint"
                     r"|write copy\.json first|set \"as_is\"|no card\.jpg yet|--[\w-]+\b.*\bis required|no brief\.json in"
                     r"|not a (link|regeneration) brief|no runs yet|no feed folder|studio \w+ needs a folder)")
# SKIP (another run has it) and HELD (SocialPilot down, or the run already stopped): PUBLISH.md's
# expected outcomes. A SocialPilot outage shows as the health status's publisher_held, not as a
# failed run. STOP (`publish mark` exit 5) stays a failure: what it found is for a person.
GATE_CODES = {"publish mark": {3, 4}, "publish preflight": {4}}

# What the catch-alls of code outside this module print when they carry on with less. Each becomes
# one stable reason (no headlines, ids or error text: the same problem reads the same every run).
DEGRADED_LINES = [
    (re.compile(r"^\s*(dashboard stats|recent posts|lessons|regeneration requests|link requests|editor feedback"
                r"|video stills|other outlets' photos) unavailable\b", re.I), lambda m: f"{m[1].lower()} unavailable"),
    (re.compile(r"^\s*images for '.*' unchecked \((\w+)\)"), lambda m: f"feed photo checks failed ({m[1]})"),
    (re.compile(r"\bfaces?\b.*\bnot compared \((\w+)"), lambda m: f"faces not compared ({m[1]})"),
    (re.compile(r"^STOPPED: "), lambda m: "collection stopped by the budget guard"),
    (re.compile(r"^escalation stopped: "), lambda m: "escalation stopped by the budget guard"),
    (re.compile(r"^feeds: failed"), lambda m: "website feeds failed"),
    (re.compile(r"^refresh failed"), lambda m: "engagement refresh failed"),
    (re.compile(r"^usage log failed"), lambda m: "usage log failed"),
    # no APIFY_TOKEN, a rotated or rejected one, Apify down: prepare carries on without collecting
    (re.compile(r"^Apify unavailable \("), lambda m: "apify unavailable: collection skipped"),
]

# Errors that mean the machine is broken (a package or model that does not load, no browser), not
# that one photo or page was bad: catch-alls re-raise these, so the run fails and says so.
MACHINE_RE = re.compile(r"Executable doesn't exist|cannot open shared object|DLL load failed|undefined symbol|libGL"
                        r"|Load model from .* failed|NO_SUCHFILE|Can't read ONNX|Failed to load (the )?model", re.I)


def machine_fault(e: BaseException) -> bool:
    return isinstance(e, ImportError) or bool(MACHINE_RE.search(f"{type(e).__name__}: {e}"))


def fault(e: BaseException, where: str) -> bool:
    """A catch-all's first question: `if ops.fault(e, "still text check"): raise`. A machine fault
    is recorded before it is re-raised, so the run is amber at least (red 3 runs in a row) even
    when a caller further up swallows it."""
    if not machine_fault(e):
        return False
    degraded(f"machine fault: {type(e).__name__} ({where})")
    return True


_LOCK = threading.Lock()
_DEGRADED: list[str] = []         # this command's reasons (catch-alls run in threads too)


def degraded(reason: str) -> None:
    """Something ran with less than it should: the run is reported amber, with this reason."""
    reason = re.sub(r"https?://\S+", "", str(reason)).strip()[:120]
    with _LOCK:
        if reason and reason not in _DEGRADED and len(_DEGRADED) < 20:
            _DEGRADED.append(reason)


# ------------------------------------------------------------------ the record
def command_name(args) -> str:
    """'routine prepare', 'studio feed', 'publish mark', 'routine state push', 'health'."""
    step = getattr(args, "step", None) or (getattr(args, "action", None) if args.cmd == "publish" else None)
    name = f"{args.cmd} {step}" if step else args.cmd
    return f"{name} {args.action}" if name == "routine state" else name


def target_of(args) -> str:
    """The folder, run or draft id a command acts on (for the report: ids, never texts). A template's
    folder of a templates job is named by its id: the job's folder only."""
    t = str(getattr(args, "dir", None) or getattr(args, "run", None) or getattr(args, "id", None) or "")
    cmd, step = getattr(args, "cmd", None), getattr(args, "step", None)
    spec = cmd == "routine" and step == "tspec-render"          # (its <job>/<id>/spec.json)
    if t and (spec or (cmd == "studio" and step in ("template-send", "fail"))):
        from .studio import WORK, is_template_dir
        d = Path(t) if Path(t).is_absolute() else (WORK / t if (WORK / t).exists() else Path.cwd() / t)
        if is_template_dir(d.parent if spec else d):
            t = str(Path(t).parent.parent if spec else Path(t).parent)
    return t[:160]


def _routine_branch() -> str:
    """The branch the routines run (routines/triggers.yaml)."""
    try:
        m = re.search(r"^branch:\s*(\S+)", (ROOT / "routines" / "triggers.yaml").read_text(), re.M)
        return m[1] if m else ""
    except OSError:
        return ""


def routine_for(command: str, target: str = "", session: dict | None = None) -> str:
    """Which routine runs this: from BORE_ROUTINE (a developer's or diagnostic session sets
    BORE_ROUTINE=manual: its commands use the live dashboard too, but are not the pipeline), else the
    command (and its folder), else the session's earlier commands. The first steps every routine shares (`studio
    sync`, the state pull) run before a session names its routine: they report as 'setup'."""
    if os.environ.get("BORE_ROUTINE"):
        return re.sub(r"[^a-z0-9-]", "", os.environ["BORE_ROUTINE"].lower())[:20] or "setup"
    # a developer or diagnostic session says so (BORE_ROUTINE=manual): guessing it from the checkout
    # labelled routine sessions 'manual' (Oct 22-23), which hid their failures from the health status
    head, _, step = command.partition(" ")
    if head == "publish":
        return "publisher"
    if head == "studio" and step in ("feed", "feed-publish"):
        return "topics"
    if head == "studio" and step in ("jobs", "images", "done", "fail", "precheck"):
        return "studio"
    if head == "routine" and "/studio/" in f"/{target}":      # a Studio job's post folder (work/studio/<job>/<post>)
        return "studio"
    if (head == "routine" and not step.startswith("state")) or head == "health":
        return "hourly"
    if head in ("hourly", "collect", "brief", "refresh", "escalate", "cluster", "budget", "check", "dna", "sources",
                "import-workbook", "add-page", "capture-fixture", "tune"):
        return "manual"
    return (session or {}).get("routine") or "setup"


def _session() -> dict:
    try:
        s = json.loads(SESSION.read_text())
        if time.time() - s.get("at", 0) < SESSION_HOURS * 3600:
            return s
    except Exception:
        pass
    return {"id": uuid.uuid4().hex[:10], "routine": ""}


def _save_session(s: dict) -> None:
    try:
        SESSION.parent.mkdir(parents=True, exist_ok=True)
        SESSION.write_text(json.dumps({**s, "at": time.time()}))
    except Exception:
        pass


_COMMIT: list[str] = []


def _commit() -> str:
    if not _COMMIT:
        try:
            _COMMIT.append(subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, capture_output=True,
                                          text=True, timeout=3).stdout.strip())
        except Exception:
            _COMMIT.append("")
    return _COMMIT[0]


# ------------------------------------------------------------------ sending
def _post(url: str, secret: str, rec: dict, timeout=(4, 6)) -> int:
    import requests
    r = requests.post(f"{url.rstrip('/')}/api/routine/runs", json=rec, timeout=timeout,
                      headers={"Authorization": f"Bearer {secret}"})
    return r.status_code


_REAL_POST = _post
SECRET_RE = re.compile(r"(?i)\b(token|key|secret|password|signature)=[^&\s'\"]+")


def scrub(rec: dict) -> dict:
    """The record without secrets: an error can quote a URL with a token in it (Apify's do)."""
    text = SECRET_RE.sub(lambda m: f"{m[1]}=<secret>", json.dumps(rec, default=str))
    for k, v in os.environ.items():
        if re.search(r"(TOKEN|SECRET|KEY|PASSWORD)$", k) and len(v) >= 8:
            text = text.replace(v, "<secret>")
    try:
        return json.loads(text)
    except ValueError:                   # a replacement broke the JSON: the bare record, without its texts
        return {**{k: rec.get(k) for k in ("run_id", "routine", "command", "started_at", "ended_at", "ok")},
                "degraded": [], "summary": {"scrubbed": True}}


def send(rec: dict, session: dict | None = None) -> None:
    """POST one record; a problem prints one line (never the secret) and the command goes on. When
    the dashboard did not answer (the session file remembers it), the next reports wait 1-2 s, and
    after a second miss none is sent for 10 minutes: a slow dashboard never adds minutes to a run."""
    from .config import load_env
    load_env()
    url, secret = os.environ.get("DASHBOARD_URL"), os.environ.get("ROUTINE_SECRET")
    if not url or not secret or os.environ.get("BORE_REPORT") == "0":
        return
    if os.environ.get("PYTEST_CURRENT_TEST") and _post is _REAL_POST:     # tests never reach a real dashboard
        return
    s = session if session is not None else {}
    down = s.get("report_down") or {}
    if time.time() - down.get("at", 0) >= DOWN_S:
        down = {}
    if down.get("n", 0) >= 2:
        return
    try:
        status, why = _post(url, secret, scrub(rec), timeout=(1, 2) if down else (4, 6)), ""
    except Exception as e:
        status, why = 0, type(e).__name__
    if status and status < 500:
        s.pop("report_down", None)
        if status >= 400:
            print(f"run report not saved (HTTP {status}): carry on", file=sys.stderr)
    else:
        s["report_down"] = {"at": time.time(), "n": down.get("n", 0) + 1}
        print(f"run report not sent ({why or f'HTTP {status}'}): carry on", file=sys.stderr)
    if session is not None:
        _save_session(session)


# ------------------------------------------------------------------ the output
class _Tee:
    """Passes everything through, and keeps the text (capped) for the command's summary."""
    CAP = 300_000

    def __init__(self, stream, run):
        self._s, self._run, self._part, self._size = stream, run, "", 0
        self.text: list[str] = []

    def write(self, s):
        n = self._s.write(s)
        try:
            if self._size < self.CAP:
                self.text.append(s[: self.CAP - self._size])
                self._size += len(s)
            *lines, self._part = (self._part + s).split("\n")
            for line in lines:
                self._run.line(line)
        except Exception:
            pass
        return n

    def flush(self):
        self._s.flush()

    def __getattr__(self, k):
        return getattr(self._s, k)


class Run:
    def __init__(self, command: str, target: str = ""):
        self.command, self.target = command, target
        self.session = _session()
        self.routine = routine_for(command, target, self.session)
        if self.routine not in ("setup", "manual") and not self.session.get("routine"):
            self.session["routine"] = self.routine
        self.run_id = uuid.uuid4().hex[:12]
        self.started = time.time()
        self.summary: dict = {}
        self.lines: list[str] = []
        self.tail: deque[str] = deque(maxlen=20)
        self.out = self.err = None

    def line(self, line: str) -> None:
        for rx, reason in DEGRADED_LINES:
            m = rx.search(line)
            if m:
                degraded(reason(m))
        if len(self.lines) < 3000:
            self.lines.append(line[:300])
        self.tail.append(line[:300])               # a long run's closing lines, whatever came before

    def record(self, ended: float | None = None, ok: bool | None = None) -> dict:
        return {"run_id": self.run_id, "routine": self.routine, "command": self.command,
                "started_at": int(self.started * 1000), "ended_at": int(ended * 1000) if ended else None, "ok": ok,
                "degraded": list(_DEGRADED) if ended else [],
                "summary": {"session": self.session["id"], "target": self.target, "commit": _commit(),
                            "python": platform.python_version(), **self.summary}}

    def end(self, ok: bool) -> dict:
        ended = time.time()
        self.summary["seconds"] = round(ended - self.started, 1)
        try:
            self.summary.update(summarize(self))
        except Exception as e:                           # a summary never breaks the command or its report
            self.summary["summary_error"] = type(e).__name__
        rec = self.record(ended, ok)
        if ok and _DEGRADED:
            print(f"run report: degraded ({'; '.join(_DEGRADED[:5])})", file=sys.stderr)
        if self.command in ("routine link-fail", "routine regen-fail") and self.summary.get("exit") == 0:
            # the editor was told why: `routine check` no longer counts that request as owed
            done = self.session.setdefault("requests_failed", [])
            done[:] = [*done, str(_brief_dir(self.target))][-200:]
            _save_session(self.session)
        send(_fit(rec), self.session)
        return rec


def _fit(rec: dict, limit: int = 8000) -> dict:
    """The summary trimmed to fit one small record (long lists first)."""
    while len(json.dumps(rec, default=str)) > limit:
        s = rec["summary"]
        key = max((k for k, v in s.items() if isinstance(v, (list, dict, str)) and len(v) > 1),
                  key=lambda k: len(json.dumps(s[k], default=str)), default=None)
        if key is None:
            rec["summary"] = {k: s[k] for k in ("session", "target", "commit", "python", "seconds", "exit", "error") if k in s}
            break
        v = s[key]
        s[key] = v[: len(v) // 2] if isinstance(v, (list, str)) else dict(list(v.items())[: len(v) // 2])
    return rec


def _begin(command: str, target: str) -> Run:
    with _LOCK:
        _DEGRADED.clear()
    run = Run(command, target)
    _save_session(run.session)
    if command in LONG:
        send(run.record(), run.session)
    run.out, run.err = _Tee(sys.stdout, run), _Tee(sys.stderr, run)
    sys.stdout, sys.stderr = run.out, run.err
    return run


def outcome(command: str, e: BaseException | None) -> tuple[bool, dict]:
    """ok, and what the report says about how the command ended."""
    if e is None:
        return True, {"exit": 0}
    if isinstance(e, SystemExit):
        code = e.code if isinstance(e.code, int) or e.code is None else 1
        msg = "" if isinstance(e.code, int) or e.code is None else (str(e.code).strip().splitlines() or [""])[0][:200]
        refused = bool(code) and ((bool(msg) and bool(GATE_RE.match(msg))) or code in GATE_CODES.get(command, ()))
        ok = not code or refused
        return ok, {"exit": code or 0, **({"refused": msg or f"exit {code}"} if refused else {}),
                    **({"error": msg or f"exit {code}"} if not ok else {})}
    return False, {"exit": 1, "error": f"{type(e).__name__}: {str(e)[:300]}"}


@contextmanager
def reporting(command: str, target: str = ""):
    """Around one CLI command: the start (long commands), the output kept for the summary, and the end."""
    try:
        run = _begin(command, target)
    except Exception:                    # reporting never stops a command
        run = None
    if run is None:
        yield None
        return
    err = None
    try:
        yield run
    except BaseException as e:
        err = e
        raise
    finally:
        sys.stdout, sys.stderr = run.out._s, run.err._s
        try:
            ok, info = outcome(command, err)
            run.summary.update(info)
            run.end(ok)
        except Exception:
            pass


# ------------------------------------------------------------------ per-command summaries
def summarize(run: Run) -> dict:
    """What each command adds to its report, from what it printed and the files it wrote."""
    fn = {"routine prepare": _prepare, "routine check": _check, "health": _health, "publish plan": _plan,
          "publish mark": _mark, "studio jobs": _jobs, "studio done": _done, "studio feed": _feed,
          "studio feed-publish": _feed_publish, "routine state pull": _state, "routine state push": _state}.get(run.command)
    return fn(run) if fn else {}


def _out(run: Run) -> str:
    return "".join(run.out.text) if run.out else ""


def _brief_dir(target: str) -> Path:
    """A post folder as the routine commands find it (relative to work/ when it is there)."""
    from .routine import WORK
    d = Path(target)
    if not d.is_absolute():
        d = WORK / d if (WORK / d).exists() else Path.cwd() / d
    return d.resolve()


def _prepare(run: Run) -> dict:
    pages, cur = {}, None
    for line in run.lines:
        m = re.match(r"^(.+): quota (\d+) this run, (\d+) candidate\(s\)$", line)
        if m:
            cur = pages.setdefault(m[1], {"quota": int(m[2]), "candidates": int(m[3]), "repeats": 0, "filtered": 0})
        elif cur and line.startswith("  skipped repeat:"):
            cur["repeats"] += 1
        elif cur and (m := re.match(r"^\s+filtered out (\d+) low-value", line)):
            cur["filtered"] = int(m[1])
    out = {"pages": pages, "pacer_skipped": sorted({m[1] for x in run.lines if (m := re.match(r"^(\w+): skipped by pacer", x))})}
    budget = next((x for x in run.lines if x.startswith("budget: ")), "")
    if budget:
        out["budget"] = budget[8:160]
    run_dir = next((Path(m[1]) for x in reversed(run.tail) if (m := re.match(r"^\d+ brief\(s\) in (.+)$", x))), None)
    if run_dir and (run_dir / "run.json").exists():
        meta = json.loads((run_dir / "run.json").read_text())
        out.update(run=run_dir.name, regenerations=len(meta.get("regenerations") or []), links=len(meta.get("links") or []),
                   created_today=meta.get("created_today") or {})
    return out


REQUEST_FAILED = "request_failed.json"        # what `routine link-fail` / `regen-fail` leave in the folder (bore/cli.py)


def _check(run: Run) -> dict:
    from .cli import run_accounting
    from .routine import latest_run_dir
    d = Path(run.target) if run.target else latest_run_dir()
    if not d or not d.exists():
        return {}
    acc = run_accounting(d)
    pages = {p: {"quota": a["quota"], "uploaded": len(a["uploaded"]), "skipped": len(a["skipped"]), "open": len(a["open"]),
                 "extra": len(a["extra"]), "failed": len(a["failed"])} for p, a in acc.items()}
    # an editor's link or a reviewer's regeneration with no upload, no skip and no link-fail /
    # regen-fail (its marker, or this session's run of it): still owed
    told = set(run.session.get("requests_failed") or [])
    reqs = [x for x in sorted(d.iterdir()) if (x / "brief.json").exists() and not (x / "uploaded").exists()
            and not (x / "skip.json").exists() and _is_request(x / "brief.json")]
    failed = [x.name for x in reqs if (x / REQUEST_FAILED).exists() or str(x.resolve()) in told]
    owed = [x.name for x in reqs if x.name not in failed]
    return {"run": d.name, "pages": pages, "requests_open": owed, "requests_failed": failed,
            "check": "OK" if "check: OK" in run.lines else "NOT DONE"}


def _is_request(brief: Path) -> bool:
    b = json.loads(brief.read_text())
    return bool(b.get("link") or b.get("regeneration"))


def _norm_warning(text: str) -> str:
    """'huddle: 5 handle(s) produced nothing in 72h (renamed?): a, b' -> 'huddle: N handle(s) produced
    nothing in Nh': no handle names (source lists are confidential), no counts (the same every run)."""
    slug, sep, rest = text.strip().partition(": ")
    if not (sep and re.fullmatch(r"[a-z0-9-]+", slug)):
        slug, rest = "", text.strip()
    rest = re.sub(r"\d+(\.\d+)?", "N", re.split(r"\s\(|: ", rest, maxsplit=1)[0])[:80]
    return f"{slug}: {rest}" if slug else rest


def _health(run: Run) -> dict:
    warn = [_norm_warning(x[5:]) for x in run.lines if x.startswith("WARN ")]
    err = [_norm_warning(x[5:]) for x in run.lines if x.startswith("ERROR")]
    return {"warnings": warn[:12], "errors": err[:12]}


def _plan(run: Run) -> dict:
    text = _out(run)
    data = json.loads(text[text.index("{"): text.rindex("}") + 1]) if "{" in text else {}
    no_account = [m[1] for x in run.lines if (m := re.match(r"^# (\S+): page \S+ has no usable SocialPilot account", x))]
    ids = lambda k: [x.get("id") for x in data.get(k) or []][:20]
    out = {"to_schedule": len(data.get("to_schedule") or []), "already_scheduled": len(data.get("already_scheduled") or []),
           "waiting_for_socialpilot": ids("waiting_for_socialpilot"), "superseded": ids("superseded"),
           "interrupted": ids("interrupted"), "delete_in_socialpilot": ids("scheduled_but_superseded"), "no_account": no_account}
    # what needs a person is not a degraded run: the health status reads these lists from the latest
    # plan's report and shows them as their own reason until they are done
    return out


def _mark(run: Run) -> dict:
    out = {"id": run.target}
    for x in run.lines:                 # bore/cli.py cmd_publish: the last of these lines is how it ended
        if m := re.match(r"^(HELD|SKIP|FAILED|STOP|MISSING|NEEDS ACTION|LEAVE) ", x):
            out["outcome"] = m[1].lower().replace(" ", "_")
    return out


def _jobs(run: Run) -> dict:
    taken = [{"id": Path(m[1]).name, "kind": m[2].lower(), "page": m[3]}
             for x in run.lines if (m := re.match(r"^JOB\s+(\S+)\s+([A-Z]+)\s+page (\S+)", x))]
    left = [m[1] for x in run.lines if (m := re.match(r"^LEAVE\s+(\S+)", x))]
    back = [m[1].rstrip(":") for x in run.lines if (m := re.match(r"^BACK\s+(\S+)", x))]
    skipped = [m[1].rstrip(":") for x in run.lines if (m := re.match(r"^SKIP\s+(\S+)", x))]
    if left:
        degraded("left a Studio job: packages not loading on this machine")
    if back:
        degraded("handed a Studio job back: it could not be prepared")
    return {"taken": taken, "left": left, "handed_back": back, "skipped": skipped,
            "none_waiting": "no Studio jobs waiting" in run.lines}


def _done(run: Run) -> dict:
    from .routine import WORK
    d = Path(run.target)
    d = WORK / d if not d.is_absolute() and (WORK / d).exists() else d        # as `studio done` finds it
    job = json.loads((d / "job.json").read_text()) if (d / "job.json").exists() else {}
    status = next((m[1] for x in run.lines if (m := re.match(r"^job \S+ (\w+): ", x))), "")
    created = (job.get("created_at") or 0) / 1000
    return {"job": job.get("id") or d.name, "kind": job.get("kind", ""), "page": job.get("page", ""), "status": status,
            **({"latency_s": round(time.time() - created)} if created else {})}


def _feed(run: Run) -> dict:
    return {"pages": {Path(m[1]).stem: {"stories": int(m[2]), "to_write": int(m[3])}
                      for x in run.lines if (m := re.match(r"^FEED\s+(\S+): (\d+) stories, (\d+) to write", x))}}


CUT_RE = re.compile(r"[.!?\"'”’)\]]$")


def _feed_publish(run: Run) -> dict:
    from .routine import latest_run_dir
    d = Path(run.target) if run.target else latest_run_dir()
    pages = {}
    now = time.time()
    for f in sorted(((d / "feed").glob("*.json")) if d else []):
        stories = json.loads(f.read_text()).get("stories") or []
        # the feed's times are in milliseconds (bore/studio.py); seconds are read too
        ages = [now - (t / 1000 if t > 1e12 else t) for s in stories if (t := s.get("published_at"))]
        imgs = [i for s in stories for i in s.get("images") or []]
        pages[f.stem] = {
            "stories": len(stories), "newest_min": round(min(ages) / 60) if ages else None,
            "in_3h": sum(a <= 3 * 3600 for a in ages), "in_12h": sum(a <= 12 * 3600 for a in ages),
            "written": sum(bool(s.get("summary")) for s in stories),
            "unwritten": sum(bool(s.get("needs_writing")) and not s.get("summary") for s in stories),
            "cut_summaries": sum(bool(s.get("summary")) and not CUT_RE.search(s["summary"].strip()) for s in stories),
            "no_images": sum(not s.get("images") for s in stories),
            "unchecked": sum(not s.get("images_v") for s in stories),
            "face_match": sum(i.get("id_match") == "match" for i in imgs),
            "face_mismatch": sum(i.get("id_match") == "mismatch" for i in imgs)}
    return {"pages": pages}


def _state(run: Run) -> dict:
    for x in run.lines:
        if m := re.search(r"\((\d+) KB\)", x):
            return {"kb": int(m[1])}
        if "no saved state yet" in x:
            return {"kb": 0}
    return {}
