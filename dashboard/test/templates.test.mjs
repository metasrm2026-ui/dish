// A page's post templates as template specs (routes/templates.js): a templates document made active
// is converted by a Studio job, each template comes back as a spec with its preview and document page,
// the page's editors approve it or reject it with a note (converted again with it), and the routines
// draw only approved specs. Every template here is made up (the pages' own are confidential).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const { client, adminPage } = await import('./helpers.mjs');
const { updateRecord, getRecord } = await import('../lib/db.js');
const mem = globalThis.__boreBlobs;

const R = { routine: true };
const pdf = readFileSync(new URL('./fixtures/dummy_templates.pdf', import.meta.url));
const jpeg = Buffer.from('/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=', 'base64');
const png = Buffer.concat([Buffer.from('89504e470d0a1a0a', 'hex'), Buffer.alloc(40, 1)]);
const spec = (id, over = {}) => ({ id, name: `Made-up ${id}`, use_when: 'A made-up test template for one big number.', visual_lane: 'clean',
  photos: [{ box: [0, 0, 1, 0.6] }], text: [{ slot: 'headline', box: [0.05, 0.65, 0.9, 0.2] }],
  slots: { headline: { type: 'text', max_words: 6, examples: ['A MADE-UP LINE'] } }, ...over });

let admin, editor, stranger, robot, slug;
async function setPassword(c, link, pw) { return c.call('POST', '/api/auth/reset', { token: link.split('#reset=')[1], password: pw }); }
async function setup() {
  if (admin) return;
  admin = client(); editor = client(); stranger = client(); robot = client();
  const boot = await robot.call('POST', '/api/routine/bootstrap', { email: 'owner@example.com', name: 'O' }, R);
  await setPassword(admin, boot.body.setup_link, 'correct horse 42');
  assert.equal((await admin.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  slug = await adminPage(admin, 'Made Up Page', '1');
  const other = await adminPage(admin, 'Other Page', '2');
  for (const [c, email, pages] of [[editor, 'ed@example.com', [slug]], [stranger, 'st@example.com', [other]]]) {
    const inv = await admin.call('POST', '/api/admin/users', { email, role: 'editor', workspace: admin.ws, pages });
    assert.equal((await setPassword(c, inv.body.setup_link, 'editor pass 77')).status, 200);
  }
}
const jobsOf = async kind => (await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs.filter(j => j.kind === kind);
const claim = async id => assert.equal((await robot.call('POST', `/api/routine/jobs/${id}`, { status: 'running' }, R)).status, 200);
const send = (job, s, over = {}) => robot.call('POST', '/api/routine/templates', { page: slug, job, doc_version: 1, doc_pages: [2],
  spec: s, preview_b64: jpeg.toString('base64'), pages: [{ n: 2, image_b64: png.toString('base64') }], ...over }, R);
const screen = (c = editor) => c.call('GET', `/api/templates?page=${slug}`);
const decide = (c, id, v, action, note) => c.call('POST', `/api/templates/${slug}/${id}/${v}/${action}`, note === undefined ? {} : { note });
const approvedInConfig = async () => (await robot.call('GET', '/api/routine/config', undefined, R)).body.pages.find(p => p.slug === slug).templates;
const audits = async () => (await admin.call('GET', '/api/admin/audit')).body.entries;

test('a new templates document is converted by a Studio job; each template waits for an editor of the page', async () => {
  await setup();
  const up = await admin.call('POST', `/api/admin/pages/${slug}/docs/templates`, { filename: 'made-up.pdf', data_b64: pdf.toString('base64') });
  assert.equal(up.status, 200);
  const [job] = await jobsOf('templates');
  assert.equal(up.body.conversion.job, job.id);
  assert.deepEqual([job.page, job.batch_id, job.post_ids, job.doc_version, job.only, job.reason, job.templates],
                   [slug, null, [], 1, null, 'new_document', []]);
  assert.match(job.id, new RegExp(`^${slug}-\\d{13}-`), 'named like every job: the health check finds it');
  await claim(job.id);

  // the session sends a template: a new version, pending; nothing reaches the routines yet
  const r = await send(job.id, spec('big-figure'));
  assert.deepEqual([r.status, r.body.id, r.body.v, r.body.status], [200, 'big-figure', 1, 'pending']);
  const again = await send(job.id, spec('big-figure'));
  assert.deepEqual([again.body.v, again.body.again], [1, true], 'sent again (its answer lost): the same version');
  assert.deepEqual(await approvedInConfig(), []);
  const stored = [...mem.keys()].filter(k => k.startsWith(`templates/${slug}/big-figure/`));
  assert.equal(stored.length, 2, 'the preview and the document page, once');
  assert.ok(stored.every(k => k.endsWith('.enc')), 'pictures of the confidential document are stored encrypted');

  // the page's editor sees it with its preview; an editor of another page does not; the document's own
  // page only an admin (Oct 23 audit: every editor could see the whole confidential document, cached a day)
  const s = await screen();
  assert.equal(s.status, 200);
  assert.deepEqual([s.body.doc.version, s.body.job.status, s.body.job.sent, s.body.can_convert, s.body.can_see_document],
                   [1, 'running', 1, false, false]);
  const t = s.body.templates[0];
  assert.deepEqual([t.id, t.name, t.in_use, t.versions[0].status, t.versions[0].doc_version, t.versions[0].doc_pages, t.versions[0].pages],
                   ['big-figure', 'Made-up big-figure', null, 'pending', 1, [2], [2]]);
  assert.ok(!JSON.stringify(s.body).includes('.enc') && !JSON.stringify(s.body).includes('"photos"'), 'no storage paths, no spec');
  assert.equal((await editor.call('GET', `/api/templates/image?page=${slug}&id=big-figure&v=1&kind=page&n=2`)).status, 403);
  const pic = await admin.call('GET', `/api/templates/image?page=${slug}&id=big-figure&v=1&kind=page&n=2`);
  assert.deepEqual([pic.status, pic.headers['content-type'], pic.headers['cache-control']], [200, 'image/png', 'private, no-store']);
  assert.ok(Buffer.from(pic.body).equals(png));
  // one audit entry per view of the screen, not one per picture fetched (Oct 23: 60 a draw pushed the
  // logins out of the admin's audit view)
  const ofPage = async () => (await audits()).filter(e => e.page === slug && e.action.startsWith('template'));
  const before = (await ofPage()).length;
  for (let i = 0; i < 3; i++) await admin.call('GET', `/api/templates/image?page=${slug}&id=big-figure&v=1&kind=page&n=2`);
  assert.equal((await ofPage()).length, before, 'fetching the pictures writes no entry');
  assert.equal((await admin.call('GET', `/api/templates?page=${slug}`)).status, 200);
  assert.equal((await admin.call('GET', `/api/templates?page=${slug}&poll=1`)).status, 200);
  const after = await ofPage();
  assert.deepEqual([after.length - before, after[0].action, after[0].actor, after[0].document_pages], [1, 'templates_viewed', 'owner@example.com', 1],
                   'the view, with the document pages beside it');
  // a preview (the template on a stand-in photo) is kept a few minutes: the Studio's idea cards are drawn often
  const prev = await editor.call('GET', `/api/templates/image?page=${slug}&id=big-figure&v=1&kind=preview`);
  assert.deepEqual([prev.status, prev.headers['content-type'], prev.headers['cache-control']], [200, 'image/jpeg', 'private, max-age=300']);
  assert.equal((await stranger.call('GET', `/api/templates?page=${slug}`)).status, 403);
  assert.equal((await stranger.call('GET', `/api/templates/image?page=${slug}&id=big-figure&v=1&kind=preview`)).status, 403);
  assert.equal((await decide(stranger, 'big-figure', 1, 'approve')).status, 403);
  assert.equal((await admin.call('GET', `/api/templates/image?page=${slug}&id=big-figure&v=1&kind=page&n=3`)).status, 404);
  assert.ok((await audits()).some(e => e.action === 'templates_viewed' && e.actor === 'ed@example.com'));

  // an editor (not only an admin) approves it: the routines get it in their config
  const ok1 = await decide(editor, 'big-figure', 1, 'approve');
  assert.equal(ok1.status, 200);
  assert.deepEqual([ok1.body.templates[0].in_use, ok1.body.templates[0].versions[0].status, ok1.body.templates[0].versions[0].decided_by],
                   [1, 'approved', 'ed@example.com']);
  const cfg = await approvedInConfig();
  assert.deepEqual(cfg.map(x => [x.id, x.v, x.doc_version, x.spec.name]), [['big-figure', 1, 1, 'Made-up big-figure']]);
  assert.equal((await decide(editor, 'big-figure', 1, 'approve')).status, 409, 'approved already');
  assert.ok((await audits()).some(e => e.action === 'template_approved' && e.template === 't1' && e.version === 1));
  // the audit log names a template by its neutral ref: never its id or name (both from the document)
  assert.ok(!JSON.stringify(await audits()).includes('big-figure'));
  const thumb = await editor.call('GET', `/api/templates/image?page=${slug}&id=big-figure&v=in_use&kind=preview`);
  assert.equal(thumb.status, 200, 'the approved version\'s preview, for the Studio\'s idea cards');
  assert.equal((await robot.call('POST', `/api/routine/jobs/${job.id}`, { status: 'done', posts: {}, templates: { sent: ['big-figure'], failed: {} } }, R)).status, 200);
  assert.deepEqual((await screen()).body.job.result, { sent: ['big-figure'], failed: {} });
});

test('a rejected template is converted again with the editor\'s note; until a new version is approved, it is not drawn', async () => {
  await setup();
  const [job0] = (await jobsOf('templates'));
  assert.equal(job0, undefined, 'the first conversion is done');
  const first = (await getRecord('templates', slug)).job.id;
  await updateRecord('jobs', first, cur => ({ ...cur, status: 'running', started_at: Date.now() }));
  await send(first, spec('tall-stack'));
  await updateRecord('jobs', first, cur => ({ ...cur, status: 'done' }));

  assert.equal((await decide(editor, 'tall-stack', 1, 'reject', '')).status, 400, 'a rejection says what is wrong');
  const no = await decide(editor, 'tall-stack', 1, 'reject', 'The number sits too low: it belongs above the photo.');
  assert.equal(no.status, 200);
  const t = no.body.templates.find(x => x.id === 'tall-stack');
  assert.deepEqual([t.in_use, t.versions[0].status, t.versions[0].note], [null, 'rejected', 'The number sits too low: it belongs above the photo.']);
  let [job] = await jobsOf('templates');
  assert.equal(no.body.queued, job.id);
  assert.deepEqual([job.only, job.notes, job.reason], [['tall-stack'], { 'tall-stack': 'The number sits too low: it belongs above the photo.' }, 'rejected']);
  const given = job.templates.find(x => x.id === 'tall-stack');
  assert.deepEqual([given.status, given.note, given.spec.id], ['rejected', 'The number sits too low: it belongs above the photo.', 'tall-stack'],
                   'the session gets the spec and the note to convert it again');

  // taking an approved one out of use, while that job still waits: it joins the job
  const out = await decide(editor, 'big-figure', 1, 'reject', 'Use the wide logo, not the small one.');
  assert.equal(out.body.queued, job.id);
  assert.deepEqual(out.body.templates.find(x => x.id === 'big-figure').in_use, null);
  assert.deepEqual(await approvedInConfig(), [], 'out of use at once');
  [job] = await jobsOf('templates');
  assert.deepEqual([job.only, Object.keys(job.notes)], [['tall-stack', 'big-figure'], ['tall-stack', 'big-figure']]);

  // the new version: the rejected one stays as it was; approving the new one puts it in use
  await claim(job.id);
  const r = await send(job.id, spec('tall-stack', { use_when: 'A made-up test template, number above the photo.' }));
  assert.equal(r.body.v, 2);
  const view = (await screen()).body.templates;
  assert.equal(view[0].id, 'tall-stack', 'waiting for a decision comes first');
  assert.deepEqual(view[0].versions.map(x => [x.v, x.status]), [[2, 'pending'], [1, 'rejected']]);
  await decide(editor, 'tall-stack', 2, 'approve');
  assert.deepEqual((await approvedInConfig()).map(x => [x.id, x.v]), [['tall-stack', 2]]);
  // a newer version arriving while one is waiting replaces the waiting one; approving it replaces the one in use
  await send(job.id, spec('tall-stack', { name: 'Made-up tall stack 3' }));
  await send(job.id, spec('tall-stack', { name: 'Made-up tall stack 4' }));
  assert.equal((await decide(editor, 'tall-stack', 3, 'approve')).status, 409, 'replaced: decide on the newest');
  await decide(admin, 'tall-stack', 4, 'approve');
  const tv = (await screen()).body.templates.find(x => x.id === 'tall-stack');
  assert.deepEqual([tv.in_use, tv.versions.map(x => [x.v, x.status])], [4, [[4, 'approved'], [3, 'replaced'], [2, 'replaced'], [1, 'rejected']]]);
  assert.deepEqual((await approvedInConfig()).map(x => [x.id, x.v, x.spec.name]), [['tall-stack', 4, 'Made-up tall stack 4']]);
});

test('only an admin converts the whole document again; a job no session takes is restarted, then fails', async () => {
  await setup();
  for (const j of await jobsOf('templates')) await updateRecord('jobs', j.id, cur => ({ ...cur, status: 'done' }));
  assert.equal((await editor.call('POST', `/api/templates/${slug}/convert`, {})).status, 403);
  const c = await admin.call('POST', `/api/templates/${slug}/convert`, {});
  assert.equal(c.status, 200);
  const [job] = await jobsOf('templates');
  assert.deepEqual([c.body.queued, job.only, job.reason, job.doc_version, c.body.joined], [job.id, null, 'admin', 1, false]);
  assert.equal((await admin.call('POST', `/api/templates/${slug}/convert`, {})).body.joined, true, 'pressed twice: one job');
  assert.equal((await jobsOf('templates')).length, 1);
  // nobody takes it: the Templates screen (or the hourly watchdog) starts the routine again
  const aged = async min => {
    await updateRecord('jobs', job.id, x => ({ ...x, created_at: x.created_at - min * 60e3 }));
    for (const [k, v] of mem) if (k.startsWith(`job-nudges/${job.id}/`)) v.uploadedAt = new Date(v.uploadedAt - min * 60e3);
  };
  const nudges = () => [...mem.keys()].filter(k => k.startsWith(`job-nudges/${job.id}/`)).length;
  for (let n = 1; n <= 5; n++) {
    await aged(5);
    await screen();
    assert.equal(nudges(), n);
  }
  await aged(5);
  const s = await screen();
  assert.deepEqual([s.body.job.status, (await getRecord('jobs', job.id)).status], ['failed', 'failed']);
  assert.match(s.body.job.error, /after 5 restarts.*Convert templates again/);
});

test('what the routine sends is checked: a spec, its preview and the page it came from', async () => {
  await setup();
  const bad = async (over, status, msg) => {
    const r = await send('', spec('checked'), over);
    assert.equal(r.status, status, JSON.stringify(r.body));
    assert.match(r.body.error, msg);
  };
  await bad({ spec: spec('Not An Id') }, 400, /spec.id must be/);
  await bad({ spec: { ...spec('checked'), use_when: '' } }, 400, /use_when is required/);
  await bad({ preview_b64: Buffer.from('not a picture').toString('base64') }, 400, /preview must be a JPEG or PNG/);
  await bad({ preview_b64: Buffer.concat([jpeg, Buffer.alloc(1_600_000)]).toString('base64') }, 413, /under 1.5 MB/);
  await bad({ doc_pages: [] }, 400, /doc_pages/);
  await bad({ pages: [{ n: 5, image_b64: png.toString('base64') }] }, 400, /pages: the picture of each/);
  await bad({ page: 'no-such-page' }, 404, /no such page/);
  assert.equal((await robot.call('POST', '/api/routine/templates', {}, {})).status, 401, 'the routine secret only');
  assert.equal((await screen()).body.templates.some(t => t.id === 'checked'), false, 'nothing was saved');
});

test('a templates document made active again whose templates were converted queues nothing new', async () => {
  await setup();
  for (const j of await jobsOf('templates')) await updateRecord('jobs', j.id, cur => ({ ...cur, status: 'done' }));
  await admin.call('POST', `/api/admin/pages/${slug}/docs/templates`, { filename: 'made-up-2.pdf', data_b64: pdf.toString('base64') });
  let [job] = await jobsOf('templates');
  assert.equal(job.doc_version, 2);
  await updateRecord('jobs', job.id, cur => ({ ...cur, status: 'done' }));
  const back = await admin.call('PATCH', `/api/admin/pages/${slug}/docs/templates`, { active: 1 });
  assert.equal(back.body.conversion, undefined, 'v1 was converted already');
  assert.deepEqual(await jobsOf('templates'), []);
  const fwd = await admin.call('PATCH', `/api/admin/pages/${slug}/docs/templates`, { active: 2 });
  [job] = await jobsOf('templates');
  assert.deepEqual([fwd.body.conversion.job, job.doc_version], [job.id, 2], 'v2 never was');
});

test('an idea naming one of the page\'s templates reaches the visual job with it; the watchdog restarts a templates job', async () => {
  await setup();
  // (tall-stack: the one in use; big-figure was rejected above)
  const ideas = [{ title: 'The figure', template: 'Made-up tall-stack', template_spec: 'tall-stack', description: 'One number.' },
                 { title: 'Plain', template: 'headline', layout: 'headline', description: 'Just the news.' }];
  await robot.call('POST', '/api/routine/feed', { page: slug, stories: [{ id: 's1', headline: 'Made-up story', summary: 'S.', score: 5,
    published_at: Date.now(), url: 'https://e.com/1', sources: [{ name: 'E', url: 'https://e.com/1' }], ideas,
    images: [{ url: 'https://img.e.com/1.jpg', source: 'web' }] }] }, R);
  const fed = (await editor.call('GET', `/api/studio/feed?page=${slug}&hours=24`)).body.stories[0];
  assert.deepEqual(fed.ideas.map(i => i.template_spec), ['tall-stack', undefined]);
  const bt = (await editor.call('POST', '/api/studio/batches', { page: slug, stories: [{ id: 's1' }] })).body.batch;
  const P = bt.posts[0];
  await editor.call('PATCH', `/api/studio/batches/${bt.id}/posts/${P.id}`, { idea: { choice: 1 }, base_image_url: 'https://img.e.com/1.jpg' });
  const run = await editor.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [P.id] });
  const [job] = (await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs.filter(j => j.id === run.body.job);
  assert.deepEqual([job.posts[0].idea.template_spec, job.posts[0].idea.template], ['tall-stack', 'Made-up tall-stack']);
  // a session's ideas naming a template spec are kept as sent
  const ij = (await editor.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'ideas', post_ids: [P.id] })).body.job;
  await robot.call('POST', `/api/routine/jobs/${ij}`, { status: 'running' }, R);
  await robot.call('POST', `/api/routine/jobs/${ij}`, { status: 'done', posts: { [P.id]: { ideas: [{ ...ideas[0], template_spec: 'big-figure' }, ideas[1]] } } }, R);
  const after = (await editor.call('GET', `/api/studio/batches/${bt.id}`)).body.batch.posts[0];
  assert.equal(after.story.ideas[0].template_spec, 'big-figure');

  // nobody takes a templates job: the hourly watchdog starts the routine again, as for a post's job
  for (const j of await jobsOf('templates')) await updateRecord('jobs', j.id, cur => ({ ...cur, status: 'done' }));
  const c = (await admin.call('POST', `/api/templates/${slug}/convert`, {})).body.queued;
  await updateRecord('jobs', c, x => ({ ...x, created_at: x.created_at - 5 * 60e3 }));
  for (const [k, v] of mem) if (k.startsWith(`db/jobs/${c}/`)) v.uploadedAt = new Date(Date.now() - 5 * 60e3);   // (not written since)
  assert.equal((await robot.call('POST', '/api/routine/watchdog', {}, R)).status, 200);
  assert.equal([...mem.keys()].filter(k => k.startsWith(`job-nudges/${c}/`)).length, 1);
});

test('a template keeps its last six versions and the one in use; the pictures of the others are removed', async () => {
  await setup();
  const files = () => [...mem.keys()].filter(k => k.startsWith(`templates/${slug}/many/`));
  for (let i = 1; i <= 2; i++) await send('', spec('many', { name: `Made-up many ${i}` }));
  assert.equal((await decide(editor, 'many', 2, 'approve')).status, 200);
  for (let i = 3; i <= 9; i++) await send('', spec('many', { name: `Made-up many ${i}` }));
  const t = (await screen()).body.templates.find(x => x.id === 'many');
  assert.deepEqual(t.versions.map(x => x.v), [9, 8, 7, 6, 5, 4, 2], 'the newest six, and version 2 in use');
  assert.equal(t.in_use, 2);
  assert.equal(files().length, 7 * 2, 'a preview and a page for each version kept, none for the others');
  assert.equal((await approvedInConfig()).find(x => x.id === 'many').v, 2);
});

test('take out of use retires a template without converting anything; one the new document dropped is flagged', async () => {
  await setup();
  for (const j of await jobsOf('templates')) await updateRecord('jobs', j.id, cur => ({ ...cur, status: 'done' }));
  await send('', spec('old-one', { limits: 'A made-up callout line is drawn as a plain box.' }));
  const ok1 = await decide(editor, 'old-one', 1, 'approve');
  assert.equal(ok1.body.templates.find(x => x.id === 'old-one').versions[0].limits, 'A made-up callout line is drawn as a plain box.',
               'what the format could not draw, beside the preview');
  const jobsBefore = (await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs.length;
  const out = await decide(editor, 'old-one', 1, 'retire');
  assert.equal(out.status, 200);
  const t = out.body.templates.find(x => x.id === 'old-one');
  assert.deepEqual([t.in_use, t.versions[0].status, out.body.queued], [null, 'retired', undefined]);
  assert.equal((await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs.length, jobsBefore, 'no conversion queued (no Studio session spent)');
  assert.ok(!(await approvedInConfig()).some(x => x.id === 'old-one'), 'never drawn again');
  assert.equal((await decide(editor, 'old-one', 1, 'retire')).status, 409);
  assert.ok((await audits()).some(e => e.action === 'template_retired' && /^t\d+$/.test(e.template)));
  assert.equal((await decide(editor, 'old-one', 1, 'approve')).body.templates.find(x => x.id === 'old-one').in_use, 1, 'back in use');
  // a new document: while its conversion waits or runs, nothing is flagged (it may send it again)
  const up = await admin.call('POST', `/api/admin/pages/${slug}/docs/templates`, { filename: 'made-up-3.pdf', data_b64: pdf.toString('base64') });
  let s = (await screen()).body;
  assert.ok(s.doc.version > 1 && s.job.status === 'queued' && !s.templates.some(x => x.dropped), 'not while it is queued');
  await claim(up.body.conversion.job);
  assert.ok(!(await screen()).body.templates.some(x => x.dropped), 'not while it runs');
  // the conversion done without sending it again: in use, flagged as not in the active document
  assert.equal((await robot.call('POST', `/api/routine/jobs/${up.body.conversion.job}`, { status: 'done', posts: {}, templates: { sent: [], failed: {} } }, R)).status, 200);
  s = (await screen()).body;
  assert.ok(s.templates.find(x => x.id === 'old-one').dropped);
  assert.equal((await decide(editor, 'old-one', 1, 'retire')).body.templates.find(x => x.id === 'old-one').dropped, false);
});

test('an editor\'s job is offered before a templates conversion queued earlier', async () => {
  await setup();
  const conv = (await admin.call('POST', `/api/templates/${slug}/convert`, {})).body.queued;
  await robot.call('POST', '/api/routine/feed', { page: slug, stories: [{ id: 's9', headline: 'Made-up story nine', summary: 'S.', score: 5,
    published_at: Date.now(), url: 'https://e.com/9', sources: [{ name: 'E', url: 'https://e.com/9' }], ideas: [],
    images: [{ url: 'https://img.e.com/9.jpg', source: 'web' }] }] }, R);
  const bt = (await editor.call('POST', '/api/studio/batches', { page: slug, stories: [{ id: 's9' }] })).body.batch;
  const ed = bt.posts[0].job.id;                                       // a story without ideas: they are asked for at once
  await updateRecord('jobs', ed, cur => ({ ...cur, created_at: cur.created_at + 1 }));           // queued after the conversion
  const ids = (await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs.map(j => j.id);
  assert.ok(ids.indexOf(ed) >= 0 && ids.indexOf(ed) < ids.indexOf(conv), ids.join(' '));
});

test('a template sent for a job that is not this page\'s templates job is refused, and its pictures are not kept', async () => {
  await setup();
  const before = [...mem.keys()].filter(k => k.startsWith(`templates/${slug}/strayed/`)).length;
  const r = await send('no-such-job', spec('strayed'));
  assert.deepEqual([r.status, before], [409, 0]);
  assert.equal([...mem.keys()].filter(k => k.startsWith(`templates/${slug}/strayed/`)).length, 0);
  // the same spec sent again by its job (its answer lost): no second copy of its pictures
  const job = (await admin.call('POST', `/api/templates/${slug}/convert`, {})).body.queued
    || (await jobsOf('templates'))[0].id;
  await updateRecord('jobs', job, cur => ({ ...cur, status: 'running', started_at: Date.now() }));
  await send(job, spec('strayed'));
  const once = [...mem.keys()].filter(k => k.startsWith(`templates/${slug}/strayed/`)).length;
  const { saveFromRoutine } = await import('../routes/templates.js');
  assert.equal((await send(job, spec('strayed'))).body.again, true);
  assert.equal([...mem.keys()].filter(k => k.startsWith(`templates/${slug}/strayed/`)).length, once);
  // a duplicate the first check misses (both requests read before either wrote) is found inside the
  // write: its copies of the pictures go
  const body = { page: slug, job, doc_version: 1, doc_pages: [2], spec: spec('strayed', { use_when: 'A made-up test template, sent twice at once.' }),
                 preview_b64: jpeg.toString('base64'), pages: [{ n: 2, image_b64: png.toString('base64') }] };
  const twice = await Promise.all([saveFromRoutine(body), saveFromRoutine(body)]);
  assert.deepEqual(twice.map(x => [x.v, !!x.again]).sort(), [[2, false], [2, true]]);
  assert.equal([...mem.keys()].filter(k => k.startsWith(`templates/${slug}/strayed/`)).length, once + 2, 'no orphaned copies');
});

test('a template stored before refs were kept gets one that never moves or collides', async () => {
  await setup();
  const legacy = await adminPage(admin, 'Legacy Made Up Page', '3');
  const old = id => ({ id, in_use: null, versions: [{ v: 1, name: `Made-up ${id}`, status: 'pending', note: '', doc_version: 1, doc_pages: [1], pages: [], at: 1 }] });
  await updateRecord('templates', legacy, () => ({ page: legacy, specs: { b: old('b'), c: old('c') } }));   // b was t1, c t2
  const r = await robot.call('POST', '/api/routine/templates', { page: legacy, job: '', doc_version: 1, doc_pages: [2], spec: spec('a'),
    preview_b64: jpeg.toString('base64'), pages: [{ n: 2, image_b64: png.toString('base64') }] }, R);
  assert.equal(r.status, 200);
  const refs = Object.fromEntries(Object.values((await getRecord('templates', legacy)).specs).map(t => [t.id, t.ref]));
  assert.deepEqual(refs, { a: 't3', b: 't1', c: 't2' }, 'the older ones keep the ref they were audited by');
  assert.equal((await admin.call('POST', `/api/templates/${legacy}/c/1/approve`, {})).status, 200);
  const entries = await audits();
  assert.equal(entries.find(e => e.action === 'template_received' && e.page === legacy).template, 't3');
  assert.equal(entries.find(e => e.action === 'template_approved' && e.page === legacy).template, 't2');
});

test('an idea whose template was taken out of use is refused when picked and when its visual is asked for', async () => {
  await setup();
  await send('', spec('quote-made'));
  assert.equal((await decide(editor, 'quote-made', 1, 'approve')).status, 200);
  const ideas = [{ title: 'The quote', template: 'Made-up quote-made', template_spec: 'quote-made', description: 'One quote.' },
                 { title: 'Plain', template: 'headline', layout: 'headline', description: 'Just the news.' }];
  await robot.call('POST', '/api/routine/feed', { page: slug, stories: [{ id: 'sq', headline: 'Made-up quoted story', summary: 'S.', score: 5,
    published_at: Date.now(), url: 'https://e.com/q', sources: [{ name: 'E', url: 'https://e.com/q' }], ideas,
    images: [{ url: 'https://img.e.com/q.jpg', source: 'web' }] }] }, R);
  const bt = (await editor.call('POST', '/api/studio/batches', { page: slug, stories: [{ id: 'sq' }] })).body.batch;
  const P = bt.posts[0], path = `/api/studio/batches/${bt.id}/posts/${P.id}`;
  // picked while in use, then taken out of use: Make visual is refused (the visual could only fail)
  assert.equal((await editor.call('PATCH', path, { idea: { choice: 1 }, base_image_url: 'https://img.e.com/q.jpg' })).status, 200);
  assert.equal((await decide(admin, 'quote-made', 1, 'retire')).status, 200);
  const run = await editor.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [P.id] });
  assert.equal(run.status, 409);
  assert.match(run.body.error, /taken out of use: pick the other idea/);
  assert.ok(!(await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs.some(j => j.kind === 'compose' && j.post_ids.includes(P.id)));
  // picking it again is refused too; the other idea still works
  const pick = await editor.call('PATCH', path, { idea: { choice: 1 } });
  assert.deepEqual([pick.status, /taken out of use/.test(pick.body.error)], [409, true]);
  assert.equal((await editor.call('PATCH', path, { idea: { choice: 2 } })).status, 200);
  assert.equal((await editor.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [P.id] })).status, 200);
});

test('Make all visuals makes the posts it can and names the one whose template was taken out of use', async () => {
  await setup();
  await send('', spec('quote-all'));
  assert.equal((await decide(editor, 'quote-all', 1, 'approve')).status, 200);
  const ideas = [{ title: 'The quote', template: 'Made-up quote-all', template_spec: 'quote-all', description: 'One quote.' },
                 { title: 'Plain', template: 'headline', layout: 'headline', description: 'Just the news.' }];
  const story = n => ({ id: `sa${n}`, headline: `Made-up story all ${n}`, summary: 'S.', score: 5, published_at: Date.now(), url: `https://e.com/a${n}`,
    sources: [{ name: 'E', url: `https://e.com/a${n}` }], ideas, images: [{ url: `https://img.e.com/a${n}.jpg`, source: 'web' }] });
  await robot.call('POST', '/api/routine/feed', { page: slug, stories: [story(1), story(2)] }, R);
  const bt = (await editor.call('POST', '/api/studio/batches', { page: slug, stories: [{ id: 'sa1' }, { id: 'sa2' }] })).body.batch;
  const [A, B] = bt.posts;
  for (const [P, choice] of [[A, 1], [B, 2]])
    assert.equal((await editor.call('PATCH', `/api/studio/batches/${bt.id}/posts/${P.id}`, { idea: { choice }, base_image_url: P.story.images[0].url })).status, 200);
  assert.equal((await decide(admin, 'quote-all', 1, 'retire')).status, 200);
  const composing = async () => (await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs.filter(j => j.kind === 'compose' && j.batch_id === bt.id);
  // only the post whose template is gone: refused, nothing queued
  const one = await editor.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [A.id] });
  assert.deepEqual([one.status, (await composing()).length], [409, 0]);
  // both (Make all visuals): the other one is made, and the answer names the one that is not
  const all = await editor.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [A.id, B.id] });
  assert.equal(all.status, 200, JSON.stringify(all.body));
  assert.deepEqual((await composing()).map(j => j.post_ids), [[B.id]]);
  assert.match(all.body.skipped, new RegExp(`^Not made: "${A.story.headline}": the template of this idea was taken out of use: pick the other idea`));
  assert.ok(!all.body.skipped.includes(B.story.headline));
});
