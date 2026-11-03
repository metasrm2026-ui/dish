"""Photos for post cards: the story's own images first, then a web search for the subject.

Every candidate is downloaded and measured: anything below the resolution floor is rejected, so
nothing soft reaches a card ("it must be as clear as what already exists")."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests

from .textutil import domain

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
META_IMG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*>', re.I)
CONTENT_RE = re.compile(r'content=["\']([^"\']+)["\']', re.I)
MAX_SIDE = 2400          # larger photos are scaled down (a card is 1080x1350)


@dataclass
class Photo:
    url: str
    source: str          # x | reddit | web | search
    credit: str          # handle or domain
    w: int = 0
    h: int = 0
    data: bytes | None = None
    note: str = ""       # e.g. "video frame"
    via: str = ""        # where it came from, for editors ("post embedded in example.com", "frame at 18s of ...")
    post_url: str = ""
    flags: list = field(default_factory=list)   # checks editors should see (watermark, soft, fatal incident ...)
    consensus: int = 1   # accounts / outlets that used this same image
    ref_match: float = 0.0
    sharp: float = 0.0
    face: float = 0.0
    phash: int = 0
    ocr: list = field(default_factory=list)
    created_at: float = 0.0
    id_match: str = ""   # its faces vs the subject's (bore/faceid.py): match | mismatch ('may not be') | '' neutral
    id_score: float = 0.0
    id_name: str = ""    # who it was compared with (set when a face was compared)
    id_ref: str = ""     # the subject's reference face: espn | wikipedia

    @property
    def short_side(self) -> int:
        return min(self.w, self.h)

    def describe(self) -> str:
        return f"{self.source} photo from {self.credit}, {self.w}x{self.h}{' (' + self.note + ')' if self.note else ''}"


def x_orig(url: str) -> str:
    """Largest variant of a pbs.twimg.com image."""
    if "pbs.twimg.com/media/" in url:
        base = url.split("?")[0]
        m = re.match(r"(.*)\.(jpg|jpeg|png|webp)$", base)
        return f"{m.group(1)}?format={m.group(2)}&name=orig" if m else base + "?name=orig"
    return url


VTHUMB_RE = re.compile(r"(https://pbs\.twimg\.com/(?:amplify_video_thumb|ext_tw_video_thumb|tweet_video_thumb)/[^?]+?)"
                       r"(?:\.(?:jpe?g|png))?(?:\?.*)?$")


def media_orig(url: str) -> str:
    """Largest variant of any pbs.twimg.com image (video thumbnails default to 1200px without it)."""
    m = VTHUMB_RE.match(url or "")
    return f"{m.group(1)}?format=jpg&name=orig" if m else x_orig(url)


def og_image(page_url: str, timeout: int = 12) -> str | None:
    try:
        r = requests.get(page_url, headers=UA, timeout=timeout)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return None
        for tag in META_IMG_RE.findall(r.text[:300_000]):
            m = CONTENT_RE.search(tag)
            if m:
                return urljoin(page_url, m.group(1).replace("&amp;", "&"))
    except requests.RequestException:
        return None
    return None


def flatten(im):
    """RGB for the renderer: a transparent image (a cutout headshot) on neutral grey, not black."""
    from PIL import Image
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (128, 128, 128))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def fetch(photo: Photo, timeout: int = 20, max_bytes: int = 15_000_000) -> Photo | None:
    from PIL import Image
    try:
        r = requests.get(photo.url, headers=UA, timeout=timeout)
        if r.status_code != 200 or len(r.content) > max_bytes:
            return None
        im = Image.open(io.BytesIO(r.content))
        data = r.content
        if im.format not in ("JPEG", "PNG") or max(im.size) > MAX_SIDE or im.mode not in ("RGB", "L"):
            # WebP (news sites' feeds), cutouts and camera-size originals become a JPEG the renderer takes
            im = flatten(im)
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=92)
            data = buf.getvalue()
        photo.w, photo.h = im.size
        photo.data = data
        return photo
    except Exception:
        return None


SKIP_LINK_HOSTS = ("x.com", "twitter.com", "youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com",
                   "threads.net", "bsky.app")
STOCK_DOMAINS = ("alamy.", "shutterstock.", "gettyimages.", "istockphoto.", "dreamstime.", "depositphotos.", "123rf.",
                 "agefotostock.", "pinterest.", "pinimg.")
NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[‘'\"“]?[A-Z][a-zA-Z'’.-]+[’'\"”]?){1,2})")   # list_post's count

# Who a story is about (subject_of). A name is a run of 2-4 capitalised words inside one sentence,
# never a role, title, team, team city, position, month, pronoun or function word, nor an ALL-CAPS
# word: "Packers RB Josh Jacobs" is Josh Jacobs, "On March 25", "He COULD" and "Green Bay" name no
# one. Whether a name is a person at all is the face lookup's call (bore/faceid.py: an ESPN headshot
# or a Wikipedia portrait): only then is it searched by name (bore/studio.py) or compared with faces.
# The live feed of Oct 21 gave 'On March', 'He COULD', 'Packers RB Josh', 'Riley Patterson.'.
NAME_WORD_RE = re.compile(r"[^\W\d_][^\W\d_]*(?:['’\-][^\W\d_]+)*\.?|(?:[A-Z]\.){1,3}|[A-Z]J")
NAME_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}
NON_NAME = frozenset("""former ex late old veteran rookie free agent per via breaking report reports source
sources watch meet inside happy dear congrats congratulations thank thanks rip legend star superstar coach owner
president chairman commissioner officer deputy sheriff detective lawyer attorney judge editor reporter anchor host
doe rankings ranking draft mock bowl bowlers pro meeting league insiders standings tracker podcast ever spring summer
fall autumn winter trade trades rumor rumors although though despite given however meanwhile instead besides unlike
amid following plus general manager head offensive defensive coordinator assistant personal conduct policy""".split())
OBJECT_OF = frozenset("""with of to from by about against for at in into over under vs versus like between behind
toward towards alongside featuring feat ft per via""".split())
PLACE_OF = frozenset("at in across near".split())     # "in North America": a place, never the subject
# How a post mentions someone (subject_of prefers whom the news is about): as a coach or executive
# ("Texans HC DeMeco Ryans announced that WR Tank Dell will play"), as the speaker of the quote it
# carries ("Ben Johnson: Drake Maye is...", "Ryan Fitzpatrick on Broncos QB Bo Nix: ..."), or as an
# object ("emails with Jeffrey Epstein", "per Alex Rivera"). Not by position, nor every "X says":
# on 558 real drafts (Sep 29-Oct 21) preferring "WR Tank Dell" over a plainly named person picked
# the wrong one more often (an injury list names everyone by position), and so did taking "X says"
# for a speaker (a celebrity page's news is often what a celebrity says).
STAFF_BEFORE = frozenset("hc coach gm oc dc coordinator manager owner president ceo chairman commissioner".split())
ROLE_RANK = {"": 3, "staff": 2, "speaker": 1, "object": 0, "place": 0}


def _team_words() -> frozenset[str]:
    from .teams import NFL_TEAMS
    return frozenset(NFL_TEAMS)


def _city_names() -> frozenset[str]:
    from .teams import NFL_CITIES
    return frozenset(c.lower() for c in NFL_CITIES)


def _name_word(word: str, run: list, sentence_start: bool) -> bool:
    from .textutil import NOT_ENTITIES, STOPWORDS
    low = word.lower().replace("’", "'").rstrip(".")
    if low in NAME_SUFFIX:                          # Jr., III: only after a name
        return bool(run)
    if not word[0].isupper() or not NAME_WORD_RE.fullmatch(word):
        return False
    if word.replace(".", "").isupper():             # ALL-CAPS: only initials (A.J., CJ)
        return bool(re.fullmatch(r"(?:[A-Z]\.){1,3}|[A-Z]J", word))
    if low in STOPWORDS or low in NOT_ENTITIES or low in NON_NAME or low in _team_words() or low.startswith("ex-"):
        return low == "will" and not sentence_start   # "...and Will Campbell" (but "Will Lamar play?")
    return True


def names_in(text: str) -> list[tuple[str, str]]:
    """The names a text mentions, in order, with how it mentions them (ROLE_RANK): "" (plainly),
    "staff" (a coach or executive), "speaker" (of a quote), "object" ("emails with Jeffrey Epstein",
    "per Alex Rivera"; not "every snap of Justin Herbert's career") or "place" ("in North
    America"). A quoted nickname inside a name stays in it (Sean ‘Diddy’ Combs), not a quoted word
    after one ('Nick Sirianni "hopeful" WR...'). Not names: a team's city ("Green Bay Packers", "Los
    Angeles sits at 0-5") and a title after 'the' ("The Daily Show")."""
    from .textutil import URL_RE
    out = []
    for line in re.split(r"[\r\n]+", URL_RE.sub(" ", text or "")):
        run, start, prev, at, last = [], True, "", 0, 0
        found = []                    # [name, role, first token, last token, ends with a comma, a colon]
        toks = line.split()

        def flush(comma=False, colon=False, possessive=False, team=False):
            words = [w for w in run if w.lower().rstrip(".") not in NAME_SUFFIX and w[0].isalpha()]
            name = " ".join(run if len(words) <= 4 else run[:2])           # longer: Title-Case prose
            if len(words) >= 2 and not team and before.lower() != "the" and name.lower() not in _city_names():
                b, after = before.lower(), [re.sub(r"\W", "", t).lower() for t in toks[last + 1:last + 2]]
                # "Mike Macdonald on Sam Darnold: He's..." (and "Dan Campbell: I expect Kerby Joseph...", below)
                speaks = after == ["on"] and ":" in " ".join(toks[last + 2:])
                role = ("speaker" if speaks else "staff" if b in STAFF_BEFORE
                        else ("place" if b in PLACE_OF else "object") if b in OBJECT_OF and not possessive else "")
                found.append([name, role, at, last, comma, colon])
            run.clear()
        for i, tok in enumerate(toks):
            core = tok.lstrip("\"“‘'([{*_")
            quoted = core != tok
            if quoted and run and core.rstrip(",.;:!?").endswith(("’", "'", "”", "\"")) and core[:1].isupper():
                nxt = toks[i + 1].strip("\"“”‘’'(),.;:!?") if i + 1 < len(toks) else ""
                if nxt and _name_word(nxt, run, False):
                    run.append(tok)                 # Sean ‘Diddy’ Combs (faceid looks up Sean Combs),
                    last = i
                    continue                        # not 'Nick Sirianni "hopeful" WR ...'
            if quoted or core.startswith(("#", "@")):
                flush()
            word = core.rstrip(",;:!?)]}\"”…*")
            ends = word != core
            possessive = re.search(r"['’]s$|(?<=s)['’]$", word)
            if possessive:
                word, ends = word[: possessive.start()], True
            if word.endswith(".") and not (word[:-1].lower() in NAME_SUFFIX | {"st"} or re.fullmatch(r"(?:[A-Z]\.)*[A-Z]\.", word)):
                word, ends = word[:-1], True        # 'Riley Patterson.': the sentence ends
            if word and not core.startswith(("#", "@")) and _name_word(word, run, start):
                if not run:
                    before, at = prev, i
                run.append(word)
                last = i
            else:
                flush(team=word.lower() in _team_words())     # "Green Bay Packers": a city
            if ends:
                flush(core.endswith(","), core.rstrip("\"”'’)").endswith(":"), bool(possessive))
            start = ends and bool(re.search(r"[.!?:…]$", core))
            prev = re.sub(r"\W", "", tok)
        flush()
        # a list ("with A, B and C", three names or two joined by 'and'): every name is an object too
        k = 0
        while k < len(found):
            j, joined = k, False
            while j + 1 < len(found):
                gap = toks[found[j][3] + 1: found[j + 1][2]]
                if not (gap == [] and found[j][4] or [g.lower() for g in gap] in (["and"], ["or"], ["&"])):
                    break
                joined = joined or bool(gap)
                j += 1
            if (j - k >= 2 or (j > k and joined)) and found[k][1] in ("object", "place"):
                for f in found[k + 1: j + 1]:
                    f[1] = found[k][1]
            k = j + 1
        for f in found[:-1]:          # "Ben Johnson: Drake Maye is..." (not "Dak Prescott: we didn't...")
            if f[5] and f[1] in ("", "staff"):
                f[1] = "speaker"
        out += [(f[0], f[1]) for f in found]
    return out


def resolve_tco(u: str, timeout: int = 6) -> str:
    """Where a t.co link points (one request, no download)."""
    try:
        r = requests.get(u, allow_redirects=False, timeout=timeout, headers={"User-Agent": "curl/8"})
        return r.headers.get("location", "") if r.status_code in (301, 302, 303, 307, 308) else ""
    except requests.RequestException:
        return ""


def x_links(m: dict) -> list[str]:
    """The article links in an X post (stored by the collector; older items: guessed from the text)."""
    met = m.get("metrics") or {}
    if "links" in met:
        return list(met["links"])
    links = re.findall(r"https://t\.co/\w+", m.get("text") or "")
    return links[:-1] if met.get("media") else links       # the last t.co of a post with media is the media


def subject_of(members: list[dict]) -> str:
    """Who a story is about: a name in a headline that is mentioned again elsewhere, else one of the
    names its posts mention most (at least twice), else a name in the lead post. Among those, the
    one mentioned as the subject of the news (ROLE_RANK, by how the posts mostly mention them):
    "Texans HC DeMeco Ryans announced that WR Tank Dell will play" is about Tank Dell, "...emails
    with Jeffrey Epstein, Steve Tisch gave up..." about Steve Tisch. The same subject drives the
    Studio feed's photo search and every face comparison (hourly too). Only a candidate:
    bore/faceid.py decides whether it is a person."""
    from collections import Counter
    members = sorted(members, key=lambda m: -int(m.get("engagement") or 0))      # the lead first

    def key(n: str) -> str:                      # Sam Franklin Jr. (and Sean ‘Diddy’ Combs) is Sam Franklin
        return " ".join(w for w in n.lower().split() if w.rstrip(".") not in NAME_SUFFIX and w[0].isalpha())
    c, form, roles = Counter(), {}, {}
    for m in members:
        found = names_in(f"{m.get('title') or ''}\n{m.get('text') or ''}")
        c.update(list(dict.fromkeys(key(n) for n, _ in found)))      # once per post, in order
        for n, role in found:
            if len(n) > len(form.get(key(n), "")):
                form[key(n)] = n
            roles.setdefault(key(n), Counter())[role] += 1
    order = {k: i for i, k in enumerate(c)}

    def rank(k: str) -> int:     # how they are mostly mentioned otherwise than as an object (a tie: the worse)
        rs = {r: n for r, n in roles[k].items() if r not in ("object", "place")} or {"object": 1}
        return min(ROLE_RANK[r] for r, n in rs.items() if n == max(rs.values()))

    def best(keys) -> str:
        keys = list(dict.fromkeys(keys))
        return form[max(keys, key=lambda k: (rank(k), c[k], -order[k]))] if keys else ""
    for m in members:                      # a name in a headline, mentioned again elsewhere
        pick = best(key(n) for n, role in names_in(m.get("title") or "")
                    if role not in ("object", "place") and (c[key(n)] >= 2 or len(members) == 1))
        if pick:
            return pick
    top = max(c.values(), default=0)
    if top >= 2:                           # the most mentioned (a 49ers owner in 6 posts, not a policy in 4)
        pick = best(k for k, n in c.items() if n >= max(2, top * 3 / 4))
        if rank(key(pick)) >= ROLE_RANK[""]:
            return pick
    # the lead post's subject (or, when the posts name only coaches and speakers, the one they name most)
    lead = names_in(f"{members[0].get('title') or ''}\n{members[0].get('text') or ''}") if members else []
    return (best(key(n) for n, role in lead if rank(key(n)) >= ROLE_RANK[""]) or (pick if top >= 2 else "")
            or best(key(n) for n, role in lead if role not in ("object", "place"))
            or next((form[key(n)] for n, role in lead if role == "object"), ""))    # "a song by Britney Spears"


ROUNDUP_RE = re.compile(r"\b(winners and losers|what we learned|takeaways|news roundup|roundup|power rankings|"
                        r"live (updates|blog|coverage|scores?)|week \d+ (grades|recap|observations)|grades)\b|^live:", re.I)


def list_post(m: dict) -> bool:
    """A post naming several people (a ranking, a stat list), or a roundup or live-blog article:
    its photo shows someone else."""
    return (len(set(NAME_RE.findall(f"{m.get('title') or ''} {m.get('text') or ''}"))) >= 3
            or bool(m.get("platform") == "web" and ROUNDUP_RE.search(m.get("title") or "")))


def linked_photos(members: list[dict], max_links: int = 3) -> list[Photo]:
    """Lead photos of the articles the story's X posts link to (free: one redirect + one page)."""
    from concurrent.futures import ThreadPoolExecutor
    links = []
    for m in sorted((m for m in members if m.get("platform") == "x" and not list_post(m)),
                    key=lambda m: -int(m.get("engagement") or 0)):
        links += [(u, m) for u in x_links(m) if u not in {x for x, _ in links}]
    links = links[: max_links * 2]

    def one(pair):
        u, m = pair
        art = resolve_tco(u)
        if not art or any(h in domain(art) for h in SKIP_LINK_HOSTS):
            return None
        img = og_image(art)
        return Photo(img, "article", domain(art), via=f"the linked {domain(art)} article", post_url=art) if img else None

    out, seen = [], set()
    with ThreadPoolExecutor(6) as ex:
        for ph in ex.map(one, links):
            if ph and ph.url not in seen and not any(sd in ph.url for sd in STOCK_DOMAINS):
                seen.add(ph.url)
                out.append(ph)
    return out[:max_links]


def dhash(data: bytes) -> int:
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("L").resize((9, 8))
    px = list(im.tobytes())        # mode L: one byte per pixel (Image.getdata goes in Pillow 14)
    return sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])


def graphic(p: Photo, sports: bool = True) -> str:
    """Why a downloaded image is not a usable photo (a designed card, a screenshot, a scoreboard), or ''."""
    from .stills import inspect_still
    flags = inspect_still(p, sports=sports).flags
    bad = [f for f in flags if f.startswith(("scoreboard", "wide broadcast", "text-heavy"))]
    return bad[0] if bad else ""


def story_photos(members: list[dict], max_web: int = 4, policy: str = "allow", max_links: int = 0) -> list[Photo]:
    """Candidate photos from the cluster's own posts and articles, most engaged first.
    policy "never" (sports pages): no video frames (broadcast screenshots with scoreboards).
    max_links: also the lead photos of articles the X posts link to. Photos from posts that name
    several people (rankings, stat lists) go last: they usually show someone else."""
    out, seen = [], set()
    ranked = sorted(members, key=lambda m: -int(m.get("engagement") or 0))
    web_checked = 0
    for m in ranked:
        metrics = m.get("metrics") or {}
        if m["platform"] == "x":
            for med in metrics.get("media") or []:
                u = med.get("url")
                if u and u not in seen:
                    seen.add(u)
                    out.append(Photo(media_orig(u), "x", "@" + (m.get("author") or ""), med.get("w") or 0, med.get("h") or 0,
                                     note="video frame" if med.get("type") in ("video", "animated_gif") else "",
                                     via="in the post", post_url=m.get("url") or ""))
        elif m["platform"] == "reddit" and metrics.get("image"):
            u = metrics["image"]
            if u not in seen:
                seen.add(u)
                out.append(Photo(u, "reddit", "r/" + (m.get("subreddit") or ""), via="in the post", post_url=m.get("url") or ""))
        elif m["platform"] == "reddit" and metrics.get("link") and web_checked < max_web:
            web_checked += 1          # a link post: the lead image of the article it shares
            u = og_image(metrics["link"])
            if u and u not in seen:
                seen.add(u)
                out.append(Photo(u, "web", domain(metrics["link"]), via="the article the post links to",
                                 post_url=metrics["link"]))
        elif m["platform"] == "web" and metrics.get("image"):       # a feed article's lead image
            u = metrics["image"]
            if u not in seen:
                seen.add(u)
                out.append(Photo(u, "web", domain(m["url"] or ""), via="the article's lead image", post_url=m.get("url") or ""))
        elif m["platform"] == "web" and m.get("url") and web_checked < max_web:
            web_checked += 1
            u = og_image(m["url"])
            if u and u not in seen:
                seen.add(u)
                out.append(Photo(u, "web", domain(m["url"]), via="the article's lead image", post_url=m["url"]))
    out = [p for p in out if not (policy == "never" and p.note == "video frame")]
    if max_links:
        try:
            out += [p for p in linked_photos(members, max_links) if p.url not in seen]
        except Exception:
            pass
    listy = {m.get("url") for m in members if list_post(m)}
    return [p for p in out if p.post_url not in listy] + [p for p in out if p.post_url in listy]


def search_photos(query: str, apify, cfg: dict, max_results: int = 6) -> list[Photo]:
    """Web search for the subject, then the lead images of the top articles (news photos are
    usually 1200-2000px wide). One Google query through the budgeted Apify actor."""
    from .collect import expand_web, keep_web
    wc = cfg["apify"]["web"]
    run_input = dict(wc["input"])
    run_input.pop("quickDateRange", None)          # recent is not required for a portrait
    run_input[wc["queries_key"]] = query
    raw, _ = apify.run(wc["actor"], run_input, wc["timeout_seconds"])
    out = []
    for item in expand_web(raw, cfg["fields"]["web"], 0, wc)[:max_results]:
        u = og_image(item["url"])
        if u:
            out.append(Photo(u, "search", domain(item["url"])))
    return out


def floor_for(p: Photo, min_short_side: int) -> int:
    """Minimum short side for a photo: the picture other outlets used 600 (news images are often
    1200x630), a still of the story's video 720, anything else the page's floor."""
    if p.source == "news" and p.consensus >= 2:
        return min(min_short_side, 600)
    if p.note == "video frame":
        return min(min_short_side, 720)
    return min_short_side


def best(photos: list[Photo], min_short_side: int, limit: int = 3, allow_frames: bool = False,
         keep_order: bool = False) -> list[Photo]:
    """Download candidates and keep the sharp ones, largest first (keep_order: as ranked)."""
    good = []
    for p in photos:
        if p.note == "video frame" and not allow_frames:
            continue
        got = p if p.data and p.w else fetch(p)
        if got and got.short_side >= floor_for(got, min_short_side):
            good.append(got)
        if len(good) >= limit * 2:
            break
    if not keep_order:
        good.sort(key=lambda p: -(p.w * p.h))
    return good[:limit]


BING_M_RE = re.compile(r'\sm="(\{[^"]+\})"')


def image_search(query: str, must_contain: list[str] | None = None, max_candidates: int = 12) -> list[Photo]:
    """Large-image search (Bing image results, then Wikimedia Commons). Results must mention the
    subject (surname) in their title or page URL; the routine session still inspects the pick."""
    import html as _html
    import json as _json
    words = [w.lower() for w in (must_contain or query.split()[-1:]) if w]
    variants = [query] + ([" ".join(query.split()[:2])] if len(query.split()) > 2 else [])
    out, seen = [], set()
    for q in variants:
        try:
            r = requests.get("https://www.bing.com/images/async", headers=UA, timeout=20,
                             params={"q": q, "first": "0", "count": "35", "qft": "+filterui:imagesize-large",
                                     "mmasync": "1", "setlang": "en-us", "cc": "US"})
            items = [_json.loads(_html.unescape(m)) for m in BING_M_RE.findall(r.text)]
        except Exception:
            items = []
        for it in items:
            u, page, title = it.get("murl", ""), it.get("purl", ""), it.get("t", "")
            hay = (title + " " + page).lower()
            if any(sd in (u + " " + page).lower() for sd in STOCK_DOMAINS):   # watermarked stock: never
                continue
            if u and u not in seen and all(w in hay for w in words):
                seen.add(u)
                out.append(Photo(u, "search", domain(page) if page else domain(u), note=title[:80]))
        if len(out) >= max_candidates:
            break
    if len(out) < 3:   # free, high-resolution, clearly licensed fallback
        try:
            import os
            wua = {**UA, "User-Agent": os.environ.get("WIKIMEDIA_UA") or "BorePostStudio/1.0 (image search for editors)"}
            r = requests.get("https://commons.wikimedia.org/w/api.php", headers=wua, timeout=20, params={
                "action": "query", "generator": "search", "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": 10,
                "prop": "imageinfo", "iiprop": "url|size", "format": "json"})
            if r.status_code != 200:
                print(f"Wikimedia unavailable (HTTP {r.status_code})")
            for pg in (r.json().get("query", {}).get("pages", {}) or {}).values():
                ii = (pg.get("imageinfo") or [{}])[0]
                if ii.get("url") and re.search(r"\.(jpe?g|png|webp)$", ii["url"], re.I):
                    out.append(Photo(ii["url"], "commons", "Wikimedia Commons", ii.get("width", 0), ii.get("height", 0),
                                     note=pg.get("title", "")[:80]))
        except Exception:
            pass
    return out[:max_candidates]
