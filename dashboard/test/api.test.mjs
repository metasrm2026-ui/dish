import test from 'node:test';
import assert from 'node:assert/strict';
import { client, remote, makeLive, workspace, livePage } from './helpers.mjs';
import { readFileSync } from 'node:fs';

const R = { routine: true };
const pdf = readFileSync(new URL('./fixtures/dummy_templates.pdf', import.meta.url));
const tinyJpeg = Buffer.from('/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=', 'base64');

async function setPassword(c, link, pw) {
  const token = link.split('#reset=')[1];
  return c.call('POST', '/api/auth/reset', { token, password: pw });
}

test('phase 1 workflow end to end', async () => {
  const admin = client(), editor = client(), other = client(), robot = client();

  // nothing without a session; routine bootstrap creates the first user, the product owner, once
  assert.equal((await admin.call('GET', '/api/studio/pages')).status, 401);
  assert.equal((await admin.call('GET', '/api/drafts?status=pending')).status, 401);
  const boot = await robot.call('POST', '/api/routine/bootstrap', { email: 'Owner@Example.com', name: 'Owner' }, R);
  assert.equal(boot.status, 200);
  assert.equal((await robot.call('POST', '/api/routine/bootstrap', { email: 'x@y.z' }, R)).status, 409);
  assert.equal((await setPassword(admin, boot.body.setup_link, 'short')).status, 400);
  assert.equal((await setPassword(admin, boot.body.setup_link, 'correct horse 42')).status, 200);
  assert.equal((await setPassword(other, boot.body.setup_link, 'another pass 42')).status, 400, 'link works once');
  const me = await admin.call('GET', '/api/auth/me');
  assert.equal(me.body.user.role, 'super');

  // the product owner: a tester's workspace, a page in it, sources, a reference document
  const WS = (await workspace(admin)).id;
  const pg = await admin.call('POST', '/api/admin/pages', { name: 'Dummy Sports', fb_page_id: '123', workspace: WS, theme: { accent: '#ff0' } });
  assert.equal(pg.status, 200);
  const slug = pg.body.page.slug;
  assert.equal(slug, 'dummy-sports');
  await makeLive(slug, '999');                    // as the onboarding routine leaves it (onboard.test.mjs)
  await admin.call('POST', `/api/admin/pages/${slug}/sources`, { url: 'https://example.com/news', type: 'website', label: 'Example' });
  assert.equal((await admin.call('POST', `/api/admin/pages/${slug}/sources`, { url: 'not a url', type: 'website' })).status, 400);
  const srch = await admin.call('POST', `/api/admin/pages/${slug}/sources`, { url: 'dummy league news today', type: 'search' });
  assert.equal(srch.status, 200, 'a web search is a source too');
  await admin.call('DELETE', `/api/admin/pages/${slug}/sources/${srch.body.sources.find(s => s.type === 'search').id}`);
  const src = await admin.call('GET', `/api/admin/pages/${slug}/sources`);
  assert.equal(src.body.sources.length, 1);
  const up = await admin.call('POST', `/api/admin/pages/${slug}/docs/templates`, { filename: 't.pdf', data_b64: pdf.toString('base64') });
  assert.equal(up.status, 200, JSON.stringify(up.body));
  assert.ok(up.body.extracted_chars > 20);
  await admin.call('POST', `/api/admin/pages/${slug}/docs/templates`, { filename: 't2.pdf', data_b64: pdf.toString('base64') });
  const rb = await admin.call('PATCH', `/api/admin/pages/${slug}/docs/templates`, { active: 1 });
  assert.equal(rb.body.page.docs.templates.active, 1);
  assert.equal(rb.body.page.docs.templates.versions.length, 2);
  assert.ok(!JSON.stringify(rb.body).includes('.enc'), 'storage paths never reach the browser');
  assert.equal((await admin.call('POST', `/api/admin/pages/${slug}/docs/tone`, { data_b64: Buffer.from('hello').toString('base64') })).status, 400);
  // the templates document made active is converted into template specs (routes/templates.js): one
  // job, which the second upload and the rollback joined while no session had it yet
  assert.ok(up.body.conversion.job);
  const conv = (await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs;
  assert.deepEqual(conv.map(j => [j.id, j.kind, j.doc_version, j.posts.length]), [[up.body.conversion.job, 'templates', 1, 0]]);
  await robot.call('POST', `/api/routine/jobs/${conv[0].id}`, { status: 'running' }, R);
  assert.equal((await robot.call('POST', `/api/routine/jobs/${conv[0].id}`, { status: 'done', posts: {} }, R)).status, 200);

  // the routine reads config (document text included) and the PDF; both audited
  const cfg = await robot.call('GET', '/api/routine/config', undefined, R);
  const cp = cfg.body.pages.find(p => p.slug === slug);
  assert.match(cp.docs.templates.text, /Stat card/);
  assert.equal(cp.sources[0].url, 'https://example.com/news');
  const doc = await robot.call('GET', `/api/routine/doc?page=${slug}&type=templates`, undefined, R);
  assert.equal(Buffer.from(doc.body).subarray(0, 5).toString(), '%PDF-');

  // invite an editor for this page
  const inv = await admin.call('POST', '/api/admin/users', { email: 'ed@example.com', role: 'editor', workspace: WS, pages: [slug] });
  assert.equal(inv.status, 200);
  const sp = await setPassword(editor, inv.body.setup_link, 'editor pass 77');
  assert.equal(sp.status, 200, JSON.stringify(sp.body));
  assert.equal((await editor.call('GET', '/api/admin/users')).status, 403, 'editors never see admin');
  assert.equal((await editor.call('GET', `/api/admin/pages/${slug}/sources`)).status, 403);
  assert.deepEqual((await editor.call('GET', '/api/studio/pages')).body.pages.map(p => p.slug), [slug]);

  // the hourly routine publishes the page feed; the window filters by publish time
  const now = Date.now();
  const story = (i, hoursAgo, ideas = true) => ({ id: `s${i}`, headline: `Story ${i}`, summary: `Summary ${i}`, score: 10 - i,
    sources: [{ name: 'Example', url: `https://example.com/${i}` }], published_at: now - hoursAgo * 3600e3, url: `https://example.com/${i}`,
    ideas: ideas ? [{ title: 'Stat', template: 'stat_card', description: 'Big number' }, { title: 'Then vs now', template: 'then_vs_now', description: 'Two photos' }] : [],
    images: [{ url: `https://img.example.com/${i}.jpg`, source: 'web', credit: 'example.com',
               // whose face it shows (bore/faceid.py); unknown values never reach the picker
               ...(i === 1 ? { id_match: 'match', id_score: 0.5123, id_name: 'Jane Example', id_ref: 'espn' } : {}),
               ...(i === 2 ? { id_match: 'maybe', id_score: 0.1, id_name: 'Jane Example', id_ref: 'elsewhere' } : {}) },
             ...(i === 1 ? [{ url: 'https://img.example.com/1-outlets.jpg', source: 'news', credit: 'outlet.example.org', via: 'used by 3 outlets',
                              consensus: 3, flags: ['agency photo'] }] : [])] });
  await robot.call('POST', '/api/routine/feed', { page: slug, stories: [story(1, 1), story(2, 6), story(3, 20), story(4, 2, false)] }, R);
  assert.equal((await editor.call('GET', `/api/studio/feed?page=${slug}&hours=3`)).body.stories.length, 2);
  const fed = (await editor.call('GET', `/api/studio/feed?page=${slug}&hours=24`)).body.stories;
  assert.equal(fed.length, 4);
  const img = id => fed.find(s => s.id === id).images[0];
  assert.deepEqual([img('s1').id_match, img('s1').id_score, img('s1').id_name, img('s1').id_ref], ['match', 0.51, 'Jane Example', 'espn']);
  assert.equal(img('s2').id_match, undefined);
  assert.equal(img('s3').id_name, undefined);

  // a pasted URL becomes a story
  remote.set('https://news.example.org/a', { body: '<meta property="og:title" content="Pasted headline"><meta property="og:image" content="/i.jpg">' });
  const us = await editor.call('POST', '/api/studio/url', { page: slug, url: 'https://news.example.org/a' });
  assert.equal(us.body.story.headline, 'Pasted headline');
  assert.equal(us.body.story.images[0].url, 'https://news.example.org/i.jpg');

  // a batch: at most 5; stories without ideas queue an ideas job
  const six = Array.from({ length: 6 }, (_, i) => ({ id: `s${i}` }));
  assert.equal((await editor.call('POST', '/api/studio/batches', { page: slug, stories: six })).status, 400);
  const bt = await editor.call('POST', '/api/studio/batches', { page: slug, stories: [{ id: 's1' }, { id: 's4' }, us.body.story] });
  assert.equal(bt.status, 200);
  const batch = bt.body.batch;
  assert.ok(bt.body.ideas_job);
  let jobs = (await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs;
  assert.equal(jobs.length, 1);
  assert.equal(jobs[0].posts.length, 2);
  const [p1, p4, pu] = batch.posts;
  await robot.call('POST', `/api/routine/jobs/${jobs[0].id}`, { status: 'done', posts: {
    [p4.id]: { ideas: [{ title: 'A', template: 'headline', description: 'a' }, { title: 'B', template: 'stat_card', description: 'b' }] },
    [pu.id]: { summary: 'Read in full', ideas: [{ title: 'C', template: 'quote', description: 'c' }, { title: 'D', template: 'timeline', description: 'd' }] } } }, R);

  // ideation + base image (candidate or upload); a stranger cannot touch the batch
  const path = id => `/api/studio/batches/${batch.id}/posts/${id}`;
  assert.equal((await other.call('PATCH', path(p1.id), { idea: { choice: 1 } })).status, 401);
  await editor.call('PATCH', path(p1.id), { idea: { choice: 2 }, base_image_url: 'https://img.example.com/1.jpg' });
  await editor.call('PATCH', path(p1.id), { base_image_url: 'https://img.example.com/1-outlets.jpg' });   // changed their mind
  assert.equal((await editor.call('PATCH', path(p4.id), { base_image_b64: Buffer.from('<html>not an image</html>').toString('base64') })).status, 400);
  await editor.call('PATCH', path(p4.id), { idea: { choice: 'own', text: 'Ranked list of the top 5' }, base_image_b64: tinyJpeg.toString('base64') });
  const bad = await editor.call('PATCH', path(pu.id), { base_image_url: 'https://evil.example.com/x.jpg' });
  assert.equal(bad.status, 400, 'only candidate images can be picked by URL');
  await editor.call('PATCH', path(pu.id), { idea: { choice: 1 }, base_image_url: 'https://news.example.org/i.jpg' });
  let b2 = (await editor.call('GET', `/api/studio/batches/${batch.id}`)).body.batch;
  assert.deepEqual(b2.posts.map(p => p.status), ['ideated', 'ideated', 'ideated']);
  assert.equal(b2.posts[0].idea.template, 'then_vs_now');
  // which candidate each editor picked, and what it was, is kept on the post and in the review ledger
  const { url: _picked, ...bi } = b2.posts[0].base_image;
  assert.deepEqual(bi, { source: 'news', credit: 'outlet.example.org', via: 'used by 3 outlets', note: '',
    flags: ['agency photo'], consensus: 3, rank: 1, of: 2 });
  assert.equal(b2.posts[1].base_image.source, 'upload');
  assert.equal(b2.posts[1].base_image.rank, null);
  const picks = Object.fromEntries((await robot.call('GET', '/api/routine/reviews?days=1', undefined, R)).body.entries
    .filter(e => e.verdict === 'studio_pick' && e.draft_id.startsWith(batch.id)).map(e => [e.draft_id.split(':')[1], e]));
  assert.equal(Object.keys(picks).length, 3, 'one pick per post: the newest (with the photo) wins');
  assert.deepEqual(picks[p1.id].photo, { source: 'news', note: '', consensus: 3, flags: ['agency photo'], rank: 1, of: 2,
    via: 'used by 3 outlets', passed: [{ source: 'web', note: '', consensus: 0, flags: [] }] });
  assert.equal(picks[p4.id].photo.source, 'uploaded');
  assert.equal(picks[p4.id].photo.rank, null);
  assert.equal(picks[p4.id].photo.passed.length, 1, 'an upload passes over the candidates shown');
  assert.equal(picks[pu.id].photo.rank, 0);
  assert.equal(picks[pu.id].photo.source, 'link');

  // compose (routine): visual + caption + hashtags; one job (and session) per post, so each visual
  // shows as soon as it is made
  const run = await editor.call('POST', `/api/studio/batches/${batch.id}/run`, { kind: 'compose' });
  assert.equal(run.status, 200);
  assert.equal(run.body.jobs.length, 3);
  jobs = (await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs;
  assert.deepEqual(jobs.map(j => [j.kind, j.posts.length]), [['compose', 1], ['compose', 1], ['compose', 1]]);
  for (const j of jobs) {
    const results = Object.fromEntries(j.posts.map(p => [p.id, { image_b64: tinyJpeg.toString('base64'), caption: `Caption for ${p.story.headline}`,
      hashtags: ['Tag', '#Two'], card: { template: p.idea.template } }]));
    await robot.call('POST', `/api/routine/jobs/${j.id}`, { status: 'done', posts: results }, R);
  }
  b2 = (await editor.call('GET', `/api/studio/batches/${batch.id}`)).body.batch;
  assert.deepEqual(b2.posts.map(p => p.status), ['captioned', 'captioned', 'captioned']);
  assert.deepEqual(b2.posts[0].hashtags, ['#Tag', '#Two']);

  // check (routine, independent reviewer) on two posts, skip the third; editing marks it stale
  await editor.call('POST', `/api/studio/batches/${batch.id}/run`, { kind: 'check', post_ids: [p1.id, p4.id] });
  jobs = (await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs;
  const verdict = { factual: [], timing: ['Story is 6 hours old'], grammar: [], other: [], guidelines: [] };
  await robot.call('POST', `/api/routine/jobs/${jobs[0].id}`, { status: 'done', posts: { [p1.id]: { check: verdict }, [p4.id]: { check: verdict } } }, R);
  await editor.call('POST', `/api/studio/batches/${batch.id}/skip-check`, { post_ids: [pu.id] });
  await editor.call('PATCH', path(p1.id), { caption: 'Edited caption' });
  b2 = (await editor.call('GET', `/api/studio/batches/${batch.id}`)).body.batch;
  assert.deepEqual(b2.posts.map(p => p.status), ['checked', 'checked', 'check_skipped']);
  assert.equal(b2.posts[0].check.stale, true);

  // publish: schedule and post-now become approved drafts for the publisher routine
  const hint = (await editor.call('GET', `/api/studio/schedule-hint?page=${slug}`)).body;
  assert.ok(hint.next >= Date.now() + 59 * 60e3);
  assert.equal((await editor.call('POST', `/api/studio/batches/${batch.id}/publish`, { post_id: p1.id, mode: 'schedule', at: Date.now() })).status, 400);
  const pub = await editor.call('POST', `/api/studio/batches/${batch.id}/publish`, { post_id: p1.id, mode: 'schedule', at: hint.next });
  assert.equal(pub.status, 200, JSON.stringify(pub.body));
  await editor.call('POST', `/api/studio/batches/${batch.id}/publish`, { post_id: p4.id, mode: 'now' });
  const q = (await robot.call('GET', '/api/queue', undefined, R)).body.queue;
  assert.equal(q.length, 2);
  const sched = q.find(x => x.id.endsWith(p1.id));
  assert.equal(sched.requested_at, hint.next);
  const sent = (await robot.call('GET', '/api/routine/reviews?days=1', undefined, R)).body.entries.find(e => e.verdict === 'studio_sent' && e.draft_id === sched.id);
  assert.equal(sent.photo.source, 'news');
  assert.equal(sent.photo.rank, 1);
  assert.equal(sent.photo.passed, undefined, 'passed-over candidates are counted once, with the pick');
  assert.match(sched.caption, /Edited caption\n\n#Tag #Two/);
  assert.equal((await robot.call('POST', '/api/result', { id: sched.id, status: 'scheduled', scheduled_for: '2026-10-04T15:00:00Z', post_id: 'sp1' }, R)).status, 409,
               'only a claimed draft is scheduled');
  assert.equal((await robot.call('POST', '/api/result', { id: sched.id, status: 'publishing', scheduled_for: '2026-10-04T15:00:00Z' }, R)).status, 200);
  await robot.call('POST', '/api/result', { id: sched.id, status: 'scheduled', scheduled_for: '2026-10-04T15:00:00Z', post_id: 'sp1' }, R);
  b2 = (await editor.call('GET', `/api/studio/batches/${batch.id}`)).body.batch;
  assert.equal(b2.posts[0].status, 'scheduled');
  assert.equal(b2.posts[0].publish.scheduled_for, '2026-10-04T15:00:00Z');
  assert.equal((await editor.call('PATCH', path(p1.id), { caption: 'too late' })).status, 409);
  const next = (await editor.call('GET', `/api/studio/schedule-hint?page=${slug}`)).body;
  assert.ok(next.next >= Date.parse('2026-10-04T15:00:00Z') + 45 * 60e3 || next.next >= Date.now() + 3600e3);

  // audit + usage logs
  await robot.call('POST', '/api/routine/usage', { entries: [{ tool: 'krea', page: slug, user: 'ed@example.com', units: 15, unit: 'credits' }] }, R);
  const audit = (await admin.call('GET', '/api/admin/audit')).body.entries.map(e => e.action);
  for (const a of ['bootstrap_super', 'reference_uploaded', 'reference_rolled_back', 'sources_viewed', 'config_read', 'reference_read', 'user_invited', 'post_scheduled', 'post_now'])
    assert.ok(audit.includes(a), `audit has ${a}`);
  assert.equal((await admin.call('GET', '/api/admin/usage')).body.entries[0].tool, 'krea');
});

test('lockout after five wrong passwords; deactivation ends sessions', async () => {
  const admin = client();
  // the first test's owner exists already (shared in-memory store)
  for (let i = 0; i < 4; i++) assert.equal((await admin.call('POST', '/api/auth/login', { email: 'ed@example.com', password: 'nope nope 1' })).status, 401);
  const fifth = await admin.call('POST', '/api/auth/login', { email: 'ed@example.com', password: 'nope nope 1' });
  assert.equal(fifth.status, 429, 'the try that locks the account says so');
  assert.match(fifth.body.error, /too many attempts, try again in 15 min/);
  assert.equal((await admin.call('POST', '/api/auth/login', { email: 'ed@example.com', password: 'editor pass 77' })).status, 429);
  assert.equal((await admin.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const users = (await admin.call('GET', '/api/admin/users')).body.users;
  const ed = users.find(u => u.email === 'ed@example.com');
  assert.equal(ed.locked, true);
  assert.equal((await admin.call('PATCH', `/api/admin/users/${users.find(u => u.role === 'super').id}`, { role: 'editor' })).status, 400);
});

test('two publisher runs cannot both schedule one approved draft', async () => {
  const robot = client();
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const id = 'dummy-sports-202610060700-claim-test';
  await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: 'x' }, tinyJpeg);
  await setStatus(id, 'approved', { caption: 'x' });
  assert.equal((await robot.call('GET', '/api/queue', undefined, R)).body.queue.filter(q => q.id === id).length, 1);
  const a = await robot.call('POST', '/api/result', { id, status: 'publishing', scheduled_for: '2026-10-06T08:00:00Z' }, R);
  const b = await robot.call('POST', '/api/result', { id, status: 'publishing' }, R);
  assert.equal(a.status, 200);
  assert.equal(b.status, 409, 'the second run must skip it');
  const q = (await robot.call('GET', '/api/queue', undefined, R)).body;
  assert.equal(q.queue.filter(x => x.id === id).length, 0, 'a claimed draft leaves the queue');
  assert.equal(q.in_progress.find(x => x.id === id).scheduled_for, '2026-10-06T08:00:00Z');
  // SocialPilot refuses it: the list still says an editor approved it (the routine learns from that)
  assert.equal((await robot.call('POST', '/api/result', { id, status: 'failed', error: 'SocialPilot account is locked' }, R)).status, 200);
  const failed = (await robot.call('GET', '/api/drafts?status=failed', undefined, R)).body.drafts.find(d => d.id === id);
  assert.ok(failed.approved_at > 0);
  // the dashboard warns while SocialPilot refuses posts
  const pub = (await robot.call('GET', '/api/stats?since=0', undefined, R)).body.publisher;
  assert.equal(pub.blocked, true);
  assert.equal(pub.locked, true);
  assert.match(pub.error, /locked/);
  // after the unlock, editors retry it in bulk: approved again, back in the publisher's queue
  assert.equal((await client().call('POST', '/api/decide', { ids: [id], decision: 'retry' })).status, 401, 'signed-in editors only');
  assert.equal((await robot.call('GET', '/api/queue', undefined, R)).body.queue.filter(x => x.id === id).length, 0, 'failed: not queued');
  const owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const r = await owner.call('POST', '/api/decide', { ids: [id, 'no-such-draft'], decision: 'retry' });
  assert.equal(r.status, 200);
  assert.equal(r.body.retried, 1);
  assert.equal((await robot.call('GET', '/api/queue', undefined, R)).body.queue.filter(x => x.id === id).length, 1, 'back in the queue');
  assert.equal((await robot.call('POST', '/api/result', { id, status: 'publishing', scheduled_for: '2026-10-06T09:00:00Z' }, R)).status, 200);
  assert.equal((await robot.call('POST', '/api/result', { id, status: 'scheduled', scheduled_for: '2026-10-06T09:00:00Z' }, R)).status, 200);
  assert.equal((await robot.call('GET', '/api/stats?since=0', undefined, R)).body.publisher.blocked, false, 'a success clears the warning');
});

test('waiting posts: editors clear them, and yesterday\'s clear at midnight (not as Not okay)', async () => {
  const robot = client(), ed = client();
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const { lastMidnight } = await import('../lib/store.js');
  const at = unix => new Date(unix * 1000).toISOString().replace(/\D/g, '').slice(0, 12);
  const now = Math.floor(Date.now() / 1000), midnight = lastMidnight();
  const ids = [`dummy-sports-${at(now - 2 * 86400)}-old-story`, `dummy-sports-${at(now)}-new-story-a`, `dummy-sports-${at(now - 60)}-new-story-b`,
               `dummy-sports-${at(midnight - 5 * 3600)}-last-evening`];
  for (const id of ids) { await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: 'x' }, tinyJpeg); await setStatus(id, 'pending', {}); }
  const waiting = async () => (await robot.call('GET', '/api/drafts?status=pending', undefined, R)).body.drafts.map(d => d.id);
  const w = await waiting();
  assert.ok(!w.includes(ids[0]), 'waited a whole day: cleared automatically');
  assert.ok(w.includes(ids[1]) && w.includes(ids[2]));
  assert.ok(w.includes(ids[3]), 'made last evening: stays for the morning review');
  // the 12-hour boundary (ids have minute precision: 120 s margins)
  const edge = [`dummy-sports-${at(midnight - 12 * 3600 - 120)}-edge-old`, `dummy-sports-${at(midnight - 12 * 3600 + 120)}-edge-new`];
  for (const id of edge) { await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: 'x' }, tinyJpeg); await setStatus(id, 'pending', {}); }
  const w2 = await waiting();
  assert.ok(!w2.includes(edge[0]) && w2.includes(edge[1]));
  // a cleared post from last evening can go back to Waiting; one from two days ago can't (it would clear again)
  const { setStatus: st } = await import('../lib/store.js');
  await st(ids[3], 'cleared', { by: 'midnight clean-up', auto: true });
  assert.equal((await robot.call('POST', '/api/drafts?action=restore', { ids: [ids[3]] }, R)).status, 401, 'editors only');
  const boss = client();
  assert.equal((await boss.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const restored = await boss.call('POST', '/api/drafts?action=restore', { ids: [ids[3], ids[0]] });
  assert.equal(restored.body.restored, 1);
  assert.ok((await robot.call('GET', '/api/routine/reviews?days=2', undefined, R)).body.entries.some(e => e.draft_id === ids[3] && e.verdict === 'restored'));
  assert.equal(restored.body.skipped, 1);
  assert.ok((await waiting()).includes(ids[3]));
  const cleared = (await robot.call('GET', '/api/drafts?status=cleared', undefined, R)).body.drafts.find(d => d.id === ids[0]);
  assert.equal(cleared.status_info.auto, true);
  assert.equal((await ed.call('POST', '/api/drafts?action=clear', { ids: [ids[1]] })).status, 401, 'editors only');
  const rejected = (await robot.call('GET', '/api/drafts?status=rejected', undefined, R)).body.drafts.map(d => d.id);
  assert.ok(!rejected.includes(ids[0]), 'a cleared post is not a rejection');
});

test('every editor verdict goes to the review ledger; lessons keep the admins\' settings', async () => {
  const robot = client(), owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const { saveDraft, setStatus, lastMidnight } = await import('../lib/store.js');
  const at = unix => new Date(unix * 1000).toISOString().replace(/\D/g, '').slice(0, 12);
  const now = Math.floor(Date.now() / 1000);
  const ids = ['ledger-a', 'ledger-b', 'ledger-c', 'ledger-old'].map((s, i) => `dummy-sports-${at(i === 3 ? lastMidnight() - 20 * 3600 : now - i * 60)}-${s}`);
  for (const id of ids) { await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: 'x', headline: id.toUpperCase(), kind: 'transaction', copy: { template: 'report' } }, tinyJpeg); await setStatus(id, 'pending', {}); }
  assert.equal((await owner.call('POST', '/api/decide', { id: ids[0], decision: 'reject', reason: 'Weak story', note: 'too minor' })).status, 200);
  assert.equal((await owner.call('POST', '/api/decide', { id: ids[1], decision: 'approve' })).status, 200);
  assert.equal((await owner.call('POST', '/api/drafts?action=clear', { ids: [ids[2]] })).status, 200);
  await robot.call('GET', '/api/drafts?status=pending', undefined, R);          // the midnight clean-up takes ids[3]
  const entries = (await robot.call('GET', '/api/routine/reviews?days=2', undefined, R)).body.entries;
  const by = Object.fromEntries(entries.filter(e => ids.includes(e.draft_id)).map(e => [e.draft_id, e]));
  assert.equal(by[ids[0]].verdict, 'rejected');
  assert.equal(by[ids[0]].note, 'too minor');
  assert.equal(by[ids[0]].by, 'owner@example.com');
  assert.equal(by[ids[0]].kind, 'transaction');
  assert.equal(by[ids[0]].template, 'report');
  assert.equal(by[ids[1]].verdict, 'approved');
  assert.equal(by[ids[2]].verdict, 'cleared');
  assert.equal(by[ids[3]], undefined, 'the midnight clean-up is not a verdict');
  assert.equal((await owner.call('GET', '/api/routine/reviews', undefined)).status, 401, 'routines only');
  // clearing by age is housekeeping; Not okay on an approved-then-failed post is a withdrawal; bulk Not okay
  const more = ['ledger-e', 'ledger-f', 'ledger-g', 'ledger-h'].map((s2, i) => `dummy-sports-${at(now - 30 * 3600 - i * 60)}-${s2}`);
  for (const id of more) { await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: 'x', headline: id }, tinyJpeg); await setStatus(id, 'pending', {}); }
  await setStatus(more[1], 'approved', {}); await setStatus(more[1], 'failed', { error: 'SocialPilot account is locked' });
  assert.equal((await owner.call('POST', '/api/drafts?action=clear', { before: now - 29 * 3600 })).body.cleared >= 1, true);
  assert.equal((await owner.call('POST', '/api/decide', { id: more[1], decision: 'reject', reason: 'Weak story' })).status, 200);
  const bulk = await owner.call('POST', '/api/decide', { ids: [more[2], more[3], ids[1]], decision: 'reject_many', reason: 'Old news by now' });
  assert.equal(bulk.body.rejected, 0, 'those were cleared by age just now: only waiting posts');
  const e2 = (await robot.call('GET', '/api/routine/reviews?days=2', undefined, R)).body.entries;
  assert.ok(!e2.some(e => e.draft_id === more[0] && e.verdict === 'cleared'), 'an age clear is not a verdict');
  assert.equal(e2.find(e => e.draft_id === more[1]).verdict, 'withdrawn');
  const fresh = [`dummy-sports-${at(now - 120)}-ledger-i`, `dummy-sports-${at(now - 180)}-ledger-j`];
  for (const id of fresh) { await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: 'x', headline: id }, tinyJpeg); await setStatus(id, 'pending', {}); }
  assert.equal((await owner.call('POST', '/api/decide', { ids: fresh, decision: 'reject_many', reason: 'Old news by now' })).body.rejected, 2);
  // backfill is idempotent
  const old = [{ verdict: 'rejected', page: 'dummy-sports', draft_id: 'dummy-sports-202609291200-old', headline: 'OLD ONE', reason: 'Weak story', decided_at: 1790000000000 }];
  assert.equal((await robot.call('POST', '/api/routine/reviews', { entries: old }, R)).body.added, 1);
  assert.equal((await robot.call('POST', '/api/routine/reviews', { entries: old }, R)).body.added, 0);
  // a re-backfill with better data replaces it (the newest entry per post and verdict wins)
  assert.equal((await robot.call('POST', '/api/routine/reviews', { entries: [{ ...old[0], excerpt: 'Jeanty was limited' }], replace: true }, R)).body.added, 1);
  const olds = (await robot.call('GET', '/api/routine/reviews?days=2', undefined, R)).body.entries.filter(e => e.draft_id === old[0].draft_id);
  assert.equal(olds.length, 1);
  assert.equal(olds[0].excerpt, 'Jeanty was limited');
  // lessons: the routine saves, admins add notes / overrides, a rebuild keeps them, reset clears learning
  assert.equal((await robot.call('POST', '/api/routine/lessons', { page: 'dummy-sports', record: { kinds: { transaction: { mult: 1.2 } }, reviews_used: 3 } }, R)).status, 200);
  assert.equal((await owner.call('PUT', '/api/admin/pages/dummy-sports/lessons', { admin_notes: 'no jersey numbers', overrides: { quote: 0.7 } })).status, 200);
  await robot.call('POST', '/api/routine/lessons', { page: 'dummy-sports', record: { kinds: {}, reviews_used: 4, admin_notes: 'routine must not change this' } }, R);
  const l = (await robot.call('GET', '/api/routine/lessons?page=dummy-sports', undefined, R)).body.lessons;
  assert.equal(l.admin_notes, 'no jersey numbers');
  assert.deepEqual(l.overrides, { quote: 0.7 });
  assert.equal(l.reviews_used, 4);
  assert.equal((await owner.call('POST', '/api/admin/pages/dummy-sports/lessons/reset', {})).status, 400, 'needs confirm');
  const reset = (await owner.call('POST', '/api/admin/pages/dummy-sports/lessons/reset', { confirm: true })).body.lessons;
  assert.ok(reset.reset_at > 0);
  assert.equal(reset.admin_notes, 'no jersey numbers');
});

test('while SocialPilot is down approved posts wait (held), then go out; old ones move to Failed', async () => {
  const robot = client();
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const { putRecord } = await import('../lib/db.js');
  await putRecord('health', 'socialpilot', {});
  const id = 'dummy-sports-202610130500-held-test', old = 'dummy-sports-202610120500-held-old';
  await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: 'x' }, tinyJpeg);
  await setStatus(id, 'approved', { caption: 'x', by: 'ed@example.com' });
  const first = (await robot.call('GET', '/api/queue', undefined, R)).body.queue.find(q => q.id === id).first_approved_at;
  assert.ok(first > 0);
  const claim = await robot.call('POST', '/api/result', { id, status: 'publishing' }, R);
  assert.equal(claim.status, 200);
  assert.ok(claim.body.claim, 'the claiming run gets a token');
  // another run (no token) cannot put a claimed draft back up for a second claim
  assert.equal((await robot.call('POST', '/api/result', { id, status: 'held', error: 'connector not attached' }, R)).status, 409);
  const h = await robot.call('POST', '/api/result', { id, status: 'held', error: 'SocialPilot error: Your Account is Locked', claim: claim.body.claim }, R);
  assert.equal(h.body.status, 'held');
  // back in the queue with its first approval time; the lock recorded for the publisher and editors
  const q = (await robot.call('GET', '/api/queue', undefined, R)).body;
  const item = q.queue.find(x => x.id === id);
  assert.equal(item.first_approved_at, first);
  assert.equal(item.holds, 1);
  assert.match(item.held_error, /Locked/);
  assert.equal(q.socialpilot.down, true);
  // a second report within the retry gap (e.g. a run without the connector) is not another try
  assert.equal((await robot.call('POST', '/api/result', { id, status: 'held', error: 'connector not attached' }, R)).body.holds, 1);
  const pub = (await robot.call('GET', '/api/stats?since=0', undefined, R)).body.publisher;
  assert.equal(pub.held, true);
  assert.equal(pub.locked, true);
  assert.ok(pub.waiting >= 1);
  const listed = (await robot.call('GET', '/api/drafts?status=approved', undefined, R)).body.drafts.find(d => d.id === id);
  assert.equal(listed.status_info.held, true, 'editors see it waiting, not failed');
  // confirming an old delivery does not mean CreatePost works again
  const other = 'dummy-sports-202610130400-held-other';
  await saveDraft({ id: other, page: 'dummy-sports', page_name: 'Dummy', caption: 'z' }, tinyJpeg);
  await setStatus(other, 'scheduled', { caption: 'z', result: { scheduled_for: '2026-10-13T04:30:00Z' } });
  await robot.call('POST', '/api/result', { id: other, status: 'posted' }, R);
  assert.equal((await robot.call('GET', '/api/queue', undefined, R)).body.socialpilot.down, true);
  // the next run can claim it again (a new approval record), and SocialPilot is back
  assert.equal((await robot.call('POST', '/api/result', { id, status: 'publishing' }, R)).status, 200);
  assert.equal((await robot.call('POST', '/api/result', { id, status: 'scheduled', scheduled_for: '2026-10-13T11:00:00Z' }, R)).status, 200);
  assert.equal((await robot.call('GET', '/api/queue', undefined, R)).body.socialpilot.down, false);
  assert.equal((await robot.call('GET', '/api/stats?since=0', undefined, R)).body.publisher.blocked, false);
  // a draft that waited more than 12 hours: a hold sends it to Failed ...
  await saveDraft({ id: old, page: 'dummy-sports', page_name: 'Dummy', caption: 'y' }, tinyJpeg);
  await setStatus(old, 'approved', { caption: 'y', first_approved_at: Math.floor(Date.now() / 1000) - 13 * 3600, holds: 2,
                                     held: true });
  await new Promise(r => setTimeout(r, 5));
  const e = await robot.call('POST', '/api/result', { id: old, status: 'held', error: 'Your Account is Locked' }, R);
  assert.equal(e.body.status, 'held', 'held moments ago: not another try');
  // ... and so does claiming it after SocialPilot is back: never scheduled unseen
  const late = await robot.call('POST', '/api/result', { id: old, status: 'publishing' }, R);
  assert.equal(late.status, 409);
  assert.match(late.body.error, /do NOT schedule/);
  assert.equal((await robot.call('GET', '/api/drafts?status=failed', undefined, R)).body.drafts.filter(d => d.id === old).length, 1);
  // editors can withdraw an approved post that is not scheduled yet, not one a run has claimed
  const w = 'dummy-sports-202610130600-withdraw';
  await saveDraft({ id: w, page: 'dummy-sports', page_name: 'Dummy', caption: 'w' }, tinyJpeg);
  await setStatus(w, 'approved', { caption: 'w' });
  const owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  assert.equal((await owner.call('POST', '/api/decide', { id: w, decision: 'withdraw' })).body.status, 'rejected');
  assert.equal((await robot.call('POST', '/api/result', { id: w, status: 'publishing' }, R)).status, 409, 'no run can claim it now');
  const w2 = 'dummy-sports-202610130610-withdraw-late';
  await saveDraft({ id: w2, page: 'dummy-sports', page_name: 'Dummy', caption: 'w2' }, tinyJpeg);
  await setStatus(w2, 'approved', { caption: 'w2' });
  assert.equal((await robot.call('POST', '/api/result', { id: w2, status: 'publishing' }, R)).status, 200);
  assert.equal((await owner.call('POST', '/api/decide', { id: w2, decision: 'withdraw' })).status, 409, 'being scheduled: too late');
  // a withdrawn post is never put back by a hold
  assert.equal((await robot.call('POST', '/api/result', { id: w, status: 'held', error: 'connector not attached' }, R)).status, 409);
  // a post already in SocialPilot that errored there during the lock: Failed, for an editor
  const sch = 'dummy-sports-202610130620-scheduled-lock';
  await saveDraft({ id: sch, page: 'dummy-sports', page_name: 'Dummy', caption: 's' }, tinyJpeg);
  await setStatus(sch, 'scheduled', { caption: 's', result: { scheduled_for: '2026-10-13T07:00:00Z' } });
  assert.equal((await robot.call('POST', '/api/result', { id: sch, status: 'held', error: 'Your Account is Locked' }, R)).body.status, 'failed');
  // the age limit counts to the planned slot, not to the moment of the claim
  const slot = 'dummy-sports-202610130630-slot-check';
  await saveDraft({ id: slot, page: 'dummy-sports', page_name: 'Dummy', caption: 'q' }, tinyJpeg);
  const firstAt = Math.floor(Date.now() / 1000) - 10 * 3600;
  await setStatus(slot, 'approved', { caption: 'q', first_approved_at: firstAt, holds: 1, held: true });
  const far = new Date((firstAt + 13 * 3600) * 1000).toISOString();
  const c = await robot.call('POST', '/api/result', { id: slot, status: 'publishing', scheduled_for: far }, R);
  assert.equal(c.status, 409, 'it would go out 13h after approval');
});

test('a verdict on an hourly post keeps the photo it used; redo notes and the banner', async () => {
  const robot = client(), owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const at = new Date().toISOString().replace(/\D/g, '').slice(0, 12);
  const photos = [{ url: 'https://img.example.com/a.jpg', source: 'web', credit: 'example.com' },
                  { url: 'https://img.example.com/b.mp4#t=3', source: 'x', credit: '@a', note: 'video frame', flags: ['soft (sharpness 30)'] }];
  const ids = ['photo-new', 'photo-old', 'photo-none', 'photo-redo'].map(s => `dummy-sports-${at}-${s}`);
  // a draft that records its photo (routine.py draft_payload); one from before (only its brief); one without a photo
  await saveDraft({ id: ids[0], page: 'dummy-sports', page_name: 'Dummy', caption: 'x', headline: 'NEW',
                    photo: { rank: 1, of: 2, source: 'x', note: 'video frame', via: 'frame at 3s', flags: ['soft (sharpness 30)'] } }, tinyJpeg);
  await saveDraft({ id: ids[1], page: 'dummy-sports', page_name: 'Dummy', caption: 'x', headline: 'OLD', brief: { photos }, copy: { photo_index: 1, template: 'report' } }, tinyJpeg);
  await saveDraft({ id: ids[2], page: 'dummy-sports', page_name: 'Dummy', caption: 'x', headline: 'NONE', copy: { photo_index: -1 } }, tinyJpeg);
  await saveDraft({ id: ids[3], page: 'dummy-sports', page_name: 'Dummy', caption: 'x', headline: 'REDO', brief: { photos }, copy: { photo_index: 0 } }, tinyJpeg);
  for (const id of ids) await setStatus(id, 'pending', {});
  await owner.call('POST', '/api/decide', { id: ids[0], decision: 'reject', reason: 'Image quality', note: 'blurry frame' });
  await owner.call('POST', '/api/decide', { id: ids[1], decision: 'reject', reason: 'Image quality' });
  await owner.call('POST', '/api/decide', { id: ids[2], decision: 'reject', reason: 'Weak story' });
  assert.equal((await owner.call('POST', '/api/feedback', { id: ids[3], feedback: 'Wrong player in the photo, use the QB' })).status, 200);
  const entries = (await robot.call('GET', '/api/routine/reviews?days=1', undefined, R)).body.entries;
  const by = v => Object.fromEntries(entries.filter(e => e.verdict === v && ids.includes(e.draft_id)).map(e => [e.draft_id, e]));
  const rej = by('rejected');
  assert.deepEqual(rej[ids[0]].photo, { source: 'x', note: 'video frame', consensus: 0, flags: ['soft (sharpness 30)'], rank: 1, of: 2, via: 'frame at 3s' });
  assert.equal(rej[ids[0]].note, 'blurry frame');
  assert.deepEqual(rej[ids[1]].photo, { source: 'x', note: 'video frame', consensus: 0, flags: ['soft (sharpness 30)'], rank: 1, of: 2, via: '' },
    'drafts from before: the chosen photo from the brief they kept');
  assert.equal(rej[ids[1]].template, 'report');
  assert.equal(rej[ids[2]].photo, undefined);
  const redo = by('feedback')[ids[3]];
  assert.equal(redo.note, 'Wrong player in the photo, use the QB');
  assert.equal(redo.reason, 'Redo');
  assert.equal(redo.photo.source, 'web');
  // the regeneration failed, the post is back in Waiting and the editor asks again: both notes are kept
  assert.equal((await robot.call('POST', '/api/feedback', { id: ids[3], action: 'failed', error: 'no closer photo' }, R)).status, 200);
  await new Promise(r => setTimeout(r, 5));
  assert.equal((await owner.call('POST', '/api/feedback', { id: ids[3], feedback: 'Then use his team photo' })).status, 200);
  // a second reviewer whose page was drawn before is told in words what happened, never the status name
  for (const r of [await owner.call('POST', '/api/decide', { id: ids[3], decision: 'approve' }),
                   await owner.call('POST', '/api/feedback', { id: ids[3], feedback: 'Bigger headline' })])
    assert.deepEqual([r.status, r.body.error], [409, 'Someone already sent this back for a new version (see Regenerating)']);
  const redos = (await robot.call('GET', '/api/routine/reviews?days=1', undefined, R)).body.entries
    .filter(e => e.verdict === 'feedback' && e.draft_id === ids[3]).map(e => e.note);
  assert.deepEqual(redos.sort(), ['Then use his team photo', 'Wrong player in the photo, use the QB']);
  // backfill keeps the photo and accepts redo notes
  const bf = [{ verdict: 'feedback', page: 'dummy-sports', draft_id: 'dummy-sports-202610010000-bf', note: 'bigger face', decided_at: Date.now() - 3600e3,
                photo: { source: 'search', rank: 0, of: 3, flags: ['small: may look soft on a card'] } }];
  assert.equal((await robot.call('POST', '/api/routine/reviews', { entries: bf }, R)).body.added, 1);
  const got = (await robot.call('GET', '/api/routine/reviews?days=1', undefined, R)).body.entries.find(e => e.draft_id === bf[0].draft_id);
  assert.equal(got.photo.source, 'search');
  assert.equal(got.photo.rank, 0);
  // the editors' banner: what their notes keep saying and the photos they keep rejecting
  await robot.call('POST', '/api/routine/lessons', { page: 'dummy-sports', record: { kinds: {}, reviews_used: 9,
    note_themes: [{ id: 'photo', label: 'the photo', count: 3, notes: ['blurry frame'] }],
    photos: { frame: { label: 'a video frame', approved: 2, image_rejected: 4, picked: 0, passed: 0, n: 6, mult: 0.8, rule: true },
              'source:web': { label: "the article's own lead photo", approved: 9, image_rejected: 0, n: 9, mult: 1.1 } } } }, R);
  const lines = (await owner.call('GET', '/api/stats?since=0')).body.lessons.find(l => l.page === 'dummy-sports').lines;
  assert.ok(lines.includes('your notes: the photo (3)'), JSON.stringify(lines));
  assert.ok(lines.includes('fewer photos like a video frame (4 of 6 not okay for the image)'), JSON.stringify(lines));
  assert.equal(lines.filter(l => l.includes('lead photo')).length, 0, 'only photo features with a rule');
});

test('a post "as is" keeps all its images, in order, for editors and the publisher', async () => {
  const robot = client();
  const { setStatus } = await import('../lib/store.js');
  const stamp = new Date().toISOString().replace(/[-T:]/g, '').slice(0, 12);     // made now: not cleared as old
  const id = `dummy-sports-${stamp}-link-as-is`;
  const b64 = tinyJpeg.toString('base64');
  const up = await robot.call('POST', '/api/drafts', { id, page: 'dummy-sports', page_name: 'Dummy', caption: 'Mars', as_is: true,
                                                       image_b64: b64, extra_images_b64: [b64, b64] }, R);
  assert.equal(up.status, 201);
  const owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const d = (await owner.call('GET', '/api/drafts?status=pending')).body.drafts.find(x => x.id === id);
  assert.equal(d.extra_images.length, 2);
  assert.ok(d.as_is);
  await setStatus(id, 'approved', { caption: 'Mars' });
  const q = (await robot.call('GET', '/api/queue', undefined, R)).body.queue.find(x => x.id === id);
  assert.deepEqual(q.image_urls, [d.image_url, ...d.extra_images]);
  // an ordinary post: just its card
  const one = `dummy-sports-${stamp}-plain`;
  await robot.call('POST', '/api/drafts', { id: one, page: 'dummy-sports', page_name: 'Dummy', caption: 'c', image_b64: b64 }, R);
  await setStatus(one, 'approved', { caption: 'c' });
  assert.equal((await robot.call('GET', '/api/queue', undefined, R)).body.queue.find(x => x.id === one).image_urls.length, 1);
});

test('Studio jobs: one session per job, stalled jobs restart, pre-checks go stale on edits, preview text in sentences', async () => {
  const robot = client();
  const { putRecord, getRecord } = await import('../lib/db.js');
  const { wholeSentences, decodeEntities } = await import('../routes/studio.js');
  assert.equal(wholeSentences('When Jaxson Dart went down for the season, the Giants brought in McCarthy. And while some of that was due to a'),
               'When Jaxson Dart went down for the season, the Giants brought in McCarthy.');
  assert.equal(wholeSentences('A short cut-off preview with no full stop at all'), 'A short cut-off preview with no full stop at all…');
  assert.equal(wholeSentences('Complete sentence.'), 'Complete sentence.');
  // preview text: initials are not sentence ends, apostrophes and entities survive, emoji end a sentence
  assert.equal(wholeSentences('The Giants turned to J.J. McCarthy when Jaxson Dart went down for the season. And while some of that was due to a'),
               'The Giants turned to J.J. McCarthy when Jaxson Dart went down for the season.');
  assert.equal(wholeSentences('Some of that was due to a...'), 'Some of that was due to a…');
  assert.equal(wholeSentences('What a win 🏈'), 'What a win 🏈');
  assert.equal(decodeEntities('McCarthy&#8217;s role &hellip;'), 'McCarthy’s role …');

  const post = (id, extra = {}) => ({ id, story: { id: `s-${id}`, headline: 'H', summary: '' }, caption: 'Caption one', hashtags: ['#A'],
                                     check: { status: 'none' }, job: { id: 'job-test-1', kind: 'compose', status: 'queued' }, ...extra });
  const bt = { id: 'bt-test-1', page: 'dummy-sports', posts: ['p1', 'p2', 'p3', 'p4', 'p5'].map(id => post(id)) };
  bt.posts[4].job = { id: 'job-test-1', kind: 'compose', status: 'queued', error: '' };
  await putRecord('batches', bt.id, bt);
  const job = { id: 'job-test-1', kind: 'compose', batch_id: bt.id, page: 'dummy-sports', post_ids: bt.posts.map(p => p.id), status: 'queued',
                created_at: Date.now() - 5 * 60e3 };
  await putRecord('jobs', job.id, job);
  // two sessions start at once: only one gets the job
  const [a, b] = await Promise.all([robot.call('POST', '/api/routine/jobs/job-test-1', { status: 'running' }, R),
                                    robot.call('POST', '/api/routine/jobs/job-test-1', { status: 'running' }, R)]);
  assert.deepEqual([a.status, b.status].sort(), [200, 409]);
  // the composed posts (p5 failed): the Check step shows the automatic check under way
  const img = tinyJpeg.toString('base64');
  const made = { caption: 'Caption two', hashtags: ['#A'], image_b64: img };
  await robot.call('POST', '/api/routine/jobs/job-test-1', { status: 'done', posts: { p1: made, p2: made, p3: made, p4: made,
                                                                                         p5: { error: 'pick a photo where the face is clear' } } }, R);
  let ps = (await getRecord('batches', bt.id)).posts;
  assert.equal(ps[0].check.status, 'running'); assert.equal(ps[0].check.auto, true);
  assert.equal(ps[4].job.error, 'pick a photo where the face is clear');
  // a repeated `studio done` is refused: it would overwrite what the editor changed since
  assert.equal((await robot.call('POST', '/api/routine/jobs/job-test-1', { status: 'done', posts: { p1: { caption: 'again' } } }, R)).status, 409);
  // meanwhile the editor: starts another job on p1, edits p2's caption, makes a new visual for p3, skips p4's check
  const b0 = await getRecord('batches', bt.id);
  b0.posts[0].job = { id: 'job-test-9', kind: 'images', status: 'queued', error: '' };
  b0.posts[1].caption = 'Caption three';
  b0.posts[2].card_job = 'job-test-8';
  b0.posts[3].check = { status: 'skipped', at: Date.now() };
  await putRecord('batches', bt.id, b0);
  // the pre-check: only the checks, only of the visual this job made, out of date after an edit
  const checks = { factual: [], timing: [], grammar: [], other: [], guidelines: [] };
  const all = Object.fromEntries(['p1', 'p2', 'p3', 'p4'].map(id => [id, { check: checks }]));
  assert.equal((await robot.call('POST', '/api/routine/jobs/job-test-1', { precheck: true, posts: all }, R)).status, 200);
  ps = (await getRecord('batches', bt.id)).posts;
  assert.equal(ps[0].check.status, 'done'); assert.equal(ps[0].check.stale, false); assert.equal(ps[0].check.auto, true);
  assert.equal(ps[0].job.id, 'job-test-9', 'the other job is still shown');
  assert.equal(ps[1].check.stale, true, 'the caption was edited since');
  assert.equal(ps[2].check.status, 'running', 'a check of the old visual is not one of the new');
  assert.equal(ps[3].check.status, 'skipped', 'the editor skipped it');
  assert.equal(ps[4].job.error, 'pick a photo where the face is clear', 'failure reasons stay');

  // a job whose session died is offered again after 20 minutes, whatever its record says
  const mem = globalThis.__boreBlobs;
  await putRecord('jobs', 'job-test-3', { ...job, id: 'job-test-3', status: 'running', started_at: Date.now() - 5 * 60e3 });
  assert.equal((await robot.call('POST', '/api/routine/jobs/job-test-3', { status: 'running' }, R)).status, 409, 'a live session has it');
  await putRecord('jobs', 'job-test-4', { ...job, id: 'job-test-4' });
  assert.equal((await robot.call('POST', '/api/routine/jobs/job-test-4', { status: 'running' }, R)).status, 200);
  await putRecord('jobs', 'job-test-4', { ...job, id: 'job-test-4' });           // a lost update put it back to 'queued'
  assert.equal((await robot.call('POST', '/api/routine/jobs/job-test-4', { status: 'running' }, R)).status, 409, 'its claim is fresh');
  mem.get('job-claims/job-test-4/0.json').uploadedAt = new Date(Date.now() - 21 * 60e3);
  const [c, d] = await Promise.all([robot.call('POST', '/api/routine/jobs/job-test-4', { status: 'running' }, R),
                                    robot.call('POST', '/api/routine/jobs/job-test-4', { status: 'running' }, R)]);
  assert.deepEqual([c.status, d.status].sort(), [200, 409], 'claimed again, by one session');
  // a session that could not prepare a job (a problem on its machine) hands it back: queued, claimable at once
  const bt3 = { ...bt, id: 'bt-test-3', posts: [{ ...bt.posts[0], job: { id: 'job-test-5', kind: 'compose', status: 'queued' } }] };
  await putRecord('batches', bt3.id, bt3);
  await putRecord('jobs', 'job-test-5', { ...job, id: 'job-test-5', batch_id: bt3.id, post_ids: ['p1'] });
  assert.equal((await robot.call('POST', '/api/routine/jobs/job-test-5', { status: 'running' }, R)).status, 200);
  assert.equal((await getRecord('batches', bt3.id)).posts[0].job.status, 'running');
  assert.equal((await robot.call('POST', '/api/routine/jobs/job-test-5', { status: 'queued', release: true }, R)).status, 200);
  assert.equal((await getRecord('jobs', 'job-test-5')).status, 'queued');
  assert.equal((await getRecord('batches', bt3.id)).posts[0].job.status, 'queued');
  assert.equal([...mem.keys()].filter(k => k.startsWith('job-claims/job-test-5/')).length, 0, 'the claim is gone');
  assert.equal((await robot.call('POST', '/api/routine/jobs/job-test-5', { status: 'queued', release: true }, R)).status, 409, 'only a running job');
  assert.equal((await robot.call('POST', '/api/routine/jobs/job-test-5', { status: 'running' }, R)).status, 200, 'claimed again at once');
  // a job that cannot be prepared on any machine never holds up the others: the third hand-back fails it, and the editor is told
  await robot.call('POST', '/api/routine/jobs/job-test-5', { status: 'queued', release: true, error: 'AttributeError: x' }, R);
  assert.equal((await robot.call('POST', '/api/routine/jobs/job-test-5', { status: 'running' }, R)).status, 200);
  assert.equal((await robot.call('POST', '/api/routine/jobs/job-test-5', { status: 'queued', release: true, error: 'AttributeError: x' }, R)).body.status, 'failed');
  const j5 = await getRecord('jobs', 'job-test-5'), p5 = (await getRecord('batches', bt3.id)).posts[0];
  assert.equal(j5.status, 'failed'); assert.equal(j5.releases, 3);
  assert.equal(p5.job.status, 'failed'); assert.match(p5.job.error, /could not be prepared \(tried on 3 Studio machines\)/);
  // the machine's own error is for the admin, never in the editor's sentence
  assert.doesNotMatch(p5.job.error, /AttributeError/); assert.deepEqual([p5.job.detail, j5.detail], ['AttributeError: x', 'AttributeError: x']);
  assert.equal((await robot.call('POST', '/api/routine/jobs/job-test-5', { status: 'running' }, R)).status, 409, 'not claimed again');

  // a job nobody picked up is started again while the editor's page waits (every 4 minutes, at most 5 times)
  const bt2 = { ...bt, id: 'bt-test-2', posts: [{ ...bt.posts[0], job: { id: 'job-test-2', kind: 'ideas', status: 'queued' } }] };
  await putRecord('batches', bt2.id, bt2);
  await putRecord('jobs', 'job-test-2', { ...job, id: 'job-test-2', kind: 'ideas', batch_id: bt2.id, status: 'queued' });
  const owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const nudges = () => [...mem.keys()].filter(k => k.startsWith('job-nudges/job-test-2/')).length;
  await Promise.all([owner.call('GET', '/api/studio/batches/bt-test-2'), owner.call('GET', '/api/studio/batches/bt-test-2')]);
  assert.equal(nudges(), 1, 'two pages polling at once start one session');
  await owner.call('GET', '/api/studio/batches/bt-test-2');
  assert.equal(nudges(), 1, 'not again within 4 minutes');
  assert.equal((await getRecord('jobs', 'job-test-2')).status, 'queued', 'the job record is not written');
  // no machine can take the job (its packages do not load): the third session that leaves it tells the editor
  const bt5 = { ...bt, id: 'bt-test-5', posts: [{ ...bt.posts[0], job: { id: 'job-test-7', kind: 'compose', status: 'queued' } }] };
  await putRecord('batches', bt5.id, bt5);
  await putRecord('jobs', 'job-test-7', { ...job, id: 'job-test-7', batch_id: bt5.id, post_ids: ['p1'], status: 'queued' });
  const leave = () => robot.call('POST', '/api/routine/jobs/job-test-7', { left: true, error: 'packages not loading: Pillow' }, R);
  assert.equal((await leave()).body.status, 'queued');
  assert.equal((await leave()).body.status, 'queued');
  assert.equal((await leave()).body.status, 'failed');
  const p7 = (await getRecord('batches', bt5.id)).posts[0];
  assert.equal(p7.job.status, 'failed'); assert.match(p7.job.error, /^The Studio machines could not start this step\. Press the button again/);
  assert.doesNotMatch(p7.job.error, /Pillow/); assert.equal(p7.job.detail, 'packages not loading: Pillow');
  assert.equal((await getRecord('jobs', 'job-test-7')).status, 'failed');
  // restarted 5 times and still nobody took it: the editor is told
  const bt6 = { ...bt, id: 'bt-test-6', posts: [{ ...bt.posts[0], job: { id: 'job-test-8', kind: 'ideas', status: 'queued' } }] };
  await putRecord('batches', bt6.id, bt6);
  await putRecord('jobs', 'job-test-8', { ...job, id: 'job-test-8', kind: 'ideas', batch_id: bt6.id, status: 'queued', created_at: Date.now() - 30 * 60e3 });
  const { put } = await import('../lib/blob.js');
  for (let n = 0; n < 5; n++) await put(`job-nudges/job-test-8/${n}.json`, '{}', { access: 'public', addRandomSuffix: false });
  for (let n = 0; n < 5; n++) mem.get(`job-nudges/job-test-8/${n}.json`).uploadedAt = new Date(Date.now() - (25 - 4 * n) * 60e3);
  await owner.call('GET', '/api/studio/batches/bt-test-6');
  const p8 = (await getRecord('batches', bt6.id)).posts[0];
  assert.equal(p8.job.status, 'failed'); assert.match(p8.job.error, /after 5 restarts/);
  // a job just handed back: its session takes it again, no new session is started for it
  const bt4 = { ...bt, id: 'bt-test-4', posts: [{ ...bt.posts[0], job: { id: 'job-test-6', kind: 'compose', status: 'queued' } }] };
  await putRecord('batches', bt4.id, bt4);
  await putRecord('jobs', 'job-test-6', { ...job, id: 'job-test-6', batch_id: bt4.id, status: 'queued', released_at: Date.now() });
  await owner.call('GET', '/api/studio/batches/bt-test-4');
  assert.equal([...mem.keys()].filter(k => k.startsWith('job-nudges/job-test-6/')).length, 0);
});

// ---- the publisher's rules, checked by the dashboard as well (findings 28-35, 76) ----------------
const tick = () => new Promise(r => setTimeout(r, 5));      // status records are named by the millisecond

test('publisher: a result is taken only after the step before it', async () => {
  const robot = client();
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const ids = ['pending', 'withdrawn', 'unclaimed', 'flow'].map(s => `dummy-sports-202610210700-order-${s}`);
  for (const id of ids) await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: 'x' }, tinyJpeg);
  await setStatus(ids[0], 'pending', {});
  await setStatus(ids[1], 'approved', { caption: 'x' }); await tick(); await setStatus(ids[1], 'rejected', { withdrawn: true });
  await setStatus(ids[2], 'approved', { caption: 'x' });
  await setStatus(ids[3], 'approved', { caption: 'x' });
  const report = (id, status, extra = {}) => robot.call('POST', '/api/result', { id, status, ...extra }, R);
  // never approved, withdrawn, or approved but never claimed: SocialPilot cannot have it from us
  for (const id of ids.slice(0, 3)) {
    assert.equal((await report(id, 'scheduled', { scheduled_for: '2026-10-21T09:00:00Z' })).status, 409, id);
    assert.equal((await report(id, 'posted')).status, 409, id);
  }
  assert.equal((await report(ids[0], 'failed', { error: 'x' })).status, 409, 'a pending draft never failed to publish');
  assert.equal((await report(ids[3], 'publishing', { scheduled_for: '2026-10-21T09:00:00Z' })).status, 200); await tick();
  assert.equal((await report(ids[3], 'scheduled', { scheduled_for: '2026-10-21T09:00:00Z', post_id: 'cp-1', sp_post_id: 'sp-1', sp_login_id: 700001 })).status, 200);
  await tick();
  assert.equal((await report(ids[3], 'scheduled', { scheduled_for: '2026-10-21T09:05:00Z', post_id: 'cp-1' })).status, 200, 'scheduled again: corrected');
  await tick();
  assert.equal((await report(ids[3], 'posted', { url: 'https://facebook.com/x' })).status, 200);
  assert.equal((await report(ids[3], 'failed', { error: 'x' })).status, 409, 'already posted');
});

test('publisher: a run\'s breaker can hold a draft it failed; delivery misses count per run', async () => {
  const robot = client(), owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const { putRecord, getRecord } = await import('../lib/db.js');
  await putRecord('health', 'socialpilot', {});
  const report = (id, status, extra = {}) => robot.call('POST', '/api/result', { id, status, ...extra }, R);
  // the breaker: a draft this run failed moments ago was the same outage, so it waits too
  const b = 'dummy-sports-202610210710-breaker-first';
  await saveDraft({ id: b, page: 'dummy-sports', page_name: 'Dummy', caption: 'b' }, tinyJpeg);
  await setStatus(b, 'approved', { caption: 'b' });
  const claim = (await report(b, 'publishing')).body.claim; await tick();
  assert.equal((await report(b, 'failed', { error: '401 Unauthorized' })).status, 200); await tick();
  assert.equal((await report(b, 'held', { error: '401 Unauthorized' })).status, 409, 'without the claim: no');
  assert.equal((await report(b, 'held', { error: '401 Unauthorized', claim: 'other' })).status, 409, 'another run\'s claim: no');
  const h = await report(b, 'held', { error: '401 Unauthorized', claim });
  assert.equal(h.body.status, 'held');
  let item = (await robot.call('GET', '/api/queue', undefined, R)).body.queue.find(x => x.id === b);
  assert.equal(item.holds, 1);
  assert.equal(item.ever_sent, true, 'it was claimed before: looked up in SocialPilot before it is created again');
  assert.equal(item.prior_post_id, null, 'never scheduled: no earlier post');
  await putRecord('health', 'socialpilot', {});
  // a scheduled post nobody can find: counted, failed only when a second run misses it too
  const m = 'dummy-sports-202610210720-missing-post';
  await saveDraft({ id: m, page: 'dummy-sports', page_name: 'Dummy', caption: 'm' }, tinyJpeg);
  await setStatus(m, 'approved', { caption: 'm' }); await tick();
  await report(m, 'publishing'); await tick();
  await report(m, 'scheduled', { scheduled_for: new Date(Date.now() - 90 * 60e3).toISOString(), post_id: 'cp-9', sp_post_id: 'sp-q-9', sp_login_id: 700001 });
  const sch = (await robot.call('GET', '/api/queue', undefined, R)).body.scheduled.find(x => x.id === m);
  assert.deepEqual([sch.sp_post_id, sch.sp_login_id, sch.caption_start], ['sp-q-9', 700001, 'm'], 'what the delivery check looks it up with');
  assert.equal((await report(m, 'missing')).status, 409, 'not 2 hours past its time: SocialPilot may just be late');
  await tick();
  await report(m, 'scheduled', { scheduled_for: new Date(Date.now() - 3 * 3600e3).toISOString(), post_id: 'cp-9', sp_post_id: 'sp-q-9', sp_login_id: 700001 });
  assert.equal((await report(m, 'missing')).body.misses, 1);
  assert.equal((await report(m, 'missing')).body.misses, 1, 'the same run reporting again is not a second miss');
  const rec = await getRecord('health', 'delivery');
  rec.misses[m].last_at -= 31 * 60e3;
  await putRecord('health', 'delivery', rec);
  const second = await report(m, 'missing');
  assert.equal(second.body.status, 'failed');
  assert.match(second.body.error, /^not delivered: .*look on the Facebook page/);
  const pub = (await owner.call('GET', '/api/stats?since=0')).body.publisher;
  assert.match(pub.needs_action[m].text, /look on the Facebook page/, 'an editor is asked to look on Facebook');
  // no one-click retry: the editor confirms they looked; then it carries ever_sent and the earlier post
  const r1 = await owner.call('POST', '/api/decide', { ids: [m], decision: 'retry' });
  assert.deepEqual([r1.body.retried, r1.body.refused[0].id], [0, m]);
  assert.equal((await owner.call('POST', '/api/decide', { id: m, decision: 'approve' })).status, 409);
  assert.equal((await owner.call('POST', '/api/decide', { ids: [m], decision: 'retry', confirmed: true })).body.retried, 1);
  item = (await robot.call('GET', '/api/queue', undefined, R)).body.queue.find(x => x.id === m);
  assert.deepEqual([item.ever_sent, item.prior_post_id, item.holds], [true, 'sp-q-9', 0]);
  assert.equal((await owner.call('GET', '/api/stats?since=0')).body.publisher.needs_action[m], undefined, 'retrying settles it');
});

test('publisher: what needs a person is shown until someone marks it done', async () => {
  const robot = client(), owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const report = (id, status, extra = {}) => robot.call('POST', '/api/result', { id, status, ...extra }, R);
  const o = 'dummy-sports-202610210730-outdated-original', d = 'dummy-sports-202610210740-double-copy';
  for (const id of [o, d]) { await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: id }, tinyJpeg); await setStatus(id, 'approved', { caption: id }); }
  await tick();
  for (const id of [o, d]) { await report(id, 'publishing'); await tick(); }
  await report(o, 'scheduled', { scheduled_for: '2026-10-21T12:00:00Z', post_id: 'cp-o' });
  // `publish mark --status scheduled` found two copies in SocialPilot's queue
  await report(d, 'scheduled', { scheduled_for: '2026-10-21T13:00:00Z', post_id: 'cp-d', copies: 2,
                                 needs_action: '2 copies are queued in SocialPilot: delete all but one' });
  // the plan found the original's update approved while it is still in SocialPilot
  assert.equal((await report(o, 'needs_action', {})).status, 400, 'say what to do');
  const na = await report(o, 'needs_action', { error: 'delete it in SocialPilot: it is scheduled for 2026-10-21 17:30 and its update replaces it',
                                               kind: 'superseded_original' });
  assert.equal(na.body.status, 'scheduled', 'its status does not change');
  await report(o, 'needs_action', { error: 'delete it in SocialPilot: it is scheduled for 2026-10-21 17:30 and its update replaces it',
                                    kind: 'superseded_original' });          // every plan reports it again: one note
  let acts = (await owner.call('GET', '/api/stats?since=0')).body.publisher.needs_action;
  assert.match(acts[o].text, /^delete it in SocialPilot/);
  assert.match(acts[d].text, /2 copies/);
  assert.equal(acts[d].page, 'dummy-sports');
  const listed = (await owner.call('GET', '/api/drafts?status=scheduled')).body.drafts.find(x => x.id === d);
  assert.equal(listed.status_info.result.copies, 2);
  assert.equal(listed.status_info.result.needs_action, undefined, 'kept in one place');
  assert.equal((await client().call('POST', '/api/decide', { id: o, decision: 'done' })).status, 401);
  assert.equal((await owner.call('POST', '/api/decide', { id: o, decision: 'done' })).status, 200);
  acts = (await owner.call('GET', '/api/stats?since=0')).body.publisher.needs_action;
  assert.equal(acts[o], undefined);
  // the original was deleted in SocialPilot: no longer scheduled, so no plan or delivery check looks for it
  const q = (await robot.call('GET', '/api/queue', undefined, R)).body;
  assert.ok(!q.scheduled.some(x => x.id === o));
  assert.ok(q.scheduled.some(x => x.id === d), 'one copy of the double stays scheduled');
  await owner.call('POST', '/api/decide', { id: d, decision: 'done' });
  assert.ok((await robot.call('GET', '/api/queue', undefined, R)).body.scheduled.some(x => x.id === d));
});

test('publisher: waking the routine never fails an approval; retry refuses superseded originals', async () => {
  const robot = client(), owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const { putRecord } = await import('../lib/db.js');
  await putRecord('health', 'socialpilot', {});
  const FIRE = 'https://api.example.com/fire';
  process.env.ROUTINE_FIRE_URL = FIRE; process.env.ROUTINE_FIRE_TOKEN = 'fire-token';
  const prev = globalThis.fetch, signals = [];
  globalThis.fetch = async (u, opts) => {
    if (String(u) !== FIRE) return prev(u, opts);
    signals.push(opts && opts.signal);
    throw new TypeError('fetch failed');
  };
  try {
    const at = new Date().toISOString().replace(/\D/g, '').slice(0, 12);
    const a = `dummy-sports-${at}-fire-throws`;
    await saveDraft({ id: a, page: 'dummy-sports', page_name: 'Dummy', caption: 'a' }, tinyJpeg);
    await setStatus(a, 'pending', {});
    const r = await owner.call('POST', '/api/decide', { id: a, decision: 'approve' });
    assert.equal(r.status, 200);
    assert.equal(r.body.status, 'approved', 'approved: the next run publishes it');
    assert.equal(r.body.result.fired, false);
    assert.ok(signals[0] instanceof AbortSignal, 'the wake-up call gives up after a few seconds');
    assert.equal((await robot.call('GET', '/api/drafts?status=approved', undefined, R)).body.drafts.filter(x => x.id === a).length, 1);
    assert.equal((await robot.call('GET', '/api/stats?since=0', undefined, R)).body.publisher.blocked, false, 'no SocialPilot banner');
    // bulk retry still answers (it used to be a 500 after re-approving)
    const f = `dummy-sports-${at}-fire-retry`, s = `dummy-sports-${at}-superseded-original`;
    for (const id of [f, s]) { await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: id }, tinyJpeg); await setStatus(id, 'approved', { caption: id }); }
    await tick();
    await robot.call('POST', '/api/result', { id: f, status: 'failed', error: 'Error uploading media' }, R);
    await robot.call('POST', '/api/result', { id: s, status: 'failed', error: 'superseded by its update dummy-sports-x' }, R);
    const rt = await owner.call('POST', '/api/decide', { ids: [f, s], decision: 'retry' });
    assert.equal(rt.status, 200);
    assert.equal(rt.body.retried, 1);
    assert.deepEqual(rt.body.refused, [{ id: s, why: 'its update replaced it' }]);
    assert.equal((await owner.call('POST', '/api/decide', { id: s, decision: 'approve' })).status, 409);
  } finally {
    globalThis.fetch = prev;
    delete process.env.ROUTINE_FIRE_URL; delete process.env.ROUTINE_FIRE_TOKEN;
  }
});

test('a page\'s SocialPilot account is never typed in: only the onboarding routine links it', async () => {
  const owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const WS = (await owner.call('GET', '/api/admin/workspaces')).body.workspaces[0].id;
  const made = await owner.call('POST', '/api/admin/pages', { name: 'Typed Login', workspace: WS, socialpilot_login_id: '700002' });
  assert.equal(made.status, 400);
  assert.match(made.body.error, /linked by the onboarding routine/);
  const slug = (await owner.call('POST', '/api/admin/pages', { name: 'Good Login', workspace: WS })).body.page.slug;
  const r = await owner.call('PATCH', `/api/admin/pages/${slug}`, { socialpilot_login_id: '700002' });
  assert.equal(r.status, 400);
  assert.match(r.body.error, /linked by the onboarding routine/);
  assert.equal((await owner.call('PATCH', `/api/admin/pages/${slug}`, { name: 'Good Login 2' })).status, 200, 'other fields as before');
  assert.equal((await owner.call('GET', '/api/admin/pages')).body.pages.find(p => p.slug === slug).socialpilot_login_id, '');
});

test('publisher: one page\'s account or one lookup holds only that draft, never "SocialPilot is down"', async () => {
  const robot = client(), owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const { putRecord, getRecord } = await import('../lib/db.js');
  await putRecord('health', 'socialpilot', {});
  const report = (id, status, extra = {}) => robot.call('POST', '/api/result', { id, status, ...extra }, R);
  const queued = async id => (await robot.call('GET', '/api/queue', undefined, R)).body.queue.find(x => x.id === id);
  const a = 'dummy-sports-202610211000-account-held', l = 'dummy-sports-202610211010-lookup-held';
  for (const id of [a, l]) { await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: id }, tinyJpeg); await setStatus(id, 'approved', { caption: id }); }
  await tick();
  // the breaker on one page's account: that draft waits, SocialPilot is not marked down for every page
  let claim = (await report(a, 'publishing')).body.claim; await tick();
  assert.equal((await report(a, 'held', { error: 'Page needs to be reconnected', claim, scope: 'account' })).body.status, 'held');
  assert.equal(((await getRecord('health', 'socialpilot')) || {}).down, undefined);
  assert.deepEqual([(await queued(a)).holds, (await queued(a)).held_scope], [1, 'account']);
  const pub = (await owner.call('GET', '/api/stats?since=0')).body.publisher;
  assert.equal(pub.held, undefined, 'no "SocialPilot is unavailable" banner');
  // a lookup that failed: only that draft waits; three in a row go to a person (it may exist already)
  for (let n = 1; n <= 3; n++) {
    claim = (await report(l, 'publishing')).body.claim; await tick();
    const h = await report(l, 'held', { error: 'lookup failed: ViewPost: Request timed out', claim, scope: 'draft' }); await tick();
    assert.equal(h.body.status, n < 3 ? 'held' : 'failed', `lookup failure ${n}`);
    if (n < 3) assert.equal((await queued(l)).held_scope, 'draft');
  }
  assert.equal(((await getRecord('health', 'socialpilot')) || {}).down, undefined);
  const failed = (await owner.call('GET', '/api/drafts?status=failed')).body.drafts.find(x => x.id === l);
  assert.match(failed.status_info.error, /^lookups failed: .*look in SocialPilot and on the Facebook page/);
  assert.equal(failed.status_info.check_facebook, true);
  assert.match((await owner.call('GET', '/api/stats?since=0')).body.publisher.needs_action[l].text, /look in SocialPilot/);
  assert.deepEqual((await owner.call('POST', '/api/decide', { ids: [l], decision: 'retry' })).body.refused, [{ id: l, why: 'look on the Facebook page first' }]);
  // SocialPilot itself refusing (no scope) still marks it down, as before
  claim = (await report(a, 'publishing')).body.claim; await tick();
  assert.equal((await report(a, 'held', { error: 'SocialPilot error: Your Account is Locked', claim })).body.status, 'held');
  assert.equal((await getRecord('health', 'socialpilot')).down, true);
  assert.equal((await queued(a)).held_scope, '');
  await putRecord('health', 'socialpilot', {});
});

test('publisher: a copy on the wrong account keeps its red note through a retry', async () => {
  const robot = client(), owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const report = (id, status, extra = {}) => robot.call('POST', '/api/result', { id, status, ...extra }, R);
  const notes = async () => (await owner.call('GET', '/api/stats?since=0')).body.publisher.needs_action;
  const w = 'dummy-sports-202610211020-wrong-account';
  await saveDraft({ id: w, page: 'dummy-sports', page_name: 'Dummy', caption: 'w' }, tinyJpeg);
  await setStatus(w, 'approved', { caption: 'w' }); await tick();
  await report(w, 'publishing'); await tick();
  // what `publish mark --status scheduled` sends when the queued copy is on an account on the deny list
  await report(w, 'failed', { error: 'scheduled on the wrong SocialPilot account: delete it in SocialPilot, then retry',
                              post_id: 'cp-w', sp_post_id: 'sp-w', sp_login_id: 900001, copies: 1,
                              needs_action: 'a copy is on the wrong SocialPilot account (Other Account, loginId 900001): delete it in SocialPilot' });
  assert.match((await notes())[w].text, /wrong SocialPilot account/);
  const plain = await owner.call('POST', '/api/decide', { ids: [w], decision: 'retry' });
  assert.deepEqual([plain.body.retried, plain.body.refused], [0, [{ id: w, why: 'do what its red note says first' }]]);
  assert.equal((await owner.call('POST', '/api/decide', { id: w, decision: 'approve' })).status, 409);
  assert.match((await notes())[w].text, /wrong SocialPilot account/, 'the only reminder is still there');
  // the editor confirms they deleted the copy: it goes out on the right account, the note stays until Done
  assert.equal((await owner.call('POST', '/api/decide', { ids: [w], decision: 'retry', confirmed: true })).body.retried, 1);
  assert.ok((await notes())[w]);
  await owner.call('POST', '/api/decide', { id: w, decision: 'done' });
  assert.equal((await notes())[w], undefined);
});

test('publisher: "SocialPilot is refusing posts" only for SocialPilot\'s own refusals', async () => {
  const robot = client(), owner = client();
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const { putRecord } = await import('../lib/db.js');
  await putRecord('health', 'socialpilot', {});
  const report = (id, status, extra = {}) => robot.call('POST', '/api/result', { id, status, ...extra }, R);
  const banner = async () => (await owner.call('GET', '/api/stats?since=0')).body.publisher;
  const ids = ['ok', 'no-account', 'wrong-account', 'lost', 'refused', 'ok-again'].map(s => `dummy-sports-202610211030-banner-${s}`);
  for (const id of ids) { await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: id }, tinyJpeg); await setStatus(id, 'approved', { caption: id }); }
  await tick();
  await report(ids[0], 'publishing'); await tick();
  await report(ids[0], 'scheduled', { scheduled_for: '2026-10-21T12:00:00Z', post_id: 'cp-ok' }); await tick();
  await report(ids[1], 'failed', { error: 'page has no SocialPilot account: set it in the admin panel, then retry' }); await tick();
  await report(ids[2], 'failed', { error: 'scheduled on the wrong SocialPilot account: delete it in SocialPilot, then retry' }); await tick();
  await setStatus(ids[3], 'failed', { caption: ids[3], error: 'look again later', check_facebook: true }); await tick();
  assert.equal((await banner()).blocked, false, 'the publisher\'s own verdicts are not SocialPilot refusing posts');
  await report(ids[4], 'failed', { error: 'Error uploading media' }); await tick();
  const b = await banner();
  assert.deepEqual([b.blocked, b.error, b.failed_since_ok], [true, 'Error uploading media', 1]);
  await report(ids[5], 'publishing'); await tick();
  await report(ids[5], 'scheduled', { scheduled_for: '2026-10-21T13:00:00Z', post_id: 'cp-ok-2' });
  assert.equal((await banner()).blocked, false);
});

test('publisher: a resent post counts what the old one did; ViewPost gets CreatePost\'s id', async () => {
  const robot = client();
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const report = (id, status, extra = {}) => robot.call('POST', '/api/result', { id, status, ...extra }, R);
  const o = 'dummy-sports-202610211040-resent-original', n = 'dummy-sports-202610211050-resent-again';
  await saveDraft({ id: o, page: 'dummy-sports', page_name: 'Dummy', caption: 'o' }, tinyJpeg);
  await setStatus(o, 'approved', { caption: 'o' }); await tick();
  await report(o, 'publishing'); await tick();
  await report(o, 'scheduled', { scheduled_for: '2026-10-21T14:00:00Z', post_id: '0fa8d79d-653f-48f8-9682-1f9d0a220b14' });
  const sch = (await robot.call('GET', '/api/queue', undefined, R)).body.scheduled.find(x => x.id === o);
  assert.equal(sch.sp_post_id, '0fa8d79d-653f-48f8-9682-1f9d0a220b14', 'the delivery check can ViewPost it');
  await tick();
  await report(o, 'failed', { error: 'Error uploading media' });
  // the Studio sends it again under a new id: it is looked up in SocialPilot before it is created
  await saveDraft({ id: n, page: 'dummy-sports', page_name: 'Dummy', caption: 'o', resent_from: o }, tinyJpeg);
  await setStatus(n, 'approved', { caption: 'o' });
  const item = (await robot.call('GET', '/api/queue', undefined, R)).body.queue.find(x => x.id === n);
  assert.deepEqual([item.ever_sent, item.prior_post_id], [true, '0fa8d79d-653f-48f8-9682-1f9d0a220b14']);
  assert.equal(item.resent_from, o, 'a breaker flag left by the old draft\'s failure is not held against it (bore/cli.py)');
});

test('publisher: a claim keeps 45 minutes from the page\'s other posts, whichever run planned it', async () => {
  const robot = client();
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const report = (id, status, extra = {}) => robot.call('POST', '/api/result', { id, status, ...extra }, R);
  const base = Math.ceil(Date.now() / 3600e3) * 3600e3 + 2 * 3600e3;
  const at = m => new Date(base + m * 60e3).toISOString();
  const ids = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 's'].map(s => `dummy-slots-202610221200-slot-${s}`);
  await livePage('dummy-slots');
  for (const id of ids) { await saveDraft({ id, page: 'dummy-slots', page_name: 'Slots', caption: id }, tinyJpeg); await setStatus(id, 'approved', { caption: id }); }
  await tick();
  // run A claimed a; run B, which planned a few seconds apart, asks 40 minutes from it: it plans again
  assert.equal((await report(ids[0], 'publishing', { scheduled_for: at(0) })).status, 200);
  const near = await report(ids[1], 'publishing', { scheduled_for: at(40) });
  assert.equal(near.status, 409);
  assert.match(near.body.error, /^another post on this page \(dummy-slots-202610221200-slot-a\) goes out at .*less than 45 minutes from this slot: it gets a new time when you run publish plan again$/);
  assert.equal((await report(ids[1], 'publishing', { scheduled_for: at(45) })).status, 200, 'the new plan\'s slot');
  // two runs claiming two drafts for the same minute at the same moment: only one gets it
  const both = await Promise.all([ids[2], ids[3]].map(id => report(id, 'publishing', { scheduled_for: at(120) })));
  assert.deepEqual(both.map(r => r.status).sort(), [200, 409]);
  const [won, other] = both[0].status === 200 ? [ids[2], ids[3]] : [ids[3], ids[2]];
  // a claim that failed frees its slot
  await tick();
  assert.equal((await report(won, 'failed', { error: 'Error uploading media' })).status, 200);
  assert.equal((await report(other, 'publishing', { scheduled_for: at(120) })).status, 200);
  // SocialPilot keeps a post at its own time (an earlier copy the duplicate guard found): that time counts
  assert.equal((await report(ids[4], 'publishing', { scheduled_for: at(240) })).status, 200); await tick();
  assert.equal((await report(ids[4], 'scheduled', { scheduled_for: at(300), post_id: 'cp-e' })).status, 200);
  assert.equal((await report(ids[5], 'publishing', { scheduled_for: at(310) })).status, 409);
  assert.equal((await report(ids[5], 'publishing', { scheduled_for: at(345) })).status, 200);
  // an editor's own Studio time is kept as chosen; the others move around it
  await setStatus(ids[7], 'approved', { caption: ids[7], requested_at: base + 370 * 60e3, mode: 'schedule' }); await tick();
  assert.equal((await report(ids[7], 'publishing', { scheduled_for: at(370) })).status, 200);
  assert.equal((await report(ids[6], 'publishing', { scheduled_for: at(400) })).status, 409);
  const q = (await robot.call('GET', '/api/queue', undefined, R)).body;
  assert.ok(q.queue.some(x => x.id === ids[6]), 'refused: still approved, for the next plan');
});

test('publisher: a Studio post sent again as a new draft: the old one is never scheduled', async () => {
  const robot = client();
  const { saveDraft, setStatus, loadDraft } = await import('../lib/store.js');
  const { putRecord } = await import('../lib/db.js');
  const report = (id, status, extra = {}) => robot.call('POST', '/api/result', { id, status, ...extra }, R);
  const later = h => new Date(Date.now() + h * 3600e3).toISOString();
  const o = 'dummy-sports-202610221100-studio-rs1', n = 'dummy-sports-202610221110-studio-rs1';
  const c = 'dummy-sports-202610221100-studio-rs2', lost = 'dummy-sports-202610221120-studio-rs2';
  const studio = (post, publish) => putRecord('batches', 'bt-resent', { id: 'bt-resent', page: 'dummy-sports', posts: [
    { id: 'rs1', publish: post === 'rs1' ? publish : { draft_id: o, state: 'failed' } },
    { id: 'rs2', publish: post === 'rs2' ? publish : { draft_id: c, state: 'failed' } }] });
  for (const [id, post] of [[o, 'rs1'], [c, 'rs2']]) {
    await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: id, studio: { batch: 'bt-resent', post, user: 'ed@example.com' } }, tinyJpeg);
    await setStatus(id, 'approved', { caption: id }); await tick();
    await report(id, 'publishing'); await tick();
    await report(id, 'failed', { error: 'page has no SocialPilot account: set it in the admin panel, then retry' }); await tick();
  }
  // the editor presses Send again in the Studio while a reviewer retries the old draft (Failed tab)
  await studio('rs1', { draft_id: n, state: 'sending', sending_at: Date.now(), resent_from: o });
  await setStatus(o, 'approved', { caption: o, retry: true }); await tick();
  const mid = await report(o, 'publishing', { scheduled_for: later(2) });
  assert.equal(mid.status, 409);
  assert.match(mid.body.error, /the Studio is sending this post again/);
  assert.equal((await loadDraft(o)).status, 'approved', 'nothing decided while the Send is being made');
  await saveDraft({ id: n, page: 'dummy-sports', page_name: 'Dummy', caption: 'edited', resent_from: o,
                    studio: { batch: 'bt-resent', post: 'rs1', user: 'ed@example.com' } }, tinyJpeg);
  await setStatus(n, 'approved', { caption: 'edited' });
  await studio('rs1', { draft_id: n, state: 'approved', resent_from: o });
  const r = await report(o, 'publishing', { scheduled_for: later(2) });
  assert.equal(r.status, 409);
  assert.equal(r.body.error, `resent from the Studio as ${n}: never schedule it`);
  const old = await loadDraft(o);
  assert.deepEqual([old.status, old.statusBody.reason, old.statusBody.withdrawn], ['rejected', `resent from the Studio as ${n}`, true]);
  const q = (await robot.call('GET', '/api/queue', undefined, R)).body.queue;
  assert.deepEqual([q.some(x => x.id === o), q.some(x => x.id === n)], [false, true]);
  assert.equal((await report(n, 'publishing', { scheduled_for: later(2) })).status, 200, 'the new one goes out');
  // a Send cut short long ago made no draft: the old one is still the post, and goes out
  await studio('rs2', { draft_id: lost, state: 'sending', sending_at: Date.now() - 3600e3, resent_from: c });
  await setStatus(c, 'approved', { caption: c, retry: true }); await tick();
  assert.equal((await report(c, 'publishing', { scheduled_for: later(5) })).status, 200);
});

test('publisher: a resent draft waits while the draft it was resent from is being scheduled', async () => {
  const robot = client();
  const { saveDraft, setStatus, loadDraft } = await import('../lib/store.js');
  const { putRecord } = await import('../lib/db.js');
  const report = (id, status, extra = {}) => robot.call('POST', '/api/result', { id, status, ...extra }, R);
  const later = h => new Date(Math.ceil(Date.now() / 3600e3) * 3600e3 + h * 3600e3).toISOString();
  const o = 'dummy-wait-202610221400-studio-wt1', n = 'dummy-wait-202610221410-studio-wt1';
  await livePage('dummy-wait');
  const studio = { batch: 'bt-wait', post: 'wt1', user: 'ed@example.com' };
  await saveDraft({ id: o, page: 'dummy-wait', page_name: 'Dummy', caption: 'Routine caption', studio }, tinyJpeg);
  await putRecord('batches', 'bt-wait', { id: 'bt-wait', page: 'dummy-wait', posts: [{ id: 'wt1', publish: { draft_id: o, state: 'failed' } }] });
  // O was retried on the review page and a run claimed it; the Studio, still showing it failed, sends the post again
  await setStatus(o, 'approved', { caption: 'Routine caption', retry: true }); await tick();
  assert.equal((await report(o, 'publishing', { scheduled_for: later(3) })).status, 200); await tick();
  await saveDraft({ id: n, page: 'dummy-wait', page_name: 'Dummy', caption: 'Edited caption', resent_from: o, studio }, tinyJpeg);
  await setStatus(n, 'approved', { caption: 'Edited caption' });
  await putRecord('batches', 'bt-wait', { id: 'bt-wait', page: 'dummy-wait', posts: [{ id: 'wt1', publish: { draft_id: n, state: 'approved', resent_from: o } }] });
  await tick();
  // N's lookups could not see O's copy yet (no post id; another caption): it waits for it
  const mid = await report(n, 'publishing', { scheduled_for: later(5) });
  assert.equal(mid.status, 409);
  assert.equal(mid.body.error, `the draft it was resent from (${o}) is being scheduled right now: skip it, a later run looks for its copy`);
  assert.equal((await loadDraft(n)).status, 'approved', 'it stays in the queue');
  assert.equal((await report(o, 'scheduled', { scheduled_for: later(3), post_id: 'cp-wt1' })).status, 200); await tick();
  // the next run looks for O's copy before it creates anything (PUBLISH.md 4.2: ViewPost)
  const item = (await robot.call('GET', '/api/queue', undefined, R)).body.queue.find(x => x.id === n);
  assert.deepEqual([item.ever_sent, item.prior_post_id], [true, 'cp-wt1']);
  assert.equal((await report(n, 'publishing', { scheduled_for: later(5) })).status, 200);
});

test('publisher: a page\'s unknown CreatePost failure is kept for the next hour\'s runs', async () => {
  const robot = client();
  const { saveDraft, setStatus } = await import('../lib/store.js');
  const { putRecord, getRecord } = await import('../lib/db.js');
  await putRecord('health', 'socialpilot', {});
  const report = (id, status, extra = {}) => robot.call('POST', '/api/result', { id, status, ...extra }, R);
  const unknown = 'Something went wrong, please try again later (E5021)';
  const ids = ['first', 'own', 'ok'].map(s => `dummy-sports-202610221300-breaker-${s}`);
  for (const id of ids) { await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: id }, tinyJpeg); await setStatus(id, 'approved', { caption: id }); }
  await tick();
  for (const id of ids) { await report(id, 'publishing'); await tick(); }
  // what `publish mark --status failed` sends for a page's first unknown failure in its run
  await report(ids[0], 'failed', { error: unknown, breaker: true }); await tick();
  await report(ids[1], 'failed', { error: 'publisher run interrupted' }); await tick();      // not SocialPilot failing
  const kept = (await getRecord('health', 'socialpilot')).failing_pages;
  assert.deepEqual(Object.keys(kept), ['dummy-sports']);
  assert.deepEqual([kept['dummy-sports'].id, kept['dummy-sports'].error], [ids[0], unknown]);
  assert.ok(Date.now() - kept['dummy-sports'].at < 60e3);
  const q = (await robot.call('GET', '/api/queue', undefined, R)).body;
  assert.equal(q.socialpilot.failing_pages['dummy-sports'].id, ids[0], 'the next run\'s plan reads it');
  const failed = (await robot.call('GET', '/api/drafts?status=failed', undefined, R)).body.drafts.find(x => x.id === ids[0]);
  assert.equal(failed.status_info.result.breaker, undefined, 'kept in one place');
  // a post scheduled on the page: its account works, so its next failure fails as before
  await report(ids[2], 'scheduled', { scheduled_for: '2026-10-22T09:00:00Z', post_id: 'cp-ok' });
  assert.deepEqual((await getRecord('health', 'socialpilot')).failing_pages, {});
  await putRecord('health', 'socialpilot', {});
});
