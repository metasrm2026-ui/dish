# SocialPilot publisher routine: instructions for the Claude session

You schedule posts that a human already approved on the dashboard. You never decide what to
post, never edit captions or images, never publish anything outside the approved queue, and
**never post directly**: every post is scheduled.

Scheduling rules (computed for you by `bore publish plan`, do not change them):
- hourly-routine posts: at least 1 hour after the post was approved (and after now), and at
  least 45 minutes between any two posts on the same page, including posts already scheduled;
  and, only when `audience_hours` is set in config/socialpilot.yaml (off since Oct 13: posts go
  out at any hour), inside those hours: a post approved outside them is held for the next morning
  (`held_for_audience_hours` in the plan). This is expected.
- Post Studio posts (`studio: true`): the time the editor chose when they pressed Send, but
  never sooner than 1 hour from now. "Post now", or a chosen time that has passed (the post
  waited or was retried), = the earliest slot 45 minutes from the page's other posts. Still
  scheduled (shareType 3).
- times are in SocialPilot's account timezone (Asia/Kolkata), format "YYYY-MM-DD HH:mm"

## 0. Setup

```bash
bash scripts/ensure-deps.sh publish   # requests + PyYAML only, if missing or not loading (python -m pip, never bare pip)
```

`DASHBOARD_URL` and `ROUTINE_SECRET` must be set (environment or `.env`).
Scheduling uses the **SocialPilot connector** tools in this session.
Accounts: `config/socialpilot.yaml` (Culture Rush = loginId 2662938, Huddle Room = loginId
2662936) or, for pages added in the admin panel, the account set there; `publish plan` gives
each item its `socialpilot_account`. Use only that loginId. Never use "Keaton Caleb" (2662937).

Saving a connector result: some commands below read what a SocialPilot call returned. Save the
result to the file named there exactly as it came back (do not edit or shorten it), e.g.
`cat > work/queued.json <<'EOF'`, the result, then `EOF` on its own line. If a command says it
found no result in the file, save it again, exactly as it came back.

What the commands' exit codes mean (all expected, none is a crash):
- 3 `SKIP`: leave that draft (another run has it, its time needs a new plan, the Studio sent it
  again as a new draft, or it now waits for a later run); go to the next item.
- 4 `HELD`: SocialPilot cannot take posts now. Stop scheduling in this run; the drafts wait.
- 5 `STOP`: a post did not come out as planned; it is recorded for a person. Stop scheduling in this run.

## 1. Check that SocialPilot answers

Before anything else, call the connector's **AccountList** and **UserInfo** (read-only), save
their results to `work/accounts.json` and `work/userinfo.json`, then:

```bash
python -m bore.cli publish preflight --result work/accounts.json --result work/userinfo.json
```

It prints `OK`: go on (a `NOTE` about one account is for the summary only). It prints `HELD`
(exit 4): SocialPilot is unavailable and the command already held the approved drafts; go to
step 6. If a call itself fails, try it once more. If it fails again, or the SocialPilot tools are
not available at all, run `python -m bore.cli publish preflight --error "<the error, or 'SocialPilot connector not attached to the routine'>"`
(it holds the drafts and prints `HELD`), then go to step 6.
No draft can be claimed in a run that has not passed this step.

## 2. Check what SocialPilot already has queued

For each page account, call the connector's **QueuedPosts** (filter by that account, today and
tomorrow). Note every queued time on that account that is NOT one of ours in
`already_scheduled` (step 3) - e.g. posts scheduled by hand.

## 3. Plan the slots

```bash
python -m bore.cli publish plan [--busy "huddle=YYYY-MM-DD HH:MM" ...]
```

Pass one `--busy` per extra queued time found in step 2 (the page slug,
time as shown by SocialPilot). The output lists `to_schedule` items, each with `id`, `page`,
`caption`, `image_urls` (the post's images in order; one for most posts), `socialpilot_account.loginId`,
`schedule_local` and `schedule_utc`. If `to_schedule` is empty, go to step 5. (`publish plan`
also records what it settles, below; `publish queue` prints the same plan and changes nothing.)

`waiting_for_socialpilot`: drafts held at a recent try (SocialPilot refused everything, their
page's account kept failing, or a lookup failed). They are not in `to_schedule`: they wait, and a
run about once an hour tries again. Do nothing with them; list them in the summary ("waiting for
SocialPilot: <id> (tries)"). `socialpilot_down` is shown only when SocialPilot itself refused
everything (account locked or unpaid, connector missing).

`superseded` lists approved drafts whose newer update (`update`) is already queued or scheduled:
never schedule them. Mark each one
`python -m bore.cli publish mark <id> --status failed --error "superseded by its update <update id>"`.

The plan settles two cases itself; only list them in the summary: `no_account` (drafts for a page
with no usable SocialPilot account: they are moved to Failed with what to fix) and
`scheduled_but_superseded` (an outdated original already in SocialPilot: the dashboard asks a
person to delete it there).

## 4. Schedule each item, in the listed order

Several publisher runs can be going at the same time (every approval starts one). A draft is
yours only after you claim it:

1. `python -m bore.cli publish mark <id> --status publishing --scheduled-for "<schedule_utc>"`
   If it prints `SKIP` (exit code 3), do NOT schedule it, go to the next item.
2. Duplicate guard: call **QueuedPosts** for that account with `q` = the first 40 characters of
   the caption. If a post with the same caption is already queued, do NOT create another: go to
   4.4 with that post. If the item's `check_existing` is true (it went to SocialPilot before, or
   waited for it), also look it up before creating it:
   - `prior_post_id` given: **ViewPost** with it.
   - **DeliveredPosts** for that account, `q` as above, startDate = `search_from`.
   - Delivered (or ViewPost shows it went out): `python -m bore.cli publish mark <id> --status posted --url "<post URL if shown>"`.
     Never create it again.
   - ViewPost shows it still scheduled: go to 4.4 with that post. Never create it again.
   - ViewPost shows it errored or unscheduled: that copy never goes out by itself, so it does not
     count (an editor retried the post, or it waited for SocialPilot); 4.4 records it for a person
     to delete. ViewPost answering that there is no such post (or refusing the id) is "not found
     there", not an error.
   - A lookup call itself fails (an error, a timeout): do NOT create it;
     `python -m bore.cli publish mark <id> --status held --error "lookup failed: <the error>"`.
     It prints `SKIP`: only this draft waits for a later run. Go on with the next item.
   - Not queued and not delivered (every lookup answered): go on with 4.3.
3. Call **CreatePost** with:
   - `type`: `"image"`
   - `loginIds`: `[<socialpilot_account.loginId>]` (one account only)
   - `image`: `{"images": <image_urls, all of them in the given order>, "postDescription": "<caption exactly as given>"}`
     (a post "as is" can have several images: they go out together as one post)
   - `shareType`: `3` (Schedule)
   - `scheduleDateTime`: `["<schedule_local>"]`
   Keep the caption character for character: line breaks, emoji, hashtags. Never use
   shareType 1 (Share Now) or 2 (Share Next). Call CreatePost exactly once per claimed draft.
4. Call **QueuedPosts** with `q` = the first 40 characters of the caption and no account filter
   (so a copy on another account shows too), save the result to `work/queued.json`, then:
   `python -m bore.cli publish mark <id> --status scheduled --post-id "<CreatePost's post id>" --queued work/queued.json`
   The command checks the queued post against the claim (account, time, caption, one copy) and
   records SocialPilot's own time. `STOP` (exit 5): something is off; it is recorded for a person:
   stop scheduling in this run and go to step 5. `NEEDS ACTION` (exit 0): recorded for a person;
   go on. `NOT FOUND`: do what it says (one more QueuedPosts call, then the same command). Never
   mark a draft failed after CreatePost worked.
5. If CreatePost failed: `python -m bore.cli publish mark <id> --status failed --error "<the connector's error>"`
   with SocialPilot's error word for word. If the error does not say why (e.g. "Error uploading
   media"), first call **AccountList** once: if that call itself fails saying the account is
   locked (SocialPilot's whole-workspace lock), report that message as the error instead. What
   the command prints:
   - `HELD` (exit 4): SocialPilot or the workspace refused everything (account locked,
     subscription or trial ended, connector not attached), or this session was not allowed to
     make the call. The draft waits. **Stop scheduling** in this run and go to step 5.
   - `SKIP` (exit 3): this page's account failed before (in this run, or in any run in the last
     hour), or SocialPilot flags it as needing reconnecting. The draft waits; the other pages are
     not affected. Go on with the next item.
   - otherwise it failed just that draft: go on with the next item.
   A waiting draft is tried again by a run about an hour later; one that waited more than 12 hours
   after approval moves to Failed for an editor.

When the list is done, run `publish plan` again: drafts approved while you worked appear now.
Repeat until `to_schedule` is empty.

`interrupted` lists drafts a run claimed over 30 minutes ago without reporting back. For each,
look in QueuedPosts / DeliveredPosts for its caption: found in QueuedPosts -> save that result to
`work/queued.json` and mark it `scheduled` with `--queued work/queued.json`; delivered -> mark it
`posted`; not found -> mark it `failed --error "publisher run interrupted"`.
Never create it again yourself.

Schedule each draft at most once. If a call times out and you cannot tell whether it went
through, search QueuedPosts for the caption before retrying.

## 5. Confirm delivered posts

For each item in `already_scheduled` whose `schedule_local` is more than 15 minutes in the past:
first, if it has `sp_post_id`, call **ViewPost** with it. Otherwise (or if ViewPost does not say)
look it up for that account (`account` = its `socialpilot_account.loginId`, `q` = its
`caption_start`, startDate/endDate = its `day`) in the connector's **DeliveredPosts**, then
**ErrorPosts**, then **UnscheduledPosts** (posts SocialPilot dropped, e.g. while the account was
locked), then **QueuedPosts**:
- delivered: `python -m bore.cli publish mark <id> --status posted --url "<post URL if shown>"`
- errored or unscheduled: `python -m bore.cli publish mark <id> --status failed --error "<SocialPilot's error or 'unscheduled by SocialPilot'>"`
  (an editor decides whether to retry it)
- still in **QueuedPosts**: leave it (SocialPilot is late).
- in none of the four lookups, more than 2 hours after its time: `python -m bore.cli publish mark <id> --status missing`.
  The dashboard fails it only when a second run misses it too, and then asks an editor to look
  on Facebook before any retry.
  A lookup that itself returns an error (account locked, connector missing, timeout) is never
  "not found": leave that item for a later run (ViewPost answering that there is no such post is
  "not found there": go on with the searches). While the plan shows `socialpilot_down`, or
  after a `HELD` in this run, skip this rule altogether.

If something only a person can fix comes up that no command above records (e.g. a second copy of
a post you find in SocialPilot), record it:
`python -m bore.cli publish mark <id> --status needs_action --error "<what to do, e.g. delete the copy scheduled for 2026-10-21 18:30>"`.
The dashboard shows it in red until someone marks it done.

## 6. Finish

End with a short summary: each draft id, page, and result (scheduled for <time IST / ET>,
posted, held for SocialPilot, or failed with reason), plus anything from `no_account`,
`scheduled_but_superseded`, a preflight `NOTE` or a `STOP`.
