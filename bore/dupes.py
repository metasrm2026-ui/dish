"""Same-story detection against posts already made (the dashboard's drafts are the record).

A story is a repeat when it names the same people/things AND describes the same situation:
  - at least 2 shared names and 3 shared content words ("Tyson Bagent" + cleared/concussion/
    protocol/practice), or
  - at least 5 shared names (a long caption about the same event).
Two different stories about one celebrity share the names but not the situation words
(Taylor Swift's chart debut vs her road trip: 3 names, 1 word), so they are not repeats.

Names that say nothing about the story are ignored: the page's own name, hashtags (#TrueCrime
#Justice made every two crime posts share 'names'), the reporters and outlets it quotes ("per
Alex Rivera", or a name that spells one of the page's accounts: "Alex Rivera reports"), NFL
team names (and their cities on a sports page), and words capitalised only because they start a
sentence ("Police said...", "Officers found..."). A person's name stays a name even when a word of
it is inside a handle: 'jordan' is in a reporter's handle, Jordan Elliott is still the story. (Not
"names common across recent posts": a story repeated four times would make its own subject look
common.) tests/test_repeat_gate.py holds real pairs of drafts (Oct 11-21): different stories the
gate must let through, and real repeats it must stop, with the Sep 29 page and one shaped like the
live one (about 30 reporters' handles).
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

from .config import Page
from .textutil import STOPWORDS, entities, stem, token_set

HASHTAG_RE = re.compile(r"#\w+")
# a reporter or outlet credited for the news: "per Alex Rivera and Sam Cole", "via @ColeReports"
_WHO = r"(?:@\w+|[A-Z][\w'’.\-]*(?:\s+[A-Z][\w'’.\-]*){0,3})"
CREDIT_RE = re.compile(rf"\b(?i:per|via|according to|reported by)\s+{_WHO}(?:\s*(?:,|and|&)\s*{_WHO})*")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’]*")
CAPS_LINE_RE = re.compile(r"[^a-z]*[A-Z][^a-z]*")
PLURAL_CAPS_RE = re.compile(r"\b[A-Z]{2,}s\b")      # "Vikings WRs", "TDs": positions, not names

MIN_NAMES, MIN_WORDS, MANY_NAMES = 2, 3, 5


def _team_tokens() -> set[str]:
    try:
        from .teams import NFL_TEAMS
    except Exception:  # pragma: no cover
        return set()
    return {t for k in NFL_TEAMS for t in token_set(k)}


GENERIC = _team_tokens() | {"nfl", "mnf", "snf", "tnf", "nba", "mlb", "nhl", "ufc", "wwe", "fan",
                            "breaking", "news", "entertainmentnew", "celebritynew", "viral", "trend",
                            # where a story was seen ("per a viral Reddit thread"), never what it is
                            "reddit", "tiktok", "instagram", "youtube", "twitter", "facebook", "snapchat"}


def _cities():
    from .teams import NFL_CITIES, NFL_TEAMS
    before_team = re.compile(rf"\b(?:{'|'.join(NFL_CITIES)})(?=\s+(?:{'|'.join(NFL_TEAMS)})\b)", re.I)
    return before_team, token_set(" ".join(NFL_CITIES))


# The teams' cities ("Green Bay" made two Jordan Love stories share 4 names): never names before a
# team's name, nor on a sports page. Elsewhere they are names (Whitney Houston, Draymond Green).
CITY_TEAM_RE, CITY_TOKENS = _cities()
# two capitalised words that may be a person's name ("Jordan Elliott", "the Network's Alex Rivera")
NAME_PAIR_RE = re.compile(r"\b([A-Z][a-z][\w'’\-]*)\s+(?=([A-Z][a-z][\w'’\-]*))")


# Words every story of a kind uses; they never make two stories the same.
WEAK_WORDS = {"game", "night", "day", "week", "weekend", "season", "year", "today", "tonight", "tomorrow",
              "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "ahead",
              "matchup", "first", "new", "last", "big", "huge", "time", "back", "report", "confirm",
              "share", "post", "reveal", "fan", "people", "star", "show", "much", "never", "already",
              "great", "just", "now", "still", "break", "look", "watch", "call", "make", "take",
              "say", "ask", "tell", "told", "talk", "add"}      # every quote story says, asks and tells


@dataclass
class Features:
    names: frozenset[str]      # tokens written as proper nouns in this text
    tokens: frozenset[str]     # every meaningful token (names included)
    head: frozenset[str] = frozenset()   # the names in its headline lines (the top ones): whom it is about


@dataclass
class Match:
    draft: dict
    names: list[str]
    words: list[str]

    def describe(self) -> str:
        d = self.draft
        return (f"{d.get('id')} [{d.get('status')}] \"{(d.get('headline') or '')[:70]}\" "
                f"(same names: {', '.join(self.names)}; same details: {', '.join(self.words[:8])})")


def draft_text(d: dict) -> str:
    return f"{d.get('headline') or ''}\n{d.get('caption') or ''}"


def story_text(text: str) -> str:
    """What a post says about its story: without hashtags and the reporters it credits (and with
    "Commanders' end-of-game" read as Commanders)."""
    return re.sub(r"(?<=s)['’](?=\W|$)", "", CREDIT_RE.sub(" ", HASHTAG_RE.sub(" ", text or "")))


def _key(w: str) -> str:
    t = w.lower().replace("’", "'")
    return stem(t[:-2] if t.endswith("'s") else t)


def sentence_capitals(text: str) -> frozenset[str]:
    """Words capitalised only because they start a sentence or line ("Police said", "Officers
    found"): not also capitalised inside a sentence, and not the start of a longer name."""
    first, inside = set(), set()
    words = list(_WORD_RE.finditer(text))
    for i, m in enumerate(words):
        w = m.group(0)
        if not w[0].isupper() or w.lower() in STOPWORDS:
            continue
        before = text[:m.start()].rstrip(" \t\"“‘'(*•-–—")
        nxt = words[i + 1].group(0) if i + 1 < len(words) else ""
        joined = i + 1 < len(words) and not text[m.end():words[i + 1].start()].strip()     # "Jalen Hurts"
        if (not before or before.endswith(("\n", ".", "!", "?", ":"))) and not (joined and nxt[:1].isupper()):
            first.add(_key(w))
        else:
            inside.add(_key(w))
    return frozenset(first - inside)


class Deduper:
    def __init__(self, page: Page, recent: list[dict]):
        from .stills import SPORTS
        self.recent = [d for d in recent if d.get("page") == page.slug]
        handles = [h.lower() for h in page.x_handles + page.tier_one + page.officials + page.x_handles_secondary]
        self.page_tokens = set(token_set(page.name)) | set(token_set(page.slug.replace("-", " ")))
        if {str(x).lower() for x in page.leagues} & SPORTS:
            self.page_tokens |= CITY_TOKENS
        self.handles = handles
        self.features = [self.features_of(draft_text(d)) for d in self.recent]

    def _raw(self, text: str) -> Features:
        text = CITY_TEAM_RE.sub(" ", story_text(text))
        # an ALL-CAPS headline line capitalises every word: its names are the ones the text names
        body = "\n".join(ln for ln in text.splitlines() if not CAPS_LINE_RE.fullmatch(ln))
        names = (frozenset(entities(body)) - sentence_capitals(body)
                 - token_set(" ".join(PLURAL_CAPS_RE.findall(body))))
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        top = lines[:1] + [ln for ln in itertools.takewhile(CAPS_LINE_RE.fullmatch, lines[1:])]   # headline(s)
        return Features(names, frozenset(token_set(text)), frozenset(token_set("\n".join(top))) & names)

    def _people(self, text: str) -> tuple[frozenset[str], frozenset[str]]:
        """Two-word names in a text: (the words of those that spell one of the page's accounts:
        'Alex Rivera' for alexrivera, 'Dana Price' for dpricenews; the words of all others:
        people the story is about, 'Jordan Elliott')."""
        source, other = set(), set()
        for m in NAME_PAIR_RE.finditer(text):
            if m.group(1).endswith(("'s", "’s", "'", "’")) or m.group(1).lower() in STOPWORDS:   # "Network's Ian"
                continue
            first, last = (re.sub(r"\W", "", re.sub(r"['’]s?$", "", w)).lower() for w in m.groups())
            spelled = any(first + last in h or (len(last) >= 4 and h.startswith(first[0]) and last in h[1:])
                          for h in self.handles)
            (source if spelled else other).update(token_set(f"{first} {last}"))
        return frozenset(source - other), frozenset(other)

    def _generic(self, t: str, people: frozenset[str] = frozenset()) -> bool:
        # a word inside a handle ("rivera" in alexrivera, "@ColeReports"), unless it is part of a
        # person's name here ('jordan' is in a reporter's handle; Jordan Elliott is the story)
        return (t in GENERIC or t in self.page_tokens
                or (t not in people and len(t) >= 3 and any(t in h for h in self.handles)))

    def _clean(self, f: Features, text: str = "") -> Features:
        source, people = self._people(story_text(text))
        keep = frozenset(t for t in f.tokens | f.names if t not in source and not self._generic(t, people))
        return Features(frozenset(n for n in f.names if n in keep),
                        frozenset(t for t in keep if t in f.names or t not in WEAK_WORDS), f.head & keep)

    def features_of(self, text: str) -> Features:
        return self._clean(self._raw(text), text)

    @staticmethod
    def same_story(a: Features, b: Features) -> tuple[bool, list[str], list[str]]:
        # a shared token is a name if either text writes it as one (an all-Title-Case tweet
        # detects no names of its own, but "Barkley" is still a name)
        shared = a.tokens & b.tokens
        names = shared & (a.names | b.names)
        n, w = sorted(names), sorted(shared - names)
        # when both headlines name someone, the stories share one of them: a suspension story that
        # mentions an earlier one (Franklin, then Gardner-Johnson) is not that story
        if a.head and b.head and not (names & a.head and names & b.head):
            return False, n, w
        # the same people in both headlines, four names in all: one detail is enough (a punch, then
        # its review: the stories tell it in different words), or none when every name in both
        # headlines is shared ("HURTS SIDELINED" then "HURTS OUT, MCKEE SET TO START": Jalen Hurts
        # and Tanner McKee in both, no word in common)
        subject = names & a.head & b.head
        return ((len(n) >= MIN_NAMES and len(w) >= MIN_WORDS) or len(n) >= MANY_NAMES
                or (len(n) >= 4 and bool(subject) and (bool(w) or a.head | b.head <= names))), n, w

    def match(self, text: str, ignore: set[str] | None = None) -> Match | None:
        """The recent post this text repeats, if any (the strongest overlap)."""
        f = self.features_of(text)
        best, best_key = None, (-1, -1)
        for d, g in zip(self.recent, self.features):
            if ignore and d.get("id") in ignore:
                continue
            dup, n, w = self.same_story(f, g)
            if dup and (len(n), len(w)) > best_key:
                best, best_key = Match(d, n, w), (len(n), len(w))
        return best

    def add(self, draft: dict) -> None:
        """Count a post made in this run, so the run does not pick the same story twice."""
        self.recent.append(draft)
        self.features.append(self.features_of(draft_text(draft)))


def trend_text(t) -> str:
    """What a candidate story says: its lead post plus the other top posts."""
    members = sorted(t.cluster.members, key=lambda m: -int(m.get("engagement") or 0))[:6]
    return "\n".join(f"{m.get('title') or ''} {m.get('text') or ''}" for m in members)
