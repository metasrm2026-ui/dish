// Run the API in-process against the in-memory blob store.
process.env.BLOB_MOCK = '1';
process.env.DATA_KEY = Buffer.alloc(32, 7).toString('base64');
process.env.SESSION_SECRET = 'test-session-secret-0123456789abcdef0123456789';
process.env.ROUTINE_SECRET = 'routine-test-secret';
const { mockFetch } = await import('../lib/blob.js');
const realFetch = globalThis.fetch;
export const remote = new Map();     // url -> {status, body, type}
globalThis.fetch = mockFetch(async (url, opts) => {
  const r = remote.get(String(url));
  if (r) return new Response(r.body, { status: r.status || 200, headers: { 'content-type': r.type || 'text/html' } });
  if (String(url).startsWith('http')) return new Response('offline in tests', { status: 503 });
  return realFetch(url, opts);
});
const { default: main } = await import('../api/main.js');

export function client() {
  let cookie = '';
  async function call(method, path, body, { routine = false, raw = false } = {}) {
    const [p, qs] = path.split('?');
    const query = Object.fromEntries(new URLSearchParams(qs || ''));
    query.route = p.replace(/^\/api\//, '');
    const payload = body === undefined ? Buffer.alloc(0) : Buffer.from(JSON.stringify(body));
    const req = {
      method, query, url: path,
      headers: { 'content-type': 'application/json', host: 'test.local', cookie,
                 ...(routine ? { authorization: `Bearer ${process.env.ROUTINE_SECRET}` } : {}) },
      async *[Symbol.asyncIterator]() { if (payload.length) yield payload; },
    };
    const out = { status: 200, headers: {}, body: null };
    const res = {
      headersSent: false,
      setHeader(k, v) { out.headers[k.toLowerCase()] = v; },
      status(s) { out.status = s; return res; },
      json(o) { out.body = o; res.headersSent = true; return res; },
      send(b) { out.body = b; res.headersSent = true; return res; },
      end() { res.headersSent = true; return res; },
    };
    await main(req, res);
    const sc = out.headers['set-cookie'];
    if (sc) cookie = sc.split(';')[0].endsWith('=') ? '' : sc.split(';')[0];
    return out;
  }
  return { call, get cookie() { return cookie; }, set cookie(c) { cookie = c; } };
}
