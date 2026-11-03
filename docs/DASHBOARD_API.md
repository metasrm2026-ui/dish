# The dashboard's API (Dish)

What the screens and the routines rely on: the records, who may see what, and every route the
product added to Bore's (docs/PRODUCT.md is the specification). Every route is `/api/<route>`,
served by `dashboard/api/main.js`.

## Open access

The owner's instruction for now: "nothing to sign up, no password: click the link and view".

- `OPEN_ACCESS` is `"1"` for this deployment (`dashboard/lib/defaults.js`). A request with no session
  is then the product owner **Owner** (role `super`, its own workspace `studio-owner`), made on first
  use. The dashboard opens straight in: no sign-in and no sign-up screen (`GET auth/me` answers
  `open_access: true`).
- Everything else is unchanged: workspaces, roles and every safety rule apply to Owner as to anyone.
  Someone who signs in (a tester, an editor) is themselves. Routine calls still need `ROUTINE_SECRET`.
- **To turn it off**: set `OPEN_ACCESS=0` in the Vercel project's environment variables and redeploy.
  Sign-in then works as before. To sign in as Owner afterwards, send its account a set-password link
  first (Admin > Users > Reset link on `owner@studio.invalid`, or `OPEN_ACCESS_EMAIL` if it was set).
- `node dev/server.mjs` runs with it off (the seeded people sign in); `OPEN_ACCESS=1 node dev/server.mjs`
  opens as Owner.

## Roles and workspaces (`lib/tenancy.js`)

- `users/<id>`: `{email, name, role: super | owner | editor, workspace, pages: [slug] (an editor's),
  active, must_change, open_access?}`. The first bootstrap user (`POST routine/bootstrap`) is `super`
  (`workspace: null`). Users made before roles were renamed (`admin`) are `super`.
- `workspaces/<id>`: `{id, name, limits: {posts_per_day, premium_per_day}, owner_id, owner_email, created_at}`.
- A super sees every workspace. An owner sees their workspace's pages; an editor the pages of their
  workspace they were given. Another workspace's page, draft, batch, job or editor is "no such ..."
  (404) whatever its id; one of their own workspace's they were not given is 403.
- Ids that belong to a page start with its slug: drafts `<page>-<YYYYMMDDHHMM>-<x>`, jobs, batches and
  links `<page>-<ms>-<x>`. A slug never holds 8 digits in a row.

## Pages (`pages/<slug>`, `lib/pages.js`)

`{slug, name, workspace, status: draft | going_live | live | paused | failed, fb_page_url, niche,
language, timezone, posts_per_day, premium_per_day, limits (the super's), socialpilot_connected {at, by}
(the owner's tick), socialpilot_login_id (set only by the onboarding routine), sources, playbook, docs,
onboarding {started_at, job, steps: [{step, ok, detail, at}], error, done_at}, paused_by, archived}`.

Budgets: what the routines work with is the lowest of the page's value, the page's limit and the
workspace's limit (defaults 10 posts and 6 premium cards a day; at most 40). The super sets limits; an
owner may lower a page's budgets, never raise them above the limits.

## Routes for people

| Route | Who | Does |
|---|---|---|
| `GET auth/me` | anyone signed in | `{user, pages: [{slug, name, status, workspace, can_manage}], workspace, open_access}` |
| `GET pages` | all | the pages the person may see (editors: name and status only), `doc_types`, `steps` |
| `POST pages` | owner, super | `{name, fb_page_url?, niche?, language?, timezone?}` a new page in `draft` (the super: `{workspace}`, else their own) |
| `GET pages/<slug>` | owner, super (editors: name and status) | the page, `missing` (what Go live still needs) |
| `PATCH pages/<slug>` | owner, super | settings, budgets, `playbook`, `socialpilot_connected: true` (the tick); never `socialpilot_login_id` |
| `* pages/<slug>/docs/<templates\|visual\|tone>[/start\|part\|finish]` | owner, super | the reference documents, uploaded in parts as in Admin > Pages |
| `* pages/<slug>/sources`, `lessons`, `sheet` | owner, super | as in Admin > Pages |
| `POST pages/<slug>/go-live` | owner, super | checks (Facebook address, niche, templates and tone documents, the tick), `going_live`, an `onboard/<job>` record, fires ONBOARD (`ONBOARD_FIRE_URL` / `ONBOARD_FIRE_TOKEN`), audited. 400 `{error, missing: [field]}`; 409 when not draft/failed |
| `POST pages/<slug>/retry` | owner, super | Try again after `failed` (the same checks, a new job) |
| `POST pages/<slug>/pause` / `resume` | owner, super | `live` ⇄ `paused`; a page the super paused only the super resumes |
| `GET pages/<slug>/onboarding` | anyone who may see the page | `{status, steps, expected: [{step, label, ok?, detail?, at?}], error, job}` for the live progress view |
| `GET workspace` | owner (super: `?workspace=`) | the workspace, its pages and its people |
| `POST workspace/editors` | owner | `{email, name, pages}` an editor's invite: `{user, setup_link}` |
| `PATCH workspace/editors/<id>` | owner | `{pages?, pages_add?, pages_remove?, active?, name?}` |
| `POST workspace/editors/<id>/reset` | owner | a new set-password link |
| `GET workspace/audit?days=` | owner | the workspace's own audit entries |
| `POST admin/testers` | super | `{email, name, workspace_name, posts_per_day?, premium_per_day?}`: the workspace and its owner's invite `{workspace, user, setup_link}` |
| `GET admin/workspaces`, `PATCH admin/workspaces/<id>` | super | `{name?, limits: {posts_per_day?, premium_per_day?}}` |
| `POST admin/users` | super | an invite `{email, role, workspace?, pages?}` (no workspace: the one of the pages given) |

A paused page's Studio is read only; a page that is not live has no Studio changes, no posts made, none
approved and none scheduled.

## Routes for the routines (`Bearer ROUTINE_SECRET`)

- `GET routine/config`: `{pages: [{slug, name, workspace, status, fb_page_url, niche, language, timezone,
  posts_per_day, premium_per_day, limits, socialpilot_login_id ('' when on the deny list, then
  socialpilot_blocked: true), onboarding, sources, playbook, docs, templates, ...}], deny}`. The hourly,
  topics and publisher routines work only pages whose `status` is `live` (the first run of a page going
  live works it too).
- `GET routine/onboard/jobs`: `{jobs: [{id, page: {slug, name, workspace, fb_page_url, niche, language,
  timezone, posts_per_day, premium_per_day, docs, sources, playbook, socialpilot_login_id}, status,
  stale}], used: [{page, socialpilot_login_id}], deny}`: waiting jobs, and running ones quiet for 30 min.
- `POST routine/onboard/jobs/<id>/claim {session}`: one session per job (409 for the others).
- `POST .../step {step, ok, detail}`: a step's report (`link_account`, `read_documents`,
  `research_sources`, `write_playbook`, `queue_templates`, `first_run`).
- `POST .../result {sources?, playbook?, socialpilot_login_id?, deny?}`: 409 for an account on the deny
  list (`config/socialpilot.yaml` `blocked_login_ids`, `SOCIALPILOT_BLOCKED_IDS`, or the `deny` sent)
  or one another page uses.
- `POST .../done`: the page is `live` (409 without its own account linked). `POST .../fail {error}`:
  `failed` with the reason the owner reads.
- `POST routine/krea {who, cap, page, key?, ...}`: also the page's `premium_per_day` per UTC day:
  refused with `KREA PACE: n/m images today for <page>`; a page that is not live: `KREA PACE: <page> is
  paused ...`. The monthly cap stays product-wide (`KREA CAP`). Reports (`report_only`) and `made` need
  no page.
- `GET queue`: also `waiting: [{id, page, why}]`, approved posts of pages that cannot publish now.
  `POST result {status: publishing}` is refused for them. `socialpilot_login_id` is only ever a live
  page's own, never one on the deny list.
