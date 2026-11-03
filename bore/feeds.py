"""Websites read straight from their own RSS / Atom feeds (free, every hour).

A website an admin adds as a source is searched on Google (`site:`) only when it has no feed.
Most news sites publish one, often listing every article (dozens a day) with
the summary, the full-size lead image and the time, where the Google search found a handful.

  discover_feed(url)   the feed of a website or section page (alternate link, then common paths)
  parse_feed(xml)      feed entries as collector items (platform "web", author = the site)
  collect_feeds(...)   fetch every page's feeds and store new items for that page
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from .textutil import domain, identity, parse_ts

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9",
      "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5"}
ALT_RE = re.compile(r'<link[^>]+rel=["\']alternate["\'][^>]*>', re.I)
TYPE_RE = re.compile(r'type=["\']application/(rss|atom)\+xml["\']', re.I)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
FEED_HREF_RE = re.compile(r'href=["\']([^"\']*(?:feed|rss)[^"\']*\.xml)["\']', re.I)
COMMON = ("/feeds/all.xml", "/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml")
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_)", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _get(url: str, timeout: int = 20) -> requests.Response | None:
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        return r if r.status_code == 200 else None
    except requests.RequestException:
        return None


def is_feed(body: bytes | str) -> bool:
    head = (body[:2000].decode("utf-8", "ignore") if isinstance(body, bytes) else body[:2000]).lower()
    return "<rss" in head or "<feed" in head or "<rdf:rdf" in head


def discover_feed(url: str) -> str | None:
    """The feed for a website or one of its section pages, or None."""
    if not url:
        return None
    r = _get(url)
    if r is not None:
        if is_feed(r.content):
            return url
        page = r.text[:400_000]
        for tag in ALT_RE.findall(page):
            if TYPE_RE.search(tag) and (m := HREF_RE.search(tag)):
                return urljoin(url, unescape(m.group(1)))
    parts = urlsplit(url)
    origin = f"{parts.scheme or 'https'}://{parts.netloc}"
    # a page listing the site's feeds (e.g. /rss -> /feeds/all.xml, /feeds/news.xml ...)
    listing = _get(origin + "/rss")
    if listing is not None and not is_feed(listing.content):
        hrefs = [urljoin(origin, unescape(h)) for h in FEED_HREF_RE.findall(listing.text[:400_000])]
        hrefs.sort(key=lambda h: (0 if re.search(r"/(all|latest|news)\.xml$", h) else 1, len(h)))
        for h in hrefs[:3]:
            got = _get(h)
            if got is not None and is_feed(got.content):
                return h
    for path in COMMON:
        got = _get(origin + path)
        if got is not None and is_feed(got.content):
            return origin + path
    return None


def clean_url(url: str) -> str:
    p = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


NS = {"media": "http://search.yahoo.com/mrss/", "atom": "http://www.w3.org/2005/Atom",
      "content": "http://purl.org/rss/1.0/modules/content/", "dc": "http://purl.org/dc/elements/1.1/"}


def _text(el, *names) -> str:
    for n in names:
        found = el.find(n, NS)
        if found is not None:
            if found.text and found.text.strip():
                return found.text.strip()
            if found.get("href"):
                return found.get("href")
    return ""


def _when(raw: str) -> float | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError):
        return parse_ts(raw)


def _image(el, ns: dict) -> str:
    for tag in ("media:content", "media:thumbnail"):
        for m in el.findall(tag, ns):
            if m.get("url") and (m.get("medium") in (None, "image") or re.search(r"\.(jpe?g|png|webp)", m.get("url"), re.I)):
                return m.get("url")
    enc = el.find("enclosure")
    if enc is not None and (enc.get("type") or "").startswith("image") and enc.get("url"):
        return enc.get("url")
    desc = _text(el, "description", "content:encoded") or ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc, re.I)
    return m.group(1) if m else ""


def parse_feed(body: bytes | str, feed_url: str = "", now: float | None = None) -> list[dict]:
    """RSS 2.0 / Atom entries as collector items."""
    now = now or time.time()
    root = ET.fromstring(body if isinstance(body, bytes) else body.encode("utf-8"))
    ns = NS
    entries = root.findall(".//item") or root.findall(".//atom:entry", ns)
    out = []
    for e in entries:
        atom = e.tag.endswith("entry")
        if atom:
            link_el = next((l for l in e.findall("atom:link", ns) if l.get("rel") in (None, "alternate")), None)
            link = link_el.get("href") if link_el is not None else ""
            title = _text(e, "atom:title")
            summary = _text(e, "atom:summary", "atom:content")
            when = _when(_text(e, "atom:published", "atom:updated"))
        else:
            link = _text(e, "link") or _text(e, "guid")
            title = _text(e, "title")
            summary = _text(e, "description", "content:encoded")
            when = _when(_text(e, "pubDate", "dc:date"))
        if not link or not title:
            continue
        url = clean_url(urljoin(feed_url, link))
        summary = re.sub(r"\s+", " ", TAG_RE.sub(" ", unescape(summary or ""))).strip()
        img = _image(e, ns)
        cats = [c.text.strip() for c in e.findall("category") if c.text]
        out.append({
            "id": identity("web", url=url), "platform": "web", "url": url, "author": domain(url),
            "title": unescape(title).strip(), "text": summary[:2000], "query": f"feed:{domain(feed_url or url)}",
            "created_at": when or now, "collected_at": now, "engagement": 0,
            "metrics": {**({"image": urljoin(url, img)} if img else {}), **({"categories": cats[:8]} if cats else {})},
        })
    return out


def resolve_feeds(store, pages, now: float | None = None, workers: int = 6) -> dict[str, str]:
    """Give every page the feeds of its websites (sets `page.feeds`) and drop the Google `site:`
    search of each website that has one. A website's feed is looked up once a week (once a day
    while it has none) and remembered in the store. Returns {website: feed} for those found."""
    from concurrent.futures import ThreadPoolExecutor
    now = now or time.time()
    sites = sorted({w for p in pages for w in (p.websites or [])})
    known = {w: store.get_kv(f"feed:{w}") or {} for w in sites}
    stale = [w for w in sites if not known[w] or now - known[w].get("at", 0) > (7 if known[w].get("feed") else 1) * 86400]
    if stale:
        with ThreadPoolExecutor(min(workers, len(stale))) as ex:
            for w, f in zip(stale, ex.map(lambda w: discover_feed(f"https://{w}"), stale)):
                if f or not known[w].get("feed"):        # a failed look-up keeps the feed we had
                    known[w] = {"feed": f, "at": now}
                    store.set_kv(f"feed:{w}", known[w])
        store.commit()
    found = {w: known[w]["feed"] for w in sites if known[w].get("feed")}
    for p in pages:
        own = [found[w] for w in p.websites or [] if w in found]
        p.feeds = list(dict.fromkeys(list(getattr(p, "feeds", None) or []) + own))
        p.web_searches = [q for q in p.web_searches if not (q.startswith("site:") and q[5:] in found)]
    return found


def collect_feeds(store, pages, now: float | None = None, max_age_hours: float = 36) -> dict:
    """Fetch each page's feeds; new entries are stored for that page. Returns {feed_url: new items}."""
    now = now or time.time()
    by_feed: dict[str, set[str]] = {}
    for p in pages:
        for f in getattr(p, "feeds", None) or []:
            by_feed.setdefault(f, set()).add(p.slug)
    out = {}
    for feed, slugs in by_feed.items():
        r = _get(feed, timeout=30)
        if r is None:
            store.mark_attempt(f"feed:{feed}", "unreachable", 0, None)
            out[feed] = 0
            continue
        try:
            items = parse_feed(r.content, feed, now)
        except ET.ParseError as e:
            store.mark_attempt(f"feed:{feed}", f"not a feed: {e}", 0, None)
            out[feed] = 0
            continue
        new = 0
        for it in items:
            if now - (it["created_at"] or now) > max_age_hours * 3600:
                continue
            if store.upsert_item(it, [(s, f"feed:{domain(feed)}") for s in sorted(slugs)]):
                new += 1
        store.commit()
        store.mark_attempt(f"feed:{feed}", None, new, now)
        out[feed] = new
    return out


__all__ = ["discover_feed", "parse_feed", "collect_feeds", "resolve_feeds", "is_feed", "clean_url"]
