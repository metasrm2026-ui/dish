// POST  (routine)  create a pending draft: JSON {id, page, page_name, caption, ..., image_b64}
// GET   (reviewer) list drafts: ?status=pending|approved|posted|rejected|failed|regenerate|all
//       (approved includes those the publisher is scheduling right now: publishing)
//       A routine POST with `replaces: <old id>` marks the old draft regenerated.
import { isRoutine, isReviewer, deny } from '../lib/auth.js';
import { listAll, groupDrafts, getJson, pageOf, saveDraft, setStatus, deleteOlderThan, lastMidnight, claimDecision, setDecided, decidedBy } from '../lib/store.js';
import { audit } from '../lib/audit.js';
import { recordReview } from '../lib/reviews.js';
import { setLinkStatus } from './links.js';
import { already } from './decide.js';
import { mapLimit, READ_CONCURRENCY, errorStatus, getRecord } from '../lib/db.js';
import { pageScope, inScope } from '../lib/tenancy.js';
import { statusOf } from '../lib/pages.js';

export default async function handler(req, res) {
  try {
    // a person sees and decides only the posts of the pages they may use (lib/tenancy.js: their
    // workspace's, an editor's own); the product owner and the routines every page
    const scope = await pageScope(req);
    const mine = (r, d) => inScope(scope, d.page);
    if (req.method === 'POST' && req.query.action === 'clear') {   // editors clear waiting posts
      if (!isReviewer(req)) return deny(res);
      const b = req.body || {};
      const ids = new Set(Array.isArray(b.ids) ? b.ids.map(String) : []);
      const before = Number(b.before) || 0;                         // unix: everything waiting made before it
      const pg = b.page ? String(b.page) : null;                    // ... on the page shown (all when null)
      if (!ids.size && !before) return res.status(400).json({ error: 'choose posts to clear' });
      const all = groupDrafts(await listAll());
      const pending = all.filter(d => d.status === 'pending' && mine(req, d) &&
        (ids.has(d.id) || (before && d.createdAt < before && (!pg || d.page === pg))));
      const cleared = [];                           // the ids applied: the page keeps edits on the others
      // selected posts someone else decided meanwhile: said why, never just left out of the count
      const skipped = all.filter(d => ids.has(d.id) && d.status !== 'pending' && mine(req, d)).map(d => ({ id: d.id, why: already(d.status) }));
      for (let i = 0; i < pending.length; i += 10) await Promise.all(pending.slice(i, i + 10).map(async d => {
        const at = d.statuses.length ? d.statuses[d.statuses.length - 1].at : 0;
        const taken = await claimDecision(d.id, at, req.user.email, 'clear');
        if (taken) { if (ids.has(d.id)) skipped.push({ id: d.id, why: decidedBy(taken) }); return; }
        await setDecided(d.id, at, 'cleared', { by: req.user.email, ...(ids.has(d.id) ? {} : { age_clear: true }) });
        cleared.push(d.id);
        if (!ids.has(d.id)) return;                 // clearing by age is housekeeping, not a verdict
        const body = await getJson(d.draftUrl, d.draftKey).catch(() => ({ id: d.id, page: d.page }));
        await recordReview('cleared', { ...body, id: d.id }, { by: req.user.email });   // a weak "not this one"
      }));
      await audit(req.user, 'drafts_cleared', { count: cleared.length, before: before || null });
      return res.status(200).json({ ok: true, cleared: cleared.length, ids: cleared, ...(skipped.length ? { skipped } : {}) });
    }
    if (req.method === 'POST' && req.query.action === 'restore') { // cleared posts back to Waiting
      if (!isReviewer(req)) return deny(res);                     // an editor's action, never a routine's
      const ids = new Set(Array.isArray((req.body || {}).ids) ? req.body.ids.map(String) : []);
      if (!ids.size) return res.status(400).json({ error: 'choose posts to restore' });
      const keepFrom = lastMidnight() - Number(process.env.CLEAR_MIN_AGE_HOURS ?? 12) * 3600;   // else they'd clear again
      const back = groupDrafts(await listAll()).filter(d => ids.has(d.id) && d.status === 'cleared' && d.createdAt >= keepFrom && mine(req, d));
      const who = req.user.email;
      for (const d of back) {
        await setStatus(d.id, 'pending', { restored_by: who });
        await recordReview('restored', { id: d.id, page: d.page }, { by: who });   // an earlier clear no longer counts
      }
      await audit(req.user, 'drafts_restored', { count: back.length });
      return res.status(200).json({ ok: true, restored: back.length, skipped: ids.size - back.length });
    }
    if (req.method === 'POST') {
      if (!isRoutine(req)) return deny(res);
      const { image_b64, extra_images_b64, ...draft } = req.body || {};
      if (!draft.id || !draft.page || !draft.caption || !image_b64) return res.status(400).json({ error: 'id, page, caption, image_b64 required' });
      if (pageOf(String(draft.id)) !== String(draft.page)) return res.status(400).json({ error: 'the id must start with the page: <page>-<YYYYMMDDHHMM>-<slug>' });
      // a post is made only for a live page (or one making its first posts): never a paused one
      const pg = await getRecord('pages', String(draft.page));
      if (!pg) return res.status(404).json({ error: 'no such page' });
      if (!['live', 'going_live'].includes(statusOf(pg))) return res.status(409).json({ error: `the page is ${statusOf(pg).replace('_', ' ')}: no posts are made for it` });
      // a post "as is" can carry the editor's other images (posted together, in order)
      const extras = (Array.isArray(extra_images_b64) ? extra_images_b64 : []).slice(0, 5).map(b => Buffer.from(String(b), 'base64'));
      const imageUrl = await saveDraft(draft, Buffer.from(image_b64, 'base64'), extras);
      await setStatus(draft.id, 'pending', {});
      // a regenerated post supersedes the draft the reviewer sent back
      if (draft.replaces) await setStatus(draft.replaces, 'regenerated', { new_id: draft.id });
      // a post made from an editor's link closes that request
      if (draft.link_request) await setLinkStatus(draft.link_request, 'done', { draft_id: draft.id });
      // housekeeping: keep two weeks of drafts
      if (Math.random() < 0.1) await deleteOlderThan(Number(process.env.KEEP_DAYS || 14));
      return res.status(201).json({ ok: true, id: draft.id, image_url: imageUrl });
    }
    if (req.method === 'GET') {
      if (!isReviewer(req) && !isRoutine(req)) return deny(res);
      const want = (req.query.status || 'pending').toString();
      const since = Number(req.query.since || 0);   // routine: posts made since <unix> (duplicate check)
      let grouped = groupDrafts(await listAll());
      // midnight clean-up (editors' time zone): at midnight, posts that have already waited a full
      // review day leave Waiting. Posts made in the evening (the US-daytime batch) stay for the
      // editors' next morning: only those made before noon yesterday (CLEAR_MIN_AGE_HOURS) clear.
      const minAge = Number(process.env.CLEAR_MIN_AGE_HOURS ?? 12) * 3600;
      const cutoff = lastMidnight() - minAge;
      const stale = grouped.filter(d => d.status === 'pending' && d.createdAt < cutoff);
      if (stale.length && process.env.AUTO_CLEAR !== 'off') {
        for (const d of stale) {      // an editor deciding it at the same moment wins
          const at = d.statuses.length ? d.statuses[d.statuses.length - 1].at : 0;
          if (!await claimDecision(d.id, at, 'the midnight clean-up', 'clear'))
            await setDecided(d.id, at, 'cleared', { by: 'midnight clean-up', auto: true });
        }
        grouped = groupDrafts(await listAll());
      }
      const matching = grouped
        .filter(d => (want === 'all' || d.status === want || (want === 'approved' && d.status === 'publishing')) && d.createdAt >= since && mine(req, d))
        .sort((a, b) => b.createdAt - a.createdAt);
      const limit = Math.min(Number(req.query.limit || 100), 300);
      const drafts = matching.slice(0, limit);
      const full = await mapLimit(drafts, READ_CONCURRENCY, async d => {
        const body = await getJson(d.draftUrl, d.draftKey);
        const last = d.statuses[d.statuses.length - 1];
        const statusBody = last ? await getJson(last.url, last.key).catch(e => { if (errorStatus(e) === 503) throw e; return {}; }) : {};
        const approved = [...d.statuses].reverse().find(x => x.status === 'approved');
        return { ...body, status: d.status, image_url: d.imageUrl, extra_images: d.extraUrls || [], created_at_unix: d.createdAt, status_info: statusBody,
                 approved_at: approved ? Math.floor(approved.at / 1000) : null };
      });
      // the newest `limit` only: say so, so a caller never takes a cut list for all of them
      return res.status(200).json({ drafts: full, ...(matching.length > limit ? { truncated: true, total: matching.length } : {}) });
    }
    res.setHeader('Allow', 'GET, POST');
    return res.status(405).end();
  } catch (e) {
    return res.status(errorStatus(e)).json({ error: String(e.message || e) });
  }
}
