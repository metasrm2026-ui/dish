"""Assume a page's sources from what its own posts are about.

Detects leagues and teams by keyword frequency across the page's post history, then assigns the
catalog's reporters (tier_one), official feeds, subreddits and web searches for those leagues,
plus team-specific searches. Everything assigned is recorded as assumed in `sources_origin`."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import yaml

from .config import ROOT, Page


def load_catalog(path: Path | None = None) -> dict:
    with open(path or ROOT / "config" / "source_catalog.yaml") as f:
        return yaml.safe_load(f)["leagues"]


def _pattern(keyword: str) -> re.Pattern:
    return re.compile(r"(?<![\w#])#?" + re.escape(keyword.lower()) + r"(?!\w)")


def detect(messages: list[str], catalog: dict) -> dict:
    """{league: {"posts": n, "share": pct, "teams": [(team, n), ...]}} sorted by posts."""
    texts = [m.lower() for m in messages if m]
    out = {}
    for league, spec in catalog.items():
        pats = {k: _pattern(k) for k in spec["keywords"]}
        team_hits, posts = Counter(), 0
        for t in texts:
            hit = [k for k, p in pats.items() if p.search(t)]
            if hit:
                posts += 1
                team_hits.update(hit)
        out[league] = {"posts": posts, "share": round(100 * posts / len(texts), 1) if texts else 0,
                       "teams": team_hits.most_common(5)}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["posts"]))


def choose_leagues(detected: dict, min_share: float = 5.0, rel: float = 0.35) -> list[str]:
    ranked = [(lg, d) for lg, d in detected.items() if d["posts"]]
    if not ranked:
        return []
    top = ranked[0][1]["posts"]
    return [lg for lg, d in ranked if d["share"] >= min_share and d["posts"] >= rel * top]


def assign(page: Page, detected: dict, catalog: dict, leagues: list[str] | None = None) -> Page:
    leagues = leagues if leagues is not None else choose_leagues(detected)

    def add(lst: list, vals, lower: bool = True):
        for v in vals:
            v = v.lower() if lower else v
            if v not in lst:
                lst.append(v)

    generic = {"nfl", "nba", "mlb", "nhl", "cfb", "mls", "playoffs", "super bowl", "world series",
               "stanley cup", "home run", "homer", "pitcher", "inning", "touchdown", "quarterback",
               "college football", "heisman", "transfer portal", "transfer news", "premier league",
               "champions league", "sec", "acc", "big ten", "big 12"}
    for lg in leagues:
        spec = catalog[lg]
        add(page.tier_one, spec["tier_one"])
        add(page.officials, spec["officials"])
        add(page.x_handles, spec["tier_one"] + spec["officials"])
        add(page.subreddits, spec["subreddits"])
        add(page.web_searches, spec["web_searches"], lower=False)
        add(page.exclude_terms, spec.get("exclude_terms", []))
        for team, n in detected[lg]["teams"][:3]:
            if team in generic or n < 3:
                continue
            name = team.title()
            add(page.web_searches, [f"{name} news today", f"{name} injury update"], lower=False)
    page.leagues = leagues
    page.sources_origin = ("assumed from page post history: "
                           + ", ".join(f"{catalog[l]['label']} {detected[l]['share']}%" for l in leagues)
                           if leagues else "no league detected; add sources by hand")
    return page
