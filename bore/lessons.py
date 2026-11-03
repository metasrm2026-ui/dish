"""What the editors' reviews teach, per page: approval per story kind and per card template,
turned into ranking weights and plain rules with counts. Built once a day by the hourly routine (and
in the next run after an editor writes a note) from the dashboard's review ledger, stored encrypted
in the dashboard (admins can see it, add notes, override weights or reset it), and synced into
private/lessons/<page>.json for the collector's ranking, the briefs and the Studio topic feed.

Only verdicts about the story count, older ones less (half-life 21 days): Okay +1, Not okay for a
weak or off-niche story -1 (half as much when the post had waited 10+ hours: then it was often just
stale), a post an editor selected and cleared during a review session -0.5. Not okay for the image,
a duplicate, facts or the caption, "Old news by now", clears by age, the midnight clean-up,
withdrawing a post SocialPilot refused, posts made from editors' links and Studio picks don't move
the story-kind weights (they are counted separately). A kind's approval is smoothed toward the
page's overall rate (a prior of 6 reviews), and its weight only moves once it has the equivalent
of 5 recent reviews. Kinds are the ones the ranking sees (`kind_rank`, from the lead post).

Editors' own words: every note they wrote (Not okay + note, a redo request) is quoted to the
routine verbatim, newest first, without repeats, for six weeks, with the kind and template of the
post it was about; notes that keep coming back to one theme (the photo, the caption, minor
stories...) become a rule. Grouping is by keywords, deterministic; only notes about the story itself
(its Not okay reason judged the story, and the note isn't about the photo, card or caption unless it
names the story) count toward the story themes, so "weak photo" never teaches "skip minor stories".

Photos: which candidates editors keep. Per kind of photo source, video frames, the picture several
outlets used and each check flag: Okay vs Not okay for the image on hourly posts, and in the
Studio the photo an editor picked vs the candidates shown above it. Decayed and smoothed like the
kinds; a feature moves nothing until it has 5 recent observations. It becomes a PHOTO rule when
editors reject it clearly more often than the page's other photos, and a small weight (at most
±10%) that only breaks ties when candidates are ordered, never past a check.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from pathlib import Path

from .config import PRIVATE
from .kinds import ids, kind_for, label, profile_for

HALF_LIFE_D = 21
PRIOR = 6.0
MIN_N = 5
LO, HI = 0.5, 1.3
AVOID_P, PREFER_P, STATUS_MIN_N = 0.35, 0.65, 8
REBUILD_HOURS = 20
WEIGHTS = {"approved": 1.0, "rejected": -1.0, "cleared": -0.5}
STORY_REASONS = {"", "weak story", "off-niche"}       # Not okay reasons that judge the story itself
STALE_HOURS = 10
SESSION_MIN = 45
MAX_CHARS = 55000          # a record the dashboard accepts (it refuses over 60000), with room for admin notes

# editors' notes
NOTE_VERDICTS = {"rejected", "approved", "cleared", "feedback"}   # a withdrawal is about SocialPilot
NOTE_KEEP, NOTE_BRIEF, NOTE_PER = 20, 8, 3     # kept in the record, newest in each brief, older ones per kind/template
NOTE_TPL = 6                                   # older notes quoted under template_notes, all templates together
NOTE_MIN_W = 0.25                              # two half-lives (six weeks): no longer quoted
THEME_MIN = 2                                  # notes on one theme before it is a rule
STORY_THEMES = {"timing", "minor", "niche", "taste"}       # judge the story: only from notes about the story
POST_THEMES = ("photo", "design", "text")                  # judge how the post was made
NOTE_STORY_REASONS = STORY_REASONS | {"old news by now", "redo"}
_STORY_WORD = re.compile(r"\b(?:story|stories|news|topic|subject)\b", re.I)
_W = r"(?:player|person|guy|man|woman|photo|pic|picture|image|face|page|niche|audience|team colou?rs?)"
THEMES = (   # id, what the notes are about, their words, what the routine should do about it
    ("photo", "the photo",
     r"\b(?:photos?|pictures?|pics?|images?|blurr?y|blurred|pixel\w*|grainy|crop\w*|zoom\w*|faces?|headshots?|"
     r"watermark\w*|screenshots?|video frames?|wrong (?:player|person|guy|man|woman)|not (?:him|her))\b",
     "Choose the photo more carefully: the right person, a close and sharp shot, never a screenshot or a soft frame."),
    ("design", "the card design",
     r"\b(?:layout|design|fonts?|colou?rs?|templates?|cards?|graphics?|logos?|background|cluttered|too much text|"
     r"small text|hard to read|unreadable|text (?:over|on|covers?|covering)|covers? (?:the|his|her|their) faces?)\b",
     "Keep the card clean: text away from faces, nothing cluttered, a template that fits the photo."),
    ("text", "the caption and card text",
     r"\b(?:captions?|typos?|\w*spell\w*|grammar\w*|wording|worded|headlines?|titles?|hashtags?|emojis?|tone|voice|"
     r"clickbait\w*|cringe|punctuation)\b",
     "Write the caption and card text more carefully: the page's voice, correct spelling, no clickbait."),
    ("facts", "facts",
     r"\b(?:incorrect|inaccurate|false|fake|rumou?rs?|unconfirmed|unverified|untrue|not true|misleading|facts?|"
     r"factually|verify|mistaken|mistakes?)\b|\bwrong\b(?!\s+" + _W + r"\b)",
     "Check every fact against the sources; keep unconfirmed reports hedged, and skip when the sources don't support it."),
    ("timing", "timing",
     r"(?<!year-)(?<!years )\bold\b|\b(?:stale|late|outdated|yesterday|days? ago|by now|not new|too slow|"
     r"already (?:out|known|reported|everywhere))\b",
     "Skip stories that will be old news by the time editors review them."),
    ("duplicate", "repeats",
     r"\b(?:duplicates?|dupes?|same story|repeats?|repeated|already posted|posted (?:it|this|that) "
     r"(?:already|before|earlier)|posted (?:already|before|earlier))\b",
     "Check recent_posts harder: never the same story again with a new angle."),
    ("minor", "minor stories",
     r"\b(?:minor|boring|weak|trivia\w*|nobody cares|no one cares|who cares|not interesting|uninteresting|"
     r"irrelevant|not (?:news|newsworthy|a story|big enough)|low[- ]value|filler|meh|too small)\b",
     "Skip minor stories like these unless they involve a star or a big consequence."),
    ("niche", "the page's niche",
     r"\b(?:off[- ]?(?:niche|topic|brand)|not (?:our|for (?:us|this page|the page))|wrong (?:page|niche|audience)|"
     r"audience)\b",
     "Stay inside the page's niche: skip stories its audience doesn't follow."),
    ("taste", "sensitive stories",      # judgements only: a true-crime note names victims and deaths anyway
     r"\b(?:insensitive|tasteless|disrespect\w*|offensive|(?<!-)sensitive|sensational\w*|exploitative|glorif\w*|"
     r"gory|gruesome|morbid|too (?:dark|soon|graphic|grim)|(?:bad|poor) taste)\b",
     "Handle tragedies and violence with care (ROUTINE.md 'Crime, abuse and tragedy are news'): no sensational framing."),
)
_THEME_RE = [(t, lbl, re.compile(rx, re.I), advice) for t, lbl, rx, advice in THEMES]

# photos
PHOTO_MIN_N = 5            # decayed observations before a photo feature moves anything
PHOTO_MULT = (0.8, 1.2)    # per feature
PHOTO_W = (0.9, 1.1)       # a candidate's weight: a tie-breaker, never more than ±10%
PASSED_W = 0.5             # a Studio candidate shown above the one the editor picked
PHOTO_RULE_GAP = 0.1       # Okay this far under the page's other photos (image verdicts) makes a rule
SOURCE_LABELS = {          # kinds of photo source, never sites
    "x": "a photo from an X post", "reddit": "a Reddit photo", "web": "the article's own lead photo",
    "article": "a linked article's photo", "news": "a news outlet's photo of the story", "search": "a photo-search result",
    "official": "an official headshot", "commons": "a Wikimedia Commons photo", "instagram": "an Instagram photo",
    "youtube": "a YouTube frame", "link": "the linked page's photo", "uploaded": "the editor's own upload",
}
PIPELINE_SOURCES = {"previous post's photo", "story"}   # a regeneration's labels, not kinds of source


def _ts(e: dict) -> float:
    v = e.get("decided_at") or e.get("at") or 0
    return v / 1000 if v > 1e11 else v


def _decay(e: dict, now: float) -> float:
    return 0.5 ** (max(now - _ts(e), 0) / 86400 / HALF_LIFE_D)


def themes_of(text: str, reason: str = "") -> list[str]:
    """The themes a note is about. Story themes (minor, niche, timing, taste) only when the note judges
    the story: its Not okay reason is about the story (or none, or a redo request) and it isn't about
    the photo, card or caption unless it names the story ("boring pic" is about the photo)."""
    found = [t for t, _, rx, _ in _THEME_RE if rx.search(text or "")]
    about_post = any(t in POST_THEMES for t in found) and not _STORY_WORD.search(text or "")
    if about_post or (reason or "").strip().lower() not in NOTE_STORY_REASONS:
        found = [t for t in found if t not in STORY_THEMES]
    return found


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()


def flag_key(flag: str) -> str:
    """A check flag without its details ('soft (sharpness 31)' -> 'soft'), '' for pipeline states."""
    f = re.sub(r"\s+", " ", str(flag or "")).strip().lower()
    if f.startswith("photo of "):         # 'photo of <subject> (search)', maybe cut short: never the name
        return "subject search"
    k = re.split(r"\s*[(:]", f, maxsplit=1)[0].strip()[:40]
    return "" if k.startswith(("not checked", "not text-checked")) else k


def photo_features(p) -> list[str]:
    """What a photo was, as stable keys without names or sites: its kind of source, a video frame,
    the picture several outlets used, and its check flags. `p`: a candidate dict or a media.Photo."""
    if not p:
        return []
    g = p.get if isinstance(p, dict) else (lambda k: getattr(p, k, None))
    src = str(g("source") or "").strip().lower()
    src = "uploaded" if src in ("upload", "reviewer upload") else "" if src in PIPELINE_SOURCES else src
    out = {f"source:{src}"} if src else set()
    if g("note") == "video frame":
        out.add("frame")
    try:
        if int(g("consensus") or 0) >= 2:
            out.add("consensus")
    except (TypeError, ValueError):
        pass
    out |= {f"flag:{k}" for k in map(flag_key, g("flags") or []) if k}
    return sorted(out)


def feature_label(f: str) -> str:
    kind, _, v = f.partition(":")
    if kind == "source":
        return SOURCE_LABELS.get(v, f"a '{v}' photo")
    return {"frame": "a video frame", "consensus": "the picture several outlets used"}.get(f, f"flagged '{v}'")


def _image_reason(reason: str) -> bool:
    r = (reason or "").strip().lower()
    return "image" in r or "photo" in r


def photo_weight(rec: dict | None, photo) -> float:
    """A candidate's learned weight, 0.9-1.1 (1.0 until a feature has enough reviews). Callers use it
    only to break ties between candidates that passed the same checks."""
    feats = (rec or {}).get("photos") or {}
    if not feats:
        return 1.0
    m = 1.0
    for f in photo_features(photo):
        m *= float((feats.get(f) or {}).get("mult") or 1.0)
    return round(min(max(m, PHOTO_W[0]), PHOTO_W[1]), 3)


def tie_break(photos: list, rec: dict | None, key=lambda ph: 0) -> list:
    """`photos` in their order, except that within each run of equal `key` the learned weight
    decides (stable: without lessons nothing moves)."""
    out, run = [], []
    for ph in photos:
        if run and key(ph) != key(run[0]):
            out += sorted(run, key=lambda x: -photo_weight(rec, x))
            run = []
        run.append(ph)
    return out + sorted(run, key=lambda x: -photo_weight(rec, x))


def _notes(mine: list[dict], now: float, kind_of) -> list[dict]:
    """The editors' notes for the page, newest first, a repeated note once (with how often)."""
    out = []
    for e in sorted(mine, key=lambda e: -_ts(e)):
        text = re.sub(r"\s+", " ", str(e.get("note") or "")).strip()
        w = _decay(e, now)
        if not text or e.get("verdict") not in NOTE_VERDICTS or w < NOTE_MIN_W:
            continue
        key, toks = _norm(text), set(_norm(text).split())
        dup = next((n for n in out if n["_key"] == key or
                    (toks and len(toks & n["_toks"]) / len(toks | n["_toks"]) >= 0.8)), None)
        if dup:
            dup["times"] += 1
            dup["w"] += w
            continue
        out.append({"text": text[:300], "verdict": e.get("verdict"), "reason": str(e.get("reason") or "")[:60],
                    "kind": kind_of(e), "template": e.get("template") or "", "headline": (e.get("headline") or "")[:90],
                    "at": int(_ts(e) * 1000), "times": 1, "w": w, "themes": themes_of(text, e.get("reason")),
                    "_key": key, "_toks": toks})
    return out


def _note_themes(notes: list[dict]) -> tuple[list[dict], list[str]]:
    themes, rules = [], []
    for t, lbl, _, advice in _THEME_RE:
        on = [n for n in notes if t in n["themes"]]
        count = sum(n["times"] for n in on)
        if count < THEME_MIN:
            continue
        themes.append({"id": t, "label": lbl, "count": count, "notes": [n["text"][:160] for n in on[:3]]})
        quotes = "; ".join(f'"{n["text"][:120]}"' for n in on[:2])
        rules.append(f"EDITORS' NOTES ({count} recent) about {lbl}: {advice} Their words: {quotes}.")
    return themes, rules


def _photo_lessons(mine: list[dict], now: float) -> tuple[dict, float, list[str], dict]:
    """Per photo feature: hourly Okay vs Not okay for the image, Studio picks vs candidates passed over."""
    obs = []                                   # (features, positive, weight, what)
    uploads = picks = 0
    for e in mine:
        ph, v = e.get("photo"), e.get("verdict")
        if not isinstance(ph, dict) or not ph:
            continue
        w = _decay(e, now)
        if v == "approved":
            obs.append((photo_features(ph), True, w, "approved"))
        elif v == "rejected" and _image_reason(e.get("reason")):
            obs.append((photo_features(ph), False, w, "image_rejected"))
        elif v == "studio_pick":
            picks += 1
            uploads += photo_features(ph).count("source:uploaded")
            obs.append((photo_features(ph), True, w, "picked"))
            for q in (ph.get("passed") or [])[:6]:
                obs.append((photo_features(q), False, w * PASSED_W, "passed"))
    if not obs:
        return {}, 0.0, [], {"studio_photo_picks": picks, "studio_uploads": uploads}
    tot = sum(w for _, _, w, _ in obs)
    p0 = sum(w for _, ok, w, _ in obs if ok) / tot if tot else 0.0
    hourly = [o for o in obs if o[3] in ("approved", "image_rejected")]
    th = sum(w for _, _, w, _ in hourly)
    p0h = sum(w for _, ok, w, _ in hourly if ok) / th if th else 0.0
    t = defaultdict(lambda: {"A": 0.0, "N": 0.0, "Ah": 0.0, "Nh": 0.0, "approved": 0, "image_rejected": 0,
                             "picked": 0, "passed": 0, "obs": []})
    for i, (feats, ok, w, what) in enumerate(obs):
        for f in feats:
            x = t[f]
            x[what] += 1
            x["obs"].append(i)
            x["N"] += w
            x["A"] += w if ok else 0.0
            if what in ("approved", "image_rejected"):
                x["Nh"] += w
                x["Ah"] += w if ok else 0.0
    # features seen on exactly the same posts (an X post's video frame, flagged soft) are one piece of
    # evidence: the most specific of them carries the weight and the rule, the others point to it
    same: dict = defaultdict(list)
    for f, x in t.items():
        same[tuple(x["obs"])].append(f)
    lead = {f: min(fs, key=lambda g: (_label_order(g), g)) for fs in same.values() for f in fs}
    out, hits = {}, defaultdict(list)
    for f, x in sorted(t.items(), key=lambda kv: -kv[1]["N"]):
        p = (x["A"] + PRIOR * p0) / (x["N"] + PRIOR)
        mult = (min(max(p / p0, PHOTO_MULT[0]), PHOTO_MULT[1])
                if x["N"] >= PHOTO_MIN_N and p0 > 0 and lead[f] == f else 1.0)
        n_h = x["approved"] + x["image_rejected"]
        p_h = (x["Ah"] + PRIOR * p0h) / (x["Nh"] + PRIOR) if p0h else 0.0
        why = ("hourly" if x["Nh"] >= PHOTO_MIN_N and x["image_rejected"] >= 2 and p_h <= p0h - PHOTO_RULE_GAP else
               "studio" if x["passed"] >= 4 and x["picked"] * 4 <= x["passed"] and x["N"] >= PHOTO_MIN_N else "")
        if why:
            hits[(why, tuple(x["obs"]))].append(f)
        out[f] = {"label": feature_label(f), "approved": x["approved"], "image_rejected": x["image_rejected"],
                  "picked": x["picked"], "passed": x["passed"], "n": n_h + x["picked"] + x["passed"],
                  "smoothed": round(p, 3), "mult": round(mult, 3), **({"rule": True} if why and lead[f] == f else {}),
                  **({"same_as": lead[f]} if lead[f] != f else {})}
    rules = []
    for (why, _), feats in hits.items():
        x, what = out[feats[0]], " / ".join(feature_label(f) for f in sorted(feats, key=lambda g: (_label_order(g), g)))
        rules.append(f"PHOTO: editors rejected {x['image_rejected']} of {x['approved'] + x['image_rejected']} posts whose "
                     f"photo was {what} for the image. Prefer another candidate that passes the checks when one is as good."
                     if why == "hourly" else
                     f"PHOTO: in the Studio editors passed over {what} {x['passed']} times and picked it {x['picked']} "
                     "times. Prefer another candidate when one is as good.")
    return out, round(p0, 3), rules, {"studio_photo_picks": picks, "studio_uploads": uploads}


def _label_order(f: str) -> int:
    return 0 if f == "frame" else 1 if f == "consensus" else 2 if f.startswith("flag:") else 3


def build(page: str, reviews: list[dict], playbook: dict | None = None, now: float | None = None,
          prev: dict | None = None, made_7d: int | None = None) -> dict:
    now = now or time.time()
    prev = prev or {}
    profile = profile_for(page, playbook)
    valid = set(ids(profile))
    reset = (prev.get("reset_at") or 0) / 1000
    mine = [e for e in reviews if e.get("page") == page and _ts(e) >= reset]
    restored = {e.get("draft_id") for e in mine if e.get("verdict") == "restored"}
    decided = [(e.get("by"), _ts(e)) for e in mine if e.get("verdict") in ("approved", "rejected")]
    rows, other = [], {"reasons": defaultdict(int), "image": [], "studio_picks": 0, "studio_sent": 0, "withdrawn": 0}
    fresh = defaultdict(lambda: [0, 0])               # approval by how long the post waited for review

    def kind_of(e: dict) -> str:
        k_rank = e.get("kind_rank") if e.get("kind_rank") in valid else None
        return k_rank or (e.get("kind") if e.get("kind") in valid else
                          kind_for(e.get("headline") or "", e.get("excerpt") or "", profile))

    for e in mine:
        v, reason = e.get("verdict"), str(e.get("reason") or "").strip().lower()
        if v in ("studio_pick", "studio_sent", "withdrawn"):
            other[{"studio_pick": "studio_picks", "studio_sent": "studio_sent", "withdrawn": "withdrawn"}[v]] += 1
            continue
        if v not in WEIGHTS or e.get("route") in ("studio", "manual"):
            continue
        age_h = (_ts(e) - (e.get("created_at_unix") or _ts(e))) / 3600
        if v in ("approved", "rejected") and reason in STORY_REASONS:
            b = "under 4h" if age_h < 4 else "4-10h" if age_h < STALE_HOURS else "10h+"
            fresh[b][0 if v == "approved" else 1] += 1
        if v == "rejected" and reason not in STORY_REASONS:          # about the image, facts, caption, timing
            other["reasons"][e.get("reason")] += 1
            if _image_reason(reason) and len(other["image"]) < 10:   # with the photo it was about, when known
                other["image"].append({"headline": (e.get("headline") or "")[:90], "template": e.get("template") or "",
                                       "note": e.get("note") or "",
                                       **({"photo": photo_features(e["photo"])} if e.get("photo") else {})})
            continue
        w = WEIGHTS[v]
        if v == "cleared":   # only a selected clear in a review session, of a post that wasn't stale yet
            in_session = any(by == e.get("by") and abs(t - _ts(e)) <= SESSION_MIN * 60 for by, t in decided)
            if e.get("draft_id") in restored or not in_session or age_h >= 12:
                continue
        if v == "rejected" and age_h >= STALE_HOURS:
            w *= 0.5
        rows.append({**e, "kind": kind_of(e), "w": w * _decay(e, now)})

    def tally(key):
        out = defaultdict(lambda: {"A": 0.0, "N": 0.0, "approved": 0, "rejected": 0, "cleared": 0, "picked": 0,
                                   "ex_a": [], "ex_r": []})
        for r in sorted(rows, key=lambda r: -_ts(r)):
            k = r.get(key) or ""
            if not k:
                continue
            t = out[k]
            t["N"] += abs(r["w"])
            if r["w"] > 0:
                t["A"] += r["w"]
            v = r["verdict"]
            t["approved" if v in ("approved", "studio_sent") else "rejected" if v == "rejected" else
              "cleared" if v == "cleared" else "picked"] += 1
            h = (r.get("headline") or "")[:90]
            why = " - ".join(x for x in (r.get("detail") or r.get("reason"), r.get("note")) if x)
            if v in ("approved", "studio_sent") and len(t["ex_a"]) < 3 and h:
                t["ex_a"].append(h)
            if v == "rejected" and len(t["ex_r"]) < 3 and h:
                t["ex_r"].append(h + (f" ({why})" if why else ""))
        return out

    N = sum(abs(r["w"]) for r in rows)
    p0 = (sum(r["w"] for r in rows if r["w"] > 0) / N) if N else 0.5
    overrides = prev.get("overrides") or {}

    def stats(t: dict, name: str) -> dict:
        n_raw = t["approved"] + t["rejected"]
        p = (t["A"] + PRIOR * p0) / (t["N"] + PRIOR)
        # gates on the decayed amount of evidence: old verdicts alone don't keep a weight active
        mult = min(max(p / p0, LO), HI) if (t["N"] >= MIN_N and p0 > 0) else 1.0
        status = ("avoid" if p <= AVOID_P and t["N"] >= STATUS_MIN_N else
                  "prefer" if p >= PREFER_P and t["N"] >= STATUS_MIN_N else "mixed")
        if name in overrides:
            mult = float(overrides[name])
        return {"approved": t["approved"], "rejected": t["rejected"], "cleared": t["cleared"], "picked": t["picked"],
                "n": n_raw, "rate": round(t["approved"] / n_raw, 3) if n_raw else None, "smoothed": round(p, 3),
                "mult": round(mult, 2), "status": status, "examples": {"approved": t["ex_a"], "rejected": t["ex_r"]}}

    kinds = {k: {"label": label(k, profile), **stats(t, k)} for k, t in tally("kind").items()}
    templates = {k: stats(t, f"template:{k}") for k, t in tally("template").items()
                 if k and t["approved"] + t["rejected"] > 0}
    rules = []
    for k, v in sorted(kinds.items(), key=lambda kv: kv[1]["smoothed"]):
        if v["n"] < MIN_N:
            continue
        line = f"{v['label']}: editors approved {v['approved']} of {v['n']}"
        if v["status"] == "avoid":
            rules.append(f"AVOID {line}. Skip these unless the story clearly has what the approved ones had."
                         + (f" Approved: {'; '.join(v['examples']['approved'])}." if v["examples"]["approved"] else "")
                         + (f" Not okay: {'; '.join(v['examples']['rejected'])}." if v["examples"]["rejected"] else ""))
        elif v["status"] == "prefer":
            rules.append(f"PREFER {line}.")
        elif v["smoothed"] < p0 - 0.05:
            rules.append(f"BE PICKY: {line} (page average {round(p0 * 100)}%). Compare with both example lists."
                         + (f" Approved: {'; '.join(v['examples']['approved'])}." if v["examples"]["approved"] else "")
                         + (f" Not okay: {'; '.join(v['examples']['rejected'])}." if v["examples"]["rejected"] else ""))
    for t, v in templates.items():
        if v["status"] == "avoid":
            rules.append(f"AVOID the '{t}' card template: editors approved {v['approved']} of {v['n']}.")
    notes = _notes(mine, now, kind_of)
    themes, theme_rules = _note_themes(notes)
    photos, photo_p0, photo_rules, picks = _photo_lessons(mine, now)
    rules += theme_rules + photo_rules
    recent = sorted((r for r in rows if now - _ts(r) < 7 * 86400 and r["verdict"] in ("approved", "rejected")),
                    key=lambda r: -_ts(r))
    reviewed_7d = sum(1 for r in rows if now - _ts(r) < 7 * 86400 and r["verdict"] in ("approved", "rejected", "cleared"))
    hist = list(prev.get("history") or [])[-29:] + [{"at": int(now * 1000), "p0": round(p0, 3), "n": len(rows)}]
    rec = {
        "page": page, "built_at": int(now * 1000), "ledger_at": int(now * 1000),   # (the dashboard resets built_at)
        "reviews_used": len(rows), "p0": round(p0, 3),
        "coverage_7d": round(reviewed_7d / made_7d, 3) if made_7d else None,
        "kinds": kinds, "templates": templates, "rules": rules,
        "avoid": [k for k, v in kinds.items() if v["status"] == "avoid"],
        "freshness": {b: {"approved": a, "rejected": r, "rate": round(a / (a + r), 3) if a + r else None}
                      for b, (a, r) in sorted(fresh.items())},
        "other": {"not_okay_reasons": dict(other["reasons"]), "image_rejections": other["image"],
                  "studio_picks": other["studio_picks"], "studio_sent": other["studio_sent"],
                  "withdrawn": other["withdrawn"], **picks},
        # the editors' own words, newest first, each with the kind and template of its post
        "notes": [{**{k: v for k, v in n.items() if not k.startswith("_")}, "w": round(n["w"], 3),
                   "kind_label": label(n["kind"], profile)} for n in notes[:NOTE_KEEP]],
        "note_themes": themes,
        "photos": photos, "photo_p0": photo_p0, "photo_rules": photo_rules,
        "recent": {
            "approved": [r.get("headline", "")[:90] for r in recent if r["verdict"] == "approved"][:10],
            "rejected": [(r.get("headline", "")[:90] + (f" ({r.get('detail') or r.get('reason')}{' - ' + r['note'] if r.get('note') else ''})"
                          if (r.get("detail") or r.get("reason")) else "")) for r in recent if r["verdict"] == "rejected"][:10],
        },
        "history": hist,
    }
    while rec["notes"] and len(json.dumps(rec, ensure_ascii=False)) > MAX_CHARS:   # the dashboard keeps 60k
        rec["notes"].pop()
    return rec


def path(slug: str) -> Path:
    return PRIVATE / "lessons" / f"{slug}.json"


def load(slug: str) -> dict:
    f = path(slug)
    try:
        return json.loads(f.read_text()) if f.exists() else {}
    except (OSError, ValueError):
        return {}


def save(slug: str, rec: dict | None) -> None:
    f = path(slug)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rec or {}, ensure_ascii=False))


def weight(rec: dict, kind: str) -> float:
    """The ranking multiplier for a kind (an admin's override wins)."""
    if not rec:
        return 1.0
    o = (rec.get("overrides") or {}).get(kind)
    if o is not None:
        return float(o)
    return float(((rec.get("kinds") or {}).get(kind) or {}).get("mult") or 1.0)


def for_brief(rec: dict, kind: str) -> dict:
    """The compact part of the lessons a brief carries."""
    if not rec:
        return {}
    k = (rec.get("kinds") or {}).get(kind) or {}
    notes = rec.get("notes") or []

    def said(n: dict) -> str:        # the note as the editor wrote it, with what it was about
        about = " · ".join(x for x in (n.get("reason") or ("redo request" if n.get("verdict") == "feedback" else ""),
                                       n.get("kind_label") or n.get("kind"),
                                       f"'{n['template']}' card" if n.get("template") else "", n.get("headline")) if x)
        return f"\"{n['text']}\"" + (f" ({about})" if about else "") + (f" x{n['times']}" if n.get("times", 1) > 1 else "")
    # each note once: the newest in `notes` (each says its kind and template), older ones on this
    # story's kind and on card templates after them, so a brief quotes at most 8 + 3 + 6
    older = notes[NOTE_BRIEF:]
    kind_notes = [n for n in older if n.get("kind") == kind][:NOTE_PER]
    by_tpl: dict = {}
    for n in (n for n in older if n.get("template") and n not in kind_notes):
        if sum(map(len, by_tpl.values())) >= NOTE_TPL:
            break
        if len(by_tpl.setdefault(n["template"], [])) < NOTE_PER:
            by_tpl[n["template"]].append(said(n))
    return {"rules": rec.get("rules", []), "admin_notes": rec.get("admin_notes", ""), "avoid_kinds": rec.get("avoid", []),
            "notes": [said(n) for n in notes[:NOTE_BRIEF]],
            "this_kind": {"kind": kind, "label": k.get("label", kind), "approved": k.get("approved", 0),
                          "n": k.get("n", 0), "status": k.get("status", "mixed"), "examples": k.get("examples", {}),
                          "notes": [said(n) for n in kind_notes]},
            "template_notes": by_tpl,
            "avoid_templates": [t for t, v in (rec.get("templates") or {}).items() if v.get("status") == "avoid"],
            "recent": rec.get("recent", {})}


def stale(rec: dict | None, now: float | None = None) -> bool:
    now = now or time.time()
    return not rec or not rec.get("built_at") or now - rec["built_at"] / 1000 > REBUILD_HOURS * 3600


def new_notes(rec: dict | None, latest: list[dict], page: str) -> bool:
    """An editor wrote a note for the page after the lessons were built (a redo request reaches no
    brief otherwise): rebuild now rather than within the day."""
    since = ((rec or {}).get("ledger_at") or (rec or {}).get("built_at") or 0) / 1000
    return any(e.get("page") == page and e.get("verdict") in NOTE_VERDICTS and str(e.get("note") or "").strip()
               and _ts(e) > since for e in latest)


def refresh(dash, pages, now: float | None = None, force: bool = False, quiet: bool = False) -> dict:
    """Bring each page's lessons up to date (rebuilt once a day from the review ledger, or sooner when
    editors wrote a note since; saved to the dashboard) and keep a local copy for ranking. Returns {slug: record}."""
    from .routine import load_playbook
    now = now or time.time()
    out, reviews, made, latest = {}, None, {}, None
    for p in pages:
        prev = dash.lessons(p.slug) or {}
        rec = prev
        due = force or stale(prev, now)
        if not due:                                   # editors' notes since the last build: within the hour
            if latest is None:
                try:
                    latest = dash.reviews(2)
                except Exception:
                    latest = []
            due = new_notes(prev, latest, p.slug)
        if due:
            if reviews is None:
                reviews = dash.reviews(90)          # older verdicts weigh under 5% at a 21-day half-life
                made = dash.made_since(now - 7 * 86400)
            rec = build(p.slug, reviews, load_playbook(p.slug), now, prev, made.get(p.slug))
            try:
                dash.save_lessons(p.slug, rec)
            except Exception as e:                    # keep it locally so the next run doesn't redo it
                print(f"LESSONS {p.slug}: not saved to the dashboard ({e})")
            rec = {**rec, **{k: prev[k] for k in ("admin_notes", "overrides", "reset_at") if k in prev}}
            if not quiet:
                print(f"LESSONS {p.slug}: rebuilt from {rec['reviews_used']} reviews, approval {round(rec['p0'] * 100)}%"
                      + (f", avoid {', '.join(rec['avoid'])}" if rec.get("avoid") else "")
                      + (f"; {len(rec.get('notes') or [])} editors' note(s)" if rec.get("notes") else "")
                      + (f", note themes: {', '.join(t['label'] for t in rec['note_themes'])}" if rec.get("note_themes") else "")
                      + (f"; {len(rec['photo_rules'])} photo rule(s)" if rec.get("photo_rules") else "")
                      + (f"; only {round(rec['coverage_7d'] * 100)}% of last week's posts were reviewed"
                         if rec.get("coverage_7d") is not None and rec["coverage_7d"] < 0.3 else ""))
        save(p.slug, rec)
        out[p.slug] = rec
    return out


__all__ = ["build", "load", "save", "weight", "for_brief", "stale", "new_notes", "refresh", "WEIGHTS", "photo_features",
           "photo_weight", "tie_break", "themes_of", "flag_key"]
