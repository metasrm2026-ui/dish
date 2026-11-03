// Reviewer feedback -> regenerate in the next hourly routine run.
// POST (reviewer) {id, feedback, caption?, image_b64?, image_type?}
//      Records the request (and an optional reference image); the draft leaves Waiting.
// GET  (routine)  the open requests: draft content + feedback + reference image URL.
// POST (routine)  {id, action: "failed", error}: the routine could not apply it; back to Waiting
//      with the reason shown on the card. (Success is recorded by POST /api/drafts with `replaces`.)
import { isRoutine, isReviewer, deny } from '../lib/auth.js';
import { listAll, groupDrafts, loadDraft, setStatus, saveReference, claimDecision, decidedBy, releaseDecision } from '../lib/store.js';
import { recordReview } from '../lib/reviews.js';
import { mapLimit, READ_CONCURRENCY, errorStatus } from '../lib/db.js';
import { resentAs, already, notYours } from './decide.js';
import { pageScope } from '../lib/tenancy.js';

const OPEN = ['pending', 'failed', 'rejected'];

export default async function handler(req, res) {
  try {
    if (req.method === 'GET') {
      if (!isRoutine(req)) return deny(res);
      const todo = groupDrafts(await listAll()).filter(d => d.status === 'regenerate');
      const requests = await mapLimit(todo, READ_CONCURRENCY, async d => {
        const x = await loadDraft(d.id);
        const { statusBody, ...draft } = x;
        return { ...draft, feedback: statusBody.feedback || '', reviewer_caption: statusBody.caption || '',
                 reference_image_url: statusBody.reference_image_url || '', requested_at: statusBody.at || 0 };
      });
      requests.sort((a, b) => a.requested_at - b.requested_at);
      return res.status(200).json({ requests });
    }
    if (req.method !== 'POST') { res.setHeader('Allow', 'GET, POST'); return res.status(405).end(); }
    const { id, action, feedback, caption, image_b64, image_type, error } = req.body || {};
    const draft = await loadDraft(id);
    if (!draft) return res.status(404).json({ error: 'no such draft' });

    if (isRoutine(req) && action === 'failed') {
      if (draft.status !== 'regenerate') return res.status(409).json({ error: `draft is ${draft.status}` });
      await setStatus(id, 'pending', { regen_error: String(error || 'could not apply the feedback').slice(0, 600),   // and the
                                       feedback: draft.statusBody.feedback || '',                               // editor's caption
                                       ...(draft.statusBody.caption ? { caption: draft.statusBody.caption } : {}) });
      return res.status(200).json({ ok: true, status: 'pending' });
    }
    if (!isReviewer(req)) return deny(res);
    await pageScope(req);
    if (await notYours(req, res, draft)) return;
    const text = String(feedback || '').trim();
    if (!text && !image_b64) return res.status(400).json({ error: 'write feedback or attach an image' });
    if (!OPEN.includes(draft.status)) return res.status(409).json({ error: already(draft.status) });
    // taken back on purpose (withdrawn, or retired when its Studio post was sent again), or a Studio
    // post's old draft: redone, it would be a second post of the story
    const gone = draft.statusBody.withdrawn ? draft.statusBody.reason || 'withdrawn' : await resentAs(draft);
    if (gone) return res.status(409).json({ error: `not redone: ${gone}` });
    const buf = image_b64 ? Buffer.from(image_b64, 'base64') : null;
    if (buf && buf.length > 3_500_000) return res.status(413).json({ error: 'image too large (max ~3.5 MB)' });
    const taken = await claimDecision(draft.id, draft.statusBody.at, req.user.email, 'regenerate');
    if (taken) return res.status(409).json({ error: decidedBy(taken) });
    try {                                     // not sent after all: the claim is given back
      const reference_image_url = buf ? await saveReference(id, buf, image_type || 'image/jpeg') : '';
      const edited = caption && caption.trim() !== (draft.caption || '').trim() ? caption.trim() : '';
      await setStatus(id, 'regenerate', { feedback: text.slice(0, 2000), caption: edited, reference_image_url });
    } catch (e) { await releaseDecision(draft.id, draft.statusBody.at).catch(() => {}); throw e; }
    // the editor's words also teach the routine (a note, not a verdict on the story)
    if (text) await recordReview('feedback', draft, { reason: 'Redo', note: text, by: req.user && req.user.email });
    return res.status(200).json({ ok: true, status: 'regenerate' });
  } catch (e) {
    return res.status(errorStatus(e)).json({ error: String(e.message || e) });
  }
}
