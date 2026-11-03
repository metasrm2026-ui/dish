"""Scheduling rules for the SocialPilot publisher (never post directly):

  * every post is scheduled at least `min_delay_minutes` (60) after it was approved, and after now
  * posts on the same page are at least `min_gap_minutes` (45) apart, counting posts already
    scheduled from the dashboard and anything else queued in SocialPilot for that account
  * times are rounded up to the next 5 minutes and expressed in SocialPilot's account timezone
  * hourly-routine posts go out only in the audience's hours (`audience_hours`: New York 07:00-23:00):
    a slot outside them moves to the next morning, then the 45-minute gaps apply from there
  * Studio posts carry the time the editor chose (`requested_at`, ms): that time is kept as given
    (an editor's explicit choice) while it is at least `min_lead_minutes` (60, the owner's rule)
    from now. "Post now", and a chosen time that has passed (the post was held or retried), get
    the earliest slot from `min_lead_minutes` on that keeps the 45-minute gap. They are scheduled,
    never shared directly.
  * when SocialPilot itself is unavailable (account locked or unpaid, connector missing, outage) a
    draft is held, not failed: it waits and is tried again about once an hour, until it would go
    out more than 12 hours after its approval (then it moves to Failed for an editor's look)
  * what the model did is checked by code: a read-only preflight call before the first CreatePost,
    a breaker on failures per SocialPilot account (one page's broken account holds only its own
    drafts), and the QueuedPosts entry of every scheduled post (account, time, caption, one copy)
    against what was claimed
"""
from __future__ import annotations

import ast
import html
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

# The owner's rule, whatever config/socialpilot.yaml says: "Keaton Caleb" is never posted to.
NEVER_LOGIN_IDS = frozenset({"2662937"})


def _round_up(ts: float, minutes: int = 5) -> float:
    step = minutes * 60
    return math.ceil(ts / step) * step


def into_window(t: float, window: tuple[str, int, int] | None) -> float:
    """t, or the start of the next audience window (tz, start hour, end hour) when t is outside it."""
    if not window:
        return t
    wtz, start, end = window
    local = datetime.fromtimestamp(t, ZoneInfo(wtz))
    if start <= local.hour < end:
        return t
    day = local if local.hour < start else local + timedelta(days=1)
    return day.replace(hour=start, minute=0, second=0, microsecond=0).timestamp()


def plan(queue: list[dict], busy: dict[str, list[float]], now: float | None = None,
         min_delay_minutes: int = 60, min_gap_minutes: int = 45, tz: str = "Asia/Kolkata",
         min_lead_minutes: int = 60, window: tuple[str, int, int] | None = None) -> list[dict]:
    """queue: [{id, page, approved_at}] oldest approval first. busy: {page: [unix times already taken]}.
    window: hourly-routine posts only go out inside it (tz, start hour, end hour).
    Returns [{id, page, at (unix), local ("YYYY-MM-DD HH:mm" in tz), iso_utc}] in the same order."""
    now = now or time.time()
    gap, delay, lead = min_gap_minutes * 60, min_delay_minutes * 60, min_lead_minutes * 60
    taken = {p: sorted(v) for p, v in busy.items()}
    out = []
    studio = sorted((q for q in queue if q.get("requested_at")), key=lambda q: q["requested_at"])
    # a time the editor picked is kept as given. "Post now", or a time that passed while the post
    # was held or failed, was not their pick: it gets the earliest free slot from then, kept 45
    # minutes from the page's other posts like an hourly post's (two such posts used to get the
    # same minute, next to a post already scheduled). Only ever later, never sooner.
    chosen = [q for q in studio if q.get("mode") != "now" and q["requested_at"] / 1000 >= now + lead]
    for item in chosen:                                              # editors' chosen times first
        t = item["requested_at"] / 1000
        taken.setdefault(item["page"], []).append(t)
        out.append(_slot(item, t, tz))
    for item in (q for q in studio if q not in chosen):
        slots = taken.setdefault(item["page"], [])
        t = _clear(max(item["requested_at"] / 1000, _round_up(now + lead, 1)), slots, gap)
        slots.append(t)
        out.append(_slot(item, t, tz))
    # oldest approval first (a draft held while SocialPilot was down keeps its first approval)
    for item in sorted((q for q in queue if not q.get("requested_at")),
                       key=lambda q: q.get("first_approved_at") or q.get("approved_at") or now):
        page = item["page"]
        held = False

        def fit(x: float) -> float:
            nonlocal held
            y = into_window(x, window)
            held = held or y != x
            return y
        slots = taken.setdefault(page, [])
        t = _clear(fit(_round_up(max(now, item.get("approved_at") or now) + delay)), slots, gap, fit)
        slots.append(t)
        out.append({**_slot(item, t, tz), **({"held_for_audience_hours": True} if held else {})})
    return out


def _clear(t: float, slots: list[float], gap: float, fit=lambda x: x) -> float:
    """t, or the first time after it that is at least `gap` from every one of `slots`."""
    moved = True
    while moved:
        moved = False
        for s in slots:
            if abs(t - s) < gap:
                t = fit(_round_up(s + gap))
                moved = True
    return t


def _slot(item: dict, t: float, tz: str) -> dict:
    t = int(math.ceil(t / 60) * 60)   # SocialPilot takes whole minutes
    local = datetime.fromtimestamp(t, ZoneInfo(tz))
    return {"id": item["id"], "page": item["page"], "at": t, "local": local.strftime("%Y-%m-%d %H:%M"),
            "iso_utc": datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")}


def parse_busy(values: list[str], tz: str = "Asia/Kolkata") -> dict[str, list[float]]:
    """--busy 'huddle=2026-09-26 21:00' (SocialPilot local time) -> {page: [unix]}"""
    out: dict[str, list[float]] = {}
    for v in values or []:
        page, _, when = v.partition("=")
        dt = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(tz))
        out.setdefault(page.strip(), []).append(dt.timestamp())
    return out


# Errors that say SocialPilot or the workspace refused everything, so the post was certainly not
# created: the draft is held for a later try. Anything ambiguous (a timeout or server error, where
# the post may exist; "Error uploading media"; one page's Facebook token) fails as before, for a
# person to look at, until the breaker sees that page's account fail again (breaker_trips).
# PUBLISH.md asks the routine to confirm a vague error with AccountList, which answered "Your
# Account is Locked" during the Oct 2026 lock.
OUTAGE_RE = re.compile(r"\b(your |socialpilot )?account (is |has been )?(currently |temporarily )?locked\b|"
                       r"\bworkspace (is |has been )?(locked|inactive|suspended)\b|\btrial( period)?( has| is)? (ended|expired|over)\b|"
                       r"\bsubscription (has |is )?(ended|expired|inactive|required|cancell?ed)\b|\bplan (has |is )?(expired|ended|inactive)\b|"
                       r"\bno active (plan|subscription)\b|\bbilling (issue|problem|required|failed)\b|\bpayment (failed|required|due|declined)\b|"
                       r"\bupgrade your (plan|account|subscription)\b|\bconnector not attached\b|"
                       r"\bsocialpilot (connector|tools?) (is |are )?(not available|unavailable|missing)\b|\bno such tool\b", re.I)
OWN_MESSAGES = ("superseded by", "publisher run interrupted", "not delivered", "unscheduled by socialpilot", "waited too long",
                "earlier copy", "created but not", "page has no socialpilot account", "lookup failed",
                "scheduled on the wrong socialpilot account")
DRAFT_ID_RE = re.compile(r"\b[a-z0-9-]+-\d{12}-[a-z0-9-]+\b", re.I)
RETRY_MINUTES = 40
# A page's unrecognised CreatePost failure counts for the breaker in later runs this long
# (dashboard/routes/result.js failing_pages): one run per approval used to fail each post on its own.
BREAKER_MINUTES = 60
# The session's own permission check stopped the call (the routine's classifier, a denied tool):
# CreatePost never ran, and every later call in the run would be stopped the same way.
SESSION_BLOCK_RE = re.compile(r"\bclassifier\b|\bpermission to use \S+ (has been |was )?(denied|blocked)\b|"
                              r"\btool (use |call )?(was |has been )?(denied|blocked|rejected)\b|"
                              r"\bblocked by (the )?(session|auto mode|permission)", re.I)


def own_message(error: str | None) -> bool:
    return bool(error) and error.strip().lower().startswith(OWN_MESSAGES)


def outage_error(error: str | None) -> bool:
    """SocialPilot / the workspace is down (hold the draft), not this post. Never the publisher's
    own fixed messages, and never words inside a draft id (ids are made of headline words)."""
    if not error or own_message(error):
        return False
    return bool(OUTAGE_RE.search(DRAFT_ID_RE.sub(" ", error)))


def session_blocked(error: str | None) -> bool:
    """A permission block in this session, not SocialPilot refusing the post: held, never failed."""
    return bool(error) and not own_message(error) and bool(SESSION_BLOCK_RE.search(DRAFT_ID_RE.sub(" ", error)))


def breaker_trips(failures: list[dict], login_id, since: float) -> list[dict]:
    """This run's earlier unrecognised CreatePost failures on the same SocialPilot account, with no
    post scheduled on it since. Any one of them means a second failure there is SocialPilot failing
    that account, worded in a way OUTAGE_RE does not know (Oct 10-11: every approved post failed one
    by one): the second is held, not failed. Per account: one page's broken Facebook connection
    must not stop the other pages' posts (it used to hold them all for up to 12 hours)."""
    return [f for f in failures if str(f.get("login_id") or "") == str(login_id or "") and f.get("at", 0) >= since]


def split_waiting(queue: list[dict], socialpilot: dict, now: float | None = None,
                  retry_minutes: int = RETRY_MINUTES) -> tuple[list[dict], list[dict]]:
    """(ready, waiting). While SocialPilot is down it is tried about once an hour, not by every run:
    a draft held in the last `retry_minutes` waits, and so does everything when the last try (any
    draft) failed that recently. A run that finds SocialPilot back schedules them all."""
    now = now or time.time()
    tried = (socialpilot or {}).get("down") and now - ((socialpilot or {}).get("last_try_at") or 0) / 1000 < retry_minutes * 60
    ready, waiting = [], []
    for x in queue:
        recent = x.get("holds") and now - (x.get("approved_at") or 0) < retry_minutes * 60
        (waiting if tried or recent else ready).append(x)
    return ready, waiting


# ---- what the model passes back from the SocialPilot connector ---------------------------------

def sp_results(text: str) -> list:
    """Every JSON value in what the model saved from connector calls: one result, several in a row,
    or the tool's content wrapper ([{"type": "text", "text": "{...}"}]). Code fences, a label or any
    other text around them is skipped: a slip in how the file was written must not hide a result
    (or look like SocialPilot failing)."""
    out, dec, i, text = [], json.JSONDecoder(), 0, (text or "").strip()
    while i < len(text):
        if text[i] not in "{[":
            i += 1
            continue
        try:
            v, i = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(v, list) and v and all(isinstance(x, dict) and x.get("type") == "text" for x in v):
            out.extend(r for x in v for r in sp_results(x.get("text", "")))
        else:
            out.append(v)
    if not out and "{" in text:        # written out as a Python value ({'success': True, ...})
        try:
            v = ast.literal_eval(text[text.index("{"):text.rindex("}") + 1])
            out = [v] if isinstance(v, (dict, list)) else []
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            pass
    return out


def sp_entries(text: str) -> list[dict]:
    """The posts in a QueuedPosts (or DeliveredPosts ...) result: {"posts": [...]}, {"data": [...]},
    a list of posts, or one post."""
    out = []
    for v in sp_results(text):
        if isinstance(v, dict) and isinstance(v.get("posts"), list):
            v = v["posts"]
        elif isinstance(v, dict) and isinstance(v.get("data"), list):
            v = v["data"]
        out.extend(x for x in (v if isinstance(v, list) else [v]) if isinstance(x, dict) and ("postDesc" in x or "postId" in x))
    return out


def _caption_key(s: str | None) -> str:
    # SocialPilot may change line endings, spacing and entities, and shortens links
    return " ".join(re.sub(r"https?://\S+", "<link>", html.unescape(str(s or ""))).split())


def same_caption(a: str | None, b: str | None) -> bool:
    return bool(_caption_key(a)) and _caption_key(a) == _caption_key(b)


def sp_time(entry: dict, tz: str) -> float | None:
    """SocialPilot's own time for a post: "postTimeFormat": "2026-10-13 16:30", in the account timezone."""
    try:
        return datetime.strptime(str(entry.get("postTimeFormat") or "").strip()[:16], "%Y-%m-%d %H:%M") \
            .replace(tzinfo=ZoneInfo(tz)).timestamp()
    except ValueError:
        return None


def sp_created(entry: dict, tz: str) -> float | None:
    """When the post was made in SocialPilot: "createdOn": "Oct 13, 2026 09:31 AM" (account timezone)."""
    try:
        return datetime.strptime(str(entry.get("createdOn") or "").strip(), "%b %d, %Y %I:%M %p") \
            .replace(tzinfo=ZoneInfo(tz)).timestamp()
    except ValueError:
        return None


# What a failed connector call says, when the model saved that instead of a result
ERRORISH_RE = re.compile(r"\berror\b|\bfail|\bunavailable\b|\btimed? ?out\b|\btimeout\b|\bdenied\b|\brefused\b|"
                         r"\bunauthori[sz]ed\b|\bforbidden\b|\blocked\b|\bexpired\b|\binvalid\b|\bdisconnected\b|"
                         r"\bnot (available|found|attached|connected|allowed)\b|\bno such tool\b|\b[45]\d\d\b", re.I)


# Old names for the same zones: the routines' containers have no tzdata "backward" links
ZONE_ALIASES = {"Asia/Calcutta": "Asia/Kolkata", "Asia/Katmandu": "Asia/Kathmandu", "Asia/Saigon": "Asia/Ho_Chi_Minh",
                "Asia/Rangoon": "Asia/Yangon", "Asia/Dacca": "Asia/Dhaka", "Europe/Kiev": "Europe/Kyiv",
                "US/Eastern": "America/New_York", "US/Central": "America/Chicago", "US/Mountain": "America/Denver",
                "US/Pacific": "America/Los_Angeles", "GMT": "UTC", "Etc/UTC": "UTC", "Etc/GMT": "UTC"}


def _zone(name: str) -> tzinfo | None:
    """A timezone from what SocialPilot shows: "Asia/Kolkata", "(GMT+05:30) Asia/Calcutta", "UTC+05:30"."""
    name = str(name or "").strip()
    for cand in [name] + re.findall(r"\b[A-Z][A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?", name):
        try:
            return ZoneInfo(ZONE_ALIASES.get(cand, cand))
        except (ValueError, KeyError, OSError):
            pass
    m = re.fullmatch(r"\(?(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?\)?", name, re.I)
    if m:
        off = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return timezone(off if m.group(1) == "+" else -off)
    return None


def same_zone(a: str, b: str, now: float | None = None) -> bool:
    """Do the two zones keep the same clock, now and half a year from now (Asia/Calcutta is
    Asia/Kolkata)? One that cannot be read never matches: every post's time would be a guess."""
    za, zb = _zone(a), _zone(b)
    if not za or not zb:
        return False
    t = datetime.fromtimestamp(now or time.time(), timezone.utc)
    return all(x.astimezone(za).utcoffset() == x.astimezone(zb).utcoffset() for x in (t, t + timedelta(days=182)))


def check_preflight(text: str, tz: str) -> tuple[str, list[str], dict[str, str]]:
    """(error, notes, troubled) from the AccountList / UserInfo results the model saved before the
    first CreatePost. An error, whatever its wording, means SocialPilot cannot take posts now: the
    run's drafts are held. A different account timezone would move every post: held too, for a
    person. troubled: {loginId: note} for accounts SocialPilot itself flags (needs reconnecting,
    locked): one failure there holds that page's draft, and the other pages go on.
    ValueError for a mistake in the command (no AccountList result; a file with no result in it that
    does not read like an error): nothing is held, the model saves the result and runs it again."""
    if not (text or "").strip():
        raise ValueError("the result file is empty: save the AccountList result in it")
    vals = sp_results(text)
    if not vals:
        said = " ".join(text.split())
        if ERRORISH_RE.search(said) or outage_error(said):      # what the connector said instead of a result
            return f"SocialPilot did not answer: {said[:200]}", [], {}
        raise ValueError("no AccountList result found in the file: save the result exactly as the connector returned it "
                         "(if the call itself failed, run publish preflight --error \"<its error>\" instead)")
    notes, troubled, accounts_seen, zone_seen = [], {}, False, False
    for v in vals:
        if not isinstance(v, dict):
            continue
        if v.get("success") is False:
            return f"SocialPilot error: {str(v.get('message') or 'request failed')[:200]}", notes, troubled
        if "accounts" in v:
            accounts_seen = True
            accts = [a for a in v.get("accounts") or [] if isinstance(a, dict)]
            if not accts:
                return "SocialPilot lists no accounts", notes, troubled
            for a in accts:
                if a.get("isReconnect") or a.get("isLocked"):
                    note = (f"account {a.get('accountUsername') or a.get('loginId')} ({a.get('loginId')}) "
                            f"{'needs reconnecting' if a.get('isReconnect') else 'is locked'} in SocialPilot")
                    notes.append(note)
                    troubled[str(a.get("loginId"))] = note
        setting = ((v.get("data") or {}).get("setting") or {}) if isinstance(v.get("data"), dict) else {}
        if setting.get("timeZone"):
            zone_seen = True
            if not same_zone(setting["timeZone"], tz):
                return (f"SocialPilot's account timezone is {setting['timeZone']}, config/socialpilot.yaml says {tz}: "
                        "fix the config before anything is scheduled"), notes, troubled
    if not accounts_seen:
        raise ValueError("no AccountList result in the file(s): call AccountList and save its result")
    if not zone_seen:
        notes.append(f"no UserInfo timezone in the results: SocialPilot's account timezone was not checked against {tz}")
    return "", notes, troubled


def verify_queued(entries: list[dict], expect: dict, tz: str, allowed: set[str], blocked: set[str]) -> dict:
    """The QueuedPosts entry of a post this run scheduled, checked against its claim.
    expect: {caption, login_id, slot (unix), claimed_at (unix)}; slot/claimed_at are unknown for a
    post another run made. Returns {entry, copies, problems, stop, wrong_account, sp_time}."""
    caption, login = expect.get("caption"), str(expect.get("login_id") or "")
    slot, claimed = expect.get("slot"), expect.get("claimed_at")
    mine = [e for e in entries if same_caption(e.get("postDesc"), caption)]
    altered = False
    if not mine and slot:      # the caption was changed on the way: the post on our account at our slot
        mine = [e for e in entries if str(e.get("loginId")) == login and (t := sp_time(e, tz)) and abs(t - slot) <= 60]
        altered = bool(mine)
    if not mine:
        return {"entry": None, "copies": 0, "problems": [], "stop": False, "wrong_account": False, "sp_time": None}
    bad = lambda lid: lid in blocked or lid in NEVER_LOGIN_IDS or bool(allowed and lid not in allowed) or bool(login and lid != login)
    ours = [e for e in mine if not bad(str(e.get("loginId") or ""))]
    others = [e for e in mine if e not in ours]
    entry = ours[0] if ours else mine[0]
    # a copy elsewhere is for a person to delete; the draft fails only when no copy is on its own
    # account (its own copy still goes out: failing it then would invite a Retry, a second post)
    problems, stop, wrong = [], bool(others), not ours
    if others:
        e = others[0]
        problems.append(f"a copy is on the wrong SocialPilot account ({e.get('accountUsername') or 'unknown'}, "
                        f"loginId {e.get('loginId') or '?'}): delete it in SocialPilot")
    t = sp_time(entry, tz)
    created = sp_created(entry, tz)
    new = bool(claimed) and (created is None or created >= claimed - 120)    # made after this run's claim
    when = lambda x: datetime.fromtimestamp(x, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")
    if t is None:
        problems.append("SocialPilot shows no scheduled time for it: check it in SocialPilot")
        stop = True
    elif new and slot and abs(t - slot) > 60:
        problems.append(f"it is scheduled for {when(t)}, not the planned {when(slot)} ({tz}): fix the time in SocialPilot or delete it")
        stop = True
    elif new and t < claimed + 55 * 60:
        problems.append(f"it is scheduled for {when(t)}, less than an hour out: fix the time in SocialPilot or delete it")
        stop = True
    copies = len(ours) if ours else len(mine)
    if copies > 1:
        problems.append(f"{copies} copies are queued in SocialPilot: delete all but one")
        stop = True
    if altered:
        problems.append("its caption in SocialPilot is not the approved one: check it in SocialPilot")
    return {"entry": entry, "copies": copies, "problems": problems, "stop": stop, "wrong_account": wrong, "sp_time": t}
