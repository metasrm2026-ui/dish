# Studio jobs: instructions for the Claude session

Editors build posts in the Post Studio (the dashboard's Studio tab). Every AI step they start
there is a **job** this session does: write visual ideas, search images, compose the visual and
caption, or review a finished post; and, when a page gets a new templates document, turn its
templates into templates the card renderer draws (TEMPLATES). Jobs are small and an editor is
waiting: work fast, one job at a time, and always send a result or a reason back. `studio jobs` claims one job (the oldest);
when it is done, run `python -m bore.cli studio jobs` again for the next, until it prints
`no Studio jobs waiting`. Other sessions take the rest at the same time.

Nothing is published from here. Posting happens only when an editor presses Send.

Each page's own rules (its voice, caption rules, card templates, what it posts) are in its playbook
and reference documents, which every brief carries. A paused page's Studio is read-only: the
dashboard starts no job for it.

## 0. Setup

```bash
bash scripts/studio-start.sh          # one step: packages (only if missing), page settings, jobs
```

It installs the packages only if they are missing or do not load (the machines carry a broken
system Pillow; it is repaired in seconds), syncs pages, playbooks and reference-document text
(`studio sync`, into private/), then claims the waiting jobs (`studio jobs`, writing
work/studio/<job>/<post>/). Run it as one command: an editor is waiting for every step.

A problem with this machine or session (a package that does not load, an ImportError, a crashed
command, a network error) is never the editor's and never a reason to fail their job: run
`bash scripts/ensure-deps.sh` (or retry the network call), then the step again. `studio jobs`
leaves a job whose packages do not load here (`LEAVE`), and hands back a job it could not prepare
(`BACK`, with the error; after 3 tries the dashboard tells the editor it failed): fix the problem,
then run `studio jobs` again and do the job.

Secrets come from the environment (`DASHBOARD_URL`, `ROUTINE_SECRET`). Never run `routine state`
here: Studio jobs keep no local state, so they can run while the hourly routine runs.

`studio jobs` prints a `JOB` line for the job it claimed, with its kind, page and the editor's
note. Each job is claimed by one session only: several sessions start when editors click quickly,
and a job another session took prints `SKIP` (leave it). If it prints `no Studio jobs waiting`, you are done. Each post folder has `brief.json`: the story
(`studio.story`), what the article says (`posts`), the editor's chosen idea (`studio.idea`), the
page's playbook, `page_templates`, the generic `layouts`, `templates` (the page's own templates,
approved by its editors: see IDEAS) and `studio.docs`: the text of the page's reference documents
(post templates, visual references, caption tone) in `private/docs/`, and for the templates and
visual references documents a picture of each page (`studio.docs.<type>.images`:
`private/docs/<page>/<type>/page-NN.png`, drawn when an IDEAS, COMPOSE or templates job is prepared;
`studio sync` downloads no document; empty only when they could not be drawn here, then read the
text); their zone diagrams and examples are pictures, so open them
when they are there, not only the text). Read the documents that the step needs. They are confidential: never
quote them in output that leaves this session, and never paste them into job results.

Editors chose these stories: never refuse one for its topic (crime, abuse, death, terrorism,
suicide, politics) or for being off the page's usual niche. Write it by ROUTINE.md's "Crime, abuse
and tragedy are news" rules, leave out only what those rules forbid, and say what you left out in
the post's notes. A post fails only when it is technically impossible (no readable source, no
usable image after the search and the editor's own upload).

When a post cannot be done (only for a reason about the post itself, after any problem on this
machine was fixed), say why in words the editor understands and move on:
`python -m bore.cli studio fail work/studio/<job>/<post> --reason "<why>"`. Then finish the
job with `python -m bore.cli studio done studio/<job>`. A job is never left unfinished.

## IDEAS

For each post write `<post>/ideas.json`: exactly two different visual ideas,

```json
[{"title": "The number", "template": "<the template's name>", "template_spec": "<its id>",
  "description": "2-3 sentences: the photo(s), what the card says, the mood"}]
```

- When the brief lists `templates` (the page's own templates, converted from its templates
  document and approved by its editors: `id`, `name`, `use_when`, `visual_lane`, `slots`), an idea
  whose story one of them fits names it: `template_spec` its id and `template` its name, the one
  whose `use_when` fits best (both ideas may use one template with two different concepts). The
  card is then drawn with exactly that template. Prefer them when one fits; while only some of the
  document's templates are approved, none may fit: then the idea uses a layout as below (never
  force a story into a template that does not suit it). `studio done` refuses an idea naming a
  template that is not approved.
- Otherwise (or on a page with no approved templates yet): `template` is the name the page's
  **post templates document** uses for the template that best fits the story
  (`studio.docs.templates`; if the page has no document, the playbook's card templates), and
  `layout` is how it gets drawn: one of `page_templates` (the page's own designs) or a generic
  layout from `layouts` (headline, quote, stat_card, comparison, then_vs_now, ranked_list,
  timeline). Pick the closest.
- Follow the **visual references document** (`studio.docs.visual`) for look and photo choices.
- Only facts in the story. When `studio.summary_needed` is true (a pasted link, or a summary that
  is missing or stops mid-sentence, like a site's preview text cut at "...due to a"), also write
  `<post>/summary.json` `{"headline": "...", "summary": "2-3 complete lines"}` from what the
  article says. `studio done` refuses an IDEAS job whose summary still stops mid-sentence.

## IMAGES

Search for real photos of the story's subject:

```bash
python -m bore.cli studio images work/studio/<job>/<post> --query "<Full Name>"
```

If the editor typed who or what to search for (`studio.feedback`: "Search for photos of: ..."),
search that first. The bare full name works best; add the team/show if the name is common.
Results must name the person in full, as a phrase (a middle initial or Jr. allowed); for a thing
rather than a person, pass the words it must mention with `--must "monarch butterfly"` (plurals
count). `images.json` is checked automatically: the picture other outlets used for this story
comes first, then news photos, DuckDuckGo, Bing and Wikimedia (credit the author: see its licence
flag); designed graphics, screenshots, scoreboards, collages, duplicates,
stock and shop sites are dropped. Faces are compared with the subject's (bore/faceid.py): photos
of them come first (`id_match: "match"`; the editor sees "matches <Name>"), a clear face of
someone else last (`"mismatch"`: "may not be <Name>"); nothing is dropped on it, and no
`id_match` (a helmet, a profile, no face, or no ESPN/Wikipedia photo of the subject to compare
with) is common. Each kept image is also saved in
`<post>/search/` (`file`): open them, and delete wrong-person or off-story entries. `images_report.json` says what each
source returned or why it failed: if the searches worked and nothing good remains, fail the job
with that reason (sources that timed out or could not connect are a machine problem: search again). Run it again with other
queries until there are a few good candidates (the results add up in `images.json`). Look at
them: delete entries from `images.json` that are designed graphics, show the wrong person, or are
blurry. The editor sees the source of each one. Between equally sharp results, the kinds of photo
this page's editors picked before come first; the dashboard records which candidate they choose.

## COMPOSE (the visual, caption and hashtags)

The editor chose the idea (`studio.idea`: an idea's `template`/`layout`, or their own words) and
the base image (`photos[0]`). If `studio.base_image_error` is set, fail the post with that reason.
`studio.feedback` is the editor's note when they asked to regenerate: do what it says.

1. The editor chose `photos/0.jpg`. The card MUST be built on that image (`"photo_index": 0`):
   never replace it with another candidate, even if you think another photo is better (`studio
   done` refuses a card on any other photo). Text that is part of their image (a poster or album
   title) is fine: note it in `warnings_checked`. Only if the image truly cannot make a card
   (not an image of the story at all, or a face can't be kept clear in any layout), fail the post
   with `studio fail` and a reason the editor can act on ("pick a photo where ..."). For
   comparison / then_vs_now the second photo (`extra_photo_indexes`) may come from `photos/1..`.
2. Write `copy.json` (`copy_schema` in the brief):
   - The idea names one of the page's templates (`studio.idea.template_spec`): `template_spec` is
     that id, never another (`studio done` refuses another; if it truly cannot be drawn for this
     story or photo, fail the post and say why). Open its spec (`templates[].file`, the format in
     docs/TEMPLATE_SPEC.md) and fill `slots`: every slot it requires, each within its rules (its
     `hint`, words, characters, lines; a list slot's plates). The card shows the slots and nothing
     else: no `label`, `dek`, `stat_*`, `footer*` or `items`; `headline_lines` stays (the post's
     title in the dashboard). "My idea" on a page with templates: the one whose `use_when` fits.
   - Otherwise `template`: the layout the idea names (`layout`), or the best fit for "My idea";
     card text: `headline_lines`, `label`, `dek`, `stat_value`/`stat_label`, `items` as the
     layout needs (see `layouts`); short, punchy, every name spelled as in the story.
   - `people_in_photo`: exactly who is visible.
   - `caption`: in the page's tone (**caption tone document** `studio.docs.tone`, then the
     playbook's `voice` / `caption_rules` / `examples`), without hashtags; `hashtags`: a list.
   - Only facts and numbers that are in `posts` (the story text). Keep hedges ("reportedly").
3. `python -m bore.cli routine lint studio/<job>/<post>` until `lint: PASS`.
4. Render: premium cards, the Krea flow in ROUTINE.md step 4 (`get_upload_url`, `routine krea-request
   studio/<job>/<post> --upload-url ...`, connector `generate_image`, `routine krea-result`, `routine
   verify`), for every kind of card: one of the page's templates (`template_spec`, the layout image is
   that template drawn with the editor's photo and the slots), a page design (`page_templates`) or a
   generic layout. The card is built on the editor's photo (photos/0.jpg); the face on it must match
   that photo (verify says so when it does not).
   - The template renderer instead (`python -m bore.cli routine fallback studio/<job>/<post>`) when
     this session has no Krea connector, when `krea-request` prints `KREA CAP`, `KREA PACE` or
     `KREA UNCHECKED` (the month's Krea images are shared with the hourly routine), or when the
     premium card fails verify twice. The editor gets that card, never an error.
   - `REFUSED: ... does not fit` means a slot's text is too long for its zone: shorten it. `BLOCKED`
     means no placement keeps the face clear: fail the post and ask for another photo (never another
     photo or template yourself).
5. `python -m bore.cli routine verify studio/<job>/<post>` must PASS. Open `card.jpg` and check it
   by eye exactly as ROUTINE.md step 4b says (faces clear, nothing invented, text reads well),
   then write `verdict.json` the same way.
6. When every post of the job is done (or failed): `python -m bore.cli studio done studio/<job>`.
   The editor sees the visual now.
7. Pre-check, so the check is ready when the editor gets there (they used to skip it because a
   separate check took 10 minutes to start). Visuals come first: claim and finish any other
   waiting job (`studio jobs`) before pre-checking, then pre-check every COMPOSE job you finished,
   before Finish. `python -m bore.cli studio precheck studio/<job>` writes each post's
   `review_request.md`. Give each to a separate fresh subagent with web search, given only that
   file (start them all at once), save each answer as `<post>/check.json`, then
   `python -m bore.cli studio precheck studio/<job> --send`. Do NOT run `studio done` again for
   that job: `--send` sends the check (the dashboard shows "checking automatically" until it
   arrives, and marks it out of date if the editor changes the caption meanwhile).

## TEMPLATES (a page's templates document into templates the renderer draws)

Queued when a page goes live (ONBOARD.md step 5), when its owner uploads a new templates document
(or makes another version active) or presses "Convert templates again", and when an editor rejects
a template with a note. Such a
job has no posts: `work/studio/<job>/brief.json` says what to convert. Editors see each template
you send under Templates, the document page beside your preview, and approve it or reject it with
a note, or take it out of use; only approved templates are offered in IDEAS (beside the layouts)
and drawn. The document is confidential: its names and text go only into this job's files (no
summaries, results or command lines quoting them). The job draws the document's pages itself
(`studio sync` downloads none): a document that cannot be drawn here hands the job back.

1. Read `docs/TEMPLATE_SPEC.md` (the format) and `brief.json`: `doc.text` (the document's text:
   template names, word and character limits, rules) and `doc.images` (each page as a picture:
   open them, the zone diagrams show where each box sits). `convert` is `"all"` or the ids to
   convert again; `notes` has the editor's note for each rejected one; `templates` lists the ones
   converted before (status, note, document pages; the spec is `_current/<id>.json`). `doc` null
   (the page has no templates document any more): `python -m bore.cli studio fail studio/<job> --reason "<why>"`.
2. For each template of the document (or each id in `convert`), a folder `work/studio/<job>/<id>/`
   (a template converted before keeps its id, so its versions follow on; a new one's id is neutral:
   `new_id` in `brief.json` (the next `tN` no template in `templates` has), then counting up from it
   for each further new one, never its place in the document (a template added in the middle would
   take another one's id and replace it) and never made from its name, because ids reach folder
   names and logs):
   - `spec.json`: `name` as the document names it; `use_when` from what the document says the
     template is for; `visual_lane` from the kind of visual it is (AI-made artwork with typeset
     text over it: `ai_art` with an `art` layer, whose `prompt` describes the artwork the document
     shows there, no people and no text; the photo with plain type: `clean`; `editorial`;
     a screenshot: `screenshot`); `limits`: in plain words, what the document's template shows
     that the format cannot draw (a cut-out subject, a number behind the subject, an angled split,
     callout lines), so the editors know what the preview leaves out; every box of the diagram (photos, text, numbers, plates, logo)
     as a zone measured off the page picture, in fractions of the card; each text
     slot with the document's word and character limits and lines, and a `hint`; `examples`:
     neutral made-up sample text for each slot, as long as a real post's (the preview draws the
     first). Photo 0 is always the editor's photo, never a fixed picture; text is never rotated.
   - `source.json`: `{"pages": [n]}`, the document page(s) it was made from (page-NN.png).
   - the preview, on neutral stand-in photos (no real person):
     `python -m bore.cli routine tspec-render work/studio/<job>/<id>/spec.json --sample --page <page> --out work/studio/<job>/<id>/preview.png`.
     It prints the slot rules the examples break, `REFUSED:` with every problem of a spec that
     does not validate, `BLOCKED:` when no placement keeps the face clear. Open the preview next to
     the page picture and compare zone by zone (place, size, colour, plates, order); fix spec.json
     and draw it again until it looks like the diagram.
   - send it: `python -m bore.cli studio template-send work/studio/<job>/<id>`. Send each one as
     soon as it is right: the editors see it at once, and a job with nothing sent for 20 minutes
     counts as stalled (another session would take it over).
   A rejected template: start from `_current/<id>.json`, do what its note says, keep its id. A
   template the format cannot draw: `python -m bore.cli studio fail work/studio/<job>/<id> --reason "<why, for the editors>"`.
3. `python -m bore.cli studio done studio/<job>` sends any not sent yet and finishes the job (it
   refuses while a rejected template is neither converted again nor failed).

## CHECK (independent review)

The check must be independent of whoever made the post. For each post, give the text of
`<post>/review_request.md` (it points at `card.jpg`) to a **separate subagent** (Agent tool, a
fresh general-purpose agent) with web search. Do not add anything about how the post was made,
and do not review it yourself. Save the subagent's JSON answer as `<post>/check.json`
(five lists: factual, timing, grammar, other, guidelines; empty = no issues). Then
`python -m bore.cli studio done studio/<job>`.

## Finish

Send the pre-checks of every COMPOSE job you finished that has none yet (COMPOSE step 7). Then
`python -m bore.cli studio jobs` again: new jobs may have arrived while you worked. Stop when it
prints `no Studio jobs waiting`. End with a one-line summary per job (kind, page, done/failed; for
TEMPLATES how many were sent and failed, no template names).
