// Admin screens and accounts (the product owner's): what one admin saves is never undone by another admin, by the user
// signing in or by an upload finishing; settings the routines cannot read are refused; sign-in locks.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { client, makeLive, workspace } from './helpers.mjs';
import { resetReads } from '../lib/db.js';

const R = { routine: true };
const PW = 'correct horse 42';
const pdf = readFileSync(new URL('./fixtures/dummy_templates.pdf', import.meta.url));
const tinyJpeg = Buffer.from('/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=', 'base64');
const token = link => link.split('#reset=')[1];

const owner = client(), robot = client();
let slug, WS;
async function invite(email, extra = {}) {
  const c = client();
  const inv = await owner.call('POST', '/api/admin/users', { email, role: 'editor', workspace: WS, pages: [slug], ...extra });
  assert.equal(inv.status, 200, JSON.stringify(inv.body));
  assert.equal((await c.call('POST', '/api/auth/reset', { token: token(inv.body.setup_link), password: PW })).status, 200);
  return { c, id: inv.body.user.id };
}
const userOf = async email => (await owner.call('GET', '/api/admin/users')).body.users.find(u => u.email === email);
const pageOf = async s => (await owner.call('GET', '/api/admin/pages')).body.pages.find(p => p.slug === s);

// The next storage read of a record version that matches `re` waits until release() is called, so
// another request can run in between (as two admins, or a sign-in and an admin, do on slow storage).
const realFetch = globalThis.fetch;
let gate = null;
globalThis.fetch = async (url, opts) => {
  if (gate && gate.re.test(String(url))) { const g = gate; gate = null; g.reached(); await g.held; }
  return realFetch(url, opts);
};
function holdNextRead(re) {
  resetReads();                               // a version already read would come from memory
  let release, reached;
  const held = new Promise(r => { release = r; }), at = new Promise(r => { reached = r; });
  gate = { re, held, reached };
  return { release, reached: at };
}

test.before(async () => {
  const boot = await robot.call('POST', '/api/routine/bootstrap', { email: 'owner@example.com', name: 'Owner' }, R);
  assert.equal((await owner.call('POST', '/api/auth/reset', { token: token(boot.body.setup_link), password: PW })).status, 200);
  WS = (await workspace(owner)).id;
  slug = (await owner.call('POST', '/api/admin/pages', { name: 'Dummy Sports', workspace: WS })).body.page.slug;
  await makeLive(slug, '999');
});

test('time zones: only names the routines can read are saved', async () => {
  for (const tz of ['Eastern', 'US/Eastern', 'EST', 'America/Nowhere', 'eastern time']) {
    const r = await owner.call('PATCH', `/api/admin/pages/${slug}`, { timezone: tz });
    assert.equal(r.status, 400, tz);
    assert.match(r.body.error, /time zone must be a name like America\/New_York/);
  }
  assert.equal((await owner.call('POST', '/api/admin/pages', { name: 'Zone Page', timezone: 'Eastern', workspace: WS })).status, 400);
  assert.equal((await pageOf(slug)).timezone, 'America/New_York', 'nothing saved');
  assert.equal((await owner.call('PATCH', `/api/admin/pages/${slug}`, { timezone: 'Asia/Calcutta' })).body.page.timezone, 'Asia/Kolkata');
  assert.equal((await owner.call('PATCH', `/api/admin/pages/${slug}`, { timezone: 'America/Buenos_Aires' })).body.page.timezone,
               'America/Argentina/Buenos_Aires', 'an old name the browser\'s list offers is stored as the one the routines know');
  assert.equal((await owner.call('PATCH', `/api/admin/pages/${slug}`, { timezone: 'Europe/london' })).body.page.timezone,
               'Europe/London', 'stored as zoneinfo spells it: it does not ignore letter case');
  assert.equal((await owner.call('PATCH', `/api/admin/pages/${slug}`, { timezone: 'America/NEW_YORK' })).body.page.timezone, 'America/New_York');
  assert.equal((await owner.call('POST', '/api/admin/pages', { name: 'Case Zone', timezone: 'Asia/TOKYO', workspace: WS })).body.page.timezone, 'Asia/Tokyo');
  assert.equal((await owner.call('PATCH', `/api/admin/pages/${slug}`, { timezone: 'UTC' })).body.page.timezone, 'UTC');
  assert.equal((await owner.call('PATCH', `/api/admin/pages/${slug}`, { timezone: 'America/New_York' })).body.page.timezone, 'America/New_York');
  const cfg = (await robot.call('GET', '/api/routine/config', undefined, R)).body.pages.find(p => p.slug === slug);
  assert.equal(cfg.timezone, 'America/New_York');
});

test('a page keeps its name: an empty one is refused', async () => {
  const r = await owner.call('PATCH', `/api/admin/pages/${slug}`, { name: '  ' });
  assert.equal(r.status, 400);
  assert.equal(r.body.error, 'name the page');
  assert.equal((await pageOf(slug)).name, 'Dummy Sports');
});

test('a session that ended is asked to sign in again (401), not told "admins only"', async () => {
  const gone = client();
  const r = await gone.call('PATCH', `/api/admin/pages/${slug}`, { name: 'X' });
  assert.equal(r.status, 401);
  assert.equal(r.body.error, 'sign in first');
});

test('settings saved while a source is added or an upload finishes keep the source and the version', async () => {
  // a settings save that read the page before another admin added a source
  let h = holdNextRead(new RegExp(`db/pages/${slug}/`));
  const save = owner.call('PATCH', `/api/admin/pages/${slug}`, { timezone: 'America/Chicago' });
  await h.reached;
  const other = client();
  assert.equal((await other.call('POST', '/api/auth/login', { email: 'owner@example.com', password: PW })).status, 200);
  assert.equal((await other.call('POST', `/api/admin/pages/${slug}/sources`, { url: 'https://example.com/a', type: 'website' })).status, 200);
  h.release();
  assert.equal((await save).status, 200);
  let p = await pageOf(slug);
  assert.equal(p.source_count, 1, 'the source added meanwhile is kept');
  assert.equal(p.timezone, 'America/Chicago');
  // a source added while the page's settings were saved
  h = holdNextRead(new RegExp(`db/pages/${slug}/`));
  const add = owner.call('POST', `/api/admin/pages/${slug}/sources`, { url: 'https://example.com/b', type: 'website' });
  await h.reached;
  assert.equal((await other.call('PATCH', `/api/admin/pages/${slug}`, { language: 'Spanish' })).status, 200);
  h.release();
  assert.equal((await add).status, 200);
  p = await pageOf(slug);
  assert.equal(p.source_count, 2);
  assert.equal(p.language, 'Spanish', 'the setting saved meanwhile is kept');
  // a settings save that read the page before an upload finished keeps the new document version
  h = holdNextRead(new RegExp(`db/pages/${slug}/`));
  const logo = owner.call('PATCH', `/api/admin/pages/${slug}`, { theme: { logo_text: 'DUMMY' } });
  await h.reached;
  const up = await other.call('POST', `/api/admin/pages/${slug}/docs/tone`, { filename: 'tone.pdf', data_b64: pdf.toString('base64') });
  assert.equal(up.status, 200, JSON.stringify(up.body));
  h.release();
  assert.equal((await logo).status, 200);
  p = await pageOf(slug);
  assert.equal(p.docs.tone.active, 1, 'the version the upload made is kept');
  assert.equal(p.theme.logo_text, 'DUMMY');
  // the source sheet, saved while the settings were
  h = holdNextRead(new RegExp(`db/pages/${slug}/`));
  const sheet = owner.call('PUT', `/api/admin/pages/${slug}/sheet`, { categories: [{ category: 'Teams', entities: ['Dummy FC'] }] });
  await h.reached;
  assert.equal((await other.call('PATCH', `/api/admin/pages/${slug}`, { fb_page_id: '555' })).status, 200);
  h.release();
  assert.equal((await sheet).status, 200);
  p = await pageOf(slug);
  assert.equal(p.fb_page_id, '555');
  assert.equal(p.source_count, 2);
  assert.equal((await owner.call('GET', `/api/admin/pages/${slug}/sheet`)).body.categories[0].category, 'Teams');
});

test('a user deactivated while they sign in stays deactivated', async () => {
  const { id } = await invite('race@example.com');
  const ed = client();
  const h = holdNextRead(new RegExp(`db/users/${id}/`));
  const signin = ed.call('POST', '/api/auth/login', { email: 'race@example.com', password: PW });
  await h.reached;
  assert.equal((await owner.call('PATCH', `/api/admin/users/${id}`, { active: false })).body.user.active, false);
  h.release();
  await signin;
  assert.equal((await userOf('race@example.com')).active, false);
  assert.equal((await ed.call('GET', '/api/auth/me')).status, 401, 'the new session does not work');
});

test('saving a user\'s pages keeps an archived page the row could not show', async () => {
  const second = (await owner.call('POST', '/api/admin/pages', { name: 'Second Page', workspace: WS })).body.page.slug;
  const { id } = await invite('pages@example.com', { pages: [slug, second] });
  await owner.call('PATCH', `/api/admin/pages/${second}`, { archived: true });
  assert.equal((await owner.call('PATCH', `/api/admin/users/${id}`, { pages: [slug] })).status, 200);
  await owner.call('PATCH', `/api/admin/pages/${second}`, { archived: false });
  assert.deepEqual((await userOf('pages@example.com')).pages, [slug, second]);
  // unticking a page that is not archived still removes it
  assert.deepEqual((await owner.call('PATCH', `/api/admin/users/${id}`, { pages: [second] })).body.user.pages, [second]);
  // a role change sends no pages, and keeps them
  assert.deepEqual((await owner.call('PATCH', `/api/admin/users/${id}`, { role: 'owner' })).body.user.pages, [second]);
});

test('two admins giving a user pages: each one\'s ticks and unticks are kept', async () => {
  const second = (await owner.call('POST', '/api/admin/pages', { name: 'Grant Two', workspace: WS })).body.page.slug;
  const third = (await owner.call('POST', '/api/admin/pages', { name: 'Grant Three', workspace: WS })).body.page.slug;
  const { id } = await invite('grants@example.com');
  // admin B gives the second page; admin A, whose row still shows only the first, ticks the third
  assert.deepEqual((await owner.call('PATCH', `/api/admin/users/${id}`, { pages_add: [second] })).body.user.pages, [slug, second]);
  assert.deepEqual((await owner.call('PATCH', `/api/admin/users/${id}`, { pages_add: [third] })).body.user.pages, [slug, second, third]);
  // unticking one page removes that page only
  assert.deepEqual((await owner.call('PATCH', `/api/admin/users/${id}`, { pages_remove: [slug] })).body.user.pages, [second, third]);
  assert.deepEqual((await userOf('grants@example.com')).pages, [second, third]);
});

test('a lock that has ended starts the count again; the try that locks says so', async () => {
  const { id } = await invite('lock@example.com');
  const c = client();
  const wrong = () => c.call('POST', '/api/auth/login', { email: 'lock@example.com', password: 'nope nope 1' });
  for (let i = 0; i < 4; i++) assert.equal((await wrong()).status, 401);
  const fifth = await wrong();
  assert.equal(fifth.status, 429);
  assert.match(fifth.body.error, /too many attempts, try again in 15 min/);
  const real = Date.now;
  Date.now = () => real() + 16 * 60e3;          // the 15 minutes have passed
  try {
    assert.equal((await wrong()).status, 401, 'one typo after the lock is one try, not a new lock');
    assert.equal((await c.call('POST', '/api/auth/login', { email: 'lock@example.com', password: PW })).status, 200);
  } finally { Date.now = real; }
  assert.ok(id);
});

test('change password: after a lock it says so, not "current password is wrong"', async () => {
  const { c } = await invite('pw@example.com');
  for (let i = 0; i < 4; i++) await c.call('POST', '/api/auth/password', { current: 'nope nope 1', password: 'new pass word 9' });
  const fifth = await c.call('POST', '/api/auth/password', { current: 'nope nope 1', password: 'new pass word 9' });
  assert.equal(fifth.status, 429);
  assert.equal(fifth.body.error, 'current password is wrong: too many attempts, try again in 15 min', 'no email was typed here');
  const r = await c.call('POST', '/api/auth/password', { current: PW, password: 'new pass word 9' });
  assert.equal(r.status, 429);
  assert.match(r.body.error, /too many attempts/);
});

test('a deactivated user gets no reset link, and a link issued before cannot set a password', async () => {
  const { id } = await invite('off@example.com');
  const link = (await owner.call('POST', `/api/admin/users/${id}/reset`, {})).body.setup_link;
  await owner.call('PATCH', `/api/admin/users/${id}`, { active: false });
  const r = await owner.call('POST', `/api/admin/users/${id}/reset`, {});
  assert.equal(r.status, 400);
  assert.match(r.body.error, /deactivated: press Activate first/);
  const set = await client().call('POST', '/api/auth/reset', { token: token(link), password: 'another pass 42' });
  assert.equal(set.status, 403);
  assert.match(set.body.error, /deactivated: ask your admin/);
  for (let i = 0; i < 6; i++) {
    const r = await client().call('POST', '/api/auth/login', { email: 'off@example.com', password: PW });
    assert.equal(r.status, 403, 'the right password: told the account is deactivated, and it is not a wrong try');
    assert.equal(r.body.error, 'this account is deactivated: ask your admin to activate it');
  }
  assert.equal((await client().call('POST', '/api/auth/login', { email: 'off@example.com', password: 'nope nope 1' })).body.error,
               'wrong email or password');
  await owner.call('PATCH', `/api/admin/users/${id}`, { active: true });
  assert.equal((await client().call('POST', '/api/auth/login', { email: 'off@example.com', password: PW })).status, 200, 'never locked');
});

test('"asked for a reset" goes once the user has set a new password', async () => {
  const { id } = await invite('forgot@example.com');
  await client().call('POST', '/api/auth/forgot', { email: 'forgot@example.com' });
  assert.ok((await userOf('forgot@example.com')).reset_requested);
  const link = (await owner.call('POST', `/api/admin/users/${id}/reset`, {})).body.setup_link;
  const weak = await client().call('POST', '/api/auth/reset', { token: token(link), password: 'short' });
  assert.equal(weak.status, 400);
  assert.ok(!weak.body.expired, 'a password too weak is not a dead link: the form stays');
  assert.equal((await client().call('POST', '/api/auth/reset', { token: token(link), password: 'fresh pass 42' })).status, 200);
  assert.equal((await userOf('forgot@example.com')).reset_requested, null);
  // the link used up says so (the screen then offers a new link and Sign in)
  const used = await client().call('POST', '/api/auth/reset', { token: token(link), password: 'fresh pass 43' });
  assert.equal(used.status, 400);
  assert.equal(used.body.expired, true);
  // and after signing in
  await client().call('POST', '/api/auth/forgot', { email: 'forgot@example.com' });
  assert.equal((await client().call('POST', '/api/auth/login', { email: 'forgot@example.com', password: 'fresh pass 42' })).status, 200);
  assert.equal((await userOf('forgot@example.com')).reset_requested, null);
});

test('lessons: each admin\'s weights and notes are kept; out-of-range weights are refused; reset keeps weights', async () => {
  const L = `/api/admin/pages/${slug}/lessons`;
  const other = client();
  await other.call('POST', '/api/auth/login', { email: 'owner@example.com', password: PW });
  assert.equal((await owner.call('PUT', L, { admin_notes: 'no jersey numbers' })).status, 200);
  assert.equal((await other.call('PUT', L, { overrides: { injury: 1.2 } })).status, 200);
  assert.equal((await owner.call('PUT', L, { overrides: { trade: 0.5 } })).status, 200);
  let l = (await owner.call('GET', L)).body.lessons;
  assert.deepEqual(l.overrides, { injury: 1.2, trade: 0.5 });
  assert.equal(l.admin_notes, 'no jersey numbers');
  const far = await owner.call('PUT', L, { overrides: { injury: 2, trade: 0 } });
  assert.equal(far.status, 400);
  assert.match(far.body.error, /weights go from 0\.3 to 1\.5/);
  assert.deepEqual((await owner.call('GET', L)).body.lessons.overrides, { injury: 1.2, trade: 0.5 }, 'nothing saved');
  for (const v of ['abc', [], true, {}, ' ']) {
    const r = await owner.call('PUT', L, { overrides: { trade: v } });
    assert.equal(r.status, 400, JSON.stringify(v));
    assert.equal(r.body.error, 'weights are numbers from 0.3 to 1.5 (trade was not a number): nothing was saved');
  }
  assert.equal((await owner.call('PUT', L, { overrides: { trade: '0.6' } })).body.lessons.overrides.trade, 0.6, 'a number typed as text');
  await owner.call('PUT', L, { overrides: { trade: 0.5 } });
  assert.deepEqual((await owner.call('PUT', L, { overrides: { trade: null } })).body.lessons.overrides, { injury: 1.2 }, 'back on auto');
  // the routine's rebuild, sent while an admin saved a note, keeps the note
  const h = holdNextRead(new RegExp(`db/lessons/${slug}/`));
  const build = robot.call('POST', '/api/routine/lessons', { page: slug, record: { kinds: { injury: { mult: 1.1 } } } }, R);
  await h.reached;
  assert.equal((await owner.call('PUT', L, { admin_notes: 'stars only' })).status, 200);
  h.release();
  assert.equal((await build).status, 200);
  l = (await owner.call('GET', L)).body.lessons;
  assert.equal(l.admin_notes, 'stars only');
  assert.deepEqual(l.kinds, { injury: { mult: 1.1 } });
  const reset = (await owner.call('POST', `${L}/reset`, { confirm: true })).body.lessons;
  assert.deepEqual(reset.overrides, { injury: 1.2 }, 'the admins\' weights stay, like their notes');
  assert.equal(reset.admin_notes, 'stars only');
});

test('hourly approvals: an editor sees and decides only their own pages\' posts', async () => {
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const stamp = new Date(Date.now() - 60e3).toISOString().replace(/\D/g, '').slice(0, 12);
  const ids = ['a', 'b', 'c'].map(s => `${slug}-${stamp}-access-${s}`);
  for (const id of ids) { await saveDraft({ id, page: slug, page_name: 'Dummy', caption: 'x', headline: id }, tinyJpeg); await setStatus(id, 'pending', {}); }
  const { c: ed, id: edId } = await invite('removed@example.com');
  assert.ok((await ed.call('GET', '/api/drafts?status=pending')).body.drafts.some(d => d.id === ids[0]));
  await owner.call('PATCH', `/api/admin/users/${edId}`, { pages: [] });
  assert.equal((await ed.call('GET', '/api/drafts?status=pending')).body.drafts.length, 0, 'the page is no longer theirs');
  const ok = await ed.call('POST', '/api/decide', { id: ids[0], decision: 'approve' });
  assert.equal(ok.status, 403);
  assert.match(ok.body.error, /page you no longer have/);
  assert.equal((await ed.call('POST', '/api/decide', { ids: [ids[1]], decision: 'reject_many', reason: 'Weak story' })).body.rejected, 0);
  assert.equal((await ed.call('POST', '/api/drafts?action=clear', { ids: [ids[2]] })).body.cleared, 0);
  const redo = await ed.call('POST', '/api/feedback', { id: ids[0], feedback: 'make it better' });
  assert.equal(redo.status, 403, 'nor sent back to be redone');
  assert.match(redo.body.error, /page you no longer have/);
  const left = (await robot.call('GET', '/api/drafts?status=pending', undefined, R)).body.drafts.map(d => d.id);
  for (const id of ids) assert.ok(left.includes(id), `${id} still waiting`);
  assert.ok((await owner.call('GET', '/api/drafts?status=pending')).body.drafts.some(d => d.id === ids[0]), 'admins see every page');
});

test('storage refusing a write: the editor is told in plain words, never the library\'s message', async () => {
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const id = `${slug}-${new Date(Date.now() - 60e3).toISOString().replace(/\D/g, '').slice(0, 12)}-refused`;
  await saveDraft({ id, page: slug, page_name: 'Dummy', caption: 'x', headline: id }, tinyJpeg);
  await setStatus(id, 'pending', {});
  const mem = globalThis.__boreBlobs, set = mem.set;
  mem.set = function (k, v) { if (/-approved\.json$/.test(k)) throw new Error('Vercel Blob: injected put failure'); return set.call(this, k, v); };
  try {
    const r = await owner.call('POST', '/api/decide', { id, decision: 'approve' });
    assert.equal(r.status, 503);
    assert.doesNotMatch(r.body.error, /Vercel Blob|injected/);
    assert.match(r.body.error, /storage is not answering.*check whether it went through/);
  } finally { mem.set = set; }
});

test('post from a link: any page added in Admin that the editor has, and no other', async () => {
  const { c: ed } = await invite('links@example.com');
  const made = await ed.call('POST', '/api/links', { url: 'https://example.com/story-1', page: slug });
  assert.equal(made.status, 201, JSON.stringify(made.body));
  assert.equal(made.body.link.page_name, 'Dummy Sports', 'the name set in Admin');
  const other = (await owner.call('POST', '/api/admin/pages', { name: 'Not Theirs', workspace: WS })).body.page.slug;
  await makeLive(other);
  assert.equal((await ed.call('POST', '/api/links', { url: 'https://example.com/story-2', page: other })).status, 400);
  assert.equal((await ed.call('POST', '/api/links', { url: 'https://example.com/story-2', page: 'nowhere' })).status, 400);
  assert.equal((await owner.call('POST', '/api/links', { url: 'https://example.com/story-2', page: other })).status, 201, 'admins: every page');
  await owner.call('PATCH', `/api/admin/pages/${other}`, { archived: true });
  assert.equal((await owner.call('POST', '/api/links', { url: 'https://example.com/story-3', page: other })).status, 400, 'not an archived page');
});

test('an invite from the users screen (pages, no workspace) joins the workspace of those pages; pages of two workspaces are refused', async () => {
  const r = await owner.call('POST', '/api/admin/users', { email: 'screen-invite@example.com', role: 'editor', pages: [slug] });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.deepEqual([r.body.user.workspace, r.body.user.pages], [WS, [slug]]);
  const other = (await workspace(owner, 'Second Demo Workspace')).id;
  const theirs = (await owner.call('POST', '/api/admin/pages', { name: 'Second Demo Page', workspace: other })).body.page.slug;
  for (const pages of [[slug, theirs], []]) {
    const x = await owner.call('POST', '/api/admin/users', { email: `mixed-${pages.length}@example.com`, role: 'editor', pages });
    assert.equal(x.status, 400);
    assert.equal(x.body.error, 'choose the workspace the user belongs to');
  }
  assert.equal((await owner.call('POST', '/api/admin/users', { email: 'second-admin@example.com', role: 'super' })).body.user.workspace, null);
});
