// POST (reviewer) {id, decision: "approve"|"reject"|"withdraw", caption?} or {ids, decision: "retry"} (failed posts)
// or {id, decision: "done"}: a person did what a needs-action note asked (in SocialPilot or on Facebook).
// Approving records the (possibly edited) caption and immediately triggers the publish routine.
import { isReviewer, deny } from '../lib/auth.js';
import { loadDraft, setStatus, setDecided, claimDecision, decidedBy, getJson } from '../lib/store.js';
import { publishDraft } from './publish.js';
import { fireRoutine } from '../lib/publish.js';
import { audit } from '../lib/audit.js';
import { recordReview } from '../lib/reviews.js';
import { put, list, del } from '../lib/blob.js';
import { getRecord, errorStatus } from '../lib/db.js';
import { setNeedsAction, needsAction } from './result.js';
import { health } from './health.js';
import { isSuper, pageScope } from '../lib/tenancy.js';
import { notPublishable } from '../lib/pages.js';

// a person decides only the posts of the pages they may use (lib/tenancy.js), as in the Studio. The
// request's scope is read first (pageScope): a check without it is a mistake, never "every page".
export const mine = (req, d) => {
  if (!req.user || isSuper(req.user)) return true;
  if (!req._scope) throw new Error('the pages this person may use were not read');
  return req._scope.has(d.page);
};
export const NOT_YOURS = 'this post is for a page you no longer have: reload the page';
// A post of a page this person may not use: one of their own workspace's pages they were not given
// says so; another workspace's is no post at all (its id says nothing).
export async function notYours(req, res, d) {
  if (mine(req, d)) return false;
  const p = await getRecord('pages', d.page).catch(() => null);
  if (p && req.user.workspace && p.workspace === req.user.workspace) res.status(403).json({ error: NOT_YOURS });
  else res.status(404).json({ error: 'no such draft' });
  return true;
}
// an approval goes to the publisher: only for a live page with its own SocialPilot account
async function unpublishable(page) {
  return notPublishable(await getRecord('pages', page));
}

// Why a failed post must not simply be approved again: an original whose update went out instead
// would go out after it, and so would a Studio post's draft once the editor sent the post again
// from the Studio (a new draft); a post SocialPilot lost may be on Facebook already, and a copy made
// on the wrong SocialPilot account still goes out unless someone deleted it there. For those an
// editor confirms first that they looked or did what the red note says (the review page asks).
async function retryRefused(draft, confirmed, note) {
  const err = String((draft.statusBody && draft.statusBody.error) || '');
  if (/^superseded by/i.test(err)) return 'its update replaced it';
  const resent = await resentAs(draft);
  if (resent) return resent;
  if (draft.statusBody && draft.statusBody.check_facebook && !confirmed) return 'look on the Facebook page first';
  if (note && !confirmed) return 'do what its red note says first';
  return '';
}

// A Studio post's draft that is no longer its post's draft: the editor sent the post again from the
// Studio, as a new draft. The Studio retires the old draft when it does; this covers posts sent
// again before it did. A batch that cannot be read is not guessed at.
export async function resentAs(draft) {
  const s = draft.studio;
  if (!s || !s.batch || !s.post) return '';
  let bt;
  try { bt = await getRecord('batches', s.batch); } catch (e) { return 'its Studio post could not be read: try again in a few minutes'; }
  const p = bt && bt.posts.find(x => x.id === s.post);
  const now = p && p.publish && p.publish.draft_id;
  return now && now !== draft.id ? `resent from the Studio as ${now}` : '';
}

// An approved draft no run has scheduled yet is taken back: its approval's claim first, so no
// publisher run can be scheduling it at the same moment. false: a run (or another take-back) has it.
// A take-back whose status write failed gives its claim back; one whose claim could not be given back
// either (storage down) is finished by the next one: a "withdrawn by" claim is never a run's.
export async function takeBack(d, reason, by) {
  const name = `claims/${d.id}/${d.approved_ms || 0}.json`;
  try {
    await put(name, JSON.stringify({ at: Date.now(), run: `withdrawn by ${by}`, reason }),   // (reason: for result.js withdrawnMeanwhile)
              { access: 'public', contentType: 'application/json', addRandomSuffix: false, allowOverwrite: false });
  } catch (e) {
    const [b] = (await list({ prefix: name })).blobs.filter(x => x.pathname === name);
    const c = b && await getJson(b.url).catch(() => null);
    if (!(c && /^withdrawn by /.test(String(c.run || '')) && d.status === 'approved')) return false;
  }
  try { await setStatus(d.id, 'rejected', { reason, withdrawn: true, by }); }
  catch (e) { await del((await list({ prefix: name })).blobs.filter(x => x.pathname === name).map(x => x.url)).catch(() => {}); throw e; }
  return true;
}

// Approved again just as the editor sent its Studio post again (two people at the same moment: the
// batch was read before the Studio moved the post to its new draft): the approval is taken back, so
// the post goes out once. The Studio checks the old draft again too, once its new one is approved.
async function resentMeanwhile(draft, by) {
  const why = draft.studio ? await resentAs(draft) : '';
  if (why) await takeBack(await loadDraft(draft.id), why, by);
  return why;
}

// Approving it again settles only a "look on Facebook" note (the editor just confirmed they
// looked). Any other note, e.g. a copy to delete in SocialPilot, stays until someone marks it Done.
async function settleNote(draft) {
  if (draft.statusBody && draft.statusBody.check_facebook) await setNeedsAction(draft.id, draft.page, '');
}

// POST {ids: [...], decision: "retry", captions?: {id: caption}}: approve failed posts again (e.g.
// after SocialPilot was unlocked) with the captions the editor sees (their approved ones unless
// edited), then start the publisher once for all of them.
async function retryMany(req, res, ids) {
  const done = [], refused = [], notes = await needsAction({ strict: true });
  for (const id of ids.slice(0, 40)) {
    const draft = await loadDraft(String(id));
    if (!draft || draft.status !== 'failed' || !mine(req, draft)) continue;
    const why = await unpublishable(draft.page) || await retryRefused(draft, req.body.confirmed === true, notes[draft.id]);
    if (why) { refused.push({ id: draft.id, why }); continue; }
    const taken = await claimDecision(draft.id, draft.statusBody.at, req.user.email, 'retry');
    if (taken) { refused.push({ id: draft.id, why: decidedBy(taken) }); continue; }
    const edited = [String(((req.body.captions || {})[draft.id]) || '')].filter(written).map(c => c.trim())[0] || '';
    // a Studio post keeps the editor's chosen time (already past: the earliest slot, 1 hour out)
    await setDecided(draft.id, draft.statusBody.at, 'approved', { caption: edited || (draft.caption_final || draft.caption).trim(), retry: true, by: req.user.email,
                                                                  ...(draft.requested_at ? { requested_at: draft.requested_at, mode: draft.publish_mode } : {}) });
    const again = await resentMeanwhile(draft, req.user.email);
    if (again) { refused.push({ id: draft.id, why: again }); continue; }
    await settleNote(draft);
    done.push(draft.id);
  }
  if (done.length) await audit(req.user, 'drafts_retried', { count: done.length });
  const fired = done.length && (process.env.PUBLISH_MODE || 'routine') === 'routine' ? await fireRoutine(done[0]) : null;
  return res.status(200).json({ ok: true, retried: done.length, ids: done, refused, fired: fired ? fired.fired : false });
}

// a caption with something to read: not only spaces or zero-width characters (trim() keeps those)
const written = s => /[^\s\u200b-\u200d\u2060\ufeff]/.test(s);

// What a reviewer is told when someone else decided on the post first (their page was drawn before):
// a sentence, never the internal status name
const ALREADY = { approved: 'Someone already approved this post (see Queued)', publishing: 'This post is being sent to SocialPilot right now (see Queued)',
  scheduled: 'This post is already scheduled in SocialPilot (see Queued)', posted: 'This post is already on the page (see Posted)',
  rejected: 'Someone already marked this post Not okay', cleared: 'This post was cleared from the queue',
  regenerate: 'Someone already sent this back for a new version (see Regenerating)',
  regenerated: 'A new version of this post was made already (see Waiting)', failed: 'This post failed to publish (see Failed)',
  pending: 'This post is waiting for review again (see Waiting)' };
export const already = status => ALREADY[status] || 'Someone else changed this post meanwhile: the list is reloaded';

// POST {id, decision: "withdraw"}: an approved post not scheduled yet (e.g. waiting while SocialPilot
// is down) is not published after all. Taking the publisher's claim first means no run can be
// scheduling it at the same moment; if a run already has it, it is too late here.
async function withdraw(req, res, id) {
  const d = await loadDraft(String(id || ''));
  if (!d) return res.status(404).json({ error: 'no such draft' });
  if (await notYours(req, res, d)) return;
  if (d.status !== 'approved') return res.status(409).json({ error: already(d.status) });
  if (!await takeBack(d, 'Withdrawn', req.user.email))
    return res.status(409).json({ error: 'being scheduled in SocialPilot right now: too late to withdraw here' });
  await recordReview('withdrawn', d, { by: req.user.email });
  return res.status(200).json({ ok: true, status: 'rejected' });
}

export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).end();
  if (!isReviewer(req)) return deny(res);
  try {
    await pageScope(req);
    const { id, ids, decision, caption, reason, note } = req.body || {};
    if (decision === 'withdraw') return await withdraw(req, res, id);
    if (decision === 'done') {                       // a needs-action note was dealt with
      const d = await loadDraft(String(id || ''));
      if (!d) return res.status(404).json({ error: 'no such draft' });
      if (await notYours(req, res, d)) return;
      const act = (await needsAction({ strict: true }))[d.id];
      // the outdated original is gone from SocialPilot: it is not scheduled any more, nor looked for
      if (act && act.kind === 'superseded_original' && d.status === 'scheduled')
        await setStatus(d.id, 'rejected', { reason: 'Removed from SocialPilot: its update replaced it', withdrawn: true, by: req.user.email });
      await setNeedsAction(d.id, d.page, '');
      const h = await health({ fresh: true }).catch(() => null);   // the red "needs a person" banner goes with the note
      await audit(req.user, 'needs_action_done', { draft: d.id });
      return res.status(200).json({ ok: true, ...(h ? { health_at: h.checked_at } : {}) });   // the page asks for that status
    }
    if (decision === 'retry') {
      if (!Array.isArray(ids) || !ids.length) return res.status(400).json({ error: 'choose posts to retry' });
      return await retryMany(req, res, ids);
    }
    if (decision === 'reject_many') {                 // Not okay for several waiting posts, one reason
      if (!Array.isArray(ids) || !ids.length) return res.status(400).json({ error: 'choose posts' });
      const done = [], skipped = [];                  // the ids applied: the page keeps edits on the others
      for (const one of ids.slice(0, 60)) {
        const d = await loadDraft(String(one));
        if (!d || !mine(req, d)) continue;
        if (d.status !== 'pending') { skipped.push({ id: d.id, why: already(d.status) }); continue; }
        const taken = await claimDecision(d.id, d.statusBody.at, req.user.email, 'reject');
        if (taken) { skipped.push({ id: d.id, why: decidedBy(taken) }); continue; }
        await setDecided(d.id, d.statusBody.at, 'rejected', { reason: String(reason || '').slice(0, 60), note: String(note || '').trim().slice(0, 300), by: req.user.email });
        await recordReview('rejected', d, { reason, note, by: req.user.email });
        done.push(d.id);
      }
      return res.status(200).json({ ok: true, rejected: done.length, ids: done, ...(skipped.length ? { skipped } : {}) });
    }
    const draft = await loadDraft(id);
    if (!draft) return res.status(404).json({ error: 'no such draft' });
    if (await notYours(req, res, draft)) return;
    if (draft.status !== 'pending' && draft.status !== 'failed') return res.status(409).json({ error: already(draft.status) });
    if (decision !== 'reject' && decision !== 'approve') return res.status(400).json({ error: 'decision must be approve or reject' });
    // an emptied caption is refused, never swapped for the routine's (callers that send none get it)
    if (decision === 'approve' && typeof caption === 'string' && !written(caption))
      return res.status(400).json({ error: 'the caption is empty: write one first' });
    const closed = decision === 'approve' && await unpublishable(draft.page);
    if (closed) return res.status(409).json({ error: `not approved: ${closed}` });
    const refused = decision === 'approve' && draft.status === 'failed' && await retryRefused(draft, req.body.confirmed === true, (await needsAction({ strict: true }))[draft.id]);
    if (refused) return res.status(409).json({ error: `not approved again: ${refused}` });
    const taken = await claimDecision(draft.id, draft.statusBody.at, req.user.email, decision);
    if (taken) return res.status(409).json({ error: decidedBy(taken) });
    if (decision === 'reject') {
      await setDecided(id, draft.statusBody.at, 'rejected', { reason: String(reason || '').slice(0, 60), note: String(note || '').trim().slice(0, 300), by: req.user.email });
      // approved, then SocialPilot failed: dismissing it now withdraws it, it does not judge the story
      await recordReview(draft.status === 'failed' ? 'withdrawn' : 'rejected', draft, { reason, note, by: req.user.email });
      if (draft.statusBody && draft.statusBody.check_facebook) await setNeedsAction(draft.id, draft.page, '');   // they looked
      return res.status(200).json({ ok: true, status: 'rejected' });
    }
    await setDecided(id, draft.statusBody.at, 'approved', { caption: (caption || draft.caption).trim(), by: req.user.email,   // a failed Studio post keeps its time
                                                            ...(draft.status === 'failed' && draft.requested_at ? { requested_at: draft.requested_at, mode: draft.publish_mode } : {}) });
    const again = draft.status === 'failed' && await resentMeanwhile(draft, req.user.email);
    if (again) return res.status(409).json({ error: `not approved again: ${again}` });
    if (draft.status === 'failed') await settleNote(draft);
    if (draft.status === 'pending') await recordReview('approved', draft, { by: req.user.email });
    const result = await publishDraft(id);
    return res.status(200).json({ ok: true, ...result });
  } catch (e) {
    return res.status(errorStatus(e)).json({ error: String(e.message || e) });
  }
}
