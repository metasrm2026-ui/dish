// Workspaces (docs/PRODUCT.md "Roles"): an owner or an editor of workspace A sees and changes nothing of
// workspace B, in lists, in counts and by direct ids: pages, drafts, stats, links, notes, health
// reasons, Studio feed, batches and jobs, templates, documents and audit. The product owner sees all.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { client, signedInSuper, workspace, setPassword, makeLive } from './helpers.mjs';
import { putRecord } from '../lib/db.js';
import { setNeedsAction } from '../routes/result.js';
import { health } from '../routes/health.js';

const R = { routine: true };
const pdf = readFileSync(new URL('./fixtures/dummy_templates.pdf', import.meta.url)).toString('base64');
const jpeg = '/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=';
const stamp = () => new Date().toISOString().replace(/\D/g, '').slice(0, 12);
const robot = client();
const A = {}, B = {};
let sup;

// a workspace with its owner, one live page with a draft, a story feed, a link, a batch and its
// documents, and an editor of that page
async function build(x, name, login) {
  const w = await workspace(sup, `${name} Workspace`);
  x.ws = w.id;
  x.owner = client();
  assert.equal((await setPassword(x.owner, w.link, 'owner pass 42')).status, 200);
  x.page = (await x.owner.call('POST', '/api/pages', { name: `${name} Page`, fb_page_url: `facebook.com/${name.toLowerCase()}page`, niche: 'Made-up news.' })).body.page.slug;
  x.second = (await x.owner.call('POST', '/api/pages', { name: `${name} Second` })).body.page.slug;
  await x.owner.call('POST', `/api/pages/${x.page}/docs/templates`, { filename: `${name}-templates.pdf`, data_b64: pdf });
  await makeLive(x.page, login);
  await makeLive(x.second, login + 1);
  const inv = await x.owner.call('POST', '/api/workspace/editors', { email: `${name.toLowerCase()}-editor@example.com`, pages: [x.page] });
  x.editorId = inv.body.user.id;
  x.editor = client();
  assert.equal((await setPassword(x.editor, inv.body.setup_link, 'editor pass 77')).status, 200);
  x.draft = `${x.page}-${stamp()}-${name.toLowerCase()}`;
  assert.equal((await robot.call('POST', '/api/drafts', { id: x.draft, page: x.page, page_name: `${name} Page`, caption: `${name} caption`, image_b64: jpeg }, R)).status, 201);
  await setNeedsAction(x.draft, x.page, `${name} note: look in SocialPilot`);
  await robot.call('POST', '/api/routine/feed', { page: x.page, stories: [{ id: `${name}-s1`, headline: `${name} story`, summary: 'S.', score: 5,
    published_at: Date.now(), url: `https://example.com/${name}`, sources: [{ name: 'E', url: `https://example.com/${name}` }],
    ideas: [{ title: 'A', template: 'headline', description: 'a' }], images: [{ url: `https://img.example.com/${name}.jpg`, source: 'web' }] }] }, R);
  assert.equal((await x.editor.call('POST', '/api/links', { url: `https://news.example.com/${name}`, page: x.page })).status, 201);
  const bt = await x.editor.call('POST', '/api/studio/batches', { page: x.page, stories: [{ id: `${name}-s1` }] });
  assert.equal(bt.status, 200, JSON.stringify(bt.body));
  x.batch = bt.body.batch.id;
  x.job = `${x.page}-${Date.now() - 120e3}-nf`;
  await putRecord('jobs', x.job, { id: x.job, kind: 'ideas', page: x.page, status: 'queued', created_at: Date.now() - 120e3, fired: false, fire_status: 403 });
}

test.before(async () => {
  sup = await signedInSuper(robot);
  await build(A, 'Alpha', 700300);
  await build(B, 'Beta', 700400);
  await health({ fresh: true });
});

const asWhoInA = () => [['owner', A.owner], ['editor', A.editor]];

test('lists and counts: only the person\'s own workspace', async () => {
  for (const [who, c] of asWhoInA()) {
    const mine = who === 'owner' ? [A.page, A.second].sort() : [A.page];
    assert.deepEqual((await c.call('GET', '/api/pages')).body.pages.map(p => p.slug).sort(), mine, who);
    assert.deepEqual((await c.call('GET', '/api/auth/me')).body.pages.map(p => p.slug).sort(), mine, who);
    assert.deepEqual((await c.call('GET', '/api/studio/pages')).body.pages.map(p => p.slug).sort(), mine, who);
    const drafts = (await c.call('GET', '/api/drafts?status=all')).body.drafts.map(d => d.id);
    assert.ok(drafts.includes(A.draft) && !drafts.includes(B.draft), who);
    const stats = (await c.call('GET', '/api/stats')).body;
    assert.equal(Object.values(stats.totals).reduce((n, v) => n + v, 0), 1, `${who}: one post counted, their own`);
    assert.deepEqual(Object.keys(stats.publisher.needs_action), [A.draft], `${who}: only their own notes`);
    const links = (await c.call('GET', '/api/links?status=all')).body.links;
    assert.deepEqual(links.map(l => l.page), [A.page], who);
    const batches = (await c.call('GET', '/api/studio/batches')).body.batches;
    assert.ok(batches.every(x => x.page === A.page) && batches.some(x => x.id === A.batch), who);
    const h = (await c.call('GET', '/api/health')).body;
    const fire = h.reasons.find(r => r.code === 'studio_fire_failed');
    assert.ok(fire, `${who}: their own job's reason`);
    assert.ok(!JSON.stringify(h).includes(B.page) && !JSON.stringify(h).includes('Beta'), `${who}: nothing of the other workspace's in health`);
  }
  // the owner's workspace screen and audit: only theirs
  const ws = (await A.owner.call('GET', '/api/workspace')).body;
  assert.deepEqual([ws.workspace.id, ws.pages.map(p => p.slug).sort()], [A.ws, [A.page, A.second].sort()]);
  assert.ok(ws.users.every(u => u.workspace === A.ws));
  const audit = (await A.owner.call('GET', '/api/workspace/audit')).body.entries;
  assert.ok(audit.length && !JSON.stringify(audit).includes(B.page) && !JSON.stringify(audit).includes('beta-'), 'no entry of the other workspace');
  assert.equal((await A.editor.call('GET', '/api/workspace')).status, 403, 'an editor has no workspace screen');
  assert.equal((await A.owner.call('GET', `/api/workspace?workspace=${B.ws}`)).body.workspace.id, A.ws, 'naming another workspace changes nothing');
  // the product owner: everything
  const all = (await sup.call('GET', '/api/drafts?status=all')).body.drafts.map(d => d.id);
  assert.ok(all.includes(A.draft) && all.includes(B.draft));
  assert.equal(Object.keys((await sup.call('GET', '/api/stats')).body.publisher.needs_action).length, 2);
});

test('direct ids of the other workspace: nothing to see, nothing to change', async () => {
  for (const [who, c] of asWhoInA()) {
    const no = async (method, path, body) => {
      const r = await c.call(method, path, body);
      assert.ok([403, 404].includes(r.status), `${who} ${method} ${path}: ${r.status}`);
      assert.ok(!JSON.stringify(r.body).includes('Beta'), `${who} ${method} ${path}: says nothing of it`);
      return r;
    };
    assert.equal((await no('GET', `/api/pages/${B.page}`)).status, 404, 'another workspace\'s page is no page at all');
    await no('GET', `/api/pages/${B.page}/onboarding`);
    await no('PATCH', `/api/pages/${B.page}`, { niche: 'taken over' });
    await no('POST', `/api/pages/${B.page}/pause`, {});
    await no('POST', `/api/pages/${B.page}/go-live`, {});
    await no('GET', `/api/pages/${B.page}/docs/templates`);
    await no('POST', `/api/pages/${B.page}/docs/tone`, { filename: 't.pdf', data_b64: pdf });
    await no('GET', `/api/pages/${B.page}/sources`);
    await no('GET', `/api/admin/pages/${B.page}/sources`);
    await no('GET', `/api/studio/feed?page=${B.page}`);
    await no('POST', '/api/studio/batches', { page: B.page, stories: [{ id: 'Beta-s1' }] });
    assert.equal((await no('GET', `/api/studio/batches/${B.batch}`)).status, 404);
    await no('POST', `/api/studio/batches/${B.batch}/run`, { kind: 'compose', post_ids: [] });
    await no('GET', `/api/studio/jobs/${B.job}`);
    await no('GET', `/api/studio/schedule-hint?page=${B.page}`);
    await no('GET', `/api/templates?page=${B.page}`);
    await no('POST', `/api/templates/${B.page}/convert`, {});
    assert.equal((await no('POST', '/api/decide', { id: B.draft, decision: 'reject', reason: 'Other' })).status, 404);
    await no('POST', '/api/decide', { id: B.draft, decision: 'done' });
    await no('POST', '/api/feedback', { id: B.draft, feedback: 'make it better' });
    await no('POST', '/api/publish', { id: B.draft });
    assert.equal((await c.call('POST', '/api/links', { url: 'https://news.example.com/x', page: B.page })).status, 400, 'not a page to choose');
    assert.equal((await c.call('POST', '/api/studio/batches/clear', { ids: [B.batch] })).body.cleared, 0);
    assert.equal((await c.call('POST', '/api/drafts?action=clear', { ids: [B.draft] })).body.cleared || 0, 0);
    await no('PATCH', `/api/workspace/editors/${B.editorId}`, { pages_add: [A.page] });
    await no('POST', `/api/workspace/editors/${B.editorId}/reset`, {});
    await no('GET', '/api/admin/users');
  }
  // the other workspace's things are as they were
  const bPage = (await B.owner.call('GET', `/api/pages/${B.page}`)).body.page;
  assert.deepEqual([bPage.niche, bPage.status], ['Made-up news.', 'live']);
  assert.equal((await B.owner.call('GET', `/api/studio/batches/${B.batch}`)).body.batch.cleared, undefined);
  assert.ok((await B.owner.call('GET', '/api/drafts?status=pending')).body.drafts.some(d => d.id === B.draft));
});

test('an editor sees only the pages of their workspace they were given', async () => {
  const no = await A.editor.call('GET', `/api/studio/feed?page=${A.second}`);
  assert.equal(no.status, 403, 'one of their own workspace\'s pages they were not given is said so');
  assert.equal((await A.editor.call('GET', `/api/pages/${A.second}`)).status, 404, 'not theirs to open either');
  // given it by their owner: there at once
  await A.owner.call('PATCH', `/api/workspace/editors/${A.editorId}`, { pages_add: [A.second] });
  assert.equal((await A.editor.call('GET', `/api/studio/feed?page=${A.second}`)).status, 200);
  await A.owner.call('PATCH', `/api/workspace/editors/${A.editorId}`, { pages_remove: [A.second] });
});
