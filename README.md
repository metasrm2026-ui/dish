# Bore: sports trend sourcing for Facebook pages

Every hour, Bore collects posts from X and Reddit (via Apify) plus web-search results for a set of
Facebook pages. Twice a day it groups that window into **trends** and verifies them. It then
renders one PDF briefing that covers every page.

A trend is **one real-world event that several independent sources are discussing inside the
window**. A single popular post is not a trend.

## Pipeline

```
Hourly Claude routine (ROUTINE.md)
  bore routine prepare     X + Reddit + web via Apify (budget-paced), verify, pick (10/page/day),
                           sharp candidate photos, one brief per pick
  Claude (the session)     niche judgement, caption + card copy from config/playbooks/<page>.yaml
  Krea connector           infographic from the real photo + page style references
                           (plan compute units; hard cap 600 images/month)
  Claude (the session)     checks the image: exact text, no invented people, no added logos
                           -> one retry -> template renderer (bore/cards.py) as fallback
  bore routine upload      pending draft on the dashboard
Dashboard (dashboard/, Vercel)
  Okay / Not okay          -> /api/publish (API-triggered) -> SocialPilot (webhook / API)
```

See `docs/samples/` for posts produced by this flow.

Dashboard: https://bore-dashboard.vercel.app (open, no password, by the owner's choice).
Redeploy with `VERCEL_TOKEN=... dashboard/deploy.sh`. Vercel project env: `ROUTINE_SECRET`,
`BLOB_READ_WRITE_TOKEN` (Blob store `bore-drafts`), `PUBLISH_MODE` (dry_run | webhook),
`PUBLISH_WEBHOOK_URL`, `SOCIALPILOT_ACCOUNTS`; optional `DASHBOARD_PASSWORD` turns a login back on.

## Setup

Data sources are **X, Reddit and web search, all through Apify**. No Facebook scraper is used.

```bash
bash scripts/ensure-deps.sh                # python -m pip install -r requirements.txt, only if missing or not loading
cp .env.example .env                       # APIFY_TOKEN (gitignored)
python -m bore.cli check                   # token, budget, and the three actors resolve
python -m bore.cli add-page https://www.facebook.com/<page>      # share links are accepted
python -m bore.cli sources --page page-1 --league nfl --team Eagles   # assume sources
python -m bore.cli dna --page page-1 --file export.csv           # page DNA (see below)
python -m pytest -q
```

**Sources** are assumed from `config/source_catalog.yaml`:
- national insiders as `tier_one`
- league feeds as `officials`
- league subreddits
- league and team web searches

`bore health` flags any handle that goes silent. Edit `config/pages.yaml` freely.
A workbook can still be imported with `import-workbook`.

**Source tiers** (for large source sheets such as Culture Rush's `Resource_Sheet.xlsx`, kept in full
in `config/sources/culture-rush-sheet.yaml` with every exclusion and its reason):
- `x_handles`: hourly, every run
- `x_handles_secondary`: one rotating batch every `schedule.x_secondary_every_hours`, capped at
  `apify.x.secondary_max_items` tweets
- `subreddits` every Reddit pass, plus `apify.reddit.rotation_per_run` from `subreddits_rotation`
- `web_searches` plus `collection.web_rotation_per_run` from `web_rotation` (site: searches)
- `priority_terms_file`: names that add `scoring.weights.priority` per match (max 2) to the ranking
  score only; verification routes are unchanged

**Page DNA** comes from the page's own history, without scraping. Use either:
- `--file`: a Meta Business Suite export (Insights → Content → Export, CSV)
- `--source graph`: the Graph API with `FB_PAGE_TOKEN_<SLUG>` in `.env`. Publishing will need this token anyway.

If a page's DNA exists, `bore dna` also detects its leagues and teams from the posts.

**Network:** the environment must allow `api.apify.com`, and `graph.facebook.com` for the Graph route.

Scheduling: see `crontab.example`.

## Commands

| command | what it does |
|---|---|
| `add-page URL` | Registers a page. Share links are accepted. |
| `sources --page P --league nfl --team Eagles` | Assumes X, Reddit and web sources from the catalog. |
| `check` | Verifies the Apify token, the budget, and that the X, Reddit and web actors resolve. |
| `import-workbook X.xlsx` | Parses one sheet per page into `config/pages.yaml`. Keeps hand edits on re-import. Lists cells it could not parse. |
| `dna --page P --file export.csv` or `--source graph` | Builds page DNA and writes `config/dna/<page>/dna.{json,md}`. |
| `collect [--x --reddit --web]` | Runs one global actor run per source and routes items to pages. With no flags it follows the configured cadence. |
| `refresh` | Re-measures engagement into the overlay. Counts never decrease. |
| `cluster [--page]` | Clusters and selects, then writes JSON that includes an **escalation block** for thin pages. |
| `escalate` | Runs the required web searches for pages with no trends. |
| `brief [--dry-run --offline]` | Runs refresh, cluster, escalate and re-cluster, renders the PDF, and records what was sent. |
| `health` | Exits 1 on data loss. Warns on silent handles, pages with no web items, and an empty overlay. |
| `capture-fixture --page P --out f.json` | Dumps a real window for hand-labelling. |
| `tune fixture.json` | Sweeps the join threshold. Use the tightest value that passes. |

## Layout

```
bore/sources.py    league/team detection -> assumed sources (config/source_catalog.yaml)
bore/workbook.py   workbook parsing (x.com URL -> first path segment = handle)
bore/dna.py        page DNA: slicing, format cross-tabs, acronym-safe caps metric
bore/apify.py      Apify client: budget guard, partial-result salvage
bore/collect.py    global collection, routing, gap backfill, field mapping
bore/cluster.py    clustering rules (see module docstring)
bore/select.py     scoring, corroboration and scoop routes, no-repeat, watchlist, escalation
bore/refresh.py    engagement overlay
bore/render.py     PDF with a second freshness gate
bore/health.py     health checks
bore/fixture.py    labelled-fixture evaluation and threshold sweep
config/config.yaml all tunables and the actor field mappings (candidate paths, first match wins)
```

## Settled findings (do not re-derive)

These came from live runs that cost real money. Each one is encoded in config or code.

- **X actor** `xquik/x-tweet-scraper`
  - Use `mode: search` with `from:a OR from:b` batches under 460 characters. `profileTweets` returned 1 tweet where search returned 40.
  - Bound the window with `since_time`, never `within_time`: with `within_time`, 96% of the tweets fell outside the window.
  - Exclude replies. Keep reposts, because they are how corroboration shows up.
- **Reddit actor** `fatihtahta/reddit-scraper-search-fast`
  - Sort by `hot`: `new` returns score 0 on every post.
  - `maxPosts` applies per subreddit.
  - Use a 900 s timeout. On failure, salvage the dataset, because a timed-out run is still billed.
  - Run it every Nth hour.
- **Budget guard:** a run refuses to start past 85% of the account's monthly ceiling.
- **Clustering**
  - Match against the most similar member, then require average similarity of at least 0.55 × the threshold.
  - Require at least 2 shared tokens.
  - Use the stronger of plain and IDF-weighted similarity.
  - Keep 3-character tokens.
  - Filter subreddit scaffolding threads.
- **Routes:** a trend qualifies with 2 or more distinct sources, or with one tier-one **reporter** whose engagement clears the threshold. Accounts that post more than 25 times in a window can never take the scoop route.
- **Freshness:** a hard 24 h age limit is applied at clustering and again at render.
- **No repeats:** trend keys contain no date. The index is per page and matches exactly or fuzzily. Suppression lasts 96 h. A story comes back only on 2× engagement, +2 sources, or a sharp score rise.
- **Re-measure engagement before scoring.** Capture-time counts rank breaking news lowest.
- **Page DNA:** cross-tabulate every caption device against post format. Exclude sports acronyms from the caps metric.

## Status and known gaps

- Every live API input and output shape is in `config/config.yaml` and has not yet been checked against a real run. This covers the Reddit `startUrls`/`maxPosts` keys, the web-search actor, the Business Suite export columns, and the field paths. Run each command once and correct the config if a field comes back empty.
- The web-search actor defaults to `apify/google-search-scraper`. Swap it in config if you use a different provider.
- `join_threshold: 0.70` is **provisional**. It was tuned on `tests/fixtures/regression.json`, a *synthetic* fixture that encodes the known failure modes. Replace it with a real labelled window (`capture-fixture`, label it, then `tune`) and commit that window.

## Images and posting (spec §9): status

Built: image step (Krea + template fallback), caption playbooks, approval dashboard, publish
adapter (dry run / webhook to Zapier-SocialPilot / SocialPilot API placeholder), daily cap.
Pending: dashboard deployment, SocialPilot connection details, the scheduled routine.

Original spec notes:

1. An image step. Either generate a text-free visual and overlay the numbers afterwards, or render HTML to an image for anything built around numbers.
2. Publishing through the Meta Graph API photo endpoint with a multipart upload of the raw bytes.
3. A caption playbook per page, derived from `config/dna/<page>`.
4. A posting cadence with a daily cap, a minimum gap, quiet hours, and a dry-run mode.

Nothing may reach a page without passing the verification routes and the freshness gate.

## Faster routine sessions

Every routine run (hourly posts, topic feed, publisher, Studio jobs) starts a fresh cloud session.
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
the checkout (a guess labelled routine sessions `manual` on Oct 22-23 and hid their failures). When the dashboard does not answer, the next
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
| `publisher_needs_person` | red, amber when a person only has to look | the publisher's notes for a person: red for what goes out unless someone fixes it (a copy on the wrong account, a second copy, the wrong time, a changed caption, anything a publisher session recorded itself); amber for one SocialPilot's queue did not show, one to look for on Facebook, and the latest `publish plan`'s outdated originals and pages with no account | the review page lists them: fix each in SocialPilot (or set the page's account in admin), then mark it Done |
| `studio_queued`, `studio_stuck` | red | a Studio job waiting 10 min, running 25 min | the Studio routine's sessions |
| `studio_fire_failed` | amber | a job still waiting, in its first hour, could not wake the Studio routine | the admin Health tab says what the trigger answered: `STUDIO_FIRE_URL` / `STUDIO_FIRE_TOKEN` in Vercel, then deploy |
| `link_waiting`, `regen_waiting` | amber, red after 6 h | an editor's link or regeneration waiting 2 h | the hourly routine's last session |
| `run_failed` | amber, red 2 runs in a row | a command crashed or exited with an error | its run report (`ids` = run ids) |
| `run_degraded`, `run_degraded_repeat` | amber, red 3 runs in a row | a command ran with less (the reasons say what) | the reason: often a package (`bash scripts/ensure-deps.sh`) |
| `run_unfinished` | amber | a long command started 45 min ago never ended | its session died |
| `runlog_failed` | amber | a failed step in the old trigger prompts' run log | until the repo prompts replace them |
| `watchdog_silent` | amber | the hourly health check has not run for 2 h | GitHub Actions, workflow `health` |
| `*_unreadable`, `storage_unreadable` | amber, red | the dashboard could not read its storage (red at once when storage refuses reads, a security check or a rate limit: the status is then not worked out again for 2 min, and never shown green; a read that failed through its retries is amber, red from 3 checks) | Vercel Blob / Firewall logs |
| `dashboard_unreachable` | red | (the GitHub issue only) the health workflow got no status; its ids say what each call answered | `HTTP 401`: `BORE_ROUTINE_SECRET` is not the dashboard's secret; else Vercel |

**Every push to the routines' branch, and every pull request, is tested** (`.github/workflows/test.yml`:
pytest on Python 3.11 with the dashboard's packages, `npm test`): a red mark within minutes. Other
branches are tested through their pull request (the private repository's free Actions minutes also run
the hourly health check). **Hourly health check** (`.github/workflows/health.yml`): restarts or fails stalled
Studio jobs (`POST /api/routine/watchdog`, the same logic as an editor's open page), judges the
health status the watchdog works out then (`GET /api/routine/health` only when it gave none), and keeps one
issue "Bore health: RED" open while something is broken (red at once, amber after 3 checks; green
closes it). GitHub emails it to the repository owner. It lists codes and ids only. Owner, once:
repository Settings > Secrets and variables > Actions > secret `BORE_ROUTINE_SECRET` (the dashboard's
`ROUTINE_SECRET`); GitHub > Settings > Notifications: email for issues.

**Deploying the dashboard** (`bash dashboard/deploy.sh`, `VERCEL_TOKEN` set): commit first (it refuses
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
known count (this month's, and today's against today's share); the run is amber on the admin's
Health tab only. A count that is wrong (it only moves up within the month) is reset between hourly
runs by saving a new version of the record: from `dashboard/`, a one-off `node -e` that imports
`lib/db.js` and calls `putRecord('krea', '<YYYY-MM>', {month: '<YYYY-MM>', base: <h>, studio_base:
<s>, base_today: {}, hourly_used: 0, hourly_today: {}, seq: 0, reported: true, res: []})` with the
production env loaded, `<h>` being the hourly routine's ledger (the record's `hourly_used`) and `<s>`
every other Krea image of the month so far (Krea's own usage page minus `<h>`), so that images the
hourly routine makes while the dashboard does not answer still count at once. Old run reports are pruned a few at a
time by the reports themselves and whole days by the hourly watchdog (a week is kept).

**Rolling back the dashboard.** The address `bore-dashboard.vercel.app` is moved by `deploy.sh`
itself (`vercel alias set`); Vercel's own rollback button moves only the project's domains, so it may
leave this address on the new deployment. Before deploying, `deploy.sh` prints where it points
(`deploy: bore-dashboard.vercel.app pointed at https://<previous>.vercel.app ...`): keep that line
(or find the previous production deployment in Vercel > Deployments). To roll back:
1. `npx -y vercel@65.0.0 alias set https://<previous>.vercel.app bore-dashboard.vercel.app --token $VERCEL_TOKEN`
2. Check it: `https://bore-dashboard.vercel.app/version.json` serves the previous commit, or answers
   404 for a deployment older than version.json.
3. Revert the push too, if the Python on the routines' branch needs the newer API: an older dashboard
   answers 400 to the newer CLI's requests (e.g. the publisher's `missing` / `needs_action`, which it
   then leaves undone). `git revert <commits> && git push` on the routines' branch: every push is live
   for the next routine run.
Then fix forward: commit, `bash dashboard/deploy.sh`, push.

**Trigger prompts** live in `routines/` (`<name>.prompt.md` and `triggers.yaml`: id, schedule, model,
connectors) with variable names, never values; `tests/test_prompts.py` checks them (no secret values,
no line telling the routine to skip a topic, the hard rules word for word, every command parses). To change a
trigger: change the file, then paste it into the trigger (after `ROUTINE_SECRET` and `APIFY_TOKEN` are in
the environment's secrets: a prompt finding them missing stops and says so). **Rotating the routine
secret**: set the new value as `ROUTINE_SECRET_NEXT` in Vercel and deploy (both values work), move the
routines' environment secrets and the repository secret `BORE_ROUTINE_SECRET` (GitHub Actions) to it,
then make it `ROUTINE_SECRET`, remove `ROUTINE_SECRET_NEXT` and deploy.
