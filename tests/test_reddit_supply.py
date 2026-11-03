"""Culture Rush supply (Oct 17-21): Reddit link posts were stored as bare titles (their article and
image dropped) and skipped as 'single uncredited Reddit post'; escalation searches with the page's
name appended brought class schedules and investment pages."""
import json

from bore import collect
from bore.cluster import Cluster
from bore.config import Page
from bore.select import escalation_queries

from conftest import FIXTURES

# the actor's records of Sep 25 as the parser reads them (their field shapes: media_metadata,
# url_overridden_by_dest, domain, media_type), with made-up ids, titles, text, links and counts:
# which subreddit a page reads is confidential
RAW = json.loads((FIXTURES / "reddit_actor_20260925.json").read_text())


def _norm(raw, cfg):
    return collect.normalise_reddit(raw, cfg["fields"]["reddit"], 0)


def test_real_actor_records_keep_the_article_and_the_image(cfg):
    by_type = {r["media_type"] + ("-" + r["domain"] if r["media_type"] == "link" else ""): _norm(r, cfg) for r in RAW}
    link = by_type["link-news.example.org"]
    assert link["metrics"]["link"].startswith("https://news.example.org/sports/") and link["url"].startswith("https://www.reddit.com/")
    assert "link" not in by_type["link-threads.com"]["metrics"]           # a social post is not an article
    assert by_type["image"]["metrics"]["image"] == "https://i.redd.it/aaaabbbbcccc1.jpg"    # an inline image
    assert by_type["video"]["metrics"]["image"] is None and "link" not in by_type["video"]["metrics"]
    assert by_type["text"]["metrics"]["image"] is None


def test_a_gallery_gives_its_first_image_and_a_link_post_tries_the_preview(cfg):
    gallery = {"id": "g1", "title": "Mars", "url": "https://www.reddit.com/r/demo/comments/g1/mars/",
               "url_overridden_by_dest": "https://www.reddit.com/gallery/g1",
               "gallery_data": {"items": [{"media_id": "bbbbbbbbbbbbb"}, {"media_id": "aaaaaaaaaaaaa"}]},
               "media_metadata": {"aaaaaaaaaaaaa": {"status": "valid", "e": "Image", "m": "image/jpg"},
                                  "bbbbbbbbbbbbb": {"status": "valid", "e": "Image", "m": "image/png"}}}
    assert _norm(gallery, cfg)["metrics"]["image"] == "https://i.redd.it/bbbbbbbbbbbbb.png"
    linked = {"id": "l1", "title": "News", "url": "https://www.reddit.com/r/demo/comments/l1/news/",
              "url_overridden_by_dest": "https://news.example.org/story",
              "preview": {"images": [{"source": {"url": "https://preview.redd.it/x.jpg?width=1200&amp;s=1"}}]}}
    m = _norm(linked, cfg)["metrics"]
    assert m["image"] == "https://preview.redd.it/x.jpg?width=1200&s=1" and m["link"] == "https://news.example.org/story"


def test_a_link_post_brings_its_article_to_the_brief(monkeypatch):
    from bore import linkpost, routine
    monkeypatch.setattr(linkpost, "read_article", lambda url: {"title": "Cornell fraternity president dropped from suit",
                                                              "text": "A judge dismissed the claims, the court record shows."})
    posts = [{"platform": "reddit", "author": "u", "subreddit": "demo", "text": "Former Cornell fraternity president dropped",
              "url": "https://www.reddit.com/r/demo/comments/1/x/", "link": "https://news.example.org/story", "hours_ago": 1}]
    routine._full_articles(posts)
    art = posts[1]
    assert art["platform"] == "web" and art["author"] == "news.example.org" and "court record" in art["text"]
    assert art["shared_by"] == posts[0]["url"]


def test_each_shared_article_is_read_once_and_the_feeds_own_articles_keep_their_reads(monkeypatch):
    """Two Reddit posts sharing one article, and one the feed already has: one read, one added post;
    the article added is not read again, and the feed's two short articles still get theirs."""
    from bore import linkpost, routine
    reads = []

    def read(url):
        reads.append(url)
        return {"title": "", "text": "short"}
    monkeypatch.setattr(linkpost, "read_article", read)
    reddit = lambda i, link: {"platform": "reddit", "author": "u", "subreddit": "demo", "text": "a title", "hours_ago": 1,
                              "url": f"https://www.reddit.com/r/demo/comments/{i}/x/", "link": link}
    web = lambda u: {"platform": "web", "author": "news.example.org", "text": "one line", "url": u, "hours_ago": 1}
    posts = [reddit(1, "https://news.example.org/a"), reddit(2, "https://news.example.org/a"),
             reddit(3, "https://news.example.org/b"), web("https://news.example.org/b"), web("https://news.example.org/c")]
    routine._full_articles(posts)
    assert reads == ["https://news.example.org/a", "https://news.example.org/b", "https://news.example.org/c"]
    assert [p["url"] for p in posts if p.get("shared_by")] == ["https://news.example.org/a"]


def test_story_photos_use_the_linked_articles_image(monkeypatch):
    from bore import media
    monkeypatch.setattr(media, "og_image", lambda url, timeout=12: "https://news.example.org/lead.jpg")
    m = {"platform": "reddit", "subreddit": "demo", "url": "https://www.reddit.com/r/demo/comments/1/x/", "engagement": 5,
         "metrics": {"image": None, "link": "https://news.example.org/story"}}
    (ph,) = media.story_photos([m])
    assert ph.url == "https://news.example.org/lead.jpg" and ph.source == "web" and ph.post_url == "https://news.example.org/story"


def test_escalation_searches_never_append_the_page_name():
    page = Page.from_dict({"slug": "culture-rush", "name": "Culture Rush", "subreddits": ["demo"]})
    members = [{"id": f"m{i}", "platform": "reddit", "author": "u", "subreddit": "demo", "engagement": 50, "created_at": 1.0,
                "title": "Karen variant spotted at the grocery store demanding a manager"} for i in range(2)]
    from bore.cluster import IDF
    from bore.textutil import token_set
    cl = Cluster(members=members, toks=[token_set(m["title"]) for m in members])
    qs = escalation_queries(page, [cl], IDF([token_set(m["title"]) for m in members]))
    assert qs and not any("culture" in q.lower() or "rush" in q.lower() for q in qs)


def test_an_escalation_result_must_share_two_of_the_storys_words():
    q = "car class dollar example"
    assert not collect.on_story({"query": q, "title": "Riverside City College - Fall 2026 Class Schedule",
                                 "text": "Cultural Anthropology. Prerequisite: None"})
    assert collect.on_story({"query": "karen grocery manager", "title": "Karen demands manager at grocery store", "text": ""})
