// GET/PUT (routine) the routine's gzipped SQLite database, so each hourly session resumes
// where the last one stopped.
// GET answers 503 unless the stored copy was read and is gzip: during a storage outage it once
// passed on a security-check web page as the state, and a session that went on without its state
// would push an empty one over it. 24 copies are kept (about a day of hourly pushes).
import { put, list } from '../lib/blob.js';
import { fetchBlob } from '../lib/db.js';
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
    if (!latest) return res.status(404).json({ error: 'no state yet' });
    if (!isGzip(buf)) return res.status(503).json({ error: 'state unreadable: storage returned something that is not the saved database. Do not run without it and do not push' });
    res.setHeader('Content-Type', 'application/gzip');
    return res.status(200).send(buf);
  }
  if (req.method === 'PUT') {
    const body = await readBody(req);
    if (!isGzip(body)) return res.status(400).json({ error: 'send the gzipped database' });
    // new name each time (no stale CDN reads); older copies are pruned below
    const b = await put(`state/bore-${Date.now()}.db.gz`, body, { access: 'public', contentType: 'application/gzip', addRandomSuffix: true });
    const { blobs } = await list({ prefix: 'state/' });
    const old = blobs.sort((a, b2) => new Date(b2.uploadedAt) - new Date(a.uploadedAt)).slice(KEEP_STATES);
    if (old.length) { const { del } = await import('../lib/blob.js'); await del(old.map(o => o.url)); }
    return res.status(200).json({ ok: true, bytes: body.length, url: b.pathname });
  }
  res.setHeader('Allow', 'GET, PUT');
  return res.status(405).end();
}
