"""Real stills of the viral video a story is about, and the image other outlets used.

A celebrity page's articles often lead with a stock or file photo (a generic animal close-up for an
animal attack, an agency file photo for a proposal), while every other outlet used a still of
the actual video. Those videos are usually in the article itself: the X posts it embeds (and the
posts they quote or reply to). This module finds them for free (the public embed endpoint), groups
copies of the same frame posted by different accounts (consensus), samples sharp frames from the
video, checks each still (scoreboards, text-heavy cards, watermarks, soft or suspiciously sharp
images) and ranks them, best first. Editors still choose; each still says where it came from.

Page policy (`video_policy`): "prefer" (celebrity / viral pages: stills go ahead of the article's
lead image), "never" (sports pages: broadcast frames with scoreboards are not photos), "allow".
"""
from __future__ import annotations

import io
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests

from . import ops
from .media import UA, Photo, fetch, media_orig, story_photos
from .textutil import domain, parse_ts

VIDEO_STORY_RE = re.compile(r"\b(video|footage|clip|viral|caught on (camera|video|tape)|filmed|livestream(ed)?|"
                            r"live ?stream|on stream|bodycam|cctv|dashcam|ring camera|tiktok|watch:)", re.I)
# violent incidents only (a natural death is not one): every still gets a warning for editors
SENSITIVE_RE = re.compile(r"\b(mauled|maul\w*|killed|kills|attack\w*|shot dead|shooting|stabbed|stabbing|murder\w*|"
                          r"fatal\w*|crash\w*|drown\w*|massacre|beaten|bitten)\b", re.I)
VIOLENT_FLAG = "violent incident: do not use a frame showing the victim being harmed"
TWEET_RE = re.compile(r'data-tweet-id=["\'](\d{8,20})["\']|(?:twitter|x)\.com/[A-Za-z0-9_]{1,15}/status(?:es)?/(\d{8,20})')
IG_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]{5,})")
YT_RE = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")
LD_RE = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
SCORE_RE = re.compile(r"^(?:(?:1st|2nd|3rd|4th)&(?:\d{1,2}|goal)|1st|2nd|3rd|4th|OT|:\d{2}|\d{1,2}:\d{2})$", re.I)
# a clip tool's or an account's mark; not a network name (those are on backdrops behind good photos)
WATERMARK_RE = re.compile(r"(kick\.com|\btiktok\b|clipped by|capcut|@[\w.]{3,}|\b\w+\.(com|tv|net)\b)", re.I)
COUNTER_RE = re.compile(r"\d{3,}/\d{3,}")
FINAL_RE = re.compile(r"final(\s*/?\s*ot)?|halftime|end of (1st|2nd|3rd|4th)( quarter)?", re.I)
DROP = ("scoreboard", "wide broadcast shot", "text-heavy")
SPORTS = {"nfl", "nba", "mlb", "nhl", "soccer", "football", "sports"}
STOP = set("the a an and or of to in on at for with from by is are was were be been as it its this that his her their "
           "after before over into new says said video viral watch internet reacts react shared online".split())


def video_policy(page) -> str:
    """prefer | allow | never for a page (Page.video_stills, else from its leagues)."""
    explicit = getattr(page, "video_stills", "") or ""
    if explicit in ("prefer", "allow", "never"):
        return explicit
    leagues = {str(x).lower() for x in getattr(page, "leagues", None) or []}
    if leagues & SPORTS:
        return "never"
    if leagues & {"entertainment", "celebrity", "viral", "true crime", "pop culture"}:
        return "prefer"
    return "allow"


# ------------------------------------------------------------------ embedded posts (free)
def snowflake_ts(tid) -> float:
    return ((int(tid) >> 22) + 1288834974657) / 1000


def _js_token(tid: str) -> str:
    """The token X's embed script sends with a post id."""
    x = int(tid) / 1e15 * math.pi
    digits, ip, fp, s, frac = "0123456789abcdefghijklmnopqrstuvwxyz", int(x), x - int(x), "", ""
    while ip:
        s, ip = digits[ip % 36] + s, ip // 36
    for _ in range(12):
        fp *= 36
        d = int(fp)
        frac += digits[d]
        fp -= d
    return re.sub(r"(0+|\.)", "", (s or "0") + "." + frac)


_POSTS: dict[str, tuple[float, dict | None]] = {}


def x_post(tid: str, timeout: int = 8) -> dict | None:
    """A public X post via the free endpoint embedded posts load from (cached for 6 hours)."""
    hit = _POSTS.get(tid)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    try:
        r = requests.get("https://cdn.syndication.twimg.com/tweet-result", headers=UA, timeout=timeout,
                         params={"id": tid, "lang": "en", "token": _js_token(tid)})
        j = r.json() if r.status_code == 200 and r.content else None
    except (requests.RequestException, ValueError):
        j = None
    _POSTS[tid] = (time.time(), j)
    return j


def _post_photos(j: dict, via: str) -> list[Photo]:
    out = []
    for post, rel in ((j, ""), (j.get("quoted_tweet") or {}, "quoted "), (j.get("parent") or {}, "replied-to ")):
        user = (post.get("user") or {}).get("screen_name") or ""
        for md in post.get("mediaDetails") or []:
            if not md.get("media_url_https"):
                continue
            oi = md.get("original_info") or {}
            out.append(Photo(media_orig(md["media_url_https"]), "x", "@" + user, oi.get("width") or 0, oi.get("height") or 0,
                             note="video frame" if md.get("type") in ("video", "animated_gif") else "",
                             via=f"{rel}post {via}", post_url=f"https://x.com/{user}/status/{post.get('id_str')}",
                             created_at=snowflake_ts(post.get("id_str") or 0)))
    return out


def page_embeds(page_url: str, text: str | None = None) -> dict:
    """Posts, videos and players embedded in an article."""
    if text is None:
        try:
            r = requests.get(page_url, headers=UA, timeout=12)
            text = r.text if r.status_code == 200 else ""
        except requests.RequestException:
            text = ""
    text = (text or "")[:1_500_000]
    vids = []
    for blob in LD_RE.findall(text):
        try:
            stack = [json.loads(blob.strip())]
        except ValueError:
            continue
        while stack:
            o = stack.pop()
            if isinstance(o, list):
                stack += o
            elif isinstance(o, dict):
                if "VideoObject" in str(o.get("@type")):
                    th = o.get("thumbnailUrl") or o.get("thumbnail")
                    th = th[0] if isinstance(th, list) and th else th
                    th = th.get("url") if isinstance(th, dict) else th
                    if isinstance(th, str) and th:
                        vids.append({"thumb": urljoin(page_url, th), "name": o.get("name") or "", "upload": o.get("uploadDate") or ""})
                stack += list(o.values())
    return {"tweets": list(dict.fromkeys(a or b for a, b in TWEET_RE.findall(text)))[:10],
            "instagram": list(dict.fromkeys(IG_RE.findall(text)))[:4],
            "youtube": list(dict.fromkeys(YT_RE.findall(text)))[:3], "videos": vids[:4]}


def embedded_photos(page_url: str, text: str | None = None, terms=(), since: float = 0) -> list[Photo]:
    e, via = page_embeds(page_url, text), f"embedded in {domain(page_url)}"
    out = []
    with ThreadPoolExecutor(8) as ex:
        for j in ex.map(x_post, e["tweets"]):
            if j:
                out += _post_photos(j, via)
    for code in e["instagram"]:
        out.append(Photo(f"https://www.instagram.com/p/{code}/media/?size=l", "instagram", "instagram.com/p/" + code,
                         via=f"post {via}", post_url=f"https://www.instagram.com/p/{code}/"))
    for vid in e["youtube"]:      # YouTube's own frames at 25/50/75% (never the designed thumbnail)
        out += [Photo(f"https://i.ytimg.com/vi/{vid}/{k}.jpg", "youtube", "youtu.be/" + vid, note="video frame",
                      via=f"video {via}", post_url=f"https://youtu.be/{vid}") for k in ("maxres1", "maxres2", "maxres3")]
    for v in e["videos"]:         # outlets' players: often unrelated or a title card
        recent = not since or (parse_ts(v["upload"]) or 0) >= since - 72 * 3600
        if recent and sum(t.lower() in v["name"].lower() for t in terms) >= 2:
            out.append(Photo(v["thumb"], "web", domain(page_url), note="video frame", via=f"video {via}", post_url=page_url))
    return out


def story_terms(members: list[dict], n: int = 4) -> list[str]:
    """Search terms for a story: its categories, then the capitalised words of its titles."""
    terms = []
    for m in members:
        terms += [c for c in ((m.get("metrics") or {}).get("categories") or []) if isinstance(c, str)]
    for m in members:
        terms += [w for w in re.findall(r"\b[A-Z][a-zA-Z'’-]{2,}\b", m.get("title") or "") if w.lower() not in STOP]
    return list(dict.fromkeys(terms))[:n]


def is_video_story(members: list[dict], embedded: list[Photo] = ()) -> bool:
    if any(VIDEO_STORY_RE.search(f"{m.get('title') or ''} {m.get('text') or ''}") for m in members):
        return True
    if any(p.note == "video frame" and not p.via.startswith("replied-to") and "embedded" in p.via for p in embedded):
        return True
    top = max((m for m in members if m.get("platform") == "x"), key=lambda m: int(m.get("engagement") or 0), default=None)
    return bool(top and any(md.get("type") in ("video", "animated_gif") for md in (top.get("metrics") or {}).get("media") or []))


def x_search_videos(apify, cfg: dict, terms: list[str], since: float, max_items: int = 20) -> list[Photo]:
    """One budgeted X search for the clip (only when the article's own embeds found no copies)."""
    from .collect import normalise_x
    xc = cfg["apify"]["x"]
    run_input = {**xc["input"], "queryType": "Top", xc["search_terms_key"]: [" ".join(terms) + " filter:videos min_faves:100"],
                 xc["since_key"]: str(int(time.time() - 72 * 3600)), xc["max_items_key"]: max_items}
    raw, _ = apify.run(xc["actor"], run_input, 180)
    out = []
    for it in filter(None, (normalise_x(r, cfg["fields"]["x"], time.time()) for r in raw)):
        if (it.get("created_at") or 0) < since - 36 * 3600:          # older clips of the same people
            continue
        for md in (it.get("metrics") or {}).get("media") or []:
            out.append(Photo(media_orig(md["url"]), "x", "@" + (it.get("author") or ""), md.get("w") or 0, md.get("h") or 0,
                             note="video frame" if md.get("type") not in (None, "photo") else "", via="X search",
                             post_url=it.get("url") or "", created_at=it.get("created_at") or 0))
    return out


# ------------------------------------------------------------------ frames and checks
def phash(im) -> int:
    import cv2
    import numpy as np
    from PIL import Image
    g = np.asarray(im.convert("L").resize((32, 32), Image.LANCZOS), dtype=np.float32)
    d = cv2.dct(g)[:8, :8].flatten()
    return int("".join("1" if b else "0" for b in d > np.median(d[1:])), 2)


def ham(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def sharpness(im) -> float:
    """Laplacian variance of the central 60% at 720 px short side."""
    import cv2
    import numpy as np
    from PIL import Image
    g = im.convert("L")
    s = 720 / max(min(g.size), 1)
    a = cv2.Laplacian(np.asarray(g.resize((max(int(g.size[0] * s), 1), max(int(g.size[1] * s), 1)), Image.BILINEAR),
                                 np.float32), cv2.CV_32F)
    h, w = a.shape
    return float(a[int(h * .2):int(h * .8), int(w * .2):int(w * .8)].var())


def x_video_mp4(post_url: str) -> str | None:
    m = re.search(r"/status/(\d+)", post_url or "")
    j = x_post(m.group(1)) if m else None
    for post in (j or {}, (j or {}).get("quoted_tweet") or {}, (j or {}).get("parent") or {}):
        for md in post.get("mediaDetails") or []:
            mp4 = [v for v in (md.get("video_info") or {}).get("variants") or [] if v.get("content_type") == "video/mp4"]
            if mp4:
                return max(mp4, key=lambda v: v.get("bitrate") or 0)["url"]
    return None


def sample_frames(mp4_url: str, n: int = 8, max_seconds: float = 180) -> list:
    """(second, BGR frame) at n points of the video; seeks over HTTP, no full download."""
    import cv2
    cap = cv2.VideoCapture(mp4_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = min(int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0), int(max_seconds * fps))
    out = []
    for k in range(n if total > 0 else 0):
        pos = int(total * (k + .5) / n)
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ok, fr = cap.read()
        if ok:
            out.append((round(pos / fps, 1), fr))
    cap.release()
    return out


def match_score(ref_bgr, frame_bgr) -> float:
    """Is this (smaller) still a crop of this frame? >= 0.7: the same moment."""
    import cv2
    import numpy as np
    g = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (max(g.shape[1] // 2, 1), max(g.shape[0] // 2, 1)))
    t0 = cv2.cvtColor(ref_bgr, cv2.COLOR_BGR2GRAY)
    best = -1.0
    for s in np.linspace(0.3, 1.0, 12):
        t = cv2.resize(t0, (max(8, int(t0.shape[1] * s)), max(8, int(t0.shape[0] * s))))
        if t.shape[0] < g.shape[0] and t.shape[1] < g.shape[1]:
            best = max(best, float(cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED).max()))
    return best


def frames_as_photos(post: Photo, refs: list, n: int = 8) -> list[Photo]:
    import cv2
    from PIL import Image
    mp4 = x_video_mp4(post.post_url)
    out, seen = [], []
    for sec, fr in (sample_frames(mp4, n) if mp4 else []):
        im = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
        h = phash(im)
        if any(ham(h, x) <= 6 for x in seen):          # looped clips repeat
            continue
        seen.append(h)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        out.append(Photo(f"{mp4}#t={sec}", post.source, post.credit, im.size[0], im.size[1], buf.getvalue(),
                         note="video frame", via=f"frame at {sec}s of the video in {post.credit}'s post",
                         post_url=post.post_url, consensus=post.consensus, phash=h,
                         ref_match=max([match_score(r, fr) for r in refs] or [0])))
    return out


def _dedupe_flags(p: Photo) -> Photo:
    """One flag of each kind (checks can run more than once on the same photo)."""
    seen, out = set(), []
    for f in p.flags:
        k = re.split(r"[ :(]", f, 1)[0]
        if k not in seen:
            seen.add(k)
            out.append(f)
    p.flags = out
    return p


def inspect_still(p: Photo, sports: bool = False, ocr: bool = True) -> Photo:
    """Flags for a still: soft, wide broadcast shot, scoreboard, watermark, text-heavy, burned-in text."""
    return _dedupe_flags(_inspect(p, sports, ocr))


def _inspect(p: Photo, sports: bool, ocr: bool) -> Photo:
    import tempfile

    import cv2
    import numpy as np
    from PIL import Image

    from .guard import _ocr, detect_faces
    im = Image.open(io.BytesIO(p.data)).convert("RGB")
    p.sharp, p.phash = sharpness(im), p.phash or phash(im)
    small = im.copy()
    small.thumbnail((960, 960))
    W, H = small.size
    bgr = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2BGR)
    try:
        p.face = max([w * h / (W * H) for (_, _, w, h, _) in detect_faces(bgr)] or [0])
    except Exception as e:
        if ops.fault(e, "still face check"):  # no face model on this machine: not a faceless photo
            raise
        ops.degraded("still face check failed")
        p.face = 0.0
    if p.sharp < 60:
        p.flags.append(f"soft (sharpness {p.sharp:.0f})")
    if sports:
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        green = ((hsv[..., 0] > 35) & (hsv[..., 0] < 85) & (hsv[..., 1] > 60) & (hsv[..., 2] > 50)).mean()
        if green >= 0.6 and p.face < 0.005:
            p.flags.append("wide broadcast shot")
    if not ocr:
        p.flags.append("not text-checked")
        return p
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            small.save(f.name, quality=90)
            lines = [(t, b) for t, c, b in _ocr(f.name) if c >= 0.6]
    except Exception as e:
        if ops.fault(e, "still text check"):  # OCR does not load: every still would pass unread
            raise
        ops.degraded("still text check failed")
        p.flags.append("not text-checked")
        return p
    p.ocr = [t for t, _ in lines]
    score = [t for t in p.ocr if SCORE_RE.match(t.replace(" ", ""))]
    nums = [t for t, b in lines if re.fullmatch(r"\d{1,2}", t.strip()) and b[1] > .65 * H]
    final = any(FINAL_RE.fullmatch(t.strip()) for t in p.ocr)          # a final-score card: FINAL 29 32
    if len(score) >= 2 or (score and len(nums) >= 2) or \
            (final and sum(bool(re.fullmatch(r"\d{1,2}", t.strip())) for t in p.ocr) >= 2):
        p.flags.append("scoreboard")
    if wm := [t for t in p.ocr if WATERMARK_RE.search(t)]:
        p.flags.append("watermark: " + ", ".join(wm)[:60])
    once = once_words(p.ocr)
    told = [(t, b) for t, b in lines if set(_words(t)) & once]        # lines saying something only once
    area = sum((b[2] - b[0]) * (b[3] - b[1]) for _, b in told) / (W * H)
    letters = sum(map(len, once))
    if area > 0.10 or (letters >= 25 and area >= 0.04) or len([t for t, _ in told if len(t) >= 15]) >= 4:
        p.flags.append(f"text-heavy ({area:.0%} text)")
    elif (area > 0.03 and letters >= 8) or any(COUNTER_RE.search(t) for t in p.ocr):
        p.flags.append("burned-in text: " + " / ".join(t for t, _ in told)[:80])
    return p


def _words(t: str) -> list[str]:
    return re.findall(r"[a-z]{2,}", t.lower())


def once_words(texts: list[str]) -> set[str]:
    """Words read once in an image. A step-and-repeat backdrop repeats its words, a card or a
    screenshot does not; jersey numbers and other digits don't count. A word inside a longer one
    read elsewhere ('emmys' in 'emmysawards') is the same word."""
    from collections import Counter
    n = Counter(w for t in texts for w in _words(t))
    for w in list(n):
        for v in n:
            if len(w) >= 4 and w != v and w in v:
                n[v] += n[w]
                n[w] = 0
                break
    return {w for w, c in n.items() if c == 1}


def _clusters(frames: list[Photo]) -> list[list[Photo]]:
    """Copies of the same frame (pHash distance <= 10), most accounts first."""
    groups: list[list[Photo]] = []
    for p in frames:
        g = next((g for g in groups if ham(g[0].phash, p.phash) <= 10), None)
        if g:
            g.append(p)
        else:
            groups.append([p])
    for g in groups:
        c = len({x.credit.lower() for x in g})       # distinct accounts (being embedded is ranked apart)
        for x in g:
            x.consensus = c
    groups.sort(key=lambda g: (-g[0].consensus, not any("embedded" in x.via for x in g), -max(x.w * x.h for x in g)))
    return groups


def _with_hash(photos: list[Photo]) -> list[Photo]:
    from PIL import Image
    out = []
    with ThreadPoolExecutor(8) as ex:
        for p in ex.map(fetch, photos):
            if p and p.data:
                try:
                    p.phash = phash(Image.open(io.BytesIO(p.data)))
                    out.append(p)
                except Exception as e:            # not an image (a page, a broken file): left out
                    if ops.fault(e, "still fingerprint"):
                        raise
    return out


def video_stills(members: list[dict], policy: str, apify=None, cfg: dict | None = None, budget_s: float = 25,
                 frames: bool = True, ocr: bool = True, min_short: int = 720, n_frames: int = 8,
                 refs_extra: list | None = None) -> list[Photo]:
    """Real stills of the viral video a story is about, best first ([] when it isn't one).
    frames=False, ocr=False: posters only (fast enough for the hourly Studio feed).
    refs_extra: the pictures other outlets used (bore/consensus.py Photos with data): a sampled
    frame that matches one is that moment in HD and carries its outlet count."""
    if policy == "never":
        return []
    t0 = time.time()
    terms = story_terms(members)
    since = min((m.get("created_at") or time.time()) for m in members) if members else time.time()
    cands: list[Photo] = []
    for m in [m for m in members if m.get("platform") == "web" and m.get("url")][:2]:
        cands += embedded_photos(m["url"], terms=terms, since=since)
    cands += [p for p in story_photos(members, max_web=0) if p.note == "video frame"]
    if not is_video_story(members, cands):
        return []
    cands = [p for p in cands if p.note == "video frame" or p.source != "web"]
    got = _with_hash(cands)
    groups = _clusters([p for p in got if p.note == "video frame"])
    vs = ((cfg or {}).get("posts") or {}).get("video_stills") or {}
    # search X for the clip unless two accounts already posted the same frame
    if apify and cfg and vs.get("x_search") and not any(g[0].consensus >= 2 for g in groups) and terms:
        try:
            got += _with_hash(x_search_videos(apify, cfg, terms[:3], since))
            groups = _clusters([p for p in got if p.note == "video frame"])
        except Exception as e:                       # incl. the budget guard: never fail a brief over stills
            if ops.fault(e, "video search"):
                raise
            if type(e).__name__ != "BudgetExceeded":    # the budget saying no is not a fault
                ops.degraded("video search failed")
            print(f"  video search skipped ({type(e).__name__})")
    import cv2
    import numpy as np
    from PIL import Image
    pool = [max(g, key=lambda x: x.w * x.h) for g in groups[:4]]
    pool += [p for p in got if p.note != "video frame"]          # photos in the posts (fallback)
    outside = [p for p in (refs_extra or []) if p.data]
    if frames and groups and time.time() - t0 < budget_s - 8:
        refs = [cv2.imdecode(np.frombuffer(p.data, np.uint8), cv2.IMREAD_COLOR)
                for p in [*outside, *(x for x in got if x.w * x.h < 700_000)]]
        refs = [r for r in refs if r is not None]
        tops = [groups[0]] + [g for g in groups[1:2] if g[0].consensus >= 2]
        with ThreadPoolExecutor(2) as ex:
            for fr in ex.map(lambda g: frames_as_photos(max(g, key=lambda x: x.w * x.h), refs, n_frames), tops):
                pool += fr
    if outside:          # the moment other outlets used, in HD: carries their outlet count
        best_ref = max(outside, key=lambda x: x.consensus)
        for p in pool:
            if p.ref_match >= 0.7:
                p.consensus = max(p.consensus, best_ref.consensus)
                p.via = f"{p.via}: the moment {best_ref.consensus} outlets used"
    pool = [p for p in pool if p.data and p.short_side >= min_short]
    for p in pool:
        try:
            p.sharp = sharpness(Image.open(io.BytesIO(p.data)))
        except Exception as e:
            if ops.fault(e, "still sharpness"):
                raise
            p.sharp = 0.0
    pool.sort(key=lambda p: (p.ref_match >= 0.7, p.consensus, p.note == "video frame", min(p.sharp, 150)), reverse=True)
    pool = pool[:6]
    for p in pool:
        try:
            inspect_still(p, ocr=ocr and time.time() - t0 < budget_s)
        except Exception as e:
            if ops.fault(e, "still checks"):
                raise
            ops.degraded("still checks failed")
            p.flags.append("not checked")
    frame_sharp = sorted(p.sharp for p in pool if p.note == "video frame")
    med = frame_sharp[len(frame_sharp) // 2] if frame_sharp else 0
    sensitive = any(SENSITIVE_RE.search(f"{m.get('title') or ''} {m.get('text') or ''}") for m in members)
    for p in pool:
        if p.note != "video frame" and med >= 20 and p.sharp > 8 * med:
            p.flags.append("much sharper than the footage: possibly enhanced or AI")
        if p.consensus < 2 and "embedded" not in p.via and not p.via.startswith("frame"):
            p.flags.append("only one account posted this")
        if sensitive:
            p.flags.append(VIOLENT_FLAG)
    pool = [p for p in pool if not any(f.startswith(DROP) for f in p.flags) and not (p.sharp < 15 and p.consensus < 2)]
    pool.sort(key=lambda p: (p.ref_match >= 0.7, p.consensus, p.face >= 0.01,
                             not any(f.startswith(("watermark", "much sharper", "only one")) for f in p.flags),
                             not any(f.startswith("soft") for f in p.flags), min(p.sharp, 150)), reverse=True)
    return pool[:3]


def broadcast_frame(p: Photo) -> bool:
    """A sports broadcast screenshot (scoreboard or wide field shot), even when posted as a photo."""
    try:
        if not p.data:
            fetch(p)
        f = inspect_still(p, sports=True).flags if p.data else []
    except Exception as e:
        if ops.fault(e, "broadcast frame check"):  # a broken machine must not pass every frame as a photo
            raise
        return False
    return any(x.startswith(("scoreboard", "wide broadcast")) for x in f)


__all__ = ["video_policy", "video_stills", "embedded_photos", "page_embeds", "inspect_still", "broadcast_frame",
           "is_video_story", "story_terms", "phash", "ham", "sharpness", "x_post"]
