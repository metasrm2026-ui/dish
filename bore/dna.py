"""Page DNA: derive a page's identity from its own posting history (Fanpage Karma API v2).

Traps handled:
  * one request caps at 1000 posts -> walk the window in 7-day slices and merge
    (slices under 8 days also get the better 365/hr rate limit)
  * every caption device is cross-tabulated against post format before any engagement claim
    (raw "hashtags = 64x" was really photos-vs-links; within photos the lift was ~20%)
  * sports acronyms are excluded from the ALL-CAPS metric (NFL/QB/WR/RBI made 17% look like 81%)
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .textutil import SPORTS_ACRONYMS, URL_RE, parse_ts, pick, to_int

EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001F5FF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF☀-⛿✀-➿⭐⭕‼⁉™ℹ"
    "⌚-⌛⏩-⏺▪-◾]")
HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
MENTION_RE = re.compile(r"(?<!\w)@\w+")
CAPS_WORD_RE = re.compile(r"\b[A-Z][A-Z']{2,}\b")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ------------------------------------------------------------------ fetching
def slices(start: date, end: date, days: int) -> list[tuple[date, date]]:
    out, cur = [], start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        out.append((cur, stop))
        cur = stop + timedelta(days=1)
    return out


def fetch_posts(fk_slug: str, token: str, cfg: dict, end: date | None = None,
                session: requests.Session | None = None) -> list[dict]:
    fk = cfg["fanpagekarma"]
    f = cfg["fields"]["fanpagekarma"]
    end = end or datetime.now(timezone.utc).date()
    start = end - timedelta(days=fk["history_days"] - 1)
    s = session or requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    url = f"{fk['base_url'].rstrip('/')}/facebook/{fk_slug}/posts"
    seen, posts = set(), []
    for a, b in slices(start, end, fk["slice_days"]):
        period = fk["period_format"].format(start=a.isoformat(), end=b.isoformat())
        for attempt in range(5):
            r = s.get(url, params={fk["period_param"]: period}, timeout=120)
            if r.status_code == 429:
                time.sleep(min(60 * (attempt + 1), 300))
                continue
            r.raise_for_status()
            break
        payload = r.json()
        batch = pick(payload, f["list"]) if isinstance(payload, dict) else payload
        batch = batch or []
        if len(batch) >= 1000:
            print(f"warning: {period} returned {len(batch)} posts (cap). Reduce slice_days.")
        for p in batch:
            n = normalise_fk(p, f)
            if n and n["key"] not in seen:
                seen.add(n["key"])
                posts.append(n)
        time.sleep(fk["min_seconds_between_requests"])
    return posts


def fetch_posts_graph(page_token: str, cfg: dict, end: date | None = None,
                      session: requests.Session | None = None) -> tuple[list[dict], dict]:
    """Page history from the official Graph API using the page's own access token (the same token
    publishing will need). No scraping. Returns (posts, page_meta)."""
    g = cfg["page_dna"]["graph"]
    f = cfg["fields"]["fb_posts"]
    s = session or requests.Session()
    base = f"https://graph.facebook.com/{g['version']}"
    me = s.get(f"{base}/me", params={"fields": "id,name,link", "access_token": page_token}, timeout=60)
    me.raise_for_status()
    meta = me.json()
    end = end or datetime.now(timezone.utc).date()
    start = end - timedelta(days=cfg["page_dna"]["history_days"] - 1)
    url = f"{base}/{meta['id']}/posts"
    params = {"fields": g["fields"], "since": start.isoformat(),
              "until": (end + timedelta(days=1)).isoformat(), "limit": 100, "access_token": page_token}
    seen, posts = set(), []
    while url:
        r = s.get(url, params=params, timeout=120)
        r.raise_for_status()
        payload = r.json()
        for p in payload.get("data", []):
            n = normalise_fb(p, f)
            if n and n["key"] not in seen:
                seen.add(n["key"])
                posts.append(n)
        url, params = payload.get("paging", {}).get("next"), None
    return posts, {"name": meta.get("name"), "url": meta.get("link"), "id": meta.get("id")}


def load_posts_file(path: str | Path, cfg: dict) -> tuple[list[dict], dict]:
    """Posts from a Meta Business Suite export (CSV) or a JSON list, mapped through
    fields.fb_posts. Returns (posts, page_meta)."""
    import csv
    path = Path(path)
    f = cfg["fields"]["fb_posts"]
    if path.suffix.lower() == ".json":
        rows = json.loads(path.read_text())
    else:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    seen, posts = set(), []
    for row in rows:
        n = normalise_fb(row, f)
        if n and n["key"] not in seen:
            seen.add(n["key"])
            posts.append(n)
    meta = {"name": pick(rows[0], f["page_name"]) if rows else None,
            "url": pick(rows[0], f["page_url"]) if rows else None}
    return posts, meta


def normalise_fb(p: dict, f: dict) -> dict | None:
    t = parse_ts(pick(p, f["created_at"]))
    if t is None:
        return None
    reactions, comments, shares = (to_int(pick(p, f["reactions"])), to_int(pick(p, f["comments"])),
                                   to_int(pick(p, f["shares"])))
    if not (reactions or comments or shares):
        reactions = to_int(pick(p, f.get("interactions", [])))
    url = pick(p, f["url"]) or ""
    media_type = str(pick(p, f["media_type"]) or "")
    if "/reel/" in url:
        kind = "reel"
    elif pick(p, f["is_video"]) or "video" in media_type.lower():
        kind = "video"
    else:
        kind = normalise_type(media_type, pick(p, f["link"]) or "")
        if kind not in ("photo", "video", "reel", "link"):
            kind = "text"
    msg = pick(p, f["message"]) or ""
    return {"key": str(pick(p, f["id"]) or url or f"{t}|{msg[:40]}"), "ts": t, "message": msg,
            "type": kind, "interactions": reactions + comments + shares, "url": url}


def normalise_fk(p: dict, f: dict) -> dict | None:
    t = parse_ts(pick(p, f["created_at"]))
    if t is None:
        return None
    reactions, comments, shares = (to_int(pick(p, f["reactions"])), to_int(pick(p, f["comments"])),
                                   to_int(pick(p, f["shares"])))
    interactions = to_int(pick(p, f["interactions"])) or reactions + comments + shares
    msg = pick(p, f["message"]) or ""
    return {"key": str(pick(p, f["id"]) or f"{t}|{msg[:40]}"), "ts": t, "message": msg,
            "type": normalise_type(pick(p, f["type"]) or "", pick(p, f["link"]) or ""),
            "interactions": interactions}


def normalise_type(t: str, link: str) -> str:
    t = t.lower()
    for key, name in (("share", "link"), ("genericattachment", "link"), ("externalurl", "link"), ("reel", "reel"), ("video", "video"), ("live", "video"), ("photo", "photo"),
                      ("image", "photo"), ("album", "photo"), ("link", "link"), ("status", "text"),
                      ("text", "text")):
        if key in t:
            return name
    return "link" if link and "facebook.com" not in link else (t or "other")


# ------------------------------------------------------------------ features
def features(msg: str) -> dict:
    text_wo_urls = URL_RE.sub("", msg)
    caps = [w for w in CAPS_WORD_RE.findall(text_wo_urls) if w.strip("'") not in SPORTS_ACRONYMS]
    lines = [ln for ln in msg.splitlines() if ln.strip()]
    first = msg.strip()[:2]
    return {
        "hashtag": bool(HASHTAG_RE.search(msg)),
        "emoji": bool(EMOJI_RE.search(msg)),
        "question": "?" in text_wo_urls,
        "exclamation": "!" in text_wo_urls,
        "url_in_caption": bool(URL_RE.search(msg)),
        "mention": bool(MENTION_RE.search(msg)),
        "all_caps_word": bool(caps),
        "multi_line": len(lines) > 1,
        "starts_with_emoji": bool(EMOJI_RE.match(first)),
        "starts_with_breaking": msg.strip().upper().startswith(("BREAKING", "JUST IN", "REPORT")),
        "quote_marks": bool(re.search(r"[\"“”]", msg)),
        "empty_caption": not msg.strip(),
    }


def median(xs):
    return statistics.median(xs) if xs else 0


def pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def lift(with_: list[int], without: list[int]) -> float | None:
    if len(with_) < 5 or len(without) < 5:
        return None
    a, b = median(with_), median(without)
    return round((a + 1) / (b + 1), 2)


def analyse(posts: list[dict], tz_name: str, min_cell: int = 10) -> dict:
    tz = ZoneInfo(tz_name)
    n = len(posts)
    if not n:
        return {"posts": 0}
    days = max((max(p["ts"] for p in posts) - min(p["ts"] for p in posts)) / 86400, 1)
    hours, weekdays = Counter(), Counter()
    fmt = Counter(p["type"] for p in posts)
    by_fmt = defaultdict(list)
    feats = []
    emojis, tags = Counter(), Counter()
    lengths, words, emoji_counts, tag_counts, caps_ratio = [], [], [], [], []
    for p in posts:
        dt = datetime.fromtimestamp(p["ts"], tz)
        hours[dt.hour] += 1
        weekdays[dt.weekday()] += 1
        by_fmt[p["type"]].append(p["interactions"])
        fe = features(p["message"])
        feats.append(fe)
        msg = p["message"]
        lengths.append(len(msg))
        words.append(len(msg.split()))
        e = EMOJI_RE.findall(msg)
        emojis.update(e)
        emoji_counts.append(len(e))
        h = [t.lower() for t in HASHTAG_RE.findall(msg)]
        tags.update(h)
        tag_counts.append(len(h))
        alpha = [w for w in re.findall(r"\b[A-Za-z]{3,}\b", URL_RE.sub("", msg))]
        caps = [w for w in alpha if w.isupper() and w not in SPORTS_ACRONYMS]
        caps_ratio.append(len(caps) / len(alpha) if alpha else 0)

    # Engagement by format: usually the most actionable finding.
    format_eng = {f: {"posts": len(v), "share_pct": pct(len(v), n), "median_interactions": median(v),
                      "mean_interactions": round(sum(v) / len(v), 1)}
                  for f, v in sorted(by_fmt.items(), key=lambda kv: -len(kv[1]))}

    # Caption devices: raw lift vs lift WITHIN each format, and a format-stratified pooled lift.
    devices = {}
    for dev in feats[0]:
        users = sum(1 for fe in feats if fe[dev])
        raw = lift([p["interactions"] for p, fe in zip(posts, feats) if fe[dev]],
                   [p["interactions"] for p, fe in zip(posts, feats) if not fe[dev]])
        within, logs, weights = {}, [], []
        for f in by_fmt:
            yes = [p["interactions"] for p, fe in zip(posts, feats) if p["type"] == f and fe[dev]]
            no = [p["interactions"] for p, fe in zip(posts, feats) if p["type"] == f and not fe[dev]]
            if len(yes) >= min_cell and len(no) >= min_cell:
                l = lift(yes, no)
                within[f] = {"lift": l, "n_with": len(yes), "n_without": len(no)}
                logs.append(math.log(l))
                weights.append(min(len(yes), len(no)))
        stratified = round(math.exp(sum(l * w for l, w in zip(logs, weights)) / sum(weights)), 2) \
            if weights else None
        confounded = bool(raw and stratified and (max(raw, stratified) / min(raw, stratified) > 1.5))
        devices[dev] = {"usage_pct": pct(users, n), "raw_lift": raw, "within_format": within,
                        "stratified_lift": stratified, "confounded_by_format": confounded}

    never = [d for d, v in devices.items() if v["usage_pct"] < 2 and d != "empty_caption"]
    never += [f"format:{f}" for f in ("video", "reel", "photo", "link", "text") if f not in fmt]

    return {
        "posts": n,
        "days_covered": round(days, 1),
        "posts_per_day": round(n / days, 1),
        "timezone": tz_name,
        "hour_distribution_pct": {h: pct(hours[h], n) for h in range(24)},
        "weekday_distribution_pct": {WEEKDAYS[d]: pct(weekdays[d], n) for d in range(7)},
        "format_mix_pct": {f: pct(c, n) for f, c in fmt.most_common()},
        "format_engagement": format_eng,
        "caption": {
            "length_chars_median": median(lengths),
            "length_chars_p25_p75": quantiles(lengths),
            "words_median": median(words),
            "emoji_per_post_mean": round(sum(emoji_counts) / n, 2),
            "hashtags_per_post_mean": round(sum(tag_counts) / n, 2),
            "all_caps_word_ratio_mean_pct": round(100 * sum(caps_ratio) / n, 1),
            "top_emojis": emojis.most_common(12),
            "top_hashtags": tags.most_common(15),
        },
        "devices": devices,
        "never_does": never,
        "engagement_drivers": drivers(devices, format_eng),
    }


def quantiles(xs):
    if len(xs) < 4:
        return [min(xs, default=0), max(xs, default=0)]
    q = statistics.quantiles(xs, n=4)
    return [round(q[0]), round(q[2])]


def drivers(devices: dict, format_eng: dict) -> list[str]:
    out = []
    fmts = [(f, v) for f, v in format_eng.items() if v["posts"] >= 10]
    if len(fmts) >= 2:
        best = max(fmts, key=lambda kv: kv[1]["median_interactions"])
        worst = min(fmts, key=lambda kv: kv[1]["median_interactions"])
        out.append(f"{best[0]} posts: median {best[1]['median_interactions']} interactions vs "
                   f"{worst[1]['median_interactions']} for {worst[0]} posts "
                   f"({worst[0]} = {worst[1]['share_pct']}% of output)")
    for dev, v in devices.items():
        if v["stratified_lift"] is None:
            continue
        if v["stratified_lift"] >= 1.15 or v["stratified_lift"] <= 0.87:
            note = f"{dev}: within-format lift x{v['stratified_lift']}"
            if v["confounded_by_format"]:
                note += f" (raw x{v['raw_lift']} is inflated by format mix; do not use raw)"
            out.append(note)
        elif v["confounded_by_format"]:
            out.append(f"{dev}: raw x{v['raw_lift']} but ~no effect within format "
                       f"(x{v['stratified_lift']}); a format effect in disguise")
    return out


def render_markdown(name: str, dna: dict) -> str:
    if not dna.get("posts"):
        return f"# {name}: no posts returned\n"
    c = dna["caption"]
    peak_hours = sorted(dna["hour_distribution_pct"].items(), key=lambda kv: -kv[1])[:5]
    lines = [
        f"# Page DNA: {name}",
        "",
        f"{dna['posts']} posts over {dna['days_covered']} days, **{dna['posts_per_day']}/day**. "
        f"Times in {dna['timezone']}.",
        "",
        "## Engagement drivers (format-controlled)",
        *[f"- {d}" for d in dna["engagement_drivers"]],
        "",
        "## Format mix and engagement",
        "| format | share | posts | median interactions |",
        "|---|---|---|---|",
        *[f"| {f} | {v['share_pct']}% | {v['posts']} | {v['median_interactions']} |"
          for f, v in dna["format_engagement"].items()],
        "",
        "## When it posts",
        "Peak hours: " + ", ".join(f"{h:02d}:00 ({p}%)" for h, p in peak_hours),
        "",
        "Weekdays: " + ", ".join(f"{d} {p}%" for d, p in dna["weekday_distribution_pct"].items()),
        "",
        "## Caption style",
        f"- Length: median {c['length_chars_median']} chars (IQR {c['length_chars_p25_p75']}), "
        f"{c['words_median']} words",
        f"- Emoji per post: {c['emoji_per_post_mean']}; top: {' '.join(e for e, _ in c['top_emojis'][:8])}",
        f"- Hashtags per post: {c['hashtags_per_post_mean']}; top: "
        f"{', '.join(t for t, _ in c['top_hashtags'][:8])}",
        f"- ALL-CAPS words (sports acronyms excluded): {c['all_caps_word_ratio_mean_pct']}% of words",
        "",
        "## Caption devices",
        "| device | usage | raw lift | within-format lift | confounded? |",
        "|---|---|---|---|---|",
        *[f"| {d} | {v['usage_pct']}% | {v['raw_lift']} | {v['stratified_lift']} | "
          f"{'YES' if v['confounded_by_format'] else ''} |" for d, v in dna["devices"].items()],
        "",
        "## Never does",
        *([f"- {x}" for x in dna["never_does"]] or ["- (nothing below 2% usage)"]),
        "",
    ]
    return "\n".join(lines)


def build_dna(page, cfg: dict, out_dir: Path, source: str, *, file: str | None = None,
              token: str | None = None, posts: list[dict] | None = None) -> dict:
    """source: file (Business Suite export) | graph (page token) | fanpagekarma | cached."""
    meta = {}
    if posts is None:
        if source == "file":
            posts, meta = load_posts_file(file, cfg)
        elif source == "graph":
            posts, meta = fetch_posts_graph(token, cfg)
        else:
            posts = fetch_posts(page.fk_slug or page.slug, token, cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "posts.json").write_text(json.dumps(posts))
    dna = analyse(posts, page.timezone)
    dna["page_meta"] = meta
    (out_dir / "dna.json").write_text(json.dumps(dna, indent=2, ensure_ascii=False))
    (out_dir / "dna.md").write_text(render_markdown(page.name, dna))
    return dna
