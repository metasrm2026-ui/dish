"""A page's source sheet: tiers, rotation, priority boost, and the budget each tier may use."""
import time

from bore.cluster import Cluster
from bore.collect import chunk_handles, collect_reddit, collect_x_secondary, rotate, route_map, routes_for
from bore.config import Page
from bore.select import priority_hits, priority_matcher


class FakeApify:
    def __init__(self, items=None):
        self.calls, self.items = [], items or []

    def run(self, actor, run_input, timeout):
        self.calls.append(run_input)
        return self.items, "SUCCEEDED"


def test_chunks_keep_order_and_fit():
    handles = [f"handle_number_{i}" for i in range(60)]
    groups = chunk_handles(handles, 460)
    assert [h for g in groups for h in g] == handles
    assert all(len(" OR ".join(f"from:{h}" for h in g)) <= 460 for g in groups)


def test_rotation_round_robin(store):
    items = ["a", "b", "c", "d", "e"]
    seen = [rotate(store, "t", items, 2) for _ in range(3)]
    assert seen == [["a", "b"], ["c", "d"], ["e", "a"]]
    assert rotate(store, "empty", [], 2) == []


def test_secondary_x_one_capped_batch_per_run(cfg, store):
    page = Page.from_dict({"slug": "cr", "name": "CR", "x_handles": ["tmz"],
                           "x_handles_secondary": [f"acct_{i:03d}" for i in range(80)] + ["TMZ"]})
    assert "tmz" not in page.x_handles_secondary            # primary wins over secondary
    tweet = {"id": "1", "text": "Big celebrity news tonight", "createdAt": "2026-09-26T10:00:00Z",
             "url": "https://x.com/acct_001/status/1", "likeCount": 10}
    ap = FakeApify([tweet])
    now = time.time()
    collect_x_secondary(ap, store, [page], cfg, now=now)
    collect_x_secondary(ap, store, [page], cfg, now=now)
    q1, q2 = ap.calls[0]["searchTerms"][0], ap.calls[1]["searchTerms"][0]
    assert q1 != q2 and len(q1) <= 460
    assert ap.calls[0]["maxItems"] == cfg["apify"]["x"]["secondary_max_items"]
    assert store.get_state("x2")["last_success"]
    bh, bs = route_map([page])
    assert routes_for({"platform": "x", "author": "acct_001"}, bh, bs) == [("cr", "x:acct_001")]


def test_reddit_core_plus_rotation(cfg, store):
    page = Page.from_dict({"slug": "cr", "name": "CR", "subreddits": ["popculturechat"],
                           "subreddits_rotation": ["r/WTF", "tifu", "movies"]})
    ap = FakeApify()
    collect_reddit(ap, store, [page], cfg)
    collect_reddit(ap, store, [page], cfg)
    urls = [u for c in ap.calls for u in c["urls"]]
    n = cfg["apify"]["reddit"]["rotation_per_run"]
    assert len(ap.calls[0]["urls"]) == 1 + n
    assert all("popculturechat" in " ".join(c["urls"]) for c in ap.calls)
    assert {"https://www.reddit.com/r/wtf/", "https://www.reddit.com/r/tifu/",
            "https://www.reddit.com/r/movies/"} <= set(urls)


def _cluster(texts):
    members = [{"id": str(i), "platform": "x", "author": f"a{i}", "text": t, "created_at": time.time(),
                "engagement": 0} for i, t in enumerate(texts)]
    cl = Cluster.__new__(Cluster)
    cl.members = members
    return cl


def test_priority_matching_rules():
    rx = priority_matcher(["Taylor Swift", "Drake", "Future", "The Bear"])
    assert priority_hits(_cluster(["TAYLOR SWIFT and Drake announce tour"]), rx) == 2
    assert priority_hits(_cluster(["what the future holds for the bearded man"]), rx) == 0
    assert priority_hits(_cluster(["Drake, Drake and Drake"]), rx) == 1
