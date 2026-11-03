# Onboarding routine: instructions for the Claude session

A tester pressed **Go live on Facebook** for one of their pages. The dashboard set the page to
`going_live`, wrote an onboard job and started you. Your job: take that page from its three
reference documents to its first drafts on the dashboard, step by step, and report each step as
you go: the tester watches the steps appear on the page's screen. At the end the page is `live`
(the hourly routine makes its posts from then on) or `failed` with a reason the tester can act on
(they fix it and press Try again).

Nothing is published from here. Every post you make goes to the dashboard as a pending draft; an
editor approves it.

Every `onboard` command checks what you give it before it reaches the dashboard,
and reports its own step when it works. A command that prints `REFUSED: ...` saved nothing: fix
what it says and run it again (or, when the fix is the tester's, fail the job with its words). One
that prints `STOPPED: ...` means the dashboard stopped this onboarding (the tester paused the page,
or another session has it): do nothing more for this page and finish with one line saying so.

## 0. Setup

```bash
bash scripts/ensure-deps.sh                        # packages, only if missing or not loading (python -m pip, never bare pip)
python -m bore.cli onboard jobs                    # claims one waiting job, syncs the documents, writes the brief
```

If it prints `no onboard jobs waiting`, you are done: finish with that line. Otherwise it prints a
`JOB` line with the page's slug and `work/onboard/<slug>/brief.json`. Read the brief: the page
(`page`: name, Facebook address, niche, language, time zone, posts per day), its documents (`docs`:
the text files, and for the templates and visual references documents a picture of each page),
the files you will write (`files`), the source budget (`source_limits`), what a source file and a
playbook look like (`sources_example`, `playbook_keys`, `playbook_example`).

The documents are the tester's own and confidential: use them, never copy them into step
details, failure reasons, summaries or anywhere outside this session's files.

If `onboard jobs` prints `DOCS MISSING`, the templates or tone document is not there: fail the
job with the line it prints (step 7) and stop.

If `ensure-deps.sh` ends with "still not working", report its lines and stop: the first run cannot
check photos or draw cards on this machine. A problem with this machine (a package, a crashed
command, a network error) is never the tester's: never fail their page for it. Retry, or report it
in the summary and stop without failing the job (the dashboard offers it again).

Secrets come from the environment: `DASHBOARD_URL`, `ROUTINE_SECRET`, `APIFY_TOKEN`.

## 1. Link the scheduler account

The tester connected the page to SocialPilot (the connect instructions are on the page's screen).
Find that account:

1. Call the SocialPilot connector's **AccountList** (read-only) and save its result, exactly as it
   came back, to the brief's `files.accounts` (`work/onboard/<slug>/accounts.json`).
2. In it, find the **Facebook** account whose name is the page's name (`page.name`; case, spaces
   and punctuation aside) or whose address is the page's own (`page.fb_page_url`). There must be
   exactly one.
3. Save it:

```bash
python -m bore.cli onboard link <slug> --login-id <its loginId> --name "<its name as SocialPilot lists it>" --accounts work/onboard/<slug>/accounts.json
```

`link` finds the match itself and refuses anything but exactly one Facebook account that is the
page's own: never an account on the product's deny list (`config/socialpilot.yaml`), never one
another page already uses, never one SocialPilot flags as needing reconnecting. What it refuses
with is what to tell the tester:
- `NO MATCH`: fail with "connect <page name> in SocialPilot (instructions on the page), then press Try again".
- `SEVERAL MATCHES`: fail with "more than one SocialPilot account looks like <page name>: disconnect
  the extra one in SocialPilot, then press Try again".
- `DENIED` or `IN USE`: fail with "the SocialPilot account that looks like <page name> cannot be used
  for it: connect the page's own Facebook page in SocialPilot, then press Try again".
- `needs reconnecting` / `is locked`: fail with "reconnect <page name> in SocialPilot, then press Try again".
Never pass another account's loginId to get past it.
AccountList is the only SocialPilot call this routine makes.

## 2. Read the documents and the page's own posts

Read every document in `docs`:
- `docs.tone.file` (caption language and tone): the page's voice, how captions are built (first
  line, length, emoji, the closing question, hashtags), words it uses and never uses.
- `docs.templates.file` and its page pictures (`docs.templates.images`): the page's post templates;
  the zone diagrams are pictures, so open them.
- `docs.visual.file` and its pictures, when there is one: the page's look.

Then open the page's own recent posts (`page.fb_page_url`, with your web tools): what it posts,
how often, the captions word for word, the kind of photo. A page you cannot read (private, no
posts yet) is not a failure: the documents are enough; say so in the step's detail.

```bash
python -m bore.cli onboard step <slug> documents --ok --detail "read the templates, tone and visual references documents and the page's recent posts"
```

A document with no text you can read (a scanned image without text, an empty file) is the
tester's to fix: fail with "the <type> document has no text we can read: upload it as a PDF with
text, then press Try again".

## 3. Research the sources

The hourly routine finds stories in the sources you choose now: X accounts, subreddits, web
searches and websites. Research them with web search, for this page's niche (`page.niche`) and
language:
- the niche's main outlets (their websites, the section that covers the niche) and their X accounts;
- the reporters who break this niche's news on X (their own accounts, not fan accounts);
- the official accounts (the league, the club, the network, the brand) on X;
- the subreddits where the niche's fans share the news;
- two to four web searches that bring the day's coverage ("<niche> news today").

A source must be **active this month**: open it and note the date of its latest post. A source
that has not posted for a month, a fan account that only reposts, a parody account or a subreddit
that is mostly memes does not go in. Stay within `source_limits` (the page's budget: each source
costs collection every hour); fewer, better sources beat many. Then report the research and write
`files.sources` (`work/onboard/<slug>/sources.json`) in the shape of `sources_example`:
`x` (`handle`, `role`: reporter, official, outlet or other, `last_post` as YYYY-MM-DD, `why`),
`subreddits` (`name`, `last_post`, `why`), `web_searches` (`query`, `why`), `websites` (`url`,
`last_post`, `why`).

```bash
python -m bore.cli onboard step <slug> research --ok --detail "<how many of each you found>"
python -m bore.cli onboard sources <slug> work/onboard/<slug>/sources.json
```

`sources` refuses a handle or name that is not one, a source quiet for over a month, a duplicate,
an X or Reddit address under websites, and more than the budget allows: fix the file and run it
again. Reporters go in with `role: reporter` (they can break a story alone); league, club and
outlet accounts as `official` or `outlet` (authoritative, but they never scoop).

## 4. Write the playbook

The playbook is how the hourly routine and Post Studio write for this page (ROUTINE.md reads it at
every post). Write it from the tone document (the voice and the caption rules), the templates
document (which card for which story), the page's own posts (the examples, the kinds of story it
posts) and your research (the niche, what to leave out). Use only the keys in `playbook_keys`, in
the shape of `playbook_example` (`config/playbooks/example.yaml`, made-up data):
- `name`, `niche`, `audience`;
- `worth_posting`: the kinds of story to post and not to post, and "score 0-10 for niche fit";
- `voice`: a few sentences; `caption_rules`: one rule per line, as the tone document says them;
- `examples`: three to six of the page's own recent captions, word for word (the page's own words
  are not the confidential documents);
- `lint.hashtags` (`min`, `max`, `required`) and `lint.closing_question` only when the tone
  document or the page's posts are that consistent;
- `card.templates`: the generic layouts that suit the page (the brief's layouts: headline, quote,
  stat_card, comparison, then_vs_now, ranked_list, timeline); the page's own templates come from its
  templates document in step 5, not from here;
- `prefilter.skip` for stories the page never posts (patterns), only when the documents say so.
Facts only: never write a rule or an example the documents and the page's posts do not support.

```bash
python -m bore.cli onboard playbook <slug> work/onboard/<slug>/playbook.yaml
```

It refuses keys the engine does not read, missing required ones (`niche`, `worth_posting`, `voice`,
`caption_rules`), patterns that are not valid and layouts the renderer does not draw: fix and run
it again.

## 5. Queue the template conversion

```bash
python -m bore.cli onboard templates <slug>
```

The templates document is turned into templates the renderer draws by a Studio job (STUDIO.md
TEMPLATES), as when a templates document is uploaded. The editors approve each one on the
Templates screen; until then posts use the generic layouts. Nothing else to do here.

## 6. First run

```bash
python -m bore.cli onboard first-run <slug>
```

It runs ROUTINE.md step 1 (`routine prepare`) for this page only, on a fresh database:
it never pulls or pushes the hourly routine's saved state (that routine may be running now). It can take up
to 10 minutes: run it with a 10-minute command timeout. Then follow ROUTINE.md steps 2 to 5 for
each brief it printed (decide, write, lint, Krea or the template renderer, look at the card,
upload), exactly as the hourly routine does, with every rule there. Then
`python -m bore.cli routine check` (ROUTINE.md step 6, first line only). Never run `routine state`
(it refuses anyway) and skip ROUTINE.md steps 6 to 8 otherwise. Outside the page's active hours
`prepare` gives quota 0: that is fine, the hourly routine makes the first posts when they start.

## 7. Finish

```bash
python -m bore.cli onboard done <slug>
```

It refuses until the link, sources, playbook, templates and first run steps are done. The page is
then `live`: the hourly routine posts for it every hour, within its budget.

When a step cannot be done for a reason the tester can fix, fail the page:

```bash
python -m bore.cli onboard fail <slug> "<what happened, and what to do, then press Try again>"
```

The tester reads the reason under the step and presses Try again after fixing it, so say what to
do in plain words: "connect Demo Page in SocialPilot (instructions on the page), then press Try
again", "the tone document has no text we can read: upload it as a PDF with text, then press Try
again", "no active news source covers this niche: describe the niche in more words, then press Try
again". Never a code, a command or a stack trace. A problem with this machine is never a reason
(step 0).

End with a short summary: the page's slug, each step and its result, how many sources of each
kind, how many first drafts were uploaded, and the reason when it failed.
