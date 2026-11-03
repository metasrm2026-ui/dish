// Open access (the owner's instruction for now: "nothing to sign up, no password: click the link and
// view"): with OPEN_ACCESS=1 a request without a session is the product owner "Owner", made on first
// use; workspaces, roles and every safety rule still apply. Off, sign-in works as before.
import test from 'node:test';
import assert from 'node:assert/strict';
import { client, workspace, setPassword } from './helpers.mjs';
import { allUsers, OPEN_WORKSPACE } from '../lib/session.js';
import { DEFAULTS } from '../lib/defaults.js';
import { readLogs } from '../lib/db.js';

const R = { routine: true };
const open = fn => async () => {
  process.env.OPEN_ACCESS = '1';
  try { await fn(); } finally { process.env.OPEN_ACCESS = '0'; }
};

test('this deployment opens straight in unless the hosting provider\'s settings turn it off', async () => {
  assert.equal(DEFAULTS.OPEN_ACCESS, '1');
  delete process.env.OPEN_ACCESS;
  try {
    assert.equal((await client().call('GET', '/api/auth/me')).status, 200, 'no setting: the deployment\'s default, open');
  } finally { process.env.OPEN_ACCESS = '0'; }
  assert.equal((await client().call('GET', '/api/auth/me')).status, 401, 'OPEN_ACCESS=0: sign in first, as before');
  assert.equal((await client().call('GET', '/api/drafts?status=pending')).status, 401);
});

test('with open access a visitor is the owner, made once, with a workspace of its own', open(async () => {
  const a = client(), b = client();
  const me = await a.call('GET', '/api/auth/me');
  assert.equal(me.status, 200);
  assert.deepEqual([me.body.user.name, me.body.user.role, me.body.user.workspace, me.body.user.must_change, me.body.open_access],
                   ['Owner', 'super', OPEN_WORKSPACE, false, true]);
  assert.equal(me.body.workspace.id, OPEN_WORKSPACE);
  assert.equal(a.cookie, '', 'no session is made: nothing to sign in to');
  const all = await Promise.all([b.call('GET', '/api/auth/me'), client().call('GET', '/api/auth/me')]);
  assert.ok(all.every(r => r.body.user.id === me.body.user.id));
  assert.equal((await allUsers()).filter(u => u.open_access).length, 1, 'one Owner, however many visitors');
  assert.equal((await readLogs('audit', 1)).filter(e => e.action === 'open_owner_created').length, 1);
  // the owner's screens: Admin, its own pages
  assert.equal((await a.call('GET', '/api/admin/users')).status, 200);
  const pg = await a.call('POST', '/api/pages', { name: 'Open Demo Page' });
  assert.deepEqual([pg.status, pg.body.page.workspace, pg.body.page.status], [200, OPEN_WORKSPACE, 'draft']);
  // a tester named their workspace like the owner's: theirs is their own
  const w = await a.call('POST', '/api/admin/testers', { email: 'open-tester@example.com', workspace_name: OPEN_WORKSPACE });
  assert.notEqual(w.body.workspace.id, OPEN_WORKSPACE);
}));

test('with open access the safety rules and the roles still hold, and routines still need their secret', open(async () => {
  const owner = client();
  const slug = (await owner.call('POST', '/api/pages', { name: 'Open Rules Page' })).body.page.slug;
  // Go live still needs everything it needs
  const r = await owner.call('POST', `/api/pages/${slug}/go-live`, {});
  assert.equal(r.status, 400);
  assert.ok(r.body.missing.includes('templates'));
  // a post of a page that is not live is never approved
  const id = `${slug}-${new Date().toISOString().replace(/\D/g, '').slice(0, 12)}-demo`;
  assert.equal((await client().call('POST', '/api/drafts', { id, page: slug, caption: 'c', image_b64: 'AA==' }, R)).status, 409,
               'no post is made for a page that is not live');
  // the routine API: never the owner's, only with ROUTINE_SECRET
  assert.equal((await client().call('GET', '/api/routine/config')).status, 401);
  assert.equal((await client().call('GET', '/api/routine/onboard/jobs')).status, 401);
  assert.equal((await client().call('GET', '/api/routine/config', undefined, R)).status, 200);
  // someone who signs in is themselves: a tester sees their own workspace only, never Admin
  const w = await workspace(owner, 'Open Tester Workspace');
  const tester = client();
  assert.equal((await setPassword(tester, w.link, 'tester pass 42')).status, 200);
  const me = (await tester.call('GET', '/api/auth/me')).body.user;
  assert.deepEqual([me.role, me.workspace], ['owner', w.id]);
  assert.equal((await tester.call('GET', '/api/admin/users')).status, 403);
  assert.equal((await tester.call('GET', `/api/pages/${slug}`)).status, 404, 'the owner\'s page is not theirs');
  // signed out: the owner again
  await tester.call('POST', '/api/auth/logout', {});
  assert.equal((await tester.call('GET', '/api/auth/me')).body.user.name, 'Owner');
}));

test('a deactivated open-access owner opens nothing', open(async () => {
  const owner = client();
  const id = (await owner.call('GET', '/api/auth/me')).body.user.id;
  // a second product owner (a signed-in one) deactivates it
  const second = (await owner.call('POST', '/api/admin/users', { email: 'second-super@example.com', role: 'super' })).body;
  const s = client();
  await setPassword(s, second.setup_link, 'second pass 42');
  assert.equal((await s.call('PATCH', `/api/admin/users/${id}`, { active: false })).status, 200);
  assert.equal((await client().call('GET', '/api/auth/me')).status, 401);
  await s.call('PATCH', `/api/admin/users/${id}`, { active: true });
  assert.equal((await client().call('GET', '/api/auth/me')).status, 200);
}));
