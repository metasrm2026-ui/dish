"""Websites read from their RSS / Atom feeds instead of a Google `site:` search."""
import time

from bore import feeds
from bore.config import Page
from bore.feeds import collect_feeds, parse_feed, resolve_feeds
from bore.studio import classify, page_from_config

NOW = time.time()
RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Demo TV</title>
<item><title>Star &amp; co-star split after 10 years</title>
  <link>https://demo.tv/news/split?utm_source=rss&amp;utm_medium=feed&amp;id=7</link>
  <description><![CDATA[<p>The couple <b>confirmed</b> the split on Monday.</p>]]></description>
  <pubDate>{time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(NOW - 3600))}</pubDate>
  <category>Star</category><category>Co-Star</category>
  <media:content url="https://cdn.demo.tv/split.webp" medium="image" width="5616" height="3744"/>
</item>
<item><title>Old story</title><link>https://demo.tv/news/old</link>
  <pubDate>{time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(NOW - 5 * 86400))}</pubDate></item>
<item><title></title><link>https://demo.tv/news/no-title</link></item>
</channel></rss>"""
ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><title>Season 2 renewed</title><link rel="alternate" href="/posts/s2"/>
  <summary>The show returns next year.</summary><published>2026-10-11T10:00:00Z</published></entry>
</feed>"""


def test_parse_rss_items():
    items = parse_feed(RSS, "https://demo.tv/feeds/all.xml", NOW)
    assert [i["title"] for i in items] == ["Star & co-star split after 10 years", "Old story"]
    it = items[0]
    assert it["platform"] == "web" and it["author"] == "demo.tv" and it["query"] == "feed:demo.tv"
    assert it["url"] == "https://demo.tv/news/split?id=7"            # tracking parameters dropped
    assert it["text"] == "The couple confirmed the split on Monday."
    assert abs(it["created_at"] - (NOW - 3600)) < 2
    assert it["metrics"] == {"image": "https://cdn.demo.tv/split.webp", "categories": ["Star", "Co-Star"]}


def test_parse_atom_items():
    (it,) = parse_feed(ATOM, "https://blog.example.com/atom.xml", NOW)
    assert it["url"] == "https://blog.example.com/posts/s2" and it["title"] == "Season 2 renewed"
    assert it["text"] == "The show returns next year." and it["created_at"] < NOW


def test_feed_address_is_a_feed_source():
    assert classify({"url": "https://www.demo.tv/feeds/all.xml", "type": "website"}) == ("feed", "https://www.demo.tv/feeds/all.xml")
    assert classify({"url": "https://www.demo.tv/", "type": "website"}) == ("site", "demo.tv")
    p = page_from_config({"slug": "d", "name": "D", "sources": [
        {"url": "https://www.demo.tv/", "type": "website"}, {"url": "https://other.tv/rss.xml", "type": "website"}]})
    assert p.feeds == ["https://other.tv/rss.xml"] and p.websites == ["demo.tv"] and p.web_searches == ["site:demo.tv"]


class _Resp:
    status_code = 200

    def __init__(self, body):
        self.content = body.encode() if isinstance(body, str) else body
        self.text = self.content.decode()


def test_websites_with_a_feed_are_read_from_it(store, monkeypatch):
    looked = []

    def discover(url):
        looked.append(url)
        return "https://www.demo.tv/feeds/all.xml" if "demo.tv" in url else None
    monkeypatch.setattr(feeds, "discover_feed", discover)
    page = Page(slug="cr", name="CR", websites=["demo.tv", "nofeed.com"],
                web_searches=["celebs today", "site:demo.tv", "site:nofeed.com"])
    found = resolve_feeds(store, [page], NOW)
    assert found == {"demo.tv": "https://www.demo.tv/feeds/all.xml"}
    assert page.feeds == ["https://www.demo.tv/feeds/all.xml"]
    assert page.web_searches == ["celebs today", "site:nofeed.com"]     # Google only for the site without one
    # remembered: the next hour looks nothing up
    page2 = Page(slug="cr", name="CR", websites=["demo.tv", "nofeed.com"], web_searches=["site:demo.tv"])
    resolve_feeds(store, [page2], NOW + 3600)
    assert len(looked) == 2 and page2.feeds == ["https://www.demo.tv/feeds/all.xml"] and page2.web_searches == []
    # a look-up that fails later keeps the feed we had
    monkeypatch.setattr(feeds, "discover_feed", lambda url: None)
    resolve_feeds(store, [page2], NOW + 8 * 86400)
    assert store.get_kv("feed:demo.tv")["feed"] == "https://www.demo.tv/feeds/all.xml"

    monkeypatch.setattr(feeds, "_get", lambda url, timeout=20: _Resp(RSS))
    assert collect_feeds(store, [page], NOW) == {"https://www.demo.tv/feeds/all.xml": 1}   # old story skipped
    (item,) = store.page_items("cr", NOW - 86400)
    assert item["title"].startswith("Star & co-star") and item["metrics"]["image"].endswith("split.webp")
    assert collect_feeds(store, [page], NOW + 600) == {"https://www.demo.tv/feeds/all.xml": 0}   # nothing new


def test_feed_photo_used_and_webp_converted(monkeypatch):
    import io

    from PIL import Image

    from bore import media
    buf = io.BytesIO()
    Image.new("RGB", (4000, 2000), "red").save(buf, "WEBP")

    class R:
        status_code, content = 200, buf.getvalue()
    monkeypatch.setattr(media.requests, "get", lambda *a, **k: R)
    monkeypatch.setattr(media, "og_image", lambda url: (_ for _ in ()).throw(AssertionError("no page fetch")))
    (ph,) = media.story_photos([{"platform": "web", "url": "https://demo.tv/news/split", "author": "demo.tv",
                                 "metrics": {"image": "https://cdn.demo.tv/split.webp"}}])
    assert ph.url == "https://cdn.demo.tv/split.webp" and ph.credit == "demo.tv"
    got = media.fetch(ph)
    assert got.data[:3] == b"\xff\xd8\xff" and (got.w, got.h) == (2400, 1200)


def test_website_only_page_gets_no_keyword_escalation_and_trusts_its_sites():
    from bore.cli import escalates
    from bore.routine import admin_sources
    site_only = Page(slug="cr", name="CR", websites=["demo.tv"], feeds=["https://www.demo.tv/feeds/all.xml"],
                     web_searches=["site:other.com"])
    assert not escalates(site_only)
    assert escalates(Page(slug="h", name="H", x_handles=["reporter1"]))
    assert escalates(Page(slug="h", name="H", web_searches=["team news today"]))
    members = [{"platform": "web", "author": "demo.tv"}, {"platform": "web", "author": "elsewhere.com"},
               {"platform": "x", "author": "demo.tv"}]
    assert admin_sources(site_only, members) == ["demo.tv"]
