# Template specs: a post template as data

A page's post templates used to be code: the card renderer only drew the layouts written in
`bore/cards.py` (each page's own) and `bore/layouts.py` (the generic ones), so a template an editor
added to the page's templates document could only be *described* to the session, never drawn. A
**template spec** is one template as a JSON file. The card renderer draws any valid spec, lint
checks copy against its slot rules and verify reads its text back, so a new template needs a new
file, never new code.

Code: `bore/tspec.py` (load, validate, plan, draw), `bore/cards.py` (`CardSpec.tspec`, `render`),
`bore/guard.py` (lint and verify). Examples (made up): `tests/fixtures/tspec/`.

## Where specs live

Specs are made from a page's templates document, which is confidential: they never go into the
repository, logs or prompts. The renderer looks for a spec in this order:

1. the brief: `"template_specs": {"<id>": {spec}, ...}` (or a list of specs);
2. the page's spec store: `private/templates/<page>/<id>.json`, the specs the page's editors
   approved, written by `studio sync` (a spec taken out of use is removed there).

A spec that does not validate is never drawn; lint names its problems when copy.json picks it.

## How a page gets its specs

1. A templates document made active (Admin > Pages: a new upload, or another version made active)
   queues a Studio job of kind `templates` (dashboard `routes/templates.js`; an admin can also press
   **Convert templates again** under Templates).
2. The Studio session (STUDIO.md TEMPLATES) writes one spec per template from the document's text
   and page pictures, draws a preview on neutral stand-in photos (`tspec-render --sample`), compares
   it with the page and sends spec, preview and page (`studio template-send`).
3. Under **Templates** (Admin > Templates; editors see it in the top bar), an editor of the page or
   an admin sees each template's document page beside its preview, with the document version it came
   from, and approves it or rejects it with a note. A rejected one is converted again with the note
   (a new version); one taken out of use is no longer drawn. Only the approved version is in the
   routines' config.
4. `studio sync` writes the approved specs into the store; the briefs list them as `templates`
   (`id`, `name`, `use_when`, `visual_lane`, `art_layer`, `slots`, `file`). The Studio's ideas name
   one (`template_spec`) whenever the page has any, COMPOSE draws exactly that one on the editor's
   photo, and the hourly routine may use one whose `use_when` fits.

## A spec

Every box is `[x, y, w, h]` as fractions of the canvas (0-1), from the top left. Colours are a
theme role or `#rrggbb`, optionally with `/<opacity 0-100>` (`"bg/80"`). Theme roles come from the
page's colours in the admin panel: `accent`, `bg`, `ink` (text on the background), `accent_ink`
(readable text on the accent), `white`, `black`. Text colours may also be `auto`: readable ink on
the zone's plate, or on the background. Fonts are roles: `display` (Anton), `condensed` (Oswald),
`body` (Inter), `script` (Permanent Marker). Unknown keys are refused (a typo never goes unseen).

| key | meaning |
|---|---|
| `id` | what copy.json names: 1-48 lower-case letters, digits, `-`, `_` |
| `name` | the template's name, as editors know it |
| `use_when` | one or two sentences: which story it is for (the ideas step reads it) |
| `limits` | optional, up to 400 characters: what the document's template shows that this format cannot draw (a cut-out subject, a number behind the subject, an angled split, callout lines), in words for the editors: the Templates screen shows it beside the preview |
| `version` | optional number, for the page's own records |
| `canvas` | `{"w": 1080, "h": 1350}` by default (the feed's 4:5); `w`, `h` 600-2400 px |
| `visual_lane` | the kind of visual: `ai_art` (AI-made artwork with typeset text over it), `clean`, `editorial`, `screenshot` (photos default to `contain`). Default `clean` |
| `background` | `fill` (a colour, default `bg`), `gradient` (`to`: a colour; `direction`: `to bottom` / `to left` ... / `<n>deg` / `radial`), `texture` (`none`, `grit`, `noise`, `worn` = both, `stripes`, `halftone`) |
| `art` | optional AI-art layer: `box`, `blend` (`normal`, `screen`, `multiply`, `overlay`, `soft-light`), `opacity`, `prompt` (10-600 characters: the artwork the document shows in this layer, as a picture description with no people and no text; Krea paints it in the page's colours). Drawn over the background, under the photos, only when the session made art (Krea); without art the background shows. The preview marks the layer's box AI ART HERE |
| `photos` | the photo zones, in drawing order (later ones on top). At least one shows photo 0 |
| `text` | text zones, drawn over the photos in order |
| `lists` | list zones (stat plates), drawn over the photos |
| `logo` | where the page's logo text goes (the theme's logo text; nothing when it has none) |
| `slots` | the text the copy fills, by name, with the rules lint enforces |
| `rules` | `max_words`: the whole card's words (default 26) |

### Photo zones (`photos`)

| key | meaning |
|---|---|
| `box` | where the photo shows |
| `photo` | which photo: 0 is the editor's chosen photo (always shown, never optional), 1, 2 ... are copy.json's `extra_photo_indexes` in order. Default: the zone's position in the list |
| `fit` | `cover` (fills the box, cropped) or `contain` (the whole photo over a blurred copy of itself) |
| `focus` | `[x, y]` (0-1 of the box): where the subject's face centre should sit. Default `[0.5, 0.42]` |
| `duotone` | optional `[dark colour, light colour]`: the photo mapped onto two theme colours |
| `shade` | optional list of edge gradients for text legibility: `edge` (`top`, `bottom`, `left`, `right`), `size` (0-1 of the box), `color` (default `bg`) |
| `border` | optional `width` (px, default 6) and `color` (default `accent`) |
| `radius` | corner radius, px |
| `contain_fallback` | default true: when no crop keeps the face clear, show the whole photo instead of refusing |
| `optional` | an extra photo zone that may stay empty (left out when copy.json gives no photo for it) |

### Text zones (`text`)

Each zone draws either a slot (`"slot": "headline"`) or fixed words (`"text": "VS"`, 1-30
characters, added by the layout like a logo).

| key | meaning |
|---|---|
| `box` | the zone; the text is fitted inside it |
| `alt_box` | optional second position, used when the text in `box` would touch a face |
| `font`, `weight` | font role and weight (100-900; defaults per font) |
| `size` | `[min_px, max_px]`: auto-fit starts at the largest size and shrinks until the text fits the box in `max_lines` lines |
| `max_lines` | lines the zone holds (1-8, default 2). A slot given as a list of lines must not have more |
| `fit` | `block` (one size for all lines, default) or `each_line` (a stacked headline: each line as large as the width allows, all capped until the block fits) |
| `case` | `upper` (default), `lower`, `title`, `as_is` |
| `color` | text colour (default `ink`, `auto` on a plate) |
| `highlight` | colour of copy.json's `highlight` phrase in this zone (default `accent`, `ink` when the text is accent) |
| `align`, `valign` | `left` / `center` / `right`; `top` / `middle` / `bottom` |
| `plate` | optional background behind the text: a colour, or an object with `color`, `style` (`block`: one box, the default; `lines`: a box per line, like stacked labels) and `pad` (`[vertical, horizontal]` px) |
| `line_height`, `letter_spacing` | type spacing (0.7-2.0; -5 to 30 px) |
| `rotation` | must be 0: card text is read back by OCR |
| `worn` | true: the worn-print lettering of the pages' display type (verify reads the clean twin) |

### List zones (`lists`)

Stat plates: a list slot of 2-4 `{"value", "label"}` items, one plate each.

| key | meaning |
|---|---|
| `slot`, `box` | the list slot and the area the plates share |
| `direction` | `column` (stacked, default) or `row` |
| `gap` | px between plates |
| `layout` | `stack` (value over label, default) or `inline` (value then label) |
| `plate`, `emphasis_plate` | plate colour; when `emphasis_plate` is set, the last plate takes it (so it stands out) |
| `align` | text alignment on the plates |
| `value`, `label` | `font`, `weight`, `size` ([min, max]: one size on every plate), `color` (default `auto`), `case` |

### Logo (`logo`)

`box`, `font` (default `condensed`), `weight`, `size`, `color` (default `accent`), `align`,
`valign`. It draws the theme's logo text, which verify knows as the page's brand.

### Slots (`slots`)

| key | meaning |
|---|---|
| `type` | `text` (a string, wrapped to its zone, or a list of lines), `number` (text that must contain digits, for a big figure), `list` (stat plates) |
| `required` | default true |
| `max_words`, `min_words`, `max_chars` | limits on the whole slot (its lines joined) |
| `min_items`, `max_items` | list slots: how many plates (default 1-4) |
| `value_max_chars`, `label_max_words`, `label_max_chars` | list slots: each plate's limits |
| `examples` | examples of good copy (text: strings or line lists; list: item lists). `tspec-render` without `--copy` draws the first |
| `hint` | what the slot says, for the session writing the copy |

Every slot is drawn by exactly one zone, and every zone's slot exists.

## How it is drawn

`cards.render(page, card)` with `card.tspec` set: the background, then the art layer, the photo
zones, the text zones, the list zones and the logo, at the canvas size, in headless Chromium like
every card. Text is real type, sized by auto-fit; text that does not fit its zone even at the
smallest size refuses the card (`TextOverflow`: shorten the copy) rather than clip it.

**Faces stay clear** (the same rule as the coded templates). The faces in each photo (the
detector `verify` uses) are protected with verify's head zone: the face, the hair above it and a
margin. No text, list or logo zone, no photo zone drawn on top and no darker half of a shade may
touch a visible face, and the subject's (largest) face stays whole inside its zone. The renderer
tries, in order: moving or zooming the photo inside its zone, nearest the zone's `focus`; the text
zones' `alt_box` positions; the whole photo (`contain`). When nothing works, the card is refused
(`FaceBlocked`, "BLOCKED: ... choose a photo where the face is smaller or positioned differently,
or another template"). `verify` then checks the finished card with OCR and face detection, as for
every card.

**Watermarks are never cropped or covered** (the owner's rule). A site, a handle or an agency
credit read in the photo (OCR) is kept whole inside its zone and clear of every zone over it, by
the same search; the editor's photo is never swapped.

## copy.json with a spec

```json
{"template_spec": "mega-figure",
 "slots": {"headline": ["MOST POINTS", "IN ONE NIGHT"], "figure": "61", "tagline": "A CLUB RECORD SINCE 1998"},
 "headline_lines": ["MOST POINTS IN ONE NIGHT"], "highlight": "",
 "photo_index": 0, "people_in_photo": ["<who is visible>"], "caption": "...", "hashtags": ["..."]}
```

- `slots` is the card's text, and the only text on it. `headline_lines` is still required: it is the
  post's title in the dashboard (not drawn). `template` may be left out.
- `label`, `dek`, `stat_value`, `stat_label`, `footer`, `footer_sub` and `items` are refused next to
  a spec: their text belongs in a slot.
- Lint checks the slot rules (required slots, word / character limits, lines per zone, plates per
  list, digits in a number slot, the card's total words) and everything it checks on any card:
  every number from the sources, the source's certainty on each slot (a stat plate's value with its
  label is one statement), nothing repeated, the highlight on the card, the photo, the people in it,
  and one `extra_photo_indexes` entry per extra photo zone.
- Verify reads every slot's text back from the card (each line of a list slot, each plate's value
  and label), and allows the spec's fixed words and the logo as the layout's own.

## Drawing it in the pipeline

```python
from bore import tspec
from bore.cards import render
spec = tspec.find(copy["template_spec"], brief)      # SpecError: not found / not valid
card = tspec.card_spec(spec, copy, photos, theme=brief["studio"]["theme"], art=art_or_None)
jpeg = render(brief["page"], card)                    # FaceBlocked / TextOverflow refuse the card
```

`photos[0]` is the editor's chosen photo (`photo_index`), the others `extra_photo_indexes` in order.
`card_spec` finds the faces and watermarks in each photo itself. Render a clean twin
(`dataclasses.replace(card, distress=False)`) for verify, as `render_fallback` does.

## Previewing a spec

```bash
python -m bore.cli routine tspec-render <spec.json> <photo> --copy <copy.json> --out <png>
python -m bore.cli routine tspec-render <spec.json> --sample --page <slug> --out <png>
```

Without `--copy` it draws each slot's first example. `--page <slug>` uses that page's colours and
logo text. `--sample` draws on neutral stand-in photos (a plain backdrop with a grey head and
shoulders, no real person; one per photo the spec shows), whose face the planner keeps clear as it
would a real one: the preview editors approve. It prints the slot rules the copy breaks, `BLOCKED: ...` when no placement keeps the
face clear, and `REFUSED: ...` for a spec that does not validate or text that does not fit.
