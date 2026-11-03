"""Posts from a link an editor pasted into the dashboard ("URL to post").

The link is read into the same brief format as a collected story, so the routine writes, renders,
checks and uploads it exactly like any other post. What the page says is the only fact base.
"""
from __future__ import annotations

import html
import json
import math
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .media import UA, Photo, best, x_orig
from .textutil import domain

MAX_TEXT = 6000


class _Article(HTMLParser):
    """Title, meta description/images and the readable paragraphs of a news page (stdlib only)."""
    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "figure"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.paras, self.title = {}, [], ""
        self._skip, self._in_p, self._in_title, self._buf = 0, False, False, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            if k and a.get("content") and k not in self.meta:
                self.meta[k] = a["content"]
        elif tag in self.SKIP:
            self._skip += 1
        elif tag in ("p", "h2") and not self._skip:
            self._in_p, self._buf = True, []
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in ("p", "h2") and self._in_p:
            t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if len(t) >= 40:
                self.paras.append(t)
            self._in_p = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_p and not self._skip:
            self._buf.append(data)
        elif self._in_title:
            self.title += data


def _get(url: str, **kw) -> requests.Response:
    return requests.get(url, headers=UA, timeout=kw.pop("timeout", 20), **kw)


def read_article(url: str) -> dict:
    r = _get(url)
    r.raise_for_status()
    p = _Article()
    p.feed(r.text[:2_000_000])
    m = p.meta
    title = (m.get("og:title") or m.get("twitter:title") or p.title).strip()
    desc = (m.get("og:description") or m.get("description") or "").strip()
    body = "\n".join(p.paras)
    images = [urljoin(url, m[k]) for k in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src")
              if m.get(k)]
    published = m.get("article:published_time") or m.get("og:updated_time") or ""
    return {"platform": "web", "author": domain(url), "title": title, "text": f"{desc}\n{body}".strip()[:MAX_TEXT],
            "images": list(dict.fromkeys(images)), "published": published}


def _x_token(tweet_id: str) -> str:
    """The token X's own embed script sends with a syndication request: id / 1e15 * pi written in
    base 36, without its zeros and point."""
    x = int(tweet_id) / 1e15 * math.pi
    digits, whole, frac, out = "0123456789abcdefghijklmnopqrstuvwxyz", int(x), x - int(x), ""
    while whole:
        whole, d = divmod(whole, 36)
        out = digits[d] + out
    out = (out or "0") + "."
    for _ in range(12):
        frac *= 36
        out += digits[int(frac)]
        frac -= int(frac)
    return out.replace("0", "").replace(".", "")


def x_syndication(url: str) -> dict:
    """An X post's text and media through the public endpoint X's embedded posts use (no login):
    the photos full size; a video or GIF gives its preview frame."""
    m = re.search(r"/status(?:es)?/(\d+)", url)
    if not m:
        raise ValueError("no post id in the link")
    r = requests.get("https://cdn.syndication.twimg.com/tweet-result", headers=UA, timeout=20,
                     params={"id": m.group(1), "lang": "en", "token": _x_token(m.group(1))})
    r.raise_for_status()
    return r.json()


def x_media(post: dict) -> tuple[list[str], list[str]]:
    """(image URLs in order, flags) of a syndication answer."""
    media = post.get("mediaDetails") or []
    imgs = [x_orig(med["media_url_https"]) for med in media if med.get("media_url_https")]
    video = any(med.get("type") in ("video", "animated_gif") for med in media)
    return imgs[:MAX_LINK_IMAGES], ["video: only a still of it can be posted"] if video else []


def read_x(url: str) -> dict:
    """An X post through X's public oEmbed endpoint (no login), its photos through the public
    syndication endpoint (before Oct 21 an X link never brought its photos, so an 'as is' X post
    was impossible). Either alone is enough."""
    handle = urlsplit(url).path.strip("/").split("/")[0].lower()
    text, post = "", {}
    try:
        post = x_syndication(url)
    except Exception:
        pass
    try:
        r = requests.get("https://publish.twitter.com/oembed", params={"url": url, "omit_script": "1"},
                         headers=UA, timeout=20)
        r.raise_for_status()
        text = re.sub(r"<a [^>]*>(pic\.twitter\.com/\w+)</a>", "", r.json().get("html", ""))
        text = html.unescape(re.sub(r"<br\s*/?>", "\n", re.sub(r"</?(?!br)[^>]+>", " ", text)))
        text = re.sub(r"[ \t]+", " ", text).strip()
    except Exception:
        if not post:
            raise
        text = re.sub(r"\s*https://t\.co/\w+$", "", post.get("text") or "")
    images, flags = x_media(post)
    return {"platform": "x", "author": handle, "title": "", "text": text[:MAX_TEXT], "images": images,
            **({"flags": flags} if flags else {}), "published": post.get("created_at") or ""}


def _reddit_oembed(url: str) -> dict:
    """The post's title and author through Reddit's public oEmbed (works where .json is blocked)."""
    r = requests.get("https://www.reddit.com/oembed", params={"url": url}, headers=UA, timeout=20)
    r.raise_for_status()
    d = r.json()
    m = re.search(r'<a href="[^"]*/comments/[^"]*">(.*?)</a>', d.get("html", ""), re.S)
    title = html.unescape(m.group(1)).strip() if m else (d.get("title") or "")
    sub = re.search(r"/r/([^/]+)/", url)
    return {"platform": "reddit", "author": f"r/{sub.group(1)}" if sub else (d.get("author_name") or ""),
            "title": title, "text": "", "images": [], "published": ""}


REDDIT_ID_RE = re.compile(r"(?:/comments/|/gallery/|redd\.it/)([a-z0-9]{5,10})\b", re.I)
MAX_LINK_IMAGES = 10


def _reddit_id(url: str) -> str:
    m = REDDIT_ID_RE.search(url)
    return m.group(1).lower() if m else ""


def _reddit_post(post: dict) -> dict:
    """A Reddit post's data (the .json API's, or the public archive's, which has the same fields):
    its text and every image in order - a gallery's full-size originals, a single image, or a
    video's preview frame."""
    post = (post.get("crosspost_parent_list") or [post])[0] if not post.get("selftext") and post.get("crosspost_parent_list") else post
    imgs = []
    meta = post.get("media_metadata") or {}
    for it in ((post.get("gallery_data") or {}).get("items") or []):
        m = meta.get(it.get("media_id")) or {}
        if m.get("status") not in (None, "valid") or m.get("e") not in (None, "Image", "AnimatedImage"):
            continue
        ext = (m.get("m") or "image/jpg").split("/")[-1].replace("jpeg", "jpg")
        src = html.unescape((m.get("s") or {}).get("u") or (m.get("s") or {}).get("gif") or "")
        imgs.append(f"https://i.redd.it/{it['media_id']}.{ext}" if ext in ("jpg", "png", "webp", "gif") else src)
    dest = post.get("url_overridden_by_dest") or post.get("url") or ""
    if not imgs and re.search(r"\.(jpe?g|png|webp)(\?|$)", dest, re.I):
        imgs.append(dest)
    if not imgs:
        for it in ((post.get("preview") or {}).get("images") or [])[:1]:
            imgs.append(html.unescape(it["source"]["url"]))
    created = post.get("created_utc") or 0
    text = post.get("selftext") or ""
    removed = bool(post.get("removed_by_category")) or text.strip() in ("[removed]", "[deleted]")
    flags = [f for f, on in (("video: only a still of it can be posted", post.get("is_video") or "v.redd.it" in dest),
                             ("marked NSFW on Reddit", post.get("over_18")),
                             ("removed or deleted on Reddit", removed)) if on]
    return {"platform": "reddit", "author": f"r/{post.get('subreddit', '')}", "title": post.get("title", ""),
            "text": "" if removed else text[:MAX_TEXT], "images": imgs[:MAX_LINK_IMAGES], "flags": flags,
            "published": datetime.fromtimestamp(created, timezone.utc).isoformat() if created else ""}


def _reddit_archive(url: str) -> dict:
    """The post from Arctic Shift, a public archive of Reddit that is not blocked for cloud servers
    and has the full data (text, gallery, preview) within minutes of posting."""
    pid = _reddit_id(url)
    if not pid:
        raise ValueError("no post id in the link")
    r = requests.get("https://arctic-shift.photon-reddit.com/api/posts/ids", params={"ids": pid}, headers=UA, timeout=20)
    r.raise_for_status()
    data = r.json().get("data") or []
    if not data:
        raise LookupError("not in the archive yet")
    return _reddit_post(data[0])


def _reddit_embed(url: str) -> dict:
    """The post's title and images from Reddit's embed page (embed.reddit.com is not blocked):
    the largest preview of each image, in order."""
    path = urlsplit(url).path
    r = requests.get(f"https://embed.reddit.com{path}", headers=UA, timeout=20)
    r.raise_for_status()
    best_by_id: dict[str, tuple[int, str]] = {}
    order = []
    for u in re.findall(r'https://preview\.redd\.it/[^"\s\\<>]+', r.text):
        u = html.unescape(u)
        m = re.search(r"([a-z0-9]{10,16})\.(?:jpe?g|png|webp)", u)
        if not m:
            continue
        w = int((re.search(r"[?&]width=(\d+)", u) or [0, 0])[1] or 0)
        if m.group(1) not in best_by_id:
            order.append(m.group(1))
        if w >= best_by_id.get(m.group(1), (-1, ""))[0]:
            best_by_id[m.group(1)] = (w, u)
    title = ""
    for rx in (r'<meta property="og:title" content="([^"]*)"', r"<h1[^>]*>(.*?)</h1>"):
        t = re.search(rx, r.text, re.S)
        cand = html.unescape(re.sub(r"<[^>]+>", "", t.group(1))).strip() if t else ""
        if cand and "heart of the internet" not in cand.lower():
            title = cand
            break
    sub = re.search(r"/r/([^/]+)/", url)
    # the originals of the images seen (i.redd.it serves them to cloud servers)
    full = []
    for i in order:
        ext = re.search(r"\.(jpe?g|png|webp)", best_by_id[i][1])
        full.append(f"https://i.redd.it/{i}.{ext.group(1) if ext else 'jpg'}")
    return {"platform": "reddit", "author": f"r/{sub.group(1)}" if sub else "",
            "title": title, "text": "", "images": full[:MAX_LINK_IMAGES], "published": ""}


def read_reddit(url: str, apify=None, cfg: dict | None = None) -> dict:
    """Reddit blocks direct reads from cloud servers. In order: the .json API (works elsewhere),
    the public archive (text and full gallery), Reddit's embed page (title and images), the
    budgeted scraper, then oEmbed for the title. The first with text and images wins; otherwise
    what each found is combined."""
    if "/s/" in urlsplit(url).path and not _reddit_id(url):
        raise ValueError("a Reddit share link (/s/...): open it with your web tools to get the post's "
                         "/comments/<id>/ address, then read that")
    got: dict = {}
    def merge(new: dict) -> None:
        for k, v in new.items():
            if v and not got.get(k):
                got[k] = v
    try:
        r = _get(url.split("?")[0].rstrip("/") + ".json")
        r.raise_for_status()
        return _reddit_post(r.json()[0]["data"]["children"][0]["data"])
    except Exception:
        pass
    for fn in (_reddit_archive, _reddit_embed):
        try:
            merge(fn(url))
        except Exception:
            continue
        if got.get("title") and got.get("images"):
            return got
    if apify is not None and cfg is not None and not got.get("title"):
        try:
            merge(_reddit_via_apify(url, apify, cfg))
        except Exception:
            pass
    if not (got.get("title") or got.get("text")):
        merge(_reddit_oembed(url))
    got.setdefault("images", [])
    return got


def _reddit_via_apify(url: str, apify, cfg: dict) -> dict:
    """The post through the budgeted Reddit actor (one item, ~$0.0014)."""
    import time as _t
    from .collect import normalise_reddit
    rc = cfg["apify"]["reddit"]
    run_input = dict(rc["input"])
    run_input[rc["urls_key"]] = [url]
    run_input[rc["max_posts_key"]] = 1
    raw, _status = apify.run(rc["actor"], run_input, rc["timeout_seconds"])
    if not raw:
        raise RuntimeError("the Reddit post could not be read")
    n = normalise_reddit(raw[0], cfg["fields"]["reddit"], _t.time()) or {}
    img = (n.get("metrics") or {}).get("image")
    return {"platform": "reddit", "author": f"r/{n.get('subreddit') or ''}", "title": n.get("title") or "",
            "text": (n.get("text") or "")[:MAX_TEXT], "images": [img] if img else [], "published": ""}


def read_link(url: str, apify=None, cfg: dict | None = None) -> dict:
    host = urlsplit(url).netloc.lower()
    if host.endswith(("x.com", "twitter.com")):
        return read_x(url)
    if host.endswith(("reddit.com", "redd.it")):
        return read_reddit(url, apify, cfg)
    return read_article(url)


def write_link_brief(run: Path, req: dict, cfg: dict, recent: list[dict] | None = None, apify=None) -> Path:
    """Brief for a post an editor asked for by link. If the page cannot be read the brief says so
    (`link.read_error`) and the routine reads it another way or reports it back."""
    from .config import load_pages
    from .routine import COPY_SCHEMA_DOC, UPDATE_SCHEMA, load_playbook, post_id
    page = next((p for p in load_pages() if p.slug == req["page"]), None)
    slug = re.sub(r"[^a-z0-9]+", "-", (urlsplit(req["url"]).path or "link").lower()).strip("-")[-40:] or "link"
    pid = post_id(req["page"], f"link-{slug}")
    d = run / pid
    (d / "photos").mkdir(parents=True, exist_ok=True)
    art, err = {}, ""
    try:
        art = read_link(req["url"], apify, cfg)
        if not (art.get("title") or art.get("text")):
            err = "the page returned no readable text"
    except Exception as e:
        err = f"could not read the link ({type(e).__name__}: {str(e)[:160]})"
    photos = []
    cands = [Photo(x_orig(u), art.get("platform", "web"), domain(req["url"])) for u in art.get("images", [])]
    # every image of a gallery, in its order (the editor may want them posted as they are)
    # (small ones too: it may be the post's only image; lint warns below the card floor and refuses
    # only below guard.LINK_PHOTO_MIN)
    for i, ph in enumerate(best(cands, 300, limit=MAX_LINK_IMAGES, keep_order=True)[:MAX_LINK_IMAGES]):
        ext = "png" if (ph.data or b"")[:4] == b"\x89PNG" else "jpg"
        (d / f"photos/{i}.{ext}").write_bytes(ph.data)
        photos.append({"index": i, "file": f"photos/{i}.{ext}", "url": ph.url, "source": "link",
                       "credit": ph.credit, "size": f"{ph.w}x{ph.h}"})
    text = "\n".join(x for x in (art.get("title"), art.get("text")) if x)
    brief = {
        "id": pid, "page": req["page"], "page_name": page.name if page else req["page"], "rank": 0, "backup": False,
        "verification": {"route": "manual", "sources": [f"{art.get('platform', 'web')}:{art.get('author') or domain(req['url'])}"],
                         "platforms": [art.get("platform", "web")], "engagement": 0, "developed": ""},
        "headline_hint": (art.get("title") or text)[:280],
        "posts": [{"platform": art.get("platform", "web"), "author": art.get("author"), "text": text,
                   "url": req["url"], "engagement": 0, "hours_ago": 0}] if text else [],
        "photos": photos, "key": f"link-{slug}", "signature": [],
        "recent_posts": [{"id": r.get("id"), "status": r.get("status"), "headline": r.get("headline")}
                         for r in (recent or []) if r.get("page") == req["page"]][:60],
        "playbook": load_playbook(req["page"]), "copy_schema": {**COPY_SCHEMA_DOC, **UPDATE_SCHEMA},
        "link": {"request_id": req["id"], "url": req["url"], "note": req.get("note", ""),
                 "images_found": len(art.get("images") or []), "images_kept": len(photos),
                 **({"flags": art["flags"]} if art.get("flags") else {}),
                 "published": art.get("published", ""), "read_error": err, "requested_at": req.get("at", 0)},
    }
    (d / "brief.json").write_text(json.dumps(brief, indent=1, ensure_ascii=False))
    return d


def add_source_text(d: Path, text: str, url: str = "") -> None:
    """When the routine had to read the link itself (paywall, bot block), it records what the page
    says here, so lint checks the copy against it like any other source."""
    brief = json.loads((d / "brief.json").read_text())
    brief["posts"].append({"platform": "web", "author": domain(url or brief["link"]["url"]),
                           "text": text[:MAX_TEXT], "url": url or brief["link"]["url"], "engagement": 0,
                           "hours_ago": 0, "read_by": "routine"})
    brief["headline_hint"] = brief.get("headline_hint") or text[:280]
    (d / "brief.json").write_text(json.dumps(brief, indent=1, ensure_ascii=False))


__all__ = ["read_link", "write_link_brief", "add_source_text"]
