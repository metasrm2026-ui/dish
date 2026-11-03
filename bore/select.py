"""Scoring, verification routes, repeat suppression, watchlist and escalation."""
from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field

import re

from .cluster import IDF, Cluster, cluster_items, fresh, item_text, signature, trend_key, ts
from .config import Page, priority_terms
from .textutil import tokens


# --------------------------------------------------------------------- scoring
def score(cl: Cluster, cfg: dict, now: float) -> float:
    w = cfg["scoring"]["weights"]
    hl = cfg["scoring"]["recency_half_life_hours"]
    hours = max((now - cl.first_seen) / 3600, 0.25)
    velocity = cl.engagement / hours
    age_latest = max((now - cl.last_seen) / 3600, 0)
    return (w["sources"] * math.log1p(len(cl.sources))
            + w["platforms"] * (len(cl.platforms) - 1)
            + w["velocity"] * math.log1p(velocity)
            + w["volume"] * math.log1p(len(cl.members))
            + w["recency"] * 0.5 ** (age_latest / hl))


def prefilter_rules(page: Page) -> tuple[list, list]:
    """The page playbook's prefilter: skip patterns (drop before Claude sees the story) and boost
    patterns (the kinds of story editors approve rank higher)."""
    from .routine import load_playbook
    pf = load_playbook(page.slug).get("prefilter") or {}
    comp = lambda xs: [re.compile(x, re.I) for x in xs or []]
    return comp(pf.get("skip")), comp(pf.get("boost")), {x.lower() for x in pf.get("trade_only_sources") or []}


# Topics a page's playbook rules out in its worth_posting "NO:" list, keyed by the word that list
# uses. The routine skipped every such story it was ever briefed on (politics and storm stories on
# a celebrity page, show plugs on a sports page; none posted), so they leave before briefing.
# Only the headline is tested. Politics means institutions, elections, courts, enforcement and wars:
# a politician's name alone never counts, since a star clashing with a politician is a kind editors
# approve (the routine judges those).
TOPIC_RX = {
    "politic": re.compile(
        r"\b(white house|pentagon|congress(ional)?|senate|legislat(ure|ion)|gop|democrats?|democratic|"
        r"republicans?|maga|midterms?|elections?|ballot|supreme court|federal (judge|court|agents?)|doj|"
        r"justice department|ice (agents?|raids?|operation)|immigration|deport(ation|ations|ed)?|"
        r"sanctuary cit(y|ies)|tariffs?|social security|ceasefire|nato|kremlin|moscow|ukrain(e|ian)|iran(ian)?|"
        r"houthis?|gaza|hamas|strait of hormuz|parliament|usaid)\b", re.I),
    "disaster": re.compile(
        r"\b(hurricane|tropical (storm|depression)|typhoon|cyclone|tornado (warnings?|watch(es)?)|storm surge|"
        r"landfall|evacuation (orders?|advisor(y|ies))|red-tagged|earthquake|magnitude \d)\b", re.I),
    "promotional": re.compile(
        r"\b(full show|streams? tonight|coming up (today|tonight)|is live\b|preview (&|and) predictions|"
        r"(score|upset) predictions|countdown headlines|moment of the week)", re.I),
}
# A star clashing with a politician is a kind editors approve, and a disaster story with a person in
# it has the human-interest angle the playbooks allow: those stay for the routine to judge.
ENTERTAINMENT_RX = re.compile(
    r"\b(actor|actress|singer|rapper|rocker|musician|comedian|host|celebrit\w*|stars?|author|influencer|"
    r"streamer|youtuber|tiktoker|model|director|producer|frontman|late-night|talk show|snl|saturday night live|"
    r"reality (tv|star|show)|netflix|hbo|hulu|oscars?|grammys?|emmys?|hollywood|broadway|album|concert|tour|"
    r"band|memoir|sitcom|movie|film|(his|her|their|the) show|hall of fame)\b", re.I)
HUMAN_INTEREST_RX = re.compile(r"\b(rescu\w+|hero|survivors?|viral|video|caught on camera|reunit\w+|dog|cat|horse)\b",
                               re.I)
# a public figure's own life (an anniversary, a death, an illness) is a moment, not policy
PERSONAL_RX = re.compile(r"\b(wedding|anniversary|marri\w+|divorc\w+|engage\w*|baby|pregnan\w+|birthday|dies|died|"
                         r"death of|funeral|cancer|diagnos\w+|hospitali[sz]ed|surgery)\b", re.I)
# a court is politics only when the case is: a verdict or sentence is the true-crime kind editors approve
CRIME_RX = re.compile(r"\b(sentenc\w*|verdicts?|trial|convict\w*|guilty|murder\w*|killers?|homicide|manslaughter|"
                      r"prison|jail\w*|arrest\w*|indict\w*|(court|bail|sentencing|plea|custody) hearing)\b", re.I)
# the White House trading shots with someone is that person's clash (the routine judges it): in an
# entertainment feed that someone is a star whose name no word list knows
CLASH_RX = re.compile(r"\bwhite house (fires back|hits back|claps back|slams|mocks|rips|blasts|roasts|responds|"
                      r"trolls|demands|attacks|calls out|lashes out)\b|\b(slams|mocks|rips|blasts|roasts|trolls|"
                      r"fires back at|hits back at|claps back at|responds to) (the )?white house\b", re.I)
PROTECT = {"politic": (ENTERTAINMENT_RX, HUMAN_INTEREST_RX, PERSONAL_RX, CRIME_RX, CLASH_RX),
           "disaster": (ENTERTAINMENT_RX, HUMAN_INTEREST_RX)}


def topic_rules(playbook: dict | None) -> dict:
    """The TOPIC_RX entries this page's playbook rules out (its worth_posting "NO:" list names
    them). Taking the topic out of that list turns its filter off."""
    wp = str((playbook or {}).get("worth_posting") or "")
    m = re.search(r"\bNO:(.*?)(?:\bLEARNED\b|\bYES:|$)", wp, re.S)
    no = m.group(1).lower() if m else ""
    return {k: rx for k, rx in TOPIC_RX.items() if k in no}


def excluded_topic(headline: str, text: str, topics: dict, prio_rx: list) -> str | None:
    """The ruled-out topic this story's headline is about, unless the story names an entertainment
    figure or show, or one of the page's core names, anywhere in its text. A post without a title
    (X) is judged on its first line: a quote further down is not the story's topic."""
    head = (headline or "").strip().split("\n")[0][:200]
    hit = next((k for k, rx in topics.items() if rx.search(head)), None)
    if not hit:
        return None
    whole = f"{headline} {text or ''}"
    if any(r.search(whole) for r in PROTECT.get(hit, ())) or any(r.search(whole) for r in prio_rx):
        return None
    return hit


def _nfl_rx() -> re.Pattern:
    from .teams import NFL_TEAMS
    teams = "|".join(sorted((re.escape(k) for k in NFL_TEAMS), key=len, reverse=True))
    # a story often names only the player ("Jeanty limited in practice"): injury and roster words count
    return re.compile(r"\b(nfl|football|quarterbacks?|qbs?|touchdowns?|tds?|super bowl|linebackers?|wide receivers?|"
                      r"running backs?|cornerbacks?|tight ends?|kickers?|coach\w*|coordinator|draft\w*|rookies?|"
                      r"practice\w*|injur\w*|questionable|doubtful|ruled out|sidelined|trade[ds]?|sign(s|ed|ing)?|"
                      r"contract|roster|preseason|playoffs?|offensive|defensive|sacks?|yards|\d+-yard\w*|"
                      r"interceptions?|fg|field goal|all-pro|pro bowl|mvp|"
                      rf"week \d+|{teams})\b", re.I)


NFL_RX = _nfl_rx()
# what the NFL page's web searches also bring: news, markets, weather, crime, other sports, streams
OFF_NICHE_WEB_RX = re.compile(
    r"\b(live ?stream|watch (tv )?online|recipes?|weather|storm|flood\w*|wildfire|house fire|damage|stocks?|"
    r"markets?|revenue|earnings|crypto|data cent(er|re)s?|h-1b|nhl|nba|mlb|wnba|ncaa|college|high school|"
    r"hockey|baseball|basketball|soccer|governor|opinion|protest\w*|drone strike|murder|shooting|execution|"
    r"theft|found dead|police|pet|trump|putin)\b", re.I)


def off_niche_web(members: list[dict]) -> bool:
    """An NFL page's web search results about something else (storms, markets, politics, other
    sports): no football word or team in them, and a sign of what they are about instead (such
    briefs were all skipped). A play or a coach's quote often names only the people
    ("Joe Example dots Sam Example from DEEP"), so no football word alone is not enough."""
    if not members or any(m.get("platform") != "web" for m in members):
        return False
    texts = [f"{m.get('title') or ''} {m.get('text') or ''}" for m in members]
    return not any(NFL_RX.search(t) for t in texts) and any(
        rx.search(t) for t in texts for rx in (OFF_NICHE_WEB_RX, TOPIC_RX["politic"], TOPIC_RX["disaster"]))


def lead_text(cl: Cluster) -> str:
    lead = cl.lead()
    return f"{lead.get('title') or ''} {lead.get('text') or ''}"


def priority_matcher(terms: list[str]):
    """Multi-word names match case-insensitively; single words only as written (avoids 'Future'
    the rapper matching 'the future')."""
    multi = sorted((t for t in terms if " " in t), key=len, reverse=True)
    single = sorted((t for t in terms if " " not in t), key=len, reverse=True)
    rx = []
    if multi:
        rx.append(re.compile(r"(?<!\w)(" + "|".join(map(re.escape, multi)) + r")(?!\w)", re.I))
    if single:
        rx.append(re.compile(r"(?<!\w)(" + "|".join(map(re.escape, single)) + r")(?!\w)"))
    return rx


def priority_hits(cl: Cluster, rx: list) -> int:
    """Distinct priority names mentioned across the cluster's posts."""
    found = set()
    for m in cl.members:
        text = " ".join(filter(None, (m.get("title"), m.get("text"))))
        for r in rx:
            found.update(x.lower() for x in r.findall(text))
    return len(found)


# ------------------------------------------------------------------ selection
@dataclass
class Trend:
    cluster: Cluster
    score: float
    route: str                 # corroborated | scoop | watchlist
    key: str
    sig: list[str]
    developed: str = ""        # why a previously briefed story was allowed back
    matched_key: str = ""
    kind: str = ""             # story kind (bore/kinds.py)
    mult: float = 1.0          # its learned weight from the editors' reviews (bore/lessons.py)

    @property
    def headline(self) -> str:
        lead = self.cluster.lead()
        return (lead.get("title") or lead.get("text") or "").strip().replace("\n", " ")


@dataclass
class PageResult:
    page: str
    trends: list[Trend] = field(default_factory=list)
    watchlist: list[Trend] = field(default_factory=list)
    suppressed: list[dict] = field(default_factory=list)
    escalation: dict | None = None
    item_count: int = 0
    shortlist: list[Trend] = field(default_factory=list)   # what to post this briefing
    quota: int = 0


def postable(t: "Trend", min_tokens: int) -> bool:
    """A pick must carry enough content to write a post from ("Unforgettable." does not)."""
    return len(tokens(item_text(t.cluster.lead()))) >= min_tokens


def shortlist(res: "PageResult", cfg: dict, quota: int | None = None, dedupe=None) -> None:
    """Pick this run's posts: verified trends first (by score), then the strongest watchlist
    stories to fill the quota. Unverified fills are flagged in the briefing.
    dedupe: a dupes.Deduper over the page's recent posts. Stories that repeat a recent post, or
    another story already on this shortlist (picks AND backups), are left out."""
    from .dupes import trend_text
    s = cfg["selection"]
    res.quota = quota if quota is not None else -(-s["posts_per_day"] // s["briefings_per_day"])
    want = res.quota + s["shortlist_backups"] if res.quota else 0
    min_tok = s.get("shortlist_min_tokens", 5)
    picks, chosen = [], []
    for t in [*res.trends, *res.watchlist]:
        if len(picks) >= want:
            break
        if not postable(t, min_tok):
            continue
        if dedupe is not None:
            text = trend_text(t)
            m = dedupe.match(text)
            if m:
                res.suppressed.append({"key": t.key, "matched": m.draft.get("id"), "headline": t.headline[:120],
                                       "why": "repeats a recent post: " + m.describe()})
                continue
            f = dedupe.features_of(text)
            if any(dedupe.same_story(f, g)[0] for g in chosen):
                continue
            chosen.append(f)
        picks.append(t)
    res.shortlist = picks


def off_topic(item: dict, pats: list[re.Pattern]) -> bool:
    text = item_text(item).lower()
    return any(p.search(text) for p in pats)


def high_volume_authors(items: list[dict], limit: int) -> set[str]:
    """Accounts posting more than `limit` times in the window cannot be scooping."""
    counts = Counter(i["author"] for i in items if i.get("author") and not i.get("is_repost"))
    return {a for a, n in counts.items() if n > limit}


def qualifies_scoop(cl: Cluster, page: Page, cfg: dict, busy: set[str]) -> bool:
    s = cfg["selection"]
    tier_one = set(page.tier_one) - set(page.officials) - busy
    authors = {m["author"] for m in cl.members if m["platform"] == "x" and not m.get("is_repost")}
    authors |= {m["original_author"] for m in cl.members if m.get("original_author")}
    return bool(authors & tier_one) and cl.engagement >= s["scoop_min_engagement"]


def match_sent(sig: list[str], key: str, sent: list[dict], overlap: float) -> dict | None:
    sset = set(sig)
    for row in sent:
        if row["key"] == key:
            return row
    best, best_ov = None, 0.0
    for row in sent:
        other = row["signature"]
        if not other or not sset:
            continue
        ov = len(sset & other) / min(len(sset), len(other))
        if ov >= overlap and ov > best_ov:
            best, best_ov = row, ov
    return best


def developed(cl: Cluster, sc: float, prior: dict, cfg: dict) -> str:
    """Score is log-damped and saturates, so test engagement growth and new sources directly."""
    d = cfg["dedup"]
    prev_eng = prior.get("engagement") or 0
    if prev_eng > 0 and cl.engagement >= d["redevelop_engagement_ratio"] * prev_eng:
        return f"engagement {prev_eng:,} -> {cl.engagement:,}"
    if prev_eng == 0 and cl.engagement > 0 and len(cl.sources) > (prior.get("sources") or 0):
        return f"engagement 0 -> {cl.engagement:,}"
    if len(cl.sources) >= (prior.get("sources") or 0) + d["redevelop_new_sources"]:
        return f"sources {prior.get('sources')} -> {len(cl.sources)}"
    if sc >= (prior.get("score") or 0) + d["redevelop_score_delta"]:
        return f"score {prior.get('score'):.1f} -> {sc:.1f}"
    return ""


def escalation_queries(page: Page, clusters: list[Cluster], idf: IDF) -> list[str]:
    """Every configured search, plus targeted ones built from the page's strongest open threads:
    the story's own words only. The page's name is not a topic ('car class dollar example Culture
    Rush' brought a college class schedule and an investment firm's page, Oct 21); collect_web
    keeps a result only when it shares two of the story's words."""
    qs = list(page.web_searches)
    ranked = sorted(clusters, key=lambda c: -c.engagement)[:5]
    for cl in ranked:
        sig = signature(cl, idf, 4)
        if len(sig) >= 2:
            qs.append(" ".join(sig))
    seen, out = set(), []
    for q in qs:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def website_only(page: Page) -> bool:
    """A page read only from the websites its admins chose (their feeds, or site: searches)."""
    return not (page.x_handles or page.x_handles_secondary or page.subreddits or page.subreddits_rotation
                or page.web_rotation or any(not q.startswith("site:") for q in page.web_searches))


def window_hours(page: Page, cfg: dict) -> float:
    """How far back a page's stories stay candidates. A website-only page gets a few articles an
    hour and the routine decides every brief, so its pool ran dry by evening (48 candidates in the
    morning, 1 at night) while articles from earlier in the day had aged out of
    the 12-hour window unbriefed: it keeps the day's unbriefed articles."""
    c = cfg["clustering"]
    hours = c.get("website_window_hours", c["window_hours"]) if website_only(page) else c["window_hours"]
    return min(hours, c["max_age_hours"])


def select_page(page: Page, items: list[dict], cfg: dict, sent: list[dict],
                now: float | None = None, escalated: bool = False, quota: int | None = None,
                dedupe=None) -> PageResult:
    now = now or time.time()
    s, d, c = cfg["selection"], cfg["dedup"], cfg["clustering"]
    window = fresh(items, window_hours(page, cfg), now)
    pats = [re.compile(r"(?<!\w)" + re.escape(t.lower()) + r"(?!\w)") for t in page.exclude_terms]
    window = [i for i in window if not off_topic(i, pats)]
    res = PageResult(page.slug, item_count=len(window))
    clusters, idf = cluster_items(window, cfg, now)
    busy = high_volume_authors(window, s["high_volume_posts_per_window"])
    prio_rx = priority_matcher(priority_terms(page))
    boost_w = cfg["scoring"]["weights"].get("priority", 0.0)
    skip_rx, kind_rx, trade = prefilter_rules(page)
    kind_w = cfg["scoring"]["weights"].get("kind", 0.0)
    from . import lessons as L
    from .kinds import kind_for, profile_for
    from .routine import load_playbook
    playbook = load_playbook(page.slug)
    profile = profile_for(page.slug, playbook)
    learned = L.load(page.slug) if cfg["scoring"].get("learning", True) else {}
    topics = topic_rules(playbook)
    nfl = bool(re.search(r"\b(nfl|football)\b", str(playbook.get("niche") or ""), re.I))

    for cl in clusters:
        lt = lead_text(cl)
        lead = cl.lead()
        hit = next((r.pattern for r in skip_rx if r.search(lt)), None)
        if not hit and lead.get("platform") == "web":
            from .collect import section_page, service_piece
            hit = ("section page" if section_page(lead) else
                   "service piece (where to watch / cast guide / recap / explainer)" if service_piece(lead) else
                   "off-niche web result (no football in it)" if nfl and off_niche_web(cl.members) else None)
        if not hit and topics:
            t = excluded_topic(lead.get("title") or lead.get("text") or "", lead.get("text") or "", topics, prio_rx)
            hit = f"{t} (the playbook's NO list)" if t else None
        if not hit and trade:
            authors = {src.split(":", 1)[1].lower() for src in cl.sources if src.startswith("x:")}
            if authors and authors <= trade and not cl.sources - {f"x:{a}" for a in authors} \
                    and not any(r.search(lt) for r in kind_rx):
                hit = "trade outlets only (industry news)"
        if hit:
            res.suppressed.append({"key": "", "matched": "", "headline": lt[:120], "why": f"prefilter: {hit}"})
            continue
        sc = score(cl, cfg, now)
        sig = signature(cl, idf, d["signature_terms"])
        key = trend_key(sig)
        if len(cl.sources) >= s["min_sources_corroborated"]:
            route = "corroborated"
        elif qualifies_scoop(cl, page, cfg, busy):
            route = "scoop"
        elif sc >= s["watchlist_min_score"]:
            route = "watchlist"
        else:
            continue
        # The page's core subjects rank a little higher; verification routes stay on the raw score.
        boost = boost_w * min(priority_hits(cl, prio_rx), 2) if prio_rx else 0.0
        lead = cl.lead()
        kind = kind_for(lead.get("title") or "", lead.get("text") or "", profile)
        mult = L.weight(learned, kind)          # what the editors' reviews say about this kind
        # the playbook's hand-written "kinds editors approve" boost, faded out as reviews disagree
        # (full at weight 1.0, none at 0.8 or below)
        if kind_rx and any(r.search(lt) for r in kind_rx):
            boost += kind_w * min(1.0, max(0.0, (mult - 0.8) / 0.2))
        t = Trend(cl, (sc + boost) * mult, route, key, sig, kind=kind, mult=mult)
        prior = match_sent(sig, key, sent, d["fuzzy_signature_overlap"])
        if prior:
            # Posting pages never re-post a story because it grew; a genuine new development is
            # written as an explicit update (copy.update_of) and shown to the reviewer as such.
            why = developed(cl, sc, prior, cfg) if d.get("allow_redevelop", True) else ""
            if not why:
                res.suppressed.append({"key": key, "matched": prior["key"], "headline": t.headline[:120]})
                continue
            t.developed, t.matched_key = why, prior["key"]
        (res.watchlist if route == "watchlist" else res.trends).append(t)

    res.trends.sort(key=lambda t: -t.score)
    res.watchlist.sort(key=lambda t: -t.score)
    res.watchlist = res.watchlist[: s["watchlist_max_per_page"]]
    shortlist(res, cfg, quota, dedupe)
    if res.quota and len(res.trends) < res.quota:
        res.escalation = {
            "required": not escalated,
            "done": escalated,
            "reason": (f"only {len(res.trends)} verified trend(s) for a quota of {res.quota}"
                       if res.trends else "no trend cleared corroboration or the scoop route"),
            "queries": escalation_queries(page, clusters, idf),
        }
    return res


def record_briefed(store, page: str, res: PageResult, now: float | None = None) -> None:
    """Record the picks actually proposed for posting (not backups, not unpicked trends, which
    stay eligible for later runs), so later runs don't repeat them."""
    seen = set()
    for t in res.shortlist[: res.quota]:
        if t.key in seen:
            continue
        seen.add(t.key)
        store.record_sent(page, t.key, t.sig, t.cluster.engagement, len(t.cluster.sources),
                          t.score, t.headline, now)
    store.commit()


def to_json(res: PageResult) -> dict:
    def tj(t: Trend) -> dict:
        cl = t.cluster
        return {
            "key": t.key, "route": t.route, "score": round(t.score, 2), "headline": t.headline[:280],
            "kind": t.kind, "learned_weight": round(t.mult, 2),
            "sources": sorted(cl.sources), "platforms": sorted(cl.platforms),
            "engagement": cl.engagement, "items": len(cl.members), "developed": t.developed,
            "first_seen": cl.first_seen, "last_seen": cl.last_seen,
            "links": [m["url"] for m in sorted(cl.members, key=lambda m: -int(m.get("engagement") or 0))
                      if m.get("url")][:5],
        }
    return {"page": res.page, "items": res.item_count, "quota": res.quota,
            "shortlist": [tj(t) for t in res.shortlist], "trends": [tj(t) for t in res.trends],
            "watchlist": [tj(t) for t in res.watchlist], "suppressed": res.suppressed,
            "escalation": res.escalation}


__all__ = ["score", "select_page", "record_briefed", "to_json", "Trend", "PageResult", "ts"]
