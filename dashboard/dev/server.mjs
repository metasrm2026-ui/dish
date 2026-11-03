// Local preview of the whole tool against the in-memory store (no Vercel, no real data):
//   node dev/server.mjs [port]      then open http://localhost:3000  (admin@example.com / dev password 123)
// Seeds a dummy admin, editor, page and story feed. Nothing is persisted.
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join } from 'node:path';

const PORT = Number(process.argv[2] || 3000);
process.env.BLOB_MOCK = '1';
process.env.BLOB_MOCK_BASE = `http://localhost:${PORT}/_blob/`;
process.env.DATA_KEY ||= Buffer.alloc(32, 7).toString('base64');
process.env.SESSION_SECRET ||= 'dev-session-secret-0123456789abcdef0123456789';
process.env.ROUTINE_SECRET ||= 'dev-routine';
const { mockFetch } = await import('../lib/blob.js');
globalThis.fetch = mockFetch(globalThis.fetch);
const { default: main } = await import('../api/main.js');
const PUB = new URL('../public/', import.meta.url).pathname;
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg' };

function adapt(req, res, route, query) {
  req.query = { ...query, route };
  let status = 200;
  res.status = s => { status = s; return res; };
  res.json = o => { res.statusCode = status; res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(o)); return res; };
  res.send = b => { res.statusCode = status; res.end(b); return res; };
  const end = res.end.bind(res);
  res.end = (...a) => { if (!res.headersSent) res.statusCode = status; return end(...a); };
}

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, `http://localhost:${PORT}`);
  if (u.pathname.startsWith('/api/')) {
    adapt(req, res, u.pathname.slice(5), Object.fromEntries(u.searchParams));
    return main(req, res);
  }
  if (u.pathname.startsWith('/_blob/')) {
    const r = await fetch(`${process.env.BLOB_MOCK_BASE}${decodeURIComponent(u.pathname.slice(7))}`);
    res.writeHead(r.status, { 'content-type': r.headers.get('content-type') || 'application/octet-stream' });
    return res.end(Buffer.from(await r.arrayBuffer()));
  }
  const file = u.pathname === '/' ? 'index.html' : u.pathname.slice(1);
  try {
    const body = await readFile(join(PUB, file));
    res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream' });
    res.end(body);
  } catch { res.writeHead(404); res.end('not found'); }
});

async function api(method, path, body, cookie = '', routine = false) {
  const r = await fetch(`http://localhost:${PORT}${path}`, { method, body: body && JSON.stringify(body),
    headers: { 'content-type': 'application/json', cookie, ...(routine ? { authorization: `Bearer ${process.env.ROUTINE_SECRET}` } : {}) } });
  return { status: r.status, body: await r.json().catch(() => null), cookie: (r.headers.get('set-cookie') || '').split(';')[0] };
}

server.listen(PORT, async () => {
  const PW = 'dev password 123';
  const boot = await api('POST', '/api/routine/bootstrap', { email: 'admin@example.com', name: 'Dev Admin' }, '', true);
  await api('POST', '/api/auth/reset', { token: boot.body.setup_link.split('#reset=')[1], password: PW });
  const { cookie } = await api('POST', '/api/auth/login', { email: 'admin@example.com', password: PW });
  const pg = await api('POST', '/api/admin/pages', { name: 'Dummy Sports', fb_page_id: '123', socialpilot_login_id: '999' }, cookie);
  const slug = pg.body.page.slug;
  await api('PATCH', '/api/admin/users/' + (await api('GET', '/api/auth/me', null, cookie)).body.user.id, { pages: [slug] }, cookie);
  const inv = await api('POST', '/api/admin/users', { email: 'editor@example.com', name: 'Dev Editor', role: 'editor', pages: [slug] }, cookie);
  await api('POST', '/api/auth/reset', { token: inv.body.setup_link.split('#reset=')[1], password: PW });
  const now = Date.now();
  const img = i => `https://picsum.photos/seed/bore${i}/800/1000`;
  const stories = [1, 2, 3, 4, 5, 6, 7].map(i => ({ id: `s${i}`, headline: `Dummy story number ${i} about a game result`, summary: `A two line summary of dummy story ${i}. Nothing here is real.`,
    score: 10 - i, sources: [{ name: 'Example News', url: `https://example.com/${i}` }, ...(i % 3 ? [] : [{ name: 'Other Wire', url: `https://example.org/${i}` }])],
    published_at: now - i * 2.5 * 3600e3, url: `https://example.com/${i}`,
    ideas: [{ title: 'Stat card', template: 'stat_card', description: 'One huge number with the player photo.' }, { title: 'Then vs now', template: 'then_vs_now', description: 'Two photos side by side.' }],
    images: [{ url: img(i), source: 'web', credit: 'picsum.photos', size: '800x1000' }] }));
  await api('POST', '/api/routine/feed', { page: slug, stories }, '', true);
  console.log(`dev server http://localhost:${PORT}  admin@example.com / editor@example.com  password: ${PW}`);
});
