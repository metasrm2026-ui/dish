# Hourly routine: instructions for the Claude session

You are running the hourly post-creation routine for every live page of Bore Studio. Your job
this hour: turn this hour's verified stories into finished posts (infographic + caption) at the
same quality as each page's own posts, and put them on the approval dashboard. Nothing is
published from here: a human approves every post.

Each page's own rules (its niche, voice, caption rules, card templates, what it posts and never
posts) are in its playbook and reference documents, which every brief carries. This file has the
rules for every page; where a playbook is more specific, it wins, except over the rules on facts,
photos, watermarks and crime and tragedy below.

The quality bar is the pages' own posts. A post below that bar is worse than no post.

## 0. Setup (every run)

```bash
bash scripts/ensure-deps.sh      # packages, only if missing or not loading (python -m pip, never bare pip)
python -m bore.cli studio sync                     # pages, sources, playbooks, reference docs (private/)
python -m bore.cli routine state --action pull     # restore the database from the dashboard
```

If `ensure-deps.sh` ends with "still not working" (the packages) or "the card browser still does not
start" (Chromium, which draws every card), it has already tried to repair them: report its lines and
stop. Cards cannot be checked or drawn on this machine (`prepare` refuses anyway).
If the pull fails, run it once more. If it fails twice, report it and stop: do not run `prepare` and
never push (both refuse anyway until a pull has restored the database; `--fresh` is only for a new
database the product's owner asked for).

Page settings, sources and playbooks are not in the repository: each page's owner manages them
on the dashboard (the onboarding routine proposed the sources and wrote the playbook: ONBOARD.md),
and `studio sync` brings them into `private/`. Only live pages get hourly posts: a paused page, or
one still going live, gets quota 0.

Secrets come from the environment: `APIFY_TOKEN`, `DASHBOARD_URL`, `ROUTINE_SECRET`.
Krea runs through the **Krea connector** (your `generate_image` / `get_job` / `get_upload_url`
tools), which bills the plan's monthly compute units. Never use a Krea API token.

## 1. Prepare

```bash
python -m bore.cli routine prepare
```

`prepare` can take up to 10 minutes (collection, then photo checks): run it with a 10-minute
command timeout, and don't start it twice.

This reads the websites among the page's sources from their own RSS feeds (free, every hour; a
website with no feed is searched on Google instead), collects X / Reddit / web within the Apify
budget, asks the dashboard how many posts each page already has today (each page's
`posts_per_day`, within the limit the product's owner set), selects this hour's picks plus
backups, and writes one brief per story to `work/<run>/<id>/brief.json` with sharp candidate
photos in `photos/`.
It also fetches posts a reviewer sent back from the dashboard with feedback (**REGEN** lines, see
2b) and links editors pasted into "Post from a link" (**LINK** lines, see 2c). Both are briefed
first, before any collection, and handled first, whatever the quota. If Apify cannot be used (no
token, a rejected one, an outage), `prepare` prints `Apify unavailable ... collection skipped`, still
writes the REGEN and LINK briefs and picks from the stories already stored: carry on, and report
that line in the summary. If it prints quota 0 for every page and no REGEN or LINK lines, skip to
step 6.

## 2. Decide and write (for each PICK, then backups only if a pick is skipped)

**Fill the quota.** `prepare` prints each page's quota for this run (e.g. "quota 2"). That many
posts per page should reach the dashboard this hour: work through the PICKs, and for every pick
you skip, take the next backup, until the quota is met or the backups run out. Skipping is right
for weak stories; just keep going down the list instead of stopping. A run that uploads nothing
while backups were left is a failed run. Record every skip, with its reason:
`python -m bore.cli routine skip <dir> --reason "<why>"` (add `--photo` when the ONLY problem is
that no usable photo of the subject exists: the story comes back in a few hours, when newer coverage
may carry one; a reason that is only about the photo, e.g. "no sharp photo of <name> found", counts
as `--photo` too, so keep story faults out of a photo reason and photo notes out of a story reason)
(or `"decision": "skip"` plus `reason`
in copy.json, then `routine lint`). `routine check` (step 6) refuses to finish while a page is
under quota and backups are undecided, and while an editor's LINK or REGEN is neither uploaded nor
reported back (those are never skipped). In the summary, report per page: quota, uploaded, skipped
(with the reason for each).


Read `brief.json` (story posts, sources, verification, photos, page playbook). Look at every
candidate photo (`photos/*.jpg`) before choosing one.

**Learn from the editors first.** `lessons` in the brief is what the editors' reviews teach, counted
per story kind and rebuilt daily from every review (and within the hour of an editor's new note): `rules` (AVOID / BE PICKY / PREFER with counts and
examples; EDITORS' NOTES: what their notes keep saying; PHOTO: kinds of photo editors reject for the
image), `admin_notes` (the page owner's own instructions: follow them), `notes` (the editors' latest 8 notes
in their own words, with the reason, kind, template and headline of the post: follow them),
`this_kind` (this story's kind, `brief.kind`, with its approvals, examples on both sides and older
notes written about posts of this kind), `template_notes` (older notes about posts made with each card
template: read the one for the template you choose; each note appears once in the brief) and
`avoid_templates`. Lessons override
the playbook's hand-written APPROVED / REJECTED KINDS where they disagree. If this story's kind is
AVOID, skip it unless it clearly has what the approved examples had; if you post it anyway, write
`lesson_override` in copy.json saying what (editors see it). Never switch to a template the story
does not fit just to get around a photo problem: skip instead. Set `kind` in copy.json (brief.kind, or the right one of
`brief.kinds`). `editor_feedback` in the brief lists this page's posts from the
last 7 days that editors marked Not okay (with their reason, and `editor_note` when they wrote
one: follow it) and the ones they approved. Before
deciding, compare the story with both lists: if it is the same *kind* of story as the rejected
"Weak story" / "Off-niche" ones (not just the same person), skip it. If it is like the approved
ones, go ahead. The playbook's `worth_posting` has the APPROVED KINDS / REJECTED KINDS learned so
far. BE PICKY kinds are approved about half the time: while the page is under quota, post them
unless the story has the weakness named in the rejected examples or notes. Skip AVOID kinds.
Skipping costs posts too: editors can only approve what reaches them.
Judge a kind by its own counts (`lessons.this_kind`), not by one rejected example and not by
`editor_feedback.reasons`, which adds up every kind's rejections together (a page's total of "Weak
story" rejections is every kind's, not this story's kind's; one rejected example of a kind editors
usually approve is no reason to skip it).

Skip the story (`"decision": "skip"`) when any of these hold:
- it is outside the page's niche or scores below 8 for niche fit (playbook `worth_posting`). A
  playbook's NO for politics/policy is about politicians and policy themselves: a well-known person
  in the page's niche clashing with, mocking or answering a politician is about that person, and
  judged as such. `prepare` already drops stories about institutions, elections, courts and wars
  when the playbook rules politics out (a verdict or sentence, and a government trading shots with
  someone, still reach you: judge those)
- it is one of the playbook's REJECTED KINDS, or an AVOID kind in `lessons`
- it is a private person's serious health crisis that they or their family have not made public
- the sources don't actually support a clear, postable fact
- route is `watchlist` (single source) and the source is not a credible reporter/outlet. A
  website the page's owner chose as a source (`verification.admin_sources`) counts as credible
  for this page, even if the playbook lists it under `trade_only_sources` (that list is about
  industry trade news on X): judge its stories on the niche and the story itself, word them as
  that outlet's report and attribute it in the caption ("per <outlet>")
- it is a live in-game update that will be old news when it is read: a post reaches readers hours
  after you write it (editors review in batches, and the publisher
  schedules an approved post at least an hour after approval, at any time of day). Skip
  "questionable to return" and halftime or quarter stats; post the outcome instead (ruled out,
  final stat line, record, a game-winning play) when the sources have it
- it is the same story as anything in `recent_posts` (this page's posts from the last 4 days,
  including rejected ones), even with a new angle, a new quote or more engagement. `prepare`
  already drops clear repeats; you catch the rest. The only exception is a genuinely NEW
  development that changes the story (questionable -> ruled OUT, rumour -> signed, arrested ->
  charged): then set `update_of` to that post's id and `what_is_new` to the new fact. The
  reviewer sees it flagged as an update. `upload` refuses repeats without this.

Photo rules:
- Each photo in the brief says where it came from (`via`, `post_url`), how many outlets or accounts
  used the same picture (`consensus`) and any `flags`. Photos come in order: an HD frame of the
  story's video matching the picture other outlets used, that picture ("used by N outlets"), other
  stills of the video, then the story's own photos. For a news event, prefer the
  picture other outlets used over the article's own stock or file photo (flagged "other outlets
  used a different picture"); never use generic stock when such a picture exists. Pick a clean
  copy over a watermarked one; never remove or crop a watermark. `violent incident`: never a
  picture showing the victim being harmed (a frame from just before, the place, or the animal /
  vehicle alone is fine; editors can still choose otherwise in the Studio). Never use a photo
  flagged `scoreboard`, `text-heavy`, `wide broadcast shot`, `collage` or `much sharper than the
  footage` (possibly AI-enhanced). `soft`, `agency photo` and `file photo` are warnings: prefer
  another when one is as good. Follow the playbook's own photo rules (a page may never use
  broadcast frames, for example). A PHOTO rule in `lessons`
  (e.g. "editors rejected 4 of 6 posts whose photo was a video frame") is a warning of the same
  kind: prefer another candidate that passes the checks; if none does, the rule is no reason to skip.
  Among candidates that passed the same checks, the kinds editors kept come first (a tie-breaker).
  The draft records which candidate you chose, so an "Image quality" rejection teaches about it.
- Use only real photographs of the story's subject. Never another outlet's designed graphic
  (logos, banners, composites, "THE SWEEP" style art) and never a photo showing unrelated people.
- If no candidate is a sharp photo of the subject, find one:
  `python -m bore.cli routine photos <dir> --query "<Full Name>"` (add `--team` for a player whose
  name is common; results must name the person in full, as a phrase), then look at the new
  candidates. It prints what each source returned or why it failed: quote that in a skip reason.
  An official headshot (a studio portrait, offered last) is always the right person: use it when no
  action photo of them works, rather than skip the story. A tight headshot (the face fills the
  frame) can still be `BLOCKED`: the renderer already tries the headline at the top and at the
  bottom of every layout. Then skip with `--photo` (the story comes back when newer coverage brings
  a photo).
  For news about someone's career now, prefer a recent photo over an old one.
- Record in `people_in_photo` exactly who is visible. Every person listed must be in the story
  (lint refuses a photo of someone else, e.g. a teammate on a post about another player). Check
  the face really is that person: jersey number, team, the photo's caption/title.
- Faces are compared with the story subject's (`id_name`) reference face (`id_ref`: the ESPN
  headshot or the Wikipedia photo): `id_match: "match"` is that person; `"mismatch"` is a clear
  face of someone else ("may not be"; such photos come last, and lint warns when one is chosen:
  confirm by eye); no `id_match` means it could not tell (helmet, profile, small or no face, or no
  reference photo of the subject exists, e.g. no free Wikipedia photo), which is common and is not
  a reason to reject a photo: check those by caption, jersey and title as before. Prefer a matched
  photo when one is as good as the others.
- The subject's face must be large and clear on the card (verify refuses faces under 100px).
  Use a close or medium shot of the subject; never a wide field / stadium / crowd shot, never a
  photo where the subject is small or turned away. "Image quality" rejections were wide shots,
  wrong people and a card with no photo.
- Every card needs a photo (lint refuses none). If no sharp photo of the subject exists, skip.

Templates (playbook `card.templates` and `template_rules`):
- The playbook says which layout suits which story (`template_rules`) and which it allows
  (`card.templates`, else every generic layout: headline, quote, stat_card, comparison,
  then_vs_now, ranked_list, timeline). `default_template` when nothing more specific fits.
- The page's own templates: when its editors have approved some, the brief lists them as
  `templates` (`id`, `name`, `use_when`, `visual_lane`, `art_layer`, `slots`; the spec itself is
  `file`, its format docs/TEMPLATE_SPEC.md). When one's `use_when` fits the story, draw it: set
  `template_spec` to its id and fill `slots` (every slot it requires, within its rules), with no
  `label`, `dek`, `stat_*`, `footer*` or `items`; `headline_lines` stays (the post's title on the
  dashboard). Lint checks the slot rules and the sources as for any card. Only approved templates
  are listed: never draw one that is not.

The page's reference documents: when the brief lists `docs`, read them before writing (they are the
page's own, uploaded by its owner, and confidential: use them, never copy them into the copy,
notes, summaries or anywhere else):
- `docs.tone.file` (caption language and tone): the page's voice for the card text and the caption.
  Where it and the playbook's `caption_rules` / `examples` disagree on voice, wording, length, emoji
  or hashtags, the tone document wins. The rules in this file (facts only, keep the sources'
  certainty, the crime and tragedy rules below) win over both.
- `docs.visual.file` (visual references): the page's look. Use it to choose between templates and
  layouts that fit the story, and for `art_mood` / `design_notes` (colour, texture, mood; never
  people, text or logos in Krea art).

Write `<dir>/copy.json` following `copy_schema` in the brief and the playbook's `caption_rules`
and `examples` (and the tone document above, when listed). Match the voice, structure, length,
emoji and hashtag habits exactly.
Use only facts, quotes and numbers present in the sources: never invent or embellish.
Card text: headline lines short and punchy per the playbook `headline_rules`; spell every name
exactly as the sources do. Keep the source's certainty: a report that someone "appears unlikely"
to play is "LIKELY OUT", not "OUT". Don't repeat a number or fact across lines.

### Crime, abuse and tragedy are news

These pages are news pages and report them: whatever the news is, it is reported. Never skip a
story, or fail an editor's link, because it involves a crime, sexual violence, abuse of a child, a
killing, terrorism, suicide or self-harm. Write it the way a careful newsroom does:
- Facts only, from the sources, attributed ("per the BBC", "court records show"). Someone charged
  but not convicted is "accused" / "charged with" / "allegedly"; a conviction or sentence is fact.
- Never identify a victim of sexual violence or any child victim: no name, photo, school, address
  or detail that points to them, even when a source gives it. Name the accused and public figures
  as the sources do. A private person is named as accused only once police, a court or a credible
  news outlet has named them (arrested, charged, sued, convicted); otherwise tell the story without
  their name, photo or identifying detail and say so in `note_to_editor`.
- Say what happened in plain words ("married a 13-year-old girl", "was convicted of abusing a
  child", "was killed"), never graphic or sexual detail, and never softened or played down.
- Serious tone: no jokes, no "WTF" framing, no 😳 / 😂 / 👀 emoji (⚖️ or none; 🕊️ only for a
  victim or someone mourned, never for an attacker). The
  closing question is about the case (justice, the sentence, what should change, how it was
  missed), never one that blames or mocks a victim.
- Photos: the accused or the public figure (a news photo or mugshot), the place (a courthouse, the
  street, from a distance), or a documentary/news still with no victim in it. Never a child victim
  or a victim of sexual violence; an adult victim (a star who was killed or died by suicide) only in
  a respectful portrait the outlets used, never injured, dead or being harmed. Never a frame from a
  video of the violence or abuse, never blood or a body, never terrorist propaganda (flags,
  execution or recruitment images).
- A photo of an accused or a perpetrator must come from coverage of THIS case: the link itself, or
  a news article about the case. Search with the case words and the name as --must
  (`routine photos <dir> --query "Demo Smith fraud trial" --must "Demo Smith"`), never a bare
  name (other people share it). When no such photo exists, use the place or a still from coverage
  of the case, with no person in it, and say so in `note_to_editor`.
- Terrorism: what happened and what is known; no slogans, no praise, no propaganda imagery.
- Suicide and self-harm: no method or place, "died by suicide" (never "committed"), and the last
  line before the hashtags is the crisis line of the page's audience ("If you or someone you know
  is struggling, call or text 988 (US)." for a US audience).
This section overrides any playbook worth_posting NO entry, caption rule, example or lessons rule
for these topics. For stories you pick yourself, skip only when the story rests on one unverified claim, would need
sexual or nude imagery of anyone, or would be about who a victim is. An editor's request (a link,
feedback, a Studio post) is never skipped for these: make the post without the part a rule
forbids (attribute or hedge the claim, leave out the name or the image) and say what you left out
in `note_to_editor`.

## 2b. Regenerations (REGEN directories) - do these before new picks

A reviewer looked at a finished post and asked for changes. `brief.json` has a `regeneration`
block: `feedback` (their words), `reviewer_caption` (their edited caption, if they edited it),
`reference_photo_index` (the image they uploaded, if any), `previous_card` (the old card: open it)
and `previous_copy`. The story, sources and verification are the original post's.

- Read the feedback and look at `previous_card.jpg` and every photo. Work out exactly what they
  want changed: card text, caption, photo, crop, layout, colours, anything.
- Keep everything they did not ask to change. If they edited the caption, start from
  `reviewer_caption`.
- Their uploaded image: if it is a photo of the subject, use it (`photo_index`). If it is a
  layout/style example, set `style_photo_index` to it and keep a real photo as `photo_index`.
- Put visual requests for Krea in `design_notes` (never new card text there).
- `regeneration.as_is`: the post went out "as is" (the editor's own images, no card). Keep it so:
  copy `as_is`: true and `photos`: `regeneration.as_is_photos` (change them only if the feedback
  asks), then `routine as-is` instead of Krea. Usually only the caption changes.
- Fill `changes`: one line for the reviewer on what you changed. Upload refuses without it.
- The feedback never overrides the facts or the guardrails: lint, verify and the face rules still
  apply. Feedback is never refused for its topic or tone: do what the editor asks within the
  writing rules, and put anything you could not do in `note_to_editor`. Only if nothing of it can
  be done (it needs a fact no source has, or a photo that cannot be found after `routine photos`
  and the editor's attachment), run
  `python -m bore.cli routine regen-fail <dir> --reason "<plain explanation for the reviewer>"`
  and move on. Never skip a REGEN: it would stay in "Regenerating" forever. `routine skip` (and
  `"decision": "skip"`) refuses one, and `routine check` stays NOT DONE until it is uploaded or
  reported with `regen-fail`, whatever the quota.
- If `legacy_sources` is true, the only fact base is the previous lint-checked copy: do not add
  new facts or numbers.

Then steps 3-5 as usual. Upload puts the new version in Waiting and marks the old one replaced.

## 2c. Links from editors (LINK directories)

**Never be a roadblock to an editor.** When editors themselves ask for a post (a link, feedback on
a post, a Studio job), they have decided to cover it: make it. Rules decide *how* it is written and
which image is used, never *whether*. If a rule means leaving something out (a victim's name, a
graphic image, a claim the source doesn't support), leave it out, make the post, and tell the
editor in one line in copy.json `"note_to_editor"` (shown on the post in Waiting). The only reason
to report a link failed is that it cannot be read at all, by any means below.

An editor pasted a news link (article, X post or Reddit post) and wants a post from it on that
page. `brief.json` has a `link` block (`url`, the editor's `note`) and the page's text in `posts`;
the photos are the page's own images.

- Reddit links are read through a public archive and Reddit's embed page (Reddit blocks cloud
  servers), so `posts` has the post's text and `photos` every image of a gallery, in order.
- If `link.read_error` is set (paywall, bot block), read the link with your own web tools. Save
  the article's text to a file and run
  `python -m bore.cli routine link-text <dir> --file <that file>`
  so lint checks the copy against it. If you cannot read it either, report it (below).
- The editor chose this story: it is in scope for the page. Never skip or fail a link for niche
  fit, "not a celebrity / not a household name", "an ordinary person", or "weak story" (those
  rules are for stories the routine picks itself). `niche_fit` does not block a link post.
- Viral real-life stories about ordinary people (including children) are fine when the post is
  already public and the story is positive or neutral. Use the image from the linked post itself
  (it is already public), keep the tone kind (never mock the person), and add no identifying
  detail the post does not already give (no names, school, address or location).
- A link post is never refused for its topic or its claims: crime, abuse, terrorism, death,
  suicide, politics, a private person, a single source - write it by the rules ("Crime, abuse and
  tragedy are news", attribution, hedging) and note anything left out in `note_to_editor`. A
  repeat of a recent post: make it as an update if anything is new, otherwise make it anyway;
  upload notes the repeat for the editor.
- Follow the editor's `note` (angle, photo, template) unless it conflicts with the facts or rules.
- "As is" posts: when the note asks for the images as they are ("post images as is", "no edits",
  "no branding on the image", "no card"), make no card. Set `"as_is": true`, `"photos": [the
  indexes to post, in order]` (a gallery: all of its images, up to 6, in the post's order) and
  `photo_index` (the first) in copy.json (`headline_lines` stays: it is the post's title in the
  dashboard; `template` any, it is not drawn; `people_in_photo` or `photo_subject` as usual), run
  lint, then
  `python -m bore.cli routine as-is <dir>` instead of step 4 (no Krea). It posts those images
  unedited (no text, logo, crop or credit) with your caption. Look at every image (step 4b: the
  right images, nothing the rules above forbid; leave out any image that breaks them and say so in
  `note_to_editor`), write verdict.json, then upload. If no image of the link could be read
  (`link.images_found` 0, or an X post whose photos `prepare` could not get), find a real photo of
  the story with `routine photos` (for a crime story, by the rule above) and say so in
  `note_to_editor`. "No image courtesy" means no credit line in the caption either.
- The linked page is the only source. The caption attributes it ("per TMZ", "via ESPN"), and
  hedged claims stay hedged. Add no facts from elsewhere. Exception: a Reddit or X post that
  retells a news or crime story. Find the original report with your web tools, add its text with
  `python -m bore.cli routine link-text <dir> --file <text file> --url <article>`, and attribute
  to that outlet ("per the BBC"). If no credible outlet reports it, keep it hedged ("according to
  a viral Reddit post") and say so in `note_to_editor`.
- Find a better photo with `routine photos` if the link's own images are weak or show the wrong
  person. When the link's own image cannot be read (Reddit often blocks it) and the story is about
  a private person, use a real photo of what the story is about instead, with no person in it
  (e.g. butterflies for a butterfly story: `routine photos <dir> --query "monarch butterfly"
  --must "monarch butterfly"`), set `"people_in_photo": []` and `"photo_subject": "<what it shows>"`. Never
  put a photo of some other person on the card as if it were them.
- Then steps 3-5 as usual. Upload marks the link done on the dashboard.
- `link.flags` (a Reddit video, NSFW, a removed post): say it in `note_to_editor` ("the video is
  posted as a still", "the post was removed on Reddit") and still make the post from what was read.
- Never skip a LINK. Only when it cannot be read at all (not by `prepare`, the archive,
  nor your own web tools), run
  `python -m bore.cli routine link-fail <dir> --reason "<plain explanation for the editor>"`.
  `routine skip` refuses a LINK, and `routine check` stays NOT DONE until it is uploaded or
  reported with `link-fail`, whatever the quota.
- The link's own image (an article's is usually 1200x630) is the one to use: lint only warns while
  its short side is 500px or more.

## 3. Lint the copy (mandatory gate, before any image)

```bash
python -m bore.cli routine lint <dir>
```

Deterministic checks against the source posts. Fix `copy.json` until it prints `lint: PASS`:
- the headline keeps the source's certainty ("appears unlikely" -> LIKELY OUT, never OUT;
  single-source stories are worded as reports and the caption attributes them: "per <reporter>")
- every number on the card and in the caption appears in the sources; no number or fact repeats
- line lengths and total card words within limits; the highlight is in the card text
- caption: the page's hashtag count and required tag, a closing question
- the chosen photo meets the resolution floor (an editor's link's own image only warns down to 500px)
`krea-request` and `fallback` run this lint themselves and refuse to run while it fails.

## 4. Make the infographic with Krea (premium cards)

Krea makes the whole card (config `krea.mode: premium`): Nano Banana 2.1 takes the real
photo and the page's own card for this post (drawn first by the template renderer, with the exact
card text, in the layout or approved template you chose) and turns them into a premium poster: the
subject cut out into a dramatic scene, bold 3D or metallic lettering, depth and glow. About 22
credits a card. The card names the photo's source ("Photo: ..."). The model redraws the photo, so
the checks after it matter: every line is read back, the person must match the chosen photo, and a
card that fails twice falls back to the template renderer.

First get an upload URL from the connector's `get_upload_url`, then:

```bash
python -m bore.cli routine krea-request <dir> --upload-url "<upload url>"   # draws the layout, uploads it and the photo, prints model and input
```

Send the printed `model` and `input` to the connector's `generate_image` (sync, or `get_job` until
it is done), then:

```bash
python -m bore.cli routine krea-result <dir> --url "<result image url>"   # saves card.jpg
python -m bore.cli routine verify <dir>        # mandatory gate: OCR of the finished card, and the face
```

- `CONFIRM BY EYE: '<number>'` (a warning): the hero number is drawn too stylised for OCR. Open
  `card.jpg`: it must read exactly that, once. Record it in `verdict.json` `warnings_checked`. If it
  is wrong, it is a FAIL: regenerate.
- `verify` FAIL (a line missing or misspelled, an extra number, text over a face, `the person on the
  card does not look like the person in the chosen photo`): run `krea-request <dir> --upload-url
  ... --feedback "<the errors>"`, generate once more, `krea-result`, `verify`. If it fails again, use
  `python -m bore.cli routine fallback <dir>` (the template renderer's card) and `verify` that.
- `BLOCKED` (the face can't be kept clear of the text in the layout): pick another photo of the
  subject (step 2) and start again. Never switch to a template the story does not fit to get around it.
- `krea-request` prints `KREA CAP`, `KREA PACE` or `KREA UNCHECKED` (the product's month of premium
  cards, `krea.monthly_max_images`, shared with Post Studio and spread evenly over its days, or the
  page's own `premium_per_day` used up, or the count could not be asked): use `routine fallback <dir>`
  and `verify` that card. That is the safe card, never an error.
- Without an upload URL (the connector has no `get_upload_url`), `krea-request <dir>` still works: the
  photo goes by its link and the layout is described in words.
- Run `krea-request` again for the same card only if its output was lost: it asks for the same
  image, not a second one. Each `krea-request` reserves one image of the shared allowance, and
  `krea-result` tells the allowance it was made (a reservation never made stops counting after 45
  minutes). If `krea-result` prints `the shared count was not told this image was made`, carry on
  with the card: the Health tab says so.
- One of the page's templates (`template_spec`): fill its `slots` as usual; the layout image is that
  template drawn with them, so the premium card keeps its structure. `REFUSED: ... does not fit` (from
  `krea-request` or `fallback`): a slot is too long for its zone, shorten it.
- `art_mood` in `copy.json` (a few words of scene/mood: "rainy night game", "courtroom, cold blue
  light") and `design_notes` reach the premium prompt too. Never people, text or logos there.

## 4b. Look at the card yourself (mandatory)

Open `<dir>/card.jpg` next to the chosen source photo. `verify` already fails any card where a
text box touches a detected face/head, or where the source photo shows a face that is no longer
visible on the card. You must still confirm by eye:
- EVERY face (athlete, coach, celebrity, anyone) is 100% clear: no text, stamp, logo, banner or
  graphic touches the face, hair or helmet - including faces the detector missed (helmets,
  visors, profiles, small faces)
- no person appears who is not in the source photo; faces are not altered or invented
- no league/team/brand logos were ADDED (ones on real uniforms in the photo are fine)
- the layout reads as the page's own style; nothing garbled, clipped or badly cropped
- the worn (distressed) headline lettering still reads clearly at a glance. `verify` checks the
  spelling on `card_clean.jpg`, the same card without the wear, so legibility is yours to judge
- for every `card warning` printed by `verify`, decide whether that text is genuinely part of
  the photo (jersey, sponsor, sign) or was invented; invented text = FAIL (retry/fallback)

Write `<dir>/verdict.json`:
`{"pass": true|false, "issues": [...], "warnings_checked": ["<warning>: <your finding>", ...]}`

## 5. Upload

```bash
python -m bore.cli routine upload <dir>
```

Uploads the card, caption, sources and your check to the dashboard as a pending draft, and records
the story so later runs don't pick it again (backups included). Upload refuses a story that repeats
a post from the last 4 days (see step 2), and refuses unless lint passed, `verify` passed on the current card, every verify warning is
addressed in `warnings_checked`, and verdict.json (written after the current card) says pass. If it prints
UPLOAD NOT SENT (the dashboard did not answer), nothing was sent: run the same command again a minute later.

## 6. Save state and finish

```bash
python -m bore.cli routine check               # must print "check: OK"; otherwise go back to step 2
python -m bore.cli routine state --action push
python -m bore.cli health
```

If `check` prints NOT DONE, work on the briefs it names and run it again: an editor's LINK or REGEN
is made and uploaded, or, only when it cannot be made at all, reported back with `routine link-fail` /
`routine regen-fail <dir> --reason "..."` (never skipped); a backup is written + uploaded, or skipped
with a reason. Only save state once it passes. Include its per-page lines in the summary.
If the push prints STATE NOT SAVED (the database was not restored, or lost more than half of the
recent rows the pull brought), the dashboard keeps its copy: report it and stop. Never retry it with
`--fresh`: that is for the product's owner, after checking the database.

## 7. Studio story feed

The Studio's topic list is made by its own routine ("Bore topic feed", TOPICS.md) from the
stories this run collected and saved in step 6. Nothing to do here.

## 8. Studio jobs

If time allows, process waiting Studio jobs: follow STUDIO.md from `studio jobs` (skip its
setup; `studio sync` already ran). Editors are waiting for these.

End with a short summary: regenerations and links done (and any regen-fail / link-fail with its
reason); per page, what
was posted to the dashboard, what was skipped and why,
the budget lines printed by `prepare`, and any health warnings.
