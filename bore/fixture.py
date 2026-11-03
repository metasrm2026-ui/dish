"""Labelled-fixture evaluation and join-threshold sweep.

Fixture JSON:
  {"page": {...Page fields, incl. tier_one/officials...},
   "stories": [{"name": str, "items": [item ids], "single_source_major": bool}],
   "items": [{id, platform, author, text, ..., created_at | age_hours, engagement}]}

Assertions: each labelled story lands in exactly one cluster; distinct stories never share a
cluster; single-source majors survive selection (scoop route)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .cluster import cluster_items
from .config import Page
from .select import select_page


def load(path: str | Path, now: float | None = None) -> dict:
    now = now or time.time()
    data = json.loads(Path(path).read_text())
    for it in data["items"]:
        if "age_hours" in it:
            it["created_at"] = now - it.pop("age_hours") * 3600
        it.setdefault("collected_at", it.get("created_at") or now)
        it.setdefault("engagement", 0)
    return data


def evaluate(data: dict, cfg: dict, threshold: float | None = None, now: float | None = None) -> list[str]:
    now = now or time.time()
    if threshold is not None:
        cfg = {**cfg, "clustering": {**cfg["clustering"], "join_threshold": threshold}}
    clusters, _ = cluster_items(data["items"], cfg, now)
    where = {}
    for ci, cl in enumerate(clusters):
        for m in cl.members:
            where[m["id"]] = ci
    failures = []
    story_cluster = {}
    for st in data["stories"]:
        cids = {where.get(i) for i in st["items"]}
        if None in cids:
            failures.append(f"{st['name']}: item filtered out")
        cids.discard(None)
        if len(cids) > 1:
            failures.append(f"{st['name']}: split across {len(cids)} clusters")
        story_cluster[st["name"]] = cids
    names = list(story_cluster)
    for a in range(len(names)):
        for b in range(a + 1, len(names)):
            if story_cluster[names[a]] & story_cluster[names[b]]:
                failures.append(f"merged: {names[a]} + {names[b]}")
    majors = [st for st in data["stories"] if st.get("single_source_major")]
    if majors:
        page = Page.from_dict(data.get("page", {"slug": "fixture", "name": "fixture"}))
        res = select_page(page, data["items"], cfg, [], now)
        kept = {m["id"] for t in res.trends for m in t.cluster.members}
        for st in majors:
            if not set(st["items"]) & kept:
                failures.append(f"{st['name']}: single-source major did not survive")
    return failures


def sweep(paths, cfg: dict, lo: float = 0.20, hi: float = 0.95, step: float = 0.05):
    """Sweep the join threshold; a threshold passes only if EVERY fixture passes."""
    now = time.time()
    paths = [paths] if isinstance(paths, (str, Path)) else list(paths)
    datas = [(Path(p).name, load(p, now)) for p in paths]
    out = []
    thr = lo
    while thr <= hi + 1e-9:
        f = [f"{name}: {x}" for name, d in datas for x in evaluate(d, cfg, round(thr, 2), now)]
        out.append((round(thr, 2), not f, f))
        thr += step
    passing = [t for t, ok, _ in out if ok]
    if passing:
        print(f"tightest passing join_threshold: {max(passing):.2f} "
              f"(passing range {min(passing):.2f}-{max(passing):.2f})")
    else:
        print("no threshold passes: inspect failures")
    return out
