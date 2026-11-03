# Metomate: the plug-and-play product

A separate product built from the Bore engine. Anyone the owner invites can connect a Facebook page,
upload the page's three reference documents (post templates, visual references, caption language and
tone), press **Go live on Facebook**, and from then on the system makes production hourly posts for
that page, offers Post Studio, and schedules what their editors approve. It has **nothing to do with
the owner's own pages**: its own repository, Vercel project, storage, sign-ins, routines and
budgets. No page name, account, source, playbook or document from any other deployment is in this
repository, and none may ever be added.

## Roles

| Role | Who | Can |
|---|---|---|
| `super` | the product owner | everything: invite testers, set budgets, pause any page, health, audit |
| `owner` | a tester (one workspace each) | their workspace: add pages, upload documents, go live / pause, invite editors, settings, everything editors can |
| `editor` | invited by an owner | approve/reject posts and use Studio for the workspace's pages they were given |

A **workspace** is one tester's account: its pages, its editors. Every list, count, note, link,
document, job, template, draft and health message is filtered by workspace on the server. A
`super` sees all workspaces. Sign-in is by invite only: the super creates a tester invite (a
set-password link), the tester creates their editors' invites.

## A page's life

`draft` → (Go live) → `going_live` → `live` ⇄ `paused`; `going_live` → `failed` (with a reason the
tester can act on, and Try again).

A page record (`pages/<slug>`, slug unique across the product) carries, besides what Bore's page has:
`workspace`, `status`, `fb_page_url`, `niche` (a sentence), `language`, `timezone`, `posts_per_day`,
`premium_per_day`, `socialpilot_login_id` (set **only** by the onboarding routine), `sources`
(proposed by the routine, editable), `playbook` (written by the routine from the documents and the
page's own posts, editable), `onboarding` `{started_at, steps: [{step, ok, detail, at}], error}`.

### Go live (the red button)

Allowed when the page has a Facebook page URL, a niche, the **templates** and **tone** documents
(visual references optional) and the owner ticked "I have connected this page in SocialPilot"
(the connect instructions are on the page). The dashboard sets `going_live`, writes an
`onboard` job and fires the **onboarding routine** through its API trigger (as Bore fires Studio
jobs: `lib/fire.js`). The screen shows each step live as the routine reports it.

### The onboarding routine (ONBOARD.md)

1. Claim the waiting onboard job (`onboard jobs`), report each step (`onboard step`).
2. **Link the scheduler account**: SocialPilot `AccountList`; find the Facebook page whose name or
   URL matches the page (exactly one match; never an account on the deny list, never one another
   page already uses). Save `socialpilot_login_id`. No match: fail the step with "connect <page> in
   SocialPilot (instructions on the page), then press Try again".
3. **Read the documents** (`studio sync` brings them) and the page's recent posts (its Facebook URL),
   then **research sources**: X accounts, subreddits, web searches that cover the niche, within the
   page's budget. Save them (`onboard sources`).
4. **Write the playbook** (voice, what to post and not, caption rules, card rules) from the tone and
   templates documents and the research (`onboard playbook`).
5. **Queue template conversion** (a `templates` Studio job, as Bore does on upload); the editors
   approve them on the Templates screen.
6. **First run**: run the hourly flow for this page only, so the first drafts appear within the hour.
7. `onboard done`: the page is `live`. Any failure: `onboard fail` with the reason.

## Budgets (enforced in code, not prompts)

- Per page: `posts_per_day` (default 10) and `premium_per_day` (default 6). The workspace and page
  defaults are set by the super; an owner may lower them, never raise them.
- Product-wide monthly premium cap `krea.monthly_max_images` in config (default 150 cards ≈ 3,300
  credits), checked atomically in the dashboard as Bore's shared allowance is, **plus** the page's
  `premium_per_day`. Over either: the safe card (template renderer), never an error.
- Apify: the product's own monthly budget in config (`budget.monthly_usd`, default 5).

## Safety rules (in code)

All of Bore's rules stay (nothing posts without human approval, scheduled ≥1 h after approval and
45 min apart per page, the editor's photo, watermarks never removed, facts only, confidential
documents). In addition:
- The publisher only ever uses the `socialpilot_login_id` of a **live** page of a workspace, set by
  the onboarding routine. Accounts in `config/socialpilot.yaml` `deny` are refused everywhere
  (the owner's own pages and personal accounts).
- A paused page: no hourly runs, no publishing, Studio read-only.

## The look

White background, dark green text (`--ink: #0f3d2e`), a calm green accent, generous spacing, clear
cards, readable on a phone. The Go live button is red. Plain words everywhere (Bore's editor-safety
rules in full: never lose typed text, one request per tap, plain errors, reload notice).

## Routines (Claude routines on the product's repository)

| Routine | Fires | Does |
|---|---|---|
| hourly | cron :15 | posts for every live page within its budget |
| publisher | cron :40 and API | schedules approved posts |
| topics | cron :50 | the Studio story feed |
| studio jobs | API (the dashboard) | Studio jobs and template conversions |
| onboarding | API (Go live) | the steps above |

Prompts live in `routines/*.prompt.md`, settings in `routines/triggers.yaml`.
