import random
import time

from openpyxl import Workbook

from bore.config import Page
from bore.dna import analyse, features, slices
from bore.health import check
from bore.render import render
from bore.select import select_page
from bore.workbook import parse_workbook


def test_caps_metric_excludes_sports_acronyms():
    assert features("NFL QB throws for 300 YDS and a TD, RBI for the WR")["all_caps_word"] is False
    assert features("UNBELIEVABLE finish in the NFL")["all_caps_word"] is True


def test_slices_are_under_8_days():
    from datetime import date
    s = slices(date(2026, 1, 1), date(2026, 3, 31), 7)
    assert all((b - a).days + 1 <= 7 for a, b in s)
    assert s[0][0] == date(2026, 1, 1) and s[-1][1] == date(2026, 3, 31)


def test_format_confounding_is_detected():
    rnd = random.Random(1)
    posts, t = [], time.time() - 30 * 86400
    for i in range(400):
        photo = i % 2 == 0
        tag = photo and rnd.random() < 0.8 or (not photo and rnd.random() < 0.05)
        base = 350 if photo else 4
        posts.append({"ts": t + i * 3600, "type": "photo" if photo else "link",
                      "message": "Big win tonight" + (" #GoBirds" if tag else ""),
                      "interactions": int(base * rnd.uniform(0.7, 1.3) * (1.2 if tag else 1.0))})
    dna = analyse(posts, "America/New_York")
    h = dna["devices"]["hashtag"]
    assert h["raw_lift"] > 10                     # the misleading headline number
    assert h["stratified_lift"] < 1.6             # the real within-format effect
    assert h["confounded_by_format"] is True
    assert dna["format_engagement"]["photo"]["median_interactions"] > 50 * \
        dna["format_engagement"]["link"]["median_interactions"]


def test_workbook_parsing(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Eagles Nation"
    ws.append(["X", "Website", "Reddit", "Facebook", "Instagram"])
    ws.append(["https://x.com/AdamSchefter/status/123456", "https://www.inquirer.com/eagles", None,
               "https://www.facebook.com/EaglesNationPage", None])
    ws.append(["https://twitter.com/JeffMcLane", None, None, None, None])
    ws.append(["@BLGEagles", None, None, None, None])
    ws.append(["Zach Berman", None, None, None, None])
    ws.append(["x.com/i/lists/999", None, None, None, None])
    path = tmp_path / "w.xlsx"
    wb.save(path)
    [p] = parse_workbook(str(path))
    assert p.slug == "eagles-nation"
    assert p.x_handles == ["adamschefter", "jeffmclane", "blgeagles"]
    assert p.subreddits == []
    assert "x: Zach Berman" in p.unresolved and any("lists" in u for u in p.unresolved)
    assert p.facebook_url.endswith("EaglesNationPage")


def test_health_errors_on_data_loss_and_warns_on_silent_handles(cfg, store):
    pages = [Page.from_dict({"slug": "p", "name": "P", "x_handles": ["alive", "renamed"]})]
    now = time.time()
    store.mark_attempt("x", success_at=now - 30 * 3600)
    errors, _ = check(store, pages, cfg, now)
    assert any("data is being lost" in e for e in errors)
    store.mark_attempt("x", success_at=now - 600)
    store.upsert_item({"id": "1", "platform": "x", "author": "alive", "collected_at": now,
                       "created_at": now}, [("p", "x:alive")])
    store.commit()
    errors, warnings = check(store, pages, cfg, now)
    assert errors == []
    assert any("renamed" in w for w in warnings)
    assert any("no web items" in w for w in warnings)
    assert any("overlay" in w for w in warnings)


def test_render_pdf_and_freshness_gate(cfg, tmp_path):
    now = time.time()
    page = Page.from_dict({"slug": "p", "name": "P"})
    items = [{"id": f"i{k}", "platform": "x", "author": f"a{k}", "engagement": 900,
              "text": "Chiefs trade Rashee Rice to the Jets for a first round pick",
              "created_at": now - 3600, "url": f"https://x.com/a{k}/status/{k}"} for k in range(3)]
    res = select_page(page, items, cfg, [], now)
    assert res.trends
    # an item that slipped in stale must be removed at render time too
    res.trends[0].cluster.members.append({"id": "stale", "platform": "x", "author": "z",
                                          "created_at": now - 30 * 3600, "engagement": 1})
    out = render([("P", res)], cfg, tmp_path / "b.pdf", now)
    assert out.exists() and out.stat().st_size > 1000
    assert all(m["id"] != "stale" for m in res.trends[0].cluster.members)
