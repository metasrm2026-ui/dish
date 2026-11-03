"""Text clustering of a window of items into candidate trends.

Every rule here was added after a real failure:
  * reposts are kept; the original author counts as an independent source
  * a candidate joins on its similarity to the cluster's most similar MEMBER (not the token union,
    which makes a growing cluster ever harder to join)
  * it must also clear an average similarity to the whole cluster (~0.55 x threshold), which
    stops single-linkage chaining (A~B, B~C => A+C)
  * it must share at least two content tokens with that best member
  * tokens are IDF-weighted; similarity is the stronger of plain and IDF-weighted overlap
  * three-character tokens are kept
  * recurring subreddit scaffolding (game threads, dailies, megathreads) is filtered
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field

from .textutil import entities, token_set


def is_boilerplate(item: dict, patterns: list[str]) -> bool:
    if item.get("platform") != "reddit":
        return False
    title = item.get("title") or ""
    return any(re.search(p, title) for p in patterns)


def item_text(item: dict) -> str:
    return " ".join(x for x in (item.get("title"), item.get("text")) if x)


class IDF:
    def __init__(self, docs: list[frozenset[str]], rare_share: float = 0.03):
        n = max(len(docs), 1)
        df = Counter(t for d in docs for t in d)
        self.n = n
        self.df = df
        # "rare" = story-specific in this window (a surname, a tour name), not window vocabulary
        self.rare_df = max(2, int(rare_share * n))
        self.w = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}
        self.default = math.log(n + 1) + 0.7

    def rare(self, t: str) -> bool:
        return self.df.get(t, 0) <= self.rare_df

    def __call__(self, t: str) -> float:
        return self.w.get(t, self.default)


def similarity(a: frozenset[str], b: frozenset[str], idf: IDF, ents: frozenset[str] = frozenset(),
               boost: float = 1.0) -> tuple[float, int]:
    """(similarity, shared token count). Overlap coefficient in plain and IDF-weighted form;
    the stronger wins because they catch different evidence. Plain overlap only counts when the
    shared words include two names (proper nouns), or a rare term when neither post yields names:
    otherwise template posts about different events ("Bills injury report: Allen full practice
    Thursday" / "Packers injury report: Love full practice Thursday") look like one story.
    Names are weighted `boost`x in the IDF form for the same reason."""
    if not a or not b:
        return 0.0, 0
    shared = a & b
    if not shared:
        return 0.0, 0
    # plain overlap needs an anchor: a shared name (or, when neither post yields names, a rare term)
    # (two shared names: one shared team name alone is not a shared story)
    anchored = len(shared & ents) >= 2 if ents else any(idf.rare(t) for t in shared)
    plain = len(shared) / min(len(a), len(b)) if anchored else 0.0
    # names (proper nouns in either post) identify a story; template words don't
    w = (lambda t: idf(t) * boost) if boost == 1.0 else (lambda t: idf(t) * (boost if t in ents else 1.0))
    wa = sum(w(t) for t in a)
    wb = sum(w(t) for t in b)
    weighted = sum(w(t) for t in shared) / min(wa, wb) if min(wa, wb) > 0 else 0.0
    return max(plain, weighted), len(shared)


@dataclass
class Cluster:
    members: list[dict] = field(default_factory=list)
    toks: list[frozenset[str]] = field(default_factory=list)
    ents: list[frozenset[str]] = field(default_factory=list)

    # ---- derived properties used by scoring / selection
    @property
    def sources(self) -> set[str]:
        """Independent sources: every author, plus the original author of any repost."""
        out = set()
        for m in self.members:
            if m["platform"] == "reddit":
                # a subreddit discussing it is the source; Reddit usernames aren't reporters
                out.add(f"r/{m.get('subreddit') or m.get('author')}")
            elif m.get("author"):
                out.add(f"{m['platform']}:{m['author']}")
            if m.get("original_author"):
                out.add(f"x:{m['original_author']}")
        return out

    @property
    def authors(self) -> set[str]:
        return {m["author"] for m in self.members if m.get("author")} | \
               {m["original_author"] for m in self.members if m.get("original_author")}

    @property
    def platforms(self) -> set[str]:
        return {m["platform"] for m in self.members}

    @property
    def engagement(self) -> int:
        return sum(int(m.get("engagement") or 0) for m in self.members)

    @property
    def first_seen(self) -> float:
        return min(ts(m) for m in self.members)

    @property
    def last_seen(self) -> float:
        return max(ts(m) for m in self.members)

    def lead(self) -> dict:
        """Most engaged original (non-repost) item, for the headline."""
        originals = [m for m in self.members if not m.get("is_repost")] or self.members
        return max(originals, key=lambda m: (int(m.get("engagement") or 0), -ts(m)))


def ts(item: dict) -> float:
    return item.get("created_at") or item.get("collected_at") or 0.0


def fresh(items: list[dict], max_age_hours: float, now: float) -> list[dict]:
    cutoff = now - max_age_hours * 3600
    return [i for i in items if ts(i) >= cutoff]


def cluster_items(items: list[dict], cfg: dict, now: float | None = None,
                  threshold: float | None = None) -> tuple[list[Cluster], IDF]:
    c = cfg["clustering"]
    now = now or time.time()
    thr = c["join_threshold"] if threshold is None else threshold
    boost = c.get("entity_boost", 1.0)
    avg_min = c["avg_sim_ratio"] * thr
    min_shared = c["min_shared_tokens"]

    items = fresh(items, c["max_age_hours"], now)                       # freshness gate #1
    items = [i for i in items if not is_boilerplate(i, c["boilerplate_title_patterns"])]
    toks = [token_set(item_text(i)) for i in items]
    ents = [entities(item_text(i)) for i in items]
    idf = IDF(toks)

    # Seed with the most engaged, then chronological, so big posts anchor their stories.
    order = sorted(range(len(items)), key=lambda k: (-int(items[k].get("engagement") or 0), ts(items[k])))
    clusters: list[Cluster] = []
    for k in order:
        it, tk, en = items[k], toks[k], ents[k]
        if len(tk) < min_shared:
            clusters.append(Cluster([it], [tk], [en]))
            continue
        best, best_sim = None, 0.0
        for cl in clusters:
            sims = [similarity(tk, mt, idf, en | me, boost) for mt, me in zip(cl.toks, cl.ents)]
            top_sim, top_shared = max(sims, key=lambda s: s[0])
            if top_sim < thr or top_shared < min_shared:
                continue
            avg = sum(s for s, _ in sims) / len(sims)
            if avg < avg_min:
                continue
            if top_sim > best_sim:
                best, best_sim = cl, top_sim
        if best is None:
            clusters.append(Cluster([it], [tk], [en]))
        else:
            best.members.append(it)
            best.toks.append(tk)
            best.ents.append(en)
    return clusters, idf


def signature(cl: Cluster, idf: IDF, n: int = 6) -> list[str]:
    """Distinctive terms of a cluster: frequent within it, rare in the window. No date in it,
    so a story running past midnight keeps its key."""
    counts = Counter(t for tk in cl.toks for t in tk)
    scored = sorted(counts, key=lambda t: (-(counts[t] * idf(t)), t))
    return scored[:n]


def trend_key(sig: list[str]) -> str:
    return "-".join(sorted(sig))
