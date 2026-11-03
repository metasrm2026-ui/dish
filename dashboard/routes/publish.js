// Publishing entry point. POST {id} with either credential, for an approved draft. Called by
// /api/decide on approval. In routine mode the draft stays "approved" (queued) until the
// SocialPilot publisher routine reports the result to /api/result.
import { isRoutine, isReviewer, deny } from '../lib/auth.js';
import { loadDraft, setStatus } from '../lib/store.js';
import { publish } from '../lib/publish.js';

export async function publishDraft(id) {
  const draft = await loadDraft(id);
  if (!draft) throw new Error('no such draft');
  // a failed one is approved again only through /api/decide, which asks first whether it may be
  // on Facebook already or was sent again from the Studio (this used to approve it straight away)
  if (draft.status === 'failed') throw new Error('retry a failed post on the review page: it checks first that it is not out already');
  if (draft.status !== 'approved') throw new Error(`cannot publish a ${draft.status} draft`);
  const mode = process.env.PUBLISH_MODE || 'routine';
  if (mode !== 'routine') await setStatus(id, 'publishing', { caption: draft.caption_final });
  try {
    const result = await publish(draft);
    if (mode === 'routine') return { status: 'approved', queued: true, result };
    await setStatus(id, 'posted', { caption: draft.caption_final, result });
    return { status: 'posted', result };
  } catch (e) {
    await setStatus(id, 'failed', { caption: draft.caption_final, error: String(e.message || e) });
    return { status: 'failed', error: String(e.message || e) };
  }
}

export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).end();
  if (!isRoutine(req) && !isReviewer(req)) return deny(res);
  try {
    return res.status(200).json(await publishDraft((req.body || {}).id));
  } catch (e) {
    return res.status(400).json({ error: String(e.message || e) });
  }
}
