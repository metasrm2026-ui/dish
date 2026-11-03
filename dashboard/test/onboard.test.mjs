// A tester's page from draft to live (docs/PRODUCT.md "A page's life"): the owner makes the page,
// uploads its documents and presses Go live; the onboarding routine claims the job, reports each step,
// links the page's own SocialPilot account (never one on the deny list, never another page's) and
// ends it. Pause, Resume and Try again. Everything here is made up.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { client, remote, signedInSuper, workspace, setPassword } from './helpers.mjs';
import { getRecord, updateRecord } from '../lib/db.js';

const R = { routine: true };
const pdf = readFileSync(new URL('./fixtures/dummy_templates.pdf', import.meta.url)).toString('base64');
// the onboarding routine's API trigger (lib/fire.js): what it was sent
process.env.ONBOARD_FIRE_URL = 'https://fire.example.test/onboard';
process.env.ONBOARD_FIRE_TOKEN = 'onboard-token';
remote.set(process.env.ONBOARD_FIRE_URL, { status: 200, body: '{}', type: 'application/json' });
const fired = [];
const realFetch = globalThis.fetch;
globalThis.fetch = async (url, opts) => {
  if (String(url) === process.env.ONBOARD_FIRE_URL) fired.push(JSON.parse(opts.body).text);
  return realFetch(url, opts);
};

const robot = client();
let sup, tester, WS, editor;
test.before(async () => {
  sup = await signedInSuper(robot);
  const w = await workspace(sup, 'Demo Workspace');
  WS = w.id;
  tester = client();
  assert.equal((await setPassword(tester, w.link, 'tester pass 42')).status, 200);
});

// a page ready to go live: its address, niche, the two documents and the tick
async function readyPage(name, c = tester) {
  const made = await c.call('POST', '/api/pages', { name, fb_page_url: `facebook.com/${name.toLowerCase().replace(/ /g, '')}`,
                                                   niche: 'Made-up news about a made-up hobby.', language: 'English', timezone: 'Europe/London' });
  assert.equal(made.status, 200, JSON.stringify(made.body));
  const slug = made.body.page.slug;
  for (const type of ['templates', 'tone'])
    assert.equal((await c.call('POST', `/api/pages/${slug}/docs/${type}`, { filename: `${type}.pdf`, data_b64: pdf })).status, 200);
  assert.equal((await c.call('PATCH', `/api/pages/${slug}`, { socialpilot_connected: true })).status, 200);
  return slug;
}
const jobs = async () => (await robot.call('GET', '/api/routine/onboard/jobs', undefined, R)).body;
const cfgOf = async slug => (await robot.call('GET', '/api/routine/config', undefined, R)).body.pages.find(p => p.slug === slug);
const on = (job, act, body = {}) => robot.call('POST', `/api/routine/onboard/jobs/${job}/${act}`, body, R);

test('a tester makes a page in their workspace: it starts as a draft, and settings the routines cannot read are refused', async () => {
  const r = await tester.call('POST', '/api/pages', { name: 'Demo Page', fb_page_url: 'https://www.facebook.com/demopage', niche: 'Made-up news.',
                                                     language: 'English', timezone: 'America/New_York', workspace: 'someone-else' });
  assert.equal(r.status, 200);
  assert.deepEqual([r.body.page.slug, r.body.page.workspace, r.body.page.status, r.body.page.posts_per_day, r.body.page.premium_per_day],
                   ['demo-page', WS, 'draft', 10, 6], 'their own workspace, whatever was sent; the default budgets');
  // the same name in another workspace gets a slug of its own (slugs are unique across the product)
  const other = await workspace(sup, 'Other Workspace');
  const o = client();
  await setPassword(o, other.link, 'other pass 42');
  assert.equal((await o.call('POST', '/api/pages', { name: 'Demo Page' })).body.page.slug, 'demo-page-2');
  for (const [body, re] of [[{ name: 'Bad Url', fb_page_url: 'https://example.com/page' }, /Facebook address/],
                            [{ name: 'Bad Zone', timezone: 'EST' }, /time zone/],
                            [{ name: 'Typed Login', socialpilot_login_id: '123' }, /linked by the onboarding routine/],
                            [{ name: '  ' }, /name the page/]]) {
    const x = await tester.call('POST', '/api/pages', body);
    assert.equal(x.status, 400, body.name);
    assert.match(x.body.error, re);
  }
  assert.match((await tester.call('PATCH', '/api/pages/demo-page', { socialpilot_login_id: '123' })).body.error, /linked by the onboarding routine/);
  assert.equal((await tester.call('GET', '/api/pages/demo-page')).body.page.socialpilot_login_id, '');
});

test('Go live: refused until the page has what it needs, then going live with one job and the routine woken', async () => {
  const made = await tester.call('POST', '/api/pages', { name: 'Ready Page' });
  const slug = made.body.page.slug;
  let r = await tester.call('POST', `/api/pages/${slug}/go-live`, {});
  assert.equal(r.status, 400);
  assert.deepEqual(r.body.missing, ['fb_page_url', 'niche', 'templates', 'tone', 'socialpilot_connected']);
  assert.match(r.body.error, /^Before going live: add the page's Facebook address; describe the page's niche/);
  await tester.call('PATCH', `/api/pages/${slug}`, { fb_page_url: 'facebook.com/readypage', niche: 'Made-up news.' });
  await tester.call('POST', `/api/pages/${slug}/docs/templates`, { filename: 't.pdf', data_b64: pdf });
  await tester.call('POST', `/api/pages/${slug}/docs/tone`, { filename: 'tone.pdf', data_b64: pdf });
  r = await tester.call('POST', `/api/pages/${slug}/go-live`, {});
  assert.deepEqual(r.body.missing, ['socialpilot_connected'], 'visual references are optional');
  assert.equal((await getRecord('pages', slug)).status, 'draft', 'nothing changed');
  await tester.call('PATCH', `/api/pages/${slug}`, { socialpilot_connected: true });

  const n = fired.length;
  r = await tester.call('POST', `/api/pages/${slug}/go-live`, {});
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.equal(r.body.page.status, 'going_live');
  assert.equal(r.body.fired, true);
  assert.equal(fired.length, n + 1);
  assert.match(fired[n], new RegExp(`^Onboard page ${slug} \\(job ${slug}-\\d{13}-`));
  const job = r.body.onboarding.job.id;
  assert.deepEqual([(await getRecord('onboard', job)).status, (await getRecord('onboard', job)).fired], ['queued', true]);
  assert.equal((await tester.call('POST', `/api/pages/${slug}/go-live`, {})).status, 409, 'pressed again: one job');
  assert.equal((await jobs()).jobs.filter(j => j.page.slug === slug).length, 1);
  // the live progress view: every step the routine will report, none done yet
  const view = (await tester.call('GET', `/api/pages/${slug}/onboarding`)).body.onboarding;
  assert.deepEqual([view.status, view.job.status, view.expected.map(s => s.step)],
                   ['going_live', 'queued', ['link_account', 'read_documents', 'research_sources', 'write_playbook', 'queue_templates', 'first_run']]);
  assert.ok(view.expected.every(s => s.ok === undefined));
  const audit = (await sup.call('GET', '/api/admin/audit')).body.entries.find(e => e.action === 'page_go_live' && e.page === slug);
  assert.deepEqual([audit.workspace, audit.job, audit.fired], [WS, job, true]);
});

test('the onboarding routine: one session per job, each step shown, the page\'s own account, then live', async () => {
  const slug = await readyPage('Onboard Page');
  const other = await readyPage('Taken Page');
  const job = (await tester.call('POST', `/api/pages/${slug}/go-live`, {})).body.onboarding.job.id;
  const otherJob = (await tester.call('POST', `/api/pages/${other}/go-live`, {})).body.onboarding.job.id;

  const list = await jobs();
  const j = list.jobs.find(x => x.id === job);
  assert.deepEqual([j.page.slug, j.page.workspace, j.page.niche, j.page.premium_per_day, Object.keys(j.page.docs).sort()],
                   [slug, WS, 'Made-up news about a made-up hobby.', 6, ['templates', 'tone']]);
  assert.ok(!JSON.stringify(j).includes('text'), 'the documents\' text comes from studio sync, not here');
  assert.ok(list.deny.includes('900001') && list.deny.includes('900003'), 'the deny list: the file and the settings');
  assert.equal((await on(job, 'step', { step: 'link_account' })).status, 409, 'claim it first');
  assert.equal((await on(job, 'claim', { session: 'a' })).status, 200);
  assert.equal((await on(job, 'claim', { session: 'b' })).status, 409, 'one session per job');
  assert.ok(!(await jobs()).jobs.some(x => x.id === job), 'a claimed job is not offered again');

  assert.equal((await on(job, 'step', { step: 'link_account', ok: true, detail: 'found the page in SocialPilot' })).status, 200);
  assert.equal((await on(job, 'step', { step: 'Link Account!' })).status, 400);
  // the account: never one on the deny list (the file, the settings, or the routine's own copy)
  for (const [login, deny] of [['900001', []], ['900003', []], ['700555', ['700555']]]) {
    const r = await on(job, 'result', { socialpilot_login_id: login, deny });
    assert.equal(r.status, 409, login);
    assert.match(r.body.error, /deny list/);
  }
  assert.equal((await on(job, 'result', { socialpilot_login_id: 'abc' })).status, 400);
  // done before the account is linked: refused
  assert.match((await on(job, 'done')).body.error, /link the page's SocialPilot account first/);
  assert.equal((await on(job, 'result', { socialpilot_login_id: '700100' })).status, 200);
  // never one another page uses
  await on(otherJob, 'claim', {});
  const taken = await on(otherJob, 'result', { socialpilot_login_id: '700100' });
  assert.equal(taken.status, 409);
  assert.match(taken.body.error, /linked to another page already/);
  assert.equal((await on(job, 'result', { sources: [{ type: 'search', url: 'made-up hobby news' }, { type: 'website', url: 'https://hobby.example.com/news' }],
                                          playbook: { voice: 'calm and plain', avoid: ['rumours'] } })).status, 200);
  assert.equal((await on(job, 'result', { sources: [{ type: 'website', url: 'not a link' }] })).status, 400);
  await on(job, 'step', { step: 'research_sources', ok: false, detail: 'one search found nothing: tried another' });
  await on(job, 'step', { step: 'research_sources', ok: true, detail: '2 sources' });

  let view = (await tester.call('GET', `/api/pages/${slug}/onboarding`)).body.onboarding;
  const by = Object.fromEntries(view.expected.map(s => [s.step, s]));
  assert.deepEqual([by.link_account.ok, by.research_sources.ok, by.research_sources.detail, by.write_playbook.ok], [true, true, '2 sources', undefined],
                   'the newest report of each step');
  assert.equal(view.steps.length, 3);
  // while it is going live: not worked by the hourly routine's budget, no account handed out yet in config
  let cfg = await cfgOf(slug);
  assert.deepEqual([cfg.status, cfg.workspace, cfg.posts_per_day, cfg.premium_per_day], ['going_live', WS, 10, 6]);

  assert.deepEqual((await on(job, 'done')).body, { ok: true, status: 'live' });
  cfg = await cfgOf(slug);
  assert.deepEqual([cfg.status, cfg.socialpilot_login_id, cfg.sources.length, cfg.playbook.voice, cfg.onboarding.done_at > 0],
                   ['live', '700100', 2, 'calm and plain', true]);
  view = (await tester.call('GET', `/api/pages/${slug}/onboarding`)).body.onboarding;
  assert.deepEqual([view.status, view.job.status], ['live', 'done']);
  assert.equal((await on(job, 'step', { step: 'first_run' })).status, 409, 'a finished job takes no more reports');
  const page = (await tester.call('GET', `/api/pages/${slug}`)).body.page;
  assert.deepEqual([page.status, page.socialpilot_linked, page.source_count], ['live', true, 2]);
});

test('a failed onboarding says why; Try again starts a new job and the old one\'s reports are refused', async () => {
  const slug = await readyPage('Retry Page');
  const job = (await tester.call('POST', `/api/pages/${slug}/go-live`, {})).body.onboarding.job.id;
  assert.equal((await tester.call('POST', `/api/pages/${slug}/retry`, {})).status, 409, 'only after it failed');
  await on(job, 'claim', {});
  assert.equal((await on(job, 'fail', {})).status, 400, 'a failure says what to do');
  const why = 'connect Retry Page in SocialPilot (instructions on the page), then press Try again';
  assert.equal((await on(job, 'fail', { error: why })).body.status, 'failed');
  let view = (await tester.call('GET', `/api/pages/${slug}/onboarding`)).body.onboarding;
  assert.deepEqual([view.status, view.error], ['failed', why]);
  assert.equal((await robot.call('GET', '/api/routine/config', undefined, R)).body.pages.find(p => p.slug === slug).status, 'failed');

  const again = await tester.call('POST', `/api/pages/${slug}/retry`, {});
  assert.equal(again.status, 200);
  const job2 = again.body.onboarding.job.id;
  assert.notEqual(job2, job);
  view = (await tester.call('GET', `/api/pages/${slug}/onboarding`)).body.onboarding;
  assert.deepEqual([view.status, view.error, view.steps.length], ['going_live', '', 0]);
  assert.equal((await on(job, 'step', { step: 'link_account' })).status, 409, 'the old job is not this page\'s any more');
  assert.equal((await on(job, 'claim', {})).status, 409);
  assert.ok((await jobs()).jobs.some(x => x.id === job2));
});

test('pause and resume: only a live page, and a page the product owner paused is theirs to resume', async () => {
  const slug = await readyPage('Pause Page');
  assert.equal((await tester.call('POST', `/api/pages/${slug}/pause`, {})).status, 409, 'a draft cannot be paused');
  const job = (await tester.call('POST', `/api/pages/${slug}/go-live`, {})).body.onboarding.job.id;
  await on(job, 'claim', {});
  await on(job, 'result', { socialpilot_login_id: '700200' });
  await on(job, 'done');
  let r = await tester.call('POST', `/api/pages/${slug}/pause`, {});
  assert.deepEqual([r.status, r.body.page.status], [200, 'paused']);
  assert.equal((await cfgOf(slug)).status, 'paused', 'the routines see it paused: they do not work it');
  // a paused page: no story feed, no posts made for it, Studio read only
  assert.equal((await robot.call('POST', '/api/routine/feed', { page: slug, stories: [] }, R)).status, 409);
  assert.equal((await tester.call('POST', '/api/studio/batches', { page: slug, stories: [{ id: 's1' }] })).status, 409);
  r = await tester.call('POST', `/api/pages/${slug}/resume`, {});
  assert.deepEqual([r.status, r.body.page.status], [200, 'live']);
  assert.equal((await sup.call('POST', `/api/pages/${slug}/pause`, {})).status, 200);
  r = await tester.call('POST', `/api/pages/${slug}/resume`, {});
  assert.equal(r.status, 403);
  assert.match(r.body.error, /product owner paused this page/);
  assert.equal((await sup.call('POST', `/api/pages/${slug}/resume`, {})).status, 200);
});

test('budgets: the product owner sets the most a workspace and a page may use; an owner only lowers them', async () => {
  const w = await workspace(sup, 'Budget Workspace');
  const own = client();
  await setPassword(own, w.link, 'budget pass 42');
  assert.equal((await sup.call('PATCH', `/api/admin/workspaces/${w.id}`, { limits: { posts_per_day: 8, premium_per_day: 4 } })).status, 200);
  const slug = (await own.call('POST', '/api/pages', { name: 'Budget Page' })).body.page.slug;
  let p = (await own.call('GET', `/api/pages/${slug}`)).body.page;
  assert.deepEqual([p.posts_per_day, p.premium_per_day], [8, 4], 'a new page starts within the workspace\'s most');
  let r = await own.call('PATCH', `/api/pages/${slug}`, { posts_per_day: 9 });
  assert.equal(r.status, 400);
  assert.match(r.body.error, /at most 8/);
  assert.equal((await own.call('PATCH', `/api/pages/${slug}`, { limits: { posts_per_day: 30 } })).status, 403, 'the ceiling is the product owner\'s');
  r = await own.call('PATCH', `/api/pages/${slug}`, { posts_per_day: 5, premium_per_day: 0 });
  assert.deepEqual([r.body.page.posts_per_day, r.body.page.premium_per_day], [5, 0]);
  assert.equal((await own.call('PATCH', `/api/pages/${slug}`, { posts_per_day: 8 })).status, 200, 'back up to the most: allowed');
  // the product owner lowers the workspace's most: the page's budget follows it down
  await sup.call('PATCH', `/api/admin/workspaces/${w.id}`, { limits: { posts_per_day: 3 } });
  p = (await own.call('GET', `/api/pages/${slug}`)).body.page;
  assert.equal(p.posts_per_day, 3);
  assert.equal((await cfgOf(slug)).posts_per_day, 3, 'what the routines work with');
  assert.equal((await own.call('PATCH', `/api/pages/${slug}`, { posts_per_day: 4 })).status, 400);
  // a tester's invite carries the workspace's budgets
  const t = await sup.call('POST', '/api/admin/testers', { email: 'small@example.com', name: 'Small', workspace_name: 'Small Workspace', posts_per_day: 2, premium_per_day: 1 });
  assert.deepEqual([t.status, t.body.workspace.limits, t.body.user.role, t.body.user.workspace], [200, { posts_per_day: 2, premium_per_day: 1 }, 'owner', t.body.workspace.id]);
  assert.match(t.body.setup_link, /#reset=/);
  assert.equal((await sup.call('POST', '/api/admin/testers', { email: 'small@example.com', workspace_name: 'Again' })).status, 409);
  assert.equal((await sup.call('POST', '/api/admin/testers', { email: 'big@example.com', workspace_name: 'Big', posts_per_day: 999 })).status, 400);
  assert.equal((await own.call('POST', '/api/admin/testers', { email: 'x@example.com', workspace_name: 'X' })).status, 403, 'only the product owner invites testers');
});

test('owners invite editors for their own pages only; an editor reviews, never manages', async () => {
  const slug = await readyPage('Editors Page');
  const r = await tester.call('POST', '/api/workspace/editors', { email: 'demo-editor@example.com', name: 'Demo Editor', pages: [slug] });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.deepEqual([r.body.user.role, r.body.user.workspace, r.body.user.pages], ['editor', WS, [slug]]);
  editor = client();
  assert.equal((await setPassword(editor, r.body.setup_link, 'editor pass 77')).status, 200);
  const foreign = (await sup.call('POST', '/api/admin/pages', { name: 'Foreign Page', workspace: (await workspace(sup, 'Foreign Workspace')).id })).body.page.slug;
  assert.equal((await tester.call('POST', '/api/workspace/editors', { email: 'e2@example.com', pages: [foreign] })).status, 400, 'not a page of their workspace');
  assert.equal((await tester.call('PATCH', `/api/workspace/editors/${r.body.user.id}`, { pages_add: [foreign] })).status, 400);
  assert.equal((await editor.call('POST', '/api/workspace/editors', { email: 'e3@example.com', pages: [slug] })).status, 403, 'editors invite nobody');
  assert.equal((await editor.call('POST', `/api/pages/${slug}/go-live`, {})).status, 404, 'an editor never manages a page');
  assert.equal((await editor.call('PATCH', `/api/pages/${slug}`, { niche: 'changed' })).status, 404);
  assert.equal((await editor.call('POST', '/api/pages', { name: 'Editor Page' })).status, 403);
  const seen = (await editor.call('GET', `/api/pages/${slug}`)).body.page;
  assert.deepEqual(Object.keys(seen).sort(), ['name', 'slug', 'status', 'theme'], 'what an editor sees of the page: no documents, no settings');
  const ws = (await tester.call('GET', '/api/workspace')).body;
  assert.ok(ws.users.some(x => x.email === 'demo-editor@example.com'));
  assert.ok(ws.pages.every(p => p.workspace === WS));
});

test('a job whose session went quiet for 30 minutes is offered again (stale) and another session takes it over', async () => {
  const slug = await readyPage('Quiet Page');
  const job = (await tester.call('POST', `/api/pages/${slug}/go-live`, {})).body.onboarding.job.id;
  assert.equal((await on(job, 'claim', { session: 'first' })).status, 200);
  await on(job, 'step', { step: 'link_account', detail: 'looking' });
  assert.ok(!(await jobs()).jobs.some(x => x.id === job));
  const real = Date.now;
  Date.now = () => real() + 31 * 60e3;
  try {
    const offered = (await jobs()).jobs.find(x => x.id === job);
    assert.deepEqual([offered.status, offered.stale], ['running', true]);
    assert.equal((await on(job, 'claim', { session: 'second' })).status, 200);
    assert.equal((await on(job, 'claim', { session: 'third' })).status, 409, 'and only one of them');
    assert.equal((await on(job, 'step', { step: 'read_documents' })).status, 200);
  } finally { Date.now = real; }
  // a page paused or pressed again meanwhile: its old job is not offered at all
  await updateRecord('pages', slug, cur => ({ ...cur, status: 'draft' }));
  assert.ok(!(await jobs()).jobs.some(x => x.id === job));
});
