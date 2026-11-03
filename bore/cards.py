"""Post cards: HTML templates that recreate each page's own layouts, rendered to a 1080x1350
JPEG with headless Chromium. Text is real type (never diffusion-drawn), photos are real.

Templates
  culture-rush: headline (subject photo left, stacked white/yellow caps right, dek, logo),
                casefile (true crime: polaroid photos, paper note, headline)
  huddle:       quote (team colour, cut-out speaker, label + quote boxes, highlight word),
                report (full-bleed photo, REPORT stamp, huge headline, stat bar, NEXT UP footer),
                topic  (field green, stacked yellow/white question boxes, photo inset)
  any page:     the generic layouts (bore/layouts.py), and template specs: a template as data
                (bore/tspec.py, docs/TEMPLATE_SPEC.md), drawn when CardSpec.tspec is set
"""
from __future__ import annotations

import base64
import html
import io
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT

W, H = 1080, 1350
FONTS = ROOT / "assets" / "fonts"
TEAMS_DIR = ROOT / "assets" / "teams"

# Primary, secondary, accent (hex). Used for team-colour backgrounds, bars and highlights.
NFL_TEAMS = {
    "cardinals": ("#97233F", "#000000", "#FFB612"), "falcons": ("#A71930", "#000000", "#A5ACAF"),
    "ravens": ("#241773", "#000000", "#9E7C0C"), "bills": ("#00338D", "#C60C30", "#FFFFFF"),
    "panthers": ("#0085CA", "#101820", "#BFC0BF"), "bears": ("#0B162A", "#C83803", "#FFFFFF"),
    "bengals": ("#FB4F14", "#000000", "#FFFFFF"), "browns": ("#311D00", "#FF3C00", "#FFFFFF"),
    "cowboys": ("#041E42", "#869397", "#FFFFFF"), "broncos": ("#FB4F14", "#002244", "#FFFFFF"),
    "lions": ("#0076B6", "#B0B7BC", "#FFFFFF"), "packers": ("#203731", "#FFB612", "#FFFFFF"),
    "texans": ("#03202F", "#A71930", "#FFFFFF"), "colts": ("#002C5F", "#A2AAAD", "#FFFFFF"),
    "jaguars": ("#006778", "#D7A22A", "#101820"), "chiefs": ("#E31837", "#FFB81C", "#FFFFFF"),
    "raiders": ("#000000", "#A5ACAF", "#FFFFFF"), "chargers": ("#0080C6", "#FFC20E", "#FFFFFF"),
    "rams": ("#003594", "#FFA300", "#FFFFFF"), "dolphins": ("#008E97", "#FC4C02", "#FFFFFF"),
    "vikings": ("#4F2683", "#FFC62F", "#FFFFFF"), "patriots": ("#002244", "#C60C30", "#B0B7BC"),
    "saints": ("#101820", "#D3BC8D", "#FFFFFF"), "giants": ("#0B2265", "#A71930", "#FFFFFF"),
    "jets": ("#125740", "#FFFFFF", "#000000"), "eagles": ("#004C54", "#A5ACAF", "#FFFFFF"),
    "steelers": ("#101820", "#FFB612", "#FFFFFF"), "49ers": ("#AA0000", "#B3995D", "#FFFFFF"),
    "seahawks": ("#002244", "#69BE28", "#A5ACAF"), "buccaneers": ("#D50A0A", "#34302B", "#FF7900"),
    "titans": ("#0C2340", "#4B92DB", "#C8102E"), "commanders": ("#5A1414", "#FFB612", "#FFFFFF"),
    "nfl": ("#013369", "#D50A0A", "#FFFFFF"),
}
# The teams' cities: where a team plays ("Green Bay Packers"), never whom a story is about
NFL_CITIES = ("Arizona", "Atlanta", "Baltimore", "Buffalo", "Carolina", "Chicago", "Cincinnati", "Cleveland", "Dallas",
              "Denver", "Detroit", "Green Bay", "Houston", "Indianapolis", "Jacksonville", "Kansas City", "Las Vegas",
              "Los Angeles", "Miami", "Minnesota", "New England", "New Orleans", "New York", "Philadelphia",
              "Pittsburgh", "San Francisco", "Seattle", "Tampa Bay", "Tennessee", "Washington")
TEAM_ALIASES = {"niners": "49ers", "bucs": "buccaneers", "washington": "commanders",
                "kansas city": "chiefs", "philadelphia": "eagles", "dallas": "cowboys"}


def team_colors(team: str) -> tuple[str, str, str]:
    t = (team or "").lower().strip()
    t = TEAM_ALIASES.get(t, t)
    for key, cols in NFL_TEAMS.items():
        if key == t or key in t:
            return cols
    return NFL_TEAMS["nfl"]


@dataclass
class CardSpec:
    template: str
    headline_lines: list[str]
    highlight: str = ""            # word/phrase to colour in the headline or quote
    label: str = ""                # REPORT stamp / quote speaker / casefile note
    dek: str = ""
    stat_label: str = ""
    stat_value: str = ""
    footer: str = ""
    footer_sub: str = ""
    team: str = ""
    photos: list[bytes] = field(default_factory=list)   # best first
    cutout: bytes | None = None                          # RGBA PNG of the subject, if made
    focus: tuple[float, float] | None = None             # face centre in photos[0], 0-1 (x, y)
    face_size: float = 0.0                               # face height / photo height
    face_box: tuple[float, float, float, float] | None = None  # face (x, y, w, h) in photos[0], 0-1
    text_at: str = "top"                                 # report template: headline "top" or "bottom"
    art: bytes | None = None                             # Krea-made backdrop art (no people, no text)
    fit: str = "cover"                                   # spotlight: "contain" shows the whole photo (close-ups)
    distress: bool = True                                # worn-print headline lettering
    items: list = field(default_factory=list)            # generic layouts: rows / names / dates (layouts.py)
    theme: dict | None = None                            # generic layouts: the page's colours and logo text
    tspec: dict | None = None                            # a template spec (bore/tspec.py) draws the card instead
    slots: dict = field(default_factory=dict)            # template spec: copy.json's slots
    photo_faces: list = field(default_factory=list)      # template spec: faces per photo, (x, y, w, h) 0-1
    keep: list = field(default_factory=list)             # template spec: watermarks/credits per photo, never cropped or covered


def _data_uri(b: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(b).decode()}"


def _img_mime(b: bytes) -> str:
    return "image/png" if b[:8] == b"\x89PNG\r\n\x1a\n" else "image/webp" if b[8:12] == b"WEBP" else "image/jpeg"


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _hl(text: str, highlight: str, cls: str = "hl") -> str:
    """Escape text and wrap the highlight phrase (case-insensitive) in a span."""
    t = _esc(text)
    if highlight:
        h = _esc(highlight)
        i = t.lower().find(h.lower())
        if i >= 0:
            return t[:i] + f'<span class="{cls}">' + t[i:i + len(h)] + "</span>" + t[i + len(h):]
    return t


# Alpha mask: opaque except blotches where low-frequency noise peaks. DISTRESS_A/B set how much
# wears away. OCR can lose short worn words, so text is verified on a clean twin render.
DISTRESS_A, DISTRESS_B = -14, 11.2
DISTRESS_MASK = ("url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='340' height='340'>"
                 "<filter id='d'><feTurbulence type='fractalNoise' baseFrequency='.09' numOctaves='4' seed='3'/>"
                 f"<feColorMatrix values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  {DISTRESS_A} 0 0 0 {DISTRESS_B}'/></filter>"
                 "<rect width='100%' height='100%' filter='url(%23d)'/></svg>\")")

BASE_CSS = f"""
@font-face {{ font-family: Anton; src: url('file://{FONTS}/Anton-Regular.ttf'); }}
@font-face {{ font-family: Marker; src: url('file://{FONTS}/PermanentMarker-Regular.ttf'); }}
@font-face {{ font-family: Inter; src: url('file://{FONTS}/Inter.ttf'); font-weight: 100 900; }}
@font-face {{ font-family: Oswald; src: url('file://{FONTS}/Oswald.ttf'); font-weight: 200 700; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {W}px; height: {H}px; overflow: hidden; background: #000; }}
.card {{ position: relative; width: {W}px; height: {H}px; overflow: hidden; }}
.noise {{ position: absolute; inset: 0; pointer-events: none; mix-blend-mode: overlay; opacity: .35;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='600' height='600'><filter id='n' x='0' y='0' width='100%' height='100%'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='3' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 .55 0'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>"); }}
.grit {{ position: absolute; inset: 0; pointer-events: none; opacity: .5; mix-blend-mode: multiply;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='900' height='900'><filter id='g' x='0' y='0' width='100%' height='100%'><feTurbulence type='turbulence' baseFrequency='.012 .09' numOctaves='2' seed='7' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -1.6 1.1'/></filter><rect width='100%' height='100%' filter='url(%23g)'/></svg>"); }}
.fit {{ white-space: nowrap; display: inline-block; }}
"""
# worn-print headline: blotchy wear like the pages' distressed display type
DISTRESS_CSS = (f".hl-line, .head span {{ -webkit-mask-image: {DISTRESS_MASK}; mask-image: {DISTRESS_MASK}; "
                "-webkit-mask-size: 340px 340px; mask-size: 340px 340px; }")

FIT_JS = """
async function fit() {
  await document.fonts.ready;
  for (const el of document.querySelectorAll('.fit')) {
    const max = parseFloat(el.dataset.max || 200), min = parseFloat(el.dataset.min || 20);
    const width = parseFloat(el.dataset.w || el.parentElement.clientWidth);
    let size = max; el.style.fontSize = size + 'px';
    while (el.getBoundingClientRect().width > width && size > min) { size -= 2; el.style.fontSize = size + 'px'; }
  }
  // lines in a .stack share the smallest fitted size so the block reads as one headline
  for (const st of document.querySelectorAll('.stack[data-uniform]')) {
    const sizes = [...st.querySelectorAll('.fit')].map(e => parseFloat(e.style.fontSize));
    const m = Math.min(...sizes) * parseFloat(st.dataset.uniform);
    st.querySelectorAll('.fit').forEach(e => { e.style.fontSize = Math.min(parseFloat(e.style.fontSize), m) + 'px'; });
  }
  document.body.dataset.ready = '1';
}
fit();
"""

CROWN_SVG = ("<svg viewBox='0 0 100 70' width='86' height='60'><path d='M6 60 L12 18 L32 40 L50 6 L68 40 L88 18 "
             "L94 60 Z' fill='none' stroke='#ffe01b' stroke-width='7' stroke-linejoin='round'/></svg>")


def _photo_style(pos: str = "50% 25%") -> str:
    return f"object-fit: cover; object-position: {pos};"


# Where each template wants the face centre (x, y) inside its photo box, so the face sits in the
# area that never carries text. Box sizes match the CSS below.
FACE_TARGETS = {
    ("culture-rush", "headline"): ((760, 1350), (0.30, 0.44)),   # left half, clear of the right-side headline
    ("culture-rush", "spotlight"): ((1080, 880), (0.50, 0.40)),  # big photo on top, headline below
    ("huddle", "report"): ((1080, 1350), (0.50, 0.62)),          # between the top headline and the footer
    ("huddle", "quote"): ((760, 1350), (0.55, 0.30)),            # upper right, above the quote boxes
    ("huddle", "topic"): ((1008, 760), (0.50, 0.40)),            # inside the inset
}


def focus_position(img: bytes, box: tuple[int, int], focus: tuple[float, float], target: tuple[float, float]) -> str:
    """object-position that puts the face centre at `target` in a `box` with object-fit: cover."""
    from PIL import Image
    iw, ih = Image.open(io.BytesIO(img)).size
    bw, bh = box
    s = max(bw / iw, bh / ih)
    sw, sh = iw * s, ih * s
    out = []
    for size_box, size_img, f, t in ((bw, sw, focus[0], target[0]), (bh, sh, focus[1], target[1])):
        if abs(size_box - size_img) < 1:
            out.append(50.0)
            continue
        p = (t * size_box - f * size_img) / (size_box - size_img)
        out.append(min(100.0, max(0.0, p * 100)))
    return f"{out[0]:.1f}% {out[1]:.1f}%"


def face_on_card_contain(img: bytes, box_xywh: tuple[int, int, int, int], face: tuple) -> tuple[float, float, float, float]:
    """Face position on the card when the whole photo is shown (contain-fit, centred)."""
    from PIL import Image
    iw, ih = Image.open(io.BytesIO(img)).size
    bx, by, bw, bh = box_xywh
    s = min(bw / iw, bh / ih)
    ox, oy = bx + (bw - iw * s) / 2, by + (bh - ih * s) / 2
    x, y, w, h = face
    return (ox + x * iw * s, oy + y * ih * s, ox + (x + w) * iw * s, oy + (y + h) * ih * s)


def face_on_card(img: bytes, box_xywh: tuple[int, int, int, int], pos: str, face: tuple) -> tuple[float, float, float, float]:
    """Where a face (x, y, w, h as 0-1 of the photo) lands on the card for a cover-fit photo box
    at box_xywh with the given object-position. Returns card pixels (x0, y0, x1, y1)."""
    from PIL import Image
    iw, ih = Image.open(io.BytesIO(img)).size
    bx, by, bw, bh = box_xywh
    s = max(bw / iw, bh / ih)
    sw, sh = iw * s, ih * s
    px, py = (float(v.strip("%")) / 100 for v in pos.split())
    ox, oy = bx + (bw - sw) * px, by + (bh - sh) * py
    x, y, w, h = face
    return (ox + x * sw, oy + y * sh, ox + (x + w) * sw, oy + (y + h) * sh)


# Text zones per template (card pixels) that a face must never touch.
TEXT_ZONES = {
    ("huddle", "report", "top"): [(0, 0, 1080, 600), (0, 1150, 1080, 1350)],
    ("huddle", "report", "bottom"): [(0, 700, 1080, 1350)],
    ("culture-rush", "headline", "top"): [(440, 150, 1080, 1350), (0, 0, 420, 330)],
    ("culture-rush", "spotlight", "top"): [(0, 850, 1080, 1350), (0, 0, 330, 220)],
    ("huddle", "quote", "top"): [(0, 700, 1080, 1350), (0, 0, 620, 420)],
    ("huddle", "topic", "top"): [(0, 0, 1080, 530)],
}
PHOTO_BOXES = {
    ("culture-rush", "headline"): (0, 0, 760, 1350), ("culture-rush", "spotlight"): (0, 0, 1080, 880),
    ("huddle", "report"): (0, 0, 1080, 1350),
    ("huddle", "quote"): (320, 0, 760, 1350), ("huddle", "topic"): (36, 554, 1008, 760),
}


def plan_layout(page: str, spec: "CardSpec") -> list[str]:
    """Choose the layout variant that keeps the face clear of every text zone. Returns problems
    (empty when the face is clear or no face is known)."""
    if spec.tspec is not None:      # a template spec plans its own zones (bore/tspec.py)
        from .tspec import plan
        return plan(spec).problems
    tpl = spec.template or {"culture-rush": "headline", "huddle": "report"}.get(page, "")
    if not (spec.face_box and spec.photos and (page, tpl) in PHOTO_BOXES):
        return []
    fx, fy, fw, fh = spec.face_box
    head = (fx - 0.18 * fw, fy - 0.45 * fh, fw * 1.36, fh * 1.53)   # same zone as guard.head_zone
    variants = ["top", "bottom"] if (page, tpl) == ("huddle", "report") else ["top"]
    for v in variants:
        spec.text_at = v
        pos = _pos(page, tpl, spec, "50% 35%")
        zone = face_on_card(spec.photos[0], PHOTO_BOXES[(page, tpl)], pos, head)
        if not any(_hits(zone, z) for z in TEXT_ZONES.get((page, tpl, v), [])):
            return []
    if (page, tpl) == ("culture-rush", "spotlight"):   # a tight close-up: show the whole photo
        spec.text_at = "top"
        zone = face_on_card_contain(spec.photos[0], PHOTO_BOXES[(page, tpl)], head)
        if not any(_hits(zone, z) for z in TEXT_ZONES[(page, tpl, "top")]):
            spec.fit = "contain"
            return []
    spec.text_at = variants[0]
    return [f"the subject's face cannot be kept clear of the text in the '{tpl}' layout with this photo: "
            "choose a photo where the face is smaller or positioned differently, or another template"]


def _hits(a, b) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _pos(page: str, tpl: str, spec: "CardSpec", default: str) -> str:
    t = FACE_TARGETS.get((page, tpl))
    if not (t and spec.focus and spec.photos):
        return default
    target = t[1]
    if (page, tpl) == ("huddle", "report") and spec.text_at == "bottom":
        target = (0.5, 0.26)
    return focus_position(spec.photos[0], t[0], spec.focus, target)


# --------------------------------------------------------------------------- culture rush
def culture_rush_headline(spec: CardSpec) -> str:
    photo = _data_uri(spec.photos[0], _img_mime(spec.photos[0])) if spec.photos else ""
    lines = [l.upper() for l in spec.headline_lines if l.strip()][:4] or ["CULTURE RUSH"]
    # alternate white / yellow, with the highlight word forced yellow
    parts = []
    for i, l in enumerate(lines):
        color = "#ffe01b" if (i >= len(lines) / 2) else "#ffffff"
        if spec.highlight and spec.highlight.upper() in l:
            color = "#ffe01b"
        parts.append(f'<span class="fit hl-line" data-max="210" data-min="60" data-w="560" style="color:{color}">{_esc(l)}</span>')
    dek = _esc(spec.dek)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE_CSS}
.bg {{ position:absolute; inset:0; background: radial-gradient(ellipse at 70% 40%, #1c1c1c, #050505 70%); }}
.photo {{ position:absolute; left:0; top:0; width:760px; height:{H}px; {_photo_style(_pos('culture-rush', 'headline', spec, '50% 20%'))}
  -webkit-mask-image: linear-gradient(to right, #000 62%, transparent 98%); filter: contrast(1.03); }}
.shade {{ position:absolute; inset:0; background: linear-gradient(to right, transparent 35%, rgba(0,0,0,.78) 62%, #000 100%),
  linear-gradient(to top, rgba(0,0,0,.85), transparent 38%); }}
.torn {{ position:absolute; right:-10px; top:0; width:26px; height:{H}px; background:#ffe01b; opacity:.9;
  clip-path: polygon(40% 0,100% 0,100% 100%,30% 100%,60% 93%,20% 86%,55% 78%,25% 70%,65% 61%,30% 52%,60% 44%,22% 35%,58% 27%,28% 18%,62% 9%); }}
.logo {{ position:absolute; left:44px; top:40px; transform: rotate(-5deg); text-align:center; filter: drop-shadow(0 4px 10px rgba(0,0,0,.8)); }}
.logo .w1 {{ font-family: Marker; font-size: 64px; color:#fff; line-height:.9; display:block; }}
.logo .w2 {{ font-family: Marker; font-size: 64px; color:#ffe01b; line-height:.9; display:block; margin-top:-4px; }}
.headline {{ position:absolute; right:56px; top:250px; width:560px; text-align:right; }}
.stack {{ display:flex; flex-direction:column; align-items:flex-end; }}
.hl-line {{ font-family: Anton; line-height:.98; letter-spacing:1px; text-shadow: 0 6px 18px rgba(0,0,0,.9); }}
.dekband {{ position:absolute; left:0; right:0; bottom:70px; height:180px; display:flex; align-items:center; justify-content:flex-end; padding: 0 60px; }}
.dekband::before {{ content:''; position:absolute; right:30px; width:640px; height:120px; background:#ffe01b; transform: rotate(-2deg);
  clip-path: polygon(2% 12%,98% 0,100% 88%,4% 100%,0 55%); opacity:.95; }}
.dek {{ position:relative; font-family: Inter; font-weight:800; font-size:40px; line-height:1.12; color:#0a0a0a; max-width:560px; text-align:right; }}
</style></head><body><div class="card">
<div class="bg"></div><div class="grit"></div><div class="noise"></div>{f'<img class="photo" src="{photo}">' if photo else ''}<div class="shade"></div>
<div class="torn"></div>
<div class="logo">{CROWN_SVG}<span class="w1">CULTURE</span><span class="w2">RUSH</span></div>
<div class="headline"><div class="stack" data-uniform="1.0">{''.join(parts)}</div></div>
{f'<div class="dekband"><div class="dek">{dek}</div></div>' if dek else ''}
</div><script>{FIT_JS}</script></body></html>"""


def culture_rush_spotlight(spec: CardSpec) -> str:
    """Photo-first celebrity card: the subject large across the top, the headline stacked below.
    Fits any face position, so it is the fallback when the photo-left layout would cover a face."""
    photo = _data_uri(spec.photos[0], _img_mime(spec.photos[0])) if spec.photos else ""
    lines = [l.upper() for l in spec.headline_lines if l.strip()][:3] or ["CULTURE RUSH"]
    parts = []
    for i, l in enumerate(lines):
        color = "#ffe01b" if (i >= len(lines) / 2 or (spec.highlight and spec.highlight.upper() in l)) else "#ffffff"
        parts.append(f'<div><span class="fit hl-line" data-max="{150 if len(lines) > 2 else 175}" data-min="60" '
                     f'data-w="990" style="color:{color}">{_esc(l)}</span></div>')
    dek = _esc(spec.dek)
    tag = _esc(spec.label.upper()) if spec.label else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE_CSS}
.bg {{ position:absolute; inset:0; background: radial-gradient(ellipse at 50% 80%, #1c1c1c, #050505 70%); }}
.photo {{ position:absolute; left:0; top:0; width:{W}px; height:880px; {_photo_style(_pos('culture-rush', 'spotlight', spec, '50% 25%'))}
  -webkit-mask-image: linear-gradient(to bottom, #000 78%, transparent 100%); filter: contrast(1.03); }}
.photo.blur {{ object-position: 50% 50%; filter: blur(36px) brightness(.4); transform: scale(1.15); }}
.photo.whole {{ object-fit: contain; object-position: 50% 50%; }}
.shade {{ position:absolute; inset:0; background: linear-gradient(to bottom, rgba(0,0,0,.35) 0%, transparent 16%, transparent 52%, rgba(0,0,0,.55) 64%, #000 74%); }}
.tear {{ position:absolute; left:-10px; right:-10px; top:806px; height:70px; background:#050505; opacity:.92;
  clip-path: polygon(0 55%,6% 30%,12% 60%,19% 25%,27% 55%,34% 20%,41% 50%,49% 28%,57% 58%,64% 22%,72% 52%,80% 26%,87% 56%,94% 30%,100% 50%,100% 100%,0 100%); }}
.stripe {{ position:absolute; right:-6px; top:0; width:22px; height:900px; background:#ffe01b; opacity:.9;
  clip-path: polygon(40% 0,100% 0,100% 100%,30% 100%,60% 90%,20% 78%,55% 64%,25% 50%,65% 36%,30% 22%,60% 10%); }}
.logo {{ position:absolute; left:40px; top:34px; transform: rotate(-5deg); text-align:center; filter: drop-shadow(0 4px 10px rgba(0,0,0,.8)); }}
.logo .w1, .logo .w2 {{ font-family: Marker; font-size: 50px; line-height:.9; display:block; }}
.logo .w1 {{ color:#fff; }} .logo .w2 {{ color:#ffe01b; margin-top:-4px; }}
.text {{ position:absolute; left:45px; right:45px; top:{860 if tag else 880}px; text-align:center; }}
.tag {{ display:inline-block; background:#ffe01b; color:#0a0a0a; font-family:Anton; font-size:40px; padding:0 18px 3px; transform: rotate(-2deg); margin-bottom:6px; }}
.hl-line {{ font-family: Anton; line-height:1.0; letter-spacing:1px; text-shadow: 0 6px 18px rgba(0,0,0,.9); }}
.dek {{ position:absolute; left:0; right:0; bottom:34px; display:flex; justify-content:center; }}
.dek span {{ position:relative; isolation:isolate; font-family: Inter; font-weight:800; font-size:34px; line-height:1.15; color:#0a0a0a; padding:10px 34px; max-width:900px; text-align:center; }}
.dek span::before {{ content:''; position:absolute; inset:0; z-index:-1; background:#ffe01b; transform: rotate(-1.5deg);
  clip-path: polygon(1% 10%,99% 0,100% 90%,2% 100%); }}
</style></head><body><div class="card">
<div class="bg"></div><div class="grit"></div><div class="noise"></div>{(f'<img class="photo blur" src="{photo}"><img class="photo whole" src="{photo}">' if spec.fit == "contain" else f'<img class="photo" src="{photo}">') if photo else ''}<div class="shade"></div><div class="tear"></div>
<div class="stripe"></div>
<div class="logo">{CROWN_SVG.replace("width='86' height='60'", "width='66' height='46'")}<span class="w1">CULTURE</span><span class="w2">RUSH</span></div>
<div class="text">{f'<div class="tag">{tag}</div>' if tag else ''}<div class="stack" data-uniform="1.0">{''.join(parts)}</div></div>
{f'<div class="dek"><span>{dek}</span></div>' if dek else ''}
</div><script>{FIT_JS}</script></body></html>"""


def culture_rush_casefile(spec: CardSpec) -> str:
    photos = [_data_uri(p, _img_mime(p)) for p in spec.photos[:3]]
    rots = [-6, 2, 7]
    pol = "".join(
        f'<div class="pol" style="transform:rotate({rots[i]}deg)"><img src="{u}"></div>' for i, u in enumerate(photos))
    lines = [l.upper() for l in spec.headline_lines if l.strip()][:2] or ["UNSOLVED"]
    head = "".join(
        f'<div><span class="fit" data-max="170" data-min="60" data-w="980" style="color:{"#ffe01b" if i else "#fff"}">{_esc(l)}</span></div>'
        for i, l in enumerate(lines))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE_CSS}
.bg {{ position:absolute; inset:0; background: radial-gradient(ellipse at 50% 35%, #3a3128, #0d0b09 75%); }}
.row {{ position:absolute; top:{200 if len(photos) < 3 else 230}px; left:30px; right:30px; display:flex; justify-content:center; gap:18px; }}
.pol {{ background:#f3eee2; padding:14px 14px 48px; box-shadow: 0 18px 40px rgba(0,0,0,.7); width:{[600, 470, 318][max(len(photos), 1) - 1]}px; }}
.pol img {{ width:100%; height:{[650, 600, 440][max(len(photos), 1) - 1]}px; {_photo_style('50% 25%')} display:block; }}
.note {{ position:absolute; top:48px; left:50%; transform: translateX(-50%) rotate(-2deg); background:#e9e1cf; padding:18px 30px;
  font-family: 'Courier New', monospace; font-weight:700; font-size:34px; color:#1b1b1b; text-align:center; box-shadow:0 10px 24px rgba(0,0,0,.6); }}
.note b {{ color:#b3261e; }}
.logo {{ position:absolute; left:40px; top:40px; transform: rotate(-5deg); text-align:center; z-index:3; }}
.logo span {{ font-family: Marker; font-size: 40px; line-height:.9; display:block; }}
.head {{ position:absolute; left:50px; right:50px; bottom:90px; text-align:center; }}
.head span {{ font-family: Anton; line-height:1; text-shadow: 0 8px 22px rgba(0,0,0,.9); }}
</style></head><body><div class="card"><div class="bg"></div><div class="grit"></div><div class="noise"></div>
<div class="logo">{CROWN_SVG.replace("width='86' height='60'", "width='56' height='40'")}<span style="color:#fff">CULTURE</span><span style="color:#ffe01b">RUSH</span></div>
{f'<div class="note">{_hl(spec.label, spec.highlight, "b")}</div>' if spec.label else ''}
<div class="row">{pol}</div>
<div class="head">{head}</div></div><script>{FIT_JS}</script></body></html>"""


# --------------------------------------------------------------------------------- huddle
def _team_mark(team: str, color: str) -> str:
    for ext in ("png", "webp", "svg"):
        p = TEAMS_DIR / f"{(team or '').lower()}.{ext}"
        if p.exists():
            mime = {"png": "image/png", "webp": "image/webp", "svg": "image/svg+xml"}[ext]
            return f'<img class="teamlogo" src="{_data_uri(p.read_bytes(), mime)}">'
    name = _esc((team or "NFL").upper())
    return f'<div class="wordmark">{name}</div>'


def huddle_quote(spec: CardSpec) -> str:
    c1, c2, c3 = team_colors(spec.team)
    subject = spec.cutout or (spec.photos[0] if spec.photos else None)
    is_cut = spec.cutout is not None
    img = _data_uri(subject, _img_mime(subject)) if subject else ""
    quote = [l for l in spec.headline_lines if l.strip()][:3] or ["NO COMMENT"]
    q_html = ""
    for i, l in enumerate(quote):
        text = l.upper()
        if i == 0:
            text = "“" + text
        if i == len(quote) - 1:
            text = text + "”"
        q_html += f'<div class="qbox"><span class="fit" data-max="92" data-min="40" data-w="820">{_hl(text, spec.highlight.upper())}</span></div>'
    photo_css = (f"right:-40px; bottom:0; height:1180px; width:auto; filter: drop-shadow(-12px 0 30px rgba(0,0,0,.55));"
                 if is_cut else
                 f"right:0; top:0; width:760px; height:{H}px; {_photo_style(_pos('huddle', 'quote', spec, '50% 20%'))} -webkit-mask-image: linear-gradient(to left, #000 60%, transparent 100%);")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE_CSS}
.bg {{ position:absolute; inset:0; background: radial-gradient(ellipse at 60% 30%, {c1}, color-mix(in srgb, {c1} 55%, #000) 80%); }}
.subject {{ position:absolute; {photo_css} }}
.wordmark {{ position:absolute; left:56px; top:70px; font-family:Anton; font-size:150px; line-height:.9; color:#fff;
  -webkit-text-stroke: 6px {c2 if c2.lower() != c1.lower() else '#000'}; paint-order: stroke fill; transform: rotate(-3deg);
  text-shadow: 0 10px 24px rgba(0,0,0,.45); max-width:560px; }}
.teamlogo {{ position:absolute; left:56px; top:70px; width:360px; filter: drop-shadow(0 12px 22px rgba(0,0,0,.5)); }}
.bottom {{ position:absolute; left:48px; right:48px; bottom:70px; }}
.speaker {{ display:inline-block; background:#0b0b0b; color:#fff; font-family:Oswald; font-weight:600; font-size:50px; padding:6px 18px; margin-bottom:10px; }}
.qbox {{ display:table; background:#fff; padding:2px 18px 6px; margin-top:8px; box-shadow: 0 6px 16px rgba(0,0,0,.35); }}
.qbox span {{ font-family:Anton; color:#0b0b0b; line-height:1.06; }}
.hl {{ color: {c1 if c1.lower() not in ('#000000', '#101820') else '#d50a0a'}; }}
</style></head><body><div class="card"><div class="bg"></div><div class="grit"></div><div class="noise"></div>
{_team_mark(spec.team, c1)}{f'<img class="subject" src="{img}">' if img else ''}
<div class="bottom">{f'<div class="speaker">{_esc(spec.label)}</div>' if spec.label else ''}{q_html}</div>
</div><script>{FIT_JS}</script></body></html>"""


def huddle_report(spec: CardSpec) -> str:
    c1, c2, c3 = team_colors(spec.team)
    img = _data_uri(spec.photos[0], _img_mime(spec.photos[0])) if spec.photos else ""
    lines = [l.upper() for l in spec.headline_lines if l.strip()][:2] or ["BREAKING"]
    head = "".join(f'<div><span class="fit" data-max="190" data-min="70" data-w="980">{_hl(l, spec.highlight.upper(), "hly")}</span></div>' for l in lines)
    stat = ""
    if spec.stat_value:
        stat = (f'<div class="stat" style="background:{c1}"><div class="sl">{_esc(spec.stat_label.upper())}</div>'
                f'<div class="sv fit" data-max="96" data-min="40" data-w="620">{_esc(spec.stat_value.upper())}</div></div>')
    footer = ""
    if spec.footer:
        footer = (f'<div class="footer"><span class="fit" data-max="78" data-min="36" data-w="1000">{_esc(spec.footer.upper())}</span>'
                  + (f'<div class="fsub">{_esc(spec.footer_sub.upper())}</div>' if spec.footer_sub else "") + "</div>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE_CSS}
.photo {{ position:absolute; inset:0; width:{W}px; height:{H}px; {_photo_style(_pos('huddle', 'report', spec, '50% 35%'))} filter: contrast(1.04) saturate(1.05); }}
.shade {{ position:absolute; inset:0; background: {"linear-gradient(to bottom, rgba(0,0,0,.9) 0%, rgba(0,0,0,.7) 26%, rgba(0,0,0,.15) 46%, rgba(0,0,0,0) 56%, rgba(0,0,0,.35) 74%, rgba(0,0,0,.92) 100%)" if spec.text_at != "bottom" else "linear-gradient(to bottom, rgba(0,0,0,.35) 0%, rgba(0,0,0,0) 22%, rgba(0,0,0,0) 44%, rgba(0,0,0,.72) 60%, rgba(0,0,0,.95) 100%)"}; }}
.top {{ position:absolute; left:40px; right:40px; {'top:34px' if spec.text_at != 'bottom' else 'bottom:' + str(230 if spec.footer else 60) + 'px'}; text-align:center; }}
.stamp {{ display:inline-block; background:#d11d1d; color:#fff; font-family:Anton; font-size:64px; padding:0 22px 4px; transform: rotate(-3deg);
  border:4px solid #fff; box-shadow: 0 8px 20px rgba(0,0,0,.6); letter-spacing:2px; }}
.head {{ margin-top:6px; }}
.head span {{ font-family:Anton; color:#fff; line-height:1.0; text-shadow: 0 6px 0 rgba(0,0,0,.35), 0 12px 30px rgba(0,0,0,.8); }}
.head .hly {{ color:#ffd21f; }}
.rule {{ height:6px; width:70%; margin:10px auto 0; background: linear-gradient(90deg, transparent, #ffd21f, transparent); }}
.stat {{ position:absolute; left:34px; bottom:250px; padding:14px 30px 16px; transform: skewX(-8deg); box-shadow:0 10px 24px rgba(0,0,0,.6); max-width:700px; }}
.stat .sl {{ font-family:Oswald; font-weight:700; color:#fff; font-size:34px; }}
.stat .sv {{ font-family:Anton; color:#ffd21f; line-height:1; }}
.footer {{ position:absolute; left:0; right:0; bottom:0; background: rgba(8,8,8,.92); padding:20px 40px 22px; text-align:center; border-top:5px solid #ffd21f; }}
.footer span {{ font-family:Anton; color:#fff; line-height:1.05; }}
.fsub {{ font-family:Oswald; font-weight:600; color:#ffd21f; font-size:34px; letter-spacing:3px; margin-top:4px; }}
</style></head><body><div class="card">{f'<img class="photo" src="{img}">' if img else ''}<div class="shade"></div>
<div class="top">{f'<div class="stamp">{_esc((spec.label or "REPORT").upper())}</div>'}<div class="head">{head}</div><div class="rule"></div></div>
{stat}{footer}</div><script>{FIT_JS}</script></body></html>"""


def huddle_topic(spec: CardSpec) -> str:
    img = _data_uri(spec.photos[0], _img_mime(spec.photos[0])) if spec.photos else ""
    lines = [l.upper() for l in spec.headline_lines if l.strip()][:3] or ["NFL"]
    first, rest = lines[0], lines[1:]
    boxes = f'<div class="b1"><span class="fit" data-max="110" data-min="44" data-w="640">{_esc(first)}</span></div>'
    boxes += "".join(f'<div class="b2"><span class="fit" data-max="96" data-min="40" data-w="700">{_hl(l, spec.highlight.upper())}</span></div>' for l in rest)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE_CSS}
.bg {{ position:absolute; inset:0; background: repeating-linear-gradient(90deg, #244a22 0 90px, #1f421d 90px 180px); }}
.vig {{ position:absolute; inset:0; background: radial-gradient(ellipse at 50% 40%, transparent 40%, rgba(0,0,0,.55)); }}
.boxes {{ position:absolute; left:52px; top:60px; }}
.b1, .b2 {{ display:table; padding:4px 22px 8px; margin-bottom:10px; box-shadow: 0 8px 18px rgba(0,0,0,.45); }}
.b1 {{ background:#ffd21f; transform: rotate(-1.5deg); }}
.b2 {{ background:#fff; }}
.b1 span, .b2 span {{ font-family:Anton; color:#15301a; line-height:1.02; }}
.hl {{ color:#d11d1d; }}
.inset {{ position:absolute; left:36px; right:36px; bottom:36px; height:760px; border:8px solid #fff; border-radius:28px; overflow:hidden;
  box-shadow: 0 18px 40px rgba(0,0,0,.6); }}
.inset img {{ width:100%; height:100%; {_photo_style(_pos('huddle', 'topic', spec, '50% 35%'))} }}
</style></head><body><div class="card"><div class="bg"></div><div class="noise"></div><div class="vig"></div>
<div class="boxes">{boxes}</div>{f'<div class="inset"><img src="{img}"></div>' if img else ''}
</div><script>{FIT_JS}</script></body></html>"""


TEMPLATES = {
    ("culture-rush", "headline"): culture_rush_headline,
    ("culture-rush", "casefile"): culture_rush_casefile,
    ("culture-rush", "spotlight"): culture_rush_spotlight,
    ("huddle", "quote"): huddle_quote,
    ("huddle", "report"): huddle_report,
    ("huddle", "topic"): huddle_topic,
}


def _with_art(html_doc: str, spec: CardSpec) -> str:
    """Lay the Krea artwork into the template: it replaces the backdrop (.bg). On the full-bleed
    photo template it is screened into the text bands only, never across the middle of the photo
    where the face sits (the layout planner keeps the face out of those bands)."""
    uri = _data_uri(spec.art, _img_mime(spec.art))
    if '<div class="bg"></div>' in html_doc:
        css = (f'.bg {{ background: linear-gradient(rgba(0,0,0,.05), rgba(0,0,0,.2)), url("{uri}") center/cover '
               f'!important; }}'
               # the art carries the grit now: lighter template shading so it shows through
               f'.shade {{ opacity:.45; }} .grit {{ opacity:.35; }}')
        return html_doc.replace("</style>", css + "</style>", 1)
    mask = ("linear-gradient(to bottom, #000 0%, #000 20%, transparent 38%, transparent 74%, #000 90%)"
            if spec.text_at != "bottom" else
            "linear-gradient(to bottom, #000 0%, transparent 14%, transparent 50%, #000 66%)")
    css = (f'.artband {{ position:absolute; inset:0; background: url("{uri}") center/cover; mix-blend-mode: screen; '
           f'opacity:.7; -webkit-mask-image: {mask}; mask-image: {mask}; pointer-events:none; }}')
    html_doc = html_doc.replace("</style>", css + "</style>", 1)
    return html_doc.replace('<div class="shade"></div>', '<div class="shade"></div><div class="artband"></div>', 1)


def build_html(page: str, spec: CardSpec) -> str:
    if spec.tspec is not None:      # any page: a template spec, drawn from its data
        from .tspec import build_html as spec_build
        return spec_build(spec)
    fn = TEMPLATES.get((page, spec.template))
    if fn is None and (spec.template in GENERIC or not any(k[0] == page for k in TEMPLATES)):
        out = generic_build(spec, spec.theme)    # any page: the common layouts in the page's colours
        return out.replace("</style>", DISTRESS_CSS + "</style>", 1) if spec.distress else out
    if fn is None:
        default = {"culture-rush": "headline", "huddle": "report"}.get(page, "headline")
        fn = TEMPLATES.get((page, default), culture_rush_headline)
    out = fn(spec)
    if spec.distress:
        out = out.replace("</style>", DISTRESS_CSS + "</style>", 1)
    return _with_art(out, spec) if spec.art else out


from .layouts import GENERIC, build as generic_build  # noqa: E402


def _chrome_path() -> str | None:
    for p in (os.environ.get("CHROME_PATH"), "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
        if p and Path(p).exists():
            return p
    return None  # Playwright's bundled Chromium (PLAYWRIGHT_BROWSERS_PATH)


class FaceBlocked(ValueError):
    pass


class TextOverflow(ValueError):
    """A template spec's text does not fit its zone even at the zone's smallest size."""


# each line of a template spec card's text as drawn: its text and card pixel box (x0, y0, x1, y1)
DRAWN_JS = """[...document.querySelectorAll('.tz .ln, .lz .v, .lz .l')].map(e => { const r = e.getBoundingClientRect();
  return {text: e.textContent, box: [r.left, r.top, r.right, r.bottom].map(Math.round)}; })"""


def render(page: str, spec: CardSpec, quality: int = 92, drawn: list | None = None) -> bytes:
    """Render the card to JPEG bytes. Refuses when the face cannot be kept clear of the text, and
    (template specs) when text does not fit its zone. drawn: a list given here gets each line of a
    template spec's text as drawn (DRAWN_JS), where verify reads a lone digit (guard.verify)."""
    from playwright.sync_api import sync_playwright
    from PIL import Image

    problems = plan_layout(page, spec)
    if problems:
        raise FaceBlocked(problems[0])

    doc = build_html(page, spec)
    w, h = (spec.tspec["canvas"]["w"], spec.tspec["canvas"]["h"]) if spec.tspec is not None else (W, H)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "card.html"
        path.write_text(doc, encoding="utf-8")
        with sync_playwright() as pw:
            kw = {"executable_path": _chrome_path()} if _chrome_path() else {}
            browser = pw.chromium.launch(**kw)
            pg = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
            pg.goto(path.as_uri())
            pg.wait_for_function("document.body.dataset.ready === '1'", timeout=20000)
            broken = pg.evaluate("[...document.images].filter(i => !(i.complete && i.naturalWidth > 0)).length")
            if broken:   # an empty photo frame must never reach a card
                browser.close()
                raise ValueError(f"{broken} photo(s) on the card failed to load: the file is not a usable image")
            over = pg.evaluate("document.body.dataset.overflow || ''")
            if over:     # clipped or spilling text must never reach a card: the copy is too long for the zone
                browser.close()
                # the zones on a line of their own: a report keeps only the first line
                raise TextOverflow("text does not fit its zone even at the smallest size (shorten it in copy.json):\n"
                                   f"  {over.replace(',', ', ')}")
            if drawn is not None and spec.tspec is not None:
                drawn[:] = pg.evaluate(DRAWN_JS)
            png = pg.screenshot(type="png", clip={"x": 0, "y": 0, "width": w, "height": h})
            browser.close()
    im = Image.open(io.BytesIO(png)).convert("RGB")
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()
