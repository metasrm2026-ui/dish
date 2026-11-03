"""Tokenising, timestamps, field extraction and identity hashing."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'’]*[a-z0-9]|[a-z0-9]", re.I)

STOPWORDS = frozenset("""
a about above after again against all also am an and any are aren't as at be because been before being below
between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down during each
few for from further get gets got had hadn't has hasn't have haven't having he he'd he'll he's her here here's
hers herself him himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself just let's
like me more most mustn't my myself new no nor not now of off on once one only or other ought our ours
ourselves out over own per please rt same say says said she she'd she'll she's should shouldn't so some
such than that that's the their theirs them themselves then there there's these they they'd they'll they're
they've this those through to too under until up us very via was wasn't we we'd we'll we're we've were
weren't what what's when when's where where's which while who who's whom why why's will with won't would
wouldn't yes yet you you'd you'll you're you've your yours yourself yourselves amp today tonight
tomorrow yesterday going gonna know see think want need make made way back still even well really
http https com www
""".split())

# Sports acronyms excluded from any ALL-CAPS "shouting" metric.
SPORTS_ACRONYMS = frozenset("""
NFL NBA MLB NHL MLS WNBA NCAA NCAAF NCAAB CFB CBB SEC ACC PAC BIG XII AFC NFC ALCS NLCS ALDS NLDS WS
QB RB WR TE OL OT OG DL DE DT LB ILB OLB CB DB FS SS FB KR PR ST OC DC HC GM
RBI ERA OPS OBP SLG HR SB SP RP DH IL DL WAR WHIP BABIP
PPG RPG APG FG FT 3PT MVP ROY DPOY OPOY ALL USA ESPN FOX CBS NBC ABC TNT
TD TDS INT YDS YAC EPA PFF IR PUP OTA OTAS ACL MCL PCL UFC PGA LIV F1 NASCAR
UCLA USC LSU TCU SMU BYU UCF UNLV OSU FSU UNC UK UGA MSU TBD TBA ICYMI BREAKING
OT OTL PTS REB AST BLK STL TO
AGT SNL VMA VMAS MTV BET HBO TV NYC LA UK US TMZ FBI CIA DNA DOJ CEO GOAT OMG LOL AI BBC CNN
""".split())

_TWITTER_FMT = "%a %b %d %H:%M:%S %z %Y"


def stem(t: str) -> str:
    """Very light suffix stripping so "benched"/"bench" and "starting"/"start" match.
    Short tokens are left alone (three-char tokens like "zay" carry whole stories)."""
    if len(t) > 5 and t.endswith("ing"):
        return t[:-3]
    if len(t) > 4 and t.endswith("ed"):
        return t[:-2]
    if len(t) > 4 and t.endswith("es") and t[-3] in "sxz":
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


HASHTAG_CAMEL_RE = re.compile(r"#([A-Za-z0-9_]+)")
CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _split_hashtag(m: re.Match) -> str:
    """#UnraveledTour -> "Unraveled Tour", so hashtag posts match plain-text posts."""
    return " " + CAMEL_SPLIT_RE.sub(" ", m.group(1)).replace("_", " ") + " "


def tokens(text: str) -> list[str]:
    """Content tokens: lowercased, URLs removed, stopwords removed, camel-case hashtags split.
    Three-char tokens are kept ("zay", "mcl", "usc" carry whole stories)."""
    text = HASHTAG_CAMEL_RE.sub(_split_hashtag, URL_RE.sub(" ", text or "")).replace("@", " ")
    out = []
    for t in TOKEN_RE.findall(text.lower()):
        t = t.replace("’", "'")
        if t.endswith("'s"):
            t = t[:-2]
        if len(t) < 3 or t in STOPWORDS or t.isdigit():
            continue
        out.append(stem(t))
    return out


def token_set(text: str) -> frozenset[str]:
    return frozenset(tokens(text))


NOT_ENTITIES = frozenset("""monday tuesday wednesday thursday friday saturday sunday january february
march april may june july august september october november december week breaking report
watch highlight video photo photos update source sources official fans player players team here
setlist live exclusive first opening night season finale episode
""".split())
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’]*")


def entities(text: str) -> frozenset[str]:
    """Stemmed tokens of proper nouns: capitalised words that are not sentence-initial, plus the
    words of camel-case hashtags. Names are what identify a story ("Olivia Rodrigo"), while
    template words ("beat", "walk-off", "injury report") are shared by different events.
    Title-Case headlines carry no signal (every word is capitalised), so they yield none."""
    text = HASHTAG_CAMEL_RE.sub(_split_hashtag, URL_RE.sub(" ", text or ""))
    words = [w for w in WORD_RE.findall(text) if len(w) >= 3 and w.lower() not in STOPWORDS]
    if not words:
        return frozenset()
    caps = [w for w in words if w[0].isupper()]
    if len(words) >= 8 and len(caps) / len(words) > 0.8:   # a Title-Case headline
        return frozenset()
    out = set()
    for w in WORD_RE.findall(text):
        if len(w) < 3 or not w[0].isupper() or (w.isupper() and len(w) > 4):
            continue
        t = w.lower().replace("’", "'")
        if t.endswith("'s"):
            t = t[:-2]
        if t in STOPWORDS or t in NOT_ENTITIES:
            continue
        out.add(stem(t))
    return frozenset(out)


def parse_ts(value) -> float | None:
    """Unix seconds from unix s/ms, ISO-8601, or Twitter's legacy format."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    s = str(value).strip()
    if s.isdigit():
        return parse_ts(int(s))
    try:
        return datetime.strptime(s, _TWITTER_FMT).timestamp()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def get_path(obj, path: str):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def pick(obj, candidates: list[str], default=None):
    """First candidate path with a non-empty value wins."""
    for path in candidates or []:
        v = get_path(obj, path)
        if v not in (None, "", [], {}):
            return v
    return default


def to_int(v) -> int:
    if v is None:
        return 0
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "").upper()
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def normalise_url(url: str) -> str:
    """Canonical URL for identity. Keeps path and query (articles differ there)."""
    parts = urlsplit(url.strip())
    netloc = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", netloc, path, parts.query, ""))


def identity(platform: str, native_id: str | None = None, url: str | None = None,
             text: str | None = None) -> str:
    """Stable item id. Web items are identified BY URL: never normalise URLs away,
    or every web article collapses into one."""
    if platform == "web" and url:
        key = "web|" + normalise_url(url)
    elif native_id:
        key = f"{platform}|{native_id}"
    elif url:
        key = f"{platform}|{normalise_url(url)}"
    else:
        key = f"{platform}|text|{(text or '').strip()}"
    return hashlib.sha1(key.encode()).hexdigest()


def domain(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")
