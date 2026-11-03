// POST (routine) {id, status: "publishing"|"scheduled"|"posted"|"failed"|"held"|"missing"|"needs_action", post_id?, url?, error?, claim?, scope?}
// The SocialPilot publisher routine reports each publish here. 'rejected' {reason} withdraws an
// approved draft that must not be published (e.g. a duplicate), before it is scheduled.
// 'held': SocialPilot (or, with scope, one page's account or one lookup) cannot take the post now:
// it goes back to the queue for a later run instead of failing (see hold below). 'publishing'
// answers with a claim token; holding a claimed draft needs it.
// Only these moves are taken, so the publisher's rules hold here too, not only in its prompt:
// publishing from approved; scheduled or posted from publishing or scheduled; failed from approved,
// publishing or scheduled. Anything else is a 409 (e.g. "scheduled" for a draft nobody claimed).
// A claim is also refused for a slot under 45 minutes from another claimed or scheduled post on
// the page (takeSlot), for a Studio draft the Studio has sent again as a new one (resentAs), and
// while the draft a resent one was resent from is being scheduled.
// 'missing': a run's lookups did not find a scheduled post (see missing below). 'needs_action'
// {error}: something only a person can do in SocialPilot, shown in red until someone marks it done.
import { isRoutine, deny } from '../lib/auth.js';
import { loadDraft, setStatus, getJson } from '../lib/store.js';
import { usage } from '../lib/audit.js';
import { put, list, del } from '../lib/blob.js';
import * as db from '../lib/db.js';

const { getRecord, putRecord } = db;
// The health records below are changed by several publisher runs at once (every approval starts
// one). updateRecord (versioned, create-only writes) makes the change again on top of another
// run's; a store without it reads and writes as before.
const updateRecord = db.updateRecord || (async (coll, id, mutate) => {
  const cur = await getRecord(coll, id).catch(() => null);
  const next = await mutate(cur);
  if (next != null) await putRecord(coll, id, next);
  return next ?? cur;
});

const HOLD_HOURS = () => Number(process.env.PUBLISH_HOLD_HOURS || 12);
const RETRY_MINUTES = 40;     // bore/publish.py split_waiting: SocialPilot is tried about once an hour
const MISS_GAP_MINUTES = 30;  // two delivery misses this far apart are two runs, not one run reporting twice
const LOOKUP_TRIES = 3;       // a draft whose SocialPilot lookups failed this often goes to a person
const BREAKER_MINUTES = 60;   // bore/publish.py BREAKER_MINUTES: a page's unknown failure counts this long
// the gap between two posts on a page: config/socialpilot.yaml min_gap_minutes, as in studio.js
const GAP_MS = () => Number(process.env.SCHEDULE_GAP_MINUTES || 45) * 60e3;
const CLAIMING_MS = 60e3;     // a claim is made within one request (at most 60 s)
const NEXT = { publishing: ['approved'], scheduled: ['publishing', 'scheduled'], posted: ['publishing', 'scheduled'],
               failed: ['approved', 'publishing', 'scheduled'] };
export const NOT_DELIVERED = 'not delivered: SocialPilot has no record of it (checked twice): look on the Facebook page before retrying';
export const LOOKUPS_FAILED = 'lookups failed: the publisher could not check whether this post is in SocialPilot already: look in SocialPilot and on the Facebook page before retrying';

export async function socialpilotHealth() {
  return (await getRecord('health', 'socialpilot').catch(() => null)) || {};
}

// What only a person can do (delete an outdated original or a second copy in SocialPilot, look on
// Facebook for a post SocialPilot lost) used to go only into the publisher session's summary. One
// small record keeps it, so the review page and the health status read it at once:
// {items: {<draft id>: {page, text, at}}}. A note nobody marked done drops out after a week.
// strict: a record that cannot be read throws (a 503, nothing changed), for the checks that must not
// take that for "no note" (approving a failed post again); the tolerant read is for showing them.
export async function needsAction({ strict = false } = {}) {
  const rec = (await getRecord('health', 'needs_action').catch(e => {
    if (strict) throw e instanceof db.StorageChallenged ? e : new db.StorageChallenged(0);
    return null;
  })) || {};
  const week = Date.now() - 7 * 86400e3;
  return Object.fromEntries(Object.entries(rec.items || {}).filter(([, x]) => x && x.at >= week));
}

// kind 'superseded_original': an outdated original still in SocialPilot; once a person deleted it
// there, marking it done retires the draft too (decide.js), so it is not looked for any more.
export async function setNeedsAction(id, page, text, kind = '') {      // text '' marks it done
  let changed = false;
  await updateRecord('health', 'needs_action', cur => {
    const week = Date.now() - 7 * 86400e3;
    const items = Object.fromEntries(Object.entries((cur && cur.items) || {}).filter(([, x]) => x && x.at >= week));
    changed = text ? !(items[id] && items[id].text === String(text).slice(0, 300)) : !!items[id];
    if (!changed) return null;                                   // the same note again: no write
    if (text) items[id] = { page, text: String(text).slice(0, 300), at: Date.now(), ...(kind ? { kind } : {}) };
    else delete items[id];
    return { items };
  });
  return changed;
}

// A withdraw that took the approval's claim but could not write its status nor give the claim back
// (storage down: routes/decide.js takeBack) is finished here, so the post does not sit in Queued.
async function withdrawnMeanwhile(d) {
  const name = `claims/${d.id}/${d.approved_ms || 0}.json`;
  const [b] = (await list({ prefix: name }).catch(() => ({ blobs: [] }))).blobs.filter(x => x.pathname === name);
  const c = b && await getJson(b.url).catch(() => null);
  const by = c && (String(c.run || '').match(/^withdrawn by (.+)$/) || [])[1];
  if (!by) return '';
  await setStatus(d.id, 'rejected', { reason: c.reason || 'Withdrawn', withdrawn: true, by });
  return 'withdrawn by an editor: never schedule it';
}

// When the draft's wait started: its editor's approval, or a Studio post's chosen time (ms).
const waitStart = d => Math.max((d.first_approved_at || d.approved_at || 0) * 1000, d.requested_at || 0);

// Did this draft wait because SocialPilot was down (held, or approved during the outage)?
function waitedForOutage(d, health) {
  if (d.holds > 0) return true;
  const t = (d.first_approved_at || d.approved_at || 0) * 1000;
  return !!health.since && t >= health.since - 3600e3 && (health.down || t < (health.recovered_at || 0));
}

// What a draft that waited too long moves to Failed with, by why it waited (hold below)
function tooOld(scope = '') {
  const h = `${HOLD_HOURS()}h after this was approved`;
  if (scope === 'account') return `SocialPilot kept refusing this page's posts for over ${h}: check the page's account in SocialPilot (e.g. reconnect it), then retry it if it is still timely`;
  if (scope === 'run') return `the publisher was not allowed to call SocialPilot for over ${h}: retry it if it is still timely`;
  if (scope === 'draft') return LOOKUPS_FAILED;
  return `SocialPilot was unavailable for over ${h}: retry it if it is still timely`;
}
const failedBody = (d, scope, error, holds) => ({ caption: d.caption_final, error, holds,
  ...(scope ? { held_scope: scope } : { outage: true }), ...(scope === 'draft' ? { check_facebook: true } : {}) });

// Why a draft waits (scope, from bore/cli.py cmd_publish):
//   ''        SocialPilot itself refused (account locked, subscription, connector missing:
//             bore/publish.py OUTAGE_RE, or the run's preflight call failed): every page waits, and
//             the health record says SocialPilot is down (the editors' banner, split_waiting)
//   'account' one page's SocialPilot account failed twice in a run (or SocialPilot flags it)
//   'draft'   a lookup for this draft failed, so it was not created
//   'run'     the publisher session was not allowed to call SocialPilot
// Only the first marks SocialPilot down: one page's or one draft's trouble used to make every page
// wait, and a held draft heads every later plan, so it stalled them all for up to 12 hours.
// In every case the draft was certainly not created by this try. It goes back to the queue -
// approved again, keeping its first approval time - and a run about an hour later tries again
// (split_waiting). Only the run that claimed a draft can hold it (claim token), so no other run can
// put it up for a second claim. That run can also hold a draft it failed moments ago, when its
// breaker finds a second failure on the same account. Held drafts are looked up in SocialPilot
// before they are created again (queue.js ever_sent).
// Past HOLD_HOURS it moves to Failed instead: news that old needs an editor's look first. A draft
// whose lookups failed LOOKUP_TRIES times goes to a person sooner: it may be in SocialPilot already.
async function hold(res, d, detail) {
  if (['publishing', 'failed'].includes(d.status) && String(detail.claim || '') !== String(d.approved_ms || '')) {
    return res.status(409).json({ error: 'claimed by another publisher run: leave it to that run' });
  }
  const now = Date.now(), error = String(detail.error || 'SocialPilot unavailable').slice(0, 300);
  const scope = ['account', 'draft', 'run'].includes(detail.scope) ? detail.scope : '';
  if (d.status === 'approved' && d.holds && now - (d.approved_ms || 0) < RETRY_MINUTES * 60e3) {
    return res.status(200).json({ ok: true, id: d.id, status: 'held', error, holds: d.holds });   // just held: not another try
  }
  if (d.status === 'approved') {    // unclaimed: take this approval's claim, so a withdraw or a run's claim can't overlap
    try {
      await put(`claims/${d.id}/${d.approved_ms || 0}.json`, JSON.stringify({ at: now, run: 'held' }),
                { access: 'public', contentType: 'application/json', addRandomSuffix: false, allowOverwrite: false });
    } catch (e) {
      return res.status(409).json({ error: 'claimed by another publisher run or withdrawn: leave it' });
    }
  }
  const holds = (d.holds || 0) + 1, lookups = (d.lookup_fails || 0) + (scope === 'draft' ? 1 : 0);
  const fail = now + 3600e3 - waitStart(d) > HOLD_HOURS() * 3600e3      // it would go out 1 hour later at the earliest
    || lookups >= LOOKUP_TRIES;
  const why = scope === 'draft' ? LOOKUPS_FAILED : tooOld(scope);
  try {
    if (fail) await setStatus(d.id, 'failed', { ...failedBody(d, scope, `${why} (${error})`, holds), ...(lookups ? { lookup_fails: lookups } : {}) });
    else await setStatus(d.id, 'approved', { caption: d.caption_final, held: true, error, holds,
                                             ...(scope ? { held_scope: scope } : {}), ...(lookups ? { lookup_fails: lookups } : {}),
                                             first_approved_at: d.first_approved_at || d.approved_at,
                                             requested_at: d.requested_at, mode: d.publish_mode, by: d.approved_by });
  } catch (e) {     // not held after all: the claim taken above is given back, so a run or a withdraw can have it
    if (d.status === 'approved') {
      const name = `claims/${d.id}/${d.approved_ms || 0}.json`;
      await del((await list({ prefix: name })).blobs.filter(x => x.pathname === name).map(x => x.url)).catch(() => {});
    }
    throw e;
  }
  if (fail && scope === 'draft')
    await setNeedsAction(d.id, d.page, 'look in SocialPilot and on the Facebook page: the publisher could not check whether this post is there already');
  if (!scope) await updateRecord('health', 'socialpilot', cur => {
    const h = cur || {};
    return { ...h, down: true, error, since: h.down ? h.since : now, last_try_at: now };
  });
  return res.status(200).json({ ok: true, id: d.id, status: fail ? 'failed' : 'held', error: fail ? why : error, holds });
}

// A scheduled post that SocialPilot errored or dropped during an outage: Failed, for an editor.
async function failNow(res, d, detail) {
  const error = String(detail.error || 'SocialPilot unavailable').slice(0, 300);
  const prev = d.statusBody && d.statusBody.result ? d.statusBody.result : {};
  await setStatus(d.id, 'failed', { caption: d.caption_final, result: { via: 'socialpilot-routine', ...prev }, error, outage: true });
  return res.status(200).json({ ok: true, id: d.id, status: 'failed', error });
}

// A scheduled post that a run's lookups (DeliveredPosts, ErrorPosts, UnscheduledPosts, QueuedPosts,
// all answering) did not find, over 2 hours after its time. SocialPilot's search can miss one, so it
// fails as "not delivered" only when a second run, MISS_GAP_MINUTES or more later, misses it too;
// even then it may be on Facebook, so the editor is asked to look there first (no one-click retry).
async function missing(res, d) {
  if (d.status !== 'scheduled') return res.status(409).json({ error: `not scheduled: already ${d.status}` });
  const now = Date.now(), due = Date.parse(((d.statusBody && d.statusBody.result) || {}).scheduled_for || '');
  if (due && now - due < 2 * 3600e3) return res.status(409).json({ error: 'not 2 hours past its time yet: leave it for a later run' });
  let m;
  await updateRecord('health', 'delivery', rec => {
    const misses = {};
    for (const [k, x] of Object.entries((rec && rec.misses) || {})) if (now - (x.last_at || 0) < 3 * 86400e3) misses[k] = x;
    m = { n: 0, last_at: 0, ...misses[d.id] };
    if (now - m.last_at >= MISS_GAP_MINUTES * 60e3) { m.n += 1; m.last_at = now; }   // the same run again does not count
    if (m.n < 2) misses[d.id] = m;
    else delete misses[d.id];
    return { misses };
  });
  if (m.n < 2) return res.status(200).json({ ok: true, id: d.id, status: 'scheduled', misses: m.n });
  const prev = d.statusBody && d.statusBody.result ? d.statusBody.result : {};
  await setStatus(d.id, 'failed', { caption: d.caption_final, result: { via: 'socialpilot-routine', ...prev }, error: NOT_DELIVERED,
                                    check_facebook: true });
  await setNeedsAction(d.id, d.page, 'look on the Facebook page: SocialPilot has no record of this post, it may or may not have gone out');
  return res.status(200).json({ ok: true, id: d.id, status: 'failed', error: NOT_DELIVERED });
}

// Several publisher runs plan at once (every approval starts one), each from its own look at the
// queue, and a claim used to check only its own draft: runs a few seconds apart took alternate
// drafts 40 minutes apart, or the same minute when one run counted a busy time the other did not.
// Every claim's slot goes into one record per page (slots/<page>: versioned create-only writes,
// so of two claims made at the same moment one sees the other), and a slot under 45 minutes from
// another draft that still goes out then is refused. The run plans again (its plan counts the
// other claim) and gets a free slot: only ever later. An editor's own Studio time is kept as it
// is (bore/publish.py plan), so it is never refused here; the other posts move around it. It has
// no entry here until its own claim, so a run that planned before that Send can still take a slot
// under 45 minutes from it (accepted: an editor's time may sit that close to a post already
// scheduled as well; a reservation made at the Send would close it).
// Returns null, {claimed} (another request is claiming this draft right now) or {id, at}.
async function takeSlot(d, at) {
  const own = !!d.requested_at && d.publish_mode !== 'now' && Math.abs(at - d.requested_at) <= 60e3;
  let clash = null;
  await updateRecord('slots', d.page, async cur => {
    const now = Date.now(), items = {};
    clash = null;
    for (const [k, x] of Object.entries((cur && cur.items) || {})) if (x && x.at + GAP_MS() > now) items[k] = x;  // gone out: no longer counts
    const mine = items[d.id];
    if (mine && mine.ms === d.approved_ms && now - mine.claimed_at < CLAIMING_MS) { clash = { claimed: true }; return null; }
    for (const [k, x] of own ? [] : Object.entries(items)) {
      if (k === d.id || Math.abs(x.at - at) >= GAP_MS()) continue;
      const t = await goesOutAt(k, x, now);
      if (t && Math.abs(t - at) < GAP_MS()) { clash = { id: k, at: t }; return null; }
    }
    items[d.id] = { at, ms: d.approved_ms, claimed_at: now };
    return { items };
  });
  return clash;
}

// When another draft's claimed slot still goes out: its record's time while it is claimed or
// scheduled (the time every plan counts, so a run that plans again gets past it); its slot while
// that claim is still being made; never once it failed, waits again, was withdrawn or went out.
// A draft that cannot be read now counts (only ever later).
async function goesOutAt(id, x, now) {
  const o = await loadDraft(id).catch(() => undefined);
  if (o === undefined) return x.at;
  if (!o) return null;
  if (['publishing', 'scheduled'].includes(o.status)) return Date.parse(((o.statusBody || {}).result || {}).scheduled_for || '') || null;
  return o.status === 'approved' && o.approved_ms === x.ms && now - x.claimed_at < CLAIMING_MS ? x.at : null;
}

// SocialPilot keeps a scheduled post at another time than its claim (an earlier copy the duplicate
// guard found): later claims are kept 45 minutes from the real time.
async function moveSlot(d, at) {
  await updateRecord('slots', d.page, cur => {
    const items = { ...((cur && cur.items) || {}) };
    if (items[d.id] && items[d.id].at === at) return null;
    items[d.id] = { claimed_at: 0, ...items[d.id], at, ms: d.approved_ms };
    return { items };
  });
}

// A Studio post the Studio sent again is a new draft (its resent_from names this one), and the
// batch's post now points to it. This one must never go out as well, whoever approved it again
// (Retry on the review page): the same story twice, perhaps with an edited caption no lookup
// finds. Returns the new draft's id, {sending} while that Send is still being made, or null. A
// Send cut short long ago made no draft: this one is still the post.
async function resentAs(d) {
  if (!d.studio || !d.studio.batch) return null;
  const bt = await getRecord('batches', d.studio.batch).catch(() => null);
  const pub = ((bt && (bt.posts || []).find(x => x.id === d.studio.post)) || {}).publish || {};
  if (!pub.draft_id || pub.draft_id === d.id) return null;
  if (await loadDraft(pub.draft_id)) return pub.draft_id;
  return ['sending', 'pending'].includes(pub.state) && Date.now() - (pub.sending_at || 0) < 5 * 60e3 ? { sending: pub.draft_id } : null;
}

export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).end();
  if (!isRoutine(req)) return deny(res);
  const { id, status, ...detail } = req.body || {};
  if (!['publishing', 'scheduled', 'posted', 'failed', 'rejected', 'held', 'missing', 'needs_action'].includes(status))
    return res.status(400).json({ error: 'status must be publishing, scheduled, posted, failed, held, missing, needs_action or rejected' });
  const d = await loadDraft(id);
  if (!d) return res.status(404).json({ error: 'no such draft' });
  if (status === 'held' && ['approved', 'publishing'].includes(d.status)) return hold(res, d, detail);
  if (status === 'held' && d.status === 'failed' && detail.claim) return hold(res, d, detail);   // the run's breaker
  if (status === 'held' && d.status === 'scheduled') return failNow(res, d, detail);   // errored or dropped in SocialPilot
  if (status === 'held') return res.status(409).json({ error: `already ${d.status}` });
  if (status === 'missing') return missing(res, d);
  if (status === 'needs_action') {
    const text = String(detail.error || '').trim();
    if (!text) return res.status(400).json({ error: 'say what a person must do (error)' });
    await setNeedsAction(d.id, d.page, text, detail.kind === 'superseded_original' ? detail.kind : '');
    return res.status(200).json({ ok: true, id, status: d.status, needs_action: text.slice(0, 300) });
  }
  if (status === 'rejected') {
    if (!['approved', 'failed', 'pending'].includes(d.status)) return res.status(409).json({ error: `already ${d.status}` });
    await setStatus(id, 'rejected', { reason: detail.reason || '' });
    return res.status(200).json({ ok: true, id, status });
  }
  if (d.status === 'posted') return res.status(409).json({ error: 'already posted' });
  if (!NEXT[status].includes(d.status))
    return res.status(409).json({ error: `not ${status}: the draft is ${d.status} (only from ${NEXT[status].join(' or ')})` });
  if (status === 'publishing') {
    // One publisher run per approval: several runs can be going at once (every approval starts
    // one), and each must claim a draft before scheduling it. The claim blob is created only if
    // it does not exist yet, so exactly one run wins; the others get 409 and skip the draft.
    // one claim per approval record (a held draft is approved again, so it can be claimed again)
    const claim = () => put(`claims/${id}/${d.approved_ms || 0}.json`, JSON.stringify({ at: Date.now(), run: detail.run || '' }),
                            { access: 'public', contentType: 'application/json', addRandomSuffix: false, allowOverwrite: false })
      .then(() => true, () => false);
    const taken = () => res.status(409).json({ error: 'already claimed by another publisher run: skip it' });
    const resent = await resentAs(d).catch(() => ({ unreadable: true }));
    if (resent && typeof resent === 'object')
      return res.status(409).json({ error: resent.sending ? `the Studio is sending this post again (as ${resent.sending}) right now: skip it`
                                                          : 'its Studio post could not be read now: skip it, a later run tries again' });
    if (resent) {                    // withdrawn, with the claim taken first (as a withdraw does): no run is scheduling it
      if (!await claim()) return taken();
      await setStatus(id, 'rejected', { reason: `resent from the Studio as ${resent}`, withdrawn: true });
      return res.status(409).json({ error: `resent from the Studio as ${resent}: never schedule it` });
    }
    // The other order: the draft this one was resent from is being scheduled right now (retried on
    // the review page while the Studio sent the post again). Its copy has no post id yet for this
    // one's lookups, and an edited caption hides it from the duplicate guard: both used to be
    // created. A later run finds its copy (queue.js prior_post_id), or goes ahead if it failed.
    const from = d.resent_from ? await loadDraft(d.resent_from).catch(() => undefined) : null;
    if (from === undefined || (from && from.status === 'publishing'))
      return res.status(409).json({ error: from ? `the draft it was resent from (${from.id}) is being scheduled right now: skip it, a later run looks for its copy`
                                                : 'the draft it was resent from could not be read now: skip it, a later run tries again' });
    const slot = Date.parse(detail.scheduled_for || '');
    const clash = slot ? await takeSlot(d, slot).catch(() => null) : null;     // storage refusing: the plan kept the gap
    if (clash && clash.claimed) return taken();
    if (clash) return res.status(409).json({ error: `another post on this page (${clash.id}) goes out at ${new Date(clash.at).toISOString()}, `
                                                    + `less than ${GAP_MS() / 60e3} minutes from this slot: it gets a new time when you run publish plan again` });
    if (!await claim()) return withdrawnMeanwhile(d).then(why => why ? res.status(409).json({ error: why }) : taken());
    const health = await socialpilotHealth();
    const goesOut = Date.parse(detail.scheduled_for || '') || Date.now() + 3600e3;          // its planned slot
    if (waitedForOutage(d, health) && goesOut - waitStart(d) > HOLD_HOURS() * 3600e3) {     // too old to go out unseen
      const scope = d.holds > 0 ? d.held_scope || '' : '';
      await setStatus(id, 'failed', failedBody(d, scope, tooOld(scope), d.holds || 0));
      if (scope === 'draft')
        await setNeedsAction(d.id, d.page, 'look in SocialPilot and on the Facebook page: the publisher could not check whether this post is there already');
      return res.status(409).json({ error: `${tooOld(scope)} - moved to Failed (do NOT schedule it)` });
    }
  }
  const prev = d.statusBody && d.statusBody.result ? d.statusBody.result : {};
  const { needs_action: act, claim: _claim, breaker, ...kept } = detail;
  await setStatus(id, status, { caption: d.caption_final, result: { via: 'socialpilot-routine', ...prev, ...kept },
                                error: status === 'failed' ? String(detail.error || '').trim() || 'the publisher gave no reason' : detail.error });
  // `publish mark --status scheduled` checked SocialPilot's queue entry: a second copy, another time
  // or account, or a changed caption is for a person to fix there
  if (act) await setNeedsAction(id, d.page, String(act));
  if (status === 'scheduled') {      // CreatePost worked: SocialPilot is back, the waiting posts' warning ends
    const at = Date.parse(detail.scheduled_for || '');
    if (at && !(Math.abs(at - Date.parse(prev.scheduled_for || '')) <= 60e3)) await moveSlot(d, at).catch(() => {});
    const health = await socialpilotHealth();
    if (health.down || (health.failing_pages || {})[d.page] || Date.now() - (health.last_ok_at || 0) > 10 * 60e3)
      await updateRecord('health', 'socialpilot', cur => {
        const h = cur || {}, { [d.page]: _ok, ...failing } = h.failing_pages || {};     // and this page's account works
        return { ...h, down: false, last_ok_at: Date.now(), ...(h.down ? { recovered_at: Date.now() } : {}),
                 ...(h.failing_pages ? { failing_pages: failing } : {}) };
      });
  }
  // An unknown CreatePost failure (bore/cli.py sends breaker: the page's first in its run): a run's
  // breaker lasts one run, and every approval starts its own, so each post of an outage worded in
  // a way the code does not know failed on its own (Oct 10-11). The page's next failure within
  // the hour, from any run, is held at once instead (bore/cli.py _pub_plan); a post scheduled on
  // the page ends it.
  if (status === 'failed' && d.status === 'publishing' && breaker)
    await updateRecord('health', 'socialpilot', cur => {
      const h = cur || {}, since = Date.now() - BREAKER_MINUTES * 60e3;
      const failing = Object.fromEntries(Object.entries(h.failing_pages || {}).filter(([, x]) => x && x.at >= since));
      return { ...h, failing_pages: { ...failing, [d.page]: { id, error: String(detail.error || '').slice(0, 200), at: Date.now() } } };
    });
  if (status === 'scheduled' || status === 'posted' || status === 'failed') {   // usage log (section 7)
    await usage({ tool: 'socialpilot', page: d.page, user: (d.studio && d.studio.user) || 'hourly-routine', units: 1, unit: 'post',
                  detail: `${status} ${id}` });
  }
  res.status(200).json({ ok: true, id, status, ...(status === 'publishing' ? { claim: String(d.approved_ms || '') } : {}) });
}
