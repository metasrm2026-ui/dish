"""Health: exit non-zero on anything actively losing data; warn on quiet failures.
A renamed or suspended handle looks exactly like a slow news day unless something checks."""
from __future__ import annotations

import time

from .config import Page
from .store import Store


def check(store: Store, pages: list[Page], cfg: dict, now: float | None = None) -> tuple[list[str], list[str]]:
    now = now or time.time()
    errors, warnings = [], []
    c = cfg["collection"]
    cap = c["max_backfill_hours"] * 3600
    # hourly sources are expected every hour; brief-time sources twice a day
    expected = {src: (c["default_window_hours"] if src in c["hourly_sources"] else 12)
                for src in ("x", "reddit", "web")}
    needed = {"x": any(p.x_handles for p in pages), "reddit": any(p.subreddits for p in pages),
              "web": any(p.web_searches for p in pages)}
    for src, hours in expected.items():
        if not needed[src]:
            continue
        st = store.get_state(src)
        if not st or not st.get("last_success"):
            errors.append(f"{src}: never succeeded")
            continue
        gap = now - st["last_success"]
        if gap > cap:
            errors.append(f"{src}: last success {gap / 3600:.1f}h ago, beyond the {cap / 3600:.0f}h "
                          "backfill cap: data is being lost")
        elif gap > (hours + 1) * 3600:
            warnings.append(f"{src}: last success {gap / 3600:.1f}h ago (backfill will cover it)")
        if st.get("last_error"):
            warnings.append(f"{src}: last error: {st['last_error']}")

    # Handles producing nothing.
    quiet_since = now - cfg["health"]["quiet_handle_hours"] * 3600
    active = {r[0] for r in store.db.execute(
        "SELECT DISTINCT author FROM items WHERE platform='x' AND collected_at>=?", (quiet_since,))}
    x_state = store.get_state("x")
    if x_state and x_state.get("last_success") and x_state["last_success"] >= quiet_since:
        for p in pages:
            silent = [h for h in p.x_handles if h not in active]
            if silent:
                warnings.append(f"{p.slug}: {len(silent)} handle(s) produced nothing in "
                                f"{cfg['health']['quiet_handle_hours']}h (renamed/suspended?): "
                                + ", ".join(silent[:15]))

    # Pages with no web items.
    day = now - 86400
    for p in pages:
        n = store.db.execute(
            "SELECT COUNT(*) FROM items i JOIN page_items pi ON pi.item_id=i.id"
            " WHERE pi.page=? AND i.platform='web' AND i.collected_at>=?", (p.slug, day)).fetchone()[0]
        if n == 0:
            warnings.append(f"{p.slug}: no web items in 24h")
        if not p.x_handles and not p.subreddits and not p.web_searches:
            errors.append(f"{p.slug}: no sources configured at all")

    # Empty engagement overlay (only real re-measurements count, not bookkeeping rows).
    real = store.db.execute(
        "SELECT COUNT(*) FROM overlay o JOIN items i ON i.id=o.item_id"
        " WHERE o.refreshed_at>=? AND o.engagement>i.engagement", (day,)).fetchone()[0]
    has_items = store.db.execute("SELECT COUNT(*) FROM items WHERE collected_at>=?", (day,)).fetchone()[0]
    if has_items and real == 0:
        warnings.append("engagement overlay: no re-measured counts in 24h; briefings are ranking "
                        "on capture-time engagement")
    return errors, warnings
