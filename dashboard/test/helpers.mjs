// Run the API in-process against the in-memory blob store.
process.env.BLOB_MOCK = '1';
process.env.DATA_KEY = Buffer.alloc(32, 7).toString('base64');
process.env.SESSION_SECRET = 'test-session-secret-0123456789abcdef0123456789';
process.env.ROUTINE_SECRET = 'routine-test-secret';
// sign-in as before (lib/defaults.js turns open access on for the deployment): open.test.mjs turns it on
process.env.OPEN_ACCESS = '0';
// the deny list the tests run with: made-up accounts only (lib/deny.js)
process.env.SOCIALPILOT_DENY_FILE = new URL('./fixtures/socialpilot.yaml', import.meta.url).pathname;
process.env.SOCIALPILOT_BLOCKED_IDS = '900003';
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

const db = await import('../lib/db.js');
let logins = 100000;

// A page as the onboarding routine leaves it: live, on its own SocialPilot account (made up). How a
// page gets there through Go live and the onboarding routine is tested in onboard.test.mjs.
export async function makeLive(slug, login = '') {
  return db.updateRecord('pages', slug, cur => cur && { ...cur, status: 'live', socialpilot_login_id: String(login || cur.socialpilot_login_id || ++logins) });
}
// a live page record made directly, for tests of the publisher about a page no screen made
export async function livePage(slug, login = '', workspace = 'demo-workspace') {
  return db.updateRecord('pages', slug, cur => ({ slug, name: slug, workspace, ...(cur || {}), status: 'live',
                                                  socialpilot_login_id: String(login || (cur && cur.socialpilot_login_id) || ++logins) }));
}
export async function setPageStatus(slug, status) {
  return db.updateRecord('pages', slug, cur => cur && { ...cur, status });
}

// A tester's workspace, made by the product owner (a signed-in super client): its id, and its owner's
// set-password link
export async function workspace(superClient, name = 'Demo Workspace', email = '') {
  const r = await superClient.call('POST', '/api/admin/testers', { email: email || `tester-${Math.random().toString(36).slice(2, 8)}@example.com`,
                                                                  name: 'Tester', workspace_name: name });
  if (r.status !== 200) throw new Error(`workspace: ${JSON.stringify(r.body)}`);
  return { id: r.body.workspace.id, link: r.body.setup_link, email: r.body.user.email };
}

// the product owner's first sign-in: bootstrap, password, a client signed in
export async function signedInSuper(robot, email = 'owner@example.com', pw = 'correct horse 42') {
  const c = client();
  const boot = await robot.call('POST', '/api/routine/bootstrap', { email, name: 'Owner' }, { routine: true });
  if (boot.status !== 200) throw new Error(`bootstrap: ${JSON.stringify(boot.body)}`);
  await c.call('POST', '/api/auth/reset', { token: boot.body.setup_link.split('#reset=')[1], password: pw });
  return c;
}
export async function setPassword(c, link, pw = 'editor pass 77') {
  return c.call('POST', '/api/auth/reset', { token: link.split('#reset=')[1], password: pw });
}

// A page made in Admin > Pages, in the product owner's test workspace (made on first use), and live
// on a made-up account unless login is null
export async function adminPage(superClient, name, login = '', extra = {}) {
  if (!superClient.ws) superClient.ws = (await workspace(superClient)).id;
  const r = await superClient.call('POST', '/api/admin/pages', { name, workspace: superClient.ws, ...extra });
  if (r.status !== 200) throw new Error(`adminPage: ${JSON.stringify(r.body)}`);
  if (login !== null) await makeLive(r.body.page.slug, login);
  return r.body.page.slug;
}
