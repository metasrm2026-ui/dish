"""Command line: bore <command>. Run `bore -h` for the list."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import Page, load_config, load_pages, resolve, save_pages, secret


def fk_slug_from_url(url: str) -> str:
    parts = urlsplit(url)
    if "profile.php" in parts.path:
        return parse_qs(parts.query).get("id", [""])[0]
    seg = [p for p in parts.path.split("/") if p and p not in ("pg", "pages", "people")]
    return seg[-1] if seg and seg[-1].isdigit() else (seg[0] if seg else "")


def _store(cfg):
    from .store import Store
    return Store(resolve(cfg, cfg["db_path"]))


def _apify(cfg, store=None):
    from .apify import Apify
    a = cfg["apify"]
    ap = Apify(secret("APIFY_TOKEN"), a["base_url"], a["budget_guard_pct"], a["poll_seconds"],
               cfg.get("budget", {}).get("monthly_usd", 0.0))
    if store is not None:
        ap.on_cost = store.record_spend
    return ap


def _pages(args, cfg) -> list[Page]:
    pages = load_pages()
    if not pages:
        sys.exit("config/pages.yaml has no pages. Run `bore import-workbook <file.xlsx>` first.")
    if getattr(args, "page", None):
        pages = [p for p in pages if p.slug == args.page]
        if not pages:
            sys.exit(f"unknown page {args.page}")
    return pages


# ------------------------------------------------------------------ commands
def cmd_import_workbook(args, cfg):
    from .workbook import parse_workbook
    parsed = parse_workbook(args.xlsx, cfg["default_timezone"])
    existing = {p.slug: p for p in load_pages()}
    fb = {}
    if args.facebook_urls:
        for line in Path(args.facebook_urls).read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fb[k.strip()] = v.strip()
    merged = []
    for p in parsed:
        old = existing.get(p.slug)
        if old:  # keep hand edits: tier_one, officials, web_searches, proposals, fk_slug, timezone
            for k in ("tier_one", "officials", "web_searches", "subreddits_proposed", "fk_slug",
                      "timezone", "facebook_url"):
                if getattr(old, k):
                    setattr(p, k, getattr(old, k))
            p.subreddits = sorted(set(p.subreddits) | set(old.subreddits))
        p.facebook_url = fb.get(p.slug, fb.get(p.name, p.facebook_url))
        if p.facebook_url and not p.fk_slug:
            p.fk_slug = fk_slug_from_url(p.facebook_url)
        merged.append(p)
    save_pages(merged)
    for p in merged:
        print(f"{p.slug}: {len(p.x_handles)} X handles, {len(p.subreddits)} subreddits, "
              f"{len(p.websites)} websites, fb={p.fk_slug or '-'}, unresolved={len(p.unresolved)}")
        for u in p.unresolved:
            print(f"    unresolved {u}")
        if not p.subreddits:
            print("    Reddit column empty: add PROPOSED subreddits under subreddits_proposed")


def cmd_add_page(args, cfg):
    from .workbook import slugify
    pages = load_pages()
    slug = slugify(args.name) if args.name else f"page-{len(pages) + 1}"
    if any(p.facebook_url == args.facebook_url for p in pages):
        sys.exit("that Facebook URL is already configured")
    pages.append(Page(slug=slug, name=args.name or slug, facebook_url=args.facebook_url,
                      timezone=args.timezone or cfg["default_timezone"]))
    save_pages(pages)
    print(f"added {slug}. Next: `bore sources --page {slug} --league nfl --team Eagles`, and "
          f"`bore dna --page {slug} --file export.csv` for page DNA.")


def _assume_sources(p, messages, cfg, leagues=None, teams=None, origin=None):
    from .sources import assign, detect, load_catalog
    catalog = load_catalog()
    detected = detect(messages, catalog)
    for t in teams or []:                        # a named team is a strong signal on its own
        for lg, spec in catalog.items():
            if t.lower() in spec["keywords"] and (not leagues or lg in leagues):
                detected[lg]["teams"].insert(0, (t.lower(), 99))
    assign(p, detected, catalog, leagues)
    if origin:
        p.sources_origin = origin
    print(f"    sources: {p.sources_origin}")
    print(f"    {len(p.x_handles)} X handles ({len(p.tier_one)} tier-one reporters), "
          f"subreddits {p.subreddits}, {len(p.web_searches)} web searches")


def cmd_dna(args, cfg):
    from .dna import build_dna
    from .workbook import slugify
    all_pages = load_pages()
    targets = {p.slug for p in _pages(args, cfg)}
    for p in all_pages:
        if p.slug not in targets:
            continue
        out = resolve(cfg, "config/dna") / p.slug
        cached = None
        if args.source == "cached":
            cached = json.loads((out / "posts.json").read_text())
        token = None
        if args.source == "graph":
            token = secret(f"FB_PAGE_TOKEN_{p.slug.upper().replace('-', '_')}")
        elif args.source == "fanpagekarma":
            token = secret("FANPAGEKARMA_TOKEN")
        source = "file" if args.file else args.source
        dna = build_dna(p, cfg, out, source, file=args.file, token=token, posts=cached)
        meta = dna.get("page_meta") or {}
        if meta.get("name") and (p.slug.startswith("page-") or p.name == p.slug):
            p.name = meta["name"]
            new_slug = slugify(meta["name"])
            if new_slug and new_slug != p.slug and new_slug not in {q.slug for q in all_pages}:
                out.rename(resolve(cfg, "config/dna") / new_slug)
                p.slug = new_slug
        if meta.get("url") and "/share/" in p.facebook_url:
            p.facebook_url = meta["url"]
        print(f"{p.slug} ({p.name}): {dna.get('posts', 0)} posts -> config/dna/{p.slug}/dna.md")
        for d in dna.get("engagement_drivers", []):
            print(f"    {d}")
        if dna.get("posts") and (args.rebuild_sources or not p.x_handles):
            posts = json.loads((resolve(cfg, "config/dna") / p.slug / "posts.json").read_text())
            _assume_sources(p, [x["message"] for x in posts], cfg)
        save_pages(all_pages)


def cmd_sources(args, cfg):
    """Assume sources from a named league / team (works before any post history is available)."""
    all_pages = load_pages()
    targets = {p.slug for p in _pages(args, cfg)}
    leagues = [x.strip().lower() for x in args.league.split(",")] if args.league else None
    teams = [x.strip() for x in args.team.split(",")] if args.team else []
    for p in all_pages:
        if p.slug not in targets:
            continue
        if args.name:
            p.name = args.name
            if p.slug.startswith("page-"):
                from .workbook import slugify
                p.slug = slugify(args.name)
        msgs = [p.name] + teams * 5
        dna_posts = resolve(cfg, "config/dna") / p.slug / "posts.json"
        if dna_posts.exists():
            msgs += [x["message"] for x in json.loads(dna_posts.read_text())]
        print(p.slug)
        origin = None
        if not dna_posts.exists() and leagues:
            origin = ("assumed from league/team: " + ", ".join(leagues).upper()
                      + (f" / {', '.join(teams)}" if teams else ""))
        _assume_sources(p, msgs, cfg, leagues, teams, origin)
    save_pages(all_pages)


PUBLISH_RUN_HOURS = 3      # a publisher run's state older than this belongs to an earlier run (local runs keep work/)


def _pub_read(value: str, root: Path) -> str:
    """--queued / --result: the file the model saved a connector result to (- reads stdin)."""
    v = (value or "").strip()
    if v == "-":
        return sys.stdin.read()
    if v[:1] in "{[":
        return v
    p = Path(v)
    if not p.exists() and not p.is_absolute() and (root / v).exists():
        p = root / v
    if not p.exists():
        sys.exit(f"{v}: no such file (save the connector's result there first)")
    return p.read_text()


def _pub_json(path: Path) -> dict:
    try:
        v = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _pub_state(work: Path) -> dict:
    """This run's publisher state: its preflight, its CreatePost failures (the breaker), why it stopped."""
    st = _pub_json(work / ".publish-run.json")
    return st if time.time() - st.get("started_at", 0) < PUBLISH_RUN_HOURS * 3600 else {}


def _pub_save(work: Path, state: dict) -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / ".publish-run.json").write_text(json.dumps(state))


def _pub_claim(work: Path, draft_id: str) -> dict:
    """What this run claimed for a draft (token, slot, account, caption); {} if it did not claim it."""
    p = work / ".claims" / draft_id
    if not p.exists():
        return {}
    text = p.read_text().strip()
    try:
        c = json.loads(text)
    except ValueError:
        c = text
    return c if isinstance(c, dict) else {"claim": str(c)}


def _pub_utc(s: str) -> float:
    from datetime import datetime as _dt
    return _dt.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def _pub_iso(t: float) -> str:
    from datetime import datetime as _dt, timezone as _tz
    return _dt.fromtimestamp(t, _tz.utc).isoformat().replace("+00:00", "Z")


def _pub_unseen(work: Path, draft_id: str, reset: bool = False) -> int:
    """How often this run's QueuedPosts results did not show a post it created: the first miss is
    looked up again with a wider search, the second is recorded for a person."""
    p = work / ".unseen" / draft_id
    if reset:
        p.unlink(missing_ok=True)
        return 0
    n = (int(p.read_text() or 0) if p.exists() else 0) + 1
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(n))
    return n


def _pub_old_dashboard(resp) -> bool:
    """A 400 from a dashboard that does not take this status yet (it needs deploying), not a 400
    about the request itself (e.g. a needs_action with nothing for the person to do)."""
    try:
        return str((resp.json() or {}).get("error", "")).startswith("status must be")
    except ValueError:
        return False


def _pub_settle(dash, draft_id: str, status: str, error: str, **extra) -> None:
    try:
        dash.report(draft_id, status, error=error, **extra)
    except Exception as e:                       # best effort: the plan is still printed
        print(f"# {draft_id}: could not record '{status}' ({e})", file=sys.stderr)


def _pub_plan(dash, sp: dict, busy_args, now: float, settle: bool = True) -> dict:
    """The plan (PUBLISH.md step 3). With settle, what the code can decide is done here instead of
    being left to the session's summary: a draft for a page with no usable account fails with what
    to do, and an outdated original already in SocialPilot is flagged for a person to delete."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from .publish import BREAKER_MINUTES, NEVER_LOGIN_IDS, parse_busy, plan, split_waiting
    accounts, tz = sp.get("accounts", {}), sp.get("timezone", "Asia/Kolkata")
    rules = sp.get("scheduling", {})
    blocked = {str(b) for b in sp.get("blocked_login_ids", [])} | NEVER_LOGIN_IDS
    local = lambda t, fmt="%Y-%m-%d %H:%M": _dt.fromtimestamp(t, ZoneInfo(tz)).strftime(fmt)
    data = dash.queue()
    scheduled, in_progress = data.get("scheduled", []), data.get("in_progress", [])

    def account(x: dict) -> dict:       # config first, then the account an admin set for a newer page
        acc = dict(accounts.get(x["page"]) or {})
        lid = x.get("socialpilot_login_id")
        if not acc.get("loginId") and lid:
            acc = {"name": x.get("page_name") or x["page"], "loginId": int(lid) if str(lid).isdigit() else lid}
        return acc

    busy = {}
    for s_ in scheduled + in_progress:                       # incl. other runs' claims
        if s_.get("scheduled_for"):
            busy.setdefault(s_["page"], []).append(_pub_utc(s_["scheduled_for"]))
    # --busy names a page by its slug; its name or loginId works too (a name used to be ignored silently)
    key = lambda s: re.sub(r"[^a-z0-9]", "", str(s or "").lower())
    alias = {}
    for slug, a in accounts.items():
        alias.update({key(slug): slug, key(a.get("name")): slug, key(a.get("loginId")): slug})
    for x in data["queue"] + scheduled + in_progress:
        for k in (x["page"], x.get("page_name"), x.get("socialpilot_login_id")):
            alias.setdefault(key(k), x["page"])
    alias.pop("", None)
    ignored = []
    for page, times in parse_busy(busy_args, tz).items():
        if key(page) in alias:
            busy.setdefault(alias[key(page)], []).extend(times)
        else:
            ignored.append(page)
            print(f"# --busy {page}: no page of ours has that name, ignored (use the page slug: "
                  f"{', '.join(sorted(set(alias.values())))})", file=sys.stderr)
    # an original whose update is already queued or scheduled must not go out after it
    updated = {x.get("update_of"): x["id"] for x in data["queue"] + scheduled if x.get("update_of")}
    superseded = [{"id": x["id"], "page": x["page"], "update": updated[x["id"]]} for x in data["queue"] if x["id"] in updated]
    # a page with no usable account is never scheduled: its drafts fail for an editor (they used to sit
    # in Queued forever, with only a line in the session's log)
    queue, no_account = [], []
    for x in data["queue"]:
        if x["id"] in updated:
            continue
        acc = account(x)
        lid = str(acc.get("loginId") or "")
        if not lid and x.get("login_error"):
            print(f"# {x['id']}: could not read page {x['page']}'s SocialPilot account: tried again next run", file=sys.stderr)
            continue
        if not lid or lid in blocked:
            err = ("page has no SocialPilot account: set it in the admin panel, then retry" if not lid else
                   f"page has no SocialPilot account it may use (loginId {lid} is never posted to): "
                   "set the page's own account in the admin panel, then retry")
            no_account.append({"id": x["id"], "page": x["page"], "error": err})
            print(f"# {x['id']}: page {x['page']} has no usable SocialPilot account; set it in the admin panel", file=sys.stderr)
            continue
        queue.append({**x, "socialpilot_account": acc})
    sp_state = data.get("socialpilot") or {}
    queue, waiting = split_waiting(queue, sp_state)          # SocialPilot down: tried hourly
    aud = rules.get("audience_hours") or {}
    window = (aud["timezone"], int(aud["start"]), int(aud["end"])) if aud.get("timezone") else None
    slots = {x["id"]: x for x in plan(queue, busy, now=now, min_delay_minutes=rules.get("min_delay_minutes", 60),
                                     min_gap_minutes=rules.get("min_gap_minutes", 45), tz=tz,
                                     min_lead_minutes=rules.get("studio_min_lead_minutes", 60), window=window)}
    items = []
    for x in queue:
        s = slots[x["id"]]
        items.append({**x, "image_urls": x.get("image_urls") or [x.get("image_url")],   # all images, in order
                      "schedule_local": s["local"], "schedule_utc": s["iso_utc"],
                      # sent to SocialPilot before, or waited for it: look for it there before creating it
                      "check_existing": bool(x.get("holds") or x.get("ever_sent")),
                      "search_from": local(x.get("first_approved_at") or x.get("approved_at") or now, "%Y-%m-%d"),
                      **({"held_for_audience_hours": True} if s.get("held_for_audience_hours") else {})})
    late = [{"id": s_["id"], "page": s_["page"], "scheduled_for": s_["scheduled_for"], "update": updated[s_["id"]]}
            for s_ in scheduled if s_["id"] in updated]
    stale = [{"id": x["id"], "page": x["page"], "scheduled_for": x.get("scheduled_for"), "caption": x.get("caption"),
              "socialpilot_account": account(x)}
             for x in in_progress if now * 1000 - (x.get("claimed_at") or 0) > 30 * 60e3]
    confirm = []
    for s_ in scheduled:          # what the delivery check (step 5) looks them up with
        t = _pub_utc(s_["scheduled_for"]) if s_.get("scheduled_for") else None
        acc = {"loginId": int(s_["sp_login_id"]) if str(s_["sp_login_id"]).isdigit() else s_["sp_login_id"]} \
            if s_.get("sp_login_id") else account(s_)
        confirm.append({**s_, "socialpilot_account": acc,
                        **({"schedule_local": local(t), "day": local(t, "%Y-%m-%d")} if t else {})})
    out = {"timezone": tz, "to_schedule": items, "superseded": superseded, "interrupted": stale,
           "scheduled_but_superseded": late, "already_scheduled": confirm, "no_account": no_account}
    if ignored:
        out["busy_ignored"] = ignored
    if sp_state.get("down"):             # SocialPilot itself (not one page's account, not one lookup)
        out["socialpilot_down"] = {k: sp_state.get(k) for k in ("error", "since", "last_try_at") if sp_state.get(k)}
    if waiting:
        out["waiting_for_socialpilot"] = [{"id": x["id"], "page": x["page"], "tries": x.get("holds") or 0,
                                           "error": x.get("held_error") or ""} for x in waiting]
    # accounts whose drafts wait because that account kept failing, or whose page had a post fail in
    # the last hour in any run (result.js failing_pages: every approval starts its own run, so the
    # run's breaker alone let each one fail): one more failure there holds the draft at once
    # (cmd_publish), instead of failing it as the run's first
    since = now * 1000 - BREAKER_MINUTES * 60e3
    failed_on = {p: f for p, f in (sp_state.get("failing_pages") or {}).items() if isinstance(f, dict) and (f.get("at") or 0) >= since}
    held_on = {str(x["socialpilot_account"]["loginId"]): f"this page's posts were held at the last try ({x.get('held_error') or 'failed'})"
               for x in queue + waiting if x.get("holds") and x.get("held_scope") == "account"}
    out["_failing_accounts"] = {
        **{str(x["socialpilot_account"]["loginId"]): f"a post on this page failed {max(1, round((now * 1000 - failed_on[x['page']]['at']) / 60e3))} min ago"
           for x in queue + waiting if x["page"] in failed_on}, **held_on}
    # ...but not for the post whose failure it was (retried, or sent again from the Studio as a new
    # draft): that post failing again (a bad image) says nothing about the account. It used to be
    # held for its own failure, kept its page flagged and went to Failed 12 hours later as a broken account
    resent = {}
    for x in queue + waiting:
        if x.get("resent_from"):
            resent.setdefault(x["resent_from"], []).append(x["id"])
    out["_flagged_by"] = {str(x["socialpilot_account"]["loginId"]): [f["id"], *resent.get(f["id"], [])]
                          for x in queue + waiting if (f := failed_on.get(x["page"])) and f.get("id")
                          and str(x["socialpilot_account"]["loginId"]) not in held_on}
    if settle:
        for x in no_account:
            _pub_settle(dash, x["id"], "failed", x["error"])
        for x in late:
            when = local(_pub_utc(x["scheduled_for"])) if x.get("scheduled_for") else "its time"
            _pub_settle(dash, x["id"], "needs_action", f"delete it in SocialPilot: it is scheduled for {when} ({tz}) "
                                                       f"and its update {x['update']} replaces it", kind="superseded_original")
    return out


def cmd_publish(args, cfg):
    """Used by the SocialPilot publisher routine (PUBLISH.md). The code checks what the session did:
    `preflight` before the first claim, a breaker per SocialPilot account in `mark --status failed`, and
    the QueuedPosts entry of every post `mark --status scheduled` records. Only SocialPilot itself
    failing (preflight, an outage wording) stops the run and holds every page; one page's account or
    one draft's lookup holds only that draft, and the run goes on."""
    import shutil
    import yaml
    from .publish import (NEVER_LOGIN_IDS, breaker_trips, check_preflight, outage_error, own_message,
                          session_blocked, sp_entries, verify_queued)
    dash = _dashboard()
    if not dash:
        sys.exit("DASHBOARD_URL / ROUTINE_SECRET not set")
    root = Path(cfg["_root"])
    sp = yaml.safe_load((root / "config" / "socialpilot.yaml").read_text()) or {}
    accounts, tz = sp.get("accounts", {}), sp.get("timezone", "Asia/Kolkata")
    work = root / "work"
    claims, plans = work / ".claims", work / ".plan"       # this run's claim tokens, and what each planned item is
    now = time.time()

    if args.action in ("queue", "plan"):
        # `queue` only shows the plan: a look at the queue (e.g. from a diagnostics session) changes
        # nothing. `plan` is the routine's step 3: it also records what the code settles, and keeps
        # each item for the claims and checks that follow
        settle = args.action == "plan"
        out = _pub_plan(dash, sp, args.busy, now, settle=settle)
        failing, flagged_by = out.pop("_failing_accounts", {}), out.pop("_flagged_by", {})
        if settle:
            shutil.rmtree(plans, ignore_errors=True)
            plans.mkdir(parents=True, exist_ok=True)
            for x in out["to_schedule"]:
                (plans / x["id"]).write_text(json.dumps({"caption": x.get("caption"), "login_id": x["socialpilot_account"]["loginId"],
                                                         "schedule_utc": x["schedule_utc"], "schedule_local": x["schedule_local"],
                                                         "prior_post_id": x.get("prior_post_id")}))
            for x in out["interrupted"]:
                (plans / x["id"]).write_text(json.dumps({"caption": x.get("caption"), "scheduled_for": x.get("scheduled_for"),
                                                         "login_id": x["socialpilot_account"].get("loginId")}))
            state = _pub_state(work)
            if state and failing:            # not an account a post was scheduled on in this run
                new = {k: v for k, v in failing.items() if k not in state.get("last_ok", {}) and k not in state.get("flagged", {})}
                state["flagged"] = {**state.get("flagged", {}), **new}
                # whose failure a flag is (an account SocialPilot itself flags has none: it holds every draft)
                state["flagged_by"] = {**state.get("flagged_by", {}), **{k: flagged_by[k] for k in new if k in flagged_by}}
                _pub_save(work, state)
        print(json.dumps(out, indent=1, ensure_ascii=False))
        waiting = out.get("waiting_for_socialpilot") or []
        print(f"# {len(out['to_schedule'])} approved draft(s) to schedule"
              + (f"; {len(waiting)} waiting for SocialPilot (tried again next hour)" if waiting else ""), file=sys.stderr)
        return

    if args.action == "preflight":
        # a read-only call before the first CreatePost: if it fails, whatever the wording, nothing is
        # claimed and the run's drafts wait (held), instead of each one failing on its own
        if args.error:
            error, notes, troubled = " ".join(args.error.split())[:300], [], {}
        elif args.result:
            try:
                error, notes, troubled = check_preflight("\n".join(_pub_read(r, root) for r in args.result), tz)
            except ValueError as e:
                sys.exit(str(e))
        else:
            sys.exit("publish preflight needs --result <file> (the AccountList result, and UserInfo's) "
                     "or --error \"<the call's error>\"")
        # a second preflight in the same run keeps what the run learned: its failures (the breaker),
        # and why it stopped (a HELD or STOP is not undone by checking SocialPilot again)
        state = _pub_state(work)
        if state.get("stopped"):
            print(f"HELD the run: it already stopped scheduling ({state['stopped']}). Do not schedule or claim anything "
                  f"else in this run: go to step {state.get('next_step', 5)}.")
            sys.exit(4)
        state = {"started_at": now, "failures": [], "last_ok": {}, **state,
                 "preflight": {"ok": not error, "at": now, **({"error": error} if error else {})}}
        # accounts SocialPilot flags (needs reconnecting, locked): one failure there holds that page's draft
        state["flagged"] = {**state.get("flagged", {}), **{k: v for k, v in troubled.items() if k not in state["last_ok"]}}
        # flagged by SocialPilot itself: no draft there is let off for being the one that failed before
        state["flagged_by"] = {k: v for k, v in state.get("flagged_by", {}).items() if k not in troubled}
        if error:
            state.update(stopped=f"SocialPilot unavailable: {error}", next_step=6)
        _pub_save(work, state)
        for n in notes:
            print(f"NOTE {n}")
        if troubled and not error:
            print("NOTE schedule that page's drafts as usual: if one fails, it waits (held) and the other pages go on")
        if not error:
            print("OK SocialPilot answers: go on with step 2.")
            return
        held = 0
        for x in _pub_plan(dash, sp, [], now, settle=False)["to_schedule"]:
            try:
                res = dash.report(x["id"], "held", error=error)
            except Exception as e:                  # another run has it, or a dashboard without holds
                print(f"# {x['id']}: not held ({e})", file=sys.stderr)
                continue
            held += res.get("status") == "held"
            print(f"{'HELD' if res.get('status') == 'held' else 'FAILED'} {x['id']}: {res.get('error') or error}")
        print(f"HELD the run: SocialPilot is unavailable ({error}). {held} approved draft(s) wait and are tried again in about "
              "an hour. Do not schedule or look up anything in this run: go to step 6.")
        sys.exit(4)

    if not args.id or not args.status:
        sys.exit("publish mark needs a draft id and --status")
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    state, claim = _pub_state(work), _pub_claim(work, args.id)
    status, error = args.status, " ".join((args.error or "").split())
    if status == "needs_action" and not error:
        sys.exit('publish mark --status needs_action needs --error "<what a person must do, in SocialPilot or on Facebook>"')
    detail = {k: v for k, v in (("post_id", args.post_id), ("url", args.url), ("error", error),
                                ("scheduled_for", args.scheduled_for)) if v}
    item, tripped, stop, scope, why, confirmed = _pub_json(plans / args.id), [], "", "", "", False
    login = str(claim.get("login_id") or item.get("login_id") or "")
    ok_on = state.get("last_ok") or {}
    local = lambda t, fmt="%Y-%m-%d %H:%M": _dt.fromtimestamp(t, ZoneInfo(tz)).strftime(fmt)

    def unconfirmed() -> str:
        # CreatePost said it worked, but SocialPilot's queue does not show the post: it may well go
        # out, so it is never failed (a retry would post it twice). It keeps its slot (spacing; the
        # delivery check looks it up by its post id) and a person checks it in SocialPilot.
        slot = claim.get("slot")
        detail.pop("error", None)
        detail.update({"copies": 0, **({"sp_login_id": claim["login_id"]} if claim.get("login_id") else {}),
                       **({"scheduled_for": _pub_iso(slot)} if slot else {})})
        detail["needs_action"] = (f"SocialPilot's queue did not show it after CreatePost: check in SocialPilot that it is "
                                  f"scheduled once, on account {claim.get('login_id') or '?'}, for "
                                  + (f"{local(slot)} ({tz})" if slot else "its planned time"))
        return detail["needs_action"]

    if status == "publishing":
        if state.get("stopped"):
            print(f"HELD {args.id}: this run stopped scheduling ({state['stopped']}). Do not schedule it: go to step 5.")
            sys.exit(4)
        if not (state.get("preflight") or {}).get("ok"):
            sys.exit("run `python -m bore.cli publish preflight` first (step 1): SocialPilot must answer before a draft is claimed")
        if not item.get("schedule_utc"):
            sys.exit(f"{args.id} is not in this run's to_schedule list: run `python -m bore.cli publish plan` "
                     "and claim only its items")
        slot = _pub_utc(item["schedule_utc"])
        try:
            asked = _pub_utc(args.scheduled_for or "")
        except ValueError:
            asked = None
        if asked is None or abs(asked - slot) > 60:
            sys.exit(f'use this item\'s schedule_utc from the plan: --scheduled-for "{item["schedule_utc"]}"')
        if slot - now < 55 * 60:          # an old plan: never take a slot sooner than about an hour from now
            print(f"SKIP {args.id}: its planned time {item['schedule_local']} is less than an hour away now. Do NOT "
                  "schedule it: it gets a new time when you run publish plan again.")
            sys.exit(3)
        detail["scheduled_for"] = item["schedule_utc"]
        _pub_unseen(work, args.id, reset=True)

    elif status == "scheduled":
        if not args.queued:
            sys.exit("pass --queued <file>: the QueuedPosts result that shows this post (PUBLISH.md step 4.4)")
        expect = {"caption": claim.get("caption") or item.get("caption"),
                  "login_id": claim.get("login_id") or item.get("login_id"),
                  "slot": claim.get("slot"), "claimed_at": claim.get("at")}
        if not expect["caption"]:
            sys.exit(f"{args.id} is not in this run's plan: run `python -m bore.cli publish plan` first")
        allowed = {str(a["loginId"]) for a in accounts.values() if a.get("loginId")} \
            | ({str(expect["login_id"])} if expect["login_id"] else set())
        blocked = {str(b) for b in sp.get("blocked_login_ids", [])} | NEVER_LOGIN_IDS
        v = verify_queued(sp_entries(_pub_read(args.queued, root)), expect, tz, allowed, blocked)
        e = v["entry"]
        if e is None:
            when = claim.get("slot") or (_pub_utc(item["schedule_utc"]) if item.get("schedule_utc") else
                                         _pub_utc(item["scheduled_for"]) if item.get("scheduled_for") else None)
            if _pub_unseen(work, args.id) < 2:        # SocialPilot's word search can miss: look once more, wider
                sys.exit(f"NOT FOUND {args.id}: the QueuedPosts result has no post with this caption. Call QueuedPosts again "
                         f"with account = {expect['login_id']}, startDate = endDate = {local(when, '%Y-%m-%d') if when else 'its day'} "
                         "and no q (this command finds the caption itself), save the whole result to work/queued.json and "
                         "run this command again.")
            if not claim.get("claim"):                # another run's claim (interrupted): it may never have been made
                sys.exit(f"NOT FOUND {args.id}: SocialPilot's queue does not show it. If DeliveredPosts does not show it "
                         "either, mark it --status failed --error \"publisher run interrupted\".")
            stop = unconfirmed()
        else:
            confirmed = True
            # SocialPilot's own time and account are recorded: later plans space posts around the real times
            detail.update({"sp_post_id": e.get("postId"), "sp_login_id": e.get("loginId"), "copies": v["copies"]})
            when = _pub_iso(v["sp_time"]) if v["sp_time"] else (args.scheduled_for or (_pub_iso(claim["slot"]) if claim.get("slot") else ""))
            detail.pop("scheduled_for", None)
            if when:
                detail["scheduled_for"] = when
            if v["problems"]:
                detail["needs_action"] = "; ".join(v["problems"])[:300]
            if v["wrong_account"]:
                status = "failed"
                detail["error"] = "scheduled on the wrong SocialPilot account: delete it in SocialPilot, then retry"
            if v["stop"]:
                stop = detail["needs_action"]
        old = str(claim.get("prior_post_id") or item.get("prior_post_id") or "")
        if old and old not in (str((e or {}).get("postId") or ""), str(args.post_id or "")):
            # created again over an old copy SocialPilot errored or dropped (PUBLISH.md 4.2): that copy
            # never goes out by itself, but rescheduled by hand there (as people do after a lock) it
            # would post the story twice
            detail["needs_action"] = "; ".join(x for x in (detail.get("needs_action"), (
                f"its old copy (SocialPilot post {old}) may still be in SocialPilot's errored or unscheduled posts: "
                "delete it there, never reschedule it (the post was scheduled again as a new copy)")) if x)[:300]

    elif status == "failed":
        if claim.get("claim") and error.lower().startswith("created but not"):
            status, stop = "scheduled", unconfirmed()          # CreatePost worked: never failed (see unconfirmed)
        elif outage_error(error):            # SocialPilot itself refused everything: every page waits
            status = "held"
        elif session_blocked(error):         # this session may not call it; SocialPilot is fine
            status, scope = "held", "run"
        elif claim and not own_message(error):              # a CreatePost this run made has failed
            earlier = breaker_trips(state.get("failures", []), login, ok_on.get(login, 0))
            flag = (state.get("flagged") or {}).get(login, "") if login not in ok_on else ""
            if args.id in (state.get("flagged_by") or {}).get(login, []):
                flag = ""                # the page's earlier failure was this post's own (_pub_plan)
            if earlier or flag:          # this page's account again (or SocialPilot flags it): only its drafts wait
                status, scope = "held", "account"
                tripped = [f for f in earlier if not f.get("held")]
                why = ("its page's SocialPilot account failed before in this run, with no post scheduled on it since"
                       if earlier else flag)
            else:                        # the page's first: the dashboard keeps it for the next hour's runs
                detail["breaker"] = True
            if state:
                state.setdefault("failures", []).append({"id": args.id, "error": error[:300], "claim": claim.get("claim"),
                                                         "at": now, "login_id": login, "held": status == "held"})
                _pub_save(work, state)
    elif status == "held" and not outage_error(re.sub(r"^lookup failed:?\s*", "", error, flags=re.I)):
        scope = "draft"                      # a lookup for this draft failed (PUBLISH.md 4.2): only it waits
    if status == "held":
        detail.update({**({"claim": claim["claim"]} if claim.get("claim") else {}), **({"scope": scope} if scope else {})})

    try:
        try:
            res = dash.report(args.id, status, **detail)
        except Exception as e:                               # an older dashboard
            resp = getattr(e, "response", None)
            if resp is None or resp.status_code != 400 or not _pub_old_dashboard(resp):
                raise
            if status in ("missing", "needs_action"):        # it cannot take these yet: leave it
                print(f"LEAVE {args.id}: the dashboard does not take '{status}' yet (it needs deploying): leave it")
                return
            if status != "held":
                raise
            status = "failed"                                # no holds: fail it as before
            res = dash.report(args.id, status, **{k: v for k, v in detail.items() if k not in ("claim", "scope")})
        print(json.dumps(res))
        if args.status == "publishing" and res.get("claim"):
            claims.mkdir(parents=True, exist_ok=True)
            (claims / args.id).write_text(json.dumps({"claim": str(res["claim"]), "at": now, "slot": _pub_utc(item["schedule_utc"]),
                                                      "login_id": item.get("login_id"), "caption": item.get("caption"),
                                                      "prior_post_id": item.get("prior_post_id")}))
        if confirmed and res.get("status") == "scheduled" and state:   # a post went in on this account: its breaker resets
            state.setdefault("last_ok", {})[login] = now
            _pub_save(work, state)
        held = res.get("status") == "held"
        if held and scope in ("account", "draft"):           # only this draft (or this page) waits: the run goes on
            for f in tripped:                                # the page's earlier failure was the same: it waits too
                try:
                    r = dash.report(f["id"], "held", error=f["error"], claim=f["claim"], scope="account")
                    print(f"  {f['id']} (failed earlier in this run): "
                          + ("waits as well" if r.get("status") == "held" else f"moved to Failed: {r.get('error')}"))
                except Exception as e2:
                    print(f"# {f['id']}: stays failed ({e2})", file=sys.stderr)
                f["held"] = True
            if tripped and state:
                _pub_save(work, state)
            if scope == "account":
                print(f"SKIP {args.id}: held, not failed: {why} ({error}). It waits and is tried again in about an hour. "
                      "Go on with the next item: the other pages are not affected.")
            else:
                print(f"SKIP {args.id}: held: a lookup failed ({error}), so it was not created. A run in about an hour "
                      "looks again. Go on with the next item.")
            sys.exit(3)
        if held:                                             # SocialPilot itself, or this session: the run stops
            why = f"this session may not call SocialPilot ({error})" if scope == "run" else f"SocialPilot is unavailable ({error})"
            if state:
                state["stopped"] = why
                _pub_save(work, state)
            print(f"HELD {args.id}: {why}. The draft stays approved and is tried again in about an hour. "
                  "Do not schedule anything else in this run: go to step 5.")
            sys.exit(4)
        if status == "held":                                 # the dashboard moved it to Failed instead
            print(f"FAILED {args.id}: {res.get('error')}")
        if args.status == "missing":
            print(f"FAILED {args.id}: {res.get('error')}" if res.get("status") == "failed" else
                  f"MISSING {args.id}: not found by this run ({res.get('misses', 1)} of 2): a later run looks again")
        if args.status == "needs_action" or detail.get("needs_action"):
            print(f"NEEDS ACTION {args.id}: recorded for a person: {detail.get('needs_action') or error}")
        if stop:
            if state:
                state["stopped"] = f"{args.id}: {stop}"
                _pub_save(work, state)
            print(f"STOP {args.id}: {stop}. Do not schedule anything else in this run: go to step 5.")
            sys.exit(5)
    except Exception as e:
        resp = getattr(e, "response", None)
        if resp is not None and resp.status_code == 409:
            print(f"SKIP {args.id}: {resp.json().get('error', 'conflict')} (do NOT schedule it)")
            sys.exit(3)
        raise


def cmd_budget(args, cfg):
    from .budget import make_pacer
    store = _store(cfg)
    print(make_pacer(_apify(cfg, store), store).summary())
    for r in store.db.execute("SELECT actor, COUNT(*), SUM(usd) FROM spend GROUP BY actor"):
        print(f"  {r[0]}: {r[1]} runs, ${r[2]:.3f}")


def cmd_check(args, cfg):
    """Verify the Apify token, the budget guard and that each configured actor resolves."""
    apify, ok = _apify(cfg), True
    try:
        used, cap = apify.usage()
        print(f"apify: token ok, monthly usage ${used:.2f} of ${cap:.2f} "
              f"(guard at {cfg['apify']['budget_guard_pct']:.0%})")
    except Exception as e:
        sys.exit(f"apify: cannot reach API or token rejected: {e}")
    for src in ("x", "reddit", "web"):
        actor = cfg["apify"][src]["actor"]
        try:
            info = apify._get(f"/acts/{actor.replace('/', '~')}")["data"]
            print(f"  {src:7s} {actor}: found ({info.get('title') or info.get('name')})")
        except Exception as e:
            ok = False
            print(f"  {src:7s} {actor}: NOT reachable ({e})")
    sys.exit(0 if ok else 1)


def _collect(which, apify, store, pages, cfg, hours=None) -> dict:
    from .collect import collect_reddit, collect_web, collect_x, collect_x_secondary, rotate
    out = {}
    if "x" in which:
        out["x"] = collect_x(apify, store, pages, cfg, hours=hours)
    if "x2" in which:
        out["x2"] = collect_x_secondary(apify, store, pages, cfg)
    if "reddit" in which:
        out["reddit"] = collect_reddit(apify, store, pages, cfg)
    if "web" in which:                   # web searches (the admin's websites run in "sites")
        n = cfg["collection"]["web_searches_per_page"]
        k = cfg["collection"].get("web_rotation_per_run", 0)
        qs = {p.slug: [q for q in p.web_searches if not q.startswith("site:")][:n]
              + rotate(store, f"web:{p.slug}", p.web_rotation, k) for p in pages}
        out["web"] = collect_web(apify, store, {k_: v for k_, v in qs.items() if v}, cfg)
    if "sites" in which:                 # the admin's websites (site: searches), every hour
        qs = {p.slug: [q for q in p.web_searches if q.startswith("site:")] for p in pages}
        out["sites"] = collect_web(apify, store, {k_: v for k_, v in qs.items() if v}, cfg, source="sites")
    return out


def cmd_collect(args, cfg):
    store = _store(cfg)
    apify, pages = _apify(cfg, store), _pages(args, cfg)
    started = time.time()
    which = {s for s in ("x", "reddit", "web") if getattr(args, s)} or set(cfg["collection"]["hourly_sources"])
    out = _collect(which, apify, store, pages, cfg, args.hours)
    store.log_run("collect", started, "ok", out)
    print(json.dumps(out))


def page_quota(page, store, cfg, now: float, created_today: int | None = None) -> int:
    """How many picks this run should propose so the page reaches its daily cap by the end of
    its active hours, without exceeding it."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    pc = cfg["posting"]
    local = datetime.fromtimestamp(now, ZoneInfo(page.timezone))
    start, end = pc["active_hours"]
    if not (start <= local.hour < end):
        return 0
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    # without the dashboard's count: this routine's own uploads (briefed or skipped stories aren't posts)
    done = store.uploaded_count(page.slug, midnight) if created_today is None else created_today
    remaining = pc["daily_cap"] - done
    if remaining <= 0:
        return 0
    hours_left = max(end - local.hour, 1)
    return min(pc["max_picks_per_run"], -(-remaining // hours_left))


def escalates(page) -> bool:
    """Thin pages search Google for more coverage, except a page sourced only from websites the
    admins chose: their feeds already bring every article, and keyword searches would bring other
    sites' stories (wars, hurricanes) the admins left out."""
    return bool(page.x_handles or page.subreddits or page.web_rotation
                or any(not q.startswith("site:") for q in page.web_searches))


def _hourly_select(args, cfg, collect: bool = True, extra_sent: dict | None = None,
                   dedupers: dict | None = None):
    """Budgeted collection + selection shared by `hourly` and `routine prepare`.
    extra_sent: {page: n} posts already created today (from the dashboard) for the daily cap."""
    import requests

    from .apify import BudgetExceeded
    from .budget import expected_cost, make_pacer, utc_midnight
    from .collect import collect_web
    from .refresh import refresh
    from .select import select_page
    store, pages = _store(cfg), _pages(args, cfg)
    sch = cfg["schedule"]
    log, why = {}, ""
    try:
        apify = _apify(cfg, store)
        pacer = make_pacer(apify, store)
    except SystemExit as e:                    # no APIFY_TOKEN (secret() exits)
        why = str(e)
    except requests.RequestException as e:     # a rotated or rejected token, Apify down (any other error is a bug)
        why = f"{type(e).__name__}: {e}"
    if why:
        # the stories already stored are still picked from (the editors' links and regenerations
        # were briefed before this). The run report counts this line as degraded (bore/ops.py).
        import types
        why = f"Apify unavailable ({why[:200]}): collection skipped, picking from the stored posts"
        apify, pacer, collect = None, types.SimpleNamespace(summary=lambda: why), False
        log["apify"] = why
    print(pacer.summary())

    def spend_ok(name: str, actor: str) -> bool:
        pacer.spent_today = store.apify_spend_since(utc_midnight(time.time()))   # Krea has its own cap
        need = expected_cost(store, actor, sch["default_cost_usd"][name])
        ok = pacer.allows(need)
        if not ok:
            print(f"{name}: skipped by pacer (needs ~${need:.3f}; "
                  f"${pacer.spent_today:.3f}/${pacer.daily_allowance:.3f} used today)")
        return ok

    def due(source: str, every: float) -> bool:
        st = store.get_state(source)
        return not st or not st.get("last_success") or time.time() - st["last_success"] >= every * 3600 - 300

    if collect:
        from .feeds import collect_feeds, resolve_feeds
        try:                                 # websites with a feed are read from it (free, hourly)
            resolve_feeds(store, pages)
            if any(p.feeds for p in pages):
                log["feeds"] = sum(collect_feeds(store, pages).values())
                print(f"feeds: {log['feeds']} new articles ({', '.join(sorted({f for p in pages for f in p.feeds}))})")
        except Exception as e:               # a feed problem never stops the rest of the collection
            print(f"feeds: failed ({e})")
        try:
            log.update(_collect({"x"}, apify, store, pages, cfg))       # X always (hard stop only)
            if (any(p.x_handles_secondary for p in pages) and due("x2", sch["x_secondary_every_hours"])
                    and spend_ok("x2", cfg["apify"]["x"]["actor"])):
                log.update(_collect({"x2"}, apify, store, pages, cfg))
            if due("reddit", sch["reddit_every_hours"]) and spend_ok("reddit", cfg["apify"]["reddit"]["actor"]):
                log.update(_collect({"reddit"}, apify, store, pages, cfg))
            if due("web", sch["web_every_hours"]) and spend_ok("web", cfg["apify"]["web"]["actor"]):
                log.update(_collect({"web"}, apify, store, pages, cfg))
            if (any(q.startswith("site:") for p in pages for q in p.web_searches)
                    and due("sites", sch.get("sites_every_hours", 1)) and spend_ok("web", cfg["apify"]["web"]["actor"])):
                log.update(_collect({"sites"}, apify, store, pages, cfg))
            if due("refresh", sch["refresh_every_hours"]) and spend_ok("refresh", cfg["apify"]["x"]["actor"]):
                log["refresh"] = refresh(apify, store, pages, cfg)
                store.mark_attempt("refresh", None, log["refresh"]["updated"], time.time())
        except BudgetExceeded as e:
            print(f"STOPPED: {e}")
            log["budget"] = str(e)

    now = time.time()
    sent_since = now - cfg["dedup"]["suppress_hours"] * 3600
    since = now - cfg["clustering"]["max_age_hours"] * 3600
    results = []
    for p in pages:
        quota = page_quota(p, store, cfg, now, (extra_sent or {}).get(p.slug)) if p.hourly else 0  # Studio-only page
        dd = (dedupers or {}).get(p.slug)
        res = select_page(p, store.page_items(p.slug, since), cfg, store.sent_for_page(p.slug, sent_since),
                          now, quota=quota, dedupe=dd)
        esc = store.last_escalation(p.slug)
        if (collect and escalates(p) and res.escalation and res.escalation["required"] and "budget" not in log
                and (not esc or now - esc["run_at"] >= sch["escalate_min_gap_hours"] * 3600)
                and spend_ok("escalation", cfg["apify"]["web"]["actor"])):
            qs = res.escalation["queries"][: sch["escalation_queries"]]
            try:
                n = collect_web(apify, store, {p.slug: qs}, cfg, source="web-escalation")
                store.record_escalation(p.slug, qs, n)
                now = time.time()
                res = select_page(p, store.page_items(p.slug, since), cfg,
                                  store.sent_for_page(p.slug, sent_since), now, escalated=True, quota=quota,
                                  dedupe=dd)
            except BudgetExceeded as e:
                print(f"escalation stopped: {e}")
        results.append((p, res))
    return store, apify, pacer, results, log, now


def cmd_hourly(args, cfg):
    """Sourcing-only hourly run: collect within budget, pick, render the PDF briefing, record."""
    from .render import render
    from .select import record_briefed
    started = time.time()
    store, apify, pacer, results, log, now = _hourly_select(args, cfg)
    stamp = datetime.now().strftime("%Y%m%d-%H")
    out_dir = resolve(cfg, cfg["output_dir"])
    path = render([(p.name, r) for p, r in results], cfg, out_dir / f"hourly-{stamp}.pdf", now)
    (out_dir / "latest.pdf").write_bytes(path.read_bytes())
    lines = [f"# Picks {datetime.now():%Y-%m-%d %H:%M}", "", pacer.summary(), ""]
    for p, r in results:
        record_briefed(store, p.slug, r, now)
        lines.append(f"## {p.name}: {min(len(r.shortlist), r.quota)} pick(s) this hour")
        for i, t in enumerate(r.shortlist, 1):
            tag = "BACKUP " if i > r.quota else ""
            status = {"corroborated": "VERIFIED", "scoop": "SCOOP", "watchlist": "VERIFY FIRST"}[t.route]
            link = t.cluster.lead().get("url") or ""
            lines.append(f"{i}. {tag}[{status}] {t.headline[:160]} ({t.cluster.engagement:,} eng) {link}")
        lines.append("")
    (out_dir / "latest.md").write_text("\n".join(lines))
    store.log_run("hourly", started, "ok", log)
    store.prune()
    for old in sorted(out_dir.glob("hourly-*.pdf"))[:-48]:   # keep two days of hourly PDFs
        old.unlink()
    print("\n".join(lines))


# ------------------------------------------------------------------ routine (posts)
def cmd_routine(args, cfg):
    from . import routine as R
    fn = {"prepare": _routine_prepare, "photos": _routine_photos, "render": _routine_render, "as-is": _routine_as_is,
          "lint": _routine_lint, "verify": _routine_verify,
          "krea-request": _routine_krea_request, "krea-result": _routine_krea_result,
          "fallback": _routine_fallback, "upload": _routine_upload, "status": _routine_status,
          "regen-fail": _routine_regen_fail, "link-fail": _routine_link_fail, "link-text": _routine_link_text,
          "skip": _routine_skip, "check": _routine_check,
          "state": _routine_state, "tspec-render": _routine_tspec_render}[args.step]
    fn(args, cfg, R)


def _dashboard():
    from .dashboard import Dashboard
    return Dashboard.from_env()


IMAGE_DEADLINE = {"at": 0.0}     # image checks stop after posts.image_budget_s per run (then plain photos)


def story_head(members: list[dict]) -> tuple[str, float, list[str]]:
    """A story's headline (its most engaged post), first time seen and its own websites."""
    from .textutil import domain
    lead = max(members, key=lambda m: int(m.get("engagement") or 0))
    head = lead.get("title") or (lead.get("text") or "")[:200]
    since = min((m.get("created_at") or time.time()) for m in members)
    return head, since, sorted({domain(m["url"]) for m in members if m.get("platform") == "web" and m.get("url")})


def consensus_prepass(stories: list[list[dict]], workers: int = 3, deadline_s: float = 15) -> list[list]:
    """The picture other outlets used, for several stories at once (each one bounded)."""
    from concurrent.futures import ThreadPoolExecutor

    from .consensus import consensus_photos

    def one(members):
        head, since, own = story_head(members)
        return consensus_photos(head, since, own, deadline_s=deadline_s, ocr=True)
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, stories))


def _story_images(members: list[dict], policy: str, cfg: dict, apify=None, same: list | None = None,
                  learned: dict | None = None) -> list:
    """A story's candidate photos, best first, in tiers (never re-sorted by size across tiers).
    Celebrity/viral pages: HD frames of the story's video matching the picture other outlets used,
    then that picture (`same`, from consensus_prepass), then other stills of the video, then the
    story's own photos (an article's stock or file photo last, flagged when outlets used another).
    Sports pages: the reporters' photos first; other outlets' picture only when fewer than two
    usable photos remain; no broadcast frames or designed graphics.
    `learned` (the page's lessons): within the story's own photos, what editors kept before decides
    between photos of about the same size (area x a 0.9-1.1 weight: up to a fifth less area, i.e.
    sides about 10% shorter); among outlets' pictures, between equal counts."""
    from .lessons import photo_weight, tie_break
    from .media import best, dhash, graphic, story_photos
    from .stills import ham, video_stills
    pc = cfg["posts"]
    floor = pc["min_photo_short_side"]
    frames = pc["allow_video_frames"] and policy != "never"
    if IMAGE_DEADLINE["at"] and time.time() > IMAGE_DEADLINE["at"]:      # out of time: the story's own photos
        return best(story_photos(members, policy=policy), floor, limit=3, allow_frames=frames)
    from . import faceid
    from .media import subject_of
    subject = subject_of(members)    # whose face the photos should show: looked up while they download
    ref = faceid.start_reference(subject, policy == "never") if subject and faceid.available() else None
    own = best(story_photos(members, policy=policy, max_links=2), floor, limit=5, allow_frames=frames)

    def clean(ph) -> bool:        # designed cards, screenshots, scoreboards (posts and outlets carry them)
        try:
            return ph.source == "web" or not graphic(ph, sports=policy == "never")
        except Exception:
            return True
    # largest first (best), nudged by what editors kept: only photos of about the same size swap
    # (at most 1.1/0.9 in area, ~10% in side length)
    own = sorted((ph for ph in own if clean(ph)), key=lambda ph: -ph.w * ph.h * photo_weight(learned, ph))
    if same is None and len(own) < 2:      # not enough of the story's own photos: what outlets used
        head, since, sites = story_head(members)
        from .consensus import consensus_photos
        same = consensus_photos(head, since, sites, deadline_s=12, ocr=True)
    same = tie_break([ph for ph in best(same or [], floor, limit=3, keep_order=True) if clean(ph)], learned,
                     key=lambda ph: ph.consensus)
    if policy != "prefer":
        tiers = [own, same if len(own) < 2 else []]
    else:
        stills = []
        vs = pc.get("video_stills") or {}
        try:
            stills = video_stills(members, policy, apify=apify, cfg=cfg, budget_s=vs.get("budget_s", 25),
                                  min_short=pc.get("video_still_min_short_side", 720), n_frames=vs.get("frames", 8),
                                  refs_extra=same)
        except Exception as e:
            print(f"    video stills unavailable ({type(e).__name__}: {e})")
        matched = [x for x in stills if x.ref_match >= 0.7]
        if same:              # the article's own picture, when outlets used another one
            from .consensus import picture_hash
            for ph in own:
                try:
                    if ph.source == "web" and not any(ham(picture_hash(ph.data), x.phash) <= 12 for x in same if x.phash):
                        ph.flags.append("other outlets used a different picture")
                except Exception:
                    pass
        tiers = [matched, same, [x for x in stills if x not in matched], own]
    out, seen = [], []
    for tier in tiers:
        for ph in tier[:3]:
            try:
                h = dhash(ph.data)
            except Exception:
                h = None
            if h is not None and any(bin(h ^ x).count("1") <= 8 for x in seen):
                continue                                  # the same picture from an earlier tier
            if h is not None:
                seen.append(h)
            out.append(ph)
    out = out[:6]                    # the six by tier; whose face it shows only reorders them, never drops one
    _faces(out, subject, policy, ref)
    if any(x.ref_match >= 0.7 or x.consensus >= 2 for x in out):
        print(f"    other outlets' picture: {next(x.via for x in out if x.ref_match >= 0.7 or x.consensus >= 2)}")
    return out


def _faces(photos: list, subject: str, policy: str, ref=None) -> None:
    """Whose face each photo shows (bore/faceid.py), within the run's image time; a photo that may
    not be the story's subject moves behind the others (the tiers keep their order otherwise)."""
    from .faceid import id_rank, label
    left = (IMAGE_DEADLINE["at"] - time.time()) if IMAGE_DEADLINE["at"] else 10
    if not (subject and photos and left > 2):
        return
    try:
        rep = label(photos, subject, sports=policy == "never", deadline_s=min(10, left), ref=ref)
    except Exception as e:
        print(f"    faces not compared ({type(e).__name__}: {e})")
        return
    photos.sort(key=lambda ph: id_rank(ph) == 2)
    if rep.get("match") or rep.get("mismatch"):
        print(f"    {rep['match']} photo(s) show {subject}, {rep['mismatch']} may not ({rep['reference']} reference)")


def _restored_or_exit(args, cfg, what: str) -> None:
    """Work only on the database this session's pull restored (or --fresh): on an empty one the
    feed has no stories, and the hourly run would push it over the saved state."""
    from .dashboard import restored
    if not getattr(args, "fresh", False) and restored(resolve(cfg, cfg["db_path"])) is None:
        sys.exit(f"{what}: the database was not restored. Run `python -m bore.cli routine state --action pull`; "
                 "if the pull fails twice, report it and stop (--fresh only for a new database the owner asked for).")


def _routine_prepare(args, cfg, R):
    """Collect, select, and write one brief per pick with sharp candidate photos."""
    from .deps import NEEDS, require
    from .media import best, story_photos
    from .select import record_briefed
    started = time.time()
    dash = _dashboard()
    if dash:          # the dashboard holds the saved state (without one there is nothing to push over)
        _restored_or_exit(args, cfg, "routine prepare")
    # and the browser every card is drawn in: without it the session would write the posts and ask
    # Krea for images before every card failed
    require(NEEDS["images"] + ("playwright", "chromium"), "routine prepare")
    created = {}
    if dash:
        try:
            created = dash.created_today(cfg)
            print("dashboard: posts created today", created)
        except Exception as e:
            print(f"dashboard stats unavailable ({e}); using local counts")
        _krea_report(dash, cfg)
    recent, dedupers = [], {}
    if dash:
        try:
            from .dupes import Deduper
            recent = dash.recent_drafts(cfg["dedup"]["suppress_hours"])
            dedupers = {p.slug: Deduper(p, recent) for p in _pages(args, cfg)}
            print(f"dashboard: {len(recent)} post(s) in the last {cfg['dedup']['suppress_hours']}h checked for repeats")
        except Exception as e:
            print(f"recent posts unavailable ({e}); repeat check uses local history only")
    if dash:  # what the editors' reviews teach (rebuilt daily, sooner after a new note), before ranking
        try:
            from .lessons import refresh
            refresh(dash, _pages(args, cfg))
        except Exception as e:
            print(f"lessons unavailable ({e}); ranking without them")
    # editors' requests first, before collection: an Apify problem (no token, a rotated one, an
    # outage) must never hold up their links and regenerations
    run = R.new_run_dir()
    pc = cfg["posts"]
    n = 0
    regens = []
    if dash:  # reviewer feedback first: these rebuild existing posts, outside the hourly quota
        try:
            regens = dash.regen_requests()
        except Exception as e:
            print(f"regeneration requests unavailable ({e})")
    for req in regens:
        d = R.write_regen_brief(run, req, cfg)
        n += 1
        b = json.loads((d / "brief.json").read_text())["regeneration"]
        print(f"REGEN  {d.relative_to(R.WORK)} replaces {req['id']}\n"
              f"       feedback: {b['feedback'] or '(image only)'}"
              + (f"\n       reviewer image: photos[{b['reference_photo_index']}]" if b["reference_photo_index"] is not None else ""))
    links = []
    if dash:  # editors' "URL to post" links: made this run, outside the hourly quota
        try:
            links = dash.link_requests()
        except Exception as e:
            print(f"link requests unavailable ({e})")
    if links:
        from .linkpost import write_link_brief
        try:          # the Reddit actor is only a last way to read a Reddit link
            link_apify = _apify(cfg, _store(cfg))
        except SystemExit:                     # no APIFY_TOKEN
            link_apify = None
        for lr in links:
            d = write_link_brief(run, lr, cfg, recent=recent, apify=link_apify)
            n += 1
            lk = json.loads((d / "brief.json").read_text())["link"]
            print(f"LINK   {d.relative_to(R.WORK)} for {lr.get('page_name') or lr['page']}: {lr['url']}"
                  + (f"\n       note: {lr['note']}" if lr.get("note") else "")
                  + (f"\n       READ FAILED: {lk['read_error']}" if lk["read_error"] else ""))
    store, apify, pacer, results, log, now = _hourly_select(args, cfg, collect=not args.no_collect,
                                                            extra_sent=created, dedupers=dedupers)
    from .lessons import load as load_lessons
    from .stills import video_policy
    IMAGE_DEADLINE["at"] = time.time() + cfg["posts"].get("image_budget_s", 240)
    for p, res in results:
        print(f"{p.name}: quota {res.quota} this run, {len(res.shortlist)} candidate(s)")
        policy = video_policy(p)
        learned = load_lessons(p.slug)       # which photos editors kept (orders near-equal candidates)
        # the picture other outlets used, for the whole shortlist at once (celebrity/viral pages)
        same = consensus_prepass([t.cluster.members for t in res.shortlist]) if policy == "prefer" and res.shortlist else []
        for i, t in enumerate(res.shortlist):
            # other pages: computed only when the story's own photos aren't enough
            pre = (same[i] if i < len(same) else []) if policy == "prefer" else None
            photos = _story_images(t.cluster.members, policy, cfg, apify, pre, learned)
            d = R.write_brief(run, p, t, i + 1, i >= res.quota, photos, cfg,
                              recent=[r for r in recent if r.get("page") == p.slug])
            n += 1
            print(f"  {'backup' if i >= res.quota else 'PICK  '} {d.relative_to(R.WORK)} "
                  f"[{t.route}] {len(photos)} sharp photo(s): {t.headline[:90]}")
        pre = [x for x in res.suppressed if str(x.get("why", "")).startswith("prefilter")]
        if pre:
            print(f"  filtered out {len(pre)} low-value stories (service pieces, promo, topics the playbook rules out...)")
        for sup in res.suppressed:
            if str(sup.get("why", "")).startswith("repeats"):
                print(f"  skipped repeat: {sup['headline'][:70]} -> {sup['matched']}")
        record_briefed(store, p.slug, res, now)
    if dash:  # every brief carries the editors' recent verdicts for its page
        try:
            fb = dash.editor_feedback(7, cap=12)   # the latest verdicts; `lessons` holds the counts
            for bf in run.glob("*/brief.json"):
                b = json.loads(bf.read_text())
                b["editor_feedback"] = fb.get(b["page"], {"rejected": [], "approved": [], "reasons": {}})
                bf.write_text(json.dumps(b, indent=1, ensure_ascii=False))
            for slug, f in fb.items():
                total = len(f["rejected"]) + len(f["approved"])
                if total:
                    print(f"editor reviews, last 14 days, {slug}: {len(f['approved'])} approved, "
                          f"{len(f['rejected'])} not okay {f['reasons']}")
        except Exception as e:
            print(f"editor feedback unavailable ({e})")
    store.log_run("routine-prepare", started, "ok", log)
    store.prune()
    (run / "run.json").write_text(json.dumps({"pacer": pacer.summary(), "quota": {p.slug: r.quota for p, r in results},
                                              "created_today": created, "regenerations": [r["id"] for r in regens],
                                              "links": [lr["id"] for lr in links]},
                                             indent=1))
    print(f"\n{n} brief(s) in {run}")


def _routine_dir(args, R) -> Path:
    d = Path(args.dir)
    if not d.is_absolute():
        d = (R.WORK / d) if (R.WORK / d).exists() else Path.cwd() / d
    if not (d / "brief.json").exists():
        sys.exit(f"no brief.json in {d}")
    return d


def _routine_photos(args, cfg, R):
    from .photosearch import print_report, search_subject, subject_name
    from .stills import video_policy
    d = _routine_dir(args, R)
    brief = json.loads((d / "brief.json").read_text())
    from .lessons import load as load_lessons
    page = next((p for p in _pages(args, cfg) if p.slug == brief.get("page")), None)
    sports = bool(page) and video_policy(page) == "never"
    found, report = search_subject(args.query, subject_name(args.query, args.must or ""), team=getattr(args, "team", "") or "",
                                   sports=sports, floor=cfg["posts"]["min_photo_short_side"], limit=4, deadline_s=20,
                                   learned=load_lessons(brief.get("page") or ""))
    print_report(report)
    brief["photo_search"] = (brief.get("photo_search") or []) + report
    start = len(brief["photos"])
    from .faceid import fields as id_fields
    for j, ph in enumerate(found):
        fn = f"photos/{start + j}.jpg"
        (d / fn).write_bytes(ph.data)
        brief["photos"].append({"index": start + j, "file": fn, "url": ph.url, "source": ph.source,
                                "credit": ph.credit, "size": f"{ph.w}x{ph.h}", "query": args.query,
                                **{k: v for k, v in (("via", ph.via), ("post_url", ph.post_url), ("flags", ph.flags),
                                                     *id_fields(ph).items()) if v}})
    (d / "brief.json").write_text(json.dumps(brief, indent=1, ensure_ascii=False))
    print(f"{len(found)} sharp photo(s) added for '{args.query}'")
    for ph in brief["photos"][start:]:
        who = {"match": f"  matches {ph.get('id_name')}", "mismatch": f"  MAY NOT BE {ph.get('id_name')}"}.get(ph.get("id_match"), "")
        print(f"  [{ph['index']}] {ph['file']} {ph['size']} {ph['credit']}{who}")


def _routine_render(args, cfg, R):
    from .krea import KreaBudgetExceeded
    d = _routine_dir(args, R)
    if not (d / "copy.json").exists():
        sys.exit("write copy.json first")
    try:
        print(json.dumps(R.render_krea(d, cfg, _store(cfg), args.feedback or "")))
    except KreaBudgetExceeded as e:
        print(f"Krea cap reached ({e}); rendering with the template instead")
        print(json.dumps(R.render_fallback(d, cfg)))
    print(d / "card.jpg")


def _lint_or_exit(d, cfg, R):
    from .guard import lint
    brief = json.loads((d / "brief.json").read_text())
    copy = json.loads((d / "copy.json").read_text())
    res = lint(brief, copy, R.load_playbook(brief["page"]), cfg["posts"]["min_photo_short_side"],
               cfg["posts"].get("min_niche_fit", 8))
    (d / "lint.json").write_text(json.dumps(res, indent=1))
    if res.get("skip") and (brief.get("link") or brief.get("regeneration")):     # the other way to skip: refused too
        sys.exit("REFUSED: an editor's link or regeneration is never skipped (ROUTINE.md 2b/2c): remove \"decision\": "
                 "\"skip\" and make the post, or report it with `routine link-fail` / `routine regen-fail <dir> --reason ...`")
    if res.get("skip"):   # decision: skip in copy.json counts as a recorded skip
        first = not (d / "skip.json").exists()   # linting again must not use up a photo retry
        (d / "skip.json").write_text(json.dumps({"reason": copy.get("reason", ""), "at": time.time()}))
        _store(cfg).record_decision(d.parent.name, brief["page"], d.name, "skipped", copy.get("reason", ""),
                                    brief.get("headline_hint", ""))
        if first:
            _remember_skip(brief, cfg, photo=photo_only(copy.get("reason", "")))
    for e in res["errors"]:
        print(f"LINT ERROR: {e}")
    for w in res["warnings"]:
        print(f"lint warning: {w}")
    if not res["pass"]:
        sys.exit("copy.json failed the lint: fix copy.json and run again (nothing was generated)")
    return res


def _routine_lint(args, cfg, R):
    d = _routine_dir(args, R)
    _lint_or_exit(d, cfg, R)
    print("lint: PASS")


def _routine_as_is(args, cfg, R):
    """The editor's images as they are (copy.json "as_is"): no Krea, no card."""
    d = _routine_dir(args, R)
    _lint_or_exit(d, cfg, R)
    if not json.loads((d / "copy.json").read_text()).get("as_is"):
        sys.exit('set "as_is": true in copy.json first (only when the editor asked for the images as they are)')
    try:
        print(json.dumps(R.render_as_is(d)))
    except ValueError as e:
        sys.exit(f"BLOCKED: {e}")
    print(d / "card.jpg", *sorted((d / "extra").glob("*.jpg")))


def _routine_verify(args, cfg, R):
    from .guard import verify
    d = _routine_dir(args, R)
    if not (d / "card.jpg").exists():
        sys.exit("no card.jpg yet")
    brief = json.loads((d / "brief.json").read_text())
    copy = json.loads((d / "copy.json").read_text())
    if copy.get("as_is"):
        print("verify: as-is post, no card text to check (look at the images yourself, step 4b)")
        return
    res = verify(str(d / "card.jpg"), brief, copy)
    (d / "ocr.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print("OCR read:", " | ".join(res["ocr"]))
    for e in res["errors"]:
        print(f"CARD ERROR: {e}")
    for w in res["warnings"]:
        print(f"card warning: {w}")
    print("verify: " + ("PASS" if res["pass"] else "FAIL - retry Krea with these errors as --feedback, or use the fallback"))


def _krea_ledger(store, cap: int, cfg) -> dict | None:
    """What the hourly routine's own ledger counted, with the UTC month and day it counted them in
    (the dashboard keeps a count only for its own month and day). None on a database this session
    did not restore (a local checkout): its count is not the routine's."""
    from datetime import timezone
    from . import routine as R
    from .dashboard import restored
    if restored(resolve(cfg, cfg["db_path"])) is None:
        return None
    now = time.time()
    d = datetime.fromtimestamp(now, timezone.utc)
    return {"hourly_used": R.krea_images_this_month(store, now), "hourly_today": R.krea_daily_allowance(store, cap, now)[0],
            "month": d.strftime("%Y-%m"), "day": d.strftime("%Y-%m-%d")}


def _krea_key(d: Path) -> str:
    """One reservation per generation: `krea-request` run again before its image is made (an answer
    lost, the step repeated) asks with the same key and gets the same reservation back. A new attempt
    (attempts.json grows with each image) is a new key."""
    import secrets
    n = len(json.loads((d / "attempts.json").read_text())) if (d / "attempts.json").exists() else 0
    f = d / "krea_key.json"
    try:
        k = json.loads(f.read_text())
    except (OSError, ValueError):
        k = {}
    if k.get("attempt") != n or not k.get("key"):
        k = {"attempt": n, "key": secrets.token_hex(8)}
        f.write_text(json.dumps(k))
    return f"{k['key']}-{n}"


def _krea_studio_known(store, got: dict | None = None) -> int:
    """Studio's images this month as the dashboard last said (kept in the routine's state, so the
    hourly routine still counts them while the dashboard does not answer), and those of today (UTC)
    under krea_studio_today:<day>."""
    key = f"krea_studio:{time.strftime('%Y-%m', time.gmtime())}"
    if got is not None and isinstance(got.get("studio"), int):
        store.set_kv(key, got["studio"])
    if got is not None and isinstance(got.get("studio_today"), int):
        store.set_kv(f"krea_studio_today:{time.strftime('%Y-%m-%d', time.gmtime())}", got["studio_today"])
    return int(store.get_kv(key, 0) or 0)


def _krea_studio(d: Path, R) -> bool:
    return (R.WORK / "studio").resolve() in d.resolve().parents


def _krea_unavailable(e: Exception) -> str:
    from . import ops
    why = f"shared Krea allowance unavailable ({type(e).__name__})"
    ops.degraded(why)            # the admin's Health tab says so (editors see nothing: posts still come)
    return why


def _krea_report(dash, cfg) -> None:
    """At every hourly run: the hourly routine's ledger to the shared Krea count, so Studio is never
    checked without it (a new month, the day this was deployed), and the line the summary quotes."""
    store, cap = _store(cfg), cfg["krea"]["monthly_max_images"]
    ledger = _krea_ledger(store, cap, cfg)
    if ledger is None:
        return
    try:
        got = dash.krea_reserve("hourly", cap, ledger=ledger, report_only=True)
    except Exception as e:
        print(f"{_krea_unavailable(e)}: Studio uses template cards until it answers")
        return
    print(f"Krea images this month (hourly routine and Studio): {got['used']}/{cap}, today {got['today']}/{got['allowed']} "
          f"({_krea_studio_known(store, got)} by Studio)")


def _spec_art_or_exit(d, cfg) -> None:
    """A template spec (copy.json template_spec) is drawn by the renderer: Krea only paints its AI-art
    layer, in art mode. A spec with no art layer is drawn on the photo alone, with no Krea image spent."""
    copy = json.loads((d / "copy.json").read_text())
    if not copy.get("template_spec") or cfg["krea"].get("mode") == "premium":
        return                  # (premium: the image model makes the whole card in the template's layout)
    from . import tspec
    spec = tspec.find(copy["template_spec"], json.loads((d / "brief.json").read_text()))   # lint found it
    if not spec.get("art"):
        sys.exit(f"REFUSED: the template you chose has no AI-art layer: draw it with `routine fallback {d}` "
                 "(the photo on the template's own background; no Krea image is spent)")
    if cfg["krea"].get("mode", "full") != "art":
        sys.exit(f"REFUSED: a template spec is drawn by the renderer, and Krea paints only its art layer (krea mode "
                 f"art): draw it with `routine fallback {d}`")


def _routine_krea_request(args, cfg, R):
    d = _routine_dir(args, R)
    _lint_or_exit(d, cfg, R)
    _spec_art_or_exit(d, cfg)
    store, cap = _store(cfg), cfg["krea"]["monthly_max_images"]
    # one allowance for the hourly routine and Studio, kept by the dashboard (a Studio session has no
    # ledger of its own: its images would never count); the hourly routine also sends what its ledger
    # counted. Without the dashboard, only the hourly routine may go on, on its own ledger.
    studio = _krea_studio(d, R)
    # the request first: one that cannot be built (a photo upload that fails) takes nothing
    req = R.krea_request(d, cfg, args.upload_url, args.feedback or "")
    from .dashboard import KreaRefused
    dash = _dashboard()
    try:
        if not dash:
            raise ConnectionError("DASHBOARD_URL / ROUTINE_SECRET not set")
        got = dash.krea_reserve("studio" if studio else "hourly", cap, _krea_key(d),
                                None if studio else _krea_ledger(store, cap, cfg))
        used, today, allowed = got["used"], got["today"], got["allowed"]
        if not studio:
            _krea_studio_known(store, got)
    except KreaRefused as e:
        sys.exit(f"{e} (the month's {cap} are shared by the hourly routine and Studio, spread over its days). "
                 f"Use `bore routine fallback {args.dir}`.")
    except Exception as e:
        why = _krea_unavailable(e)
        if studio:
            sys.exit(f"KREA UNCHECKED: the shared Krea allowance could not be read ({type(e).__name__}). "
                     f"Use `bore routine fallback {args.dir}`.")
        # this routine's own images and Studio's as last known: Studio's of today against today's
        # share, its earlier ones as already used (as the dashboard counts them)
        studio_n = _krea_studio_known(store)
        studio_t = min(studio_n, int(store.get_kv(f"krea_studio_today:{time.strftime('%Y-%m-%d', time.gmtime())}", 0) or 0))
        used = R.krea_images_this_month(store) + studio_n
        print(f"{why}: counting this routine's own images and Studio's last known {studio_n}")
        if used >= cap:
            sys.exit(f"KREA CAP: {used}/{cap} images this month. Use `bore routine fallback {args.dir}`.")
        today, allowed = R.krea_daily_allowance(store, cap - (studio_n - studio_t))
        today += studio_t
        if today >= allowed:
            sys.exit(f"KREA PACE: {today}/{allowed} Krea images today (the month's {cap} are spread over its days). "
                     f"Use `bore routine fallback {args.dir}`.")
    print(f"# Krea images this month: {used}/{cap}, today {today}/{allowed}. Send this through the Krea connector (generate_image):")
    print(json.dumps(req, indent=1, ensure_ascii=False))


def _krea_urls(d: Path) -> set:
    try:
        return {a.get("url") for a in json.loads((d / "attempts.json").read_text())}
    except (OSError, ValueError):
        return set()


def _krea_made(d: Path, cfg, R, key: str, before: set) -> None:
    """Tell the shared Krea count that the image reserved with `key` was made: only a new image (the
    same one again, with a new layout or photo, is not). Never stops krea-result: a reservation never
    said to be made stops counting after 45 minutes, and the run says so on the admin's Health tab."""
    if not (_krea_urls(d) - before):
        return
    dash = _dashboard()
    if not dash:
        return
    try:
        dash.krea_reserve("studio" if _krea_studio(d, R) else "hourly", cfg["krea"]["monthly_max_images"], key, made=True)
    except Exception as e:
        print(f"{_krea_unavailable(e)}: the shared count was not told this image was made. Carry on with the card.")


def _routine_krea_result(args, cfg, R):
    from .cards import FaceBlocked, TextOverflow
    from .tspec import SpecError
    d = _routine_dir(args, R)
    key, before = _krea_key(d), _krea_urls(d)       # the key it was reserved with (before the attempt is kept)
    try:
        print(json.dumps(R.krea_result(d, cfg, _store(cfg), args.url, args.feedback or "")))
    except R.ArtRejected as e:
        sys.exit(f"ART REJECTED: {e}. Run krea-request again with --feedback \"{e}\" (or use fallback).")
    except FaceBlocked as e:
        sys.exit(f"BLOCKED: {e}")
    except TextOverflow as e:                   # a template spec's text that does not fit: shorten the slot
        sys.exit(f"REFUSED: {e}")
    except SpecError as e:                      # a spec gone or not drawable here (its problems on later lines)
        sys.exit(f"REFUSED: the template you chose cannot be drawn:\n  {e}")
    finally:                                    # a rejected artwork was made too (its credits are spent)
        _krea_made(d, cfg, R, key, before)
    print(d / "card.jpg")


def _routine_fallback(args, cfg, R):
    d = _routine_dir(args, R)
    _lint_or_exit(d, cfg, R)
    from .cards import FaceBlocked, TextOverflow
    from .tspec import SpecError
    try:
        print(json.dumps(R.render_fallback(d, cfg)))
    except FaceBlocked as e:
        sys.exit(f"BLOCKED: {e}")
    except TextOverflow as e:                   # a template spec's text that does not fit: shorten the slot
        sys.exit(f"REFUSED: {e}")
    except SpecError as e:                      # a spec gone or not drawable here (its problems on later lines)
        sys.exit(f"REFUSED: the template you chose cannot be drawn:\n  {e}")
    print(d / "card.jpg")


def _routine_tspec_render(args, cfg, R):
    """A preview of a template spec (docs/TEMPLATE_SPEC.md): the spec drawn on a photo with copy.json's
    slots (or, without --copy, each slot's first example), in the page's colours (--page). --sample:
    on neutral stand-in photos (no real person, one per photo zone), as the editors see it when they
    approve a converted template."""
    from . import tspec
    from .cards import FaceBlocked, TextOverflow, render
    if not (args.dir and (args.photo or args.sample) and args.out):
        sys.exit("usage: routine tspec-render <spec.json> (<photo> | --sample) [--copy copy.json] [--page P] --out card.png")
    try:
        spec = tspec.load(Path(args.dir))
    except tspec.SpecError as e:
        sys.exit(f"REFUSED: the template spec is not valid:\n{e}")
    copy = (json.loads(Path(args.copy).read_text()) if args.copy
            else {"template_spec": spec["id"], "slots": tspec.example_slots(spec)})
    problems = tspec.lint_slots(spec, copy)
    for e in problems:
        print(f"slot rule: {e}")
    from .studio import studio_meta          # the page's colours and logo text, as `studio sync` stored them
    theme = (studio_meta().get(args.page) or {}).get("theme") if args.page else None
    if args.sample:
        photos, faces = tspec.sample_photos(tspec.photo_count(spec))
        # the AI-art layer's place marked: what the editors approve is the layout with art in it
        card = tspec.card_spec(spec, copy, photos, theme=theme, faces=faces, keep=[[] for _ in photos],
                               art=tspec.art_stand_in(spec))
    else:
        card = tspec.card_spec(spec, copy, [Path(args.photo).read_bytes()], theme=theme)
    try:
        img = render(args.page or "preview", card)
    except FaceBlocked as e:
        sys.exit(f"BLOCKED: {e}")
    except TextOverflow as e:
        sys.exit(f"REFUSED: {e}")
    out = Path(args.out)
    if out.suffix.lower() == ".png":
        import io

        from PIL import Image
        Image.open(io.BytesIO(img)).save(out, "PNG")
    else:
        out.write_bytes(img)
    print(f"tspec-render: {out} ({spec['canvas']['w']}x{spec['canvas']['h']})"
          + (f"; {len(problems)} slot rule(s) broken: lint would refuse this copy" if problems else ""))


def _routine_upload(args, cfg, R):
    d = _routine_dir(args, R)
    verdict = json.loads((d / "verdict.json").read_text()) if (d / "verdict.json").exists() else {}
    lint_res = json.loads((d / "lint.json").read_text()) if (d / "lint.json").exists() else {}
    ocr = json.loads((d / "ocr.json").read_text()) if (d / "ocr.json").exists() else {}
    card_mtime = (d / "card.jpg").stat().st_mtime if (d / "card.jpg").exists() else 0
    if not lint_res.get("pass"):
        sys.exit("BLOCKED: run `routine lint` and fix copy.json first")
    if not ocr.get("pass") or (d / "ocr.json").stat().st_mtime < card_mtime:
        sys.exit("BLOCKED: run `routine verify` on the current card.jpg; it must PASS")
    if ocr.get("warnings") and not verdict.get("warnings_checked"):
        sys.exit("BLOCKED: verify reported warnings; look at each one and record \"warnings_checked\": "
                 "[...your finding per warning...] in verdict.json")
    if not verdict.get("pass") or (d / "verdict.json").stat().st_mtime < card_mtime:
        sys.exit("BLOCKED: verdict.json must record pass: true for the CURRENT card.jpg")
    brief = json.loads((d / "brief.json").read_text())
    copy = json.loads((d / "copy.json").read_text())
    if copy.get("as_is") and not ocr.get("as_is"):
        sys.exit("BLOCKED: as_is is set but card.jpg is not the as-is render: run `routine as-is <dir>`")
    if not copy.get("as_is"):
        import shutil
        shutil.rmtree(d / "extra", ignore_errors=True)       # an earlier as-is try's images never ship with a card
    if brief.get("regeneration") and not str(copy.get("changes", "")).strip():
        sys.exit("BLOCKED: regeneration: add \"changes\" to copy.json (one line: what you changed per the feedback)")
    dash = _dashboard()
    if dash:
        before = copy.get("note_to_editor")
        _repeat_gate(brief, copy, dash, cfg, args)
        if copy.get("note_to_editor") != before:        # an editor's repeat: the note goes on the post
            (d / "copy.json").write_text(json.dumps(copy, indent=1, ensure_ascii=False))
    payload = R.draft_payload(d)
    if dash:
        res = dash.create_draft(payload)
        print(json.dumps(res))
        _record_uploaded(brief, copy, cfg)
        (d / "uploaded").write_text(json.dumps({"draft_id": payload["id"], "at": time.time()}))
        _store(cfg).record_decision(d.parent.name, brief["page"], d.name, "uploaded", "",
                                    " ".join(copy.get("headline_lines") or []))
    else:
        out = resolve(cfg, cfg["posts"]["drafts_dir"]) / payload["id"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "card.jpg").write_bytes((d / "card.jpg").read_bytes())
        payload.pop("image_b64")
        (out / "post.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
        print(f"DASHBOARD_URL not set: saved locally to {out}")


def _repeat_gate(brief: dict, copy: dict, dash, cfg, args) -> None:
    """Refuse a post that repeats one made in the last dedup.suppress_hours (the dashboard is the
    record), unless the copy declares it an update of that post with the new fact."""
    from .config import load_pages
    from .dupes import Deduper
    page = next((p for p in load_pages() if p.slug == brief["page"]), None)
    if not page:
        return
    import requests
    for wait in (15, None):          # a network blip (Oct 23 11:21) must not lose a finished post: once more, then say so
        try:
            recent = dash.recent_drafts(cfg["dedup"]["suppress_hours"])
            break
        except (requests.ConnectionError, requests.Timeout) as e:
            if wait is None:
                sys.exit(f"UPLOAD NOT SENT: the dashboard did not answer ({type(e).__name__}); nothing was sent. "
                         f"Run the same `routine upload {args.dir}` again in a minute.")
            time.sleep(wait)
    ignore = {brief["id"]} | ({brief["regeneration"]["replaces"]} if brief.get("regeneration") else set())
    text = " ".join(copy.get("headline_lines") or []) + "\n" + copy.get("caption", "")
    m = Deduper(page, recent).match(text, ignore=ignore)
    if not m:
        return
    if copy.get("update_of") == m.draft.get("id") and str(copy.get("what_is_new", "")).strip():
        print(f"update of {m.draft['id']}: {copy['what_is_new']}")
        return
    if brief.get("link") or brief.get("regeneration"):     # an editor asked for it: made, and the repeat noted
        note = f"Similar to a recent post ({m.describe()}): approve only if you want it again."
        copy["note_to_editor"] = " ".join(x for x in (note, copy.get("note_to_editor")) if x)[:1200]   # the repeat first
        print(f"editor's request: repeats a recent post, made anyway and noted: {m.describe()}")
        return
    sys.exit(f"BLOCKED: repeats a recent post: {m.describe()}\n"
             "Skip this story. Only if it carries a genuinely NEW fact, set \"update_of\": "
             f"\"{m.draft.get('id')}\" and \"what_is_new\" in copy.json.")


def _record_uploaded(brief: dict, copy: dict, cfg) -> None:
    """Record every uploaded story (backups too) so later runs don't pick it again."""
    if not brief.get("key"):
        return
    store = _store(cfg)
    v = brief.get("verification") or {}
    store.record_sent(brief["page"], brief["key"], brief.get("signature") or [], v.get("engagement") or 0,
                      len(v.get("sources") or []), 0.0, " ".join(copy.get("headline_lines") or []))
    store.commit()


PHOTO_RX = re.compile(r"\b(photos?|images?|pictures?|headshots?|watermark\w*|alamy|face)\b", re.I)
STORY_FAULT_RX = re.compile(
    r"\b(weak|thin|vague|repeats?|same [\w' /-]{0,30}(story|saga|event|game|storyline|subject)|duplicat\w*|"
    r"already (posted|covered|pending|queued|approved)|off[- ]?niche|niche|outside|promo\w*|stale|rejected|avoid|"
    r"single\b.{0,25}\bsource|credible|unverif\w*|uncorroborat\w*|unconfirmed|preliminary|not (a )?real|"
    r"not a (reported )?(story|news)|politic\w*|hook|stakes|"
    r"no (clear |actual |concrete )?(news|fact|quote|substance|story)|"
    r"minor|depth|low-value|engagement|notable|speculat\w*|household|fluff|trivia|meme|rumou?r|sensitive|private|"
    r"suicide|quota|backup|listicle|roundup|preview|logistics|listing|recap|informational|scripted|op-ed|"
    r"fragment|context|angle|playbook|disaster|weather|substance|"
    # story faults that also mention the photo: a retry would only skip them again
    r"postable|caption|core fact|withh(?:old|eld)\w*|paywall\w*|(?:could|can)(?:not|n't) (?:verify|confirm)|"
    r"cryptic|teaser|bare (?:headline|tweet|link|one-line)|generic [\w/-]+ (?:story|post)|never names|"
    r"not (?:a |about a )?(?:specific|named|celebrity)|no (?:named|celebrity)|names no one|(?:doesn't|does not) name|"
    r"none of the sources|no verifiable|anonymous subject|prefilter|"
    r"covers (?:nfl|football) only|not (?:nfl|football) content)\b", re.I)


def photo_only(reason: str) -> bool:
    """A skip reason about the photo and nothing else ("no sharp photo of X found"). The routine
    almost never passed --photo: no story was ever retried, while Huddle skipped about 14 stories a
    day (Oct 9-13) only for the photo, many of them injury news it called good."""
    return bool(PHOTO_RX.search(reason or "")) and not STORY_FAULT_RX.search(reason or "")


def _remember_skip(brief: dict, cfg, photo: bool = False) -> None:
    """A skipped story must not come back next hour (backups were never recorded, so the same
    rejected candidates reappeared every run). Regenerations and editors' links are not stories
    from the feed and are not recorded. A story skipped ONLY for want of a usable photo comes back
    after posts.photo_retry_hours (at most photo_retry_max times): newer coverage often has one."""
    if not brief.get("key") or brief.get("regeneration") or brief.get("link"):
        return
    store = _store(cfg)
    v = brief.get("verification") or {}
    pc, when, tag = cfg["posts"], None, "[skipped] "
    if photo:   # tries per story: its key drifts with new coverage, its signature overlaps
        sig = set(brief.get("signature") or [])
        ov = cfg["dedup"]["fuzzy_signature_overlap"]
        prior = [r for r in store.sent_for_page(brief["page"], time.time() - (cfg["dedup"]["suppress_hours"] + 24) * 3600)
                 if str(r.get("headline") or "").startswith("[photo-skip]")]
        k = f"photo_retry:{brief['page']}:{brief['key']}"
        same_key = int(store.get_kv(k, 0) or 0)       # (one row per key: a repeat replaces it)
        drifted = sum(1 for r in prior if r["key"] != brief["key"] and r["signature"] and sig and
                      len(r["signature"] & sig) / min(len(r["signature"]), len(sig)) >= ov)
        if same_key + drifted < pc.get("photo_retry_max", 2):
            store.set_kv(k, same_key + 1)
            # recorded as if older, so it leaves the repeat window after photo_retry_hours
            when = time.time() - (cfg["dedup"]["suppress_hours"] - pc.get("photo_retry_hours", 3)) * 3600
            tag = "[photo-skip] "
    store.record_sent(brief["page"], brief["key"], brief.get("signature") or [], v.get("engagement") or 0,
                      len(v.get("sources") or []), 0.0, tag + brief.get("headline_hint", "")[:200], when)
    store.commit()


def _routine_skip(args, cfg, R):
    """Record that a brief was skipped, and why (required: `routine check` counts every brief).
    An editor's link or regeneration is never skipped: the dashboard would show it waiting
    forever. It is made, or reported back with link-fail / regen-fail."""
    d = _routine_dir(args, R)
    if not args.reason:
        sys.exit("--reason is required: one line on why this story is not posted")
    brief = json.loads((d / "brief.json").read_text())
    if brief.get("link") or brief.get("regeneration"):
        what, fail = ("link", "link-fail") if brief.get("link") else ("regeneration", "regen-fail")
        sys.exit(f"REFUSED: this is an editor's {what}, never skipped (ROUTINE.md 2b/2c). Make the post; only if it "
                 f"cannot be made at all, run `python -m bore.cli routine {fail} {args.dir} --reason \"<for the editor>\"`.")
    first = not (d / "skip.json").exists()       # skipping it again must not use up a photo retry
    (d / "skip.json").write_text(json.dumps({"reason": args.reason, "at": time.time()}))
    _store(cfg).record_decision(d.parent.name, brief["page"], d.name, "skipped", args.reason,
                                brief.get("headline_hint", ""))
    photo = bool(getattr(args, "photo", False)) or photo_only(args.reason)
    if first:
        _remember_skip(brief, cfg, photo=photo)
    print(f"skipped: {args.reason}" + (" (no usable photo: the story may come back in a few hours)" if photo else ""))


REQUEST_FAILED = "request_failed.json"     # link-fail / regen-fail: the editor was told why


def run_accounting(run: Path) -> dict:
    """Per page: quota, uploaded, skipped, and briefs with no decision yet (in brief order).
    Editors' links and regenerations are counted apart: done ("extra"), reported back to the
    editor ("failed"), or still open ("requests": quota and backups never excuse them)."""
    meta = json.loads((run / "run.json").read_text()) if (run / "run.json").exists() else {}
    out = {}
    for d in sorted(x for x in run.iterdir() if (x / "brief.json").exists()):
        b = json.loads((d / "brief.json").read_text())
        acc = out.setdefault(b["page"], {"quota": (meta.get("quota") or {}).get(b["page"], 0),
                                         "uploaded": [], "skipped": [], "open": [], "extra": [],
                                         "failed": [], "requests": []})
        if b.get("regeneration") or b.get("link"):
            acc["extra" if (d / "uploaded").exists() else "failed" if (d / REQUEST_FAILED).exists() else "requests"].append(d.name)
        elif (d / "uploaded").exists():
            acc["uploaded"].append(d.name)
        elif (d / "skip.json").exists():
            acc["skipped"].append(d.name)
        else:
            acc["open"].append((b.get("rank", 99), d.name))
    for acc in out.values():
        acc["open"] = [n for _, n in sorted(acc["open"])]
    return out


def _routine_check(args, cfg, R):
    """Final gate before saving state: every editor's link and regeneration is made or reported
    back, and every page reached its quota or decided every brief. Exits non-zero with what to do
    next otherwise."""
    run = Path(args.dir) if args.dir else R.latest_run_dir()
    if not run:
        sys.exit("no runs yet")
    problems = []
    for page, acc in run_accounting(run).items():
        print(f"{page}: quota {acc['quota']}, uploaded {len(acc['uploaded'])}, skipped {len(acc['skipped'])}, "
              f"undecided {len(acc['open'])}" + (f", plus {len(acc['extra'])} regeneration/link" if acc["extra"] else "")
              + (f", {len(acc['failed'])} reported back to the editor" if acc["failed"] else ""))
        if acc["requests"]:     # an editor is waiting on these: never left for the next hour
            problems.append(f"{page}: an editor's link/regeneration is not done: {', '.join(acc['requests'])}. "
                            "Make it and upload, or `routine link-fail` / `routine regen-fail <dir> --reason ...`")
        if len(acc["uploaded"]) < acc["quota"] and acc["open"]:
            problems.append(f"{page} is under quota ({len(acc['uploaded'])}/{acc['quota']}) with backups left: "
                            f"work on {', '.join(acc['open'][:3])} next")
        elif acc["open"]:
            print(f"  (quota met; {len(acc['open'])} unused backup(s) are fine to leave)")
    if problems:
        sys.exit("NOT DONE:\n  " + "\n  ".join(problems) + "\nFor each backup: write it and upload, or `routine skip <dir> --reason ...`.")
    print("check: OK")


def _routine_link_fail(args, cfg, R):
    """No post can be made from an editor's link (unreadable, off-niche, a repeat, unsupported
    facts): tell the editor why on the dashboard."""
    d = _routine_dir(args, R)
    link = json.loads((d / "brief.json").read_text()).get("link")
    if not link:
        sys.exit("not a link brief")
    if not args.reason:
        sys.exit("--reason is required (the editor sees it)")
    dash = _dashboard()
    if not dash:
        sys.exit("DASHBOARD_URL / ROUTINE_SECRET not set")
    print(json.dumps(dash.link_failed(link["request_id"], args.reason)))
    (d / REQUEST_FAILED).write_text(json.dumps({"reason": args.reason, "at": time.time()}))   # `routine check` counts it


def _routine_link_text(args, cfg, R):
    """Record what the linked page says when you had to read it yourself (paywall, bot block)."""
    from .linkpost import add_source_text
    d = _routine_dir(args, R)
    if not args.file:
        sys.exit("--file <text file with the page's text> is required")
    add_source_text(d, Path(args.file).read_text(), args.url or "")
    print("source text added; lint checks the copy against it")


def _routine_regen_fail(args, cfg, R):
    """The feedback cannot be applied (e.g. it asks for facts the sources don't support): send the
    post back to Waiting with the reason shown to the reviewer."""
    d = _routine_dir(args, R)
    regen = json.loads((d / "brief.json").read_text()).get("regeneration")
    if not regen:
        sys.exit("not a regeneration brief")
    if not args.reason:
        sys.exit("--reason is required (the reviewer sees it)")
    dash = _dashboard()
    if not dash:
        sys.exit("DASHBOARD_URL / ROUTINE_SECRET not set")
    print(json.dumps(dash.regen_failed(regen["replaces"], args.reason)))
    (d / REQUEST_FAILED).write_text(json.dumps({"reason": args.reason, "at": time.time()}))   # `routine check` counts it


def _routine_status(args, cfg, R):
    run = Path(args.dir) if args.dir else R.latest_run_dir()
    if not run:
        sys.exit("no runs yet")
    print(run)
    for d in sorted(x for x in run.iterdir() if x.is_dir()):
        b = json.loads((d / "brief.json").read_text())
        stage = ("uploaded" if (d / "uploaded").exists() else "checked" if (d / "verdict.json").exists()
                 else "rendered" if (d / "card.jpg").exists() else "written" if (d / "copy.json").exists() else "brief")
        kind = "REGEN" if b.get("regeneration") else "LINK" if b.get("link") else "backup" if b["backup"] else "PICK"
        print(f"  {d.name}  {kind}  {stage:9s} photos={len(b['photos'])}  {b['headline_hint'][:70]}")


def _routine_state(args, cfg, R):
    from .dashboard import StateError
    dash = _dashboard()
    if not dash:
        sys.exit("DASHBOARD_URL / ROUTINE_SECRET not set")
    path = resolve(cfg, cfg["db_path"])
    try:
        if args.action == "pull":
            print(dash.pull_state(path))
        else:
            print(dash.push_state(path, fresh=args.fresh))
    except StateError as e:
        if args.action == "pull":
            sys.exit(f"STATE NOT RESTORED: {e}. Do not run prepare and never push: run the pull once more; "
                     "if it fails again, report it and stop.")
        sys.exit(f"STATE NOT SAVED: {e}. The dashboard keeps its copy: report it and stop. Do not retry with "
                 "--fresh: that is the owner's step, once they have checked this database is the one to keep.")


def cmd_studio(args, cfg):
    from . import studio as S
    from .deps import NEEDS, require
    from .routine import WORK, latest_run_dir, new_run_dir
    if args.step == "sync":
        S.sync()
    elif args.step == "export-pages":
        print("stored:", ", ".join(S.export_pages()))
    elif args.step == "feed":
        _restored_or_exit(args, cfg, "studio feed")      # the stories the hourly routine collected
        require(NEEDS["images"], "studio feed")          # never a feed with the photo checks silently off
        run = Path(args.run) if args.run else (latest_run_dir() or new_run_dir())
        S.prepare_feed(run, cfg, _store(cfg))
    elif args.step == "feed-publish":
        run = Path(args.run) if args.run else latest_run_dir()
        if not run or not (run / "feed").exists():
            sys.exit("no feed folder: run `studio feed` first")
        S.publish_feed(run)
    elif args.step == "jobs":
        S.pull_jobs(cfg)
    else:
        if not args.dir:
            sys.exit(f"studio {args.step} needs a folder")
        d = Path(args.dir)
        if not d.is_absolute():
            d = WORK / d if (WORK / d).exists() else Path.cwd() / d
        if args.step == "images":
            if not args.query:
                sys.exit("--query is required")
            require(NEEDS["images"], "studio images")
            found = S.image_search(d, args.query, [w for w in (args.must or "").split(",") if w] or None, cfg)
            print(f"{len(found)} image(s) in {d / 'images.json'}")
            for x in found:
                print(f"  {x['size']:>10}  {x['credit']:<28} {x['url'][:100]}")
        elif args.step == "fail":
            if not args.reason:
                sys.exit("--reason is required (the editor sees it)")
            # one post (or template) of a job; a job folder has job.json (a templates job's has its brief too)
            if S.is_template_dir(d) or ((d / "brief.json").exists() and not (d / "job.json").exists()):
                d.mkdir(exist_ok=True)               # (a template that could not be converted has no folder yet)
                (d / "fail.txt").write_text(args.reason)
                print(f"{d.name}: marked failed; finish the job with `studio done {d.parent.relative_to(WORK)}`")
            else:                                     # the whole job
                S.finish_job(d, fail_reason=args.reason)
        elif args.step == "template-send":
            try:
                S.send_template(d)
            except ValueError as e:                   # what to fix first: an answer, not a crash
                # the details on their own lines: a report keeps the first line, never the spec's words
                sys.exit(f"NOT DONE: the template folder needs fixing first:\n  {e}")
        elif args.step == "done":
            S.finish_job(d)
        elif args.step == "precheck":
            S.precheck(d, send=getattr(args, "send", False))


def cmd_refresh(args, cfg):
    from .refresh import refresh
    store = _store(cfg)
    print(json.dumps(refresh(_apify(cfg, store), store, _pages(args, cfg), cfg)))


def _select_all(store, pages, cfg, now):
    from .select import select_page
    since = now - cfg["clustering"]["max_age_hours"] * 3600
    sent_since = now - cfg["dedup"]["suppress_hours"] * 3600
    results = []
    for p in pages:
        esc = store.last_escalation(p.slug)
        escalated = bool(esc and now - esc["run_at"] < cfg["clustering"]["window_hours"] * 3600)
        res = select_page(p, store.page_items(p.slug, since), cfg,
                          store.sent_for_page(p.slug, sent_since), now, escalated)
        results.append((p, res))
    return results


def _escalate(store, apify, results, cfg) -> list[str]:
    from .collect import collect_web
    todo = {p.slug: res.escalation["queries"] for p, res in results
            if res.escalation and res.escalation.get("required")}
    if not todo:
        return []
    n = collect_web(apify, store, todo, cfg, source="web-escalation")
    for slug, qs in todo.items():
        store.record_escalation(slug, qs, n)
    return list(todo)


def cmd_cluster(args, cfg):
    from .select import to_json
    store, now = _store(cfg), time.time()
    results = _select_all(store, _pages(args, cfg), cfg, now)
    data = [to_json(r) for _, r in results]
    path = resolve(cfg, cfg["output_dir"]) / f"cluster-{datetime.now():%Y%m%d-%H%M}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    for d in data:
        esc = d["escalation"]
        flag = f"  ESCALATE: {len(esc['queries'])} searches required" if esc and esc["required"] else ""
        print(f"{d['page']}: {len(d['trends'])} trends, {len(d['watchlist'])} watchlist, "
              f"{len(d['suppressed'])} suppressed{flag}")
    print(path)


def cmd_escalate(args, cfg):
    store, now = _store(cfg), time.time()
    apify = _apify(cfg, store)
    done = _escalate(store, apify, _select_all(store, _pages(args, cfg), cfg, now), cfg)
    print("escalated: " + (", ".join(done) or "none needed"))


def cmd_brief(args, cfg):
    from .render import render
    from .select import record_briefed
    store, pages = _store(cfg), _pages(args, cfg)
    apify = None if args.offline else _apify(cfg, store)
    if apify and not args.no_collect:
        print("collect:", json.dumps(_collect(set(cfg["collection"]["brief_sources"]), apify, store, pages, cfg)))
    if apify and not args.no_refresh:
        from .refresh import refresh
        try:
            print("refresh:", json.dumps(refresh(apify, store, pages, cfg)))
        except Exception as e:
            print(f"refresh failed ({e}); scoring on stored counts")
    now = time.time()
    results = _select_all(store, pages, cfg, now)
    if apify:
        escalated = _escalate(store, apify, results, cfg)
        if escalated:
            print("escalated:", ", ".join(escalated))
            now = time.time()
            results = _select_all(store, pages, cfg, now)
    out = resolve(cfg, cfg["output_dir"]) / f"briefing-{datetime.now():%Y%m%d-%H%M}.pdf"
    render([(p.name, r) for p, r in results], cfg, out, now)
    if not args.dry_run:
        for p, r in results:
            record_briefed(store, p.slug, r, now)
    for p, r in results:
        print(f"{p.slug}: {len(r.trends)} trends, {len(r.watchlist)} watchlist")
    print(out)


def cmd_health(args, cfg):
    from .health import check
    errors, warnings = check(_store(cfg), _pages(args, cfg), cfg)
    for w in warnings:
        print("WARN ", w)
    for e in errors:
        print("ERROR", e)
    if not errors and not warnings:
        print("ok")
    sys.exit(1 if errors else 0)


def cmd_capture_fixture(args, cfg):
    store, now = _store(cfg), time.time()
    items = store.page_items(args.page, now - args.hours * 3600)
    keep = ("id", "platform", "url", "author", "original_author", "is_repost", "title", "text",
            "subreddit", "created_at", "collected_at", "engagement")
    data = {"captured_at": now, "page": args.page,
            "stories": [{"name": "LABEL ME", "must_contain": [], "single_source_major": False}],
            "must_not_merge": [],
            "items": [{k: i.get(k) for k in keep} for i in items]}
    Path(args.out).write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"{len(items)} items -> {args.out}. Label stories by item id, then run `bore tune`.")


def cmd_tune(args, cfg):
    from .fixture import sweep
    for thr, ok, failures in sweep(args.fixture, cfg):
        print(f"threshold {thr:.2f}: {'PASS' if ok else 'fail'}"
              + ("" if ok else f"  ({failures[0]}{' ...' if len(failures) > 1 else ''})"))


def build_parser() -> argparse.ArgumentParser:
    """Every command and its options (tests/test_prompts.py checks the docs' commands against it)."""
    ap = argparse.ArgumentParser(prog="bore", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import-workbook", help="parse the source workbook into config/pages.yaml")
    s.add_argument("xlsx")
    s.add_argument("--facebook-urls", help="file of `slug=https://facebook.com/...` lines")
    s.set_defaults(fn=cmd_import_workbook)

    s = sub.add_parser("add-page", help="register a Facebook page (share links are fine)")
    s.add_argument("facebook_url")
    s.add_argument("--name")
    s.add_argument("--timezone")
    s.set_defaults(fn=cmd_add_page)

    s = sub.add_parser("dna", help="build page DNA from the page's own posts (no scraping)")
    s.add_argument("--page")
    s.add_argument("--file", help="Meta Business Suite post export (.csv) or JSON list")
    s.add_argument("--source", choices=["graph", "fanpagekarma", "cached"], default="graph",
                   help="graph: FB_PAGE_TOKEN_<SLUG> in .env; cached: reuse posts.json")
    s.add_argument("--rebuild-sources", action="store_true")
    s.set_defaults(fn=cmd_dna)

    s = sub.add_parser("sources", help="assume X/Reddit/web sources from a league and team")
    s.add_argument("--page")
    s.add_argument("--league", help="comma list: nfl,nba,mlb,nhl,cfb,soccer")
    s.add_argument("--team", help="comma list of team names, e.g. Eagles")
    s.add_argument("--name", help="set the page's display name")
    s.set_defaults(fn=cmd_sources)

    s = sub.add_parser("hourly", help="THE ROUTINE: collect within budget, pick posts, render")
    s.add_argument("--page")
    s.set_defaults(fn=cmd_hourly)

    s = sub.add_parser("routine", help="post-creation steps used by the hourly Claude routine (see ROUTINE.md)")
    s.add_argument("step", choices=["prepare", "photos", "lint", "krea-request", "krea-result", "verify",
                                     "render", "as-is", "fallback", "upload", "status", "state", "regen-fail",
                                     "link-fail", "link-text", "skip", "check", "tspec-render"])
    s.add_argument("dir", nargs="?", help="post directory (work/<run>/<id>) for photos/render/fallback/upload; "
                                          "tspec-render: the template spec (.json)")
    s.add_argument("photo", nargs="?", help="tspec-render: the photo to draw the spec on")
    s.add_argument("--copy", help="tspec-render: a copy.json with the slots (default: the spec's examples)")
    s.add_argument("--sample", action="store_true", help="tspec-render: draw on neutral stand-in photos (no real person)")
    s.add_argument("--out", help="tspec-render: where to write the preview (.png or .jpg)")
    s.add_argument("--page")
    s.add_argument("--query", help="photos: search query for the subject (the person's name works best)")
    s.add_argument("--must", help="photos: the words results must name in full (default: the query, e.g. the full name)")
    s.add_argument("--team", default="", help="photos: the player's team (found automatically for NFL players)")
    s.add_argument("--feedback", help="render: problems to fix from the previous attempt")
    s.add_argument("--reason", help="skip / regen-fail / link-fail: why (skip: for the run record; fails: shown to the editor)")
    s.add_argument("--photo", action="store_true", help="skip: the ONLY problem is that no usable photo was found")
    s.add_argument("--file", help="link-text: a text file with what the linked page says")
    s.add_argument("--no-collect", action="store_true", help="prepare: select from stored data only")
    s.add_argument("--upload-url", help="krea-request: presigned Krea upload URL (from the connector's get_upload_url)")
    s.add_argument("--url", help="krea-result: the finished image URL; link-text: the page the text came from")
    s.add_argument("--action", choices=["pull", "push"], default="pull", help="state: pull/push the database")
    s.add_argument("--fresh", action="store_true",
                   help="prepare / state push: without a restored database (a new one: only when the owner says so)")
    s.set_defaults(fn=cmd_routine)

    s = sub.add_parser("studio", help="Post Studio side of the routines (see STUDIO.md)")
    s.add_argument("step", choices=["sync", "export-pages", "feed", "feed-publish", "jobs", "images", "done", "fail", "precheck",
                                    "template-send"])
    s.add_argument("--send", action="store_true", help="precheck: send the check.json results")
    s.add_argument("dir", nargs="?", help="images/fail: a post folder (work/studio/<job>/<post>); done: a job folder; "
                                          "template-send/fail: a template folder of a templates job (work/studio/<job>/<id>)")
    s.add_argument("--run", help="feed / feed-publish: the run folder (default: the latest work/<run>)")
    s.add_argument("--query", help="images: search words (a person's full name works best)")
    s.add_argument("--must", help="images: comma list of words results must mention")
    s.add_argument("--reason", help="fail: why (shown to the editor)")
    s.add_argument("--fresh", action="store_true", help="feed: without a restored database (no collected stories)")
    s.set_defaults(fn=cmd_studio)

    s = sub.add_parser("publish", help="SocialPilot publisher routine: check SocialPilot, plan the approved queue, report results")
    s.add_argument("action", choices=["queue", "plan", "preflight", "mark"])
    s.add_argument("id", nargs="?")
    s.add_argument("--status", choices=["publishing", "scheduled", "posted", "failed", "held", "missing", "needs_action"])
    s.add_argument("--scheduled-for", help="mark: scheduled time, UTC ISO (schedule_utc from plan)")
    s.add_argument("--busy", action="append", help="plan: page=YYYY-MM-DD HH:MM (SocialPilot local time) already taken")
    s.add_argument("--post-id")
    s.add_argument("--url")
    s.add_argument("--error", help="mark: SocialPilot's error word for word (needs_action: what a person must do); "
                                   "preflight: the connector call's error")
    s.add_argument("--queued", help="mark --status scheduled: a file with the QueuedPosts result that shows the post")
    s.add_argument("--result", action="append", help="preflight: a file with the AccountList (or UserInfo) result")
    s.set_defaults(fn=cmd_publish)

    s = sub.add_parser("budget", help="show spend vs the monthly budget")
    s.set_defaults(fn=cmd_budget)

    s = sub.add_parser("check", help="verify Apify token, budget and the three actors")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("collect", help="hourly collection (X); Reddit/web are collected by `brief`")
    s.add_argument("--page")
    for flag in ("x", "reddit", "web"):
        s.add_argument(f"--{flag}", action="store_true", help=f"force {flag} this run")
    s.add_argument("--hours", type=float, help="widen the X window (first run / manual backfill)")
    s.set_defaults(fn=cmd_collect)

    s = sub.add_parser("refresh", help="re-measure engagement into the overlay")
    s.add_argument("--page")
    s.set_defaults(fn=cmd_refresh)

    s = sub.add_parser("cluster", help="cluster + select; writes JSON incl. escalation blocks")
    s.add_argument("--page")
    s.set_defaults(fn=cmd_cluster)

    s = sub.add_parser("escalate", help="run required escalation searches for thin pages")
    s.add_argument("--page")
    s.set_defaults(fn=cmd_escalate)

    s = sub.add_parser("brief", help="refresh -> cluster -> escalate -> render PDF -> record sent")
    s.add_argument("--page")
    s.add_argument("--no-refresh", action="store_true")
    s.add_argument("--no-collect", action="store_true", help="skip the pre-brief Reddit/web collection")
    s.add_argument("--offline", action="store_true", help="no API calls at all")
    s.add_argument("--dry-run", action="store_true", help="do not record trends as sent")
    s.set_defaults(fn=cmd_brief)

    s = sub.add_parser("health", help="exit 1 on data loss, warn on quiet failures")
    s.add_argument("--page")
    s.set_defaults(fn=cmd_health)

    s = sub.add_parser("capture-fixture", help="dump a page's window to JSON for hand-labelling")
    s.add_argument("--page", required=True)
    s.add_argument("--hours", type=float, default=12)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_capture_fixture)

    s = sub.add_parser("tune", help="sweep the join threshold against labelled fixture(s)")
    s.add_argument("fixture", nargs="+")
    s.set_defaults(fn=cmd_tune)
    return ap


def main(argv=None):
    from . import ops
    args = build_parser().parse_args(argv)
    # every command reports its run to the dashboard (bore/ops.py): ok and degraded come from the
    # code, how it ended and what it printed, never from the session's own account of it
    with ops.reporting(ops.command_name(args), ops.target_of(args)):
        args.fn(args, load_config())


if __name__ == "__main__":
    main()
