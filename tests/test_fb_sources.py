import csv

from bore.config import Page
from bore.dna import load_posts_file, normalise_fb
from bore.sources import assign, choose_leagues, detect, load_catalog


def test_business_suite_export_import(cfg, tmp_path):
    path = tmp_path / "export.csv"
    cols = ["Post ID", "Page name", "Description", "Publish time", "Permalink", "Post type",
            "Reactions", "Comments", "Shares"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, cols)
        w.writeheader()
        w.writerow({"Post ID": "1", "Page name": "Birds Talk", "Description": "Eagles win! #FlyEaglesFly",
                    "Publish time": "09/24/2026 14:03", "Permalink": "https://www.facebook.com/x/posts/1",
                    "Post type": "Photos", "Reactions": "1,204", "Comments": "33", "Shares": "12"})
        w.writerow({"Post ID": "2", "Page name": "Birds Talk", "Description": "Read more",
                    "Publish time": "09/24/2026 15:10", "Permalink": "https://www.facebook.com/x/posts/2",
                    "Post type": "Links", "Reactions": "3", "Comments": "0", "Shares": "0"})
    posts, meta = load_posts_file(path, cfg)
    assert meta["name"] == "Birds Talk"
    assert [p["type"] for p in posts] == ["photo", "link"]
    assert posts[0]["interactions"] == 1249 and posts[0]["ts"] > 0


def test_graph_post_format_detection(cfg):
    f = cfg["fields"]["fb_posts"]
    base = {"id": "1", "created_time": "2026-09-24T10:00:00+0000"}
    assert normalise_fb({**base, "permalink_url": "https://www.facebook.com/reel/1"}, f)["type"] == "reel"
    assert normalise_fb({**base, "attachments": {"data": [{"media_type": "video"}]}}, f)["type"] == "video"
    assert normalise_fb({**base, "attachments": {"data": [{"media_type": "photo"}]}}, f)["type"] == "photo"
    assert normalise_fb({**base, "status_type": "shared_story"}, f)["type"] == "link"
    assert normalise_fb({**base, "message": "hi"}, f)["type"] == "text"
    n = normalise_fb({**base, "reactions": {"summary": {"total_count": 50}},
                      "comments": {"summary": {"total_count": 5}}, "shares": {"count": 2}}, f)
    assert n["interactions"] == 57


def test_sources_assumed_from_post_history():
    catalog = load_catalog()
    msgs = ["Eagles QB Jalen Hurts is back", "Eagles win again", "Big NFL Sunday for the Eagles",
            "Eagles injury news", "Cowboys lose", "Phillies clinch"] * 5
    detected = detect(msgs, catalog)
    assert choose_leagues(detected) == ["nfl"]
    page = assign(Page(slug="p", name="P"), detected, catalog)
    # the catalog names no account or subreddit: those are researched for each page (onboarding)
    assert page.tier_one == [] and page.officials == [] and page.subreddits == []
    assert "Eagles news today" in page.web_searches
    assert page.sources_origin.startswith("assumed")
    assert not set(page.tier_one) & set(page.officials)
