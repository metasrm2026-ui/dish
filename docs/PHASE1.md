# Post Studio, Phase 1: technical summary

## Stack and hosting

| Part | What | Why |
|---|---|---|
| Web app + API | Vercel (Node, one serverless function `dashboard/api/main.js`, static UI in `dashboard/public/`) | Already hosting the approval dashboard; no servers to run |
| Storage | Vercel Blob, every record and reference file **encrypted with AES-256-GCM** before it is stored (`DATA_KEY`) | Private storage; Blob never sees plaintext |
| Login | Email + password (scrypt), signed session cookie, 2 h idle expiry (12 h max), 5 failed sign-ins lock for 15 min, one-time reset links | Section 4.1 |
| AI generation | Claude **routines** (no API key): the hourly post routine, the Studio jobs routine, the publisher | Owner's choice: everything that can run in a routine does |
| AI check | A separate, fresh Claude subagent with web search, given only the finished image, caption and current time | Independent reviewer (see deviations) |
| Image composition | Template renderer (HTML to 1080x1350 JPEG) for exact text and real photos; Krea 2 Turbo paints page-style backdrops for the pages' own designs | Faces and text are never AI-redrawn |
| Images | The story's own photos and the photos of the articles its posts link to; the picture other outlets used (Bing News + DuckDuckGo news, same-picture groups counted by outlet, `bore/consensus.py`); stills of the story's video from the posts its article embeds (`bore/stills.py`); Bing / Wikimedia search for an editor's query. Every candidate is checked (OCR: graphics, screenshots, scoreboards; duplicates; size) and shows its source | Free; same in the Studio and the hourly routine |
| News collection | Websites from their own RSS feeds (`bore/feeds.py`, free, hourly; Google `site:` search only for a site without a feed); Apify collectors for X, Reddit and web searches | Every article of a site, at no cost; the rest already budgeted |
| Publishing | SocialPilot through the publisher routine's SocialPilot connector | Existing, never posts directly |

## How the workflow runs

1. **Topics**: every hour the post routine collects each page's sources, clusters near-duplicate
   stories across sources, ranks them for the page, and publishes a 24-hour feed (top 20) with a
   headline, summary, two visual ideas and candidate photos for the top 12. Topic selection and
   ideation in the Studio are therefore instant. Pasted links are read by the server immediately.
2. **Jobs**: ideas for pasted links, image searches, composing (visual + caption + hashtags) and
   the post check are queued as jobs. The Studio jobs routine (hourly at :50) and the hourly post
   routine (at :05) process them; the page polls and shows progress. With an API trigger token
   for the Studio routine (`STUDIO_FIRE_URL` / `STUDIO_FIRE_TOKEN` in Vercel) jobs start
   within a minute instead.
3. **Composition**: the idea names the template from the page's templates document and a layout
   the renderer draws: the page's own designs, or the generic layouts for any page (headline,
   quote, stat card, comparison, then vs now, ranked list, timeline) in the page's colours and
   logo text. Every card passes lint (facts and numbers only from the story, page caption rules)
   and verify (OCR spelling, faces visible and clear of text) before an editor sees it.
4. **Posting**: Send creates an approved draft; the publisher routine schedules it in SocialPilot
   at the editor's time (default: 1 hour after the latest queued post, then +45 min each).

## Asset protection

- Pages, sources, playbooks and reference PDFs live only in the encrypted store; the repository
  has dummy examples. Routines pull them into a gitignored `private/` folder per run.
- Editors never see source lists or documents; admins upload/replace/roll back but cannot
  download. The UI never receives prompts or document text.
- Audit log: sign-ins, admin changes, document uploads/rollbacks, source views, every routine
  read of config or documents, posts sent. Usage log: every routine job, image search and Krea
  image, and SocialPilot result, with page and user.
- Secrets: Vercel environment (sensitive, write-only): `DATA_KEY`, `SESSION_SECRET`,
  `ROUTINE_SECRET`, `BLOB_READ_WRITE_TOKEN`; `RESEND_API_KEY` + `MAIL_FROM` for reset emails.

## Running cost (estimate, per month)

| | 40 posts/day (5 pages x 8) | 400 posts/day (20 x 20) |
|---|---|---|
| Vercel (Pro seat, Blob, functions) | ~$20-25 | ~$25-40 |
| Claude routines | within the Claude plan (about 50 routine runs/day) | plan limits become the constraint: more runs per hour or an API key (~$0.05-0.15 per post with Sonnet) |
| Krea art (page designs only, ~15 credits/image) | $50 plan (20,000 credits, ~1,300 images) | ~180,000 credits: higher Krea tier, or generic layouts (no Krea cost) |
| Apify collection | existing $16 budget (2 pages); ~$40 for 5 pages | ~$150 for 20 pages |
| Image search | $0 | $0 (Google Custom Search would be ~$5 per 1,000 queries if Bing results get worse) |
| SocialPilot | existing plan | plan with 20 accounts |

## Deviations from the brief

- **Checker model**: the reviewer is a separate Claude session (fresh context, web search, no
  knowledge of how the post was made), not a different provider. The owner chose Claude routines
  with no API keys.
- **Keys**: encryption keys are Vercel sensitive environment variables in the owner's Vercel
  account rather than a separate key-management service.
- **Post now** schedules the post for the earliest allowed slot, 1 hour out (the owner's rule:
  never post directly). Lower `STUDIO_MIN_LEAD_MINUTES` (Vercel) and
  `scheduling.studio_min_lead_minutes` (config/socialpilot.yaml) to shorten it.
- **Gap** between scheduled posts is 45 minutes (owner's setting), not 1 hour.
- **Password reset emails** need a Resend key; until then the admin panel issues reset links.
- **Reference PDFs** up to 3 MB each (text is extracted for the routines; the PDF itself is
  stored for the routine to read).
- The repository's git history still contains the old page config from before this change.
- The brief's last heading ("AI watermark workaround") is not built: removing or hiding marks
  that identify AI-generated images is not something this system will do.

## Local development

`cd dashboard && npm test` (API) and `node dev/server.mjs` (whole UI on an in-memory store with
dummy users and pages). `python -m pytest` for the routines; tests that need a page's private
config skip unless `python -m bore.cli studio sync` has run.
