"""Global collection: one actor run covers the union of every page's sources, then each item is
routed to every page that lists its author / subreddit / search."""
from __future__ import annotations

import re
import time
from collections import defaultdict
from urllib.parse import urlsplit

from .apify import BudgetExceeded
from .config import Page
from .store import Store
from .textutil import domain, get_path, identity, parse_ts, pick, to_int


# --------------------------------------------------------------- query building
def batch_from_queries(handles: list[str], max_chars: int = 460) -> list[str]:
    """`from:a OR from:b ...` batches, each under max_chars."""
    batches, cur = [], ""
    for h in sorted(set(handles)):
        part = f"from:{h}"
        cand = part if not cur else f"{cur} OR {part}"
        if cur and len(cand) > max_chars:
            batches.append(cur)
            cur = part
        else:
            cur = cand
    if cur:
        batches.append(cur)
    return batches


def chunk_handles(handles: list[str], max_chars: int = 460) -> list[list[str]]:
    """Split handles, in the given order, into groups whose `from:` query fits max_chars."""
    groups, cur, size = [], [], 0
    for h in dict.fromkeys(handles):
        add = len(f"from:{h}") + (4 if cur else 0)
        if cur and size + add > max_chars:
            groups.append(cur)
            cur, size = [], 0
            add = len(f"from:{h}")
        cur.append(h)
        size += add
    if cur:
        groups.append(cur)
    return groups


def rotate(store: Store, key: str, items: list, n: int) -> list:
    """The next n items of a list, round-robin across runs (position kept in the store)."""
    if not items or n <= 0:
        return []
    start = store.get_kv(f"rotation:{key}", 0) % len(items)
    n = min(n, len(items))
    out = [items[(start + i) % len(items)] for i in range(n)]
    store.set_kv(f"rotation:{key}", (start + n) % len(items))
    return out


def collection_window(store: Store, source: str, now: float, cfg: dict) -> float:
    """Start of the window. A missed hour must not lose data: widen to cover the gap since the
    last success, capped at max_backfill_hours."""
    c = cfg["collection"]
    default_start = now - c["default_window_hours"] * 3600
    cap_start = now - c["max_backfill_hours"] * 3600
    state = store.get_state(source)
    if state and state.get("last_success"):
        start = min(default_start, state["last_success"] - c["overlap_minutes"] * 60)
    else:
        start = default_start
    return max(start, cap_start)


# ------------------------------------------------------------------ normalisers
X_URL_RE = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/", re.I)


def normalise_x(raw: dict, f: dict, now: float) -> dict | None:
    native = pick(raw, f["id"])
    author = (pick(raw, f["author"]) or "").lower().lstrip("@")
    if not author:  # the actor's default output leaves author empty; the URL always carries it
        m = X_URL_RE.search(pick(raw, f["url"]) or "")
        author = m.group(1).lower() if m else ""
    if not native or not author:
        return None
    is_repost = bool(pick(raw, f["is_repost"]))
    original_author = (pick(raw, f.get("original_author", [])) or "").lower().lstrip("@") or None
    text = pick(raw, f["text"]) or ""
    if is_repost:
        text = pick(raw, f.get("original_text", [])) or text
        if text.startswith("RT @") and ":" in text:
            if not original_author:
                original_author = text[4:text.index(":")].lower()
            text = text[text.index(":") + 1:].strip()
    likes, reposts = to_int(pick(raw, f["likes"])), to_int(pick(raw, f["reposts"]))
    replies, quotes = to_int(pick(raw, f["replies"])), to_int(pick(raw, f["quotes"]))
    # A repost has no likes of its own: it corroborates but must not inflate engagement.
    engagement = 0 if is_repost else likes + reposts + replies + quotes
    url = pick(raw, f["url"]) or f"https://x.com/{author}/status/{native}"
    media = []
    for m in pick(raw, f.get("media", [])) or []:
        if isinstance(m, dict) and (m.get("mediaUrl") or m.get("media_url_https")):
            media.append({"url": m.get("mediaUrl") or m.get("media_url_https"), "type": m.get("type", "photo"),
                          "w": to_int(m.get("width") or (m.get("original_info") or {}).get("width")),
                          "h": to_int(m.get("height") or (m.get("original_info") or {}).get("height")),
                          **({"tco": m["url"]} if str(m.get("url") or "").startswith("https://t.co/") else {})})
    # links to articles (not the media's own t.co): their lead photo is often the best photo
    media_links = {m.get("tco") for m in media}
    links = [u for u in dict.fromkeys(re.findall(r"https://t\.co/\w+", text)) if u not in media_links]
    return {
        "id": identity("x", str(native)), "platform": "x", "native_id": str(native), "url": url,
        "author": author, "original_author": original_author if is_repost else None,
        "is_repost": is_repost, "text": text, "created_at": parse_ts(pick(raw, f["created_at"])),
        "collected_at": now, "engagement": engagement,
        "metrics": {"likes": likes, "reposts": reposts, "replies": replies, "quotes": quotes, "media": media,
                    **({"links": links[:3]} if links else {})},
    }


def normalise_reddit(raw: dict, f: dict, now: float) -> dict | None:
    native = pick(raw, f["id"])
    url = pick(raw, f["url"]) or ""
    if url.startswith("/"):
        url = "https://www.reddit.com" + url
    if not native and not url:
        return None
    sub = (pick(raw, f["subreddit"]) or "").lower().removeprefix("r/")
    score, comments = to_int(pick(raw, f["score"])), to_int(pick(raw, f["comments"]))
    # every candidate in turn (a link post's url_overridden_by_dest is its article, not an image),
    # then a gallery's first image; a link post keeps its article (the news the post shares)
    image = next((u for u in (_reddit_image(get_path(raw, p)) for p in f.get("image", [])) if u), None) \
        or _reddit_gallery_image(raw)
    link = next((u for u in (_reddit_link(get_path(raw, p)) for p in f.get("link", [])) if u), None)
    return {
        "id": identity("reddit", str(native) if native else None, url), "platform": "reddit",
        "native_id": str(native) if native else None, "url": url,
        "author": (pick(raw, f["author"]) or "").lower() or None, "subreddit": sub,
        "title": pick(raw, f["title"]) or "", "text": (pick(raw, f["text"]) or "")[:2000],
        "created_at": parse_ts(pick(raw, f["created_at"])), "collected_at": now,
        "engagement": score + comments,
        "metrics": {"score": score, "comments": comments, "image": image, **({"link": link} if link else {})},
    }


NOT_ARTICLE_HOSTS = ("reddit.com", "redd.it", "imgur.com", "x.com", "twitter.com", "youtube.com", "youtu.be",
                     "instagram.com", "tiktok.com", "facebook.com", "threads.net", "threads.com", "streamable.com")


def _reddit_link(url) -> str | None:
    """The article a link post shares (Oct 21: every r/news item was stored as a bare title)."""
    if not isinstance(url, str) or not url.startswith("http") or _reddit_image(url):
        return None
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    return None if any(host == h or host.endswith("." + h) for h in NOT_ARTICLE_HOSTS) else url


def _reddit_gallery_image(raw: dict) -> str | None:
    """A gallery's (or a text post's inline) first image, full size from i.redd.it."""
    meta = raw.get("media_metadata") if isinstance(raw.get("media_metadata"), dict) else {}
    order = [it.get("media_id") for it in ((raw.get("gallery_data") or {}).get("items") or []) if isinstance(it, dict)]
    for mid in order + [k for k in meta if k not in order]:
        m = meta.get(mid) or {}
        if m.get("status", "valid") == "valid" and m.get("e", "Image") == "Image":
            ext = (m.get("m") or "image/jpg").split("/")[-1].replace("jpeg", "jpg")
            return f"https://i.redd.it/{mid}.{ext}" if ext in ("jpg", "png", "webp") else None
    for g in raw.get("gallery_images") or []:
        u = _reddit_image(g if isinstance(g, str) else (g or {}).get("url") or (g or {}).get("u"))
        if u:
            return u
    return None


def _reddit_image(url) -> str | None:
    if not url or not isinstance(url, str):
        return None
    url = url.replace("&amp;", "&")
    return url if ("i.redd.it" in url or "preview.redd.it" in url or
                   re.search(r"\.(jpe?g|png|webp)(\?|$)", url, re.I)) else None


def normalise_web(raw: dict, f: dict, now: float, query: str | None = None) -> dict | None:
    url = pick(raw, f["url"])
    if not url:
        return None
    return {
        "id": identity("web", url=url), "platform": "web", "url": url, "author": domain(url),
        "title": pick(raw, f["title"]) or "", "text": pick(raw, f["text"]) or "", "query": query,
        "created_at": parse_ts(pick(raw, f.get("created_at", []))), "collected_at": now,
        "engagement": 0, "metrics": {},
    }


def keep_web(url: str, wc: dict) -> bool:
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    if any(host == d or host.endswith("." + d) for d in wc.get("exclude_domains", [])):
        return False
    if wc.get("drop_homepages") and parts.path.strip("/") == "":
        return False
    return True


SECTION_TITLE_RE = re.compile(
    r"(news\s*(&|and)\s*(analysis|stats|scores|rumors)|scores,\s*news|articles filed under|latest .{0,20}news\b"
    r"|^(nfl|nba|mlb|nhl|celebrity|entertainment|tv|movies|music)( news)?$|\|\s*pro sports\b)", re.I)


def section_page(item: dict) -> bool:
    """Search results that are a site's section/index page ("NFL Scores, News & Stats | ..."),
    not a story. They reached the shortlist on Sep 29."""
    title = (item.get("title") or "").strip()
    head = re.split(r"\s[|\-–]\s", title)[0].strip()
    return bool(SECTION_TITLE_RE.search(title)) or len(head.split()) < 3


SERVICE_RX = re.compile(
    r"\b(?:(?:where|how) to (?:watch|stream)|cast guide|meet (?:all |the (?:full |main )?)?(?:the )?cast|"
    r"everything (?:we know|to know)|ending,? explained|release date and time|"
    r"(?:episode \d+|premiere|finale|season \d+) recap)\b"
    # Oct 12-13, same site: the routine skipped every one of these too (none was ever posted)
    r"|\b(?:release (?:schedule|time)|episode schedule|renewal status|is there going to be|what time (?:is|does)|"
    r"meet (?:the )?(?:singles|contestants|islanders|housemates|queens)|complete guide|details to know|"
    r"moon phase|phase,? visibility|wordle|connections hints|crossword (?:answers?|clues?))\b"
    r"|\bwhen (?:is|does|will) .{1,60}\b(?:come out|premiere|release|return|arrive|air|launch\w*|stream\w*)\b"
    r"|\bis .{1,60}\bbased on (?:a |the )?(?:true story|novel|book|real)"
    r"|\bwhat is .{1,60}\b(?:all )?about\?"
    r"|\b(?:twist|fate|finale|ending)\b.{0,40}\bexplained\b|\bseason \d+(?:'|’)s (?:ending|finale)\b"
    r"|\bwhen to (?:watch|see|stream)\b"
    r"|^(?:the )?\d+ (?:best\b|(?:movies|shows|series|films|books|songs|albums|episodes) to (?:watch|read|stream|binge))",
    re.I)


def service_piece(item: dict) -> bool:
    """A website's service article (where to watch, cast guide, recap, explainer, release schedule,
    listicle, daily almanac), not news."""
    return item.get("platform") == "web" and bool(SERVICE_RX.search(item.get("title") or ""))


def expand_web(dataset: list[dict], f: dict, now: float, wc: dict | None = None) -> list[dict]:
    out = []
    for page_result in dataset:
        q = pick(page_result, f.get("query", []))
        results = pick(page_result, f.get("list", []))
        for r in results if isinstance(results, list) else [page_result]:
            n = normalise_web(r, f, now, q if isinstance(q, str) else None)
            if n and (wc is None or keep_web(n["url"], wc)) and not section_page(n):
                out.append(n)
    return out


# --------------------------------------------------------------------- routing
def route_map(pages: list[Page]) -> tuple[dict, dict]:
    by_handle, by_sub = defaultdict(set), defaultdict(set)
    for p in pages:
        for h in p.x_handles + p.x_handles_secondary:
            by_handle[h].add(p.slug)
        for s in p.subreddits + p.subreddits_rotation:
            by_sub[s].add(p.slug)
    return by_handle, by_sub


def routes_for(item: dict, by_handle: dict, by_sub: dict) -> list[tuple[str, str]]:
    out = set()
    if item["platform"] == "x":
        for who in (item.get("author"), item.get("original_author")):
            for page in by_handle.get(who or "", ()):
                out.add((page, f"x:{who}"))
    elif item["platform"] == "reddit":
        for page in by_sub.get(item.get("subreddit") or "", ()):
            out.add((page, f"r/{item['subreddit']}"))
    return sorted(out)


# ------------------------------------------------------------------ collectors
def collect_x(apify, store: Store, pages: list[Page], cfg: dict, now: float | None = None,
              hours: float | None = None) -> int:
    now = now or time.time()
    xc = cfg["apify"]["x"]
    start = collection_window(store, "x", now, cfg)
    if hours:
        start = min(start, now - min(hours, cfg["collection"]["max_backfill_hours"]) * 3600)
    handles = sorted({h for p in pages for h in p.x_handles})
    if not handles:
        return 0
    by_handle, by_sub = route_map(pages)
    total, errors = 0, []
    for q in batch_from_queries(handles, xc["query_max_chars"]):
        run_input = dict(xc["input"])
        run_input[xc["search_terms_key"]] = [q]
        run_input[xc["since_key"]] = str(int(start))
        run_input[xc["max_items_key"]] = xc["max_items_per_query"]
        try:
            raw, status = apify.run(xc["actor"], run_input, xc["timeout_seconds"])
        except Exception as e:  # budget guard, network: record and stop, data stays recoverable
            errors.append(str(e))
            break
        if status != "SUCCEEDED":
            errors.append(f"run {status} (salvaged {len(raw)})")
        total += ingest(store, [normalise_x(r, cfg["fields"]["x"], now) for r in raw],
                        by_handle, by_sub, min_created=start)
    store.commit()
    # Only advance last_success if every batch ran; otherwise the gap is re-covered next time.
    store.mark_attempt("x", "; ".join(errors) or None, total, None if errors else now)
    return total


def collect_x_secondary(apify, store: Store, pages: list[Page], cfg: dict, now: float | None = None) -> int:
    """Secondary X tier: one `from:` batch per run, rotating through every page's
    x_handles_secondary, capped at x.secondary_max_items tweets. A batch comes round again after
    (batches x x_secondary_every_hours), so its window covers that span (capped at backfill)."""
    now = now or time.time()
    xc = cfg["apify"]["x"]
    handles = list(dict.fromkeys(h for p in pages for h in p.x_handles_secondary))
    groups = chunk_handles(handles, xc["query_max_chars"])
    if not groups:
        return 0
    group = rotate(store, "x2", groups, 1)[0]
    span = len(groups) * cfg["schedule"]["x_secondary_every_hours"]
    start = now - min(span, cfg["collection"]["max_backfill_hours"]) * 3600
    by_handle, by_sub = route_map(pages)
    run_input = dict(xc["input"])
    run_input[xc["search_terms_key"]] = [" OR ".join(f"from:{h}" for h in group)]
    run_input[xc["since_key"]] = str(int(start))
    run_input[xc["max_items_key"]] = xc["secondary_max_items"]
    error = None
    try:
        raw, status = apify.run(xc["actor"], run_input, xc["timeout_seconds"])
        if status != "SUCCEEDED":
            error = f"run {status} (salvaged {len(raw)})"
    except BudgetExceeded:
        raise
    except Exception as e:
        raw, error = [], str(e)
    total = ingest(store, [normalise_x(r, cfg["fields"]["x"], now) for r in raw],
                   by_handle, by_sub, min_created=start)
    store.commit()
    store.mark_attempt("x2", error, total, now if not error else None)
    return total


def collect_reddit(apify, store: Store, pages: list[Page], cfg: dict, now: float | None = None) -> int:
    now = now or time.time()
    rc = cfg["apify"]["reddit"]
    subs = {s for p in pages for s in p.subreddits}
    for p in pages:  # a few rotating subreddits per pass, per page
        subs.update(rotate(store, f"reddit:{p.slug}", p.subreddits_rotation, rc.get("rotation_per_run", 0)))
    subs = sorted(subs)
    if not subs:
        return 0
    by_handle, by_sub = route_map(pages)
    run_input = dict(rc["input"])
    run_input[rc["urls_key"]] = [f"https://www.reddit.com/r/{s}/" for s in subs]
    run_input[rc["max_posts_key"]] = rc["max_posts_per_subreddit"]  # per subreddit URL
    error = None
    try:
        raw, status = apify.run(rc["actor"], run_input, rc["timeout_seconds"])
        if status != "SUCCEEDED":
            error = f"run {status} (salvaged {len(raw)})"
    except Exception as e:
        raw, error = [], str(e)
    cutoff = now - cfg["clustering"]["max_age_hours"] * 3600
    raw = [r for r in raw if not any(r.get(k) for k in rc.get("drop_if_true", []))]
    items = [normalise_reddit(r, cfg["fields"]["reddit"], now) for r in raw]
    bare = sum(1 for it in items if it and not (it["text"] or it["metrics"].get("image") or it["metrics"].get("link")))
    if bare:         # the field paths were mapped by hand: a title with nothing behind it is visible here
        print(f"reddit: {bare} of {len(items)} posts have no text, image or article link")
    total = ingest(store, items, by_handle, by_sub, min_created=cutoff)
    store.commit()
    store.mark_attempt("reddit", error, total, now if raw else None)
    return total


def on_story(item: dict) -> bool:
    """An escalation search result that is about the story it searched for: its title or snippet
    shares at least two of the query's words (a story signature)."""
    from .textutil import stem, token_set
    words = token_set(f"{item.get('title') or ''} {item.get('text') or ''}")
    q = (item.get("query") or "").lower().split()
    return sum(1 for w in q if w in words or stem(w) in words) >= min(2, len(q))


def collect_web(apify, store: Store, page_queries: dict[str, list[str]], cfg: dict,
                now: float | None = None, source: str = "web") -> int:
    """page_queries: {page_slug: [query, ...]}. Items are routed to the page that searched."""
    now = now or time.time()
    wc = cfg["apify"]["web"]
    q_to_pages = defaultdict(set)
    for page, qs in page_queries.items():
        for q in qs:
            q_to_pages[q.strip()].add(page)
    if not q_to_pages:
        return 0
    run_input = dict(wc["input"])
    run_input[wc["queries_key"]] = "\n".join(q_to_pages)
    error = None
    try:
        raw, status = apify.run(wc["actor"], run_input, wc["timeout_seconds"])
        if status != "SUCCEEDED":
            error = f"run {status} (salvaged {len(raw)})"
    except Exception as e:
        raw, error = [], str(e)
    total = 0
    for item in expand_web(raw, cfg["fields"]["web"], now, wc):
        if source == "web-escalation" and not on_story(item):
            continue                     # a search on a story's words found some other page
        pages = q_to_pages.get((item.get("query") or "").strip()) or set(page_queries)
        if store.upsert_item(item, [(p, f"web:{item.get('query') or ''}") for p in sorted(pages)]):
            total += 1
    store.commit()
    store.mark_attempt(source, error, total, now if raw else None)
    return total


def ingest(store: Store, items: list, by_handle: dict, by_sub: dict, min_created: float) -> int:
    new = 0
    for it in items:
        if not it:
            continue
        if it.get("created_at") and it["created_at"] < min_created:
            continue
        routes = routes_for(it, by_handle, by_sub)
        if routes and store.upsert_item(it, routes):
            new += 1
    return new
