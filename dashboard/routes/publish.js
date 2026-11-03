// Publishing entry point. POST {id} with either credential, for an approved draft. Called by
// /api/decide on approval. In routine mode the draft stays "approved" (queued) until the
// SocialPilot publisher routine reports the result to /api/result.
import { isRoutine, isReviewer, deny } from '../lib/auth.js';
import { loadDraft, setStatus } from '../lib/store.js';
import { publish } from '../lib/publish.js';
import { getRecord } from '../lib/db.js';
import { notPublishable } from '../lib/pages.js';
import { pageScope, inScope } from '../lib/tenancy.js';

export async function publishDraft(id) {
  const draft = await loadDraft(id);
  if (!draft) throw new Error('no such draft');
  // a failed one is approved again only through /api/decide, which asks first whether it may be
  // on Facebook already or was sent again from the Studio (this used to approve it straight away)
  if (draft.status === 'failed') throw new Error('retry a failed post on the review page: it checks first that it is not out already');
  if (draft.status !== 'approved') throw new Error(`cannot publish a ${draft.status} draft`);
  const page = await getRecord('pages', draft.page);
  const closed = notPublishable(page);                                      // only a live page's, on its own account
  if (closed) throw new Error(`not published: ${closed}`);
  const mode = process.env.PUBLISH_MODE || 'routine';
  if (mode !== 'routine') await setStatus(id, 'publishing', { caption: draft.caption_final });
  try {
    const result = await publish({ ...draft, socialpilot_login_id: page.socialpilot_login_id });
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
    // a person: only a post of the pages they may use (another workspace's is no post at all)
    const scope = await pageScope(req);
    const d = scope ? await loadDraft(String((req.body || {}).id || '')) : null;
    if (scope && (!d || !inScope(scope, d.page))) return res.status(404).json({ error: 'no such draft' });
    return res.status(200).json(await publishDraft((req.body || {}).id));
  } catch (e) {
    return res.status(400).json({ error: String(e.message || e) });
  }
}
