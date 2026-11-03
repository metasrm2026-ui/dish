"""Story kinds: what sort of story a post is ("practice update", "transaction", "feud"...), so the
editors' verdicts can be counted per kind and the ranking can learn which kinds they approve.

A page's playbook may define its own `kinds: [{id, label, match: [regex, ...]}]` (first match
wins); otherwise the generic list for the page's niche is used (sports or celebrity).
"""
from __future__ import annotations

import re

SPORTS = [
    ("live_ingame", "Live in-game update", [r"\b(td (catch|run|pass)|wins it on|\d+-yard(er)?|questionable to return|halftime|\d(st|nd|rd|th) quarter|opening drive|picks it off)\b"]),
    ("transaction_ps", "Trade / signing / release", [r"\bpractice squad\b"]),
    ("injury_practice", "Practice participation update", [r"\b(practice[ds]?|practicing|limited|full participant|full go|dnp|did not practice|participat\w*|back at practice|cleared to practice|expected back|designated to return|returns? to practice)\b"]),
    ("injury_ingame", "In-game injury", [r"\b(exits?|exited|carted|left the game|doubtful to return|questionable to return|in-game|dislocat\w*|concussion check)\b"]),
    ("injury_longterm", "Long-term injury (IR, surgery, season)", [r"\b(injured reserve|ir\b|season-ending|out for (the )?season|surgery|torn|acl|achilles|indefinitely|\d+-\d+ weeks|several weeks|multiple weeks|(couple|few) (of )?weeks|(expected to|will|to) miss)\b"]),
    ("injury_status", "Game status (questionable, ruled out)", [r"\b(questionable|doubtful|ruled out|game-time|day-to-day|week-to-week|good to go|sidelined|out (sunday|again|for now|this week|week \d+)|expected to play|could (play|return|sit)|downgraded|injur\w*|hamstring|ankle|knee|concussion|turf toe)\b"]),
    ("transaction", "Trade / signing / release", [r"\b(trade[sd]?|trading|signs?|signed|signing|releas\w+|waive[sd]?|cut|extension|contract|re-?sign\w*|acquir\w+|claim(s|ed)? off waivers|retire[sd]?|retires|workout)\b"]),
    ("coaching", "Coaching / front office", [r"\b(fired|hire[sd]?|coordinator|play-?calling|takes over (the )?(offense|defense)|head coach search|interim)\b"]),
    ("off_field", "Off-field (fines, owners, refs, life)", [r"\b(fined?|suspend\w*|ban(ned)?|owner|apolog\w*|referee|officiat\w*|arrest\w*|lawsuit|dies|died|dead at|expecting|wedding|engaged|charity)\b"]),
    ("stat_record", "Stat / record / milestone", [r"\b(record|milestone|histor\w*|first (player|qb|team) (to|since)|most \w+ (in|since)|leads? the (league|nfl)|undefeated|career-high|franchise)\b", r"\d+ (yards|tds?|touchdowns|catches|receptions|sacks|interceptions)"]),
    ("performance", "Game performance / result", [r"\b(beat|beats|win|wins|won|loss|lose[sd]?|stun\w*|comeback|upset|blowout|goes off|dominat\w+|highlight)\b"]),
    ("schedule_logistics", "Schedule / logistics / minor detail", [r"\b(flexed|kickoff time|moved to|jersey (number|no\.)|no\. \d+|travel\w*|london|uniform|depth chart)\b"]),
    # only a post that IS a quote ("Name: "..."", or starting with a quote mark), not one quoting a phrase
    ("quote", "Quote / reaction", [r"^\s*[\"“‘']", r"^[^\"“:]{2,60}:\s*[\"“‘']"]),
]
ALIASES = {"transaction_ps": "transaction"}      # one kind, two patterns

# Crime and court words: the crime_court kind and, with the abuse words, the Studio feed's rule that
# a crime, court or abuse story never gets a photo search by name (bore/studio.py crime_story).
# bore/select.py keeps a narrower list on purpose: it shields crime stories from the politics
# filter, and "court" or "police" alone would let the Supreme Court's and the protests' politics in.
CRIME_COURT = (r"arrest\w*|charged|guilty|sentenc\w*|trial|court|lawsuit|sued|suing|murder\w*|police|dui|verdicts?|"
               r"indict\w*|jail\w*|prison")
CRIME_ABUSE_RX = re.compile(rf"\b({CRIME_COURT}|convict\w*|killed|killing|killers?|homicide|manslaughter|shooting|"
                            r"shot dead|stabb\w*|assault\w*|abus\w+|rape\w*|sexual\w*|molest\w*|kidnap\w*|jury|accuser|"
                            r"domestic violence|jane doe|john doe|victims?)\b", re.I)

CELEBRITY = [
    ("death", "Death / tribute", [r"\b(dies|died|dead at|passed away|passes away|death of|funeral|memorial|tributes? pour)\b"]),
    ("crime_court", "Crime / court", [rf"\b({CRIME_COURT})\b"]),
    ("viral_wtf", "Viral / strange real-life moment", [r"\b(viral|video shows|footage|caught on camera|attack(s|ed)? (by|on)|tiger|bear|shark|bizarre|wild moment|internet (reacts|outraged|is losing))\b"]),
    ("relationship", "Relationship (engaged, split, married)", [r"\b(engaged|engagement|propos\w+|married|marries|wedding|divorc\w+|split|breakup|break up|dating|girlfriend|boyfriend|husband|wife|baby|pregnan\w+|expecting)\b"]),
    ("health_selfshared", "Health news they shared", [r"\b(cancer|diagnos\w+|hospitali[sz]ed|surgery|health (update|scare|battle)|rehab|recovery)\b"]),
    ("feud_clash", "Feud / callout / clash", [r"\b(slams?|blasts?|claps back|fires back|shades?|feud|clash\w*|calls out|roasts?|snubs?|responds to|hits back|accus\w+)\b"]),
    ("record_money", "Record / money / flop", [r"(#1\b|\brecord\b|\bhistory\b|\bbillion\w*|\bmillion\b|\$\d|\bflop\w*|\bbox office\b|\bsells? out\b)"]),
    ("promo_release", "Promo / release / casting", [r"\b(trailer|teaser|premiere\w*|release date|where to watch|streaming|season \d+|episode \d+|album|single|tour dates|cast(ing)?|first look|renew\w+|cancel\w+)\b"]),
    ("politics", "Politics", [r"\b(trump|biden|harris|congress\w*|senat\w+|election|campaign|democrat\w*|republican\w*|white house|governor|press secretary)\b"]),
    ("red_carpet_fluff", "Red carpet / appearance fluff", [r"\b(stuns?|red carpet|spotted|rare photos?|sweet moment|outfit|fashion|look\b|attends?)\b"]),
]

OTHER = ("other", "Other")
_LABELS = {ALIASES.get(k, k): lbl for k, lbl, _ in SPORTS + CELEBRITY} | {OTHER[0]: OTHER[1]}


def profile_for(page_slug: str, playbook: dict | None = None) -> list:
    """The ordered kind list for a page: the playbook's own, else sports or celebrity."""
    pb = playbook or {}
    own = pb.get("kinds")
    if isinstance(own, list) and own:
        return [(k.get("id"), k.get("label") or k.get("id"), k.get("match") or []) for k in own if k.get("id")]
    hay = " ".join(str(x) for x in (page_slug, pb.get("name"), pb.get("niche"), pb.get("page"))).lower()
    return SPORTS if re.search(r"\b(nfl|football|nba|mlb|nhl|soccer|sports?|huddle)\b", hay) else CELEBRITY


_CACHE: dict = {}


def _compiled(profile: list) -> list:
    key = tuple((k, tuple(rxs)) for k, _, rxs in profile)    # by content: playbook lists are rebuilt
    if key not in _CACHE:
        _CACHE[key] = [(k, [re.compile(rx, re.I) for rx in rxs]) for k, _, rxs in profile]
    return _CACHE[key]


def kind_of(text: str, profile: list) -> str:
    """The first kind whose pattern matches the text."""
    t = (text or "").strip()
    for k, rxs in _compiled(profile):
        if any(r.search(t) for r in rxs):
            return ALIASES.get(k, k)
    return OTHER[0]


def kind_for(headline: str, text: str, profile: list) -> str:
    """A story's kind from its headline, else from the start of its text."""
    k = kind_of(headline, profile)
    return k if k != OTHER[0] else kind_of(f"{headline} {(text or '')[:300]}", profile)


def label(kind: str, profile: list | None = None) -> str:
    for k, lbl, _ in profile or []:
        if k == kind:
            return lbl
    return _LABELS.get(kind, kind.replace("_", " "))


def ids(profile: list) -> list[str]:
    return list(dict.fromkeys(ALIASES.get(k, k) for k, _, _ in profile)) + [OTHER[0]]


__all__ = ["SPORTS", "CELEBRITY", "CRIME_COURT", "CRIME_ABUSE_RX", "profile_for", "kind_of", "kind_for", "label", "ids"]
