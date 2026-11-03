// What the screens read besides the page itself: the product owner's SocialPilot instructions (Admin >
// Settings), shown to owners on a page's "Connect in SocialPilot" step, and each page's posts-a-day
// budget in /api/auth/me, which the approvals page counts against. Everything here is made up.
import test from 'node:test';
import assert from 'node:assert/strict';
import { client, signedInSuper, workspace, setPassword } from './helpers.mjs';

const robot = client();
let sup, tester, WS;
test.before(async () => {
  sup = await signedInSuper(robot);
  const w = await workspace(sup, 'Settings Workspace');
  WS = w.id;
  tester = client();
  assert.equal((await setPassword(tester, w.link, 'tester pass 42')).status, 200);
});

test('the SocialPilot instructions are the super\'s to write and the page\'s owner reads them on the page', async () => {
  const made = await tester.call('POST', '/api/pages', { name: 'Connect Demo' });
  assert.equal(made.status, 200, JSON.stringify(made.body));
  const slug = made.body.page.slug;
  assert.equal((await tester.call('GET', `/api/pages/${slug}`)).body.connect_help, '', 'nothing written yet');

  const text = 'Open https://example.com/connect-demo and sign in with the account we sent you.\r\nThen add your page.';
  const r = await sup.call('PATCH', '/api/admin/settings', { socialpilot_connect: text });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.equal(r.body.settings.socialpilot_connect, text.replace('\r\n', '\n'));
  assert.equal((await sup.call('GET', '/api/admin/settings')).body.settings.socialpilot_connect, text.replace('\r\n', '\n'));
  assert.equal((await tester.call('GET', `/api/pages/${slug}`)).body.connect_help, text.replace('\r\n', '\n'));

  // an owner can neither read nor change the product's settings
  assert.equal((await tester.call('PATCH', '/api/admin/settings', { socialpilot_connect: 'mine' })).status, 403);
  assert.equal((await tester.call('GET', '/api/admin/settings')).status, 403);
  assert.equal((await sup.call('GET', '/api/admin/settings')).body.settings.socialpilot_connect, text.replace('\r\n', '\n'));
  assert.equal((await sup.call('PATCH', '/api/admin/settings', {})).status, 400, 'nothing sent is not a change');
});

test('each page in /api/auth/me carries its posts-a-day budget, as the routines work with it', async () => {
  const slug = (await tester.call('POST', '/api/pages', { name: 'Budget Demo' })).body.page.slug;
  const mine = async () => (await tester.call('GET', '/api/auth/me')).body.pages.find(p => p.slug === slug);
  assert.equal((await mine()).posts_per_day, 10, 'the default');
  assert.equal((await tester.call('PATCH', `/api/pages/${slug}`, { posts_per_day: 4 })).status, 200);
  assert.equal((await mine()).posts_per_day, 4, 'lowered by the owner');
  // the super lowers the workspace's limit below it: the lower one counts
  assert.equal((await sup.call('PATCH', `/api/admin/workspaces/${WS}`, { limits: { posts_per_day: 3 } })).status, 200);
  assert.equal((await mine()).posts_per_day, 3);
});
