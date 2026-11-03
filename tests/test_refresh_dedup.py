import time

from bore.config import Page
from bore.refresh import select_for_refresh
from bore.select import record_briefed, select_page


def _item(i, age_h, eng=0, refreshed=None, platform="x"):
    now = time.time()
    return {"id": i, "platform": platform, "native_id": i, "author": "a", "created_at": now - age_h * 3600,
            "collected_at": now - age_h * 3600, "engagement": eng, "refreshed_at": refreshed}


def test_refresh_selection(cfg):
    now = time.time()
    items = [_item("young", 0.5), _item("ok", 2, 10), _item("recent", 5, refreshed=now - 3600),
             _item("due", 5, 50, refreshed=now - 4 * 3600), _item("old", 30), _item("web", 3, platform="web")]
    ids = [i["id"] for i in select_for_refresh(items, cfg, now)]
    assert ids == ["due", "ok"]
    cfg["refresh"]["max_items_per_run"] = 1
    assert len(select_for_refresh(items, cfg, now)) == 1


def test_overlay_never_decreases(store):
    store.upsert_item({"id": "p", "platform": "x", "engagement": 100}, [("pg", "x:a")])
    store.overlay_upsert("p", 5000)
    store.overlay_upsert("p", 12)            # partial response
    assert store.db.execute("SELECT engagement FROM overlay WHERE item_id='p'").fetchone()[0] == 5000
    store.overlay_upsert("q", 0)
    items = store.page_items("pg", 0)
    assert items[0]["engagement"] == 5000


def _story(now, eng, n_sources=2, suffix=""):
    # sources beyond the first two carry no engagement, so only the source count changes
    return [{"id": f"s{k}{suffix}", "platform": "x", "author": f"rep{k}", "engagement": eng if k < 2 else 0,
             "text": "Chiefs trade Rashee Rice to the Jets for a first round pick",
             "created_at": now - 3600, "url": f"https://x.com/rep{k}/status/{k}"} for k in range(n_sources)]


def test_briefed_story_is_suppressed_then_allowed_back_when_developed(cfg, store):
    page = Page.from_dict({"slug": "p", "name": "P"})
    now = time.time()
    res = select_page(page, _story(now, 5000), cfg, [], now)
    assert len(res.trends) == 1
    assert not any(ch.isdigit() for ch in res.trends[0].key)   # no date in the key
    record_briefed(store, "p", res, now)
    sent = store.sent_for_page("p", now - 96 * 3600)

    # next run past midnight, same story, same engagement: suppressed
    later = now + 6 * 3600
    again = select_page(page, _story(later, 5000), cfg, sent, later)
    assert again.trends == [] and len(again.suppressed) == 1

    # posting pages (default): a story that grew is still the same story
    assert select_page(page, _story(later, 60000), cfg, sent, later).trends == []
    assert select_page(page, _story(later, 5000, n_sources=4), cfg, sent, later).trends == []

    # sourcing-only use (allow_redevelop): engagement grew 2x+ -> allowed back, flagged as developed
    cfg["dedup"]["allow_redevelop"] = True
    grown = select_page(page, _story(later, 60000), cfg, sent, later)
    assert len(grown.trends) == 1 and "engagement" in grown.trends[0].developed

    # two new sources: allowed back
    more = select_page(page, _story(later, 5000, n_sources=4), cfg, sent, later)
    assert len(more.trends) == 1 and "sources" in more.trends[0].developed


def test_thin_page_requires_escalation(cfg):
    page = Page.from_dict({"slug": "p", "name": "Hoops", "web_searches": ["Hoops news today"]})
    now = time.time()
    items = [{"id": "x", "platform": "x", "author": "a", "text": "Hoops center Jones sprains ankle in practice",
              "created_at": now - 3600, "engagement": 40}]
    res = select_page(page, items, cfg, [], now)
    assert res.escalation["required"] is True
    assert "Hoops news today" in res.escalation["queries"]
    assert len(res.escalation["queries"]) >= 2
    done = select_page(page, items, cfg, [], now, escalated=True)
    assert done.escalation["required"] is False and done.escalation["done"] is True
