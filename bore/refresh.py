"""Re-measure engagement before scoring.

The collector captures posts minutes after publication, when engagement is near zero (measured:
33 at 1.2 h, 1,687 at 6.5 h, 15,000+ by 18 h). Scoring capture-time figures ranks fresh breaking
news lowest. Current counts go into an overlay applied at read time; counts never decrease."""
from __future__ import annotations

import time

from .collect import batch_from_queries, normalise_reddit, normalise_x
from .config import Page
from .store import Store


def select_for_refresh(items: list[dict], cfg: dict, now: float) -> list[dict]:
    r = cfg["refresh"]
    min_age = r["min_age_minutes"] * 60
    min_gap = r["min_interval_hours"] * 3600
    max_age = cfg["clustering"]["max_age_hours"] * 3600
    picked = []
    for it in items:
        if it["platform"] not in r.get("platforms", ["x", "reddit"]) or it.get("is_repost"):
            continue
        created = it.get("created_at") or it.get("collected_at")
        age = now - created
        if age < min_age or age > max_age:          # too young to have moved / too old to matter
            continue
        if it.get("refreshed_at") and now - it["refreshed_at"] < min_gap:
            continue
        picked.append(it)
    # Largest current engagement first (those drive rankings), then youngest.
    picked.sort(key=lambda i: (-(max(i.get("engagement") or 0, i.get("overlay_eng") or 0)),
                               -(i.get("created_at") or 0)))
    return picked[: r["max_items_per_run"]]


def refresh(apify, store: Store, pages: list[Page], cfg: dict, now: float | None = None) -> dict:
    now = now or time.time()
    since = now - cfg["clustering"]["max_age_hours"] * 3600
    targets = select_for_refresh(store.items_since(since), cfg, now)
    by_native = {(t["platform"], t["native_id"]): t for t in targets if t.get("native_id")}
    stats = {"selected": len(targets), "updated": 0}

    # X: re-run the proven search form over just the authors whose posts need re-measuring,
    # bounded by since_time to the oldest target.
    x_targets = [t for t in targets if t["platform"] == "x"]
    if x_targets:
        xc = cfg["apify"]["x"]
        authors = sorted({t["author"] for t in x_targets})
        start = min(t.get("created_at") or t["collected_at"] for t in x_targets) - 60
        for q in batch_from_queries(authors, xc["query_max_chars"]):
            run_input = dict(xc["input"])
            run_input[xc["search_terms_key"]] = [q]
            run_input[xc["since_key"]] = str(int(start))
            run_input[xc["max_items_key"]] = cfg["refresh"].get("x_max_items", xc["max_items_per_query"])
            raw, _ = apify.run(xc["actor"], run_input, xc["timeout_seconds"])
            for r in raw:
                n = normalise_x(r, cfg["fields"]["x"], now)
                if n and ("x", n["native_id"]) in by_native:
                    store.overlay_upsert(n["id"], n["engagement"], now)
                    stats["updated"] += 1

    # Reddit: re-read the hot listing of the subreddits involved.
    r_targets = [t for t in targets if t["platform"] == "reddit"]
    if r_targets:
        rc = cfg["apify"]["reddit"]
        subs = sorted({t["subreddit"] for t in r_targets if t.get("subreddit")})
        run_input = dict(rc["input"])
        run_input[rc["urls_key"]] = [f"https://www.reddit.com/r/{s}/" for s in subs]
        run_input[rc["max_posts_key"]] = max(rc["max_posts_per_subreddit"], 25)
        raw, _ = apify.run(rc["actor"], run_input, rc["timeout_seconds"])
        wanted = {t["id"] for t in r_targets}
        for r in raw:
            n = normalise_reddit(r, cfg["fields"]["reddit"], now)
            if n and n["id"] in wanted:
                store.overlay_upsert(n["id"], n["engagement"], now)
                stats["updated"] += 1

    # Mark everything we attempted so it is not re-selected within the interval, without
    # lowering any count (overlay_upsert floors at the known value).
    for t in targets:
        row = store.db.execute("SELECT refreshed_at FROM overlay WHERE item_id=?", (t["id"],)).fetchone()
        if not row or row["refreshed_at"] < now:
            store.overlay_upsert(t["id"], 0, now)
    store.commit()
    return stats
