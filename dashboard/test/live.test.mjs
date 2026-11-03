// Publishing only ever uses a live page's own SocialPilot account, linked by the onboarding routine,
// never one on the deny list (docs/PRODUCT.md "Safety rules"); a paused page gets no posts made, none
// approved and none scheduled until it is resumed. Accounts here are made up (fixtures/socialpilot.yaml).
import test from 'node:test';
import assert from 'node:assert/strict';
import { client, signedInSuper, adminPage, setPageStatus, makeLive } from './helpers.mjs';
import { updateRecord } from '../lib/db.js';

const R = { routine: true };
const jpeg = '/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=';
const robot = client();
let sup, n = 0;
const stamp = () => new Date().toISOString().replace(/\D/g, '').slice(0, 12);
async function draft(page) {
  const id = `${page}-${stamp()}-p${++n}`;
  const r = await robot.call('POST', '/api/drafts', { id, page, page_name: page, caption: 'A made-up caption.', image_b64: jpeg }, R);
  assert.equal(r.status, 201, JSON.stringify(r.body));
  return id;
}
const approve = id => sup.call('POST', '/api/decide', { id, decision: 'approve' });
const claim = id => robot.call('POST', '/api/result', { id, status: 'publishing', scheduled_for: new Date(Date.now() + 3 * 3600e3).toISOString() }, R);
const queue = async () => (await robot.call('GET', '/api/queue', undefined, R)).body;
test.before(async () => { sup = await signedInSuper(robot); });

test('a paused page: no posts made, none approved, its approved ones wait outside the queue until it is resumed', async () => {
  const slug = await adminPage(sup, 'Paused Demo', '700501');
  const before = await draft(slug);
  const waiting = await draft(slug);
  assert.equal((await approve(waiting)).status, 200);
  await setPageStatus(slug, 'paused');
  const id = `${slug}-${stamp()}-late`;
  const made = await robot.call('POST', '/api/drafts', { id, page: slug, caption: 'c', image_b64: jpeg }, R);
  assert.equal(made.status, 409);
  assert.match(made.body.error, /the page is paused/);
  const r = await approve(before);
  assert.equal(r.status, 409);
  assert.match(r.body.error, /^not approved: the page is paused/);
  let q = await queue();
  assert.ok(!q.queue.some(x => x.id === waiting), 'not offered to the publisher');
  assert.deepEqual(q.waiting.find(x => x.id === waiting), { id: waiting, page: slug, why: 'the page is paused: its posts go out once it is resumed' });
  const c = await claim(waiting);
  assert.equal(c.status, 409);
  assert.match(c.body.error, /^not publishing: the page is paused/);
  // resumed: it goes out as before
  await setPageStatus(slug, 'live');
  q = await queue();
  assert.equal(q.queue.find(x => x.id === waiting).socialpilot_login_id, '700501');
  assert.equal((await claim(waiting)).status, 200);
  assert.equal((await approve(before)).status, 200);
});

test('a page that is not live, or has no account, never publishes; ids for another page are refused', async () => {
  const slug = await adminPage(sup, 'Draft Demo', null);
  const id = `${slug}-${stamp()}-x`;
  for (const status of ['draft', 'failed']) {
    await setPageStatus(slug, status);
    assert.equal((await robot.call('POST', '/api/drafts', { id, page: slug, caption: 'c', image_b64: jpeg }, R)).status, 409, status);
  }
  // going live: the first run makes the page's first drafts, but nothing is approved until it is live
  await setPageStatus(slug, 'going_live');
  const first = await draft(slug);
  const r = await approve(first);
  assert.equal(r.status, 409);
  assert.match(r.body.error, /not live yet/);
  // live without an account (never so through the onboarding routine): still refused
  await setPageStatus(slug, 'live');
  assert.match((await approve(first)).body.error, /no SocialPilot account linked/);
  // a draft id must name its page: another page's posts are never made under this one's
  const other = await adminPage(sup, 'Other Demo', '700502');
  const wrong = await robot.call('POST', '/api/drafts', { id: `${other}-${stamp()}-y`, page: slug, caption: 'c', image_b64: jpeg }, R);
  assert.equal(wrong.status, 400);
});

test('an account on the deny list is refused everywhere, whatever the page record says', async () => {
  const slug = await adminPage(sup, 'Denied Demo', '700503');
  const id = await draft(slug);
  assert.equal((await approve(id)).status, 200);
  // a record changed behind the dashboard's back: the account is on the deny list (the file's, the settings')
  for (const login of ['900002', '900003']) {
    await updateRecord('pages', slug, cur => ({ ...cur, socialpilot_login_id: login }));
    const q = await queue();
    assert.ok(!q.queue.some(x => x.id === id), login);
    assert.match(q.waiting.find(x => x.id === id).why, /deny list/);
    assert.match((await claim(id)).body.error, /deny list/);
    const cfg = (await robot.call('GET', '/api/routine/config', undefined, R)).body.pages.find(p => p.slug === slug);
    assert.deepEqual([cfg.socialpilot_login_id, cfg.socialpilot_blocked], ['', true], 'never handed to a routine');
  }
  await makeLive(slug, '700503');
  assert.equal((await claim(id)).status, 200);
});

test('the routines\' config says each page\'s status: only live pages are worked', async () => {
  const live = await adminPage(sup, 'Config Live', '700504');
  const paused = await adminPage(sup, 'Config Paused', '700505');
  await setPageStatus(paused, 'paused');
  const draftPage = await adminPage(sup, 'Config Draft', null);
  const cfg = (await robot.call('GET', '/api/routine/config', undefined, R)).body;
  const st = Object.fromEntries(cfg.pages.map(p => [p.slug, p.status]));
  assert.deepEqual([st[live], st[paused], st[draftPage]], ['live', 'paused', 'draft']);
  assert.ok(cfg.deny.includes('900001') && cfg.deny.includes('900003'));
  for (const p of cfg.pages) for (const k of ['workspace', 'posts_per_day', 'premium_per_day', 'socialpilot_login_id', 'onboarding']) assert.ok(k in p, k);
  // a paused page gets no story feed and no premium cards
  assert.equal((await robot.call('POST', '/api/routine/feed', { page: paused, stories: [] }, R)).status, 409);
  assert.equal((await robot.call('POST', '/api/routine/krea', { who: 'hourly', cap: 100, page: paused, confirms: true }, R)).status, 409);
});
