"""The routines' side of the Post Studio (dashboard /api/routine/*). See STUDIO.md.

  sync          pull pages, sources, playbooks and reference-document text from the dashboard's
                encrypted store into private/ (gitignored), and the templates and visual documents'
                pages as images (drawn again when the version changes). Every routine run starts with it.
  feed          hourly: each page's ranked, de-duplicated stories of the last 24 hours, with
                candidate photos. The routine writes headline, summary and two visual ideas for
                the top ones; `publish_feed` sends them, so topic selection and ideation in the
                Studio are instant.
  jobs          Studio jobs the editors started (ideas, images, compose, check): one folder per
                post with a brief the normal post tools (lint, krea, fallback, verify) work on. A
                templates job converts a page's templates document into template specs: one folder
                per template (spec, the document pages it came from, a preview), sent for approval.
  finish_job    send the results back; usage (searches, Krea images, checks) is logged too.

The page's templates its editors approved (template specs, dashboard routes/templates.js) are synced
into private/templates/<page>/<id>.json: only those are ever offered in the ideas or drawn.
"""
from __future__ import annotations

import base64
import copy as _copy
import hashlib
import json
import re
import secrets
import shutil
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from .config import PRIVATE, ROOT, Page, load_pages, save_pages
from .faceid import fields as id_fields
from .layouts import GENERIC

WORK = ROOT / "work" / "studio"
FEED_MAX, FEED_WRITE = 40, 15          # stories per page in the feed; how many get written text per run
WINDOWS = ((3, 15), (12, 15), (24, 10))   # (hours, stories): every Studio time window gets fresh stories
DOC_TYPES = ("templates", "visual", "tone")
# The documents whose pages are pictures to read (zone diagrams, examples): their text alone loses
# where each box sits. `studio sync` gives the session each page as an image, next to the text.
DOC_IMAGE_TYPES = ("templates", "visual")
DOC_PAGE_PX = 1200          # page images this tall: a zone diagram's labels and word limits read clearly
DOC_PAGE_MAX_PX = 2000      # (a wide page: its long side at most this)
DOC_MAX_PAGES = 80          # a longer document: its first pages (the brief says how many it has)
LAYOUT_DOC = {
    "headline": "big photo on top, 1-3 headline lines below; optional label (small tag) and dek (one sentence)",
    "quote": "photo on top; headline_lines are the quote (1-4 short lines, no quotation marks); label is the speaker",
    "stat_card": "photo on top; stat_value is the huge number, stat_label what it counts, one headline line",
    "comparison": "two photos (photo_index, extra_photo_indexes[0]); items[0] and items[1] are {label: name}; "
                  "items[2..4] are rows {label, left, right}; one headline line",
    "then_vs_now": "two photos (then = photo_index, now = extra_photo_indexes[0]); items[0..1] are "
                   "{label: THEN/NOW or similar, year}; 1-2 headline lines",
    "ranked_list": "one photo; 1-2 headline lines; items are up to 5 rows {label, value?} in rank order",
    "timeline": "one photo; 1-2 headline lines; items are up to 5 steps {date, text} in time order",
}


def _dash():
    from .dashboard import Dashboard
    d = Dashboard.from_env()
    if not d:
        raise SystemExit("DASHBOARD_URL / ROUTINE_SECRET are not set")
    return d


def _get(dash, path: str, **params) -> dict:
    r = dash.s.get(f"{dash.url}/api/routine/{path}", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def _post(dash, path: str, body: dict) -> dict:
    r = dash.s.post(f"{dash.url}/api/routine/{path}", json=body, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"/api/routine/{path}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


# ------------------------------------------------------------------ sources <-> page settings
def classify(src: dict) -> tuple[str, str]:
    """(kind, value) for an admin source: x / reddit / search / feed / site / other."""
    url, typ = str(src.get("url") or "").strip(), src.get("type") or "website"
    if typ == "search":
        return "search", url
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = parts.netloc.lower().removeprefix("www.").removeprefix("old.")
    path = [p for p in parts.path.split("/") if p]
    if host in ("x.com", "twitter.com", "mobile.twitter.com") and path:
        return "x", path[0].lower().lstrip("@")
    if host.endswith("reddit.com") and len(path) >= 2 and path[0] == "r":
        return "reddit", path[1].lower()
    if typ == "feed" or re.search(r"\.(xml|rss|atom)$", parts.path, re.I):     # a feed's own address
        return "feed", urlunsplit((parts.scheme or "https", parts.netloc, parts.path, parts.query, ""))
    if typ == "social" or host in ("instagram.com", "tiktok.com", "facebook.com", "youtube.com", "threads.net"):
        return "other", url            # no collector for these yet; kept for the admins
    return "site", f"{host}/{'/'.join(path)}" if path else host      # keep the section path


# What the page record itself says, never the pipeline settings: its status, budgets and account
RECORD_FIELDS = ("workspace", "status", "posts_per_day", "premium_per_day", "socialpilot_login_id")


def page_from_config(p: dict) -> Page:
    """The collector's page settings: the stored pipeline settings, with the source lists taken
    from the admin's active sources (an admin who disables a source removes it everywhere). The
    page record's status, budgets and SocialPilot account come from the record alone: a record
    without a status is a draft (it never runs)."""
    pl = {k: v for k, v in (p.get("pipeline") or {}).items() if k not in RECORD_FIELDS}
    pl.update(slug=p["slug"], name=p.get("name") or pl.get("name") or p["slug"],
              timezone=p.get("timezone") or pl.get("timezone") or "America/New_York",
              hourly=bool(pl.get("hourly", True)),
              **{k: p[k] for k in RECORD_FIELDS if p.get(k) is not None})
    base = Page.from_dict(pl)
    xs, subs, searches, sites, feeds = [], [], [], [], []
    for s in p.get("sources") or []:
        if s.get("active") is False:
            continue
        kind, val = classify(s)
        {"x": xs, "reddit": subs, "search": searches, "site": sites, "feed": feeds}.get(kind, []).append(val)
    xset, sset, qset = set(xs), set(subs), set(searches)
    base.x_handles = [h for h in base.x_handles if h in xset] + [h for h in dict.fromkeys(xs)
                                                                  if h not in base.x_handles and h not in base.x_handles_secondary]
    base.x_handles_secondary = [h for h in base.x_handles_secondary if h in xset]
    base.tier_one = [h for h in base.tier_one if h in xset]
    base.officials = [h for h in base.officials if h in xset]
    base.subreddits = [s for s in base.subreddits if s in sset] + [s for s in dict.fromkeys(subs)
                                                                    if s not in base.subreddits and s not in base.subreddits_rotation]
    base.subreddits_rotation = [s for s in base.subreddits_rotation if s in sset]
    base.web_rotation = [q for q in base.web_rotation if q in qset]
    base.web_searches = ([q for q in base.web_searches if q in qset]
                         + [q for q in dict.fromkeys(searches) if q not in base.web_searches and q not in base.web_rotation]
                         + [f"site:{d}" for d in dict.fromkeys(sites)])
    base.websites = list(dict.fromkeys(sites))
    base.feeds = list(dict.fromkeys(feeds))      # websites' own feeds are found when collecting
    return base


def sources_from_page(page: Page) -> list[dict]:
    """Admin source list for a page from the old config (migration)."""
    out, seen = [], set()

    def add(typ, url, label):
        if url in seen:
            return
        seen.add(url)
        out.append({"id": secrets.token_hex(3), "url": url, "type": typ, "label": label, "active": True,
                    "added_at": int(time.time() * 1000)})
    for h in page.x_handles:
        add("social", f"https://x.com/{h}", "reporter" if h in page.tier_one else "official" if h in page.officials else "X")
    for h in page.x_handles_secondary:
        add("social", f"https://x.com/{h}", "X (rotated)")
    for s in page.subreddits:
        add("reddit", f"https://www.reddit.com/r/{s}/", "Reddit")
    for s in page.subreddits_rotation:
        add("reddit", f"https://www.reddit.com/r/{s}/", "Reddit (rotated)")
    for q in page.web_searches:
        add("search", q, "web search")
    for q in page.web_rotation:
        add("search", q, "web search (rotated)")
    for w in page.websites:
        add("website", w if "://" in w else f"https://{w}", "website")
    return out


# ------------------------------------------------------------------ sync / migration
def _doc_pdf(dash, slug: str, typ: str, d: dict) -> bytes:
    """The active reference PDF, downloaded part by part (one response carries at most 4.5 MB) and
    checked against the size and SHA-256 the config gives. (An older dashboard sends it whole.)"""
    chunks = []
    for i in range(max(1, int(d.get("parts") or 1))):
        r = dash.s.get(f"{dash.url}/api/routine/doc", params={"page": slug, "type": typ, "part": i}, timeout=60)
        r.raise_for_status()
        chunks.append(r.content)
    pdf = b"".join(chunks)
    if not pdf.startswith(b"%PDF-"):
        raise ValueError("the download is not a PDF")
    if (d.get("size") and len(pdf) != d["size"]) or (d.get("sha256") and hashlib.sha256(pdf).hexdigest() != d["sha256"]):
        raise ValueError("the downloaded parts do not add up to the document")
    return pdf


def render_doc_pages(pdf: bytes, out: Path) -> tuple[list[Path], int]:
    """Each page of the PDF as out/page-NN.png, DOC_PAGE_PX tall: (the files, the PDF's page count).
    Drawn into a folder beside it and swapped in at the end, so a failure never mixes two versions."""
    import pypdfium2 as pdfium
    tmp = out.with_name(out.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    doc = pdfium.PdfDocument(pdf)
    try:
        total = len(doc)
        for i in range(min(total, DOC_MAX_PAGES)):
            page = doc[i]
            try:
                w, h = page.get_size()
                page.render(scale=min(DOC_PAGE_PX / max(h, 1), DOC_PAGE_MAX_PX / max(w, 1))).to_pil().save(tmp / f"page-{i + 1:02d}.png")
            finally:
                page.close()
    finally:
        doc.close()
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    return sorted(out.glob("page-*.png")), total


def _drawn(slug: str, typ: str, d: dict) -> tuple[dict, list[Path], dict]:
    """What the active document's page images must be (version, checksum, size, height), the ones
    drawn here (private/docs/<page>/<type>/), and their record, when they are of that version."""
    out = PRIVATE / "docs" / slug / typ
    want = {"version": d.get("version"), "sha256": d.get("sha256") or "", "size": d.get("size"), "px": DOC_PAGE_PX}
    try:
        have = json.loads((out / "pages.json").read_text())
    except (OSError, ValueError):
        have = {}
    files = [out / f for f in have.get("files") or []]
    if {k: have.get(k) for k in want} != want or not files or not all(f.exists() for f in files):
        return want, [], {}
    return want, files, have


def _listed(files: list[Path], have: dict) -> dict:
    return {"images": [str(f.relative_to(ROOT)) for f in files], "image_pages": have.get("pages") or len(files)}


def doc_images(dash, slug: str, typ: str, d: dict) -> dict:
    """The brief's list of the active document's page images (private/docs/<page>/<type>/), drawn
    again only when the version changed (a rollback is a change too)."""
    want, files, have = _drawn(slug, typ, d)
    if not files:
        out = PRIVATE / "docs" / slug / typ
        try:
            import pypdfium2  # noqa: F401  (before downloading a document this machine could not draw)
            files, total = render_doc_pages(_doc_pdf(dash, slug, typ, d), out)
        except Exception:
            shutil.rmtree(out, ignore_errors=True)       # never the pages of another version
            raise
        have = {**want, "pages": total, "files": [f.name for f in files]}
        (out / "pages.json").write_text(json.dumps(have))
    return _listed(files, have)


def doc_pages(slug: str, typ: str, dash=None) -> dict:
    """The page's synced document entry (studio.json docs.<type>) with its page images, downloaded and
    drawn now when this machine has none of its version: only the jobs that open them (templates,
    IDEAS, COMPOSE) draw them, so the hourly, topics and Studio syncs never download a document (a 40 MB one was 17 requests a sync)."""
    meta = studio_meta()
    doc = ((meta.get(slug) or {}).get("docs") or {}).get(typ)
    if not doc or doc.get("images"):
        return doc
    doc.update(doc_images(dash or _dash(), slug, typ, doc.get("ref") or {}))
    (PRIVATE / "studio.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    return doc


def sync(dash=None, quiet: bool = False) -> list[dict]:
    dash = dash or _dash()
    cfg = _get(dash, "config")["pages"]
    PRIVATE.mkdir(exist_ok=True)
    for sub in ("playbooks", "docs", "priority"):
        (PRIVATE / sub).mkdir(exist_ok=True)
    pages, meta = [], {}
    for p in cfg:
        page = page_from_config(p)
        pl = p.get("pipeline") or {}
        sheet = pl.get("source_sheet") or ({"priority_terms": pl["priority_terms"]} if pl.get("priority_terms") else None)
        if sheet:
            f = PRIVATE / "priority" / f"{page.slug}.yaml"
            f.write_text(yaml.safe_dump(sheet, sort_keys=False, allow_unicode=True))
            page.priority_terms_file = str(f)
        else:
            page.priority_terms_file = ""
        pages.append(page)
        pb = p.get("playbook")
        pbf = PRIVATE / "playbooks" / f"{page.slug}.yaml"
        if isinstance(pb, dict) and pb:
            pbf.write_text(yaml.safe_dump(pb, sort_keys=False, allow_unicode=True))
        else:
            pbf.unlink(missing_ok=True)
        docs = {}
        for t, d in (p.get("docs") or {}).items():
            f = PRIVATE / "docs" / page.slug / f"{t}.txt"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(d.get("text") or "")
            docs[t] = {"version": d.get("version"), "file": str(f.relative_to(ROOT)), "pages": d.get("pages")}
            if t in DOC_IMAGE_TYPES:     # images: page-NN.png, the PDF's pages, drawn by the job that needs them (doc_pages)
                _, files, have = _drawn(page.slug, t, d)
                docs[t].update(_listed(files, have) if files else {"images": []},
                               ref={k: d.get(k) for k in ("version", "sha256", "size", "parts")})
        for t in DOC_IMAGE_TYPES:        # a document no longer there: its page images go too
            if t not in docs:
                shutil.rmtree(PRIVATE / "docs" / page.slug / t, ignore_errors=True)
        templates = _sync_templates(page.slug, p)
        meta[page.slug] = {"name": page.name, "theme": p.get("theme") or {}, "docs": docs, "templates": templates,
                           "hourly": page.hourly, "status": page.status, "workspace": page.workspace,
                           "posts_per_day": page.posts_per_day, "premium_per_day": page.premium_per_day,
                           "socialpilot_login_id": page.socialpilot_login_id,
                           "fb_page_id": p.get("fb_page_id"), "has_playbook": isinstance(pb, dict) and bool(pb)}
    save_pages(pages, PRIVATE / "pages.yaml")
    (PRIVATE / "studio.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    from . import lessons as _lessons   # what the editors' reviews teach (ranking, briefs, topic feed)
    for page in pages:
        try:
            _lessons.save(page.slug, _get(dash, "lessons", page=page.slug).get("lessons") or {})
        except Exception:
            pass
    if not quiet:
        for slug, m in meta.items():
            pg = next(x for x in pages if x.slug == slug)
            print(f"{m['name']} ({slug}): {m['status']}, {'hourly + Studio' if m['hourly'] else 'Studio only'}, "
                  f"{len(pg.x_handles) + len(pg.x_handles_secondary)} X, {len(pg.subreddits) + len(pg.subreddits_rotation)} "
                  f"Reddit, {len(pg.web_searches) + len(pg.web_rotation)} web searches, docs: "
                  + (", ".join(f"{t} ({len(v['images'])} page images)" if v.get("images") else t
                               for t, v in m["docs"].items()) or "none")
                  + f", {len(m['templates'])} approved template(s)")
    return cfg


def _sync_templates(slug: str, p: dict) -> list[dict]:
    """The page's approved template specs (the dashboard's config) into private/templates/<page>/<id>.json,
    where lint, the renderer and verify find them (bore/tspec.py); returns the briefs' list of them. A spec
    no longer approved is removed: only what an editor approved is ever drawn. An older dashboard sends no
    templates at all: the folder is left as it is (it can hold none it did not send)."""
    from . import ops, tspec
    out = PRIVATE / "templates" / slug
    if "templates" not in p:
        return []
    out.mkdir(parents=True, exist_ok=True)
    listed, keep = [], set()
    for t in p.get("templates") or []:
        raw = t.get("spec") or {}
        try:
            spec = tspec.load(raw)
        except tspec.SpecError as e:      # approved, but this code cannot draw it (a newer or older spec format)
            ops.degraded("approved template spec not drawable")
            print(f"TEMPLATE {slug}: an approved template spec does not validate here, left out ({str(e).splitlines()[0][:160]})")
            continue
        if spec["id"] != t.get("id"):
            continue
        f = out / f"{spec['id']}.json"
        f.write_text(json.dumps(raw, indent=1, ensure_ascii=False))
        keep.add(f.name)
        listed.append(template_entry(spec, f, t))
    for f in out.glob("*.json"):
        if f.name not in keep:
            f.unlink()
    return sorted(listed, key=lambda x: x["name"])


def template_entry(spec: dict, f: Path, t: dict | None = None) -> dict:
    """What a brief says about one approved template: enough to choose it (ideas) and to fill it (its
    slots' rules; the spec itself is the file)."""
    from . import tspec
    slots = {k: {x: v[x] for x in ("type", "required", "max_words", "max_chars", "min_items", "max_items", "hint") if v.get(x) not in (None, "")}
             for k, v in spec["slots"].items()}
    return {"id": spec["id"], "name": spec["name"], "use_when": spec["use_when"], "visual_lane": spec["visual_lane"],
            "art_layer": bool(spec.get("art")), "photos": tspec.photo_count(spec), "slots": slots,
            "file": str(f.relative_to(ROOT)) if f.is_relative_to(ROOT) else str(f),
            **({"v": t.get("v"), "doc_version": t.get("doc_version")} if t else {})}


def approved_templates(slug: str) -> list[dict]:
    """The page's approved templates (synced by `studio sync`), for the briefs."""
    return (studio_meta().get(slug) or {}).get("templates") or []


def studio_meta() -> dict:
    f = PRIVATE / "studio.json"
    return json.loads(f.read_text()) if f.exists() else {}


# ------------------------------------------------------------------ hourly feed
def _source_name(src: str) -> str:
    kind, _, who = src.partition(":")
    return {"x": f"@{who}", "reddit": f"r/{who}"}.get(kind, who)


def image_out(ph) -> dict:
    """A candidate image as editors see it: where it came from, what to check and whose face it
    shows (id_match: match | mismatch ('may not be <id_name>'), compared with the id_ref face)."""
    return {"url": ph.url, "source": ph.source, "credit": ph.credit, "size": f"{ph.w}x{ph.h}" if ph.w else "",
            **{k: v for k, v in (("via", ph.via), ("post_url", ph.post_url), ("note", ph.note),
                                 ("flags", list(ph.flags)[:4]), ("consensus", ph.consensus if ph.consensus > 1 else 0),
                                 *id_fields(ph).items()) if v}}


def _story_from_trend(t, now: float, learned_rec: dict | None = None, policy: str = "allow") -> dict:
    from .media import story_photos
    from .select import ts
    cl = t.cluster
    members = sorted(cl.members, key=lambda m: -int(m.get("engagement") or 0))
    sources, seen = [], set()
    for m in members:
        src = next((s for s in cl.sources if s.split(":", 1)[-1].lower() == str(m.get("author") or m.get("subreddit") or m.get("domain") or "").lower()), None)
        if m.get("platform") == "reddit" and m.get("subreddit"):
            name = f"r/{m['subreddit']}"
        else:
            name = _source_name(src) if src else (m.get("author") or m.get("domain") or m.get("platform") or "")
        if name and name not in seen:
            seen.add(name)
            sources.append({"name": name, "url": m.get("url") or ""})
    for s in sorted(cl.sources):
        n = _source_name(s)
        if n and n not in seen:
            seen.add(n)
            sources.append({"name": n, "url": ""})
    images = []
    try:
        images = [image_out(ph) for ph in story_photos(members, max_web=1, policy=policy)[:6]]
    except Exception:
        pass
    lead = cl.lead()
    k = ((learned_rec or {}).get("kinds") or {}).get(t.kind) or {}   # how editors judged this kind so far
    learned = f"{k.get('label', t.kind)}: editors approved {k['approved']} of {k['n']}" if k.get("n") else ""
    from .media import subject_of
    return {"id": t.key, "headline_hint": t.headline[:280], "score": round(t.score, 2), "route": t.route,
            "kind": t.kind, "learned": learned, "subject": subject_of(members),
            # the latest post about it: a story still being reported shows in the short windows
            "published_at": int(max(ts(m) for m in cl.members) * 1000),
            "first_seen": int(min(ts(m) for m in cl.members) * 1000), "url": lead.get("url") or "",
            "sources": sources[:12], "images": images,
            "posts": [{"platform": m.get("platform"), "author": m.get("author") or m.get("subreddit") or "",
                       "text": ((m.get("title") or "") + "\n" + (m.get("text") or "")).strip()[:700],
                       "url": m.get("url") or "", "engagement": int(m.get("engagement") or 0),
                       # a Reddit link post: the article it shares (read it for the facts)
                       **({"link": (m.get("metrics") or {})["link"]} if (m.get("metrics") or {}).get("link") else {})}
                      for m in members[:5]]}


IMAGES_V = 5                 # candidate images checked by the current rules (reused while a story stays)
FACELESS_V = 4               # the same checks before face matching: kept, only whose face each shows is added
FEED_IMAGE_BUDGET_S = 90     # per page per hour: new stories first (the rest next hour)
FEED_FACES_S = 6             # per story, within that budget: whose face each candidate shows
RECHECK_THIN_S = 3 * 3600    # a story with fewer than 2 good images is looked at again (new coverage brings photos)
FEED_SEARCHES = 3            # per page per run: photo searches by the subject's name for stories short of images


def _placeholder(stories: list[dict], i: dict) -> bool:
    """A site's placeholder ("NEWS ROUNDUP", a logo) rather than a photo: an article image on three
    or more stories, or on two about different people. Never the picture outlets shared, or a
    video still; near-duplicate stories about one person legitimately share a photo."""
    from .consensus import _logo_url
    if not i.get("_h") or i.get("source") not in ("web", "article") or (i.get("consensus") or 0) >= 2:
        return False
    on = [st for st in stories if any(x.get("_h") == i["_h"] for x in st.get("images") or [])]
    subjects = {(st.get("subject") or "").lower() for st in on}
    return len(on) >= 3 or (len(on) == 2 and len(subjects - {""}) == 2) or (len(on) >= 2 and _logo_url(i["url"]))


WRITTEN_FROM_MAX = 30       # posts recorded per story: what its text was written from


def member_urls(t) -> list[str]:
    """A story's posts, most engaged first (its lead first): what its text is written from."""
    ms = sorted(t.cluster.members, key=lambda m: -int(m.get("engagement") or 0))
    return list(dict.fromkeys(m["url"] for m in ms if m.get("url")))[:WRITTEN_FROM_MAX]


def _posts_of(s: dict) -> list[str]:
    """The posts a feed story's text (or verdict) came from: recorded since Oct 21; an older feed
    has only its sources, lead first."""
    return s.get("written_from") or list(dict.fromkeys(
        ([s["url"]] if s.get("url") else []) + [x["url"] for x in s.get("sources") or [] if x.get("url")]))


def _overlap(s: dict, o: dict) -> float:
    """How surely a new story is an earlier one (0: not at all). Its text came from the old
    story's posts, so the post it was written from (the lead) must still be in it, and they share
    at least half of each other's posts: a mixed cluster that splits, or a story that grew into
    another, gets new text (a Naquan Jones story once kept a Purdy headline, Oct 21). A story that
    only grew (the same lead, almost all of its old posts still there) keeps its text however many
    posts joined: the fastest-growing stories went back to a raw hint every time they doubled."""
    old = _posts_of(o)
    new_list = s.get("written_from") if o.get("written_from") else _posts_of({**s, "written_from": None})
    new = set(new_list or [])
    shared = len(new & set(old))
    if not (old and new and old[0] in new):
        return 0.0
    grew = new_list[0] == old[0] and shared >= 0.8 * len(old)
    if not grew and (shared < 0.5 * len(old) or shared < 0.5 * min(len(new), WRITTEN_FROM_MAX)):
        return 0.0
    return shared / len(new | set(old)) + (1.0 if s.get("id") == o.get("id") else 0.0)


def _inherit(stories: list[dict], old_list: list[dict]) -> dict:
    """{new story id: the earlier story it continues}. Each earlier story is continued at most
    once, by the new story that overlaps it most (one story that splits in two hands its text to
    neither half unless one of them is clearly it)."""
    pairs = sorted(((_overlap(s, o), i, j) for i, s in enumerate(stories) for j, o in enumerate(old_list)), reverse=True)
    out, used_new, used_old = {}, set(), set()
    for score, i, j in pairs:
        if score > 0 and i not in used_new and j not in used_old:
            out[stories[i]["id"]] = old_list[j]
            used_new.add(i)
            used_old.add(j)
    return out


def _fits(headline: str, members: list[dict]) -> bool:
    """The names a written headline mentions are (at least half of them) in the story's current
    posts: otherwise the text is about another story and is written again."""
    from .textutil import token_set
    names = token_set(" ".join(re.findall(r"\b[A-Z][\w'’\-]*", headline or "")))
    said = token_set(" ".join(f"{m.get('title') or ''} {m.get('text') or ''}" for m in members))
    return not (names and said) or len(names & said) >= 0.5 * len(names)


def crime_story(t, members: list[dict]) -> bool:
    """A crime, court or abuse story: no photo search by name (a wrong face there names the wrong
    person, or an accuser: 'Former Cornell' brought one, Oct 21). Its own posts' photos only."""
    from .kinds import CRIME_ABUSE_RX           # the crime_court kind's words, and abuse
    text = " ".join([getattr(t, "headline", "") or ""]
                    + [f"{m.get('title') or ''} {m.get('text') or ''}" for m in members[:3]])
    return getattr(t, "kind", "") == "crime_court" or bool(CRIME_ABUSE_RX.search(text))


def _known_face(ref) -> bool:
    """The face lookup found the subject's face (an ESPN headshot or a Wikipedia portrait): the
    subject is a person whose photos can be told apart. 'On March', 'Rio Negro' have none."""
    try:
        return ref is not None and ref.result(timeout=FEED_FACES_S / 2)[0] is not None
    except Exception:
        return False


class PhotoCheckFailed(Exception):
    """The photo check itself failed (a broken machine, a crash): the candidates stay unchecked
    (images_v 0), so the next run checks them, instead of passing as checked."""

    def __init__(self, images: list[dict], why: str):
        super().__init__(why)
        self.images = [{**i, "flags": [f"not checked: {why}"] + [f for f in i.get("flags") or []
                                                                 if not f.startswith("not checked")][:3]} for i in images]


def merge_same_stories(page, ranked: list) -> list:
    """The same news as two or more stories (clustering is strict, so separate stories never
    chain together): after ranking, the lower-ranked copy's posts join the higher-ranked one. The
    same story as any story already merged into it: a shared post or linked article, the repeat
    test of the upload gate (bore/dupes.py), or the same subject and at least two shared words about
    what happened. A live sports feed had one player's suspension, another's release and a signing
    twice each, and one dispute three times ('League finds Demo FC handled...' is the story of
    'League backs Demo FC's handling...')."""
    from .dupes import Deduper, trend_text
    from .media import list_post, subject_of
    dd, kept = Deduper(page, []), []

    def same(a, b) -> bool:
        (f, subject, listy), (g, subject2, listy2) = a, b
        what = (f.tokens & g.tokens) - f.names - g.names
        # a stat list shares its names with every story about one of them, not its story (Oct 21: a
        # completion-percentage list and a Jayden Daniels story both named Caleb Williams)
        return (not (listy or listy2) and dd.same_story(f, g)[0]) or bool(subject and subject == subject2 and len(what) >= 2)
    for t in ranked:
        ms = t.cluster.members
        urls = {u for m in ms for u in [m.get("url"), *((m.get("metrics") or {}).get("links") or []),
                                         (m.get("metrics") or {}).get("link")] if u}
        lead = max(ms, key=lambda m: int(m.get("engagement") or 0)) if ms else {}
        this = (dd.features_of(trend_text(t)), subject_of(ms), bool(ms) and list_post(lead))
        # against every story already merged into a kept one, not only the first
        into = next((k for k in kept if k[1] & urls or any(same(this, other) for other in k[2])), None)
        if into is None:
            kept.append([t, urls, [this]])
            continue
        have = {m.get("id") or m.get("url") for m in into[0].cluster.members}
        into[0].cluster.members.extend(m for m in ms if (m.get("id") or m.get("url")) not in have)
        into[1] |= urls
        into[2].append(this)
    return [k[0] for k in kept]


def _feed_consensus(trends: list, budget_s: float = 45) -> dict:
    """The picture other outlets used, for several stories in parallel within a time budget."""
    from concurrent.futures import ThreadPoolExecutor, wait

    from .cli import story_head
    from .consensus import consensus_photos
    if not trends:
        return {}

    def one(t):
        head, since, own = story_head(sorted(t.cluster.members, key=lambda m: -int(m.get("engagement") or 0)))
        return consensus_photos(t.headline or head, since, own, deadline_s=10)
    ex = ThreadPoolExecutor(4)
    futs = {t.key: ex.submit(one, t) for t in trends}
    wait(list(futs.values()), timeout=budget_s)
    ex.shutdown(wait=False, cancel_futures=True)
    return {k: f.result() for k, f in futs.items() if f.done() and not f.cancelled() and not f.exception()}


def _image_rank(ph, subject: str, members: list[dict], listy: set, learned: dict | None = None) -> tuple:
    """Order of the Studio's candidates: the picture outlets shared, then clean over flagged, the
    subject's face (or a ranking post's photo that shows it) over an unknown one, a post or article
    naming the subject, usable size, then (ties only) the kind of photo editors picked before
    (bore/lessons.py), and otherwise the order found. (A face that may not be the subject goes last
    after the cut to six: _feed_images.)"""
    from .lessons import photo_weight
    surname = subject.split()[-1].lower() if subject else ""
    said = next((f"{m.get('title') or ''} {m.get('text') or ''}" for m in members if m.get("url") == ph.post_url), "")
    named = bool(surname) and surname in f"{said} {ph.via} {ph.post_url}".lower()
    matched = ph.id_match == "match"
    return (not (ph.source == "news" and ph.consensus >= 2),
            any(f.startswith(("soft", "not checked", "small")) for f in ph.flags),
            ph.post_url in listy and not matched, not matched, not named, min(ph.w, ph.h) < 600,
            -photo_weight(learned, ph))


def _feed_images(t, policy: str, images: list[dict], same: list | None = None, searches: dict | None = None,
                 learned: dict | None = None) -> list[dict]:
    """The Studio's 'AI selects an image' candidates for a story, checked: stills of the story's
    video first on celebrity/viral pages; the posts' photos and the lead photos of the articles they
    link to; no video frames on sports pages; no designed graphics, screenshots or scoreboards
    (OCR), no duplicates or tiny images; other outlets' photos of the subject when fewer than two
    good ones remain, then a photo search for the subject by name (searches["left"] per page per run),
    only for a person whose face is known, never on a crime or court story, keeping only photos of
    that face. Raises PhotoCheckFailed when the check itself fails."""
    from concurrent.futures import ThreadPoolExecutor

    from . import faceid
    from .consensus import consensus_photos
    from .media import dhash, fetch, graphic, story_photos, subject_of
    from .stills import video_stills
    from .textutil import domain
    members = sorted(t.cluster.members, key=lambda m: -int(m.get("engagement") or 0))
    subject = subject_of(members)
    # the subject's face (ESPN headshot / Wikipedia), looked up while the candidates download
    ref = faceid.start_reference(subject, policy == "never") if subject and faceid.available() else None
    since = min((m.get("created_at") or time.time()) for m in members)
    own = sorted({domain(m["url"]) for m in members if m.get("platform") == "web" and m.get("url")})

    def others():         # the picture other outlets used for this story (never raises)
        return same if same is not None else consensus_photos(t.headline, since, own, deadline_s=10)
    try:
        cands = (others() + video_stills(members, policy, frames=False, ocr=False, budget_s=6, min_short=600)
                 if policy == "prefer" else [])
        cands += story_photos(members, max_web=2, policy=policy, max_links=3)
    except Exception as e:       # a page or download that failed: these stay unchecked, tried next run
        from . import ops
        if ops.fault(e, "feed photos"):      # a package or model that does not load: the run fails
            raise
        print(f"  images for '{t.headline[:50]}' unchecked ({type(e).__name__})")
        raise PhotoCheckFailed(images, type(e).__name__) from e
    good, hashes, hashes_of = [], [], {}

    def vet(batch):
        with ThreadPoolExecutor(8) as ex:
            got = list(ex.map(lambda ph: ph if ph.data else fetch(ph), batch))
        for ph in got:
            if not ph or not ph.data or min(ph.w, ph.h) < 400:
                continue
            try:
                h = dhash(ph.data)
            except Exception:
                continue
            if any(bin(h ^ x).count("1") <= 8 for x in hashes):          # the same photo again
                continue
            hashes.append(h)
            hashes_of[id(ph)] = h
            # posts, Instagram, Reddit and other outlets carry designed cards, ads and screenshots;
            # the story's own article lead photos rarely do
            if ph.source != "web" or ph.note == "video frame":
                try:
                    if graphic(ph, sports=policy == "never"):
                        continue
                except Exception:
                    ph.flags.append("not checked")
            if min(ph.w, ph.h) < 800 and not (ph.source == "news" and ph.consensus >= 2):
                ph.flags.append("small: may look soft on a card")
            good.append(ph)

    vet(cands)
    if len(good) < 2 and policy != "prefer":
        vet(others())
    from .media import list_post
    if ref is not None and good:      # whose face each one shows (bore/faceid.py); never drops one
        try:
            faceid.label(good, subject, sports=policy == "never", deadline_s=FEED_FACES_S, ref=ref)
        except Exception as e:
            print(f"  faces for '{t.headline[:50]}' not compared ({type(e).__name__})")
    listy = {m.get("url") for m in members if list_post(m)}

    def shows(ph) -> bool:      # ranking / stat-list / roundup photos show someone else, unless the face is the subject's
        return ph.post_url not in listy or ph.id_match == "match"
    if sum(map(shows, good)) >= 2:
        good = [ph for ph in good if shows(ph)]
    # fewer than two photos that may show the subject: search for them by name, if the subject is
    # a person whose face is known (a phrase like 'On March' or 'Former Cornell' never is), never on
    # a crime or court story, and keep only the photos showing that face (one day's 37 search photos
    # on a celebrity page, none of them face-matched, under labels that read as checked)
    if (sum(shows(ph) and ph.id_match != "mismatch" for ph in good) < 2 and subject and searches
            and searches.get("left", 0) > 0 and not crime_story(t, members) and _known_face(ref)):
        searches["left"] -= 1                       # photos of the subject by name (checked by the search)
        from .photosearch import search_subject
        try:
            found, _ = search_subject(subject, subject, sports=policy == "never", floor=600, limit=3,
                                      deadline_s=8, official=False, commons=False, learned=learned)
        except Exception:
            found = []
        for ph in (ph for ph in found if ph.id_match == "match"):
            try:
                h = dhash(ph.data)
            except Exception:
                continue
            if not any(bin(h ^ x).count("1") <= 8 for x in hashes):
                hashes.append(h)
                hashes_of[id(ph)] = h
                ph.flags.append(f"photo of {subject} (search)")
                good.append(ph)
    # the best six (a 'may not be' face never costs a photo its place), then 'may not be' last among them
    good = sorted(good, key=lambda ph: _image_rank(ph, subject, members, listy, learned))[:6]
    good.sort(key=lambda ph: ph.id_match == "mismatch")
    outlets = [ph for ph in good if ph.source == "news" and ph.consensus >= 2]
    if outlets:           # the article's own picture, when other outlets used a different one
        from .consensus import picture_hash
        from .stills import ham
        for ph in good:
            try:
                if ph.source == "web" and not any(ham(picture_hash(ph.data), x.phash) <= 12 for x in outlets if x.phash):
                    ph.flags.append("other outlets used a different picture")
            except Exception:
                pass
    for ph in good:   # what the editor should know about each one
        ph.flags = [f for f in ph.flags if not f.startswith("not text-checked")][:4]
    return [{**image_out(ph), "_h": hashes_of.get(id(ph), 0)} for ph in good]


def _feed_faces(t, policy: str, images: list[dict]) -> list[dict]:
    """A story's images checked before face matching (FACELESS_V), kept as they are: whose face
    each shows is added and a 'may not be' moves last (a download and a face check, not the full
    check again)."""
    from concurrent.futures import ThreadPoolExecutor

    from . import faceid
    from .media import Photo, fetch, subject_of
    subject = subject_of(sorted(t.cluster.members, key=lambda m: -int(m.get("engagement") or 0)))
    if not (subject and images and faceid.available()):
        return images
    ref = faceid.start_reference(subject, policy == "never")
    phs = [Photo(i["url"], i.get("source") or "", i.get("credit") or "") for i in images]
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(lambda ph: fetch(ph, timeout=8), phs))
    try:
        faceid.label(phs, subject, sports=policy == "never", deadline_s=FEED_FACES_S, ref=ref)
    except Exception as e:
        print(f"  faces for '{t.headline[:50]}' not compared ({type(e).__name__})")
    out = [{**i, **id_fields(ph)} for i, ph in zip(images, phs)]
    return sorted(out, key=lambda i: i.get("id_match") == "mismatch")


def prepare_feed(run: Path, cfg: dict, store, dash=None, now: float | None = None) -> list[Path]:
    """Write work/<run>/feed/<page>.json for every live page. A story already in the current feed (the
    same posts: _inherit) keeps its written text or the writer's 'not news' verdict; the top
    FEED_WRITE others are marked needs_writing."""
    from .select import select_page
    dash = dash or _dash()
    now = now or time.time()
    meta = studio_meta()
    c2 = _copy.deepcopy(cfg)
    c2["clustering"]["window_hours"] = 24
    out = []
    for page in load_pages():
        if page.slug not in meta or not page.live:      # a page that is not live has no story feed
            continue
        res = select_page(page, store.page_items(page.slug, now - 24 * 3600), c2, [], now, quota=0)
        ranked = merge_same_stories(page, sorted(res.trends + res.watchlist, key=lambda t: -t.score))
        trends, seen = [], set()
        for hours, n in WINDOWS:             # best stories of the last 3h, then 12h, then 24h
            fresh = [t for t in ranked if t.key not in seen and t.cluster.last_seen >= now - hours * 3600][:n]
            seen.update(t.key for t in fresh)
            trends += fresh
        trends = trends[:FEED_MAX]
        try:      # (already tried again on network errors) never read as "no feed yet": that publishes
            old_list = (_get(dash, "feed", page=page.slug).get("feed") or {}).get("stories") or []
        except Exception as e:     # every story raw, the written text and photo checks lost for hours
            raise SystemExit(f"cannot read {page.slug}'s current feed ({type(e).__name__}: {e}): stopped so its "
                             "written stories are kept. Run `studio feed` again; if it fails twice, report it and stop.")
        old = {s["id"]: s for s in old_list}
        stories, todo = [], 0
        from . import lessons as _lessons
        from .stills import video_policy
        learned_rec = _lessons.load(page.slug)
        policy = video_policy(page)
        checked = 0.0
        searches = {"left": FEED_SEARCHES}
        same_map: dict = {}
        if policy == "prefer":         # the picture other outlets used, new stories in parallel
            old_ids = {o["id"] for o in old_list if o.get("images_v") in (IMAGES_V, FACELESS_V)}
            t0 = time.time()
            same_map = _feed_consensus([t for t in trends if t.key not in old_ids][:20], budget_s=45)
            checked += time.time() - t0
        built = []
        for t in trends:
            s = _story_from_trend(t, now, learned_rec, policy)
            s["written_from"] = member_urls(t)
            built.append(s)
        # the same story an hour ago (its posts, not its id: a trend id is a word signature that can
        # outlive its posts): its written text, the writer's 'not news' verdict, its checked photos
        prev_of, failed = _inherit(built, list(old.values())), 0
        for t, s in zip(trends, built):
            prev = prev_of.get(s["id"])
            if prev and prev.get("summary") and not _fits(prev.get("headline"), t.cluster.members):
                prev = None          # its headline names people these posts don't: another story's text
            judged = bool(prev and prev.get("not_news") and not prev.get("summary"))
            fresh = prev and prev.get("images_v") in (IMAGES_V, FACELESS_V) and (judged or not (
                len(prev.get("images") or []) < 2 and now - (prev.get("images_at") or 0) >= RECHECK_THIN_S))
            if fresh:            # checked earlier: reused as it is, even when every candidate failed
                s["images"], s["images_v"], s["images_at"] = prev.get("images") or [], prev["images_v"], prev.get("images_at") or now
                if prev["images_v"] == FACELESS_V and checked < FEED_IMAGE_BUDGET_S:     # add whose face each shows
                    t0 = time.time()
                    s["images"], s["images_v"] = _feed_faces(t, policy, s["images"]), IMAGES_V
                    checked += time.time() - t0
            elif judged:         # not news: no photo checks spent on it
                s["images"], s["images_v"] = prev.get("images") or s["images"], prev.get("images_v", 0)
            elif checked < FEED_IMAGE_BUDGET_S:
                t0 = time.time()
                try:
                    s["images"] = _feed_images(t, policy, s["images"], same_map.get(t.key), searches=searches,
                                               learned=learned_rec)
                    s["images_v"], s["images_at"] = IMAGES_V, now
                except PhotoCheckFailed as e:      # unchecked, so the next run checks them
                    s["images"], s["images_v"], s["images_at"] = e.images, 0, now
                    failed += 1
                checked += time.time() - t0
            else:                # next hour; until then editors see they are unchecked
                s["images"] = [{**i, "flags": ["not checked yet"] + list(i.get("flags") or [])[:3]} for i in s["images"]]
            if prev and prev.get("summary") and len(prev.get("ideas") or []) == 2:
                s.update(headline=prev["headline"], summary=prev["summary"], ideas=prev["ideas"])
                s["written_from"] = prev.get("written_from") or s["written_from"]
            elif judged:         # the writer judged it before: its writing slot goes to another story
                s["not_news"], s["written_from"] = prev["not_news"], prev.get("written_from") or s["written_from"]
            elif todo < FEED_WRITE:
                s["needs_writing"] = True
                todo += 1
            stories.append(s)
        # the same article image across stories is usually a site's placeholder, not a photo
        drop = {id(i) for st in stories for i in st.get("images") or [] if _placeholder(stories, i)}
        for st in stories:
            st["images"] = [{k: v for k, v in i.items() if k != "_h"} for i in st.get("images") or [] if id(i) not in drop]
        d = run / "feed"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{page.slug}.json"
        m = meta[page.slug]
        f.write_text(json.dumps({"page": page.slug, "page_name": page.name, "docs": m.get("docs") or {},
                                 "layouts": LAYOUT_DOC, "page_templates": _page_templates(page.slug),
                                 # the page's own templates its editors approved: the ideas name one of these
                                 "templates": m.get("templates") or [],
                                 "stories": stories}, indent=1, ensure_ascii=False))
        judged_n = sum(1 for x in stories if x.get("not_news"))
        print(f"FEED   {f}: {len(stories)} stories, {todo} to write" + (f", {judged_n} judged not news" if judged_n else "")
              + (f"; PHOTO CHECKS FAILED on {failed} (left unchecked, tried again next run)" if failed else ""))
        out.append(f)
    return out


def _page_templates(slug: str) -> list[str]:
    from .routine import load_playbook
    return list(((load_playbook(slug).get("card") or {}).get("templates")) or [])


def _clean_hint(s: str) -> str:
    s = re.sub(r"https?://\S+", "", s or "").strip().splitlines()[0] if (s or "").strip() else ""
    return s[:200]


def check_ideas(ideas, slug: str) -> list[str]:
    """Two ideas, each drawn with one of the page's approved templates (template_spec) or with a layout
    (the page's own designs or a generic one). The approved templates come first when one fits the
    story, but the layouts stay: while only some of the document's templates are approved, a story
    none of them suits is never forced into one."""
    errs = []
    allowed = set(GENERIC) | set(_page_templates(slug))
    specs = {t["id"]: t for t in approved_templates(slug)}
    if not isinstance(ideas, list) or len(ideas) != 2:
        return ["needs exactly 2 ideas"]
    for i, idea in enumerate(ideas, 1):
        idea = idea if isinstance(idea, dict) else {}
        spec_id = str(idea.get("template_spec") or "").strip()
        for k in ("title", "template", "description") + (() if spec_id else ("layout",)):
            if not str(idea.get(k) or "").strip():
                errs.append(f"idea {i} is missing {k}")
        if spec_id and spec_id not in specs:
            errs.append(f"idea {i}: template_spec must be one of the page's approved templates ({', '.join(sorted(specs))}), "
                        "or leave it out and pick a layout" if specs
                        else f"idea {i}: the page has no approved templates yet: leave template_spec out and pick a layout")
        if spec_id in specs and str(idea.get("template") or "").strip() != specs[spec_id]["name"]:
            errs.append(f"idea {i}: template must be the name of the template_spec it names (its name in `templates`)")
        if not spec_id and idea.get("layout") and idea["layout"] not in allowed:
            errs.append(f"idea {i}: layout '{idea['layout']}' must be one of {sorted(allowed)}")
    return errs


def publish_feed(run: Path, dash=None) -> dict:
    dash = dash or _dash()
    done = {}
    for f in sorted((run / "feed").glob("*.json")):
        data = json.loads(f.read_text())
        stories, problems = [], []
        for s in data["stories"]:
            written = s.get("summary") and not check_ideas(s.get("ideas"), data["page"])
            # the writer's verdict (TOPICS.md): remembered, so it is not judged again every hour,
            # and listed last in the Studio (score 0)
            not_news = "" if written else str(s.get("not_news") or "").strip()[:200]
            if s.get("needs_writing") and not written and not not_news:
                problems.append(f"{s['id']}: " + ("; ".join(check_ideas(s.get("ideas"), data["page"])) or "no summary"))
            stories.append({"id": s["id"], "headline": s.get("headline") or _clean_hint(s["headline_hint"]),
                            "summary": s.get("summary") or "", "sources": s["sources"], "published_at": s["published_at"],
                            "url": s["url"], "score": 0 if not_news else s["score"], "images": s["images"],
                            "images_v": s.get("images_v", 0), "images_at": s.get("images_at", 0),
                            "subject": s.get("subject", ""), "kind": s.get("kind", ""), "learned": s.get("learned", ""),
                            "ideas": s.get("ideas") if written else [],
                            "written_from": (s.get("written_from") or [])[:WRITTEN_FROM_MAX],
                            **({"not_news": not_news} if not_news else {})})
        _post(dash, "feed", {"page": data["page"], "stories": stories})
        done[data["page"]] = len(stories)
        judged = sum(1 for s in stories if s.get("not_news"))
        print(f"feed {data['page']}: {len(stories)} stories published"
              + (f", {judged} judged not news (listed last)" if judged else "")
              + (f"; {len(problems)} without ideas (the Studio asks for them on demand): " + " | ".join(problems[:5]) if problems else ""))
    return done


# ------------------------------------------------------------------ Studio jobs
COPY_DOC = {
    "template": "the layout to draw: one of the page's own templates (playbook card.templates) or a generic "
                "layout (see `layouts`). The editor's idea names a template from the page's templates document: "
                "pick the layout that draws it.",
    "items": "generic list layouts only (see `layouts`)",
    "extra_photo_indexes": "comparison / then_vs_now: the second photo (from photos)",
    "caption": "the Facebook caption in the page's tone (tone document + playbook), WITHOUT the hashtags",
    "hashtags": "list of hashtags for the last line (the page's habits)",
}
# copy.json with one of the page's approved templates (docs/TEMPLATE_SPEC.md), in every brief that lists them
TEMPLATE_COPY_DOC = {
    "template_spec": "one of the page's approved templates (`templates`: its id); the editor's idea names it in "
                     "studio.idea.template_spec, and then it is that one. The card shows only its slots",
    "slots": "with template_spec: the text of each slot of that template (its file: private/templates/<page>/<id>.json), "
             "{name: \"text\" | [\"line\", ...] | [{\"value\": \"..\", \"label\": \"..\"}, ...]} within each slot's rules; "
             "label, dek, stat_value, stat_label, footer and items are not used",
}


def _download(url: str, dest: Path, required: bool = False) -> tuple[int, int] | None:
    """Save the image at url as JPEG. None when it is not there or not an image (a reason about the
    image). A connection problem (timeout, dropped connection, server error) is tried 3 times; for an
    image the job cannot do without (`required`: the editor's base image, the card to check) it then
    raises, so the job is handed back instead of the editor being told their image is unusable."""
    import io
    import requests
    from PIL import Image
    from .media import MAX_SIDE, UA, flatten
    trouble = ""
    for attempt in range(3):
        if attempt:
            time.sleep(2 * attempt)
        try:
            r = requests.get(url, headers=UA, timeout=30)
        except requests.RequestException as e:
            trouble = f"{type(e).__name__}: {e}"
            continue
        if r.status_code >= 500 or r.status_code == 429:
            trouble = f"HTTP {r.status_code}"
            continue
        if r.status_code >= 400:
            return None
        try:
            im = flatten(Image.open(io.BytesIO(r.content)))
        except Exception:
            return None
        break
    else:
        if required:
            raise ConnectionError(f"could not download {url[:200]} ({trouble[:200]})")
        return None
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "JPEG", quality=95)
    return im.size


def _article_text(story: dict) -> str:
    from .linkpost import read_link
    if not story.get("url"):
        return ""
    try:
        a = read_link(story["url"])
        return "\n".join(x for x in (a.get("title"), a.get("text")) if x)
    except Exception:
        return ""


def write_job(job: dict, cfg: dict) -> Path:
    from .routine import COPY_SCHEMA_DOC, load_playbook
    if job["kind"] == "templates":
        return write_templates_job(job)
    if job["kind"] in ("ideas", "compose"):     # the templates and visual references pages, drawn here now
        for t in DOC_IMAGE_TYPES:
            try:
                doc_pages(job["page"], t)
            except Exception as e:     # the pictures help; the document's text is in the brief either way
                print(f"DOCS   {job['page']} {t}: its page pictures could not be drawn here ({type(e).__name__}); "
                      "read its text")
    meta = studio_meta().get(job["page"], {})
    templates = meta.get("templates") or []
    jd = WORK / job["id"]
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "job.json").write_text(json.dumps({k: v for k, v in job.items() if k != "posts"}, indent=1, ensure_ascii=False))
    for post in job["posts"]:
        d = jd / post["id"]
        (d / "photos").mkdir(parents=True, exist_ok=True)
        story = post["story"]
        photos, base_error = [], ""
        if job["kind"] in ("compose",) and post.get("base_image"):
            bi = post["base_image"]
            size = _download(bi["url"], d / "photos/0.jpg", required=True)
            if not size:
                base_error = "the base image the editor chose could not be downloaded or is not an image"
            else:
                photos.append({"index": 0, "file": "photos/0.jpg", "url": bi["url"], "source": bi.get("source") or "",
                               "credit": bi.get("credit") or "", "size": f"{size[0]}x{size[1]}"})
            for i, im in enumerate([x for x in (story.get("images") or []) + (post.get("extra_images") or [])
                                    if x.get("url") != bi["url"]][:4], start=1):
                sz = _download(im["url"], d / f"photos/{i}.jpg")
                if sz:
                    photos.append({"index": len(photos), "file": f"photos/{i}.jpg", "url": im["url"], "source": im.get("source") or "",
                                   "credit": im.get("credit") or "", "size": f"{sz[0]}x{sz[1]}"})
        if job["kind"] == "check" and post.get("final_image_url"):
            _download(post["final_image_url"], d / "card.jpg", required=True)
        text = _article_text(story) if job["kind"] in ("ideas", "compose", "check") else ""
        posts = [{"platform": "web", "author": (story.get("sources") or [{}])[0].get("name", ""),
                  "text": "\n".join(x for x in (story.get("headline"), story.get("summary"), text) if x),
                  "url": story.get("url") or ""}]
        brief = {
            "id": f"studio-{post['id']}", "page": job["page"], "page_name": meta.get("name") or job["page"],
            "rank": 0, "backup": False,
            "verification": {"route": "studio", "sources": [s.get("name") for s in story.get("sources") or []],
                             "platforms": ["web"], "engagement": 0, "developed": ""},
            "headline_hint": story.get("headline", ""), "posts": posts, "photos": photos,
            "playbook": load_playbook(job["page"]), "copy_schema": {**COPY_SCHEMA_DOC, **COPY_DOC, **(TEMPLATE_COPY_DOC if templates else {})},
            "layouts": LAYOUT_DOC, "page_templates": _page_templates(job["page"]),
            # the page's own templates its editors approved (template specs): ideas offer these, COMPOSE draws them
            **({"templates": templates} if templates else {}),
            "studio": {"job": job["id"], "kind": job["kind"], "post": post["id"], "batch": job["batch_id"],
                       "user": job.get("user"), "feedback": job.get("feedback") or "", "idea": post.get("idea"),
                       "story": story, "theme": meta.get("theme") or {}, "docs": meta.get("docs") or {},
                       "caption": post.get("caption") or "", "hashtags": post.get("hashtags") or [],
                       # the editor wrote or edited the caption: a visual made again keeps theirs
                       "caption_kept": bool(post.get("caption_edited_at")),
                       "previous_image_url": post.get("final_image_url") or "", "base_image": post.get("base_image"),
                       "base_image_error": base_error,
                       "summary_needed": summary_cut(story.get("summary", "")) or bool(story.get("from_url"))},
        }
        (d / "brief.json").write_text(json.dumps(brief, indent=1, ensure_ascii=False))
        if job["kind"] == "check":
            (d / "review_request.md").write_text(review_request(d, brief))
    return jd


TEMPLATE_PARTS = 4         # document pages one template is drawn on (the dashboard takes up to 4)


def write_templates_job(job: dict) -> Path:
    """A templates job (STUDIO.md TEMPLATES): convert the page's templates document into template
    specs. The brief gives the document (its text and the page images `studio sync` drew), the
    templates converted so far (each one's newest spec in _current/<id>.json, a rejected one's note)
    and what to convert: every template, or the ones the editors rejected. The session writes one
    folder per template: <job>/<id>/spec.json, source.json and preview.png."""
    try:      # the document's pages, drawn here now (the only job that needs them)
        doc = doc_pages(job["page"], "templates")
    except Exception as e:
        from . import ops
        ops.fault(e, "reference page images")
        # not the editors' problem (a package, a download): the job is handed back
        raise RuntimeError("the templates document's pages could not be drawn here "
                           f"({' '.join(f'{type(e).__name__}: {e}'.split())[:200]}): run `studio jobs` again")
    meta = studio_meta().get(job["page"]) or {}
    jd = WORK / job["id"]
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "job.json").write_text(json.dumps({k: v for k, v in job.items() if k not in ("posts", "templates")}, indent=1,
                                            ensure_ascii=False))
    cur = jd / "_current"
    shutil.rmtree(cur, ignore_errors=True)
    cur.mkdir()
    known = []
    for t in job.get("templates") or []:
        f = cur / f"{t['id']}.json"
        f.write_text(json.dumps(t.get("spec") or {}, indent=1, ensure_ascii=False))
        known.append({**{k: t.get(k) for k in ("id", "name", "status", "in_use", "note", "v", "doc_version", "doc_pages")},
                      "spec": str(f.relative_to(ROOT)) if f.is_relative_to(ROOT) else str(f)})
    rel = str(jd.relative_to(ROOT)) if jd.is_relative_to(ROOT) else str(jd)
    only = job.get("only")
    brief = {
        "kind": "templates", "job": job["id"], "page": job["page"], "page_name": meta.get("name") or job["page"],
        "theme": meta.get("theme") or {},
        # the active document as synced (a newer upload since the job was queued: that one)
        "doc": ({"version": doc.get("version"), "text": doc.get("file"), "pages": doc.get("pages"),
                 "images": doc.get("images") or [], "image_pages": doc.get("image_pages")} if doc else None),
        "convert": "all" if only is None else list(only),
        "notes": job.get("notes") or {},
        "templates": known,
        # a template new to this page takes the next id no template of it ever had (never its place in
        # the document: a template added in the middle would take another one's id, and its versions)
        "new_id": "t%d" % (1 + max((int(m[1]) for t in known if (m := re.fullmatch(r"t(\d+)", str(t["id"])))), default=0)),
        "spec_doc": "docs/TEMPLATE_SPEC.md",
        "folder": f"{rel}/<id>/: spec.json (its id is the folder's name), source.json {{\"pages\": [the 1-{TEMPLATE_PARTS} "
                  "document page numbers it was made from]}, preview.png",
        "preview": f"python -m bore.cli routine tspec-render {rel}/<id>/spec.json --sample --page {job['page']} --out {rel}/<id>/preview.png",
        "send": f"python -m bore.cli studio template-send {rel}/<id>",
    }
    (jd / "brief.json").write_text(json.dumps(brief, indent=1, ensure_ascii=False))
    return jd


def is_template_dir(d: Path) -> bool:
    """A template's folder in a templates job (work/studio/<job>/<id>)."""
    try:
        return not d.name.startswith("_") and json.loads((d.parent / "job.json").read_text()).get("kind") == "templates"
    except (OSError, ValueError):
        return False


def _template_dirs(jd: Path) -> list[Path]:
    return [d for d in sorted(p for p in jd.iterdir() if p.is_dir()) if not d.name.startswith("_")]


def _jpeg(src: Path, side: int, quality: int) -> bytes:
    import io
    from PIL import Image
    im = Image.open(src).convert("RGB")
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


TEMPLATE_IMAGE_BYTES = 1_400_000     # each picture (the dashboard takes 1.5 MB)
TEMPLATE_SEND_BYTES = 3_100_000      # all of them in one request: 4.5 MB once base64


def _template_pictures(preview: Path, pages: list[Path]) -> tuple[bytes, list[bytes]]:
    """The preview and the document pages as JPEGs small enough for one request, sharpest first."""
    for side, q in ((1400, 85), (1400, 75), (1200, 70), (1000, 65), (900, 55)):
        pics = [_jpeg(preview, min(side, 1350), q)] + [_jpeg(f, side, q) for f in pages]
        if max(map(len, pics)) <= TEMPLATE_IMAGE_BYTES and sum(map(len, pics)) <= TEMPLATE_SEND_BYTES:
            break
    return pics[0], pics[1:]


def send_template(d: Path, dash=None) -> dict:
    """One converted template (STUDIO.md TEMPLATES): the spec checked as the renderer reads it, its
    preview drawn from the current spec, the document pages it came from; sent to the dashboard as a
    version waiting for the editors (they see it at once under Templates). Sending it again unchanged
    sends nothing. Each one sent also tells the dashboard the job's session is still working."""
    from . import tspec
    jd = d.parent
    if not is_template_dir(d):
        raise SystemExit(f"{d} is not a template folder of a templates job (work/studio/<job>/<id>)")
    job, brief = json.loads((jd / "job.json").read_text()), json.loads((jd / "brief.json").read_text())
    f = d / "spec.json"
    try:
        raw = json.loads(f.read_text())
        spec = tspec.load(raw)
    except OSError:
        raise ValueError("spec.json is missing (docs/TEMPLATE_SPEC.md)")
    except ValueError as e:          # not JSON, or a spec that does not validate (every problem listed)
        raise ValueError(f"spec.json: {'; '.join(str(e).splitlines())}")
    if spec["id"] != d.name:
        raise ValueError(f"spec.json's id is '{spec['id']}': the folder must be named after it")
    prev = d / "preview.png"
    drawn_from = max(f.stat().st_mtime, (d / "copy.json").stat().st_mtime if (d / "copy.json").exists() else 0)
    if not prev.exists() or prev.stat().st_mtime < drawn_from:
        raise ValueError(f"draw the preview from the current spec.json and compare it with the page(s): {brief['preview'].replace('<id>', d.name)}")
    doc = brief.get("doc") or {}
    images = {int(m.group(1)): ROOT / x for x in doc.get("images") or [] if (m := re.search(r"page-(\d+)\.png$", x))}
    try:
        pages = json.loads((d / "source.json").read_text()).get("pages")
    except (OSError, ValueError, AttributeError):
        pages = None
    if not (isinstance(pages, list) and 1 <= len(pages) <= TEMPLATE_PARTS and all(isinstance(n, int) and n in images for n in pages)):
        raise ValueError(f'source.json must be {{"pages": [...]}}: the 1-{TEMPLATE_PARTS} pages of the document this template '
                         f"was made from (page-NN.png: 1-{max(images, default=0)})")
    digest = hashlib.sha256(f.read_bytes() + prev.read_bytes() + json.dumps(pages).encode()).hexdigest()
    marker = d / "sent.json"
    try:
        was = json.loads(marker.read_text())
    except (OSError, ValueError):
        was = {}
    if was.get("sha") == digest:
        print(f"SENT   {d.name} v{was.get('v')} already (unchanged)")
        return was
    preview, pics = _template_pictures(prev, [images[n] for n in pages])
    r = _post(dash or _dash(), "templates", {
        "page": job["page"], "job": job["id"], "doc_version": doc.get("version"), "doc_pages": pages, "spec": raw,
        "preview_b64": base64.b64encode(preview).decode(),
        "pages": [{"n": n, "image_b64": base64.b64encode(b).decode()} for n, b in zip(pages, pics)]})
    marker.write_text(json.dumps({"sha": digest, "v": r.get("v"), "at": time.time()}))
    print(f"SENT   {d.name} v{r.get('v')}: waiting for the editors' approval (Templates)")
    return r


def finish_templates(jd: Path, dash=None, fail_reason: str = "") -> dict:
    """`studio done` for a templates job: every template folder not sent yet is sent (or its fail.txt
    reported), every template the editors rejected must have been converted again (or failed with a
    reason), then the job is done (failed when not one template could be converted)."""
    dash = dash or _dash()
    job, brief = json.loads((jd / "job.json").read_text()), json.loads((jd / "brief.json").read_text())
    if fail_reason:
        r = _post(dash, f"jobs/{job['id']}", {"status": "failed", "error": fail_reason, "posts": {}})
        print(f"job {job['id']} failed: {fail_reason}")
        return r
    sent, failed, errors = [], {}, []
    for d in _template_dirs(jd):
        reason = (d / "fail.txt").read_text().strip() if (d / "fail.txt").exists() else ""
        if reason:
            failed[d.name] = reason[:300]
            continue
        try:
            send_template(d, dash)
            sent.append(d.name)
        except ValueError as e:
            errors.append(f"{d.name}: {e}")
    redo = brief.get("convert") if isinstance(brief.get("convert"), list) else []
    left = [t for t in redo if t not in sent and t not in failed]
    if left:
        errors.append(f"{', '.join(left)}: rejected by the editors, not converted again: convert each with its note "
                      "(brief.json notes), or say why it cannot be with `studio fail <job>/<id> --reason ...`")
    if not (sent or failed or errors):
        errors.append("no template folders yet: write <job>/<id>/spec.json, source.json and preview.png for each template "
                      "(STUDIO.md TEMPLATES)")
    if errors:
        raise SystemExit("NOT DONE:\n  " + "\n  ".join(errors))
    status = "done" if sent else "failed"
    error = "" if sent else ("no template could be converted: " + "; ".join(failed.values()))[:600]
    r = _post(dash, f"jobs/{job['id']}", {"status": status, "error": error, "posts": {}, "templates": {"sent": sent, "failed": failed}})
    try:
        _post(dash, "usage", {"entries": [{"tool": "claude_routine", "units": len(sent) + len(failed), "unit": "template",
                                           "detail": job["id"], "when": int(time.time() * 1000), "page": job["page"],
                                           "user": job.get("user") or ""}]})
    except Exception as e:
        print(f"usage log failed: {e}")
    print(f"job {job['id']} {status}: {len(sent)} template(s) sent for the editors' approval"
          + (f", {len(failed)} could not be converted" if failed else ""))
    return r


def summary_cut(text: str) -> bool:
    """No summary, or one that stops mid-sentence (sites cut their preview text at ~160 characters)."""
    t = (text or "").strip()
    if not t or t.endswith(("…", "...", "[…]", "[...]")):
        return True
    last = t.rstrip("\ufe0f")[-1:]
    return not (re.search(r"[.!?\"'”’)\]।。！？]$", t) or (last and unicodedata.category(last) == "So"))   # an emoji ends it too


CHECK_PROMPT = ("Check this image post and caption for (a) factual correctness (b) relevance at the exact time "
                "right now (c) grammatical errors/ typos (d) any other glaring errors fans may point out "
                "(e) facebook guideline violations.")


def review_request(d: Path, brief: dict, caption: str | None = None) -> str:
    """What the independent reviewer gets: the finished post and nothing about how it was made."""
    st = brief["studio"]
    now = time.strftime("%A %d %B %Y, %H:%M UTC", time.gmtime())
    if caption is None:
        caption = "\n".join(x for x in (st.get("caption", "").strip(), " ".join(st.get("hashtags") or [])) if x)
    return f"""You are an independent reviewer for a Facebook page's post. You did not make it.

{CHECK_PROMPT}

It is now {now}. Use web search to confirm the facts and whether the story is still current
(for example: has the news moved on, was it denied, is the game already played).
Be quick, an editor is waiting: check what matters (names, numbers, dates, quotes, whether it is
still current) with at most 6 searches, and answer within about two minutes.

The image: {d / "card.jpg"} (open it and read every word on it).
The Facebook page: {brief.get("page_name")}.
The caption, exactly as it will be posted:
---
{caption}
---

Answer with JSON only, five lists of short findings (an empty list means no issues in that category):
{{"factual": [], "timing": [], "grammar": [], "other": [], "guidelines": []}}
Each finding names the exact problem and, where useful, the fix and the source you checked.
"""


def _session_id() -> str:
    """This machine's id for the dashboard (one per routine session: each runs in a fresh container).
    A session that leaves a job, repairs, and leaves it again is one machine, not three."""
    f = WORK / ".session"
    try:
        sid = f.read_text().strip()
    except OSError:
        sid = ""
    if not re.fullmatch(r"[0-9a-f]{12}", sid):
        sid = secrets.token_hex(6)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(sid)
        except OSError:
            pass
    return sid


def pull_jobs(cfg: dict, dash=None, limit: int = 1) -> list[Path]:
    """Claim the oldest waiting job(s): one per call, so the sessions every editor click starts
    share the work, and a session that dies leaves at most one job behind (offered again later).
    A job whose packages do not load on this machine is left for another session; one that could
    not be prepared after its claim is handed back (the dashboard fails it after 3 tries)."""
    import traceback
    from .deps import NEEDS, problems
    dash = dash or _dash()
    out, left, handed = [], {}, []
    for job in _get(dash, "jobs").get("jobs") or []:
        if len(out) >= limit:
            break
        bad = problems(NEEDS.get(job["kind"], NEEDS["compose"]))   # checked before claiming
        if bad:
            left[job["id"]] = "; ".join(f"{k}: {v}" for k, v in bad.items())
            print(f"LEAVE  {job['id']} ({job['kind']}): packages not loading on this machine")
            try:      # the dashboard tells the editor when no machine can take it (3 such sessions)
                _post(dash, f"jobs/{job['id']}", {"left": True, "error": f"packages not loading: {left[job['id']]}"[:600],
                                                  "session": _session_id()})
            except Exception:
                pass
            continue
        try:      # one session per job: several start at once when editors click quickly
            _post(dash, f"jobs/{job['id']}", {"status": "running"})
        except RuntimeError as e:
            if "HTTP 409" in str(e):
                print(f"SKIP   {job['id']}: another Studio session has it")
                continue
            raise
        try:
            jd = write_job(job, cfg)
        except Exception as e:
            # not the editor's problem: hand the job back (claimable at once), go on with the next
            traceback.print_exc()
            why = f"{type(e).__name__}: {e}"
            try:
                r = _post(dash, f"jobs/{job['id']}", {"status": "queued", "release": True, "error": why[:600]})
                handed.append(f"{job['id']}: {why} (" + {
                    "queued": "back in the queue",
                    "failed": "failed: the third try, the editor was told; do not retry it"}.get(
                    r.get("status"), f"WARNING: unexpected answer {r}: check the job on the dashboard") + ")")
            except Exception as e2:
                handed.append(f"{job['id']}: {why} (handing it back failed too: {e2}; offered again in 20 minutes)")
            print(f"BACK   {handed[-1]}")
            continue
        out.append(jd)
        idea = lambda p: (p.get("idea") or {}).get("title") or (p.get("idea") or {}).get("text", "")[:60]
        print(f"JOB    {jd.relative_to(ROOT)}  {job['kind'].upper()}  page {job['page']}"
              + (f"  editor note: {job['feedback']}" if job.get("feedback") else ""))
        for p in job["posts"]:
            print(f"       {p['id']}: {p['story']['headline'][:80]}" + (f"  [idea: {idea(p)}]" if p.get("idea") else ""))
        if job["kind"] == "templates":      # (no names here: they are the confidential document's)
            only = job.get("only")
            print(f"       convert {'every template of the document' if only is None else f'{len(only)} rejected template(s) again, with the editors notes'}"
                  f": {jd.relative_to(ROOT)}/brief.json (STUDIO.md TEMPLATES)")
    if not out and (left or handed):
        raise SystemExit("no job taken. " + " ".join(
            ([f"Packages not loading ({'; '.join(sorted(set(left.values())))}): run `bash scripts/ensure-deps.sh`."] if left else [])
            + ([f"Could not prepare {'; '.join(handed)}: fix the problem (packages: `bash scripts/ensure-deps.sh`)."] if handed else []))
            + " Then run `studio jobs` again.")
    if not out:
        print("no Studio jobs waiting")
    return out


def image_search(d: Path, query: str, must: list[str] | None, cfg: dict) -> list[dict]:
    """Photos for an editor's search: the picture other outlets used for this story, then photos of
    the subject (news, DuckDuckGo, Bing, Wikimedia; bore/photosearch.py), all checked. Each is also
    saved to <post>/search/ for the session to look at. Adds to earlier results. (No NFL.com
    cutout headshots: the editor picks a URL, and a transparent cutout would not draw as shown.)"""
    from .consensus import consensus_photos
    from .media import best, dhash, graphic
    from .photosearch import print_report, search_subject, subject_name
    from .stills import video_policy
    from .textutil import domain
    try:
        brief = json.loads((d / "brief.json").read_text())
    except (OSError, ValueError):
        brief = {}
    story = (brief.get("studio") or {}).get("story") or {}
    page = next((p for p in load_pages() if p.slug == brief.get("page")), None)
    sports = bool(page) and video_policy(page) == "never"
    since = (story.get("first_seen") or story.get("published_at") or time.time() * 1000) / 1000
    own = sorted({domain(x["url"]) for x in story.get("sources") or [] if x.get("url")})
    same = [ph for ph in best(consensus_photos(story.get("headline") or query, since, own, query=query, deadline_s=15, ocr=True),
                              600, limit=3, keep_order=True)
            if not graphic(ph, sports=sports)]
    from .lessons import load as load_lessons
    name = subject_name(query, " ".join(must or []))
    subject, report = search_subject(query, name, sports=sports, floor=600, limit=6, deadline_s=15, official=False,
                                     learned=load_lessons(brief.get("page") or ""))
    if same:          # the story's picture too: whose face (the subject's face is cached by the search)
        from .faceid import label
        report.append(label(same, name, sports, deadline_s=4))
    print_report(report)
    (d / "images_report.json").write_text(json.dumps(report, indent=1))
    f = d / "images.json"
    have = json.loads(f.read_text()) if f.exists() else []
    urls = {x["url"] for x in have}
    hashes = []
    (d / "search").mkdir(exist_ok=True)
    from .faceid import id_rank
    for ph in sorted(same + subject, key=id_rank):        # the subject's face first, 'may not be' last
        if ph.url in urls or not ph.data:
            continue
        h = dhash(ph.data)
        if any(bin(h ^ x).count("1") <= 8 for x in hashes):
            continue
        hashes.append(h)
        n = len(have)
        (d / "search" / f"{n}.jpg").write_bytes(ph.data)
        have.append({**image_out(ph), "file": f"search/{n}.jpg"})
    f.write_text(json.dumps(have, indent=1))
    _usage(d, "image_search", 1, "search", query[:120])
    return have


def _usage(d: Path, tool: str, units: float, unit: str, detail: str = "", cost: float = 0.0) -> None:
    f = d.parent / "usage.json"
    u = json.loads(f.read_text()) if f.exists() else []
    u.append({"tool": tool, "units": units, "unit": unit, "detail": detail, "cost_usd": cost, "when": int(time.time() * 1000)})
    f.write_text(json.dumps(u, indent=1))


def _tags(raw) -> list[str]:
    """Hashtags as the dashboard keeps them (dashboard/lib/caption.js routineTags), so the reviewer
    is given exactly the text that will be posted."""
    tags = [str(t).strip() for t in raw or []]
    return [(t if t.startswith("#") else f"#{t}")[:60] for t in tags if t][:30]


def _split_caption(copy: dict) -> tuple[str, list[str]]:
    caption = (copy.get("caption") or "").strip()
    tags = _tags(copy.get("hashtags"))
    lines = caption.splitlines()
    if lines and re.fullmatch(r"(\s*#\w+)+\s*", lines[-1]):       # hashtags written on the last line
        tags = tags or _tags(re.findall(r"#\w+", lines[-1]))
        caption = "\n".join(lines[:-1]).strip()
    return caption, tags


def post_result(d: Path, kind: str) -> dict:
    """What one post's folder produced, checked like `routine upload` checks a draft."""
    out = {}
    if kind == "ideas":
        ideas = json.loads((d / "ideas.json").read_text()) if (d / "ideas.json").exists() else None
        brief = json.loads((d / "brief.json").read_text())
        errs = check_ideas(ideas, brief["page"])
        if errs:
            raise ValueError("ideas.json: " + "; ".join(errs))
        out["ideas"] = ideas
        if (d / "summary.json").exists():
            out.update({k: v for k, v in json.loads((d / "summary.json").read_text()).items() if k in ("summary", "headline")})
        if (brief.get("studio") or {}).get("summary_needed") and summary_cut(out.get("summary", "")):
            raise ValueError("summary.json: the story's summary is missing or stops mid-sentence; write "
                             '{"headline": "...", "summary": "2-3 complete lines"} from what the article says')
    elif kind == "images":
        imgs = json.loads((d / "images.json").read_text()) if (d / "images.json").exists() else []
        if not imgs:
            raise ValueError("no images found: run `studio images` with another query, or fail the job with a reason")
        out["images"] = imgs[:8]
    elif kind == "compose":
        for f in ("copy.json", "lint.json", "card.jpg", "ocr.json", "verdict.json"):
            if not (d / f).exists():
                raise ValueError(f"{f} is missing: lint, render, verify and look at the card first (STUDIO.md)")
        card_t = (d / "card.jpg").stat().st_mtime
        ocr = json.loads((d / "ocr.json").read_text())
        verdict = json.loads((d / "verdict.json").read_text())
        if not json.loads((d / "lint.json").read_text()).get("pass"):
            raise ValueError("lint has not passed")
        if not ocr.get("pass") or (d / "ocr.json").stat().st_mtime < card_t:
            raise ValueError("verify has not passed on the current card")
        if ocr.get("warnings") and not verdict.get("warnings_checked"):
            raise ValueError("verify reported warnings: record your finding per warning in verdict.json warnings_checked")
        if not verdict.get("pass") or (d / "verdict.json").stat().st_mtime < card_t:
            raise ValueError("verdict.json (written after the current card) must say pass")
        copy = json.loads((d / "copy.json").read_text())
        brief = json.loads((d / "brief.json").read_text())
        base = ((brief.get("studio") or {}).get("base_image") or {}).get("url")
        photos = brief.get("photos") or []
        # the editor picked the base image: the card must be built on exactly that photo
        if base and (copy.get("photo_index") != 0 or not photos or photos[0].get("url") != base):
            raise ValueError("the card must use the image the editor chose (photo_index 0); never swap it. "
                             "If it cannot be used, fail the post with `studio fail` and say why")
        # the editor chose one of the page's own templates: the card is drawn with exactly that one
        idea = (brief.get("studio") or {}).get("idea") or {}
        if idea.get("template_spec") and copy.get("template_spec") != idea["template_spec"]:
            raise ValueError("the editor chose another of the page's templates: copy.json must draw it "
                             f"(\"template_spec\": \"{idea['template_spec']}\" with its slots), never another one. If it cannot be "
                             "drawn for this story or photo, fail the post with `studio fail` and say why")
        caption, tags = _split_caption(copy)
        if base:      # the photo it was built on: the dashboard drops a card made from a photo the editor replaced
            out["photo"] = base
        out.update(image_b64=base64.b64encode((d / "card.jpg").read_bytes()).decode(), caption=caption, hashtags=tags,
                   card={**{k: copy.get(k) for k in ("template", "headline_lines", "label", "dek", "stat_value", "stat_label", "items")},
                         **{k: copy[k] for k in ("template_spec", "slots") if copy.get(k)}})
    elif kind == "check":
        chk = json.loads((d / "check.json").read_text()) if (d / "check.json").exists() else None
        cats = ("factual", "timing", "grammar", "other", "guidelines")
        if not isinstance(chk, dict) or any(not isinstance(chk.get(c), list) for c in cats):
            raise ValueError(f"check.json needs the five lists {cats} (empty list = no issues)")
        out["check"] = {c: [str(x)[:500] for x in chk[c]][:10] for c in cats}
        # the caption the reviewer was given: the dashboard shows the check as out of date if the
        # editor has changed it since
        checked = _checked_caption(d / "review_request.md")
        if checked is not None:
            out["checked_caption"] = checked
    return out


def _checked_caption(f: Path) -> str | None:
    """The caption exactly as review_request() wrote it into the reviewer's request."""
    try:
        text = f.read_text()
    except OSError:
        return None
    head, tail = "The caption, exactly as it will be posted:\n---\n", "\n---\n\nAnswer with JSON only"
    start, end = text.find(head), text.rfind(tail)
    return text[start + len(head):end] if 0 <= start and start + len(head) <= end else None


def precheck(jd: Path, send: bool = False, dash=None) -> list[Path]:
    """Right after a COMPOSE job is done, check its posts in the same session, so editors don't
    wait for a separate check to start. Without --send: write each post's review_request.md (the
    caption as composed). With --send: send each post's check.json; the dashboard marks it stale
    if the editor has changed the caption meanwhile."""
    job = json.loads((jd / "job.json").read_text())
    if job["kind"] != "compose":
        raise SystemExit("precheck is for COMPOSE jobs (after `studio done`)")
    dirs = [d for d in sorted(p for p in jd.iterdir() if p.is_dir()) if (d / "copy.json").exists() and (d / "card.jpg").exists()
            and not (d / "fail.txt").exists()]
    if not send:
        for d in dirs:
            brief = json.loads((d / "brief.json").read_text())
            cap, tags = _split_caption(json.loads((d / "copy.json").read_text()))
            text = "\n".join(x for x in (cap.strip(), " ".join(tags)) if x)
            # the caption that will be posted: the editor's own when they wrote it (the visual's
            # caption is only offered to them), else the one just composed
            kept = (brief.get("studio") or {}).get("caption_kept")
            (d / "review_request.md").write_text(review_request(d, brief, caption=None if kept else text))
            print(f"REVIEW {d / 'review_request.md'}")
        return dirs
    posts = {}
    for d in dirs:
        if not (d / "check.json").exists():
            continue
        posts[d.name] = post_result(d, "check")
    if posts:
        _post(dash or _dash(), f"jobs/{job['id']}", {"precheck": True, "posts": posts})   # the checks only
    print(f"pre-check sent for {len(posts)} post(s)")
    return dirs


def finish_job(jd: Path, dash=None, fail_reason: str = "") -> dict:
    dash = dash or _dash()
    job = json.loads((jd / "job.json").read_text())
    if job["kind"] == "templates":
        return finish_templates(jd, dash, fail_reason)
    posts, errors = {}, []
    for d in sorted(p for p in jd.iterdir() if p.is_dir()):
        if fail_reason:
            posts[d.name] = {"error": fail_reason}
            continue
        reason = (d / "fail.txt").read_text().strip() if (d / "fail.txt").exists() else ""
        if reason:
            posts[d.name] = {"error": reason}
            continue
        try:
            posts[d.name] = post_result(d, job["kind"])
        except ValueError as e:
            errors.append(f"{d.name}: {e}")
    if errors:
        raise SystemExit("NOT DONE:\n  " + "\n  ".join(errors)
                         + "\n(fix them, or give up on a post with `studio fail <post dir> --reason ...`)")
    status = "failed" if fail_reason or all("error" in p for p in posts.values()) else "done"
    r = _post(dash, f"jobs/{job['id']}", {"status": status, "error": fail_reason, "posts": posts})
    usage = json.loads((jd / "usage.json").read_text()) if (jd / "usage.json").exists() else []
    n_cards = sum(1 for p in posts.values() if "image_b64" in p)
    usage += [{"tool": "claude_routine", "units": len(posts), "unit": f"{job['kind']} post", "detail": job["id"],
               "when": int(time.time() * 1000)}]
    for d in jd.iterdir():
        att = d / "attempts.json"
        if att.exists():
            for a in json.loads(att.read_text()):
                if a.get("engine") in ("krea", "krea-art", "krea-premium"):
                    usage.append({"tool": "krea", "units": 1, "unit": "image", "detail": a.get("model", ""), "when": int(a["at"] * 1000)})
    try:
        _post(dash, "usage", {"entries": [{**u, "page": job["page"], "user": job.get("user") or ""} for u in usage]})
    except Exception as e:
        print(f"usage log failed: {e}")
    print(f"job {job['id']} {status}: {len(posts)} post(s), {n_cards} card(s)")
    return r


__all__ = ["sync", "prepare_feed", "publish_feed", "pull_jobs", "finish_job", "precheck", "image_search",
           "page_from_config", "sources_from_page", "classify", "check_ideas", "post_result", "LAYOUT_DOC",
           "approved_templates", "send_template", "write_templates_job", "is_template_dir"]
