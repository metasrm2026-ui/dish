// "URL to post": an editor pastes a news link; the next hourly routine run makes a post from it.
//   links/<id>/link.json               {id, url, page, note, at}
//   links/<id>/s-<ts>-<status>.json     waiting -> done {draft_id} | failed {error}
// POST (reviewer) {url, page, note?}            queue a link
// GET  (reviewer|routine) ?status=waiting|all    list requests with their latest status
// POST (routine) {id, action: "failed", error}  the routine could not make a post from it
// (Success is recorded by POST /api/drafts with `link_request: <id>`.)
import { put } from '../lib/blob.js';
import { isRoutine, isReviewer, deny } from '../lib/auth.js';
import { listAll, getJson } from '../lib/store.js';
import { errorStatus, getRecord } from '../lib/db.js';
import { canUsePage } from '../lib/session.js';

// the page's name as set in Admin > Pages, when the user may post for it (and it is not archived)
async function pageName(user, slug) {
  const p = slug && typeof slug === 'string' && await canUsePage(user, slug) && await getRecord('pages', slug);
  return p && !p.archived ? p.name || slug : '';
}

// an editor sees and retries only the links of their own pages (admins and the routine: every page)
const theirs = (req, l) => !req.user || req.user.role === 'admin' || (req.user.pages || []).includes(l.page);

export async function setLinkStatus(id, status, body = {}) {
  const at = Date.now();
  await put(`links/${id}/s-${at}-${status}.json`, JSON.stringify({ status, at, ...body }), {
    access: 'public', contentType: 'application/json', addRandomSuffix: false,
  });
}

export async function listLinks() {
  const by = {};
  for (const b of await listAll('links/')) {
    const m = b.pathname.match(/^links\/([^/]+)\/(.+)$/);
    if (!m) continue;
    const r = (by[m[1]] ||= { id: m[1], statuses: [] });
    if (m[2] === 'link.json') r.url = b.url;
    const s = m[2].match(/^s-(\d+)-([a-z]+)\.json$/);
    if (s) r.statuses.push({ at: Number(s[1]), status: s[2], url: b.url });
  }
  const out = [];
  for (const r of Object.values(by)) {
    if (!r.url) continue;
    r.statuses.sort((a, b) => a.at - b.at);
    const last = r.statuses[r.statuses.length - 1];
    const [link, st] = await Promise.all([getJson(r.url), last ? getJson(last.url).catch(() => ({})) : {}]);
    out.push({ ...link, status: last ? last.status : 'waiting', status_info: st });
  }
  return out.sort((a, b) => b.at - a.at);
}

export default async function handler(req, res) {
  try {
    if (req.method === 'GET') {
      if (!isReviewer(req) && !isRoutine(req)) return deny(res);
      const want = String(req.query.status || 'all');
      const links = (await listLinks()).filter(l => (want === 'all' || l.status === want) && theirs(req, l)).slice(0, 100);
      return res.status(200).json({ links });
    }
    if (req.method !== 'POST') { res.setHeader('Allow', 'GET, POST'); return res.status(405).end(); }
    const { id, action, error, url, page, note } = req.body || {};
    if (isRoutine(req) && action === 'failed') {
      await setLinkStatus(id, 'failed', { error: String(error || 'could not make a post from this link').slice(0, 600) });
      return res.status(200).json({ ok: true, status: 'failed' });
    }
    if (action === 'retry' && (isReviewer(req) || isRoutine(req))) {  // a failed link goes back in the queue
      const cur = (await listLinks()).find(l => l.id === id && theirs(req, l));   // only a failed one: never made twice
      if (!cur) return res.status(404).json({ error: 'no such link' });
      if (cur.status !== 'failed') return res.status(409).json({ error: cur.status === 'done' ? 'already made: the post is in Waiting' : 'already in the queue' });
      await setLinkStatus(id, 'waiting', { retried: true });
      return res.status(200).json({ ok: true, status: 'waiting' });
    }
    if (!isReviewer(req)) return deny(res);
    let u;
    try { u = new URL(String(url || '').trim()); } catch { return res.status(400).json({ error: 'paste a full link starting with https://' }); }
    if (!/^https?:$/.test(u.protocol)) return res.status(400).json({ error: 'paste a full link starting with https://' });
    const name = await pageName(req.user, page);
    if (!name) return res.status(400).json({ error: 'choose the page' });
    const recent = (await listLinks()).filter(l => l.url === u.href && l.page === page && l.status !== 'failed'
      && Date.now() - l.at < 4 * 86400e3);
    if (recent.length) return res.status(409).json({ error: `already requested for ${name} (${recent[0].status})` });
    const at = Date.now();
    const rid = `${page}-${at}-${Math.random().toString(36).slice(2, 7)}`;
    const link = { id: rid, url: u.href, page, page_name: name, note: String(note || '').trim().slice(0, 1000), at };
    await put(`links/${rid}/link.json`, JSON.stringify(link), { access: 'public', contentType: 'application/json', addRandomSuffix: false });
    await setLinkStatus(rid, 'waiting', {});
    return res.status(201).json({ ok: true, link });
  } catch (e) {
    return res.status(errorStatus(e)).json({ error: String(e.message || e) });
  }
}
