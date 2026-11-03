# Dish

Dish is a plug-and-play product built from the Bore engine. Anyone the product's owner
invites (a **tester**) connects a Facebook page, uploads the page's three reference documents (post
templates, visual references, caption language and tone) and presses **Go live on Facebook**. From
then on the system makes production posts for that page every hour, offers Post Studio to its
editors, and schedules what the editors approve. Nothing is ever posted without a person's approval.

It has nothing to do with any other deployment of the engine: its own repository
(`metasrm2026-ui/dish`, branch `main`), Vercel project (`dish`), storage, sign-ins,
routines and budgets. No page, account, source, playbook or document of another deployment is in
this repository, and none may ever be added. The specification is `docs/PRODUCT.md`.

## Who does what

| Role | Who | Can |
|---|---|---|
| `super` | the product's owner | everything: invite testers, set budgets, pause any page, health, audit |
| `owner` | a tester (one workspace each) | their workspace: add pages, upload documents, go live / pause, invite editors, settings |
| `editor` | invited by an owner | approve or reject posts and use Studio for the pages they were given |

Sign-in is by invite only (a set-password link). Every list, document, job, draft and health message
is filtered by workspace on the server.

## A page's life

`draft` -> Go live -> `going_live` -> `live` <-> `paused`; `going_live` -> `failed` (with a reason the
tester can act on, and Try again). Go live needs the page's Facebook address, a niche, the templates
and tone documents, and the tester's tick that the page is connected in SocialPilot. The dashboard
then writes an onboard job and fires the onboarding routine (ONBOARD.md), which reports each step on
the page's screen:

1. claim the job (`onboard jobs`);
2. link the page's SocialPilot account (`onboard link`: exactly one Facebook account matching the
   page; never one on the deny list in `config/socialpilot.yaml`, never one another page uses);
3. read the documents and the page's own posts, research sources (`onboard sources`: X accounts,
   subreddits, web searches, websites, each active this month, within the page's source budget);
4. write the playbook (`onboard playbook`, checked against what the engine reads);
5. queue the template conversion (`onboard templates`: a Studio templates job the editors approve);
6. the first run, this page only, on a fresh database (`onboard first-run`, then ROUTINE.md 2-5);
7. `onboard done` (the page is `live`) or `onboard fail <reason>`.

A paused page gets no hourly posts and no publishing, and its Studio is read-only.

## The five routines

Claude routines (claude.ai/code scheduled tasks) on this repository, in the product's own cloud
environment. Prompts in `routines/<name>.prompt.md`, settings in `routines/triggers.yaml`, checked by
`tests/test_prompts.py` (no secret values, the hard rules word for word, every command parses).

| Routine | Fires | Instructions | Connectors | Does |
|---|---|---|---|---|
| hourly | cron :15 | ROUTINE.md | Krea | posts for every live page, within its budget, to the approval queue |
| publisher | cron :40 and API (`ROUTINE_FIRE_*`) | PUBLISH.md | SocialPilot | schedules approved posts (never posts directly) |
| topics | cron :50 | TOPICS.md | none | the Studio story feed |
| studio jobs | API (`STUDIO_FIRE_*`) | STUDIO.md | Krea | Studio jobs and template conversions |
| onboarding | API (`ONBOARD_FIRE_*`) | ONBOARD.md | SocialPilot (AccountList only) | a page's Go live, step by step |

Every routine reads `DASHBOARD_URL`, `ROUTINE_SECRET` and (hourly, onboarding) `APIFY_TOKEN` from the
environment's secrets; a prompt that does not find them stops and says so. No prompt carries a
value. Each page's own rules come from its playbook and documents, never from these files.

To set a routine up: create the trigger in claude.ai/code on this repository and branch, with the
model and connectors of `routines/triggers.yaml`, paste its prompt, and write its id into
`triggers.yaml`. For the API-fired ones, put the trigger's fire URL and token into Vercel as
`<FIRE>_URL` / `<FIRE>_TOKEN` (e.g. `ONBOARD_FIRE_URL`, `ONBOARD_FIRE_TOKEN`) and deploy.

## Setup, once

1. **Vercel**: `VERCEL_TOKEN=... DASHBOARD_ALIAS=<the address> SUPER_EMAIL=<you> bash scripts/setup-vercel.sh`
   (`VERCEL_SCOPE` for a team). It creates the project `dish`, a Blob store, generates
   `DATA_KEY`, `SESSION_SECRET` and `ROUTINE_SECRET` (never replacing one the project already has;
   the values go to `dashboard/.vercel/studio-secrets.env` only), deploys, creates the super user and
   prints its set-password link. Run again any time: it only adds what is missing.
2. **The routines' environment**: secrets `DASHBOARD_URL` (`https://<the address>`), `ROUTINE_SECRET`
   (from the file above) and `APIFY_TOKEN` (the product's own Apify account); the setup script lines
   under "Faster routine sessions".
3. **The five triggers** (above), then their fire URLs and tokens in Vercel, then deploy again.
4. **The deny list**: `config/socialpilot.yaml` `deny`, the SocialPilot accounts this product must
   never use (other deployments' pages, personal accounts), whatever a page record says.
5. **Health**: repository secret `STUDIO_ROUTINE_SECRET` (the dashboard's `ROUTINE_SECRET`) and variable
   `STUDIO_DASHBOARD_URL` for `.github/workflows/health.yml`.
6. Sign in as the super user, set the budgets, invite the first tester.

## Deploy order

Commit, then `DASHBOARD_ALIAS=<the address> bash dashboard/deploy.sh` (it refuses uncommitted changes in
`dashboard/` and a folder linked to any project but `dish`, runs the tests, deploys the commit
and fails unless the address then serves it), then push the Python that needs the new API: every
push to `main` is what the routines run next. Raise `API_LEVEL` in `deploy.sh` when the Python
starts depending on a new endpoint.

## Budgets (enforced in code, not prompts)

- **Per page**: `posts_per_day` (default 10) and `premium_per_day` (default 6). The super sets the
  workspace and page defaults; an owner may lower them, never raise them.
- **Premium cards**: one product-wide monthly cap, `krea.monthly_max_images` in `config/config.yaml`
  (default 150 cards, about 3,300 credits), counted atomically by the dashboard for the hourly routine
  and Studio, plus each page's `premium_per_day`. Over either, the card is the safe one (the template
  renderer), never an error.
- **Apify**: the product's own monthly budget, `budget.monthly_usd` (default 5), paced per day.
- **Sources**: at most 30 X accounts, 6 subreddits, 4 web searches and 10 websites per page
  (`bore/onboard.py`; `onboard.source_limits` in config may set lower ones).

## Safety rules (in code, with tests)

Nothing posts without human approval; posts are scheduled at least 1 hour after approval and 45
minutes apart per page; Studio cards are built on the editor's photo; watermarks are never removed;
facts only; reference documents stay confidential. The publisher only uses the
`socialpilot_login_id` the onboarding routine set on a live page, and never an account on the deny
list. A paused page: no hourly runs, no publishing, Studio read-only.

## The onboarding API (dashboard, routine secret)

| Route | Body | Answer |
|---|---|---|
| `GET /api/routine/onboard/jobs` | | `{jobs: [{id, page, ...}]}`, oldest first |
| `POST /api/routine/onboard/claim` | `{id, session}` | `{job, page, used_login_ids}`; 409 when another session has it |
| `POST /api/routine/onboard/step` | `{id, page, step, ok, detail}` | the page's `onboarding` record |
| `POST /api/routine/onboard/result` | `{id, page}` plus one of `{socialpilot_login_id, socialpilot_name}`, `{sources, pipeline}`, `{playbook}`, `{templates: true}` | `{ok}`; templates: `{studio_job}` |
| `POST /api/routine/onboard/done` | `{id, page}` | `{status: "live"}` |
| `POST /api/routine/onboard/fail` | `{id, page, reason, step}` | `{status: "failed"}` |

Every POST answers 409 when the job is no longer this session's or the page is no longer
`going_live` (the tester paused it): the command prints `STOPPED` and the session stops.
`POST /api/routine/bootstrap` `{email, name}` creates the super user while no user exists.

## Development

```bash
bash scripts/ensure-deps.sh                     # the Python packages, only if missing or not loading
python -m pytest -q tests                       # the engine and the routines' commands
(cd dashboard && npm ci && node --test test/*.test.mjs)
node dashboard/dev/server.mjs                   # the whole dashboard on made-up data, nothing persisted
```

Tests use local fakes only: nothing in them reaches Vercel, SocialPilot, Krea or Apify.

## Faster routine sessions

Every routine run (all five routines) starts a fresh cloud session.
To have the packages ready before a session even starts, add these lines to the cloud environment's
**Setup script** (claude.ai/code: the environment menu, then Edit):

```bash
pins=$([ -f constraints.txt ] && echo "-c constraints.txt" || true)
python3 -m pip install -q --disable-pip-version-check --prefer-binary $pins requests PyYAML Pillow numpy opencv-python rapidocr-onnxruntime playwright==1.56.0 openpyxl reportlab pypdfium2
python3 -c "from PIL import Image" 2>/dev/null || python3 -m pip install -q --disable-pip-version-check --prefer-binary $pins --ignore-installed Pillow
python3 -m playwright install --only-shell chromium || true
```

`scripts/ensure-deps.sh` then finds them working and skips the install. The second line matters:
the machines carry a system Pillow built for another Python, which pip counts as installed but
which does not load (`cannot import name '_imaging'`), so the first line skips it; this installs a
working copy ahead of it. The first line pins the versions the tests passed with (`constraints.txt`,
when the script runs in the repository folder; elsewhere it installs the newest, and every session's
`scripts/ensure-deps.sh` then prints `packages: not the tested version: ...` for each package that
differs: when a run log shows that, make the script run in the repository folder). The last line
installs the browser cards are drawn in when the machine lacks it (it does nothing when it is there).
Without these lines,
`scripts/ensure-deps.sh` does the same in each session (bore/deps.py checks that every package
actually loads and that the machine can save an image, read text and start the browser).

## Operations

Problems used to be found by editors. Now the code reports every run, the dashboard works out a
health status from what it already stores, and an hourly check outside the routines tells the owner.

**Run reports** (`bore/ops.py`). Every `python -m bore.cli` command posts one record to
`POST /api/routine/runs`: routine, command, start and end, `ok`, `degraded` and a summary (per page
quota, candidates and skips for `routine prepare`; quota, uploads and open editor requests for
`routine check`; to schedule, held, originals to delete in SocialPilot and pages without an account
for `publish plan`; jobs taken, left or handed back for `studio jobs`; stories, freshness, written and
unchecked counts per page for `studio feed-publish`; health warnings without source names). Long
commands also report their start, so a session that dies shows as a start without an end. `ok` comes
from how the command ended (a crash or an unexpected exit is a failure; a gate saying BLOCKED, NOT
DONE, REFUSED or NOT FOUND is not, nor is a publisher SKIP or HELD, `publish preflight`'s included; a
publisher STOP and STATE NOT SAVED are), and a catch-all that carried on with less calls `ops.degraded(...)`: the run is amber.
A broken machine (a package or model that does not load) is re-raised by the photo catch-alls and
recorded first (`ops.fault`), so the run is amber even where a caller swallows the error. A developer's or
diagnostic session sets `BORE_ROUTINE=manual`: its runs are not the pipeline. Nothing is guessed from
the checkout (a guess once labelled routine sessions `manual` and hid their failures). When the dashboard does not answer, the next
reports wait 1-2 s and after a second miss none is sent for 10 minutes (`work/.ops-session.json`).
The model never reports with curl; `GET /api/routine/runs?hours=24` lists the reports (diagnostic
sessions read this, not many records at once).

**Health status** (`dashboard/routes/health.js`): `GET /api/health` (signed-in users; the review page
shows it as a banner, loaded apart from the queue) and `GET /api/routine/health` (routine secret), kept
5 minutes and worked out by one request at a time in the deployment (the others get the last status
meanwhile). "In a row" counts routine runs (sessions), each by its last run of the command. Reasons:

| Code | Level | Means | Do |
|---|---|---|---|
| `hourly_stale` | red | no state save and no draft for 2 h | open the hourly routine's last session |
| `state_not_saved` | amber | drafts made, state not saved for 2 h | its last step fails: check `routine state --action push` |
| `topics_stale` | amber, red after 3 h | a page's Studio story list is over 90 min old | the topic routine's last session |
| `publisher_unclaimed` | red | approved over 90 min ago, never claimed | is the publisher trigger running? |
| `publisher_stuck` | red | "publishing" for 45 min | check SocialPilot's queue before anyone retries |
| `publisher_unconfirmed` | amber | scheduled, 3 h past its time, not confirmed | check the page on Facebook |
| `publisher_held` | amber, red after 4 h | waited over 2 h for SocialPilot (a SocialPilot outage shows here, not as failed runs) | SocialPilot's banner says why; held posts fail 12 h after approval |
| `publisher_needs_person` | red, amber when a person only has to look | the publisher's notes for a person: red for what goes out unless someone fixes it (a copy on the wrong account, a second copy, the wrong time, a changed caption, anything a publisher session recorded itself); amber for one SocialPilot's queue did not show, one to look for on Facebook, and the latest `publish plan`'s outdated originals and pages with no account | the review page lists them: fix each in SocialPilot (or link the page's account again: its owner presses Try again on Go live), then mark it Done |
| `studio_queued`, `studio_stuck` | red | a Studio job waiting 10 min, running 25 min | the Studio routine's sessions |
| `studio_fire_failed` | amber | a job still waiting, in its first hour, could not wake the Studio routine | the Health tab says what the trigger answered: `STUDIO_FIRE_URL` / `STUDIO_FIRE_TOKEN` in Vercel, then deploy |
| `link_waiting`, `regen_waiting` | amber, red after 6 h | an editor's link or regeneration waiting 2 h | the hourly routine's last session |
| `run_failed` | amber, red 2 runs in a row | a command crashed or exited with an error | its run report (`ids` = run ids) |
| `run_degraded`, `run_degraded_repeat` | amber, red 3 runs in a row | a command ran with less (the reasons say what) | the reason: often a package (`bash scripts/ensure-deps.sh`) |
| `run_unfinished` | amber | a long command started 45 min ago never ended | its session died |
| `runlog_failed` | amber | a failed step in the old trigger prompts' run log | until the repo prompts replace them |
| `watchdog_silent` | amber | the hourly health check has not run for 2 h | GitHub Actions, workflow `health` |
| `*_unreadable`, `storage_unreadable` | amber, red | the dashboard could not read its storage (red at once when storage refuses reads, a security check or a rate limit: the status is then not worked out again for 2 min, and never shown green; a read that failed through its retries is amber, red from 3 checks) | Vercel Blob / Firewall logs |
| `dashboard_unreachable` | red | (the GitHub issue only) the health workflow got no status; its ids say what each call answered | `HTTP 401`: `STUDIO_ROUTINE_SECRET` is not the dashboard's secret; else Vercel |

**Every push to `main`, and every pull request, is tested** (`.github/workflows/test.yml`:
pytest on Python 3.11 with the dashboard's packages, `npm test`): a red mark within minutes. Other
branches are tested through their pull request (the private repository's free Actions minutes also run
the hourly health check). **Hourly health check** (`.github/workflows/health.yml`): restarts or fails stalled
Studio jobs (`POST /api/routine/watchdog`, the same logic as an editor's open page), judges the
health status the watchdog works out then (`GET /api/routine/health` only when it gave none), and keeps one
issue "Dish health: RED" open while something is broken (red at once, amber after 3 checks; green
closes it). GitHub emails it to the repository owner. It lists codes and ids only. Owner, once:
repository Settings > Secrets and variables > Actions > secret `STUDIO_ROUTINE_SECRET` (the dashboard's
`ROUTINE_SECRET`) and variable `STUDIO_DASHBOARD_URL` (its address); GitHub > Settings > Notifications: email for issues.

**Deploying the dashboard** (`bash dashboard/deploy.sh`, `VERCEL_TOKEN` and `DASHBOARD_ALIAS` set): commit first (it refuses
uncommitted changes in `dashboard/`), it runs the tests, deploys that commit with a pinned Vercel CLI
and `public/version.json` `{commit, api}`, and fails unless the public address then serves that
commit. A review page or Studio tab left open notices the new commit (`version.json`, at most once
a minute), stops acting and asks the editor to reload (unsent edits on the review page are kept in
that tab). Order: commit, deploy, then push the Python that needs the new API. Raise `API_LEVEL` in
`deploy.sh` when the Python starts depending on a new endpoint.

**Krea allowance.** The month's Krea images (`krea.monthly_max_images`) are one allowance for the
hourly routine and Post Studio, kept by the dashboard (`/api/routine/krea`, record `krea/<YYYY-MM>`):
every image is reserved there before it is made (`krea-request`), and `krea-result` says it was
made; a reservation never said to be made stops counting after 45 minutes (one without `confirms`,
from a Studio session still on the Python from before `krea-result` said so, counts as made, so the
Python can be pushed after the deploy). The count is `base` (the images the hourly routine's ledger
had at its first report of the month, made before any reservation) plus `studio_base` (Studio's
images before a reset) plus every reservation kept. The hourly routine's own ledger (sent at every `routine
prepare` and `krea-request`, for the UTC month and day it was counted in, and only from a database
the session restored) is a floor: `hourly_used` and `hourly_today` keep the most it reported, so
images it made while the dashboard did not answer still count. A routine older than this count
(its ledger without the month) is refused and counts on its own ledger. Studio uses Krea only once
the hourly routine has counted the month (its first run of the month). When the dashboard cannot
be asked, Studio uses the template card and the hourly routine its own ledger plus Studio's last
known count (this month's, and today's against today's share); the run is amber on the
Health tab only. A count that is wrong (it only moves up within the month) is reset between hourly
runs by saving a new version of the record: from `dashboard/`, a one-off `node -e` that imports
`lib/db.js` and calls `putRecord('krea', '<YYYY-MM>', {month: '<YYYY-MM>', base: <h>, studio_base:
<s>, base_today: {}, hourly_used: 0, hourly_today: {}, seq: 0, reported: true, res: []})` with the
production env loaded, `<h>` being the hourly routine's ledger (the record's `hourly_used`) and `<s>`
every other Krea image of the month so far (Krea's own usage page minus `<h>`), so that images the
hourly routine makes while the dashboard does not answer still count at once. Old run reports are pruned a few at a
time by the reports themselves and whole days by the hourly watchdog (a week is kept).

**Rolling back the dashboard.** The address (`DASHBOARD_ALIAS`) is moved by `deploy.sh` itself
(`vercel alias set`); Vercel's own rollback button moves only the project's domains, so it may leave
this address on the new deployment. Before deploying, `deploy.sh` prints where it points
(`deploy: <address> pointed at https://<previous>.vercel.app ...`): keep that line (or find the
previous production deployment in Vercel > Deployments). To roll back:
1. `npx -y vercel@65.0.0 alias set https://<previous>.vercel.app <address> --token $VERCEL_TOKEN`
2. Check it: `https://<address>/version.json` serves the previous commit.
3. Revert the push too, if the Python on `main` needs the newer API: an older dashboard answers 400
   to the newer CLI's requests. `git revert <commits> && git push`: every push is live for the next
   routine run.
Then fix forward: commit, `bash dashboard/deploy.sh`, push.

**Trigger prompts** live in `routines/` (`<name>.prompt.md` and `triggers.yaml`: id, schedule, model,
connectors) with variable names, never values; `tests/test_prompts.py` checks them (no secret values,
no line telling the routine to skip a topic, the hard rules word for word, every command parses). To change a
trigger: change the file, then paste it into the trigger (after `DASHBOARD_URL`, `ROUTINE_SECRET` and `APIFY_TOKEN` are in
the environment's secrets: a prompt finding them missing stops and says so). **Rotating the routine
secret**: set the new value as `ROUTINE_SECRET_NEXT` in Vercel and deploy (both values work), move the
routines' environment secrets and the repository secret `STUDIO_ROUTINE_SECRET` (GitHub Actions) to it,
then make it `ROUTINE_SECRET`, remove `ROUTINE_SECRET_NEXT` and deploy.

## The engine

```
bore routine prepare     X + Reddit + web via Apify (budget-paced), verify, pick, sharp candidate photos,
                         one brief per pick (ROUTINE.md)
Claude (the session)     niche judgement, caption + card copy from the page's playbook and documents
Krea connector           the premium card from the real photo, within the shared allowance
Claude (the session)     checks the card: exact text, no invented people, no added logos
                         -> one retry -> template renderer (bore/cards.py) as fallback
bore routine upload      pending draft on the dashboard; an editor approves it
publisher routine        schedules it in SocialPilot (PUBLISH.md)
```

A story is **one real-world event that several independent sources are discussing inside the
window**. A single popular post is not a story.

```
bore/onboard.py    the onboarding routine's steps (ONBOARD.md)
bore/routine.py    briefs, the Krea requests, the hourly routine's steps (ROUTINE.md)
bore/studio.py     Post Studio: sync, the topic feed, jobs (STUDIO.md, TOPICS.md)
bore/publish.py    the publisher's plan and checks (PUBLISH.md)
bore/collect.py    collection, routing, gap backfill, field mapping
bore/cluster.py    clustering rules (see module docstring)
bore/select.py     scoring, corroboration and scoop routes, no-repeat, watchlist, escalation
bore/guard.py      lint and verify: the copy against the sources, the card against the copy
bore/apify.py      Apify client: budget guard, partial-result salvage
bore/ops.py        run reports
config/config.yaml all tunables and the actor field mappings (candidate paths, first match wins)
```

**Settled findings** (from live runs; each is encoded in config or code):
- X actor `xquik/x-tweet-scraper`: `mode: search` with `from:a OR from:b` batches under 460
  characters; bound the window with `since_time`, never `within_time`; exclude replies, keep reposts
  (they are how corroboration shows up).
- Reddit actor `fatihtahta/reddit-scraper-search-fast`: sort by `hot` (`new` returns score 0);
  `maxPosts` applies per subreddit; 900 s timeout, and a timed-out run's dataset is salvaged (it is
  billed anyway).
- A run refuses to start past 85% of the Apify account's monthly ceiling.
- Clustering: the most similar member, then an average similarity of at least 0.55 x the
  threshold; at least 2 shared tokens; subreddit scaffolding threads filtered.
- A story qualifies with 2 or more distinct sources, or one tier-one **reporter** whose engagement
  clears the threshold; accounts posting more than 25 times in a window never take the scoop route.
- Freshness: a hard 24 h age limit at clustering and again at render. No repeats for 96 h.
