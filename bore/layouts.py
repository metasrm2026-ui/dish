"""Generic card layouts for any page: the common shapes a page's templates document describes,
coloured from the page's theme (admin panel: accent, background, text, logo text, fonts).

  headline      big photo on top, headline band below
  quote         photo on top, the quote in large type, the speaker under it
  stat_card     photo on top, one huge number with its label, headline under it
  comparison    two photos side by side, names under them, rows of label / left / right
  then_vs_now   two photos side by side tagged THEN / NOW (with their years), headline below
  ranked_list   headline and photo, then up to 5 numbered rows
  timeline      headline and photo, then up to 5 dated steps

No text is ever drawn over a photo, so every face stays clear whatever the crop. The photo is
positioned so the face sits in the upper middle of its box. Card text is exactly copy.json.
"""
from __future__ import annotations

import io
import re

GENERIC = ("headline", "quote", "stat_card", "comparison", "then_vs_now", "ranked_list", "timeline")
W, H = 1080, 1350
DEFAULT_THEME = {"accent": "#ffd21f", "bg": "#111111", "ink": "#ffffff", "logo_text": "", "font": "Anton",
                 "logo_font": "Oswald"}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
# the fonts the cards can draw (assets/fonts, cards.BASE_CSS): a theme names one of these
FONTS = ("Anton", "Oswald", "Marker", "Inter")

# photo boxes (x, y, w, h) per layout; [0] is the main photo
BOXES = {
    "headline": [(0, 0, 1080, 860)],
    "quote": [(0, 0, 1080, 700)],
    "stat_card": [(0, 0, 1080, 720)],
    "comparison": [(0, 110, 537, 640), (543, 110, 537, 640)],
    "then_vs_now": [(0, 0, 537, 860), (543, 0, 537, 860)],
    "ranked_list": [(0, 0, 1080, 520)],
    "timeline": [(0, 0, 1080, 500)],
}


def theme_of(theme: dict | None) -> dict:
    t = dict(DEFAULT_THEME)
    for k, v in (theme or {}).items():
        if k in ("accent", "bg", "ink") and isinstance(v, str) and HEX.match(v):
            t[k] = v
        elif k == "logo_text" and isinstance(v, str):
            t[k] = v.strip()[:40]
        elif k in ("font", "logo_font") and isinstance(v, str):
            name = next((f for f in FONTS if f.lower() == v.strip().lower()), None)
            if name:          # an unknown font keeps the default (a page's card never falls back to a system font)
                t[k] = name
    return t


def luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b


def contrast_ink(hex_color: str) -> str:
    """Near-black on a light colour, white on a dark one."""
    return "#111111" if luminance(hex_color) > 150 else "#ffffff"


MIN_FACE = 190          # face height on the card we aim for (verify needs 100px); zoom in up to this


def _placement(img: bytes, box, focus, face_box) -> str:
    """Cover the box, centre the face in its upper middle and zoom in on a small face (a wide
    action shot), without upscaling the photo more than 1.6x."""
    from PIL import Image
    iw, ih = Image.open(io.BytesIO(img)).size
    bw, bh = box[2], box[3]
    s = max(bw / iw, bh / ih)
    z = 1.0
    if face_box and face_box[3] * ih * s < MIN_FACE:
        z = max(1.0, min(MIN_FACE / (face_box[3] * ih * s), 1.6 / s, 3.0))
    sw, sh = iw * s * z, ih * s * z
    fx, fy = focus or (0.5, 0.3)
    left = min(0.0, max(bw - sw, bw * 0.5 - fx * sw))
    top = min(0.0, max(bh - sh, bh * 0.42 - fy * sh))
    return f"position:absolute;width:{sw:.0f}px;height:{sh:.0f}px;left:{left:.0f}px;top:{top:.0f}px;object-fit:fill"


def items_text(copy: dict) -> list[str]:
    """Every string the generic layouts draw from `items` (for lint/verify)."""
    out = []
    for it in copy.get("items") or []:
        if isinstance(it, dict):
            out += [str(it[k]) for k in ("label", "value", "left", "right", "date", "text", "year") if it.get(k)]
    return out


def build(spec, theme: dict | None = None) -> str:
    from .cards import BASE_CSS, FIT_JS, _data_uri, _esc, _hl, _img_mime
    t = theme_of(theme)
    acc, bg, ink = t["accent"], t["bg"], t["ink"]
    acc_ink, font = contrast_ink(acc), t["font"]
    tpl = spec.template if spec.template in GENERIC else "headline"
    boxes = BOXES[tpl]
    photos = list(spec.photos or [])
    items = [i for i in (getattr(spec, "items", None) or []) if isinstance(i, dict)][:5]

    def photo(i: int) -> str:
        if i >= len(boxes):
            return ""
        x, y, w, h = boxes[i]
        if i >= len(photos):
            return f'<div class="ph empty" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px"></div>'
        img = photos[i]
        place = _placement(img, boxes[i], spec.focus if i == 0 else None, spec.face_box if i == 0 else None)
        return (f'<div class="ph" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px">'
                f'<img src="{_data_uri(img, _img_mime(img))}" style="{place}"></div>')

    def head(lines, max_px=118, min_px=48, width=980, upper=True) -> str:
        ls = [l for l in lines if l.strip()][:3]
        return "".join(f'<div class="hl-line"><span class="fit" data-max="{max_px}" data-min="{min_px}" data-w="{width}">'
                       f'{_hl(l.upper() if upper else l, (spec.highlight or "").upper() if upper else spec.highlight or "", "hi")}'
                       f'</span></div>' for l in ls)

    label = f'<div class="label">{_esc(spec.label.upper())}</div>' if spec.label else ""
    dek = f'<div class="dek">{_esc(spec.dek)}</div>' if spec.dek else ""
    logo = f'<div class="logo">{_esc(t["logo_text"].upper())}</div>' if t["logo_text"] else ""
    body = ""

    if tpl == "headline":
        body = (photo(0) + f'<div class="band" style="top:860px">{label}<div class="stack" data-uniform="1">'
                f'{head(spec.headline_lines)}</div>{dek}</div>')
    elif tpl == "quote":
        q = [l for l in spec.headline_lines if l.strip()][:4]
        qh = "".join(f'<div class="hl-line"><span class="fit q" data-max="104" data-min="40" data-w="960">'
                     f'{_hl(("“" if i == 0 else "") + l + ("”" if i == len(q) - 1 else ""), spec.highlight or "", "hi")}'
                     f'</span></div>' for i, l in enumerate(q))
        body = (photo(0) + f'<div class="band" style="top:700px"><div class="stack" data-uniform="1">{qh}</div>'
                + (f'<div class="speaker">— {_esc(spec.label)}</div>' if spec.label else "") + dek + "</div>")
    elif tpl == "stat_card":
        body = (photo(0) + f'<div class="band" style="top:720px;text-align:center">'
                f'<div class="stat"><span class="fit" data-max="230" data-min="80" data-w="980">{_esc(spec.stat_value.upper())}</span></div>'
                f'<div class="statl">{_esc(spec.stat_label.upper())}</div>'
                f'<div class="stack" data-uniform="1" style="margin-top:14px">{head(spec.headline_lines, 76, 36)}</div>{dek}</div>')
    elif tpl == "comparison":
        names = (items[0] if items else {}), (items[1] if len(items) > 1 else {})
        top = f'<div class="band" style="top:0;height:110px;padding-top:18px;text-align:center"><div class="stack">{head(spec.headline_lines[:1], 76, 34)}</div></div>'
        nm = "".join(f'<div class="nm" style="left:{x}px;width:{w}px"><span class="fit" data-max="54" data-min="26" data-w="{w - 30}">'
                     f'{_esc(str(n.get("label", "")).upper())}</span></div>' for n, (x, _, w, _) in zip(names, BOXES["comparison"]))
        rows = "".join(f'<div class="crow"><div class="cv">{_esc(r.get("left", ""))}</div><div class="cl">{_esc(str(r.get("label", "")).upper())}</div>'
                       f'<div class="cv">{_esc(r.get("right", ""))}</div></div>' for r in items[2:5])
        body = (top + photo(0) + photo(1) + '<div class="vs">VS</div>' + nm
                + f'<div class="band" style="top:820px">{rows}{dek}</div>')
    elif tpl == "then_vs_now":
        tags = [(items[i] if len(items) > i else {}) for i in (0, 1)]
        tg = "".join(f'<div class="tag" style="left:{x + 20}px;top:{h - 90}px">{_esc(str(tag.get("label") or ("THEN", "NOW")[k]).upper())}'
                     + (f' <b>{_esc(tag.get("year") or "")}</b>' if tag.get("year") else "") + "</div>"
                     for k, (tag, (x, _, _, h)) in enumerate(zip(tags, BOXES["then_vs_now"])))
        body = (photo(0) + photo(1) + tg + f'<div class="band" style="top:860px">{label}<div class="stack" data-uniform="1">'
                f'{head(spec.headline_lines)}</div>{dek}</div>')
    elif tpl in ("ranked_list", "timeline"):
        top_h = BOXES[tpl][0][3]
        if tpl == "ranked_list":
            rows = "".join(f'<div class="row"><div class="rn">{n}</div><div class="rt"><span class="fit" data-max="46" data-min="24" data-w="760">'
                           f'{_esc(str(it.get("label", "")).upper())}</span>'
                           + (f'<div class="rv">{_esc(it.get("value", ""))}</div>' if it.get("value") else "") + "</div></div>"
                           for n, it in enumerate(items, 1))
        else:
            rows = "".join(f'<div class="row tl"><div class="td">{_esc(str(it.get("date", "")).upper())}</div><div class="dot"></div>'
                           f'<div class="rt"><span class="fit" data-max="54" data-min="24" data-w="660">{_esc(it.get("text", ""))}</span></div></div>'
                           for it in items)
        body = (photo(0) + f'<div class="band" style="top:{top_h}px">{label}<div class="stack" data-uniform="1">'
                f'{head(spec.headline_lines, 84, 36)}</div><div class="rows">{rows}</div></div>')

    css = f"""{BASE_CSS}
.card {{ background:{bg}; }}
.ph {{ position:absolute; overflow:hidden; background:#000; }}
.ph img {{ width:100%; height:100%; object-fit:cover; display:block; }}
.ph.empty {{ background: repeating-linear-gradient(45deg, {bg}, {bg} 20px, #222 20px, #222 40px); }}
.band {{ position:absolute; left:0; right:0; bottom:0; padding:26px 50px 44px; display:flex; flex-direction:column; justify-content:center; background:{bg}; border-top:8px solid {acc}; }}
.label {{ align-self:flex-start; display:inline-block; background:{acc}; color:{acc_ink}; font-family:Oswald; font-weight:700; font-size:34px;
  letter-spacing:2px; padding:2px 16px 4px; margin-bottom:10px; }}
.hl-line span {{ font-family:{font}; color:{ink}; line-height:1.04; }}
.hi {{ color:{acc}; }}
.q {{ font-family:Oswald !important; font-weight:700; }}
.speaker {{ font-family:Oswald; font-weight:600; color:{acc}; font-size:40px; margin-top:10px; }}
.dek {{ font-family:Inter; font-weight:600; color:{ink}; opacity:.85; font-size:34px; line-height:1.25; margin-top:12px; }}
.stat span {{ font-family:{font}; color:{acc}; line-height:1; }}
.statl {{ font-family:Oswald; font-weight:700; color:{ink}; font-size:44px; letter-spacing:3px; }}
.vs {{ position:absolute; left:490px; top:735px; z-index:2; width:100px; height:100px; border-radius:50%; background:{acc}; color:{acc_ink};
  font-family:{font}; font-size:52px; text-align:center; line-height:100px; box-shadow:0 8px 24px rgba(0,0,0,.6); }}
.nm {{ position:absolute; top:750px; height:70px; background:{acc}; text-align:center; padding-top:6px; }}
.nm span {{ font-family:{font}; color:{acc_ink}; }}
.crow {{ display:flex; align-items:center; border-bottom:2px solid rgba(255,255,255,.12); padding:10px 0; }}
.cv {{ flex:1; font-family:{font}; font-size:58px; color:{ink}; text-align:center; }}
.cl {{ width:360px; font-family:Oswald; font-weight:700; font-size:30px; color:{acc}; text-align:center; letter-spacing:2px; }}
.tag {{ position:absolute; background:{acc}; color:{acc_ink}; font-family:{font}; font-size:52px; padding:0 18px 4px; }}
.tag b {{ font-family:Oswald; font-weight:700; font-size:40px; }}
.rows {{ margin-top:12px; }}
.row {{ display:flex; align-items:center; gap:22px; padding:12px 0; border-bottom:2px solid rgba(255,255,255,.1); }}
.rn {{ width:70px; height:70px; flex:none; background:{acc}; color:{acc_ink}; font-family:{font}; font-size:50px; text-align:center; line-height:70px; }}
.rt span {{ font-family:{font}; color:{ink}; }}
.rv {{ font-family:Inter; font-weight:600; color:{ink}; opacity:.8; font-size:28px; }}
.tl .td {{ width:200px; flex:none; font-family:Oswald; font-weight:700; color:{acc}; font-size:40px; }}
.tl .dot {{ width:22px; height:22px; flex:none; border-radius:50%; background:{acc}; }}
.tl .rt span {{ font-family:Inter; font-weight:700; }}
.logo {{ position:absolute; right:30px; bottom:12px; font-family:{t["logo_font"]}; font-weight:700; font-size:26px; letter-spacing:4px; color:{acc}; opacity:.9; }}
"""
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body><div class="card">'
            f"{body}{logo}</div><script>{FIT_JS}</script></body></html>")


def face_clear(spec) -> list[str]:
    """Generic layouts never put text on a photo; nothing to plan."""
    return []


__all__ = ["GENERIC", "BOXES", "FONTS", "build", "theme_of", "items_text", "luminance", "contrast_ink", "DEFAULT_THEME"]
