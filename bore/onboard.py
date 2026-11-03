"""The onboarding routine's side of Go live (ONBOARD.md; docs/PRODUCT.md "The onboarding routine").

A tester presses Go live: the dashboard sets the page `going_live`, writes an onboard job and fires
the onboarding routine. These commands are that routine's steps, each checked here before it reaches
the dashboard (dashboard /api/routine/onboard/*), and each reported as a step the tester sees live:

  jobs        claim one waiting onboard job: work/onboard/<slug>/brief.json with the page, its
              documents (`studio sync`) and what to produce
  step        report a step (ok or failed, with a detail in plain words)
  link        the SocialPilot account the session found: exactly one Facebook account matching the
              page in the AccountList result, never a denied one, never one another page uses
  sources     the researched sources: X handles, subreddits, web searches, websites; each active
              this month, within the page's source budget
  playbook    the page's playbook, checked against what the engine reads
  templates   queue the templates conversion (a Studio templates job, as on an upload)
  first-run   the hourly flow for this page only, on a fresh database (never the saved state)
  done        the page is live
  fail        the page is `failed`, with a reason the tester can act on

Only the onboarding routine ever sets a page's SocialPilot account (`link`): the publisher uses no
other (PUBLISH.md).
"""
from __future__ import annotations

import json
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .config import ROOT

WORK = ROOT / "work" / "onboard"
# the steps the tester sees, in order (docs/PRODUCT.md); `claim` is recorded by the dashboard itself
STEPS = ("claim", "link", "documents", "research", "sources", "playbook", "templates", "first_run", "done")
NEEDED = ("link", "sources", "playbook", "templates", "first_run")     # before `done`
REQUIRED_DOCS = ("templates", "tone")                                  # visual references are optional
ACTIVE_DAYS = 31          # a source must have posted within this many days ("active this month")
# a page's sources, at most (the hourly collection's cost grows with each one; config onboard.source_limits
# may set lower ones). Web searches: the collector runs the first collection.web_searches_per_page.
SOURCE_LIMITS = {"x": 30, "subreddits": 6, "web_searches": 4, "websites": 10}
X_ROLES = ("reporter", "official", "outlet", "other")
NOT_WEBSITES = ("x.com", "twitter.com", "reddit.com", "facebook.com", "instagram.com", "tiktok.com", "youtube.com")
DETAIL_MAX = 300

SOURCES_EXAMPLE = {
    "x": [{"handle": "demoreporter", "role": "reporter", "last_post": "2026-11-02", "why": "covers the demo league daily"}],
    "subreddits": [{"name": "demoleague", "last_post": "2026-11-02", "why": "the league's main fan community"}],
    "web_searches": [{"query": "Demo League news today", "why": "the day's coverage"}],
    "websites": [{"url": "https://demo-news.example/league", "last_post": "2026-11-01", "why": "the niche's main outlet"}],
}

# What the engine reads from a playbook (bore/routine.py, select.py, guard.py, kinds.py): the keys,
# their types, and the ones every page needs. A key the engine does not read is refused (a misspelt
# key would be silently ignored).
PLAYBOOK_DOC = {
    "page": "the page's slug (optional; must be this page's)",
    "name": "the page's name as its posts and logo show it",
    "niche": "one or two sentences: what the page covers (required)",
    "audience": "who reads it",
    "worth_posting": "what makes a story worth a post here, the kinds to post and not to post; score 0-10 for niche fit (required)",
    "voice": "the page's voice in a few sentences, from the tone document (required)",
    "caption_rules": "list of the caption rules: first line, length, emoji, closing question, hashtags (required)",
    "headline_rules": "list of the card headline rules (length, case, words)",
    "template_rules": "list: which card template for which kind of story",
    "examples": "list of 3-6 of the page's own recent captions, word for word (the voice to match)",
    "lint": "{hashtags: {min, max, required: '#Tag'}, closing_question: true|false, caps_hook: true|false}",
    "prefilter": "{skip: [regex], boost: [regex], trade_only_sources: [x handle]}: stories dropped / ranked higher",
    "logo_words": "list of the words of the page's logo (never read as invented card text)",
    "kinds": "list of {id, label, match: [regex]}: the page's kinds of story (else sports or celebrity defaults)",
    "card": "{templates: [layout names], default_template: name, art: {template: art description}}",
    "style_refs": "list of image URLs of the page's own posts (Krea style references)",
}
_STR, _LIST, _DICT = str, list, dict
PLAYBOOK_TYPES = {"page": _STR, "name": _STR, "niche": _STR, "audience": _STR, "worth_posting": _STR, "voice": _STR,
                  "caption_rules": _LIST, "headline_rules": _LIST, "template_rules": _LIST, "examples": _LIST,
                  "lint": _DICT, "prefilter": _DICT, "logo_words": _LIST, "kinds": _LIST, "card": _DICT, "style_refs": _LIST}
PLAYBOOK_REQUIRED = ("niche", "worth_posting", "voice", "caption_rules")


class Refused(ValueError):
    """What the session must fix (or tell the tester) before the step counts."""


# ------------------------------------------------------------------ dashboard
def _dash():
    from .studio import _dash as dash
    return dash()


def _call(dash, path: str, body: dict | None = None) -> dict:
    """GET (no body) or POST /api/routine/onboard/<path>. A 409 means the dashboard stopped this
    onboarding (paused, failed, or another session has it): the session stops, nothing is retried."""
    url = f"{dash.url}/api/routine/onboard/{path}"
    r = dash.s.get(url, timeout=60) if body is None else dash.s.post(url, json=body, timeout=120)
    if r.status_code == 409:
        try:
            why = r.json().get("error") or r.text
        except ValueError:
            why = r.text
        raise Stopped(str(why)[:300])
    if r.status_code >= 400:
        raise RuntimeError(f"/api/routine/onboard/{path}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


class Stopped(RuntimeError):
    """The dashboard refused: this onboarding is not this session's to continue."""


# ------------------------------------------------------------------ local record
def folder(slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", slug or ""):
        raise Refused(f"not a page slug: {slug!r}")
    return WORK / slug


def brief(slug: str) -> dict:
    f = folder(slug) / "brief.json"
    if not f.exists():
        raise Refused(f"no onboard job of {slug} was claimed on this machine: run `python -m bore.cli onboard jobs` first")
    return json.loads(f.read_text())


def _done_steps(slug: str) -> dict:
    try:
        return json.loads((folder(slug) / "progress.json").read_text())
    except (OSError, ValueError):
        return {}


def _record(slug: str, step: str, ok: bool) -> None:
    done = _done_steps(slug)
    done[step] = ok
    (folder(slug) / "progress.json").write_text(json.dumps(done))


def step(slug: str, name: str, ok: bool, detail: str = "", dash=None) -> dict:
    """Report one step to the dashboard (the tester sees it at once) and remember it here."""
    if name not in STEPS:
        raise Refused(f"unknown step {name!r}: one of {', '.join(STEPS)}")
    b = brief(slug)
    detail = " ".join(str(detail or "").split())[:DETAIL_MAX]
    got = _call(dash or _dash(), "step", {"id": b["job"], "page": slug, "step": name, "ok": bool(ok), "detail": detail})
    _record(slug, name, bool(ok))
    return got


def _result(slug: str, body: dict, dash=None) -> dict:
    return _call(dash or _dash(), "result", {"id": brief(slug)["job"], "page": slug, **body})


# ------------------------------------------------------------------ jobs
def pull_job(dash=None, cfg: dict | None = None) -> Path | None:
    """Claim the oldest waiting onboard job (one per session), sync the pages' settings and documents
    (`studio sync`) and write the job's brief: the page, its documents, the files to produce and
    their rules. None when no job waits."""
    from . import studio as S
    dash = dash or _dash()
    for job in _call(dash, "jobs").get("jobs") or []:
        try:
            got = _call(dash, "claim", {"id": job["id"], "session": S._session_id()})
        except Stopped as e:
            print(f"SKIP   {job['id']}: {e}")
            continue
        page = got.get("page") or {}
        slug = page.get("slug") or job.get("page")
        try:
            return _write_brief(dash, job, got, page, slug, cfg)
        except Exception as e:
            # this machine's problem, never the tester's: the job goes back to waiting for the next session
            try:
                _call(dash, "claim", {"id": job["id"], "session": S._session_id(), "release": True,
                                      "error": f"{type(e).__name__}: {e}"[:300]})
            except Exception:
                pass
            raise
    print("no onboard jobs waiting")
    return None


def _write_brief(dash, job: dict, got: dict, page: dict, slug: str, cfg: dict | None) -> Path:
    from . import studio as S
    d = folder(slug)
    d.mkdir(parents=True, exist_ok=True)
    job_id = (got.get("job") or {}).get("id") or job["id"]
    try:
        before = json.loads((d / "brief.json").read_text()).get("job")
    except (OSError, ValueError):
        before = None
    if before != job_id:                         # a new job (Try again): its steps start over
        (d / "progress.json").write_text("{}")   # (the same job claimed again keeps what was done)
    S.sync(dash, quiet=True)
    docs = {}
    meta = S.studio_meta().get(slug) or {}
    for t, doc in (meta.get("docs") or {}).items():
        entry = {"file": doc.get("file"), "version": doc.get("version")}
        if t in S.DOC_IMAGE_TYPES:
            try:      # the pages as pictures: the templates' zone diagrams, the visual examples
                entry["images"] = (S.doc_pages(slug, t, dash) or {}).get("images") or []
            except Exception as e:     # this machine; the text is still there
                entry["images"], entry["images_error"] = [], f"{type(e).__name__}: {e}"[:200]
        docs[t] = entry
    missing = [t for t in REQUIRED_DOCS if t not in docs]
    rel = lambda f: str(f.relative_to(ROOT)) if f.is_relative_to(ROOT) else str(f)
    out = {
        "job": job_id, "page": {k: page.get(k) for k in (
            "slug", "name", "workspace", "fb_page_url", "niche", "language", "timezone", "posts_per_day", "premium_per_day")},
        "docs": docs, "missing_docs": missing,
        "files": {"accounts": rel(d / "accounts.json"), "sources": rel(d / "sources.json"), "playbook": rel(d / "playbook.yaml")},
        "source_limits": limits(cfg), "active_days": ACTIVE_DAYS, "sources_example": SOURCES_EXAMPLE,
        "playbook_keys": PLAYBOOK_DOC, "playbook_example": "config/playbooks/example.yaml",
        "used_login_ids": [str(x) for x in got.get("used_login_ids") or []],
    }
    (d / "brief.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"JOB    {rel(d)}/brief.json  page {slug} ({page.get('name') or slug}): {page.get('fb_page_url') or 'no Facebook URL'}")
    if missing:
        print(f"DOCS MISSING: {', '.join(missing)}: fail the job (`onboard fail {slug} \"upload the "
              f"{' and '.join(missing)} document(s), then press Try again\"`)")
    return d


# ------------------------------------------------------------------ the scheduler account
def denied_login_ids(path: Path | None = None) -> set[str]:
    """Accounts never used, whatever a page record says: config/socialpilot.yaml `deny` (and the
    engine's older name for it, `blocked_login_ids`), and the publisher's own never-list."""
    f = path or ROOT / "config" / "socialpilot.yaml"
    sp = (yaml.safe_load(f.read_text()) or {}) if f.exists() else {}
    out = {str(x) for k in ("deny", "blocked_login_ids") for x in (sp.get(k) or [])}
    try:
        from .publish import NEVER_LOGIN_IDS
        out |= {str(x) for x in NEVER_LOGIN_IDS}
    except ImportError:
        pass
    return out


def _key(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _fb_slug(url: str) -> str:
    from .cli import fk_slug_from_url
    return _key(fk_slug_from_url(url)) if "facebook.com" in str(url or "").lower() else ""


def _account_name(a: dict) -> str:
    return str(a.get("accountUsername") or a.get("accountName") or a.get("name") or "")


def matches(page: dict, accounts: list[dict]) -> list[dict]:
    """The Facebook accounts in an AccountList result that are this page: the same name (case,
    spaces and punctuation aside) or a Facebook address with the page's own."""
    name, slug = _key(page.get("name")), _fb_slug(page.get("fb_page_url") or "")
    out = []
    for a in accounts:
        if "facebook" not in str(a.get("platform") or a.get("accountType") or "").lower():
            continue
        urls = {_fb_slug(v) for v in a.values() if isinstance(v, str)} - {""}
        if (name and _key(_account_name(a)) == name) or (slug and slug in urls):
            out.append(a)
    return out


def check_link(page: dict, login_id: str, name: str, result_text: str, denied: set[str], used: set[str]) -> dict:
    """The account `link` may save: the one Facebook account in the AccountList result that matches
    the page, which must be the one the session names. Refused: no match (the tester connects the
    page), several, a denied or already used account, or a name that is not the account's."""
    from .publish import sp_results
    login_id = str(login_id or "").strip()
    if not login_id.isdigit():
        raise Refused(f"--login-id must be SocialPilot's numeric loginId, not {login_id!r}")
    accounts = [a for v in sp_results(result_text) if isinstance(v, dict)
                for a in (v.get("accounts") or []) if isinstance(a, dict)]
    if not accounts:
        raise Refused("no AccountList result in the file: save the AccountList result in it exactly as it came back")
    page_name = page.get("name") or page.get("slug")
    found = matches(page, accounts)
    if not found:
        raise Refused(f"NO MATCH: connect {page_name} in SocialPilot (instructions on the page), then press Try again")
    if len(found) > 1:
        raise Refused(f"SEVERAL MATCHES: {len(found)} SocialPilot accounts match {page_name} "
                      f"({', '.join(str(a.get('loginId')) for a in found)}): disconnect the extra one in SocialPilot, then press Try again")
    a = found[0]
    if str(a.get("loginId")) in denied:
        raise Refused(f"DENIED: the SocialPilot account that matches {page_name} may never be used by this product: "
                      f"connect the page's own Facebook page in SocialPilot, then press Try again")
    if str(a.get("loginId")) in used:
        raise Refused(f"IN USE: that SocialPilot account is already linked to another page: connect {page_name}'s own "
                      "Facebook page in SocialPilot, then press Try again")
    if str(a.get("loginId")) != login_id:
        raise Refused(f"the account matching {page_name} is {a.get('loginId')}, not {login_id}")
    if _key(name) != _key(_account_name(a)):
        raise Refused(f"--name must be the account's name as SocialPilot lists it ({_account_name(a)!r})")
    if a.get("isReconnect") or a.get("isLocked"):
        raise Refused(f"the SocialPilot account of {page_name} {'needs reconnecting' if a.get('isReconnect') else 'is locked'}: "
                      "reconnect it in SocialPilot, then press Try again")
    return {"socialpilot_login_id": login_id, "socialpilot_name": _account_name(a)}


def link(slug: str, login_id: str, name: str, accounts_file: Path, dash=None) -> dict:
    from . import studio as S
    b = brief(slug)
    used = set(b.get("used_login_ids") or []) | {
        str(m.get("socialpilot_login_id")) for s, m in S.studio_meta().items() if s != slug and m.get("socialpilot_login_id")}
    try:
        text = Path(accounts_file).read_text()
    except OSError as e:
        raise Refused(f"cannot read {accounts_file}: {e}") from None
    body = check_link({**b["page"], "slug": slug}, login_id, name, text, denied_login_ids(), used)
    dash = dash or _dash()
    got = _result(slug, body, dash)
    step(slug, "link", True, f"linked to the SocialPilot account {body['socialpilot_name']}", dash)
    return got


# ------------------------------------------------------------------ sources
def limits(cfg: dict | None = None) -> dict:
    """The page's source budget: the defaults, or lower ones from config (never higher)."""
    own = ((cfg or {}).get("onboard") or {}).get("source_limits") or {}
    return {k: min(v, int(own.get(k, v))) for k, v in SOURCE_LIMITS.items()}


def _active(entry: dict, what: str, now: float) -> None:
    raw = str(entry.get("last_post") or "")
    try:
        t = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        raise Refused(f"{what}: last_post must be the date of its latest post (YYYY-MM-DD), not {raw!r}") from None
    if t > now + 86400:
        raise Refused(f"{what}: last_post {raw} is in the future")
    if now - t > ACTIVE_DAYS * 86400:
        raise Refused(f"{what}: its latest post is from {raw}, over {ACTIVE_DAYS} days ago: a source must be active this month")


def check_sources(data: dict, cfg: dict | None = None, now: float | None = None) -> dict:
    """The researched sources as the dashboard stores them ({sources: [{id, url, type, label, active}],
    pipeline: {tier_one, officials}}), or Refused with every problem at once."""
    now = now or time.time()
    lim, problems = limits(cfg), []
    if not isinstance(data, dict):
        raise Refused("sources.json must be an object with x, subreddits, web_searches and websites lists")
    extra = set(data) - set(SOURCE_LIMITS)
    if extra:
        problems.append(f"unknown keys {sorted(extra)} (only {', '.join(SOURCE_LIMITS)})")
    lists = {k: data.get(k) or [] for k in SOURCE_LIMITS}
    for k, xs in lists.items():
        if not isinstance(xs, list) or not all(isinstance(x, dict) for x in xs):
            problems.append(f"{k} must be a list of objects")
            lists[k] = []
        elif len(xs) > lim[k]:
            problems.append(f"{len(xs)} {k}: the page's budget allows at most {lim[k]} (keep the most active)")
    if not (lists["x"] or lists["subreddits"] or lists["websites"]):
        problems.append("no X account, subreddit or website: the hourly collection would have nothing to read")
    out, seen, tier, officials = [], set(), [], []

    def add(url: str, typ: str, label: str, what: str) -> None:
        if url.lower() in seen:
            problems.append(f"{what} is listed twice")
            return
        seen.add(url.lower())
        out.append({"id": secrets.token_hex(3), "url": url, "type": typ, "label": label, "active": True,
                    "added_at": int(now * 1000), "added_by": "onboarding"})

    def checked(fn, *a) -> bool:
        try:
            fn(*a)
            return True
        except Refused as e:
            problems.append(str(e))
            return False

    for x in lists["x"]:
        h = str(x.get("handle") or "").strip().lstrip("@").lower()
        role = x.get("role") or "other"
        if not re.fullmatch(r"[a-z0-9_]{1,15}", h):
            problems.append(f"X handle {x.get('handle')!r} is not a handle (letters, digits, _; at most 15)")
            continue
        if role not in X_ROLES:
            problems.append(f"@{h}: role must be one of {', '.join(X_ROLES)}")
            continue
        if checked(_active, x, f"@{h}", now):
            add(f"https://x.com/{h}", "social", {"reporter": "reporter", "official": "official"}.get(role, "X"), f"@{h}")
            (tier if role == "reporter" else officials if role in ("official", "outlet") else []).append(h)
    for s in lists["subreddits"]:
        n = str(s.get("name") or "").strip().lower().removeprefix("/").removeprefix("r/")
        if not re.fullmatch(r"[a-z0-9_]{2,21}", n):
            problems.append(f"subreddit {s.get('name')!r} is not a subreddit name")
            continue
        if checked(_active, s, f"r/{n}", now):
            add(f"https://www.reddit.com/r/{n}/", "reddit", "Reddit", f"r/{n}")
    for q in lists["web_searches"]:
        text = " ".join(str(q.get("query") or "").split())
        if not 3 <= len(text) <= 120 or text.lower().startswith("site:"):
            problems.append(f"web search {q.get('query')!r}: 3-120 characters of search words (a website goes under websites)")
            continue
        add(text, "search", "web search", f"the search {text!r}")
    for w in lists["websites"]:
        url = str(w.get("url") or "").strip()
        parts = urlsplit(url)
        host = parts.netloc.lower().removeprefix("www.")
        if parts.scheme not in ("http", "https") or "." not in host:
            problems.append(f"website {url!r} is not a web address (https://...)")
            continue
        if any(host == d or host.endswith("." + d) for d in NOT_WEBSITES):
            problems.append(f"{url}: X accounts and subreddits go in their own lists; other social sites are not collected")
            continue
        if checked(_active, w, url, now):
            add(url, "website", "website", url)
    if problems:
        raise Refused("sources not saved:\n  " + "\n  ".join(problems))
    return {"sources": out, "pipeline": {"tier_one": tier, "officials": officials}}


def save_sources(slug: str, file: Path, cfg: dict, dash=None) -> dict:
    brief(slug)
    try:
        data = json.loads(Path(file).read_text())
    except (OSError, ValueError) as e:
        raise Refused(f"cannot read {file} as JSON: {e}") from None
    body = check_sources(data, cfg)
    dash = dash or _dash()
    got = _result(slug, body, dash)
    n = {t: sum(1 for s in body["sources"] if s["type"] == t) for t in ("social", "reddit", "search", "website")}
    step(slug, "sources", True, f"{n['social']} X accounts, {n['reddit']} subreddits, {n['search']} web searches, "
                                f"{n['website']} websites", dash)
    return got


# ------------------------------------------------------------------ playbook
def _card_names() -> set[str]:
    from .layouts import GENERIC
    names = set(GENERIC)
    try:
        from .cards import TEMPLATES
        names |= {k[-1] if isinstance(k, tuple) else k for k in TEMPLATES}
    except Exception:          # (the card module needs the image packages; the generic layouts are enough)
        pass
    return names


def check_playbook(pb, slug: str) -> dict:
    """The playbook as the engine reads it, or Refused with every problem at once."""
    if not isinstance(pb, dict):
        raise Refused("playbook.yaml must be a mapping of the playbook keys (see the brief's playbook_keys)")
    problems = []
    for k in sorted(set(pb) - set(PLAYBOOK_TYPES)):
        problems.append(f"unknown key {k!r} (the engine reads only: {', '.join(PLAYBOOK_TYPES)})")
    for k, typ in PLAYBOOK_TYPES.items():
        if k in pb and not isinstance(pb[k], typ):
            problems.append(f"{k} must be a {typ.__name__}")
    for k in PLAYBOOK_REQUIRED:
        v = pb.get(k)
        if not v or (isinstance(v, str) and not v.strip()):
            problems.append(f"{k} is required")
    if pb.get("page") not in (None, slug):
        problems.append(f"page is {pb['page']!r}: it must be this page's slug ({slug}) or left out")
    for k in ("caption_rules", "headline_rules", "template_rules", "examples", "logo_words"):
        if isinstance(pb.get(k), list) and not all(isinstance(x, str) and x.strip() for x in pb[k]):
            problems.append(f"{k} must be a list of texts")

    def regexes(xs, where):
        if not isinstance(xs, list):
            problems.append(f"{where} must be a list of patterns")
            return
        for x in xs:
            try:
                re.compile(str(x))
            except re.error as e:
                problems.append(f"{where}: {x!r} is not a valid pattern ({e})")
    pf = pb.get("prefilter") if isinstance(pb.get("prefilter"), dict) else {}
    for k in set(pf) - {"skip", "boost", "trade_only_sources"}:
        problems.append(f"prefilter.{k}: only skip, boost and trade_only_sources")
    for k in ("skip", "boost"):
        if k in pf:
            regexes(pf[k], f"prefilter.{k}")
    lint = pb.get("lint") if isinstance(pb.get("lint"), dict) else {}
    for k in set(lint) - {"hashtags", "closing_question", "caps_hook"}:
        problems.append(f"lint.{k}: only hashtags, closing_question and caps_hook")
    ht = lint.get("hashtags")
    if ht is not None:
        if not isinstance(ht, dict) or set(ht) - {"min", "max", "required"}:
            problems.append("lint.hashtags must be {min, max, required}")
        else:
            lo, hi = ht.get("min", 0), ht.get("max", 30)
            if not (isinstance(lo, int) and isinstance(hi, int) and 0 <= lo <= hi <= 30):
                problems.append("lint.hashtags: min and max are whole numbers, 0 <= min <= max <= 30")
            if ht.get("required") and not re.fullmatch(r"#\w+", str(ht["required"])):
                problems.append("lint.hashtags.required must be one hashtag, e.g. '#DemoPage'")
    for k in ("closing_question", "caps_hook"):
        if k in lint and not isinstance(lint[k], bool):
            problems.append(f"lint.{k} must be true or false")
    for i, kd in enumerate(pb.get("kinds") or [] if isinstance(pb.get("kinds"), list) else []):
        if not isinstance(kd, dict) or not re.fullmatch(r"[a-z0-9_]{2,40}", str(kd.get("id") or "")):
            problems.append(f"kinds[{i}] needs an id (lowercase letters, digits, _)")
            continue
        regexes(kd.get("match") or [], f"kinds[{i}].match")
    card = pb.get("card") if isinstance(pb.get("card"), dict) else {}
    for k in set(card) - {"templates", "default_template", "art"}:
        problems.append(f"card.{k}: only templates, default_template and art")
    known = _card_names()
    tpls = card.get("templates") or []
    if not isinstance(tpls, list) or [t for t in tpls if t not in known]:
        problems.append(f"card.templates: only layouts the renderer draws ({', '.join(sorted(known))}); the page's own "
                        "templates come from its templates document (the Templates screen)")
    if card.get("default_template") and card["default_template"] not in (tpls or known):
        problems.append("card.default_template must be one of card.templates")
    if "art" in card and not (isinstance(card["art"], dict) and all(isinstance(v, str) for v in card["art"].values())):
        problems.append("card.art must map a template to a description of its artwork")
    for u in pb.get("style_refs") or [] if isinstance(pb.get("style_refs"), list) else []:
        if not str(u).startswith("https://"):
            problems.append(f"style_refs: {str(u)[:60]!r} is not an https:// image address")
    if problems:
        raise Refused("playbook not saved:\n  " + "\n  ".join(problems))
    return pb


def save_playbook(slug: str, file: Path, dash=None) -> dict:
    brief(slug)
    try:
        pb = yaml.safe_load(Path(file).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise Refused(f"cannot read {file} as YAML: {e}") from None
    pb = check_playbook(pb, slug)
    dash = dash or _dash()
    got = _result(slug, {"playbook": pb}, dash)
    step(slug, "playbook", True, f"playbook written ({len(pb.get('caption_rules') or [])} caption rules, "
                                 f"{len(pb.get('examples') or [])} examples)", dash)
    return got


# ------------------------------------------------------------------ templates, first run, finish
def queue_templates(slug: str, dash=None) -> dict:
    brief(slug)
    dash = dash or _dash()
    got = _result(slug, {"templates": True}, dash)
    step(slug, "templates", True, "the templates document is being turned into templates: approve them on the Templates screen", dash)
    return got


class _OnePage:
    """The dashboard as the first run sees it: only this page's editor links and regenerations (the
    hourly routine makes the other pages' ones)."""

    def __init__(self, dash, slug: str):
        self._d, self._slug = dash, slug

    def __getattr__(self, k):
        return getattr(self._d, k)

    def link_requests(self) -> list[dict]:
        return [x for x in self._d.link_requests() if x.get("page") == self._slug]

    def regen_requests(self) -> list[dict]:
        return [x for x in self._d.regen_requests() if x.get("page") == self._slug]


def first_run(slug: str, cfg: dict, dash=None) -> None:
    """The hourly flow for this page only (ROUTINE.md from step 1), so its first drafts come within
    the hour. On a fresh database, never the saved state: the hourly routine owns that, and may be
    running now. Its stories are collected again by the next hourly run."""
    import argparse

    from . import cli
    from . import studio as S
    from .config import load_pages, resolve, save_pages
    from .dashboard import RESTORED
    from .routine import load_playbook
    brief(slug)
    done = _done_steps(slug)
    missing = [s for s in ("link", "sources", "playbook") if not done.get(s)]
    if missing:
        raise Refused(f"first-run needs the {', '.join(missing)} step(s) done first (ONBOARD.md)")
    dash = dash or _dash()
    S.sync(dash, quiet=True)                    # the sources and playbook just saved
    page = next((p for p in load_pages() if p.slug == slug), None)
    if page is None:
        raise Refused(f"{slug} is not in the synced pages: the dashboard did not send it (report it)")
    if not load_playbook(slug):
        raise Refused(f"{slug} has no playbook on this machine after the sync: run `onboard playbook` again")
    page.hourly = True                          # this run only: the page is not live yet
    from .config import PRIVATE
    save_pages([page], PRIVATE / "pages.yaml")  # (the next `studio sync` writes every page back)
    # the saved state is never pushed from here: without the pull's marker `routine state --action push` refuses
    resolve(cfg, cfg["db_path"]).with_name(RESTORED).unlink(missing_ok=True)
    args = argparse.Namespace(page=slug, fresh=True, no_collect=False, cmd="routine", step="prepare")
    from . import routine as R
    real = cli._dashboard
    cli._dashboard = lambda: _OnePage(dash, slug)
    try:
        cli._routine_prepare(args, cfg, R)
    finally:
        cli._dashboard = real
    run = R.latest_run_dir()
    n = len(list(run.glob("*/brief.json"))) if run else 0
    # reported once the briefs are there (a page paused meanwhile stops the session here, before any upload)
    step(slug, "first_run", True, f"{n} stories picked for the first posts" if n else
         "no story to post this hour: the hourly routine makes the first posts", dash)


def done(slug: str, dash=None) -> dict:
    brief(slug)
    have = _done_steps(slug)
    missing = [s for s in NEEDED if not have.get(s)]
    if missing:
        raise Refused(f"not done yet: {', '.join(missing)} (ONBOARD.md)")
    dash = dash or _dash()
    return _call(dash, "done", {"id": brief(slug)["job"], "page": slug})


def fail(slug: str, reason: str, dash=None) -> dict:
    reason = " ".join(str(reason or "").split())
    if len(reason) < 10:
        raise Refused("give the tester a reason they can act on (what to do, then press Try again)")
    b = brief(slug)
    have = _done_steps(slug)
    at = next((s for s in STEPS[1:] if s not in have or not have[s]), "done")
    return _call(dash or _dash(), "fail", {"id": b["job"], "page": slug, "reason": reason[:400], "step": at})


# ------------------------------------------------------------------ command line
def run(args, cfg) -> None:
    slug, what = args.id, args.what
    try:
        if args.step == "jobs":
            pull_job(cfg=cfg)
            return
        if not slug:
            sys.exit(f"onboard {args.step} needs the page's slug")
        if args.step == "step":
            if what not in STEPS:
                sys.exit(f"onboard step {slug} <step>: one of {', '.join(STEPS)}")
            if args.ok == args.fail:
                sys.exit("say --ok or --fail")
            step(slug, what, args.ok, args.detail or "")
            print(f"{slug}: step {what} {'ok' if args.ok else 'failed'}")
        elif args.step == "link":
            if not (args.login_id and args.name and args.accounts):
                sys.exit("onboard link needs --login-id, --name and --accounts <the saved AccountList result>")
            link(slug, args.login_id, args.name, Path(args.accounts))
            print(f"{slug}: linked to SocialPilot account {args.login_id}")
        elif args.step == "sources":
            save_sources(slug, Path(what or folder(slug) / "sources.json"), cfg)
            print(f"{slug}: sources saved")
        elif args.step == "playbook":
            save_playbook(slug, Path(what or folder(slug) / "playbook.yaml"))
            print(f"{slug}: playbook saved")
        elif args.step == "templates":
            got = queue_templates(slug)
            print(f"{slug}: templates conversion queued" + (f" (Studio job {got['studio_job']})" if got.get("studio_job") else ""))
        elif args.step == "first-run":
            first_run(slug, cfg)
        elif args.step == "done":
            got = done(slug)
            print(f"{slug}: {got.get('status') or 'live'}")
        elif args.step == "fail":
            fail(slug, what or "")
            print(f"{slug}: failed; the tester sees the reason and Try again")
    except Refused as e:
        sys.exit(f"REFUSED: {e}")
    except Stopped as e:
        sys.exit(f"STOPPED: the dashboard stopped this onboarding ({e}). Do nothing more for this page.")
