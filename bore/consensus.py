"""The image other outlets used for a story.

Culture Rush's single source often leads with a stock or file photo (a generic tiger for the tiger
attack) while every other outlet used the same still of the event. This finds that picture for
free: news search (Bing News RSS + DuckDuckGo news) for the story -> each article's lead image ->
same-picture groups (perceptual hashes on border-trimmed centre crops, then ORB + RANSAC with a
patch check for crops, overlays and watermarks) -> groups ranked by distinct outlets (syndicated
copies count once). Groups used by at least two outlets come back as candidate photos, best first;
the editors (or the routine) still choose, and every image says which outlets used it.
"""
from __future__ import annotations

import html
import io
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import requests

from . import ops
from .media import STOCK_DOMAINS, UA, Photo, og_image
from .textutil import tokens

P_MAX, D_MAX = 12, 14                       # pHash / dHash distance for the same picture
ORB_MIN_INL, AGREE_MIN, NCC_MIN = 12, 0.65, 0.70
MIN_OUTLETS = 2
IMG_DEADLINE = 6.0
ASPECTS = (1.0, 4 / 3, 3 / 2, 16 / 9, 4 / 5)
AGG = ("msn.com", "yahoo.com", "aol.com", "aol.co.uk", "newsbreak.com", "flipboard.com", "inkl.com")
LOGO_TOKENS = {"logo", "logos", "default", "placeholder", "fallback", "favicon", "sprite", "avatar", "blank",
               "no-image", "noimage", "og-default", "default-og", "share-default", "site-image", "social-default"}
GENERIC = set("""internet netizens fans fan viewers people social media viral video videos clip footage online
reacts react reacting responds respond responded shares share shared sharing slams slam claims claim says said
reveals revealed outraged outrage after amid as over with into from the a an of in on at to for and but by is was
were be has have had his her their its this that these those who what why how when where just weeks days post
report reports reportedly allegedly new watch photos photo update breaking exclusive inside everything know
tries try trying ends end no plans put down get gets got goes going makes made stands stand stood""".split())
PRON = set("i me my you your he him his she her it its we us our they them their who whom whose this that".split())
ADJ = set("heartbreaking shocking devastating alleged elite former late star alum post-divorce ex reportedly".split())

_S = requests.Session()
_S.headers.update(UA)


def _get(url, timeout=10, **kw):
    for attempt in range(2):
        try:
            return _S.get(url, timeout=timeout, **kw)
        except requests.RequestException:
            if attempt:
                return None
            time.sleep(0.3)


# ------------------------------------------------------------------ query
def build_query(headline: str, max_terms: int = 5, max_props: int = 4, max_keys: int = 2) -> str:
    """A short news query: the headline's names plus one or two event words (quotes dropped)."""
    h = re.sub(r'^\s*["“‘\'].{3,200}?["”’\']\s*[:\-–—]\s*', "", headline or "")
    h = re.sub(r'["“”][^"“”]{1,200}["“”]', " ", h)
    h = re.sub(r"[‘’']s\b", "", h).replace("‘", " ").replace("’", " ")
    props, keys = [], []
    for w in re.findall(r"St\.|[A-Za-z][A-Za-z\-]*[A-Za-z]|[A-Za-z]", h):
        lw = w.lower()
        if lw in GENERIC or lw in PRON or (len(lw) < 3 and not w.isupper()) or len(lw) < 2:
            continue
        if w[0].isupper():
            props.append(w)
        elif len(lw) >= 4 and lw not in ADJ:
            keys.append(w)
    props = props[:max_props]
    extra = [k for k in keys if k.lower() not in {t.lower() for t in props}]
    terms = props + extra[:max(max_keys, max_terms - len(props)) if len(props) < 2 else max_keys]
    return " ".join(terms[:max_terms])


# ------------------------------------------------------------------ search
@dataclass
class Art:
    url: str
    title: str
    outlet: str
    domain: str
    ts: float
    engine: str
    rank: int
    img: str = ""
    thumb: str = ""
    maxw: int = 0
    maxh: int = 0
    h: dict = field(default_factory=dict)
    f: tuple | None = None
    w: int = 0
    hh: int = 0
    data: bytes = b""
    flags: list = field(default_factory=list)
    seam: float = 0.0
    oid: str = ""
    wider: bool = False


def _dom(u: str) -> str:
    d = urllib.parse.urlsplit(u).netloc.lower()
    return d[4:] if d.startswith("www.") else d


def _outlet_key(name: str, url: str) -> str:
    n = re.sub(r"\s+on MSN$", "", name or "").strip()
    n = re.sub(r"\.(com|co\.uk|net|org)$", "", n, flags=re.I)
    return (n or _dom(url)).lower()


_DOWN: dict[str, list] = {}           # engine -> [consecutive failures, skip until]
_DDG_LOCK, _DDG_LAST = threading.Lock(), [0.0]


def ddg_throttle(gap: float = 1.5) -> None:
    """News and image searches on DuckDuckGo come from one IP: at least `gap` seconds between searches."""
    with _DDG_LOCK:
        wait = gap - (time.time() - _DDG_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _DDG_LAST[0] = time.time()


def resting(name: str) -> bool:
    return time.time() < _DOWN.setdefault(name, [0, 0.0])[1]


def engine_result(name: str, ok: bool) -> None:
    """Count an engine's failure (twice in a row: it rests 30 minutes) or its success."""
    st = _DOWN.setdefault(name, [0, 0.0])
    if ok:
        st[0] = 0
        return
    st[0] += 1
    if st[0] >= 2:
        st[1] = time.time() + 1800
        ops.degraded(f"{name} search unavailable")
        print(f"  {name} search unavailable: skipped for 30 minutes")


def _engine(name: str, fn, q: str) -> list:
    """A search engine call that never raises; an engine failing twice in a row rests 30 minutes."""
    if resting(name):
        return []
    try:
        out = fn(q)
    except Exception as e:
        if ops.fault(e, f"{name} search"):
            raise
        out = None
    engine_result(name, out is not None)
    return out or []


def bing_news(q: str) -> list[Art]:
    r = _get("https://www.bing.com/news/search", params={"q": q, "format": "rss", "setlang": "en-us", "cc": "US"}, timeout=6)
    out = []
    if not r or r.status_code != 200:
        return None
    for i, it in enumerate(re.findall(r"<item>(.*?)</item>", r.text, re.S)):
        def tag(n):
            m = re.search(rf"<{n}>(.*?)</{n}>", it, re.S)
            return html.unescape(m.group(1)).strip() if m else ""
        link = tag("link")
        u = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get("url", [link])[0]
        try:
            ts = parsedate_to_datetime(tag("pubDate")).timestamp()
        except (TypeError, ValueError):
            ts = 0
        out.append(Art(u, tag("title"), _outlet_key(tag("News:Source"), u), _dom(u), ts, "bing", i,
                       thumb=tag("News:Image"), maxw=int(tag("News:ImageMaxWidth") or 0),
                       maxh=int(tag("News:ImageMaxHeight") or 0)))
    return out


def ddg_news(q: str) -> list[Art]:
    ddg_throttle()
    r = _get("https://duckduckgo.com/", params={"q": q, "iar": "news", "ia": "news"}, timeout=6)
    m = r is not None and re.search(r'vqd=["\']?([\d-]+)', r.text)
    if not m:
        return None
    r = _get("https://duckduckgo.com/news.js", params={"l": "us-en", "o": "json", "noamp": "1", "q": q,
                                                       "vqd": m.group(1), "p": "-1", "df": "w"},
             headers={**UA, "Referer": "https://duckduckgo.com/"}, timeout=6)
    try:
        res = r.json().get("results", []) if r is not None else None
    except ValueError:
        return None
    if res is None:
        return None
    return [Art(x.get("url") or "", x.get("title") or "", _outlet_key(x.get("source") or "", x.get("url") or ""),
                _dom(x.get("url") or ""), float(x.get("date") or 0), "ddg", i, img=x.get("image") or "")
            for i, x in enumerate(res) if x.get("url")]


# ------------------------------------------------------------------ pictures
def trim_borders(im):
    """Drop uniform letterbox / pillarbox bars around a picture."""
    import numpy as np
    g = np.asarray(im.convert("L"), dtype=np.int16)
    h, w = g.shape
    ref = np.median(np.concatenate([g[0], g[-1], g[:, 0], g[:, -1]]))

    def uniform(line):
        return line.std() < 6 and abs(int(line.mean()) - int(ref)) < 18
    top, bot, left, right = 0, h, 0, w
    while top < h * 0.4 and uniform(g[top]):
        top += 1
    while bot > h * 0.6 and uniform(g[bot - 1]):
        bot -= 1
    while left < w * 0.4 and uniform(g[top:bot, left]):
        left += 1
    while right > w * 0.6 and uniform(g[top:bot, right - 1]):
        right -= 1
    if (bot - top) < h * 0.3 or (right - left) < w * 0.3:
        return im
    return im.crop((left, top, right, bot)) if (top, bot, left, right) != (0, h, 0, w) else im


def _center_crop(im, aspect: float):
    w, h = im.size
    if w / h > aspect:
        nw = int(h * aspect)
        x = (w - nw) // 2
        return im.crop((x, 0, x + nw, h))
    nh = int(w / aspect)
    y = (h - nh) // 2
    return im.crop((0, y, w, y + nh))


def _phash(im) -> int:
    import cv2
    import numpy as np
    from PIL import Image
    a = np.asarray(im.convert("L").resize((32, 32), Image.LANCZOS), dtype=np.float32)
    low = cv2.dct(a)[:8, :8].flatten()[1:]
    return int("".join("1" if b else "0" for b in low > np.median(low)), 2)


def _dhash(im) -> int:
    import numpy as np
    from PIL import Image
    a = np.asarray(im.convert("L").resize((9, 8), Image.LANCZOS), dtype=np.int16)
    return int("".join("1" if b else "0" for b in (a[:, 1:] > a[:, :-1]).flatten()), 2)


def picture_hash(data: bytes) -> int:
    """The full-picture pHash consensus groups carry (Photo.phash), for matching another image."""
    from PIL import Image
    return _phash(trim_borders(Image.open(io.BytesIO(data)).convert("RGB")))


def hashes(im) -> dict:
    im = trim_borders(im.convert("RGB"))
    out = {"full": (_phash(im), _dhash(im))}
    for a in ASPECTS:
        c = _center_crop(im, a)
        out[f"{a:.2f}"] = (_phash(c), _dhash(c))
    return out


def _ham(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dist(h1: dict, h2: dict) -> tuple[int, int, str]:
    best = (99, 99, "")
    for k in h1:
        if k in h2:
            p, d = _ham(h1[k][0], h2[k][0]), _ham(h1[k][1], h2[k][1])
            if p + d < best[0] + best[1]:
                best = (p, d, k)
    return best


def _prep(im, side: int = 480):
    import cv2
    import numpy as np
    g = np.asarray(trim_borders(im).convert("L"))
    h, w = g.shape
    s = side / max(h, w)
    if s < 1:
        g = cv2.resize(g, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    k, d = cv2.ORB_create(500).detectAndCompute(g, None)
    return g, k, d


def _verify(A, B) -> dict:
    """ORB + RANSAC similarity, then A warped onto B: share of textured 24px patches that agree."""
    import cv2
    import numpy as np
    (g1, k1, d1), (g2, k2, d2) = A, B
    if d1 is None or d2 is None or len(k1) < 8 or len(k2) < 8:
        return {"inl": 0}
    m = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(d1, d2, k=2)
    good = [p[0] for p in m if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < ORB_MIN_INL:
        return {"inl": len(good)}
    p1 = np.float32([k1[g.queryIdx].pt for g in good])
    p2 = np.float32([k2[g.trainIdx].pt for g in good])
    M, mask = cv2.estimateAffinePartial2D(p1, p2, method=cv2.RANSAC, ransacReprojThreshold=6.0)
    if M is None:
        return {"inl": 0}
    inl, scale = int(mask.sum()), float(np.hypot(M[0, 0], M[0, 1]))
    if inl < ORB_MIN_INL or not 0.2 < scale < 5:
        return {"inl": inl}
    h2, w2 = g2.shape
    f1 = cv2.GaussianBlur(g1, (0, 0), 1.5).astype(np.float32)
    f2 = cv2.GaussianBlur(g2, (0, 0), 1.5).astype(np.float32)
    warped = cv2.warpAffine(f1, M, (w2, h2))
    valid = cv2.erode(cv2.warpAffine(np.full(g1.shape, 255, np.uint8), M, (w2, h2), flags=cv2.INTER_NEAREST),
                      np.ones((7, 7), np.uint8)) > 0
    P = 24
    hh, ww = (h2 // P) * P, (w2 // P) * P
    if hh == 0 or ww == 0:
        return {"inl": inl}

    def tiles(x):
        return x[:hh, :ww].reshape(hh // P, P, ww // P, P).swapaxes(1, 2).reshape(-1, P * P)
    a, b, v = tiles(warped).astype(np.float64), tiles(f2).astype(np.float64), tiles(valid).all(axis=1)
    keep = v & (a.std(axis=1) >= 6) & (b.std(axis=1) >= 6)
    if keep.sum() < 8:
        return {"inl": inl}
    a = a[keep] - a[keep].mean(axis=1, keepdims=True)
    b = b[keep] - b[keep].mean(axis=1, keepdims=True)
    pn = (a * b).sum(1) / (np.sqrt((a * a).sum(1) * (b * b).sum(1)) + 1e-9)
    x, y = warped[valid].astype(np.float64), f2[valid].astype(np.float64)
    x -= x.mean()
    y -= y.mean()
    ncc = float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-9))
    return {"inl": inl, "agree": float((pn > 0.6).mean()), "ncc": ncc, "cover": float(valid.mean())}


def same_picture(x: Art, y: Art) -> bool:
    p, d, _ = dist(x.h, y.h)
    if p <= P_MAX and d <= D_MAX:
        return True
    for A, B, big in ((x.f, y.f, y), (y.f, x.f, x)):
        v = _verify(A, B)
        if v.get("agree", 0) >= AGREE_MIN and v.get("ncc", 0) >= NCC_MIN:
            if v.get("cover", 1) < 0.6:
                big.wider = True
            return True
    return False


def _seam(im) -> float:
    """Side-by-side or stacked panels: a straight seam where neighbouring columns disagree."""
    import numpy as np
    g = np.asarray(trim_borders(im).convert("L").resize((240, 240)), dtype=np.float32)
    best = 0.0
    for arr in (g, g.T):
        strong = (np.abs(np.diff(arr, axis=1)) > 18).mean(axis=0)[24:-24]
        best = max(best, float(strong.max()) if strong.size else 0.0)
    return best


def _flat(im) -> bool:
    import numpy as np
    s = im.convert("RGB").resize((64, 64))
    g = np.asarray(s.convert("L"), dtype=np.float64)
    q = np.asarray(s, dtype=np.int32) // 32
    codes = q[..., 0] * 64 + q[..., 1] * 8 + q[..., 2]
    _, c = np.unique(codes, return_counts=True)
    return g.std() < 10 or len(c) < 16 or np.sort(c)[::-1][:3].sum() / codes.size > 0.85


def _logo_url(u: str) -> bool:
    toks = set(re.split(r"[^a-z0-9\-]+", urllib.parse.urlsplit(u or "").path.lower()))
    toks |= {t2 for t in list(toks) for t2 in t.split("-")}
    return bool(toks & LOGO_TOKENS)


ALIAS = {"new york post": "nypost", "the sun": "thesun", "us sun": "thesun", "the-sun": "thesun",
         "daily mail": "dailymail", "mail online": "dailymail", "the independent": "independent",
         "new york times": "nytimes", "the new york times": "nytimes", "people": "people", "people magazine": "people"}


def _norm_outlet(name: str) -> str:
    n = re.sub(r"\s+on msn$", "", (name or "").lower()).strip()
    n = ALIAS.get(n, n)
    return re.sub(r"^the\s+|[^a-z0-9]", "", n)


def _registrable(d: str) -> str:
    parts = d.split(".")
    if len(parts) < 2:
        return d
    return parts[-3] if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org") and len(parts[-1]) == 2 else parts[-2]


def _agg(d: str) -> bool:
    return any(d == x or d.endswith("." + x) for x in AGG)


def _outlet_id(a: Art) -> str:
    """The origin outlet ('' for an aggregator copy whose origin is unknown: it never counts)."""
    if _agg(a.domain):
        m = re.search(r"zenfs\.com/en/([a-z0-9_.\-]+)/", urllib.parse.unquote(a.img or "").lower())
        if m:                                        # Yahoo republishing another outlet: cleveland_com_articles_570
            toks = [t for t in re.split(r"[_.\-]", m.group(1)) if t]
            tld = next((i for i, t in enumerate(toks) if t in ("com", "net", "org", "co") and i), None)
            origin = toks[tld - 1] if tld else (toks[0] if toks and toks[0].isalpha() else "")
            if origin and origin not in ("yahoo", "aol", "msn"):
                return ALIAS.get(origin, origin)
        n = _norm_outlet(a.outlet)
        return "" if not n or n in {_registrable(x) for x in AGG} or n in ("msn", "yahoo", "aol") else n
    name = _norm_outlet(a.outlet)
    return ALIAS.get(name, _registrable(a.domain)) if name in ALIAS else _registrable(a.domain)


def _tj(a: str, b: str) -> float:
    x, y = set(tokens(a)), set(tokens(b))
    return len(x & y) / max(1, len(x | y))


def n_outlets(g: list[Art]) -> int:
    """Distinct origin outlets; near-identical titles (a wire story reprinted) count once."""
    reps: list[Art] = []
    for a in g:
        if not a.oid:
            continue
        if not any(a.oid == r.oid or _tj(a.title, r.title) >= 0.85 for r in reps):
            reps.append(a)
    return len(reps)


AGENCY_RE = re.compile(r"(gettyimages-\d+|getty-images|getty_images|\bap\d{8,}|usatsi|imagn|reuters|shutterstock|"
                       r"\bepa[-_]|\bafp[-_])", re.I)
PATH_DATE_RE = re.compile(r"/(20\d\d)/(0[1-9]|1[0-2])/")


def _load(a: Art) -> Art:
    from PIL import Image
    if not a.img and not a.thumb:
        a.img = og_image(a.url) or ""
    src = a.img or (a.thumb + "&w=480" if a.thumb else "")
    if not src:
        a.flags.append("no-image")
        return a
    r = _get(src, timeout=10)
    if (r is None or r.status_code != 200) and a.thumb and a.img:
        src = a.thumb + "&w=480"
        r = _get(src, timeout=10)
    if r is None or r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        a.flags.append("download failed")
        return a
    try:
        im = Image.open(io.BytesIO(r.content))
        im.load()
        im = im.convert("RGB")
    except Exception as e:
        if ops.fault(e, "other outlets' photo load"):  # Pillow does not load: the machine is broken, not the picture
            raise
        a.flags.append("unreadable")
        return a
    a.w, a.hh = im.size
    if a.thumb and src.startswith(a.thumb):
        a.w, a.hh = a.maxw or a.w, a.maxh or a.hh
    else:
        a.data = r.content
    a.h, a.f = hashes(im), _prep(im)
    if _flat(im):
        a.flags.append("flat")
    if _logo_url(a.img):
        a.flags.append("logo")
    if min(a.w, a.hh) < 300:
        a.flags.append("tiny")
    a.seam = _seam(im)
    if a.seam > 0.90:
        a.flags.append("composite")
    return a


VIOLENT_RE = re.compile(r"\b(mauled|maul\w*|killed|kills|attack\w*|shot dead|shooting|stabbed|stabbing|murder\w*|"
                        r"fatal\w*|crash\w*|drown\w*|massacre|beaten|bitten)\b", re.I)


def event_vocab(headline: str, titles_by_outlet: dict[str, str]) -> set:
    """What the story is about beyond its names: the headline's other words, plus words in the
    titles of 3+ outlets. An article about the same people but another subject (a film still for a
    wedding story) shares none of them."""
    names = set(tokens(" ".join(re.findall(r"\b[A-Z][a-zA-Z'’-]+", headline or ""))))
    head = set(tokens(re.sub(r'["“”][^"“”]{1,200}["“”]', " ", headline or "")))
    from collections import Counter
    c = Counter(t for title in titles_by_outlet.values() for t in set(tokens(title)))
    vocab = (head - names) | {t for t, n in c.items() if n >= 3} - names
    return vocab or head


def consensus_photos(headline: str, since: float = 0, exclude_domains=(), query: str = "",
                     max_articles: int = 24, max_groups: int = 3, deadline_s: float = 15, ocr: bool = False) -> list[Photo]:
    """Pictures at least two outlets used for this story, most outlets first ([] when there is none).
    Raises only when the machine is broken (a package or model that does not load): that is not
    'no picture', and the run must say so (Oct 21: 'Photo checks hit ImportError', reported ok)."""
    try:
        return _consensus(headline, since, exclude_domains, query, max_articles, max_groups, deadline_s, ocr)
    except Exception as e:
        if ops.fault(e, "other outlets' photos"):
            raise
        ops.degraded("other outlets' photos unavailable")
        print(f"  other outlets' photos unavailable ({type(e).__name__})")
        return []


def _consensus(headline, since, exclude_domains, query, max_articles, max_groups, deadline_s, ocr) -> list[Photo]:
    import cv2
    import numpy as np
    from PIL import Image

    from .stills import inspect_still, sharpness
    t_end = time.time() + deadline_s
    cv2.setRNGSeed(0)
    violent = bool(VIOLENT_RE.search(headline or ""))
    q1 = query or build_query(headline)
    q2 = " ".join(w for w in build_query(headline, max_terms=4, max_keys=0).split() if w[0].isupper())
    queries = [q for q in dict.fromkeys([q1, q2]) if q]
    if not queries:
        return []
    with ThreadPoolExecutor(4) as ex:
        found = list(ex.map(lambda qe: _engine(qe[1], bing_news if qe[1] == "bing" else ddg_news, qe[0]),
                            [(q, e) for q in queries for e in ("bing", "ddg")]))
    arts, by_url = [], {}
    for res in found:
        for a in res:
            nu = re.sub(r"^https?://(www\.)?", "", a.url.split("?")[0].rstrip("/"))
            if nu in by_url:
                o = by_url[nu]
                o.img = o.img or a.img
                if a.thumb and not o.thumb:
                    o.thumb, o.maxw, o.maxh = a.thumb, a.maxw, a.maxh
                continue
            by_url[nu] = a
            arts.append(a)
    arts.sort(key=lambda a: a.rank)
    excl = {d.lower().removeprefix("www.") for d in exclude_domains or ()}
    inwin = [a for a in arts if a.ts >= since - 72 * 3600 and a.domain not in excl and not any(sd in a.url for sd in STOCK_DOMAINS)]
    vocab = event_vocab(headline, {a.domain: a.title for a in inwin})
    cand = [a for a in inwin if set(tokens(a.title)) & vocab][:max_articles]
    ex = ThreadPoolExecutor(12)
    futs = [ex.submit(_load, a) for a in cand]
    wait(futs, timeout=max(0.5, min(5.0, t_end - time.time())))      # stragglers are dropped
    ex.shutdown(wait=False, cancel_futures=True)
    # a download stopped at the deadline is cancelled: asking it for its exception raises (CancelledError),
    # which threw away the whole story's photos and marked every topic run degraded (Oct 22-23)
    cand = [a for a, fu in zip(cand, futs) if fu.done() and not fu.cancelled() and not fu.exception()]
    for a in cand:
        a.oid = _outlet_id(a)
    ok = [a for a in cand if a.h and not {"flat", "tiny", "logo"} & set(a.flags)]
    plain = [a for a in ok if "composite" not in a.flags]           # collages never join two groups
    n = len(plain)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(n):
        for j in range(i + 1, n):
            if time.time() > t_end:
                break
            if find(i) != find(j) and same_picture(plain[i], plain[j]):
                parent[find(i)] = find(j)
    gmap: dict[int, list[Art]] = {}
    for i in range(n):
        gmap.setdefault(find(i), []).append(plain[i])
    groups = list(gmap.values())
    for c in (a for a in ok if "composite" in a.flags):               # attached to its best match only
        g = next((g for g in groups if same_picture(c, g[0])), None)
        if g:
            g.append(c)
    groups.sort(key=lambda g: (n_outlets(g), -min(a.rank for a in g)), reverse=True)
    out = []
    for g in groups[:max_groups]:
        outlets = n_outlets(g)
        if outlets < MIN_OUTLETS:
            break
        cen = {id(a): (float(np.mean([dist({"full": a.h["full"]}, {"full": b.h["full"]})[0] for b in g if b is not a]))
                       if len(g) > 1 else 0.0) for a in g}
        med = float(np.median(list(cen.values())))
        clean = [a for a in g if not ("composite" in a.flags or (a.wider and a.seam > 0.6) or cen[id(a)] > med + 6)] or g
        clean.sort(key=lambda a: (_agg(a.domain), -min(a.w, a.hh)))            # original outlets, then larger
        rep, rep_ph, soft = None, None, True
        for a in clean[:4]:
            img = a.img or og_image(a.url) or ""
            if not img:
                continue
            ph = Photo(img, "news", a.outlet or a.domain, post_url=a.url)
            from .media import fetch
            if not fetch(ph) or min(ph.w, ph.h) < 600:
                continue
            ph.sharp = sharpness(Image.open(io.BytesIO(ph.data)))
            if ocr and time.time() < t_end:
                inspect_still(ph)
                if any(f.startswith(("watermark", "text-heavy", "burned-in")) for f in ph.flags):
                    rep_ph = rep_ph or ph
                    continue
            if ph.sharp >= 60:
                rep, rep_ph, soft = a, ph, False
                break
            rep_ph = rep_ph or ph
        if rep_ph is None:
            continue
        ph = rep_ph
        names = sorted({a.outlet for a in g if a.outlet and a.oid})[:5]
        ph.via, ph.consensus = f"used by {outlets} outlets ({', '.join(names)})", outlets
        ph.phash = int(g[0].h["full"][0])
        ph.flags = [f for f in ph.flags if not f.startswith("not text-checked")]
        if soft:
            ph.flags = [f for f in ph.flags if not f.startswith("soft")] + ["soft (likely an upscaled video frame)"]
        if AGENCY_RE.search(urllib.parse.unquote(ph.url)):
            ph.flags.append("agency photo (Getty/AP/Reuters...)")
        m = PATH_DATE_RE.search(ph.url)
        if m and since and time.mktime((int(m.group(1)), int(m.group(2)), 1, 0, 0, 0, 0, 0, -1)) < since - 40 * 86400:
            ph.flags.append(f"file photo (uploaded {m.group(1)}-{m.group(2)})")
        if violent:
            ph.flags.append("violent incident: do not use a picture showing the victim being harmed")
        out.append(ph)
    return out


__all__ = ["consensus_photos", "picture_hash", "build_query", "bing_news", "ddg_news", "same_picture", "n_outlets", "hashes"]
