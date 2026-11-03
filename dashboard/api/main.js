// The single API function. Every /api/* path is rewritten here (vercel.json) and dispatched to
// routes/*. One function keeps the deployment inside the plan's function limit and gives every
// route the same auth, body parsing and error handling.
import { currentUser } from '../lib/session.js';
import { errorStatus } from '../lib/db.js';
import { requestWrites } from '../lib/blob.js';

export const config = { api: { bodyParser: false } };

const ROUTES = {
  // review queue of the hourly routine (paths kept for the routines already deployed)
  drafts: () => import('../routes/drafts.js'), decide: () => import('../routes/decide.js'),
  feedback: () => import('../routes/feedback.js'), links: () => import('../routes/links.js'),
  publish: () => import('../routes/publish.js'), queue: () => import('../routes/queue.js'),
  result: () => import('../routes/result.js'), state: () => import('../routes/state.js'),
  stats: () => import('../routes/stats.js'),
  // Phase 1
  auth: () => import('../routes/auth.js'), admin: () => import('../routes/admin.js'),
  studio: () => import('../routes/studio.js'), routine: () => import('../routes/routine.js'),
  health: () => import('../routes/health.js'), templates: () => import('../routes/templates.js'),
};

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return Buffer.concat(chunks);
}

// each request keeps track of whether it wrote anything, for the message of a storage error (lib/db.js)
export default function handler(req, res) {
  return requestWrites.run({ wrote: false }, () => handle(req, res));
}

async function handle(req, res) {
  const route = String(req.query.route || '').replace(/^\/+|\/+$/g, '');
  const [head, ...rest] = route.split('/');
  const load = ROUTES[head];
  if (!load) return res.status(404).json({ error: `no route /api/${route}` });
  req.subpath = rest;                      // e.g. /api/admin/users/abc -> ['users', 'abc']
  try {
    if (head !== 'state' && req.method !== 'GET' && req.method !== 'HEAD') {
      const raw = await readBody(req);
      if (raw.length) {
        if (!String(req.headers['content-type'] || '').includes('application/json')) {
          return res.status(415).json({ error: 'send JSON' });
        }
        req.body = JSON.parse(raw.toString('utf8'));
      } else req.body = {};
    }
    req.user = await currentUser(req, res);
    const mod = await load();
    return await mod.default(req, res);
  } catch (e) {
    console.error(`/api/${head}`, e && e.message);   // never the request body: it may hold prompts or documents
    // storage refusing reads (a security check) is a 503 with a clear message, not a crash
    if (!res.headersSent) res.status(errorStatus(e)).json({ error: String((e && e.message) || e) });
  }
}
