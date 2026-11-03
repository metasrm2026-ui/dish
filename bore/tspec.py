"""Template specs: a post template as DATA that the card renderer draws (docs/TEMPLATE_SPEC.md), so a
page's new template needs a JSON file, never code.

A spec names its zones as fractions of the canvas: photo zones (the editor's photo first), text zones
bound to slots, list zones (stat plates), a logo zone and an optional AI-art layer, coloured from the
page theme (admin panel: accent, background, text, logo text). copy.json picks one with
"template_spec": "<id>" and fills its "slots"; lint checks the slot rules (bore/guard.py), the renderer
keeps every face clear of every zone drawn over it (moving the photo, a zone's alt_box, or showing the
whole photo), and verify reads the text back with OCR as for every card.

Specs come from the brief ("template_specs": {id: spec}) or the page's spec store, private/templates/<page>/
<id>.json: the specs the page's editors approved (dashboard routes/templates.js), which `studio sync` writes.
Made from the page's confidential templates document, they never go into the repository.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from .config import PRIVATE, ROOT

SPEC_DIR = PRIVATE / "templates"        # the approved specs, by page (bore/studio.py sync)
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
SLOT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
LANES = ("ai_art", "clean", "editorial", "screenshot")
FONTS = {"display": "Anton", "condensed": "Oswald", "body": "Inter", "script": "Marker"}
WEIGHTS = {"display": 400, "condensed": 700, "body": 700, "script": 400}
ROLES = ("accent", "bg", "ink", "accent_ink", "white", "black")
COLOR_RE = re.compile(r"^(#[0-9a-fA-F]{6}|accent|bg|ink|accent_ink|white|black|auto)(?:/(\d{1,3}))?$")
CASES = ("upper", "lower", "title", "as_is")
ALIGNS = ("left", "center", "right")
VALIGNS = ("top", "middle", "bottom")
TEXTURES = ("none", "grit", "noise", "worn", "stripes", "halftone")
BLENDS = ("normal", "screen", "multiply", "overlay", "soft-light")
EDGES = ("top", "bottom", "left", "right")
SLOT_TYPES = ("text", "number", "list")
MAX_WORDS = 26            # the whole card (guard.LIMITS["words"]); a spec may set rules.max_words

KEYS = {
    "spec": {"id", "name", "use_when", "limits", "version", "canvas", "visual_lane", "background", "art", "photos", "text",
             "lists", "logo", "slots", "rules"},
    "canvas": {"w", "h"},
    "background": {"fill", "gradient", "texture"},
    "gradient": {"to", "direction"},
    "art": {"box", "blend", "opacity", "prompt"},
    "photo": {"box", "photo", "fit", "focus", "duotone", "shade", "border", "radius", "contain_fallback", "optional"},
    "shade": {"edge", "size", "color"},
    "border": {"width", "color"},
    "text": {"slot", "text", "box", "alt_box", "font", "weight", "size", "max_lines", "case", "color", "align",
             "valign", "plate", "rotation", "fit", "line_height", "letter_spacing", "highlight", "worn"},
    "plate": {"color", "style", "pad"},
    "list": {"slot", "box", "direction", "gap", "layout", "plate", "emphasis_plate", "align", "value", "label"},
    "item": {"font", "weight", "size", "color", "case"},
    "logo": {"box", "font", "weight", "size", "color", "align", "valign"},
    "slot": {"type", "required", "max_words", "min_words", "max_chars", "min_items", "max_items",
             "value_max_chars", "label_max_words", "label_max_chars", "examples", "hint"},
    "rules": {"max_words"},
}


class SpecError(ValueError):
    """A spec that cannot be drawn; the message lists every problem, one per line."""


# --------------------------------------------------------------------------- validation
def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _box_errors(where: str, b) -> list[str]:
    if not (isinstance(b, (list, tuple)) and len(b) == 4 and all(_num(v) for v in b)):
        return [f"{where}: box must be [x, y, w, h] as fractions of the canvas (0-1)"]
    x, y, w, h = b
    if min(x, y) < 0 or w <= 0 or h <= 0 or x + w > 1.001 or y + h > 1.001:
        return [f"{where}: box {list(b)} must lie inside the canvas (x, y >= 0, w, h > 0, x + w <= 1, y + h <= 1)"]
    return []


def _color_errors(where: str, c, auto: bool = False) -> list[str]:
    m = COLOR_RE.match(c) if isinstance(c, str) else None
    if not m or (m.group(2) and int(m.group(2)) > 100) or (m.group(1) == "auto" and not auto):
        return [f"{where}: colour {c!r} must be a theme role ({', '.join(ROLES)}{', auto' if auto else ''}) or "
                "#rrggbb, optionally /0-100 for opacity (\"bg/80\")"]
    return []


def _size_errors(where: str, s) -> list[str]:
    if not (isinstance(s, (list, tuple)) and len(s) == 2 and all(isinstance(v, int) for v in s)
            and 8 <= s[0] <= s[1] <= 600):
        return [f"{where}: size must be [min_px, max_px], whole numbers, 8 <= min <= max <= 600"]
    return []


def _keys(where: str, d, kind: str) -> list[str]:
    if not isinstance(d, dict):
        return [f"{where}: must be an object"]
    bad = sorted(set(d) - KEYS[kind])
    return [f"{where}: unknown key '{k}' (allowed: {', '.join(sorted(KEYS[kind]))})" for k in bad]


def _choice(where: str, d: dict, key: str, options) -> list[str]:
    if key in d and d[key] not in options:
        return [f"{where}: {key} {d[key]!r} must be one of {', '.join(map(str, options))}"]
    return []


def _font_errors(where: str, d: dict) -> list[str]:
    errs = _choice(where, d, "font", tuple(FONTS))
    if "weight" in d and not (isinstance(d["weight"], int) and 100 <= d["weight"] <= 900):
        errs.append(f"{where}: weight must be 100-900")
    if "size" in d:
        errs += _size_errors(where, d["size"])
    return errs


def validate(raw) -> list[str]:
    """Every problem with a spec, each saying where it is ("text[1] (slot 'tagline'): ..."); [] = drawable."""
    errs = _keys("spec", raw, "spec")
    if not isinstance(raw, dict):
        return errs
    if not (isinstance(raw.get("id"), str) and ID_RE.match(raw["id"])):
        errs.append("spec: id must be 1-48 lower-case letters, digits, '-' or '_' (it is what copy.json names)")
    for k in ("name", "use_when"):
        if not (isinstance(raw.get(k), str) and raw[k].strip()):
            errs.append(f"spec: {k} is required (text)")
    if "limits" in raw and not (isinstance(raw["limits"], str) and len(raw["limits"]) <= 400):
        errs.append("spec: limits must be text, up to 400 characters (what the document shows that this spec cannot draw)")
    errs += _choice("spec", raw, "visual_lane", LANES)
    cv = raw.get("canvas", {})
    errs += _keys("canvas", cv, "canvas")
    if isinstance(cv, dict) and not all(isinstance(cv.get(k, 1080), int) and 600 <= cv.get(k, 1080) <= 2400 for k in ("w", "h")):
        errs.append("canvas: w and h must be whole pixels, 600-2400 (default 1080 x 1350)")
    bg = raw.get("background", {})
    errs += _keys("background", bg, "background")
    if isinstance(bg, dict):
        errs += _color_errors("background.fill", bg["fill"]) if "fill" in bg else []
        errs += _choice("background", bg, "texture", TEXTURES)
        if bg.get("gradient") is not None:
            g = bg["gradient"]
            errs += _keys("background.gradient", g, "gradient")
            if isinstance(g, dict):
                errs += _color_errors("background.gradient.to", g.get("to"))
                d = g.get("direction", "to bottom")
                if not (d == "radial" or re.fullmatch(r"to (top|bottom|left|right)( (left|right))?|\d{1,3}deg", str(d))):
                    errs.append("background.gradient: direction must be 'to bottom' (top/left/right), '<n>deg' or 'radial'")
    if raw.get("art") is not None:
        a = raw["art"]
        errs += _keys("art", a, "art")
        if isinstance(a, dict):
            errs += _box_errors("art", a.get("box", [0, 0, 1, 1])) + _choice("art", a, "blend", BLENDS)
            if not (_num(a.get("opacity", 1)) and 0 <= a.get("opacity", 1) <= 1):
                errs.append("art: opacity must be 0-1")
            if "prompt" in a and not (isinstance(a["prompt"], str) and 10 <= len(a["prompt"].strip()) <= 600):
                errs.append("art: prompt must be 10-600 characters: the artwork the layer shows (no people, no text)")

    slots = raw.get("slots")
    if not (isinstance(slots, dict) and slots):
        errs.append("spec: slots is required: the text the copy fills, by name ({\"headline\": {\"type\": \"text\", ...}})")
        slots = {}
    for name, s in slots.items():
        w = f"slots.{name}"
        if not SLOT_RE.match(str(name)):
            errs.append(f"{w}: slot names are lower-case letters, digits and '_' (start with a letter)")
        errs += _keys(w, s, "slot")
        if not isinstance(s, dict):
            continue
        errs += _choice(w, s, "type", SLOT_TYPES)
        for k in ("max_words", "min_words", "max_chars", "min_items", "max_items", "value_max_chars",
                  "label_max_words", "label_max_chars"):
            if k in s and not (isinstance(s[k], int) and s[k] >= (0 if k.startswith("min") else 1)):
                errs.append(f"{w}: {k} must be a whole number{' >= 1' if not k.startswith('min') else ''}")
        if s.get("type") == "list" and isinstance(s.get("min_items", 1), int) and isinstance(s.get("max_items", 4), int) \
                and not 1 <= s.get("min_items", 1) <= s.get("max_items", 4) <= 8:
            errs.append(f"{w}: a list holds 1-8 items (min_items <= max_items)")
        if "examples" in s and not isinstance(s["examples"], list):
            errs.append(f"{w}: examples must be a list")
        if "required" in s and not isinstance(s["required"], bool):
            errs.append(f"{w}: required must be true or false")
    drawn: dict[str, str] = {}

    def bind(where: str, slot, types) -> list[str]:
        if not isinstance(slot, str) or slot not in slots:
            return [f"{where}: slot {slot!r} is not defined in slots"]
        kind = slots[slot].get("type", "text") if isinstance(slots[slot], dict) else "?"
        if kind not in types:
            return [f"{where}: slot {slot!r} is a {kind} slot; this zone draws {' or '.join(types)}"]
        if slot in drawn:
            return [f"{where}: slot {slot!r} is already drawn by {drawn[slot]} (one zone per slot)"]
        drawn[slot] = where
        return []

    photos = raw.get("photos")
    if not (isinstance(photos, list) and photos):
        errs.append("spec: photos needs at least one photo zone (the editor's chosen photo)")
        photos = []
    seen = set()
    for i, z in enumerate(photos):
        w = f"photos[{i}]"
        errs += _keys(w, z, "photo")
        if not isinstance(z, dict):
            continue
        errs += _box_errors(w, z.get("box"))
        p = z.get("photo", i)
        if not (isinstance(p, int) and 0 <= p <= 5) or p in seen:
            errs.append(f"{w}: photo must be a distinct index 0-5 (0 = the editor's chosen photo)")
        seen.add(p)
        if p == 0 and z.get("optional"):
            errs.append(f"{w}: the editor's photo (photo 0) cannot be optional")
        errs += _choice(w, z, "fit", ("cover", "contain"))
        f = z.get("focus", [0.5, 0.42])
        if not (isinstance(f, (list, tuple)) and len(f) == 2 and all(_num(v) and 0 <= v <= 1 for v in f)):
            errs.append(f"{w}: focus must be [x, y] (0-1): where the face centre should sit in the box")
        if z.get("duotone") is not None:
            d = z["duotone"]
            if not (isinstance(d, list) and len(d) == 2):
                errs.append(f"{w}: duotone must be [dark colour, light colour]")
            else:
                errs += [e for c in d for e in _color_errors(f"{w}.duotone", c)]
        for j, sh in enumerate(z.get("shade") or []):
            errs += _keys(f"{w}.shade[{j}]", sh, "shade")
            if isinstance(sh, dict):
                errs += _choice(f"{w}.shade[{j}]", {"edge": sh.get("edge")}, "edge", EDGES)
                if not (_num(sh.get("size", 0.4)) and 0 < sh.get("size", 0.4) <= 1):
                    errs.append(f"{w}.shade[{j}]: size must be 0-1 (the share of the box it darkens)")
                errs += _color_errors(f"{w}.shade[{j}]", sh.get("color", "bg"))
        if z.get("border") is not None:
            errs += _keys(f"{w}.border", z["border"], "border")
            if isinstance(z["border"], dict):
                errs += _color_errors(f"{w}.border", z["border"].get("color", "accent"))
                if not (isinstance(z["border"].get("width", 6), int) and 0 <= z["border"].get("width", 6) <= 40):
                    errs.append(f"{w}.border: width must be 0-40 px")
        if not (isinstance(z.get("radius", 0), int) and 0 <= z.get("radius", 0) <= 200):
            errs.append(f"{w}: radius must be 0-200 px")
    if photos and 0 not in seen:
        errs.append("spec: one photo zone must show photo 0, the editor's chosen photo")

    for i, z in enumerate(raw.get("text") or []):
        w = f"text[{i}]" + (f" (slot '{z.get('slot')}')" if isinstance(z, dict) and z.get("slot") else "")
        errs += _keys(w, z, "text")
        if not isinstance(z, dict):
            continue
        if ("slot" in z) == ("text" in z):
            errs.append(f"{w}: give either slot (text from the copy) or text (fixed words), not both")
        elif "slot" in z:
            errs += bind(w, z["slot"], ("text", "number"))
        elif not (isinstance(z["text"], str) and 0 < len(z["text"].strip()) <= 30):
            errs.append(f"{w}: fixed text must be 1-30 characters")
        errs += _box_errors(w, z.get("box"))
        if z.get("alt_box") is not None:
            errs += _box_errors(f"{w}.alt_box", z["alt_box"])
        errs += _font_errors(w, z)
        if z.get("rotation", 0) != 0:
            errs.append(f"{w}: rotation must be 0 (the card text is read back by OCR)")
        if not (isinstance(z.get("max_lines", 2), int) and 1 <= z.get("max_lines", 2) <= 8):
            errs.append(f"{w}: max_lines must be 1-8")
        for k, opts in (("case", CASES), ("align", ALIGNS), ("valign", VALIGNS), ("fit", ("block", "each_line"))):
            errs += _choice(w, z, k, opts)
        errs += _color_errors(w, z["color"], auto=True) if "color" in z else []
        errs += _color_errors(f"{w}.highlight", z["highlight"]) if z.get("highlight") else []
        if z.get("plate") is not None:
            pl = z["plate"] if isinstance(z["plate"], dict) else {"color": z["plate"]}
            errs += _keys(f"{w}.plate", pl, "plate") + _color_errors(f"{w}.plate", pl.get("color"))
            errs += _choice(f"{w}.plate", pl, "style", ("block", "lines"))
            if "pad" in pl and not (isinstance(pl["pad"], list) and len(pl["pad"]) == 2 and all(isinstance(v, int) and 0 <= v <= 80 for v in pl["pad"])):
                errs.append(f"{w}.plate: pad must be [vertical_px, horizontal_px], 0-80")
        for k, lo, hi in (("line_height", 0.7, 2.0), ("letter_spacing", -5, 30)):
            if k in z and not (_num(z[k]) and lo <= z[k] <= hi):
                errs.append(f"{w}: {k} must be {lo}-{hi}")
        if "worn" in z and not isinstance(z["worn"], bool):
            errs.append(f"{w}: worn must be true or false")

    for i, z in enumerate(raw.get("lists") or []):
        w = f"lists[{i}]" + (f" (slot '{z.get('slot')}')" if isinstance(z, dict) and z.get("slot") else "")
        errs += _keys(w, z, "list")
        if not isinstance(z, dict):
            continue
        errs += bind(w, z.get("slot"), ("list",)) + _box_errors(w, z.get("box"))
        for k, opts in (("direction", ("column", "row")), ("layout", ("stack", "inline")), ("align", ALIGNS)):
            errs += _choice(w, z, k, opts)
        for k in ("plate", "emphasis_plate"):
            errs += _color_errors(f"{w}.{k}", z[k]) if z.get(k) is not None else []
        if not (isinstance(z.get("gap", 10), int) and 0 <= z.get("gap", 10) <= 80):
            errs.append(f"{w}: gap must be 0-80 px")
        for part in ("value", "label"):
            if part in z:
                errs += _keys(f"{w}.{part}", z[part], "item")
                if isinstance(z[part], dict):
                    errs += _font_errors(f"{w}.{part}", z[part]) + _choice(f"{w}.{part}", z[part], "case", CASES)
                    errs += _color_errors(f"{w}.{part}", z[part]["color"], auto=True) if "color" in z[part] else []

    if raw.get("logo") is not None:
        lg = raw["logo"]
        errs += _keys("logo", lg, "logo")
        if isinstance(lg, dict):
            errs += _box_errors("logo", lg.get("box")) + _font_errors("logo", lg)
            errs += _choice("logo", lg, "align", ALIGNS) + _choice("logo", lg, "valign", VALIGNS)
            errs += _color_errors("logo", lg["color"]) if "color" in lg else []
    if raw.get("rules") is not None:
        errs += _keys("rules", raw["rules"], "rules")
        if isinstance(raw["rules"], dict) and "max_words" in raw["rules"] and not (
                isinstance(raw["rules"]["max_words"], int) and 1 <= raw["rules"]["max_words"] <= 120):
            errs.append("rules: max_words must be 1-120")
    for name in slots:
        if name not in drawn:
            errs.append(f"slots.{name}: no zone draws this slot (add a text or list zone with \"slot\": \"{name}\")")
    return errs


# --------------------------------------------------------------------------- loading
def _defaults(raw: dict) -> dict:
    """The spec with every default filled in (what the renderer and lint read)."""
    s = json.loads(json.dumps(raw))
    s.setdefault("version", 1)
    s["canvas"] = {"w": 1080, "h": 1350, **(s.get("canvas") or {})}
    s.setdefault("visual_lane", "clean")
    s["background"] = {"fill": "bg", "gradient": None, "texture": "none", **(s.get("background") or {})}
    if s.get("art"):
        s["art"] = {"box": [0, 0, 1, 1], "blend": "normal", "opacity": 1, **s["art"]}
    lane_fit = "contain" if s["visual_lane"] == "screenshot" else "cover"
    s["photos"] = [{"photo": i, "fit": lane_fit, "focus": [0.5, 0.42], "duotone": None, "shade": [], "border": None,
                    "radius": 0, "contain_fallback": True, "optional": False, **z} for i, z in enumerate(s["photos"])]
    for z in s["photos"]:
        z["shade"] = [{"size": 0.4, "color": "bg", **sh} for sh in z["shade"] or []]
        if z["border"] is not None:
            z["border"] = {"width": 6, "color": "accent", **z["border"]}
    texts = []
    for z in s.get("text") or []:
        font = z.get("font", "display")
        plate = z.get("plate")
        if plate is not None:
            plate = {"style": "block", "pad": [6, 18], **(plate if isinstance(plate, dict) else {"color": plate})}
        texts.append({"font": font, "weight": WEIGHTS[font], "size": [24, 120], "max_lines": 2, "case": "upper",
                      "color": "auto" if plate else "ink", "align": "left", "valign": "top", "rotation": 0,
                      "fit": "block", "line_height": 1.25 if plate and plate["style"] == "lines" else 1.02,
                      "letter_spacing": 0, "highlight": None, "worn": False, "alt_box": None, **z, "plate": plate})
    s["text"] = texts
    lists = []
    for z in s.get("lists") or []:
        lists.append({"direction": "column", "gap": 10, "layout": "stack", "plate": "accent", "emphasis_plate": None,
                      "align": "center", **z,
                      "value": {"font": "display", "weight": WEIGHTS[(z.get("value") or {}).get("font", "display")],
                                "size": [32, 110], "color": "auto", "case": "upper", **(z.get("value") or {})},
                      "label": {"font": "condensed", "weight": WEIGHTS[(z.get("label") or {}).get("font", "condensed")],
                                "size": [14, 40], "color": "auto", "case": "upper", **(z.get("label") or {})}})
    s["lists"] = lists
    if s.get("logo"):
        font = s["logo"].get("font", "condensed")
        s["logo"] = {"font": font, "weight": WEIGHTS[font], "size": [16, 36], "color": "accent", "align": "right",
                     "valign": "middle", **s["logo"]}
    else:
        s["logo"] = None
    s["slots"] = {k: {"type": "text", "required": True, "examples": [], "hint": "",
                      **({"min_items": 1, "max_items": 4, "value_max_chars": 8, "label_max_words": 4, "label_max_chars": 24}
                         if v.get("type") == "list" else {}), **v} for k, v in s["slots"].items()}
    s["rules"] = {"max_words": MAX_WORDS, **(s.get("rules") or {})}
    return s


def load(src) -> dict:
    """A spec from a file path, JSON text or a dict, validated and with defaults filled in. Raises
    SpecError listing every problem."""
    where = ""
    if isinstance(src, Path) or (isinstance(src, str) and not src.lstrip().startswith("{")):
        where = f"{Path(src).name}: "
        try:
            src = Path(src).read_text()
        except OSError as e:
            raise SpecError(f"{where}cannot read the spec ({e.strerror})")
    if isinstance(src, str):
        try:
            src = json.loads(src)
        except json.JSONDecodeError as e:
            raise SpecError(f"{where}not valid JSON (line {e.lineno}: {e.msg})")
    errs = validate(src)
    if errs:
        raise SpecError("\n".join(where + e for e in errs))
    return _defaults(src)


def page_specs(page: str) -> dict[str, dict]:
    """The page's stored specs by id (private/templates/<page>/*.json); a file that does not validate is left
    out here: lint names its problems when copy.json picks it."""
    out = {}
    for f in sorted((SPEC_DIR / page).glob("*.json")) if page else []:
        try:
            s = load(f)
        except SpecError:
            continue
        out[s["id"]] = s
    return out


def find(spec_id: str, brief: dict) -> dict:
    """The spec copy.json names: the brief's own copy first ("template_specs"), then the page's store.
    Raises SpecError saying which specs exist (or what is wrong with the one named)."""
    have = brief.get("template_specs") or {}
    if isinstance(have, list):
        have = {s.get("id"): s for s in have if isinstance(s, dict)}
    if spec_id in have:
        return load(have[spec_id])
    f = SPEC_DIR / str(brief.get("page") or "") / f"{spec_id}.json"
    if ID_RE.match(str(spec_id)) and f.exists():
        return load(f)
    known = sorted(set(have) | set(page_specs(brief.get("page") or "")))
    raise SpecError(f"template_spec {spec_id!r} is not one of this page's template specs"
                    + (f" ({', '.join(known)})" if known else " (the page has none)"))


# --------------------------------------------------------------------------- slots
def lines_of(value) -> list[str]:
    """A text slot's lines: a string is one block (wrapped to the zone), a list is one line each."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    if _num(value):
        return [str(value)]
    return []


def slot_strings(slots: dict) -> list[str]:
    """Every string the card shows from the slots (for lint and verify), in the copy's order."""
    out = []
    for v in (slots or {}).values():
        if isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
            out += [str(i[k]).strip() for i in v for k in ("value", "label") if str(i.get(k) or "").strip()]
        else:
            out += [str(x).strip() for x in lines_of(v)]
    return out


def slot_units(slots: dict) -> list[str]:
    """Each statement on the card (lint's certainty check): a text slot's lines together, a stat plate's
    value with its label."""
    out = []
    for v in (slots or {}).values():
        if isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
            out += [f"{i.get('value') or ''} {i.get('label') or ''}".strip() for i in v]
        elif lines_of(v):
            out.append(" ".join(str(x).strip() for x in lines_of(v)))
    return [u for u in out if u]


def static_words(spec: dict) -> set[str]:
    """The spec's fixed words (a "VS", a "THEN"): the layout adds them, like a logo."""
    return {w for z in spec.get("text") or [] if z.get("text") for w in re.split(r"\s+", z["text"].upper()) if w}


def example_slots(spec: dict) -> dict:
    """Slots filled from each slot's first example (a preview of a new spec without copy)."""
    out = {}
    for name, s in spec["slots"].items():
        ex = s.get("examples") or []
        if not ex:
            continue
        # a list slot's examples are one list of items, or several such lists
        out[name] = ex if s["type"] == "list" and isinstance(ex[0], dict) else ex[0]
    return out


def _words(lines: list[str]) -> int:
    return sum(len(l.split()) for l in lines)


def lint_slots(spec: dict, copy: dict) -> list[str]:
    """The spec's slot rules on copy.json's slots: required slots, word / character limits, list sizes,
    lines per zone, numbers, and the card's total words."""
    errs = []
    slots = copy.get("slots")
    if not isinstance(slots, dict):
        return [f"copy.json needs slots: the text for each of the template's slots ({', '.join(spec['slots'])})"]
    for k in slots:
        if k not in spec["slots"]:
            errs.append(f"slot '{k}' is not in the template (its slots: {', '.join(spec['slots'])})")
    zones = {z["slot"]: z for z in spec["text"] if z.get("slot")}
    for k, rule in spec["slots"].items():
        v = slots.get(k)
        if rule["type"] == "list":
            items = v if isinstance(v, list) else []
            if v is not None and not (isinstance(v, list) and all(isinstance(i, dict) for i in v)):
                errs.append(f"slot '{k}' is a list: [{{\"value\": \"...\", \"label\": \"...\"}}, ...]")
                continue
            if not items:
                if rule["required"]:
                    errs.append(f"slot '{k}' is required ({rule['min_items']}-{rule['max_items']} items)"
                                + (f": {rule['hint']}" if rule["hint"] else ""))
                continue
            if not rule["min_items"] <= len(items) <= rule["max_items"]:
                errs.append(f"slot '{k}' has {len(items)} items; the template takes {rule['min_items']}-{rule['max_items']}")
            for n, it in enumerate(items, 1):
                val, lab = str(it.get("value") or "").strip(), str(it.get("label") or "").strip()
                if not val:
                    errs.append(f"slot '{k}' item {n} needs a value")
                if len(val) > rule["value_max_chars"]:
                    errs.append(f"slot '{k}' item {n} value too long ({len(val)} > {rule['value_max_chars']} characters): '{val}'")
                if len(lab.split()) > rule["label_max_words"] or len(lab) > rule["label_max_chars"]:
                    errs.append(f"slot '{k}' item {n} label too long (max {rule['label_max_words']} words, "
                                f"{rule['label_max_chars']} characters): '{lab}'")
                unknown = set(it) - {"value", "label"}
                if unknown:
                    errs.append(f"slot '{k}' item {n}: only value and label are drawn (not {', '.join(sorted(unknown))})")
            continue
        lines = lines_of(v)
        if v is not None and not lines and not isinstance(v, (str, list)):
            errs.append(f"slot '{k}' is text: a string, or a list of lines")
            continue
        if not lines:
            if rule["required"]:
                errs.append(f"slot '{k}' is required" + (f": {rule['hint']}" if rule["hint"] else "")
                            + (f" (e.g. {rule['examples'][0]!r})" if rule["examples"] else ""))
            continue
        text = " ".join(l.strip() for l in lines)
        words = _words(lines)
        if rule.get("max_words") and words > rule["max_words"]:
            errs.append(f"slot '{k}' has {words} words (max {rule['max_words']}): '{text}'")
        if rule.get("min_words") and words < rule["min_words"]:
            errs.append(f"slot '{k}' has {words} words (min {rule['min_words']}): '{text}'")
        if rule.get("max_chars") and len(text) > rule["max_chars"]:
            errs.append(f"slot '{k}' is {len(text)} characters (max {rule['max_chars']}): '{text}'")
        if rule["type"] == "number" and not re.search(r"\d", text):
            errs.append(f"slot '{k}' is the template's number: it must contain digits ('{text}')")
        z = zones.get(k)
        if z and isinstance(v, list) and len(lines) > z["max_lines"]:
            errs.append(f"slot '{k}' has {len(lines)} lines; its zone holds {z['max_lines']}")
    total = sum(len(s.split()) for s in slot_strings({k: v for k, v in slots.items() if k in spec["slots"]}))
    if total > spec["rules"]["max_words"]:
        errs.append(f"too much text on the card ({total} words > {spec['rules']['max_words']}); cut it down")
    return errs


# --------------------------------------------------------------------------- colours
def _theme(card) -> dict:
    from .layouts import _contrast_ink, theme_of
    t = theme_of(card.theme)
    return {**t, "accent_ink": _contrast_ink(t["accent"]), "white": "#ffffff", "black": "#000000"}


def _hex(c: str, theme: dict) -> str:
    base = COLOR_RE.match(c).group(1)
    return base if base.startswith("#") else theme[base]


def color(c: str, theme: dict, on: str | None = None) -> str:
    """CSS colour for a role or hex with an optional /opacity; "auto" is the readable ink on `on` (the
    zone's plate, else the background)."""
    from .layouts import _contrast_ink
    m = COLOR_RE.match(c or "ink")
    base, alpha = m.group(1), m.group(2)
    hx = _contrast_ink(_hex(on or "bg", theme)) if base == "auto" else _hex(base, theme)
    if alpha is None or int(alpha) >= 100:
        return hx
    r, g, b = (int(hx[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{int(alpha) / 100:.2f})"


# --------------------------------------------------------------------------- face-safe plan
from .layouts import MIN_FACE  # noqa: E402  (the face height on the card the cover fit zooms towards)


def _head(f) -> tuple:
    """The protected area around a face (x, y, w, h; 0-1 of the photo): guard.head_zone's margins."""
    x, y, w, h = f[:4]
    return (x - 0.18 * w, y - 0.45 * h, w * 1.36, h * 1.53)


def _hits(a, b) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _clip(a, b):
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return r if r[2] > r[0] and r[3] > r[1] else None


def _inside(a, b, tol: float = 2.0) -> bool:
    return a[0] >= b[0] - tol and a[1] >= b[1] - tol and a[2] <= b[2] + tol and a[3] <= b[3] + tol


def px(box, canvas: dict) -> tuple[float, float, float, float]:
    """A fractional box as card pixels (x, y, w, h)."""
    W, H = canvas["w"], canvas["h"]
    return (box[0] * W, box[1] * H, box[2] * W, box[3] * H)


@dataclass
class Placement:
    mode: str               # cover | contain | empty
    left: float = 0.0       # the image's rectangle inside its zone (zone pixels)
    top: float = 0.0
    w: float = 0.0
    h: float = 0.0


@dataclass
class Plan:
    text_boxes: dict = field(default_factory=dict)    # text zone index -> card pixel box used (box or alt_box)
    photos: list = field(default_factory=list)        # Placement per photo zone
    problems: list = field(default_factory=list)


def _faces_of(card, p: int) -> list:
    faces = list(card.photo_faces[p] or []) if p < len(card.photo_faces or []) else []
    if p == 0 and not faces and card.face_box:
        faces = [card.face_box]
    return sorted(faces, key=lambda f: -f[2] * f[3])        # the subject (largest) first


def _keep_of(card, p: int) -> list:
    return list(card.keep[p] or []) if p < len(card.keep or []) else []


def _check(pl: Placement, zone_px, faces, keep, covers) -> str:
    """'' when the placement keeps the subject's face whole and every visible face, watermark and
    credit clear of the zones drawn over the photo; else what fails."""
    bx, by, bw, bh = zone_px
    box = (bx, by, bx + bw, by + bh)

    def rect(r):
        return (bx + pl.left + r[0] * pl.w, by + pl.top + r[1] * pl.h,
                bx + pl.left + (r[0] + r[2]) * pl.w, by + pl.top + (r[1] + r[3]) * pl.h)
    for n, f in enumerate(faces):
        if n == 0 and not _inside(rect(f), box):
            return "face"
        hz = _clip(rect(_head(f)), box)
        if hz and any(_hits(hz, c) for c in covers):
            return "face"
    for k in keep:
        r = rect(k)
        if not _inside(r, box, 1.0) or any(_hits(r, c) for c in covers):
            return "keep"
    return ""


def _place(size, zone, zone_px, faces, keep, covers, contain: bool) -> tuple[Placement | None, str]:
    """The placement nearest the zone's focus target that passes _check (cover), or the whole photo
    (contain). Cover zooms in on a small face first, like the generic layouts."""
    iw, ih = size
    bx, by, bw, bh = zone_px
    if contain:
        s = min(bw / iw, bh / ih)
        pl = Placement("contain", (bw - iw * s) / 2, (bh - ih * s) / 2, iw * s, ih * s)
        why = _check(pl, zone_px, faces, keep, covers)
        return (None if why else pl), why
    s = max(bw / iw, bh / ih)
    zooms = [1.0]
    if faces and faces[0][3] * ih * s < MIN_FACE:
        zooms.insert(0, max(1.0, min(MIN_FACE / (faces[0][3] * ih * s), 1.6 / s, 3.0)))
    tx, ty = zone["focus"]
    if faces:
        f = faces[0]
        fx, fy = f[0] + f[2] / 2, f[1] + f[3] / 2
    else:
        fx, fy = 0.5, 0.3
    why = ""
    for z in dict.fromkeys(zooms):
        sw, sh = iw * s * z, ih * s * z
        lo_x, lo_y = bw - sw, bh - sh
        pref = (min(0.0, max(lo_x, bw * tx - fx * sw)), min(0.0, max(lo_y, bh * ty - fy * sh)))
        steps = 24
        grid = {(lo_x + (0 - lo_x) * i / steps, lo_y + (0 - lo_y) * j / steps)
                for i in range(steps + 1) for j in range(steps + 1)}
        for left, top in [pref] + sorted(grid, key=lambda p: (p[0] - pref[0]) ** 2 + (p[1] - pref[1]) ** 2):
            pl = Placement("cover", left, top, sw, sh)
            why = _check(pl, zone_px, faces, keep, covers)
            if not why:
                return pl, ""
            if not (faces or keep):
                break
    return None, why


def _image_size(img: bytes) -> tuple[int, int]:
    from PIL import Image
    return Image.open(io.BytesIO(img)).size


def plan(card) -> Plan:
    """Where each photo sits and which box each text zone uses, so no zone drawn over a photo touches
    a face (the head zone verify protects) or a watermark/credit in it. Tries, in order: the photo
    moved or zoomed inside its zone; the text zones' alt_box positions; the whole photo shown
    (contain). problems is set when nothing keeps the face clear (cards.render raises FaceBlocked)."""
    spec, cv = card.tspec, card.tspec["canvas"]
    photos = list(card.photos or [])
    logo_drawn = bool(spec["logo"] and _theme(card)["logo_text"])
    alts = [i for i, z in enumerate(spec["text"]) if z.get("alt_box") and _zone_text(z, card)]
    configs = [()] + [c for n in range(1, len(alts) + 1) for c in combinations(alts, n)][:15]
    drawn = [i for i, z in enumerate(spec["photos"]) if z["photo"] < len(photos) or not z["optional"]]
    sizes = [_image_size(p) for p in photos]

    def text_boxes(cfg) -> dict:
        return {i: px(z["alt_box"] if i in cfg else z["box"], cv) for i, z in enumerate(spec["text"])
                if _zone_text(z, card)}

    def covers_for(n: int, tb: dict) -> list:
        rects = [(x, y, x + w, y + h) for x, y, w, h in tb.values()]
        rects += [_r(px(z["box"], cv)) for z in spec["lists"] if (card.slots or {}).get(z["slot"])]
        rects += [_r(px(spec["logo"]["box"], cv))] if logo_drawn else []
        rects += [_r(px(spec["photos"][m]["box"], cv)) for m in drawn if m > n]
        rects += _shade_rects(spec["photos"][n], cv) + _frame_rects(spec["photos"][n], cv)
        return rects

    last_why = "face"
    for allow_contain in (False, True):
        for cfg in configs:
            tb = text_boxes(cfg)
            out, ok = [], True
            for n, z in enumerate(spec["photos"]):
                p = z["photo"]
                if p >= len(photos):
                    out.append(Placement("empty"))
                    continue
                zone_px = px(z["box"], cv)
                faces, keep, covers = _faces_of(card, p), _keep_of(card, p), covers_for(n, tb)
                size = sizes[p]
                pl, why = _place(size, z, zone_px, faces, keep, covers, contain=z["fit"] == "contain")
                if pl is None and allow_contain and z["fit"] == "cover" and z["contain_fallback"]:
                    pl, why = _place(size, z, zone_px, faces, keep, covers, contain=True)
                if pl is None:
                    ok, last_why = False, why or last_why
                    break
                out.append(pl)
            if ok:
                return Plan(tb, out, [])
    # nothing works: draw the first try (a preview) and say why
    tb = text_boxes(())
    out = []
    for z in spec["photos"]:
        p = z["photo"]
        if p >= len(photos):
            out.append(Placement("empty"))
            continue
        iw, ih = sizes[p]
        x, y, w, h = px(z["box"], cv)
        s = max(w / iw, h / ih)
        out.append(Placement("cover", (w - iw * s) / 2, (h - ih * s) / 2, iw * s, ih * s))
    # never the template's name: this message reaches the run reports (the document is confidential)
    if last_why == "keep":
        why = ("a watermark or credit in the photo cannot be kept whole and uncovered in the template you chose "
               "(it is never cropped or covered): choose another template, or a photo without one where the text goes")
    else:
        why = ("the subject's face cannot be kept clear of the text in the template you chose with this photo: "
               "choose a photo where the face is smaller or positioned differently, or another template")
    return Plan(tb, out, [why])


def _r(b) -> tuple:
    return (b[0], b[1], b[0] + b[2], b[1] + b[3])


def _shade_rects(zone: dict, cv: dict) -> list:
    """The darker half of each shade on a photo (over half its strength): no face sits in the dark."""
    x, y, w, h = px(zone["box"], cv)
    out = []
    for sh in zone["shade"]:
        d = sh["size"] / 2
        out.append({"top": (x, y, x + w, y + h * d), "bottom": (x, y + h * (1 - d), x + w, y + h),
                    "left": (x, y, x + w * d, y + h), "right": (x + w * (1 - d), y, x + w, y + h)}[sh["edge"]])
    return out


def _frame_rects(zone: dict, cv: dict) -> list:
    """What the zone's own frame hides of its photo: the border band drawn over its edges, and the
    corners its radius cuts off (each corner's radius x radius square): no face or credit sits there."""
    x, y, w, h = px(zone["box"], cv)
    out = []
    b = zone["border"]["width"] if zone["border"] else 0
    if b:
        out += [(x, y, x + w, y + b), (x, y + h - b, x + w, y + h), (x, y, x + b, y + h), (x + w - b, y, x + w, y + h)]
    r = min(zone["radius"], w / 2, h / 2)
    if r:
        out += [(x, y, x + r, y + r), (x + w - r, y, x + w, y + r), (x, y + h - r, x + r, y + h), (x + w - r, y + h - r, x + w, y + h)]
    return out


# --------------------------------------------------------------------------- HTML
def _cased(s: str, case: str) -> str:
    return {"upper": s.upper(), "lower": s.lower(), "title": s.title()}.get(case, s)


def _zone_text(z: dict, card):
    """What a text zone shows: its slot's lines (list) or block (str), or its fixed text; None = nothing."""
    if z.get("text"):
        return z["text"]
    v = (card.slots or {}).get(z["slot"])
    lines = lines_of(v)
    if not lines:
        return None
    return lines if isinstance(v, list) else lines[0]


FIT_JS = """
async function tfit() {
  await document.fonts.ready;
  const over = [];
  const px = (e, s) => { e.style.fontSize = s + 'px'; };
  const nLines = el => [...el.querySelectorAll('.ln')].reduce((n, s) => n + s.getClientRects().length, 0);
  for (const z of document.querySelectorAll('.tz')) {
    const tx = z.querySelector('.tx'), d = tx.dataset, min = +d.min, max = +d.max, lines = +d.lines;
    const cs = getComputedStyle(tx);
    const avail = z.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    const rows = [...tx.querySelectorAll('.row .ln')];
    const wide = () => rows.some(r => r.getBoundingClientRect().width > avail + 0.5) || tx.scrollWidth > tx.clientWidth + 0.5;
    // a plate behind each line paints its padding and the font's full height outside the line box:
    // the block makes room for it, and every plate stays inside the zone (the planner keeps faces
    // clear of the zone, so of everything drawn in it)
    const plated = tx.classList.contains('plated'), zr = z.getBoundingClientRect();
    const room = () => { if (!plated) return; tx.style.paddingTop = tx.style.paddingBottom = '0px';
      const top = Math.min(...[...tx.querySelectorAll('.ln')].map(l => l.getClientRects()[0].top));
      const out = Math.max(0, Math.ceil(tx.getBoundingClientRect().top - top));
      tx.style.paddingTop = tx.style.paddingBottom = out + 'px'; };
    const inside = () => !plated || [...tx.querySelectorAll('.ln')].every(l => [...l.getClientRects()].every(r =>
      r.top >= zr.top - 0.5 && r.bottom <= zr.bottom + 0.5 && r.left >= zr.left - 0.5 && r.right <= zr.right + 0.5));
    const fits = () => { room(); return !wide() && tx.offsetHeight <= z.clientHeight + 0.5 && nLines(tx) <= lines && inside(); };
    let ok = false;
    if (d.each === '1' && rows.length) {
      // a stacked headline: each line as large as the width allows, all capped until the block fits
      for (let cap = max; cap >= min && !ok; cap -= 2) {
        for (const r of rows) { let s = cap; px(r, s); while (r.getBoundingClientRect().width > avail + 0.5 && s > min) { s -= 2; px(r, s); } }
        ok = fits();
      }
    } else {
      for (let s = max; s >= min && !ok; s -= 2) { px(tx, s); ok = fits(); }
    }
    if (!ok) over.push(z.dataset.name);
  }
  for (const z of document.querySelectorAll('.lz')) {
    const d = z.dataset, pls = [...z.querySelectorAll('.pl')];
    const spill = () => pls.some(p => p.scrollWidth > p.clientWidth + 0.5 || p.scrollHeight > p.clientHeight + 0.5);
    let ok = false;
    // values and labels shrink together, the same size on every plate so the stack reads as one
    for (let k = 0; !ok; k++) {
      const v = Math.max(+d.vmin, +d.vmax - 2 * k), l = Math.max(+d.lmin, +d.lmax - k);
      pls.forEach(p => { p.querySelectorAll('.v').forEach(e => px(e, v)); p.querySelectorAll('.l').forEach(e => px(e, l)); });
      ok = !spill();
      if (v === +d.vmin && l === +d.lmin) break;
    }
    if (!ok) over.push(z.dataset.name);
  }
  document.body.dataset.overflow = over.join(',');
  document.body.dataset.ready = '1';
}
tfit();
"""

TEXTURE_CSS = {
    "stripes": "repeating-linear-gradient(135deg, rgba(255,255,255,.05) 0 18px, transparent 18px 36px)",
    "halftone": "radial-gradient(rgba(255,255,255,.10) 22%, transparent 24%) 0 0/14px 14px",
}


def _duotone(n: int, pair, theme) -> str:
    cols = [color(c, theme)[:7] for c in pair]
    rgb = [[int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)] for c in cols]
    fn = "".join(f'<feFunc{ch} type="table" tableValues="{rgb[0][k]:.3f} {rgb[1][k]:.3f}"/>' for k, ch in enumerate("RGB"))
    return (f'<filter id="duo{n}" color-interpolation-filters="sRGB"><feColorMatrix type="matrix" values="0.299 0.587 0.114 0 0 '
            f'0.299 0.587 0.114 0 0 0.299 0.587 0.114 0 0 0 0 0 1 0"/><feComponentTransfer>{fn}</feComponentTransfer></filter>')


SHADE_DIR = {"bottom": "to top", "top": "to bottom", "left": "to right", "right": "to left"}


def build_html(card, plan_: Plan | None = None) -> str:
    """The card as HTML: background, art, photo zones, then text, list and logo zones, in the theme."""
    from .cards import BASE_CSS, DISTRESS_CSS, _data_uri, _esc, _hl, _img_mime
    spec = card.tspec
    cv, th = spec["canvas"], _theme(card)
    W, H = cv["w"], cv["h"]
    pl = plan_ or plan(card)
    photos = list(card.photos or [])
    bg = spec["background"]
    fill = color(bg["fill"], th)
    layers = [f'<div class="bgfill" style="background:{fill}"></div>']
    if bg["gradient"]:
        g = bg["gradient"]
        to = color(g["to"], th)
        grad = (f"radial-gradient(ellipse at 50% 40%, transparent 20%, {to} 100%)" if g.get("direction") == "radial"
                else f"linear-gradient({g.get('direction', 'to bottom')}, transparent 0%, {to} 100%)")
        layers.append(f'<div class="bgfill" style="background:{grad}"></div>')
    tex = bg["texture"]
    if tex in ("grit", "worn"):
        layers.append('<div class="grit"></div>')
    if tex in ("noise", "worn"):
        layers.append('<div class="noise"></div>')
    if tex in TEXTURE_CSS:
        layers.append(f'<div class="bgfill" style="background:{TEXTURE_CSS[tex]}"></div>')
    if card.art and spec.get("art"):
        a = spec["art"]
        x, y, w, h = px(a["box"], cv)
        layers.append(f'<div class="art" style="left:{x:.0f}px;top:{y:.0f}px;width:{w:.0f}px;height:{h:.0f}px;'
                      f'background:url(\'{_data_uri(card.art, _img_mime(card.art))}\') center/cover;'
                      f'mix-blend-mode:{a["blend"]};opacity:{a["opacity"]}"></div>')
    filters = []
    for n, (z, place) in enumerate(zip(spec["photos"], pl.photos)):
        x, y, w, h = px(z["box"], cv)
        if place.mode == "empty":
            if not z["optional"]:
                layers.append(f'<div class="ph empty" style="left:{x:.0f}px;top:{y:.0f}px;width:{w:.0f}px;height:{h:.0f}px"></div>')
            continue
        img = photos[z["photo"]]
        uri = _data_uri(img, _img_mime(img))
        duo = ""
        if z["duotone"]:
            filters.append(_duotone(n, z["duotone"], th))
            duo = f"url(#duo{n}) "
        flt = f"filter:{duo};" if duo else ""
        inner = ""
        if place.mode == "contain":   # the whole photo over a blurred copy of itself: nothing cropped
            inner += f'<img src="{uri}" class="blur" style="filter:{duo}blur(36px) brightness(.45)">'
        inner += (f'<img src="{uri}" style="position:absolute;left:{place.left:.1f}px;top:{place.top:.1f}px;'
                  f'width:{place.w:.1f}px;height:{place.h:.1f}px;{flt}">')
        for sh in z["shade"]:
            c = color(sh["color"], th)
            inner += (f'<div class="shade" style="background:linear-gradient({SHADE_DIR[sh["edge"]]}, {c} 0%, '
                      f'transparent {sh["size"] * 100:.0f}%)"></div>')
        if z["border"] and z["border"]["width"]:
            inner += (f'<div class="shade" style="border:{z["border"]["width"]}px solid {color(z["border"]["color"], th)};'
                      f'border-radius:{z["radius"]}px"></div>')
        layers.append(f'<div class="ph" style="left:{x:.0f}px;top:{y:.0f}px;width:{w:.0f}px;height:{h:.0f}px;'
                      f'border-radius:{z["radius"]}px">{inner}</div>')
    hi = card.highlight or ""
    for i, z in enumerate(spec["text"]):
        text = _zone_text(z, card)
        if text is None:
            continue
        x, y, w, h = pl.text_boxes[i]
        plate = z["plate"]
        on = plate["color"] if plate else spec["background"]["fill"]
        ink = color(z["color"], th, on=on)
        hl_col = color(z["highlight"] or ("ink" if z["color"] == "accent" else "accent"), th)
        if isinstance(text, list):
            body = "".join(f'<div class="row"><span class="ln">{_hl(_cased(l, z["case"]), _cased(hi, z["case"]), "hi")}</span></div>'
                           for l in text)
        else:
            body = f'<span class="ln">{_hl(_cased(text, z["case"]), _cased(hi, z["case"]), "hi")}</span>'
        tx_style = (f'font-family:{FONTS[z["font"]]};font-weight:{z["weight"]};color:{ink};line-height:{z["line_height"]};'
                    f'letter-spacing:{z["letter_spacing"]}px;text-align:{z["align"]};--hi:{hl_col};')
        if plate and plate["style"] == "block":
            tx_style += f'background:{color(plate["color"], th)};padding:{plate["pad"][0]}px {plate["pad"][1]}px;'
        elif plate:      # a plate behind each line (stacked boxes)
            tx_style += f'--plate:{color(plate["color"], th)};--py:{plate["pad"][0]}px;--px:{plate["pad"][1]}px;'
        name = z.get("slot") or f"fixed text {i + 1}"       # what an overflow names (never the document's words)
        layers.append(
            f'<div class="tz" data-name="{_esc(name)}" style="left:{x:.0f}px;top:{y:.0f}px;width:{w:.0f}px;height:{h:.0f}px;'
            f'justify-content:{ {"top": "flex-start", "middle": "center", "bottom": "flex-end"}[z["valign"]] };'
            f'align-items:{ {"left": "flex-start", "center": "center", "right": "flex-end"}[z["align"]] }">'
            f'<div class="tx{" plated" if plate and plate["style"] == "lines" else ""}{" hl-line" if z["worn"] else ""}"'
            f' data-min="{z["size"][0]}" data-max="{z["size"][1]}" data-lines="{z["max_lines"]}"'
            f' data-each="{1 if z["fit"] == "each_line" else 0}" style="{tx_style}">{body}</div></div>')
    for z in spec["lists"]:
        items = [it for it in (card.slots or {}).get(z["slot"]) or [] if isinstance(it, dict)]
        if not items:
            continue
        x, y, w, h = px(z["box"], cv)
        v, lab = z["value"], z["label"]
        cells = ""
        for n, it in enumerate(items):
            pc = z["emphasis_plate"] if (z["emphasis_plate"] and n == len(items) - 1) else z["plate"]
            vcol, lcol = color(v["color"], th, on=pc), color(lab["color"], th, on=pc)
            val = _esc(_cased(str(it.get("value") or ""), v["case"]))
            lbl = _esc(_cased(str(it.get("label") or ""), lab["case"]))
            side = {"left": "flex-start", "center": "center", "right": "flex-end"}[z["align"]]
            # stack: value over label, aligned across; inline: value then label along the plate
            flow = (f"align-items:{side};justify-content:center" if z["layout"] == "stack"
                    else f"justify-content:{side};align-items:center")
            cells += (f'<div class="pl {z["layout"]}" style="background:{color(pc, th)};text-align:{z["align"]};{flow}">'
                      f'<span class="v" style="font-family:{FONTS[v["font"]]};font-weight:{v["weight"]};color:{vcol}">{val}</span>'
                      + (f'<span class="l" style="font-family:{FONTS[lab["font"]]};font-weight:{lab["weight"]};color:{lcol}">{lbl}</span>'
                         if lbl else "") + "</div>")
        layers.append(f'<div class="lz" data-name="{_esc(z["slot"])}" data-vmin="{v["size"][0]}" data-vmax="{v["size"][1]}" '
                      f'data-lmin="{lab["size"][0]}" data-lmax="{lab["size"][1]}" style="left:{x:.0f}px;top:{y:.0f}px;'
                      f'width:{w:.0f}px;height:{h:.0f}px;flex-direction:{z["direction"]};gap:{z["gap"]}px">{cells}</div>')
    if spec["logo"] and th["logo_text"]:
        lg = spec["logo"]
        x, y, w, h = px(lg["box"], cv)
        layers.append(
            f'<div class="tz" data-name="logo" style="left:{x:.0f}px;top:{y:.0f}px;width:{w:.0f}px;height:{h:.0f}px;'
            f'justify-content:{ {"top": "flex-start", "middle": "center", "bottom": "flex-end"}[lg["valign"]] };'
            f'align-items:{ {"left": "flex-start", "center": "center", "right": "flex-end"}[lg["align"]] }">'
            f'<div class="tx" data-min="{lg["size"][0]}" data-max="{lg["size"][1]}" data-lines="1" data-each="0" '
            f'style="font-family:{FONTS[lg["font"]]};font-weight:{lg["weight"]};color:{color(lg["color"], th)};'
            f'letter-spacing:3px;line-height:1"><span class="ln">{_esc(th["logo_text"].upper())}</span></div></div>')
    css = f"""{BASE_CSS}
html, body {{ width:{W}px; height:{H}px; }}
.card {{ width:{W}px; height:{H}px; background:{fill}; }}
.bgfill, .art {{ position:absolute; inset:0; }}
.art {{ inset:auto; }}
.ph {{ position:absolute; overflow:hidden; background:#000; }}
.ph img {{ display:block; }}
.ph img.blur {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; filter:blur(36px) brightness(.45); transform:scale(1.15); }}
.ph.empty {{ background: repeating-linear-gradient(45deg, #222 0 20px, #333 20px 40px); }}
.shade {{ position:absolute; inset:0; pointer-events:none; }}
.tz {{ position:absolute; display:flex; flex-direction:column; }}
.tx {{ max-width:100%; }}
.tx .row {{ white-space:nowrap; }}
.tx .hi {{ color:var(--hi); }}
.tx.plated .ln {{ background:var(--plate); padding:var(--py) var(--px); -webkit-box-decoration-break:clone; box-decoration-break:clone; }}
.lz {{ position:absolute; display:flex; }}
.pl {{ flex:1 1 0; min-width:0; min-height:0; overflow:hidden; display:flex; padding:6px 16px; }}
.pl.stack {{ flex-direction:column; }}
.pl.inline {{ flex-direction:row; gap:14px; }}
.pl span {{ white-space:nowrap; line-height:1; }}
.pl .l {{ letter-spacing:1px; margin-top:4px; }}
.pl.inline .l {{ margin-top:0; }}
"""
    if card.distress:
        css += DISTRESS_CSS
    svg = f'<svg width="0" height="0" style="position:absolute">{"".join(filters)}</svg>' if filters else ""
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body>{svg}<div class="card">'
            f'{"".join(layers)}</div><script>{FIT_JS}</script></body></html>')


# --------------------------------------------------------------------------- the card
def _decode(img: bytes):
    import cv2
    import numpy as np
    return cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)


CARD_SIDE = 1350          # verify looks for faces on the card: the photo is looked at this size too


def _iou(a, b) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy / max(1e-9, a[2] * a[3] + b[2] * b[3] - ix * iy)


def detect(img: bytes) -> list[tuple[float, float, float, float]]:
    """Faces in a photo, as (x, y, w, h) fractions of it (the detector verify uses), found at the
    photo's own size and at the card's: a large photo's face the detector misses at full size is
    found by verify on the card (a 4000 px close-up), so the plan protects both."""
    import cv2

    from .guard import detect_faces
    im = _decode(img)
    if im is None:
        return []

    def look(m) -> list:
        h, w = m.shape[:2]
        # verify's second, gentler look too (profiles), ignoring specks: what it checks, the plan protects
        found = detect_faces(m) or [f for f in detect_faces(m, min_score=0.5) if f[3] >= 40]
        return [(x / w, y / h, fw / w, fh / h) for x, y, fw, fh, _ in found]
    out = look(im)
    k = CARD_SIDE / max(im.shape[:2])
    if k < 0.8:
        out += [f for f in look(cv2.resize(im, None, fx=k, fy=k, interpolation=cv2.INTER_AREA))
                if not any(_iou(f, g) > 0.3 for g in out)]
    return out


CREDIT_RE = re.compile(r"(©|\(c\)|\bgetty\b|\bimagn\b|\breuters\b|\bap photo\b|\balamy\b|\bshutterstock\b|"
                       r"\busa today\b|\bphoto(?:graph)? by\b|\bcredit\b)", re.I)


def watermark_boxes(img: bytes) -> list[tuple[float, float, float, float]]:
    """Watermarks and credits in a photo (OCR: a site, a handle, an agency credit), as (x, y, w, h)
    fractions: the renderer never crops or covers them (the owner's rule: never crop or remove a
    watermark)."""
    import tempfile

    from .guard import _ocr
    from .stills import WATERMARK_RE
    from PIL import Image
    iw, ih = Image.open(io.BytesIO(img)).size
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        f.write(img)
        f.flush()
        found = _ocr(f.name)
    out = []
    for t, conf, (x0, y0, x1, y1) in found:
        if conf >= 0.5 and (WATERMARK_RE.search(t) or CREDIT_RE.search(t)):
            out.append((x0 / iw, y0 / ih, (x1 - x0) / iw, (y1 - y0) / ih))
    return out


def card_spec(spec: dict, copy: dict, photos: list[bytes], theme: dict | None = None, art: bytes | None = None,
              faces: list | None = None, keep: list | None = None):
    """The CardSpec that draws copy.json (template_spec + slots) with this spec. photos[0] is the editor's
    chosen photo, the others copy.extra_photo_indexes in order. faces / keep: per photo, (x, y, w, h)
    fractions of the faces and of the watermarks/credits to keep whole; found here when not given."""
    from .cards import CardSpec
    faces = [detect(p) for p in photos] if faces is None else faces
    keep = [watermark_boxes(p) for p in photos] if keep is None else keep
    f0 = sorted(faces[0], key=lambda f: -f[2] * f[3])[0] if faces and faces[0] else None
    return CardSpec(template=spec["id"], headline_lines=list(copy.get("headline_lines") or []),
                    highlight=copy.get("highlight", "") or "", photos=list(photos), art=art, theme=theme,
                    focus=(f0[0] + f0[2] / 2, f0[1] + f0[3] / 2) if f0 else None, face_box=f0,
                    face_size=f0[3] if f0 else 0.0, tspec=spec, slots=dict(copy.get("slots") or {}),
                    photo_faces=[list(f or []) for f in faces], keep=[list(k or []) for k in keep])


# the stand-in's face in sample_photos(): the face proper; verify's head zone around it takes in the
# whole head (hair and margin), as it would for a real face
SAMPLE_FACE = (0.40, 0.22, 0.20, 0.20)
SAMPLE_TINTS = ((70, 96, 128), (128, 92, 70), (78, 120, 90), (110, 84, 124))


def art_stand_in(spec: dict) -> bytes | None:
    """For a preview: the art layer's box hatched and labelled, so the editors approving it see where
    the AI artwork goes (None: the spec has no art layer)."""
    if not spec.get("art"):
        return None
    from PIL import Image, ImageDraw, ImageFont
    _, _, w, h = px(spec["art"]["box"], spec["canvas"])
    w, h = max(1, round(w)), max(1, round(h))
    im = Image.new("RGB", (w, h), (52, 56, 64))
    d = ImageDraw.Draw(im)
    for x in range(-h, w, 36):
        d.line([(x, h), (x + h, 0)], fill=(92, 98, 110), width=10)
    font = ImageFont.truetype(str(ROOT / "assets" / "fonts" / "Inter.ttf"), 34)
    d.text((w / 2, min(h - 30, 60)), "AI ART HERE", fill=(235, 235, 235), font=font, anchor="ms")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def photo_count(spec: dict) -> int:
    """How many photos the spec shows (photo 0, the editor's, and its extra photo zones)."""
    return max(z["photo"] for z in spec["photos"]) + 1


def sample_photos(n: int = 1) -> tuple[list[bytes], list[list[tuple]]]:
    """Neutral stand-in photos for a spec's preview, no real person: a plain backdrop with a grey head
    and shoulders, a different tint for each photo the spec shows; and where each one's face is, so the
    preview keeps it clear as a card keeps a real face."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    w, h = 1200, 1500
    fx, fy, fw, fh = SAMPLE_FACE
    out = []
    for i in range(max(1, n)):
        r, g, b = SAMPLE_TINTS[i % len(SAMPLE_TINTS)]
        im = Image.new("RGB", (w, h))
        d = ImageDraw.Draw(im)
        for y in range(h):                  # light at the top, darker below, like a studio backdrop
            k = 1.3 - 0.6 * y / h
            d.line([(0, y), (w, y)], fill=tuple(min(255, int(c * k)) for c in (r, g, b)))
        d.ellipse([0.16 * w, 0.5 * h, 0.84 * w, 1.3 * h], fill=(128, 134, 142))                 # shoulders
        d.rectangle([0.45 * w, 0.4 * h, 0.55 * w, 0.56 * h], fill=(150, 156, 164))              # neck
        d.ellipse([(fx - 0.01) * w, (fy - 0.06) * h, (fx + fw + 0.01) * w, (fy + fh + 0.02) * h],
                  fill=(170, 176, 184))                                                          # head
        font = ImageFont.truetype(str(ROOT / "assets" / "fonts" / "Inter.ttf"), 30)
        d.text((w / 2, 0.96 * h), f"SAMPLE PHOTO {i + 1}", fill=(235, 235, 235), font=font, anchor="ms")
        buf = io.BytesIO()
        im.filter(ImageFilter.GaussianBlur(1.2)).save(buf, "JPEG", quality=90)
        out.append(buf.getvalue())
    return out, [[SAMPLE_FACE] for _ in out]


def covering_boxes(card, plan_: Plan) -> list[tuple[str, tuple]]:
    """(name, card pixel x0, y0, x1, y1) of every zone drawn over the photos (tests, previews)."""
    spec, cv = card.tspec, card.tspec["canvas"]
    out = [(spec["text"][i].get("slot") or "text", _r(b)) for i, b in plan_.text_boxes.items()]
    out += [(z["slot"], _r(px(z["box"], cv))) for z in spec["lists"] if (card.slots or {}).get(z["slot"])]
    if spec["logo"] and _theme(card)["logo_text"]:
        out.append(("logo", _r(px(spec["logo"]["box"], cv))))
    return out


def face_on_card(card, plan_: Plan, zone: int, face) -> tuple:
    """Where a face (x, y, w, h; 0-1 of the zone's photo) lands on the card (x0, y0, x1, y1)."""
    z, pl = card.tspec["photos"][zone], plan_.photos[zone]
    bx, by, _, _ = px(z["box"], card.tspec["canvas"])
    return (bx + pl.left + face[0] * pl.w, by + pl.top + face[1] * pl.h,
            bx + pl.left + (face[0] + face[2]) * pl.w, by + pl.top + (face[1] + face[3]) * pl.h)


__all__ = ["SpecError", "validate", "load", "find", "page_specs", "lint_slots", "slot_strings", "static_words",
           "example_slots", "plan", "build_html", "card_spec", "watermark_boxes", "detect", "Plan", "Placement",
           "sample_photos", "photo_count", "art_stand_in", "SAMPLE_FACE"]
