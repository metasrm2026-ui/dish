// GET/PUT (routine) the routine's gzipped SQLite database, so each hourly session resumes
// where the last one stopped.
// GET answers 503 unless the stored copy was read and is gzip: during a storage outage it once
// passed on a security-check web page as the state, and a session that went on without its state
// would push an empty one over it. 24 copies are kept (about a day of hourly pushes).
// A 404 says `never_saved` only while no state was ever saved (meta/state is written by the first
// save and never removed): a new product's first hourly run then starts a new database itself. Once
// a state was saved, finding none is a 503: something removed it, and a run must not start over.
import { put, list } from '../lib/blob.js';
import { fetchBlob, getRecord, createRecord } from '../lib/db.js';
import { isRoutine, deny } from '../lib/auth.js';

export const config = { api: { bodyParser: false } };
const KEEP_STATES = 24;
const isGzip = buf => buf.length > 2 && buf[0] === 0x1f && buf[1] === 0x8b;

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return Buffer.concat(chunks);
}

export default async function handler(req, res) {
  if (!isRoutine(req)) return deny(res);
  if (req.method === 'GET') {
    let latest, buf;
    try {
      const { blobs } = await list({ prefix: 'state/' });
      latest = blobs.sort((a, b) => new Date(b.uploadedAt) - new Date(a.uploadedAt))[0];
      if (latest) buf = await fetchBlob(latest.url);
    } catch (e) {
      return res.status(503).json({ error: `state unreadable: ${e.message}. Do not run without it and do not push` });
    }
    if (!latest) {
      let meta;
      try { meta = await getRecord('meta', 'state'); } catch (e) {
        return res.status(503).json({ error: `state unreadable: ${e.message}. Do not run without it and do not push` });
      }
      if (meta) return res.status(503).json({ error: `state missing: it was first saved on ${new Date(meta.first_saved_at).toISOString().slice(0, 10)} but no copy is stored. Do not run without it and do not push: report it` });
      return res.status(404).json({ error: 'no state yet', never_saved: true });
    }
    if (!isGzip(buf)) return res.status(503).json({ error: 'state unreadable: storage returned something that is not the saved database. Do not run without it and do not push' });
    res.setHeader('Content-Type', 'application/gzip');
    return res.status(200).send(buf);
  }
  if (req.method === 'PUT') {
    const body = await readBody(req);
    if (!isGzip(body)) return res.status(400).json({ error: 'send the gzipped database' });
    // new name each time (no stale CDN reads); older copies are pruned below
    const b = await put(`state/bore-${Date.now()}.db.gz`, body, { access: 'public', contentType: 'application/gzip', addRandomSuffix: true });
    // once only (create-only): from now on a missing state is an error, never a first run
    await createRecord('meta', 'state', { first_saved_at: Date.now() }).catch(e => { if (!/exist/i.test(String(e && e.message))) throw e; });
    const { blobs } = await list({ prefix: 'state/' });
    const old = blobs.sort((a, b2) => new Date(b2.uploadedAt) - new Date(a.uploadedAt)).slice(KEEP_STATES);
    if (old.length) { const { del } = await import('../lib/blob.js'); await del(old.map(o => o.url)); }
    return res.status(200).json({ ok: true, bytes: body.length, url: b.pathname });
  }
  res.setHeader('Allow', 'GET, PUT');
  return res.status(405).end();
}
