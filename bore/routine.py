"""The hourly routine's file-based workflow. Deterministic steps run here; the judgement steps
(is it worth posting, caption, card copy, checking the finished image) are done by the Claude
routine session itself, following ROUTINE.md, by reading and writing the files below.

work/<run>/<id>/
  brief.json      story, sources, photo candidates, page playbook, quota        (prepare)
  photos/*.jpg    downloaded candidate photos that passed the resolution floor  (prepare)
  copy.json       decision + caption + card text                                (Claude)
  card.jpg        the infographic                                               (render / fallback)
  verdict.json    image check                                                   (Claude)
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import ROOT, Page

WORK = ROOT / "work"
CARD_W, CARD_H = 1080, 1350


def load_playbook(slug: str) -> dict:
    from .config import PRIVATE
    p = PRIVATE / "playbooks" / f"{slug}.yaml"
    if not p.exists():
        p = ROOT / "config" / "playbooks" / f"{slug}.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def new_run_dir() -> Path:
    d = WORK / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    d.mkdir(parents=True, exist_ok=True)
    return d


def latest_run_dir() -> Path | None:
    runs = sorted(p for p in WORK.glob("*") if p.is_dir())
    return runs[-1] if runs else None


def post_id(page: str, key: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    return f"{page}-{stamp}-{re.sub(r'[^a-z0-9]+', '-', key.lower())[:40].strip('-')}"


# ------------------------------------------------------------------------------ prepare
def admin_sources(page: Page, members: list[dict]) -> list[str]:
    """The websites among the story's sources that the page's admins chose (trusted for this page)."""
    from .textutil import domain
    chosen = {w.split("/")[0] for w in page.websites or []} | {domain(f) for f in page.feeds or []}
    return sorted({m["author"] for m in members if m.get("platform") == "web" and m.get("author") in chosen})


def _full_articles(posts: list[dict], limit: int = 2, links: int = 2) -> None:
    """A feed article carries a one-line summary: read the article itself for the facts (`limit`
    of them). A Reddit link post is only a title: the article it shares is added as a post of its
    own, credited to its site (Oct 14-21: 49 Culture Rush skips were 'single uncredited Reddit
    post, no outlet'), once per article and only when the posts lack it (`links` of them)."""
    from .linkpost import read_article
    from .textutil import domain
    have, shared = {p.get("url") for p in posts if p["platform"] == "web"}, []
    for p in posts:
        if p["platform"] == "reddit" and p.get("link") and p["link"] not in have:
            have.add(p["link"])
            shared.append(p)
    for post in shared[:links]:
        try:
            a = read_article(post["link"])
        except Exception:
            continue
        text = "\n".join(x for x in (a.get("title"), a.get("text")) if x).strip()
        if text:
            posts.append({"platform": "web", "author": domain(post["link"]), "subreddit": None, "text": text[:3000],
                          "url": post["link"], "engagement": 0, "hours_ago": post.get("hours_ago", 0),
                          "shared_by": post.get("url")})
    for post in [p for p in posts if p["platform"] == "web" and p.get("url") and not p.get("shared_by")
                 and len(p["text"]) < 400][:limit]:              # the feed's own articles (a shared one is read)
        try:
            text = (read_article(post["url"]).get("text") or "").strip()
        except Exception:
            continue
        if len(text) > len(post["text"]):
            post["text"] = (post["text"] + "\n" + text)[:3000]


def write_brief(run: Path, page: Page, trend, rank: int, is_backup: bool, photos, cfg: dict,
                recent: list[dict] | None = None) -> Path:
    cl = trend.cluster
    pid = post_id(page.slug, trend.key)
    d = run / pid
    (d / "photos").mkdir(parents=True, exist_ok=True)
    from .faceid import fields as id_fields
    photo_meta = []
    for i, ph in enumerate(photos):
        ext = "png" if (ph.data or b"")[:4] == b"\x89PNG" else "jpg"
        fn = f"photos/{i}.{ext}"
        (d / fn).write_bytes(ph.data)
        # id_match: whose face (bore/faceid.py): 'match' = the subject, 'mismatch' = may not be them
        photo_meta.append({"index": i, "file": fn, "url": ph.url, "source": ph.source, "credit": ph.credit,
                           "size": f"{ph.w}x{ph.h}",
                           **{k: v for k, v in (("note", ph.note), ("via", ph.via), ("post_url", ph.post_url),
                                                ("flags", ph.flags), ("consensus", ph.consensus if ph.consensus > 1 else 0),
                                                *id_fields(ph).items())
                              if v}})
    members = sorted(cl.members, key=lambda m: -int(m.get("engagement") or 0))
    posts = [{"platform": m["platform"], "author": m.get("author"), "subreddit": m.get("subreddit"),
              "text": ((m.get("title") or "") + " " + (m.get("text") or "")).strip()[:900],
              "url": m.get("url"), "engagement": int(m.get("engagement") or 0),
              "hours_ago": round((time.time() - (m.get("created_at") or m.get("collected_at") or time.time())) / 3600, 1),
              **({"link": (m.get("metrics") or {})["link"]} if (m.get("metrics") or {}).get("link") else {})}
             for m in members[:10]]
    _full_articles(posts)
    shared = sorted({f"web:{p['author']}" for p in posts if p.get("shared_by")})     # outlets the Reddit posts link to
    brief = {
        "id": pid, "page": page.slug, "page_name": page.name, "rank": rank, "backup": is_backup,
        "verification": {"route": trend.route, "sources": sorted(set(cl.sources) | set(shared)),
                         "platforms": sorted(cl.platforms | ({"web"} if shared else set())),
                         "engagement": cl.engagement, "developed": trend.developed,
                         "admin_sources": admin_sources(page, cl.members)},
        "headline_hint": trend.headline[:280], "posts": posts, "photos": photo_meta,
        "key": trend.key, "signature": list(trend.sig),
        # what this page already made in the last days: never write the same story again
        "recent_posts": [{"id": r.get("id"), "status": r.get("status"), "headline": r.get("headline")}
                         for r in (recent or [])][:60],
        "playbook": load_playbook(page.slug),
        "copy_schema": {**COPY_SCHEMA_DOC, **UPDATE_SCHEMA},
    }
    # what the editors' reviews teach about this kind of story (bore/lessons.py)
    from . import lessons as L
    from .kinds import ids, profile_for
    learned = L.load(page.slug)
    # the page's own templates its editors approved (template specs, private/templates/<page>/): ROUTINE.md 2
    from .studio import TEMPLATE_COPY_DOC, approved_templates
    if (tpl := approved_templates(page.slug)):
        brief["templates"] = tpl
        brief["copy_schema"].update(TEMPLATE_COPY_DOC)
    # the page's reference documents its admins uploaded (studio sync, private/docs/<page>/<type>.txt):
    # the caption tone and the visual references, read as Studio reads them (ROUTINE.md 2)
    if (docs := page_docs(page.slug)):
        brief["docs"] = docs
    brief["kind"] = trend.kind or "other"
    brief["kinds"] = ids(profile_for(page.slug, brief["playbook"]))
    if learned:
        brief["lessons"] = L.for_brief(learned, brief["kind"])
    (d / "brief.json").write_text(json.dumps(brief, indent=1, ensure_ascii=False))
    return d


def regen_id(old_id: str) -> str:
    """New id for a regenerated post: same page and slug, a fresh timestamp."""
    m = re.match(r"^(.*?)-(\d{12})-(.*)$", old_id)
    page, slug = (m.group(1), m.group(3)) if m else (old_id.split("-")[0], old_id)
    slug = re.sub(r"-v\d+$", "", slug)
    return f"{page}-{datetime.now(timezone.utc):%Y%m%d%H%M}-{slug}"[:90] + "-v2"


def write_regen_brief(run: Path, req: dict, cfg: dict) -> Path:
    """Brief for rebuilding a post a reviewer sent back with feedback. Carries the original
    story (sources, verification), the previous copy and card, and the reviewer's image."""
    import requests
    from .config import load_pages
    snap = req.get("brief") or {}
    pid = regen_id(req["id"])
    d = run / pid
    (d / "photos").mkdir(parents=True, exist_ok=True)

    def fetch(url: str) -> bytes | None:
        try:
            r = requests.get(url, timeout=45, headers={"User-Agent": "Mozilla/5.0"})
            return r.content if r.ok and len(r.content) > 2000 else None
        except Exception:
            return None

    photos, seen = [], set()

    def add_photo(url: str, source: str, credit: str = "") -> int | None:
        if not url or url in seen:
            return None
        seen.add(url)
        data = fetch(url)
        if not data:
            return None
        from PIL import Image
        import io
        try:
            w, h = Image.open(io.BytesIO(data)).size
        except Exception:
            return None
        i = len(photos)
        ext = "png" if data[:4] == b"\x89PNG" else "jpg"
        (d / f"photos/{i}.{ext}").write_bytes(data)
        photos.append({"index": i, "file": f"photos/{i}.{ext}", "url": url, "source": source,
                       "credit": credit, "size": f"{w}x{h}"})
        return i

    prev_copy = req.get("copy") or {}
    as_is_photos = []
    if prev_copy.get("as_is"):      # posted "as is": its images first, in the order they were posted
        snap_photos = snap.get("photos") or []
        for i in as_is_order({"photos": snap_photos}, prev_copy):
            j = add_photo(snap_photos[i].get("url", ""), snap_photos[i].get("source", "story"), snap_photos[i].get("credit", ""))
            if j is not None:
                as_is_photos.append(j)
    ref_index = add_photo(req.get("reference_image_url", ""), "reviewer upload")
    add_photo(req.get("source_photo", ""), "previous post's photo")
    for ph in snap.get("photos") or []:
        add_photo(ph.get("url", ""), ph.get("source", "story"), ph.get("credit", ""))
    if req.get("image_url") and (prev := fetch(req["image_url"])):
        (d / "previous_card.jpg").write_bytes(prev)

    posts = snap.get("posts") or []
    legacy = not posts
    if legacy:  # drafts made before briefs were stored: the approved-by-lint copy is the fact base
        posts = [{"platform": "previous-post", "author": "previous lint-checked copy",
                  "text": f"{req.get('headline', '')}\n{req.get('caption', '')}"[:4000], "url": ""}]
        posts += [{"platform": "web", "author": "", "text": "", "url": u} for u in req.get("sources") or []]
    page = next((p for p in load_pages() if p.slug == req["page"]), None)
    brief = {
        "id": pid, "page": req["page"], "page_name": req.get("page_name") or (page.name if page else req["page"]),
        "rank": 0, "backup": False,
        "verification": req.get("verification") or snap.get("verification") or {},
        "headline_hint": snap.get("headline_hint") or req.get("headline", ""),
        "posts": posts, "photos": photos,
        "playbook": load_playbook(req["page"]), "copy_schema": {**COPY_SCHEMA_DOC, **REGEN_SCHEMA_EXTRA},
        "regeneration": {
            "replaces": req["id"], "feedback": req.get("feedback", ""),
            "reviewer_caption": req.get("reviewer_caption", ""),
            "reference_photo_index": ref_index,
            "previous_card": "previous_card.jpg" if (d / "previous_card.jpg").exists() else "",
            "previous_image_url": req.get("image_url", ""),
            "previous_copy": req.get("copy") or {"caption": req.get("caption", ""), "headline": req.get("headline", "")},
            "legacy_sources": legacy,
            **({"as_is": True, "as_is_photos": as_is_photos, "link_note": req.get("link_note", "")} if prev_copy.get("as_is") else {}),
        },
    }
    (d / "brief.json").write_text(json.dumps(brief, indent=1, ensure_ascii=False))
    return d


UPDATE_SCHEMA = {
    "update_of": "only for a genuine NEW development of a story in recent_posts (e.g. 'questionable' -> "
                 "'ruled OUT'): that post's id. Otherwise leave it out; upload refuses repeats",
    "what_is_new": "with update_of: the new fact in one line (shown to the reviewer)",
}


REGEN_SCHEMA_EXTRA = {
    "changes": "regeneration only: one line telling the reviewer what you changed in response to the feedback",
    "design_notes": "regeneration only: the feedback's visual requests restated as instructions for the image "
                    "model (layout, crop, colours, photo treatment); never new card text; empty if none",
    "style_photo_index": "regeneration only: index of the editor's uploaded image when it is a layout/style "
                         "example rather than the photo to use, else -1",
}


COPY_SCHEMA_DOC = {
    "decision": "post | skip",
    "niche_fit": "0-10: how squarely this sits in the page's niche and would perform with its audience",
    "reason": "one sentence",
    "template": "one of the playbook card.templates",
    "headline_lines": ["2-4 short ALL-CAPS lines (quote template: the quote split into 2-3 lines)"],
    "highlight": "word/phrase to colour, or empty",
    "label": "stamp (REPORT/BREAKING) / quote speaker ('Kevin Stefanski:') / casefile note, or empty",
    "dek": "one-line sentence-case subtitle, or empty",
    "stat_label": "optional", "stat_value": "optional", "footer": "optional", "footer_sub": "optional",
    "team": "NFL team nickname for colours (huddle), or empty",
    "photo_index": "index into photos[] of the subject photo, or -1",
    "as_is": "editor's link only: true when the editor asked for the images as they are (no edits/branding/card)",
    "photos": "with as_is: indexes into photos[] to post, in order (default: photo_index, else all)",
    "photo_search": "if no usable photo of the subject: a search query for one (e.g. 'Kevin Stefanski Falcons'), else empty",
    "people_in_photo": ["names of the real people visible in the chosen photo (the card may show only these)"],
    "caption": "the full Facebook caption, hashtags on the last line",
    "kind": "the story kind: brief.kind, or the right one of brief.kinds if that is wrong",
    "lesson_override": "only when posting a kind or template the lessons say to AVOID: what makes this one different (editors see it)",
    "note_to_editor": "an editor's request: one line on anything you left out or could not do (a victim's name, an image), else empty",
}


# ------------------------------------------------------------------------------- render
def team_line(team: str) -> str:
    if not team:
        return ""
    from .cards import team_colors
    c1, c2, _ = team_colors(team)
    return f"Team: {team.title()} - use their colours {c1} and {c2}."


def krea_prompt(brief: dict, copy: dict, feedback: str = "") -> str:
    pb = load_playbook(brief["page"]) or brief.get("playbook") or {}
    card = pb.get("card") or {}
    lines = [l for l in copy.get("headline_lines", []) if l.strip()]
    tpl = copy.get("template") or card.get("default_template", "")
    elements = []
    if copy.get("label"):
        elements.append(("stamp/label", copy["label"]))
    if tpl == "quote":
        elements += [(f"quote line {i + 1} (in quotation marks)", l) for i, l in enumerate(lines)]
    else:
        elements += [(f"headline line {i + 1}", l) for i, l in enumerate(lines)]
    for k, name in (("dek", "subtitle"), ("stat_label", "stat label"), ("stat_value", "stat value"),
                    ("footer", "footer line 1"), ("footer_sub", "footer line 2")):
        if copy.get(k):
            elements.append((name, copy[k]))
    if brief["page"] == "culture-rush":
        elements.append(("page logo", "CULTURE RUSH"))
    text_spec = [f"The card contains EXACTLY these {len(elements)} text elements, spelled exactly, and NO other text:"]
    text_spec += [f"{i + 1}. {name}: \"{text}\"" for i, (name, text) in enumerate(elements)]
    text_spec.append("Do NOT add any other words, numbers, stats, badges, stamps, subtitles, dates, captions, "
                     "handles or watermarks. Do not repeat any element. Every element appears exactly once.")
    text_spec.append("FACES MUST BE 100% CLEAR: no text, stamp, label, banner, logo or graphic may touch or overlap "
                     "any person's face or head (including hair and helmet). Place all text in areas away from faces; "
                     "if needed, position or scale the photo so every face sits in a clear area.")
    people = copy.get("people_in_photo") or []
    who = (f"The subject photo shows {', '.join(people)}. Keep them exactly as photographed (same face, hair, "
           "clothing) - do not redraw, age, restyle or replace any person.") if people else (
           "The reference photo shows no person the card needs: do NOT add, invent or draw any person or face.")
    style = card.get("style", "")
    tpl_style = style.get(tpl) or next(iter(style.values()), "") if isinstance(style, dict) else str(style)
    brand = (f'The page logo reads "{pb.get("name", brief["page_name"]).upper()}".' if brief["page"] == "culture-rush"
             else "")
    return "\n".join(filter(None, [
        f'Design a {CARD_W}x{CARD_H} (4:5) Facebook news graphic for the page "{pb.get("name") or brief["page_name"]}", matching the page\'s '
        f"own post style{' shown in the style reference images' if pb.get('style_refs') else ''}.",
        f"Layout: {tpl_style}",
        who,
        team_line(copy.get("team", "")),
        brand,
        *text_spec,
        "No watermarks. No league, team or brand logos" + (" other than the page's own logo." if brand else "."),
        "Sharp, high-contrast, professional sports/entertainment media design.",
        (f"Design changes requested by the page's editor (apply them; they never add text beyond the list "
         f"above): {copy['design_notes']}" if copy.get("design_notes") else ""),
        ("The last reference image is the editor's layout/style example: follow its look."
         if isinstance(copy.get("style_photo_index"), int) and copy["style_photo_index"] >= 0 else ""),
        f"Fix these problems from the previous attempt: {feedback}" if feedback else "",
    ]))


NO_PEOPLE = ("Background artwork only: absolutely no people, no faces, no bodies, no hands, no silhouettes, "
             "no text, no letters, no numbers, no logos, no watermarks.")


def art_prompt(brief: dict, copy: dict, feedback: str = "") -> str:
    """Prompt for Krea art mode: the template's backdrop in the page's style. A template spec's art
    layer: the artwork its spec describes (art.prompt, from the templates document) in the page's
    colours, never another template's backdrop."""
    pb = load_playbook(brief["page"]) or {}
    card = pb.get("card") or {}
    tpl = copy.get("template") or card.get("default_template", "")
    base = (card.get("art") or {}).get(tpl) or next(iter((card.get("art") or {}).values()), "Dark textured background")
    if copy.get("template_spec"):
        from .studio import studio_meta
        from .tspec import find
        art = find(copy["template_spec"], brief).get("art") or {}
        theme = (brief.get("studio") or {}).get("theme") or (studio_meta().get(brief["page"]) or {}).get("theme") or {}
        colours = " and ".join(theme[k] for k in ("bg", "accent") if theme.get(k))
        base = (art.get("prompt") or "Abstract textured background artwork").strip().rstrip(".") + (
            f", in the page's colours {colours}" if colours else "")
    if "{c1}" in base or "{c2}" in base:
        from .cards import team_colors
        c1, c2, _ = team_colors(copy.get("team", ""))
        base = base.replace("{c1}", f"team colour {c1}").replace("{c2}", f"accent colour {c2}")
    parts = [base.strip().rstrip(".") + "."]
    if copy.get("art_mood"):
        parts.append(f"Mood for this story: {copy['art_mood']}.")
    if copy.get("design_notes"):
        parts.append(f"Editor's notes: {copy['design_notes']}.")
    if feedback:
        parts.append(f"Avoid what went wrong last time: {feedback}.")
    parts.append(NO_PEOPLE)
    return " ".join(parts)


def page_docs(slug: str) -> dict:
    """{type: {file, version}} for the page's synced tone and visual-reference documents that have
    text (the templates document is used through its approved templates, not read whole)."""
    from .studio import studio_meta
    out = {}
    for t, d in ((studio_meta().get(slug) or {}).get("docs") or {}).items():
        f = ROOT / str(d.get("file") or "")
        if t in ("tone", "visual") and d.get("file") and f.is_file() and f.read_text().strip():
            out[t] = {"file": d["file"], "version": d.get("version")}
    return out


def krea_request(d: Path, cfg: dict, upload_url: str | None, feedback: str = "") -> dict:
    """Build the Krea generation request for the routine session to send through the Krea
    connector (billed to the plan's monthly compute units, not the separate API balance).
    The chosen photo is uploaded to Krea's asset storage first when an upload URL is given."""
    import requests
    brief = json.loads((d / "brief.json").read_text())
    copy = json.loads((d / "copy.json").read_text())
    kc = cfg["krea"]
    if kc.get("mode", "full") == "premium":
        return _premium_request(d, cfg, brief, copy, upload_url, feedback)
    if kc.get("mode", "full") == "art":
        req = {"model": kc["model"], "input": {"prompt": art_prompt(brief, copy, feedback), **kc["art_input"]}}
        si = copy.get("style_photo_index", -1)   # an editor's style example (regenerations; +~2.5 credits)
        if isinstance(si, int) and 0 <= si < len(brief["photos"]) and brief["photos"][si].get("url"):
            req["input"]["image_style_references"] = [{"url": brief["photos"][si]["url"], "strength": 0.6}]
        (d / "krea_request.json").write_text(json.dumps(req, indent=1, ensure_ascii=False))
        return req
    pb = load_playbook(brief["page"]) or brief.get("playbook") or {}
    urls = []

    def photo_url(idx) -> str | None:
        if not (isinstance(idx, int) and 0 <= idx < len(brief["photos"])):
            return None
        ph = brief["photos"][idx]
        url = ph["url"]
        if upload_url:
            fp = d / ph["file"]
            r = requests.post(upload_url, files={"file": (fp.name, fp.read_bytes(), "image/jpeg")}, timeout=120)
            r.raise_for_status()
            url = r.text.strip().strip('"')
            if url.startswith("{"):
                url = json.loads(url).get("url", url)
        return url

    if (u := photo_url(copy.get("photo_index", -1))):
        urls.append(u)
    urls += list(pb.get("style_refs") or [])[:3]
    if (u := photo_url(copy.get("style_photo_index", -1))):  # editor's example goes last (see prompt)
        urls.append(u)
    req = {"model": cfg["krea"]["model"],
           "input": {"prompt": krea_prompt(brief, copy, feedback), "aspect_ratio": cfg["krea"]["aspect_ratio"],
                     **({"image_urls": urls[:14]} if urls else {})}}
    (d / "krea_request.json").write_text(json.dumps(req, indent=1, ensure_ascii=False))
    return req


PREMIUM_STYLE = ("a premium, high-end sports and entertainment media poster, the quality of the biggest pages' "
                 "best posts: the subject cut out cleanly and placed into a dramatic scene with depth (stadium or "
                 "studio lights, smoke, light rays, sparks or bokeh in the colours below), rim light and glow on the "
                 "subject, bold condensed display type with 3D, metallic or glowing treatment on the key words and "
                 "numbers, strong contrast and colour grading. Big headline words and numbers may sit partly behind "
                 "the subject's body, never over a face.")


def photo_credit(brief: dict, copy: dict) -> str:
    """'Photo: <source>' for the chosen photo: a regenerated card drops the photo's own credit or
    watermark, so the card names where the photo came from (the site, agency or account)."""
    from urllib.parse import urlsplit
    idx = copy.get("photo_index", -1)
    ph = brief["photos"][idx] if isinstance(idx, int) and 0 <= idx < len(brief.get("photos") or []) else {}
    who = str(ph.get("credit") or "").strip().lstrip("@") or urlsplit(str(ph.get("url") or "")).netloc
    who = re.sub(r"^(www|i|images?|cdn|media|static|s|pbs|platform|sportshub)\d*\.", "", who, flags=re.I)
    return f"Photo: {who[:40]}" if who else ""


def premium_prompt(brief: dict, copy: dict, credit: str, layout: bool, feedback: str = "", example: bool = False) -> str:
    """The whole card from the real photo (first image) in the layout of the page's own card (second
    image, the template renderer's card with the exact text), restyled to a premium poster."""
    from .guard import card_strings
    pb = load_playbook(brief["page"]) or brief.get("playbook") or {}
    texts = [t for t in card_strings(copy) if t.strip()] + ([credit] if credit else [])
    people = copy.get("people_in_photo") or []
    who = (f"The FIRST image shows {', '.join(people)}: make them the hero. Keep each face, likeness, hair, "
           "skin, uniform and number exactly as photographed: never redraw, age, restyle, swap or invent a "
           "person.") if people else ("The FIRST image is the story's photo: make it the hero. Do NOT add, invent "
                                     "or draw any person or face that is not in it.")
    lines = [
        f'Design a 1080x1350 (4:5) Facebook post for the news page "{pb.get("name") or brief["page_name"]}" as {PREMIUM_STYLE}',
        who,
        ("The SECOND image is the page's own card for this post: keep its layout (where the photo, the headline, "
         "the numbers, banners and labels sit, their order, which words are biggest and their colour roles) and "
         "make it look premium." if layout else ""),
        team_line(copy.get("team", "")),
        (f"Mood for this story's scene: {copy['art_mood']}." if copy.get("art_mood") else ""),
        f"The post contains EXACTLY these {len(texts)} text elements, each spelled exactly as written, and NO other text:",
        *[f'{i + 1}. "{t}"' for i, t in enumerate(texts)],
        (f'"{credit}" is a small, plain credit line in a bottom corner.' if credit else ""),
        "Do NOT add any other words, numbers, stats, dates, handles, captions or watermarks; every element appears "
        "exactly once. No league, team or brand logos.",
        "Every face stays 100% clear: no text, number, banner or graphic over any face or head.",
        (f"Design changes requested by the page's editor (they never add text beyond the list above): "
         f"{copy['design_notes']}" if copy.get("design_notes") else ""),
        ("The LAST image is the editor's layout/style example: follow its look." if example else ""),
        f"Fix these problems from the previous attempt: {feedback}" if feedback else "",
    ]
    return "\n".join(l for l in lines if l)


def _premium_request(d: Path, cfg: dict, brief: dict, copy: dict, upload_url: str | None, feedback: str) -> dict:
    """Premium mode (owner, Nov 1): the image model makes the whole card. The page's own card is drawn
    first (the template renderer: the approved template, the exact text) and given as the layout to
    follow; with no upload URL only the photo is sent and the layout is described in words."""
    import requests
    kc = cfg["krea"]["premium"]
    idx = copy.get("photo_index", -1)
    ph = brief["photos"][idx] if isinstance(idx, int) and 0 <= idx < len(brief.get("photos") or []) else None

    def upload(fp: Path) -> str:
        r = requests.post(upload_url, files={"file": (fp.name, fp.read_bytes(), "image/jpeg")}, timeout=120)
        r.raise_for_status()
        url = r.text.strip().strip('"')
        return json.loads(url).get("url", url) if url.startswith("{") else url

    urls = []
    if ph:
        urls.append(upload(d / ph["file"]) if upload_url and (d / ph["file"]).exists() else ph["url"])
    layout = False
    if upload_url:
        from .cards import FaceBlocked, TextOverflow
        try:                 # the safe card, kept as layout.jpg (the fallback draws it again if needed)
            render_fallback(d, cfg, engine="layout")       # (not an attempt: nothing is posted from it)
            (d / "card.jpg").replace(d / "layout.jpg")
            for f in ("card_clean.jpg", "render.json"):
                (d / f).unlink(missing_ok=True)
            urls.append(upload(d / "layout.jpg"))
            layout = True
        except (FaceBlocked, TextOverflow, ValueError, OSError):
            pass             # no layout to follow: the prompt says the layout in words
    si = copy.get("style_photo_index", -1)    # an editor's style example (regenerations) goes last
    example = isinstance(si, int) and 0 <= si < len(brief.get("photos") or []) and bool(brief["photos"][si].get("url"))
    if example:
        sp = brief["photos"][si]
        urls.append(upload(d / sp["file"]) if upload_url and sp.get("file") and (d / sp["file"]).exists() else sp["url"])
    credit = photo_credit(brief, copy)
    copy["photo_credit"] = credit
    (d / "copy.json").write_text(json.dumps(copy, indent=1, ensure_ascii=False))
    req = {"model": kc["model"], "input": {"prompt": premium_prompt(brief, copy, credit, layout, feedback, example),
                                           **kc.get("input", {}), **({"image_urls": urls} if urls else {})}}
    (d / "krea_request.json").write_text(json.dumps(req, indent=1, ensure_ascii=False))
    return req


def krea_result(d: Path, cfg: dict, store, result_url: str, feedback: str = "") -> dict:
    """Download a finished Krea image into card.jpg and record it against the monthly image cap."""
    import requests
    img = requests.get(result_url, timeout=60).content
    if cfg["krea"].get("mode", "full") == "art":
        return _art_result(d, cfg, store, img, result_url, feedback)
    premium = cfg["krea"].get("mode") == "premium"
    model = cfg["krea"]["premium"]["model"] if premium else cfg["krea"]["model"]
    (d / "card.jpg").write_bytes(to_card_jpeg(img))
    (d / "card_clean.jpg").unlink(missing_ok=True)   # a full Krea card has no clean twin
    if store is not None:
        store.record_spend(f"krea:{model}", cfg["krea"]["prices_usd"].get(model, 0.0))
    attempts = json.loads((d / "attempts.json").read_text()) if (d / "attempts.json").exists() else []
    attempts.append({"at": time.time(), "engine": "krea-premium" if premium else "krea", "model": model,
                     "url": result_url, "feedback": feedback})
    (d / "attempts.json").write_text(json.dumps(attempts, indent=1))
    return {"engine": attempts[-1]["engine"], "attempt": len(attempts)}


class ArtRejected(ValueError):
    pass


def check_art(img: bytes) -> list[str]:
    """The artwork must be a pure backdrop: no person (the card shows only the real photo) and no
    text (all card text is set by the renderer)."""
    import numpy as np
    import cv2
    from .guard import detect_faces, ocr_lines
    problems = []
    im = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
    if im is None:
        return ["artwork could not be read"]
    if detect_faces(im):
        problems.append("artwork contains a face/person")
    words = [t for t in ocr_lines(img) if sum(ch.isalnum() for ch in t) >= 3]
    if words:
        problems.append(f"artwork contains text: {' | '.join(words[:4])}")
    return problems


def _art_result(d: Path, cfg: dict, store, img: bytes, result_url: str, feedback: str) -> dict:
    attempts = json.loads((d / "attempts.json").read_text()) if (d / "attempts.json").exists() else []
    problems = check_art(img)
    # re-running with the same artwork (new layout/photo/copy) is not a new generation
    if not any(a.get("url") == result_url for a in attempts):
        if store is not None:   # every generation costs credits, usable or not
            store.record_spend(f"krea:{cfg['krea']['model']}",
                               cfg["krea"]["prices_usd"].get(cfg["krea"]["model"], 0.0))
        attempts.append({"at": time.time(), "engine": "krea-art", "model": cfg["krea"]["model"], "url": result_url,
                         "feedback": feedback, "problems": problems})
        (d / "attempts.json").write_text(json.dumps(attempts, indent=1))
    (d / "art.png").write_bytes(img)
    if problems:
        raise ArtRejected("; ".join(problems))
    res = render_fallback(d, cfg, art=img, engine="krea-art")
    return {**res, "attempt": len(attempts)}


def krea_daily_allowance(store, cap: int, now: float | None = None) -> tuple[int, int]:
    """(images used today, images allowed today). The month's remaining images are spread evenly
    over its remaining days, so a busy week can't use up the plan and leave the rest of the month
    without Krea. Posts beyond today's share use the template card."""
    import calendar
    from .krea import month_start
    now = now or time.time()
    d = datetime.fromtimestamp(now, timezone.utc)
    day0 = d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    q = "SELECT COUNT(*) FROM spend WHERE actor LIKE 'krea:%' AND at >= ? AND at < ?"
    before = store.db.execute(q, (month_start(now), day0)).fetchone()[0]
    today = store.db.execute(q, (day0, day0 + 86400)).fetchone()[0]
    days_left = calendar.monthrange(d.year, d.month)[1] - d.day + 1
    return today, max(0, (cap - before) // days_left)


def krea_images_this_month(store, now: float | None = None) -> int:
    from .krea import month_start
    return store.db.execute("SELECT COUNT(*) FROM spend WHERE actor LIKE 'krea:%' AND at >= ?",
                            (month_start(now),)).fetchone()[0]


def render_krea(d: Path, cfg: dict, store, feedback: str = "") -> dict:
    from .config import secret
    from .krea import Krea
    import requests
    brief = json.loads((d / "brief.json").read_text())
    copy = json.loads((d / "copy.json").read_text())
    pb = load_playbook(brief["page"]) or brief.get("playbook") or {}
    urls = []
    idx = copy.get("photo_index", -1)
    if isinstance(idx, int) and 0 <= idx < len(brief["photos"]):
        urls.append(brief["photos"][idx]["url"])
    urls += list(pb.get("style_refs") or [])[:3]
    payload = {"prompt": krea_prompt(brief, copy, feedback), "aspect_ratio": cfg["krea"]["aspect_ratio"]}
    if urls:
        payload["image_urls"] = urls[:14]
    krea = Krea(secret("KREA_API_TOKEN"), cfg, store)
    url = krea.generate(cfg["krea"]["model"], payload)
    img = requests.get(url, timeout=60).content
    (d / "card.jpg").write_bytes(to_card_jpeg(img))
    (d / "card_clean.jpg").unlink(missing_ok=True)   # a full Krea card has no clean twin
    attempts = json.loads((d / "attempts.json").read_text()) if (d / "attempts.json").exists() else []
    attempts.append({"at": time.time(), "engine": "krea", "model": cfg["krea"]["model"], "url": url,
                     "feedback": feedback})
    (d / "attempts.json").write_text(json.dumps(attempts, indent=1))
    return {"engine": "krea", "url": url, "attempt": len(attempts)}


def render_fallback(d: Path, cfg: dict, art: bytes | None = None, engine: str = "template") -> dict:
    """Template renderer: exact text over the real photo's own pixels, on Krea's artwork when
    given (art mode) or the template's own backdrop."""
    from .cards import CardSpec, render
    brief = json.loads((d / "brief.json").read_text())
    copy = json.loads((d / "copy.json").read_text())
    if copy.get("template_spec"):
        return _render_spec(d, brief, copy, art, engine)
    photos = []
    idx = copy.get("photo_index", -1)
    if isinstance(idx, int) and 0 <= idx < len(brief["photos"]):
        photos.append((d / brief["photos"][idx]["file"]).read_bytes())
    # extra polaroids (casefile) only when Claude picked them and confirmed who is in them;
    # never "whatever other candidates there were" (that put unrelated people on cards)
    for j in copy.get("extra_photo_indexes") or []:
        if isinstance(j, int) and 0 <= j < len(brief["photos"]) and j != idx and len(photos) < 3:
            photos.append((d / brief["photos"][j]["file"]).read_bytes())
    focus, face_size, face_box = None, 0.0, None
    if photos:
        from .tspec import detect      # at the photo's size and the card's, as verify finds them
        faces = detect(photos[0])
        if faces:
            face_box = max(faces, key=lambda f: f[2] * f[3])
            x, y, w, h = face_box
            focus, face_size = (x + w / 2, y + h / 2), h
    spec = CardSpec(template=copy.get("template", ""), headline_lines=copy.get("headline_lines", []),
                    focus=focus, face_size=face_size, face_box=face_box if focus else None,
                    highlight=copy.get("highlight", ""), label=copy.get("label", ""), dek=copy.get("dek", ""),
                    stat_label=copy.get("stat_label", ""), stat_value=copy.get("stat_value", ""),
                    footer=copy.get("footer", ""), footer_sub=copy.get("footer_sub", ""), team=copy.get("team", ""),
                    photos=photos, art=art, items=copy.get("items") or [],
                    theme=(brief.get("studio") or {}).get("theme"))
    from .cards import FaceBlocked
    try:
        (d / "card.jpg").write_bytes(render(brief["page"], spec))
    except FaceBlocked:
        # photo-left would cover the face: the photo-first layout fits any face position
        if (brief["page"], spec.template or "headline") != ("culture-rush", "headline"):
            raise
        spec.template = "spotlight"
        (d / "card.jpg").write_bytes(render(brief["page"], spec))
        copy["template"] = "spotlight"
        copy.setdefault("notes", []).append("headline layout would cover the face: switched to spotlight")
        (d / "copy.json").write_text(json.dumps(copy, indent=1, ensure_ascii=False))
    # the same card without worn lettering: `verify` reads the text on it (OCR loses short worn
    # words); the geometry is identical, so face checks still run on the real card
    from dataclasses import replace
    (d / "card_clean.jpg").write_bytes(render(brief["page"], replace(spec, distress=False)))
    attempts = json.loads((d / "attempts.json").read_text()) if (d / "attempts.json").exists() else []
    if engine == "template":   # art mode records its attempt in krea_result
        attempts.append({"at": time.time(), "engine": "template"})
        (d / "attempts.json").write_text(json.dumps(attempts, indent=1))
    return {"engine": engine}


def _render_spec(d: Path, brief: dict, copy: dict, art: bytes | None, engine: str) -> dict:
    """A card drawn with one of the page's template specs (bore/tspec.py): the chosen photo (in the
    Studio always the editor's, photos/0.jpg) in the spec's photo zone, extra_photo_indexes in its
    other zones, Krea's artwork in its art layer (art mode; none without one). Never another
    photo, and no other layout when a face cannot be kept clear: FaceBlocked / TextOverflow refuse it."""
    from dataclasses import replace

    from . import tspec
    from .cards import render
    from .studio import studio_meta
    spec = tspec.find(copy["template_spec"], brief)
    photos, idx = [], copy.get("photo_index", -1)
    if isinstance(idx, int) and 0 <= idx < len(brief["photos"]):
        photos.append((d / brief["photos"][idx]["file"]).read_bytes())
    for j in copy.get("extra_photo_indexes") or []:
        if isinstance(j, int) and 0 <= j < len(brief["photos"]) and j != idx:
            photos.append((d / brief["photos"][j]["file"]).read_bytes())
    if not photos:
        raise tspec.SpecError("a template spec draws the chosen photo: set photo_index")
    # the page's colours from the admin panel (the hourly briefs carry none of their own)
    theme = (brief.get("studio") or {}).get("theme") or (studio_meta().get(brief["page"]) or {}).get("theme")
    card = tspec.card_spec(spec, copy, photos, theme=theme, art=art if spec.get("art") else None)
    (d / "card.jpg").write_bytes(render(brief["page"], card))
    # the clean twin `verify` reads (same geometry, no worn lettering), and where each line of text is
    # on it: verify reads a lone digit there (OCR alone drops a '1' on a plate)
    drawn = []
    clean = render(brief["page"], replace(card, distress=False), drawn=drawn)
    (d / "card_clean.jpg").write_bytes(clean)
    (d / "render.json").write_text(json.dumps({"sha256": hashlib.sha256(clean).hexdigest(), "lines": drawn}))
    if engine == "template":
        attempts = json.loads((d / "attempts.json").read_text()) if (d / "attempts.json").exists() else []
        attempts.append({"at": time.time(), "engine": "template", "template_spec": spec["id"]})
        (d / "attempts.json").write_text(json.dumps(attempts, indent=1))
    return {"engine": engine, "template_spec": spec["id"]}


def to_card_jpeg(img: bytes) -> bytes:
    """Normalise any result to a 1080x1350 JPEG (high-quality resample)."""
    from PIL import Image
    im = Image.open(io.BytesIO(img)).convert("RGB")
    if im.size != (CARD_W, CARD_H):
        # cover-fit to 4:5 then resize
        tw, th = im.size
        target = CARD_W / CARD_H
        if tw / th > target:
            nw = int(th * target)
            im = im.crop(((tw - nw) // 2, 0, (tw - nw) // 2 + nw, th))
        elif tw / th < target:
            nh = int(tw / target)
            im = im.crop((0, (th - nh) // 2, tw, (th - nh) // 2 + nh))
        im = im.resize((CARD_W, CARD_H), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=93, optimize=True, progressive=True)
    return out.getvalue()


# ------------------------------------------------------------------------------- as is
AS_IS_MAX = 6                 # images in one post "as is" (Facebook shows them as a grid)
AS_IS_BYTES = 2_800_000       # all of them in one upload (the dashboard takes up to 4.5 MB per request)


def as_is_order(brief: dict, copy: dict) -> list[int]:
    """The photos an as-is post shows, in order: copy.photos, else photo_index, else all of them."""
    n = len(brief.get("photos") or [])
    order = copy.get("photos")
    if not isinstance(order, list) or not order:
        idx = copy.get("photo_index")
        order = [idx] if isinstance(idx, int) and idx >= 0 else list(range(n))
    return [i for i in dict.fromkeys(order) if isinstance(i, int) and 0 <= i < n][:AS_IS_MAX]


def render_as_is(d: Path) -> dict:
    """An editor asked for the post's own images "as is" (no edits, no branding): the chosen photos
    become the post unchanged - no card, text, logo or crop - in the copy's order: card.jpg is the
    first, extra/<n>.jpg the others. They are only scaled down when larger than Facebook shows
    (2048 px) or too big to upload together."""
    import shutil

    from PIL import Image, ImageOps

    from .media import flatten
    brief = json.loads((d / "brief.json").read_text())
    copy = json.loads((d / "copy.json").read_text())
    order = as_is_order(brief, copy)
    if not order:
        raise ValueError("no photos to post as is: add the link's images (or `routine photos`) first")
    side, q = 2048, 92
    while True:
        blobs = []
        for i in order:
            im = flatten(ImageOps.exif_transpose(Image.open(d / brief["photos"][i]["file"])))
            im.thumbnail((side, side), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q, optimize=True)
            blobs.append(buf.getvalue())
        if sum(map(len, blobs)) <= AS_IS_BYTES or side <= 1080:
            break
        side, q = int(side * 0.8), max(80, q - 4)
    (d / "card.jpg").write_bytes(blobs[0])
    shutil.rmtree(d / "extra", ignore_errors=True)
    (d / "extra").mkdir()
    for n, b in enumerate(blobs[1:], 1):
        (d / "extra" / f"{n}.jpg").write_bytes(b)
    # nothing to read back: there is no card text (step 4b's look at the images still applies)
    (d / "ocr.json").write_text(json.dumps({"pass": True, "as_is": True, "ocr": [], "errors": [], "warnings": [],
                                            "images": len(blobs)}))
    return {"engine": "as-is", "images": len(blobs), "photos": order, "max_side": side}


# ------------------------------------------------------------------------------- upload
PHOTO_KEYS = ("source", "via", "note", "flags", "consensus")   # what a draft keeps about its photo


def draft_payload(d: Path) -> dict:
    import base64
    brief = json.loads((d / "brief.json").read_text())
    copy = json.loads((d / "copy.json").read_text())
    verdict = json.loads((d / "verdict.json").read_text()) if (d / "verdict.json").exists() else {}
    attempts = json.loads((d / "attempts.json").read_text()) if (d / "attempts.json").exists() else []
    idx = copy.get("photo_index")
    chosen = brief["photos"][idx] if isinstance(idx, int) and 0 <= idx < len(brief["photos"]) else None
    return {
        "id": brief["id"], "page": brief["page"], "page_name": brief["page_name"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "caption": copy["caption"], "headline": " ".join(copy.get("headline_lines", [])),
        "niche_fit": copy.get("niche_fit"), "reason": copy.get("reason", ""),
        "kind": copy.get("kind") or brief.get("kind", ""), "kind_rank": brief.get("kind", ""),
        **({"lesson_override": copy["lesson_override"]} if copy.get("lesson_override") else {}),
        **({"note_to_editor": str(copy["note_to_editor"])[:1200]} if copy.get("note_to_editor") else {}),
        "verification": brief["verification"], "sources": [p["url"] for p in brief["posts"][:6] if p.get("url")],
        "engine": "as-is" if copy.get("as_is") else attempts[-1]["engine"] if attempts else "", "check": verdict,
        "source_photo": chosen["url"] if chosen else "",
        # which candidate it was and what it was: an editor's "Image quality" teaches about this photo
        **({"photo": {"rank": idx, "of": len(brief["photos"]),
                      **{k: chosen[k] for k in PHOTO_KEYS if chosen.get(k)}}} if chosen else {}),
        "people_in_photo": copy.get("people_in_photo", []),
        # kept so a reviewer's feedback can rebuild the post later from the same facts
        "brief": {"posts": brief["posts"], "verification": brief["verification"],
                  "headline_hint": brief.get("headline_hint", ""),
                  "photos": [{**{k: p.get(k) for k in ("url", "source", "credit")},
                              **{k: p[k] for k in PHOTO_KEYS if k != "source" and p.get(k)}} for p in brief["photos"]]},
        "copy": {k: v for k, v in copy.items() if k != "caption"},
        **({"link_request": link["request_id"], "link_url": link["url"], "link_note": link.get("note", "")}
           if (link := brief.get("link")) else {}),
        **({"update_of": copy["update_of"], "what_is_new": copy.get("what_is_new", "")}
           if copy.get("update_of") else {}),
        **({"replaces": regen["replaces"], "feedback_applied": regen.get("feedback", ""),
            "previous_image_url": regen.get("previous_image_url", ""), "changes": copy.get("changes", "")}
           if (regen := brief.get("regeneration")) else {}),
        "image_b64": base64.b64encode((d / "card.jpg").read_bytes()).decode(),
        # an as-is post's other images, posted together after the first
        **({"as_is": True, "extra_images_b64": [base64.b64encode(f.read_bytes()).decode() for f in
                                                sorted((d / "extra").glob("*.jpg"), key=lambda f: int(f.stem))]}
           if copy.get("as_is") else {}),
    }
