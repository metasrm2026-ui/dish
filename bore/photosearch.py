"""Photos of a named subject, for `routine photos` and the Studio's 'Search more images'.

The old search (a Bing image scrape, then Wikimedia) returned unrelated junk for most queries
from our network (a 'Carson Schwesinger Browns' search gave Hamburger Helper mascots) and matched
only a surname substring ('Campbell' matched Campbell's soup). Measured on 12 subjects, this
pipeline's first three photos were the right person far more often:

  1. news photos: lead images of articles whose title names the subject (Bing News RSS)
  2. DuckDuckGo images ('<Full Name> <team>' for players): sizes known before downloading
  3. official NFL headshot (ESPN lookup for the current team, NFL.com): always the right person,
     a studio portrait, offered last
  4. Wikimedia Commons (licensed; rate-limited per IP: Retry-After honoured, thumbnails used)
  5. the Bing image scrape, alongside DuckDuckGo; a batch that is mostly unrelated is dropped

Every result must name the subject in full, as a phrase (a middle initial, Jr. or a plural
allowed), in its title or URL; stock, shop and content-farm sites are dropped; each source
reports what it found or why it failed. One time limit covers the whole search. The kept photos
are ranked by whose face they show (bore/faceid.py): the subject's first, 'may not be' last.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

import requests

from . import ops
from .media import STOCK_DOMAINS, UA, Photo, flatten, og_image
from .textutil import domain

FARM_DOMAINS = ("inspiredpencil", "fity.club", "wallpapers.com", "playersbio", "trendingcelebs", "picsofcelebrities",
                "alchetron", "ebay.", "amazon.", "etsy.", "fanatics.", "redbubble.", "tcdb.", "comc.", "vecteezy.",
                "freepik.", "tenor.", "giphy.", "wallpaperaccess", "wallpapercave", "pngtree", "pngwing", "cleanpng")
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", unquote(text or "")).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"['’]|(?<=\b[a-z])\.", "", t)             # D'Andre -> dandre, J.J. -> jj (a domain's dot stays a gap)
    return " " + re.sub(r"[^a-z0-9]+", " ", t).strip() + " "


def name_tokens(name: str) -> list[str]:
    return [t for t in _norm(name).split() if t not in SUFFIXES]


def _word(t: str) -> str:
    """A word, or its plural (butterfly / butterflies, photo / photos)."""
    if t.endswith("y") and len(t) > 2:
        return f"{t[:-1]}(?:y|ies)"
    return f"{re.escape(t)}(?:e?s)?"


def name_match(name: str, *texts: str) -> bool:
    """The name, as a phrase, in the texts (titles, URL slugs): a middle initial between its
    words, and Jr. or III after it, are allowed. 'Campbell will start at LT' is not Will Campbell."""
    toks = name_tokens(name)
    if not toks:
        return False
    hay = _norm(" ".join(t or "" for t in texts))
    return bool(re.search(" " + "(?: [a-z])? ".join(map(_word, toks)) + " ", hay))


def subject_name(query: str, must: str = "") -> str:
    """The words results must name: --must, or the query. An old-style surname-only --must that is
    in the query means the name in the query up to it ('Will Campbell Patriots' + 'Campbell')."""
    must = " ".join(w for w in re.split(r"[,\s]+", must or "") if w)
    if not must:
        return query
    q, m = name_tokens(query), name_tokens(must)
    if len(m) == 1 and m[0] in q:
        i = q.index(m[0])
        return " ".join(q[max(0, i - 2): i + 1]) if i else must
    return must


def _blocked(*urls: str) -> bool:
    low = " ".join(u or "" for u in urls).lower()
    return any(d in low for d in STOCK_DOMAINS + FARM_DOMAINS)


def _rep(source: str, query: str, t0: float, found: int = 0, kept: int = 0, error: str = "", **dropped) -> dict:
    return {"source": source, "query": query, "results": found, "kept": kept, "error": error,
            "dropped": {k: v for k, v in dropped.items() if v}, "ms": int((time.time() - t0) * 1000)}


# ------------------------------------------------------------------ sources
def ddg_images(query: str, name: str, floor: int = 600, n: int = 12) -> tuple[list[Photo], dict]:
    from .consensus import ddg_throttle, engine_result, resting
    t0 = time.time()
    if resting("duckduckgo images"):
        return [], _rep("duckduckgo", query, t0, error="resting after repeated failures")
    s = requests.Session()
    s.headers.update(UA)
    for attempt in range(2):
        ddg_throttle()             # shared with the news search: one IP
        try:
            r = s.get("https://duckduckgo.com/", params={"q": query, "iax": "images", "ia": "images"}, timeout=10)
            m = re.search(r'vqd=["\']?([\d-]+)', r.text)
            if not m:
                raise RuntimeError("no search token")
            r = s.get("https://duckduckgo.com/i.js", timeout=10,
                      params={"l": "us-en", "o": "json", "q": query, "vqd": m.group(1), "f": ",,,,,", "p": "1"},
                      headers={"Referer": "https://duckduckgo.com/", "Accept": "application/json, text/javascript, */*; q=0.01",
                               "X-Requested-With": "XMLHttpRequest", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            res = r.json().get("results") or []
            break
        except Exception as e:
            if ops.fault(e, "duckduckgo image search"):
                raise
            if attempt:
                engine_result("duckduckgo images", False)
                return [], _rep("duckduckgo", query, t0, error=str(e)[:120])
            time.sleep(3)
    engine_result("duckduckgo images", True)
    out, small, wrong, blocked = [], 0, 0, 0
    for x in res:
        img, page, title = x.get("image") or "", x.get("url") or "", x.get("title") or ""
        if _blocked(img, page):
            blocked += 1
        elif min(int(x.get("width") or 0), int(x.get("height") or 0)) < floor:
            small += 1
        elif not name_match(name, title, page, img):
            wrong += 1
        else:
            out.append(Photo(img, "search", domain(page) or domain(img), int(x.get("width") or 0), int(x.get("height") or 0),
                             via=title[:100], post_url=page))
        if len(out) >= n:
            break
    return out, _rep("duckduckgo", query, t0, len(res), len(out), small=small, other_subject=wrong, stock_or_shop=blocked)


OG_UPGRADES = [
    (re.compile(r"-\d{2,4}x\d{2,4}(\.(?:jpe?g|png|webp))$", re.I), r"\1"),                     # WordPress size suffix
    (re.compile(r"/image/upload/t_[a-z0-9_]*(?:headshot|thumb|lazy|desktop|mobile|tablet|landscape|portrait|square|"
                r"small|medium|large|\d{3,4})[a-z0-9_]*/"), "/image/upload/"),                     # Cloudinary size preset
    (re.compile(r"[?&](w|h|width|height|resize|fit|crop|quality|strip)=[^&]*"), ""),            # resize parameters
]
# a transform that may stamp a logo or a watermark: such a URL is never rewritten (never remove a watermark)
OVERLAY_RE = re.compile(r"[/,]l_|overlay|watermark|[/_,]wm[_,/]|logo|brand|[?&](mark|blend|txt)[\w-]*=", re.I)


def unstamp(url: str) -> str:
    """A Cloudinary size preset removed (the full upload), unless the URL may carry an overlay."""
    return url if OVERLAY_RE.search(url) else OG_UPGRADES[1][0].sub("/image/upload/", url)


def og_upgrade(url: str) -> list[str]:
    """Larger versions of a news site's lead image to try first (the original stays the fallback).
    Only size, crop and quality settings are dropped; a URL with an overlay is left as it is."""
    if OVERLAY_RE.search(url):
        return [url]
    out = []
    for rx, rep in OG_UPGRADES:
        u = rx.sub(rep, url)
        if u != url:
            out.append(re.sub(r"[?&]$", "", u.replace("?&", "?")))
    m = re.match(r"https://a\.espncdn\.com/combiner/i\?img=([^&]+)", url)
    if m:
        out.append("https://a.espncdn.com" + unquote(m.group(1)))
    return list(dict.fromkeys(out)) + [url]


def subject_news_photos(name: str, n: int = 6) -> tuple[list[Photo], dict]:
    """Lead photos of recent articles whose title names the subject."""
    from html import unescape
    from urllib.parse import parse_qs
    t0 = time.time()
    try:
        r = requests.get("https://www.bing.com/news/search", headers=UA, timeout=8,
                         params={"q": f'"{name}"', "format": "rss", "setlang": "en-us", "cc": "US"})
        items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
    except requests.RequestException as e:
        return [], _rep("news", name, t0, error=str(e)[:120])
    arts, wrong = [], 0
    for it in items:
        title = unescape((re.search(r"<title>(.*?)</title>", it, re.S) or [None, ""])[1])
        link = unescape((re.search(r"<link>(.*?)</link>", it, re.S) or [None, ""])[1])
        url = (parse_qs(urlsplit(link).query).get("url") or [link])[0]
        if not name_match(name, title):
            wrong += 1
        elif url.startswith("http") and not _blocked(url) and "msn.com" not in url:
            arts.append((url, title))
    with ThreadPoolExecutor(6) as ex:
        imgs = list(ex.map(lambda a: og_image(a[0]), arts[: n + 2]))
    out, seen = [], set()
    for (url, title), img in zip(arts, imgs):
        if img and img not in seen and not _blocked(img):
            seen.add(img)
            out.append(Photo(img, "news", domain(url), via=title[:100], post_url=url, note="upgrade"))
    return out[:n], _rep("news", name, t0, len(items), len(out[:n]), other_subject=wrong)


def nfl_player(name: str, strict: bool = False) -> dict:
    """ESPN's player lookup: the current team (fresh after trades), and the headshot, which is only
    ever the reference face for bore/faceid.py (never offered as a photo). {} when ESPN knows no
    such NFL player or could not be asked; strict: the latter raises (a cache must not keep it)."""
    try:
        r = requests.get("https://site.web.api.espn.com/apis/common/v3/search", headers=UA, timeout=8,
                         params={"query": name, "type": "player", "limit": 5})
        r.raise_for_status()
        for it in r.json().get("items") or []:
            if it.get("league") == "nfl" and name_match(name, it.get("displayName") or ""):
                team = ((it.get("teamRelationships") or [{}])[0] or {}).get("displayName") or ""
                return {"name": it.get("displayName"), "team": team, "headshot": (it.get("headshot") or {}).get("href") or ""}
    except Exception as e:
        if ops.fault(e, "NFL player lookup") or strict:
            raise
    return {}


def official_nfl(name: str) -> tuple[list[Photo], dict]:
    """The player's official NFL.com headshot (a studio portrait on grey): always the right person."""
    from PIL import Image
    t0 = time.time()
    toks = [t for t in _norm(name).split()]
    slugs = list(dict.fromkeys(["-".join(toks), "-".join(t for t in toks if t not in SUFFIXES)]))
    for slug in slugs:
        og = og_image(f"https://www.nfl.com/players/{slug}/")
        if not og or "headshot" not in og and "/league/" not in og:
            continue
        big = unstamp(og)
        try:
            r = requests.get(big, headers=UA, timeout=20)
            im = flatten(Image.open(io.BytesIO(r.content)))
            im.thumbnail((2400, 2400))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=92)
        except Exception as e:
            if ops.fault(e, "official headshot"):
                raise
            return [], _rep("official", name, t0, error=str(e)[:120])
        ph = Photo(big, "official", "NFL.com official headshot", im.size[0], im.size[1], buf.getvalue(),
                   via="official headshot (studio portrait)", post_url=f"https://www.nfl.com/players/{slug}/")
        ph.flags.append("official headshot (studio portrait)")
        return [ph], _rep("official", name, t0, 1, 1)
    return [], _rep("official", name, t0, error="no NFL.com player page")


def wikimedia(name: str, must: str = "", max_wait: float = 10) -> tuple[list[Photo], dict]:
    """Licensed photos from Wikimedia Commons (thumbnails: originals are rate-limited)."""
    t0 = time.time()
    ua = {"User-Agent": os.environ.get("WIKIMEDIA_UA") or "BorePostStudio/1.0 (photo search for editors)"}
    params = {"action": "query", "generator": "search", "gsrsearch": f"{name} filetype:bitmap", "gsrnamespace": 6,
              "gsrlimit": 20, "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 1280,
              "iiextmetadatafilter": "LicenseShortName|Artist|ImageDescription", "format": "json"}
    for attempt in range(2):
        try:
            r = requests.get("https://commons.wikimedia.org/w/api.php", headers=ua, timeout=15, params=params)
        except requests.RequestException as e:
            return [], _rep("wikimedia", name, t0, error=str(e)[:120])
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After") or 60)
            if attempt or wait > max_wait:
                return [], _rep("wikimedia", name, t0, error=f"HTTP 429 (retry after {wait:.0f}s)")
            time.sleep(wait)
            continue
        break
    try:
        pages = (r.json().get("query") or {}).get("pages") or {}
    except ValueError:
        return [], _rep("wikimedia", name, t0, error=f"HTTP {r.status_code}")
    out, wrong = [], 0
    for pg in pages.values():
        ii = (pg.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        desc = re.sub(r"<[^>]+>", " ", (meta.get("ImageDescription") or {}).get("value") or "")
        if not name_match(must or name, pg.get("title") or "", desc):
            wrong += 1
            continue
        artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value") or "").strip()[:60]
        lic = (meta.get("LicenseShortName") or {}).get("value") or ""
        url = ii.get("thumburl") or ii.get("url")
        if url:
            ph = Photo(url, "commons", f"{artist or 'Wikimedia Commons'} / Wikimedia Commons", ii.get("thumbwidth") or 0,
                       ii.get("thumbheight") or 0, via=(pg.get("title") or "")[:100], post_url=ii.get("descriptionurl") or "")
            if lic:
                ph.flags.append(f"licence: {lic} (credit the author)")
            out.append(ph)
    return out[:6], _rep("wikimedia", name, t0, len(pages), len(out[:6]), other_subject=wrong)


def bing_images(query: str, name: str) -> tuple[list[Photo], dict]:
    """The Bing image scrape: often bot-blocked (unrelated results); dropped when it is."""
    import html as _html
    t0 = time.time()
    try:
        r = requests.get("https://www.bing.com/images/async", headers=UA, timeout=15,
                         params={"q": query, "first": "0", "count": "35", "qft": "+filterui:imagesize-large",
                                 "mmasync": "1", "setlang": "en-us", "cc": "US"})
        items = [json.loads(_html.unescape(m)) for m in re.findall(r'\sm="(\{[^"]+\})"', r.text)]
    except Exception as e:
        if ops.fault(e, "bing image search"):
            raise
        return [], _rep("bing", query, t0, error=str(e)[:120])
    named = [it for it in items if name_match(name, it.get("t", ""), it.get("purl", ""), it.get("murl", ""))]
    if items and len(named) < 0.2 * len(items):
        return [], _rep("bing", query, t0, len(items), 0, error="unrelated results (bot-blocked)")
    out = [Photo(it["murl"], "search", domain(it.get("purl") or it["murl"]), via=(it.get("t") or "")[:100], post_url=it.get("purl") or "")
           for it in named if it.get("murl") and not _blocked(it.get("murl"), it.get("purl"))]
    return out[:8], _rep("bing", query, t0, len(items), len(out[:8]), other_subject=len(items) - len(named))


# ------------------------------------------------------------------ the search
def sharp_first(photos: list[Photo], learned: dict | None = None) -> list[Photo]:
    """Sharp before soft or small; then, between equals, the kind editors kept (bore/lessons.py);
    otherwise the order the sources gave."""
    from .lessons import photo_weight
    return sorted(photos, key=lambda ph: (any(f.startswith("soft") for f in ph.flags), min(ph.w, ph.h) < 800,
                                          -photo_weight(learned, ph)))


def search_subject(query: str, name: str = "", team: str = "", sports: bool = False, floor: int = 600,
                   limit: int = 8, deadline_s: float = 20, official: bool = True,
                   commons: bool = True, learned: dict | None = None) -> tuple[list[Photo], list[dict]]:
    """Downloaded, checked photos of a subject, best first, and a report per source.
    official: the NFL.com headshot (a cutout composited on grey: only where the downloaded file
    itself is used, i.e. the hourly routine, never as a URL an editor picks). Sources still
    running at the deadline are reported and left out. learned: the page's lessons (ties only)."""
    from concurrent.futures import wait

    from . import faceid
    from .media import dhash, fetch, graphic
    t0 = time.time()
    name = name or query
    player = nfl_player(name) if sports else {}
    if not team and player.get("team"):
        team = player["team"].split()[-1]                  # "New England Patriots" -> "Patriots"
    q = f"{name} {team}".strip() if sports and team else query
    # the subject's face (ESPN headshot / Wikipedia), looked up while the sources run (no answer
    # from ESPN: asked again there, so a failed search is not remembered as 'not a player')
    ref = faceid.start_reference(name, sports, player or None) if faceid.available() else None
    jobs = {"news": lambda: subject_news_photos(name), "duckduckgo": lambda: ddg_images(q, name, floor),
            "bing": lambda: bing_images(q, name)}
    if commons:
        jobs["wikimedia"] = lambda: wikimedia(name, max_wait=min(10, deadline_s / 2))
    if official and sports and player:
        jobs["official"] = lambda: official_nfl(player.get("name") or name)
    ex = ThreadPoolExecutor(len(jobs))
    futs = {k: ex.submit(f) for k, f in jobs.items()}
    wait(list(futs.values()), timeout=max(1, deadline_s - (time.time() - t0)))
    ex.shutdown(wait=False, cancel_futures=True)
    got, report = {}, []
    for f in futs.values():                  # a source that hit a broken machine: the search fails, it does not shrink
        if f.done() and not f.cancelled() and f.exception() and ops.fault(f.exception(), "photo search"):
            raise f.exception()
    for k, f in futs.items():
        if f.done() and not f.cancelled() and not f.exception():
            got[k] = f.result()
            report.append(got[k][1])
        else:
            got[k] = ([], {})
            report.append({"source": k, "query": q, "results": 0, "kept": 0, "error": "timed out" if not f.done() or f.cancelled()
                           else f"{type(f.exception()).__name__}", "dropped": {}, "ms": int((time.time() - t0) * 1000)})
    order = [*got["news"][0], *got["duckduckgo"][0], *got["bing"][0], *got.get("wikimedia", ([], {}))[0]]
    out, seen, urls, dropped = [], [], set(), {}
    checks_until = t0 + deadline_s + 15                    # downloads and checks: a little longer

    def keep(ph) -> bool:
        if ph.url in urls:
            return False
        urls.add(ph.url)
        if not ph.data:
            for u in (og_upgrade(ph.url) if ph.note == "upgrade" else [ph.url]):
                trial = Photo(u, ph.source, ph.credit, via=ph.via, post_url=ph.post_url)
                if fetch(trial) and min(trial.w, trial.h) >= floor:
                    ph.url, ph.w, ph.h, ph.data = trial.url, trial.w, trial.h, trial.data
                    break
            ph.note = ""
        if not ph.data or min(ph.w, ph.h) < floor:
            dropped["small or unreadable"] = dropped.get("small or unreadable", 0) + 1
            return False
        h = dhash(ph.data)
        if any(bin(h ^ x).count("1") <= 8 for x in seen):
            return False
        why = ""
        try:
            why = graphic(ph, sports=sports)
            if not why:
                from .consensus import _seam
                from PIL import Image
                if _seam(Image.open(io.BytesIO(ph.data))) > 0.9:
                    why = "collage"
        except Exception as e:
            if ops.fault(e, "photo search graphic check"):  # no check can run here: never pass a graphic as a photo
                raise
            ops.degraded("photo search graphic check failed")
        if why:
            dropped[why.split(" (")[0]] = dropped.get(why.split(" (")[0], 0) + 1
            return False
        seen.append(h)
        if min(ph.w, ph.h) < 800:
            ph.flags.append("small: may look soft on a card")
        return True
    for ph in order:
        if len(out) >= limit:
            break
        if time.time() > checks_until:
            dropped["not checked in time"] = dropped.get("not checked in time", 0) + 1
            continue
        if keep(ph):
            out.append(ph)
    out = sharp_first(out, learned)
    if out and ref is not None:     # the subject's face first, then unknown, then 'may not be' (none dropped)
        report.append(faceid.label(out, name, sports, deadline_s=max(3.0, min(8.0, checks_until - time.time())), ref=ref))
        out.sort(key=faceid.id_rank)
    for ph in got.get("official", ([], {}))[0]:        # always the right person: offered last
        out.append(ph)
    if dropped:
        report.append({"source": "checks", "dropped": dropped})
    return out[: limit + 1], report


def print_report(report: list[dict]) -> None:
    from .faceid import describe
    for r in report:
        if r.get("source") == "checks":
            print(f"  checks dropped {r['dropped']}")
        elif r.get("source") == "faces":
            print(f"  {describe(r)}")
        elif r.get("error"):
            print(f"  {r['source']}: {r['error']} ({r.get('ms', 0) / 1000:.1f}s)")
        else:
            print(f"  {r['source']}: {r.get('results', 0)} results, {r.get('kept', 0)} kept"
                  + (f", dropped {r['dropped']}" if r.get("dropped") else "") + f" ({r.get('ms', 0) / 1000:.1f}s)")


__all__ = ["search_subject", "sharp_first", "name_match", "name_tokens", "og_upgrade", "ddg_images", "subject_news_photos",
           "official_nfl", "nfl_player", "wikimedia", "bing_images", "print_report"]
