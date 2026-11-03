"""Guardrails for post creation. Deterministic gates that run around every infographic:

  lint(brief, copy)     BEFORE generation: the card text and caption are checked against the
                        source posts (hedges kept, numbers sourced, nothing repeated, lengths,
                        attribution, hashtags, photo quality). Krea/template refuse to run
                        until it passes.
  verify(card, brief, copy)  AFTER generation: OCR reads the finished card and compares it with
                        the approved text: every line present and spelled right, no extra text,
                        no repeated numbers. Upload refuses until it passes.

Each returns {"pass": bool, "errors": [...], "warnings": [...]}. Errors block; warnings must be
acknowledged by the routine session in verdict.json.
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher

# --- wording -------------------------------------------------------------------------------
UNCERTAIN = re.compile(
    r"\b(likely|unlikely|appears?|expected|expects|reportedly|reports?|could|may|might|possibly|"
    r"doubtful|questionable|trending|considering|in talks|discussions?|rumou?red|believed?|"
    r"not yet|pending|potential(ly)?|possibility|plans? to|set to|hopes? to|aims? to|would|can)\b", re.I)
DEFINITIVE = re.compile(
    r"\b(OUT|RULED OUT|SIGNS|SIGNED|TRADED|TRADES|RELEASED|CUT|FIRED|RETIRES|RETIRED|DONE|CONFIRMED|"
    r"DEAD|DIES|SPLIT|DIVORCE[DS]?|ENGAGED|MARRIED|ARRESTED|CHARGED|SUSPENDED|BANNED|CANCELL?ED|"
    r"LEAVING|LEAVES|JOINS|RETURN|RETURNS|RETURNING|BACK|WILL|HE'LL|SHE'LL|THEY'LL|WILL PLAY|STARTING|BENCHED|"
    r"OFFICIAL|AVOIDS|AVOIDED|SKIPS|MISSES|MISSING|CLEARED|HEALTHY|DONE FOR)\b")
HEDGE_ON_CARD = re.compile(
    r"\b(LIKELY|UNLIKELY|EXPECTED|EXPECTS|REPORTEDLY|DOUBTFUL|QUESTIONABLE|COULD|MAY|MIGHT|POSSIBLE|"
    r"POSSIBLY|SET TO|IN TALKS|RUMOU?RS?|RUMOU?RED|NEARING|CLOSE TO|EYEING|WEIGHING|CONSIDERING|FOR NOW|CAN|AIMS|HOPES|"
    r"PLANS|TRYING|TRIES|"
    r"\?)", re.I)
ATTRIBUTION = re.compile(r"\b(per|according to|reportedly|reports?|sources?|told|says|said)\b", re.I)
NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(\s*(?:million|billion|thousand|[mMkKbB])\b|%)?")
MULT = {"million": 1e6, "m": 1e6, "billion": 1e9, "b": 1e9, "thousand": 1e3, "k": 1e3}

CARD_FIELDS = ["label", "headline_lines", "dek", "stat_label", "stat_value", "footer", "footer_sub"]
LIMITS = {  # characters, per template family
    "headline_line": 22, "quote_line": 30, "dek": 64, "label": 22, "stat": 40, "footer": 36, "words": 26,
}
BRAND_WORDS = {"culture-rush": {"CULTURE", "RUSH"}, "huddle": set()}


def _logo_text(brief: dict, copy: dict | None) -> str:
    """The theme's logo text the card draws: the Studio brief's theme, or (a template spec on an hourly
    brief, which carries none) the page's theme as `studio sync` stored it, which the renderer draws."""
    logo = ((brief.get("studio") or {}).get("theme") or {}).get("logo_text") or ""
    if not logo and (copy or {}).get("template_spec") and not (brief.get("studio") or {}).get("theme"):
        from .studio import studio_meta
        logo = ((studio_meta().get(brief.get("page") or "") or {}).get("theme") or {}).get("logo_text") or ""
    return logo


def brand_words(brief: dict, copy: dict | None = None) -> set[str]:
    """Text the layout itself adds: the page logo (playbook logo_words, Studio theme logo text)
    and the generic layouts' fixed marks."""
    out = set(BRAND_WORDS.get(brief.get("page"), set()))
    out |= {w.upper() for w in ((brief.get("playbook") or {}).get("logo_words") or [])}
    out |= {w for w in re.split(r"\s+", _logo_text(brief, copy).upper()) if w}
    if (copy or {}).get("template_spec"):      # a template spec's fixed words ("VS") come with the layout
        from .tspec import SpecError, find, static_words
        try:
            out |= static_words(find(copy["template_spec"], brief))
        except SpecError:
            pass                               # lint names the problem
    tpl = (copy or {}).get("template")
    if tpl == "comparison":
        out.add("VS")
    if tpl == "then_vs_now":
        out |= {"THEN", "NOW"}
    return out


def brand_phrases(brief: dict, copy: dict | None = None) -> set[str]:
    """The layout's own marks of several words, which OCR reads as one line ("DEMO PAGE"): the theme's
    logo text and a template spec's fixed text. Matched whole, a word at a time they read as invented."""
    out = {_logo_text(brief, copy).strip().upper()}
    if (copy or {}).get("template_spec"):
        from .tspec import SpecError, find
        try:
            out |= {z["text"].strip().upper() for z in find(copy["template_spec"], brief)["text"] if z.get("text")}
        except SpecError:
            pass
    return {p for p in out if " " in p}


def _num_values(text: str) -> list[tuple[str, float]]:
    out = []
    for m in NUM_RE.finditer(text or ""):
        raw, unit = m.group(1), (m.group(2) or "").strip().lower()
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if unit in MULT:
            v *= MULT[unit]
        out.append((m.group(0).strip(), v))
    return out


def card_strings(copy: dict) -> list[str]:
    if copy.get("template_spec"):      # a template spec's card shows its slots and nothing else (bore/tspec.py)
        from .tspec import slot_strings
        return slot_strings(copy.get("slots") or {})
    from .layouts import items_text
    out = items_text(copy)
    for f in CARD_FIELDS:
        v = copy.get(f)
        if isinstance(v, list):
            out += [x for x in v if isinstance(x, str) and x.strip()]
        elif isinstance(v, str) and v.strip():
            out.append(v)
    return out


def sources_text(brief: dict) -> str:
    return "\n".join(p.get("text", "") for p in brief.get("posts", []))


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9#%.]+", " ", s.upper()).strip()


# --- BEFORE generation -----------------------------------------------------------------------
HELPLINE_RX = re.compile(r"If you or someone you know is struggling,? call or text 988 \(US\)\.?", re.I)
# an editor asked for the images as they are (the note or the feedback says so)
AS_IS_RX = re.compile(r"\b(as[- ]is|no edits?|unedited|without (any )?(edits?|branding|text|card|logo)|"
                      r"no (card|branding|logo|text on)|(don'?t|do not)( do)?( any)? (brand\w*|edit\w*)|"
                      r"original (images?|photos?))\b", re.I)
# an editor's link: its own image (an article's is usually 1200x630) only warns down to this short
# side; below it, it is too soft for a card
LINK_PHOTO_MIN = 500


def lint(brief: dict, copy: dict, playbook: dict | None = None, min_photo_short_side: int = 800,
         min_niche_fit: int = 8) -> dict:
    errors, warnings = [], []
    playbook = playbook or brief.get("playbook") or {}
    src = sources_text(brief)
    card = card_strings(copy)
    card_text = " \n ".join(card)
    # a template spec draws the card: the coded templates' own rules (items, casefile ...) do not apply
    template = "" if copy.get("template_spec") else copy.get("template", "")
    route = (brief.get("verification") or {}).get("route", "")

    if copy.get("decision") == "skip":
        return {"pass": True, "errors": [], "warnings": ["decision is skip: nothing to render"], "skip": True}
    try:
        fit = float(copy.get("niche_fit"))
    except (TypeError, ValueError):
        fit = None
    studio = route == "studio"          # an editor chose this story in the Studio
    if fit is None and not studio:
        errors.append("copy.json needs niche_fit (0-10)")
    elif fit is not None and fit < min_niche_fit and not studio and route != "manual":   # editors' links: their call
        errors.append(f"niche_fit {fit:g} is below {min_niche_fit}: skip this story (decision: skip)")
    spec = None
    if copy.get("template_spec"):      # a template spec (bore/tspec.py): its slot rules replace the layouts' limits
        from .tspec import SpecError, find, lint_slots
        try:
            spec = find(copy["template_spec"], brief)
        except SpecError as e:
            errors.append("; ".join(str(e).splitlines()))
        if spec:
            errors += lint_slots(spec, copy)
        for f in ("items", *(f for f in CARD_FIELDS if f != "headline_lines")):
            if copy.get(f):
                errors.append(f"copy.json sets '{f}' but a template spec draws only its slots: put that text in slots")
    for k in ("caption", "headline_lines") + (() if copy.get("template_spec") else ("template",)):
        if not copy.get(k):
            errors.append(f"copy.json is missing '{k}'" + (" (with a template spec: the post's title in the dashboard; "
                                                         "the card shows the slots)" if k == "headline_lines" and spec else ""))
    from .layouts import GENERIC
    allowed_templates = ((playbook.get("card") or {}).get("templates") or [])
    if allowed_templates and template not in allowed_templates and template not in GENERIC and not copy.get("template_spec"):
        errors.append(f"template '{template}' is not one of {allowed_templates}")

    # 0. what the editors' reviews teach: an AVOID kind or template needs a stated reason
    lessons = brief.get("lessons") or {}
    if lessons and not studio and route != "manual" and not (copy.get("lesson_override") or "").strip():
        tk = lessons.get("this_kind") or {}
        avoid = set(lessons.get("avoid_kinds") or ([tk.get("kind")] if tk.get("status") == "avoid" else []))
        hit = next((k for k in (brief.get("kind"), copy.get("kind"), tk.get("kind")) if k and k in avoid), None)
        if hit:          # brief.kind OR a relabelled copy.kind: relabelling doesn't get around it
            n = f"{tk.get('approved', 0)} of {tk.get('n', 0)} " if hit == tk.get("kind") else ""
            errors.append(f"editors approved {n}'{tk.get('label', hit) if hit == tk.get('kind') else hit}' stories rarely: "
                          "skip it, or say in lesson_override what makes this one different")
        if template and template in (lessons.get("avoid_templates") or []):
            errors.append(f"editors rarely approve the '{template}' template: use another one, or say why in lesson_override")
    if brief.get("kinds") and copy.get("kind") and copy["kind"] not in brief["kinds"]:
        errors.append(f"kind '{copy['kind']}' is not one of {brief['kinds']}")

    # 1. certainty: a hedged source must stay hedged on the card
    src_uncertain = bool(UNCERTAIN.search(src)) or route == "watchlist"
    # every line on the card, not just the headline: each line that states something as fact
    # must carry its own hedge when the source is hedged
    units = [" ".join(copy.get("headline_lines") or [])] + [
        copy.get(f) for f in ("dek", "stat_label", "stat_value", "footer", "footer_sub") if copy.get(f)]
    if copy.get("footer") and copy.get("footer_sub"):   # a footer split over two lines is one statement
        units = units[:-2] + [copy["footer"] + " " + copy["footer_sub"]] if units[-2:] == [copy["footer"], copy["footer_sub"]] else units
    if copy.get("template_spec"):      # each slot is one statement (a stat plate: its value and label)
        from .tspec import slot_units
        units = slot_units(copy.get("slots") or {})
    for line in units:
        definitive = DEFINITIVE.findall(line.upper())
        if not (src_uncertain and definitive) or HEDGE_ON_CARD.search(line):
            continue
        errors.append(
            f"'{line}' states {sorted(set(definitive))} as fact but the source is hedged "
            f"({', '.join(sorted({m.group(0).lower() for m in UNCERTAIN.finditer(src)})[:5]) or 'single source'}): "
            "keep the source's certainty on that line, e.g. LIKELY OUT / COULD RETURN / EXPECTED TO SIGN / FOR NOW")
    if route in ("watchlist", "manual") and not ATTRIBUTION.search(copy.get("caption", "")):
        errors.append("single-source story: the caption must attribute it (\"per <reporter>\" / \"reportedly\")")
    if not src.strip():
        errors.append("no source text to check the copy against" + (
            ": the link could not be read; record what the page says with `routine link-text`, or report "
            "it with `routine link-fail`" if brief.get("link") else ""))

    # 2. numbers on the card and in the caption must come from the sources (not the helpline line
    # the suicide/self-harm rule adds)
    src_vals = [v for _, v in _num_values(src)]
    for where, text in (("card", card_text), ("caption", HELPLINE_RX.sub("", copy.get("caption", "")))):
        for raw, v in _num_values(text):
            if v < 10 and "." not in raw:
                continue  # single digits ("#1", "2 TD") are too ambiguous to check mechanically
            if not any(abs(v - s) <= max(0.011 * s, 0.05) for s in src_vals):
                errors.append(f"{where} number '{raw}' does not appear in the sources")

    # 3. no repeated facts on the card
    seen = {}
    for s in card:
        for raw, v in _num_values(s):
            if v >= 10 or "." in raw:
                if v in seen and seen[v] != s:
                    errors.append(f"card repeats the number {raw!r} ('{seen[v]}' and '{s}')")
                seen.setdefault(v, s)
    for i in range(len(card)):
        for j in range(i + 1, len(card)):
            a, b = set(_norm(card[i]).split()), set(_norm(card[j]).split())
            if len(a) >= 2 and len(b) >= 2 and len(a & b) / min(len(a), len(b)) >= 0.6:
                errors.append(f"card lines repeat each other: '{card[i]}' / '{card[j]}'")

    # 4. lengths (a template spec's slot rules took their place above)
    generic = template in GENERIC and template not in allowed_templates
    hl_limit = LIMITS["quote_line"] if template == "quote" else LIMITS["headline_line"] + (8 if generic else 0)
    for line in [] if copy.get("template_spec") else copy.get("headline_lines") or []:
        if len(line) > hl_limit:
            errors.append(f"headline line too long ({len(line)} > {hl_limit}): '{line}'")
    for f, lim in (("dek", LIMITS["dek"]), ("label", LIMITS["label"]), ("stat_value", LIMITS["stat"]),
                   ("footer", LIMITS["footer"]), ("footer_sub", LIMITS["footer"])):
        if len(copy.get(f) or "") > lim:
            errors.append(f"{f} too long ({len(copy[f])} > {lim}): '{copy[f]}'")
    words = sum(len(s.split()) for s in card)
    max_words = 60 if template in ("comparison", "ranked_list", "timeline") else LIMITS["words"]
    if words > max_words and not copy.get("template_spec"):
        errors.append(f"too much text on the card ({words} words > {max_words}); cut it down")
    if template in ("comparison", "then_vs_now") and len(copy.get("items") or []) < 2:
        errors.append(f"the {template} layout needs items: the two sides first (see layouts in the brief)")
    if template in ("ranked_list", "timeline") and not copy.get("items"):
        errors.append(f"the {template} layout needs items (up to 5 rows)")
    if copy.get("highlight") and _norm(copy["highlight"]) not in _norm(card_text):
        errors.append(f"highlight '{copy['highlight']}' is not in the card text")

    # 5. caption shape per page (the playbook's `lint` block; pages without one: hashtags on the
    # last line only)
    caption = copy.get("caption", "")
    if copy.get("hashtags"):           # Studio keeps the hashtags apart; they go on the last line
        caption = caption.rstrip() + "\n\n" + " ".join(t if t.startswith("#") else f"#{t}" for t in copy["hashtags"])
    tags = re.findall(r"#\w+", caption.strip().splitlines()[-1] if caption.strip() else "")
    rules = playbook.get("lint") or {}
    ht = rules.get("hashtags") or {}
    name = playbook.get("name") or brief.get("page_name") or "This page"
    if ht and (not ht.get("min", 0) <= len(tags) <= ht.get("max", 30) or (ht.get("required") and ht["required"] not in tags)):
        errors.append(f"{name} caption needs {ht.get('min', 0)}-{ht.get('max', 30)} hashtags on the last line"
                      + (f" including {ht['required']}" if ht.get("required") else ""))
    if rules.get("caps_hook"):
        first = caption.strip().splitlines()[0] if caption.strip() else ""
        if first and first.upper() != first:
            warnings.append(f"{name} captions open with an ALL-CAPS hook line")
    body = "\n".join(caption.strip().splitlines()[:-1])
    if rules.get("closing_question", not studio) and "?" not in body[-200:]:
        errors.append("caption must end (before the hashtags) with a question to the audience")

    # 6. photo
    idx = copy.get("photo_index", -1)
    photos = brief.get("photos") or []
    if copy.get("as_is"):        # the editor's own images, unedited: only when an editor asked for it
        asked = " ".join(str(x or "") for x in ((brief.get("link") or {}).get("note"),
                                                (brief.get("regeneration") or {}).get("feedback"),
                                                (brief.get("regeneration") or {}).get("link_note")))
        if not ((brief.get("link") or brief.get("regeneration")) and
                (AS_IS_RX.search(asked) or (brief.get("regeneration") or {}).get("as_is"))):
            errors.append("as_is is only for an editor's link or feedback that asks for the images as they are "
                          "(\"as is\", \"no edits\", \"no branding\")")
        bad = [j for j in (copy.get("photos") or []) if not (isinstance(j, int) and 0 <= j < len(photos))]
        if bad or not photos:
            errors.append(f"as_is: photos {bad or '[]'} do not exist (add the link's images or a real photo first)")
    if isinstance(idx, int) and idx >= 0:
        if idx >= len(photos):
            errors.append(f"photo_index {idx} does not exist")
        else:
            ph = photos[idx]
            try:
                w, h = (int(x) for x in ph.get("size", "0x0").split("x"))
            except ValueError:
                w = h = 0
            floor = (min(min_photo_short_side, 600) if ph.get("source") == "news" and (ph.get("consensus") or 0) >= 2 else
                     min(min_photo_short_side, 720) if ph.get("note") == "video frame" else min_photo_short_side)
            if min(w, h) < floor and ((studio and idx == 0) or copy.get("as_is")):   # the editor's own image
                warnings.append(f"the editor's chosen image is {ph.get('size')} (below {floor}px): it may look soft")
            elif min(w, h) < floor and ph.get("source") == "link" and min(w, h) >= LINK_PHOTO_MIN:
                # the linked post's own image (usually a 1200x630 article image): the one to use
                warnings.append(f"the link's own image is {ph.get('size')} (below {floor}px): it may look soft")
            elif min(w, h) < floor:
                errors.append(f"chosen photo is {ph.get('size')} - below the {floor}px floor")
            flags = [str(f) for f in ph.get("flags") or []]
            bad = [f for f in flags if f.startswith(("scoreboard", "much sharper than the footage", "text-heavy",
                                                     "wide broadcast", "collage"))]
            if bad and not (studio and idx == 0):
                errors.append(f"chosen photo is flagged '{bad[0]}': pick another photo")
            if any(f.startswith(("violent incident", "fatal incident")) for f in flags):
                warnings.append("violent incident: the photo must not show the victim being harmed (check it)")
            for f in flags:
                if f.startswith(("soft", "agency photo", "file photo")):
                    warnings.append(f"chosen photo: {f}")
            # a clear face that is not the subject's (bore/faceid.py); helmets and profiles stay neutral
            if ph.get("id_match") == "mismatch" and not (studio and idx == 0):
                warnings.append(f"chosen photo may not be {ph.get('id_name')} (face match {ph.get('id_score', 0):.2f}): "
                                "confirm by eye who it shows, or choose a photo whose id_match is 'match'")
            if not copy.get("people_in_photo") and not str(copy.get("photo_subject") or "").strip():
                errors.append("people_in_photo is empty: name exactly who is visible in the chosen photo "
                              "(or, for a photo with no person in it, set photo_subject to what it shows)")
    else:
        # editors rejected a card with no photo ("Image quality"): every card shows the subject
        errors.append("no photo chosen: every card needs a sharp real photo of the story's subject "
                      "(find one with `routine photos`, or skip the story)")
    # the people shown must be the people in the story (a Brosmer card showed McCarthy)
    story = " ".join([caption, " ".join(copy.get("headline_lines") or []), copy.get("dek", ""), copy.get("label", ""),
                      card_text if copy.get("template_spec") else ""]).lower()
    for person in copy.get("people_in_photo") or []:
        words = [w for w in re.sub(r"[^a-z' -]", " ", person.lower()).split() if w not in {"jr", "sr", "ii", "iii"}]
        if words and words[-1] not in story:
            errors.append(f"'{person}' is in the photo but not in the story: use a photo of the people the post is about")
    extra = copy.get("extra_photo_indexes") or []
    if spec:      # photo 1, 2 ... of the spec's zones are extra_photo_indexes in order
        zones = {z["photo"]: z for z in spec["photos"]}
        need = [p for p, z in zones.items() if p and not z["optional"]]
        if len(extra) > max(zones) or len(extra) < max(need, default=0):
            errors.append("the template shows photo 0 (the chosen one) and "
                          f"{max(need, default=0)}-{max(zones)} more: extra_photo_indexes has {len(extra)}")
    elif extra and template not in ("casefile", "comparison", "then_vs_now"):
        errors.append("extra_photo_indexes is only for the casefile, comparison and then_vs_now templates")
    for j in extra:
        if not (isinstance(j, int) and 0 <= j < len(photos)):
            errors.append(f"extra_photo_indexes: photo {j} does not exist")
    # casefile (polaroids on a crime-scene backdrop) is the true-crime look; on celebrity news it
    # left small faces and dead space and every such card was rejected (Sep 29)
    if template == "casefile" and copy.get("story_type") != "true_crime":
        errors.append("the casefile template is only for true crime / mysteries / court cases: set "
                      "\"story_type\": \"true_crime\" if this is one, otherwise use headline or spotlight")

    return {"pass": not errors, "errors": errors, "warnings": warnings}


# --- AFTER generation ------------------------------------------------------------------------
_OCR = None


def _ocr(image_path: str) -> list[tuple[str, float]]:
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    res, _ = _OCR(image_path)
    out = []
    for r in res or []:
        xs, ys = [p[0] for p in r[0]], [p[1] for p in r[0]]
        out.append((r[1], float(r[2]), (min(xs), min(ys), max(xs), max(ys))))
    return out


def ocr_lines(img: bytes, min_conf: float = 0.6) -> list[str]:
    """Text found in an image given as bytes."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        f.write(img)
        f.flush()
        return [t for t, c, _ in _ocr(f.name) if c >= min_conf]


# --- faces ------------------------------------------------------------------------------------
# one detector per thread: each call resizes it to the image, and photo checks run in thread pools
_FACES = __import__("threading").local()
FACE_MODEL = __import__("pathlib").Path(__file__).resolve().parent.parent / "assets" / "models" / "face_detection_yunet_2023mar.onnx"


def detect_face_rows(image, min_score: float = 0.6) -> list:
    """YuNet's full row for each face: x, y, w, h, five landmarks (x, y), score (15 values, what
    FaceRecognizerSF.alignCrop takes). `image` is a path or a BGR numpy array."""
    import cv2
    im = cv2.imread(str(image)) if not hasattr(image, "shape") else image
    if im is None:
        return []
    h, w = im.shape[:2]
    det = getattr(_FACES, "det", None)
    if det is None:
        det = _FACES.det = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (w, h), min_score)
    det.setInputSize((w, h))
    det.setScoreThreshold(min_score)
    _, faces = det.detect(im)
    return [] if faces is None else list(faces)


def detect_faces(image, min_score: float = 0.6) -> list[tuple[float, float, float, float, float]]:
    """(x, y, w, h, score) for each face. `image` is a path or a BGR numpy array."""
    return [tuple(float(v) for v in f[:4]) + (float(f[14]),) for f in detect_face_rows(image, min_score)]


def head_zone(face: tuple, pad: float = 0.18, top: float = 0.45, below: float = 0.08) -> tuple[float, float, float, float]:
    """Protected area around a detected face: the face, hair/forehead above, a side margin, and a
    small margin under the chin (text on the chest/jersey is fine, on the chin is not)."""
    x, y, w, h = face[:4]
    return (x - pad * w, y - top * h, x + w + pad * w, y + h + below * h)


def _overlap(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


def _source_faces(image_path: str, brief: dict, copy: dict) -> list:
    from pathlib import Path
    idx = copy.get("photo_index", -1)
    photos = brief.get("photos") or []
    if not (isinstance(idx, int) and 0 <= idx < len(photos)):
        return []
    p = Path(image_path).parent / photos[idx]["file"]
    return detect_faces(p) if p.exists() else []


MIN_FACE_PX = 100   # on the 1080x1350 card


def _is_card_text(t: str, expected: list[str], brand: set[str]) -> bool:
    """Is this OCR line our own card text (headline, label, logo...) rather than text that is part
    of the photo (a helmet visor's OAKLEY, a jersey, a sign)? Only our text must stay off faces."""
    n = _norm(t).replace(" ", "")
    if len(n) < 2:
        return False
    pool = _norm(" ".join(expected) + " " + " ".join(brand)).replace(" ", "")
    if n in pool:
        return True
    return SequenceMatcher(None, n, pool).find_longest_match(0, len(n), 0, len(pool)).size >= 0.8 * len(n)


def _drawn_lines(image_path: str):
    """A template spec card's lines as the renderer drew them on this very image (render.json beside it,
    bore/routine.py _render_spec): (the image, [(text, box)]); None when there is none for it."""
    import hashlib
    import json
    from pathlib import Path
    p = Path(image_path)
    try:
        rec = json.loads(p.with_name("render.json").read_text())
        img = p.read_bytes()
    except (OSError, ValueError):
        return None
    if rec.get("sha256") != hashlib.sha256(img).hexdigest():
        return None
    return img, [(str(x.get("text") or ""), x.get("box")) for x in rec.get("lines") or [] if len(x.get("box") or []) == 4]


LONE_PX = (48, 64, 32)      # a lone value's height for OCR, tried in turn (one stroke at 300 px reads as nothing)
LOOKALIKE = {"2": "7Z"}     # what OCR reads a lone character in the display faces as (Anton's '2' reads '7')


def _reads_alone(lone, want: str) -> bool:
    """Is `want` (normalised, 1-2 characters) what OCR reads in a line drawn with that text, cut out
    on its own and scaled to a size OCR reads?"""
    import io
    import tempfile

    from itertools import product

    from PIL import Image, ImageOps
    img, lines = lone
    # the renderer drew exactly `want` there (render.json, tied to this image), so a known look-alike is it
    reads = {"".join(c) for c in product(*(ch + LOOKALIKE.get(ch, "") for ch in want))}
    im = Image.open(io.BytesIO(img)).convert("RGB")
    for text, (x0, y0, x1, y1) in lines:
        if _norm(text).replace(" ", "") != want or y1 <= y0 or x1 <= x0:
            continue
        side = 0.1 * (y1 - y0)
        cut = im.crop((max(0, int(x0 - side)), max(0, int(y0)), min(im.width, int(x1 + side)), min(im.height, int(y1))))
        for target in LONE_PX:
            k = target / cut.height
            c = cut.resize((max(8, round(cut.width * k)), max(8, round(cut.height * k))), Image.LANCZOS)
            c = ImageOps.expand(c, border=int(target * 0.6), fill=c.getpixel((0, 0)))
            with tempfile.NamedTemporaryFile(suffix=".png") as f:
                c.save(f.name)
                if any(_norm(t).replace(" ", "") in reads for t, conf, _ in _ocr(f.name) if conf >= 0.5):
                    return True
    return False


def _premium(card: Path) -> bool:
    """The card is the image model's whole card (krea mode premium), not the template renderer's."""
    try:
        attempts = json.loads((card.parent / "attempts.json").read_text())
    except (OSError, ValueError):
        return False
    return bool(attempts) and attempts[-1].get("engine") == "krea-premium"


def _premium_face(card: Path, brief: dict, copy: dict) -> str:
    """An error when the person on a premium card is not the person in the chosen photo (the model
    redraws faces): the photo's largest clear face against every face on the card."""
    from . import faceid
    idx = copy.get("photo_index", -1)
    photos = brief.get("photos") or []
    if not (copy.get("people_in_photo") and isinstance(idx, int) and 0 <= idx < len(photos)):
        return ""
    try:
        src = [f for f in faceid.faces((card.parent / photos[idx]["file"]).read_bytes()) if faceid.clear(f)]
        if not src:
            return ""          # a helmet, a profile: nothing to compare (the eye check still applies)
        verdict, sim = faceid.judge(src[0][2], faceid.faces(card.read_bytes()))
    except Exception:          # the face model not loading is the machine's problem, not the card's
        return ""
    if verdict == "mismatch" or (sim is not None and sim < faceid.MISMATCH):
        return (f"the person on the card does not look like the person in the chosen photo (similarity "
                f"{sim:.2f}): regenerate with --feedback \"keep the face exactly as in the photo\"")
    return ""


def verify(image_path: str, brief: dict, copy: dict, ocr=None, faces_override=None,
           source_faces_override=None) -> dict:
    """Compare the text actually on the card with the approved card text."""
    errors, warnings = [], []
    # Template cards come with a clean twin (same layout, lettering not worn): read the text there,
    # because OCR loses short words in distressed lettering. Faces are still checked on the real card.
    from pathlib import Path as _P
    twin = _P(image_path).with_name("card_clean.jpg")
    use_twin = (ocr is None and twin.exists() and _P(image_path).exists()
                and twin.stat().st_mtime >= _P(image_path).stat().st_mtime - 30)
    raw = (ocr or _ocr)(str(twin) if use_twin else image_path)
    found = [(r[0], r[1]) for r in raw]
    boxes = [(r[0], r[2]) for r in raw if len(r) > 2]
    found_text = " ".join(t for t, _ in found)

    # faces must be 100% clear: no text box may touch a face or head
    faces = faces_override if faces_override is not None else (detect_faces(image_path) if ocr is None else [])
    if not faces and faces_override is None and ocr is None:
        # a second, gentler look (profiles, a zoomed-out photo over its blurred copy); ignore specks
        faces = [f for f in detect_faces(image_path, min_score=0.5) if f[3] >= 40]
    for f in faces:
        zone = head_zone(f)
        for t, b in boxes:
            if _overlap(zone, b) > 0 and _is_card_text(t, card_strings(copy), brand_words(brief, copy)):
                errors.append(f"text '{t}' overlaps a face/head (face at x={f[0]:.0f}, y={f[1]:.0f}); "
                              "faces must be completely clear of text")
    # the subject must be recognisable at feed size: small polaroid faces were rejected (Sep 29)
    if faces and copy.get("people_in_photo"):
        biggest = max(f[3] for f in faces)
        if biggest < MIN_FACE_PX:
            errors.append(f"the subject's face is too small on the card ({biggest:.0f}px tall, needs {MIN_FACE_PX}px+): "
                          "use a closer photo or a photo-led layout (headline / spotlight / report)")
    src_faces = source_faces_override
    if src_faces is None and ocr is None:
        src_faces = _source_faces(image_path, brief, copy)
    if copy.get("people_in_photo") and not faces:
        if src_faces:
            errors.append("the source photo shows a clear face but no face is visible on the card: it was covered "
                          "by text/graphics or altered")
        else:
            warnings.append("no face detected on the card or the source photo (helmet, visor, profile?): confirm "
                            "by eye that no text, stamp or graphic touches any face or head")
    expected = card_strings(copy)
    brand = brand_words(brief, copy)
    marks = brand | brand_phrases(brief, copy) | ({copy["photo_credit"]} if copy.get("photo_credit") else set())
    premium = _premium(_P(image_path))
    if premium and ocr is None:
        why = _premium_face(_P(image_path), brief, copy)
        if why:
            errors.append(why)

    def is_brand(t: str) -> bool:   # the page logo (a brush/marker font OCR misreads: CULTVRE)
        n = _norm(t).replace(" ", "")
        return bool(n) and any(SequenceMatcher(None, n, _norm(b).replace(" ", "")).ratio() >= 0.7 for b in marks)
    # the logo sits among the other lines; leave it out so a wrapped label reads as one line
    got = _norm(" ".join(t for t, _ in found if not is_brand(t))).replace(" ", "")
    if premium:                 # its lettering's commas read as full stops: punctuation is not checked there
        got = got.replace(".", "")

    # every approved line must be on the card, spelled right; a template spec's value of 1-2
    # characters (a plate's '1') is read on its own where the renderer drew it
    lone = (_drawn_lines(str(twin) if use_twin else image_path)
            if ocr is None and copy.get("template_spec") and not premium else None)
    for s in expected:
        want = _norm(s).replace(" ", "").replace("“", "").replace("”", "")
        if premium:
            want = want.replace(".", "")
        if not want:
            continue
        # a premium card's hero number is drawn in 3D, behind the subject: OCR cannot read it, the
        # session confirms it by eye (an unread long line is still an error: it was misspelled)
        if premium and want not in got and len(want) <= 6 and any(c.isdigit() for c in want):
            warnings.append(f"CONFIRM BY EYE: '{s}' is drawn too stylised for OCR; check it reads exactly '{s}', "
                            "once, and record it in warnings_checked")
            continue
        if lone is not None and len(want) <= 2:
            if _reads_alone(lone, want):
                continue
            if want.isdigit():      # a digit anywhere else on the card (a jersey) is not this one
                errors.append(f"missing or misspelled on the card: '{s}' (it could not be read where it is drawn)")
                continue
        if want in got:
            continue
        best = max((SequenceMatcher(None, want, got[i:i + len(want)]).ratio()
                    for i in range(0, max(1, len(got) - len(want) + 1))), default=0)
        # (an image model draws a premium card's letters itself: a near miss there is a real typo)
        if best < (0.95 if premium else 0.86):
            errors.append(f"missing or misspelled on the card: '{s}' (best OCR match {best:.0%})")
        elif best < 1.0:
            warnings.append(f"'{s}' reads slightly differently on the card ({best:.0%}); check the spelling by eye")

    # nothing extra: every OCR line must belong to the approved text, the brand, or the photo
    exp_norm = _norm(" ".join(expected) + " " + " ".join(brand)).replace(" ", "")
    if premium:
        exp_norm = exp_norm.replace(".", "")
    for t, conf in found:
        n = _norm(t).replace(" ", "")
        if premium:
            n = n.replace(".", "")
        if not n or conf < 0.6 or is_brand(t):
            continue
        if n in exp_norm or SequenceMatcher(None, n, exp_norm).find_longest_match(0, len(n), 0, len(exp_norm)).size >= 0.8 * len(n):
            continue
        if re.fullmatch(r"\d{1,2}", n):
            continue  # jersey numbers in the photo
        if _num_values(t) and any(v >= 10 or "." in r for r, v in _num_values(t)):
            errors.append(f"unexpected number on the card: '{t}' (not in the approved text)")
        elif len(n) >= 4:
            warnings.append(f"text on the card that is not in the copy: '{t}' - fine only if it is part of the photo "
                            "(jersey, sponsor, sign); otherwise it was invented")

    # no repeated numbers
    def counted(raw: str, v: float) -> bool:   # jersey / uniform numbers (1-2 digits) are photo content
        return not re.fullmatch(r"\d{1,2}", raw)
    vals = [v for r, v in _num_values(found_text) if counted(r, v)]
    for v in set(vals):
        if vals.count(v) > sum(1 for s in expected for _, x in _num_values(s) if x == v):
            errors.append(f"the number {v:g} appears more times on the card than in the approved text")

    return {"pass": not errors, "errors": errors, "warnings": warnings, "ocr": [t for t, _ in found],
            "text_checked_on": "card_clean.jpg" if use_twin else "card.jpg",
            "faces": [[round(v) for v in f[:4]] for f in faces]}
