// Studio jobs against what editors do meanwhile: results that land late, a button pressed twice, a
// request the dashboard does not know, captions the editor wrote, and the smaller Studio rules.
import test from 'node:test';
import assert from 'node:assert/strict';
const { client, adminPage, setPassword } = await import('./helpers.mjs');
const { updateRecord, getRecord } = await import('../lib/db.js');
const { saveDraft, setStatus, loadDraft } = await import('../lib/store.js');
const { needsAction, LOOKUPS_FAILED } = await import('../routes/result.js');
const mem = globalThis.__boreBlobs;
const tick = () => new Promise(r => setTimeout(r, 5));      // status records are named by the millisecond

const R = { routine: true };
const tinyJpeg = Buffer.from('/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=', 'base64');
const img = tinyJpeg.toString('base64');

let owner, robot, slug;
async function setup() {
  if (owner) return;
  owner = client(); robot = client();
  const boot = await robot.call('POST', '/api/routine/bootstrap', { email: 'owner@example.com', name: 'O' }, R);
  await owner.call('POST', '/api/auth/reset', { token: boot.body.setup_link.split('#reset=')[1], password: 'correct horse 42' });
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  slug = await adminPage(owner, 'Dummy', '1');
  const story = i => ({ id: `s${i}`, headline: `Story ${i}`, summary: 'S.', score: 5, published_at: Date.now(), url: `https://e.com/${i}`,
    sources: [{ name: 'E', url: `https://e.com/${i}` }], ideas: [{ title: 'A', template: 'headline', description: 'a' }, { title: 'B', template: 'quote', description: 'b' }],
    images: [{ url: `https://img.e.com/${i}a.jpg`, source: 'web' }, { url: `https://img.e.com/${i}b.jpg`, source: 'web' }] });
  await robot.call('POST', '/api/routine/feed', { page: slug, stories: [1, 2, 3].map(story) }, R);
}

// a batch whose posts have an idea and photo a; with `made`, each also has a visual and a caption
async function batch(n = 1, made = false) {
  const bt = (await owner.call('POST', '/api/studio/batches', { page: slug, stories: Array.from({ length: n }, (_, i) => ({ id: `s${i + 1}` })) })).body.batch;
  for (const [i, P] of bt.posts.entries()) {
    await patch(bt, P, { idea: { choice: 1 }, base_image_url: `https://img.e.com/${i + 1}a.jpg` });
    if (made) await done(await claim(await run(bt, 'compose', [P.id])), { [P.id]: { image_b64: img, caption: `Routine caption ${i + 1}`, hashtags: ['#r'] } });
  }
  return { bt, P: bt.posts[0], posts: bt.posts };
}
const patch = (bt, P, body) => owner.call('PATCH', `/api/studio/batches/${bt.id}/posts/${P.id}`, body);
const run = async (bt, kind, ids, feedback = '') => (await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind, post_ids: ids, feedback })).body.job;
async function claim(job) { assert.equal((await robot.call('POST', `/api/routine/jobs/${job}`, { status: 'running' }, R)).status, 200); return job; }
const done = (job, posts, status = 'done') => robot.call('POST', `/api/routine/jobs/${job}`, { status, posts }, R);
const postOf = async (bt, P) => (await getRecord('batches', bt.id)).posts.find(p => p.id === P.id);
const viewed = async (bt, P) => (await owner.call('GET', `/api/studio/batches/${bt.id}`)).body.batch.posts.find(p => p.id === P.id);
// time passing for a job: it, its claims and its restarts that many minutes older
async function aged(job, min) {
  await updateRecord('jobs', job, cur => ({ ...cur, ...(cur.started_at ? { started_at: cur.started_at - min * 60e3 } : {}),
                                            created_at: cur.created_at - min * 60e3 }));
  for (const [k, v] of mem) if (k.includes(`/${job}/`) && /^job-(claims|nudges|leaves)\//.test(k)) v.uploadedAt = new Date(v.uploadedAt - min * 60e3);
}
const nudgesOf = job => [...mem.keys()].filter(k => k.startsWith(`job-nudges/${job}/`)).length;

test('a job request the dashboard does not know is refused (400), never taken for "done"', async () => {
  await setup();
  const { bt, P } = await batch();
  const j = await run(bt, 'compose', [P.id]);
  for (const body of [{}, { status: 'queud' }, { status: 'queued' }, { status: 'finished', posts: { [P.id]: { caption: 'x' } } }, { hand_back: true }]) {
    const r = await robot.call('POST', `/api/routine/jobs/${j}`, body, R);
    assert.equal(r.status, 400, JSON.stringify(body));
    assert.equal(r.body.error, 'unknown request: deploy the dashboard');
  }
  assert.equal((await getRecord('jobs', j)).status, 'queued');
  const p = await postOf(bt, P);
  assert.deepEqual([p.job.status, p.caption], ['queued', '']);
  // what the Python sends: all known
  assert.equal((await robot.call('POST', `/api/routine/jobs/${j}`, { left: true, error: 'packages', session: 'abc123' }, R)).body.status, 'queued');
  await claim(j);
  assert.equal((await robot.call('POST', `/api/routine/jobs/${j}`, { status: 'queued', release: true, error: 'x' }, R)).body.status, 'queued');
  await claim(j);
  assert.equal((await robot.call('POST', `/api/routine/jobs/${j}`, { precheck: true, posts: {} }, R)).status, 200);
  assert.equal((await done(j, { [P.id]: { image_b64: img, caption: 'Made' } })).status, 200);
});

test('a visual made from the photo the editor has since replaced is not used; Make visual works again at once', async () => {
  await setup();
  const { bt, P } = await batch();
  const j1 = await claim(await run(bt, 'compose', [P.id]));
  // pressed again while it is being made: nothing new starts
  const again = (await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [P.id] })).body;
  assert.deepEqual([again.job, again.already, again.open], [null, true, [P.id]]);
  assert.equal((await postOf(bt, P)).job.photo, 'https://img.e.com/1a.jpg', 'the line says which photo it is made from');
  // the editor picks photo b while it runs: a visual from b can be asked for straight away
  assert.equal((await patch(bt, P, { base_image_url: 'https://img.e.com/1b.jpg' })).status, 200);
  const j2 = await run(bt, 'compose', [P.id]);
  assert.ok(j2 && j2 !== j1);
  // the old one lands first: dropped (a newer one is on its way), the new one's line untouched
  const late = await done(j1, { [P.id]: { image_b64: img, caption: 'Card on photo a' } });
  assert.deepEqual(late.body.dropped, [P.id]);
  let p = await postOf(bt, P);
  assert.deepEqual([p.final_image_url, p.caption, p.job.id, p.job.status], ['', '', j2, 'queued']);
  await claim(j2);
  await done(j2, { [P.id]: { image_b64: img, caption: 'Card on photo b' } });
  p = await postOf(bt, P);
  assert.ok(p.final_image_url);
  assert.deepEqual([p.caption, p.job.status, p.card_job], ['Card on photo b', 'done', j2]);
  // with no newer job: the result is dropped with a note on its line
  await patch(bt, P, { base_image_url: 'https://img.e.com/1a.jpg' });
  const j3 = await claim(await run(bt, 'compose', [P.id]));
  await patch(bt, P, { base_image_url: 'https://img.e.com/1b.jpg' });
  await done(j3, { [P.id]: { image_b64: img, caption: 'Card on photo a again' } });
  p = await postOf(bt, P);
  assert.deepEqual([p.final_image_url, p.caption, p.job.status, p.job.error], ['', 'Card on photo b', 'done', '']);
  assert.match(p.job.note, /made from the earlier photo, so it was not used/);
  assert.deepEqual((await getRecord('jobs', j3)).dropped, { [P.id]: p.job.note });
});

test('a late result of an older job, or one that lands after Send, changes nothing', async () => {
  await setup();
  // the job that failed after its 5 restarts, whose session turns up after the editor asked again
  const { bt, P } = await batch();
  const j1 = await claim(await run(bt, 'compose', [P.id]));
  await updateRecord('jobs', j1, cur => ({ ...cur, status: 'failed', error: 'after 5 restarts' }));
  await updateRecord('batches', bt.id, cur => { cur.posts[0].job = { ...cur.posts[0].job, status: 'failed' }; return cur; });
  const j2 = await claim(await run(bt, 'compose', [P.id]));
  await done(j2, { [P.id]: { image_b64: img, caption: 'The newer one' } });
  const card = (await postOf(bt, P)).final_image_url;
  await done(j1, { [P.id]: { image_b64: img, caption: 'The older one' } });
  let p = await postOf(bt, P);
  assert.deepEqual([p.final_image_url, p.caption, p.card_job, p.job.id], [card, 'The newer one', j2, j2]);
  // regenerate pressed, then Send before it finished: the post stays what was sent
  const sent = await batch(1, true);
  const r = await claim(await run(sent.bt, 'compose', [sent.P.id], 'bigger headline'));
  const before = await postOf(sent.bt, sent.P);
  assert.equal((await owner.call('POST', `/api/studio/batches/${sent.bt.id}/publish`, { post_id: sent.P.id, mode: 'now' })).status, 200);
  await done(r, { [sent.P.id]: { image_b64: img, caption: 'Changed after Send' } });
  p = await postOf(sent.bt, sent.P);
  assert.deepEqual([p.final_image_url, p.caption, p.publish.state], [before.final_image_url, 'Routine caption 1', 'approved']);
  assert.match(p.job.note, /after the post was sent/);
  // nor does the automatic check of the visual that was sent (its pre-check, still running at Send)
  assert.deepEqual([p.card_job !== r, p.check.status, p.check.auto], [true, 'running', true]);
  assert.equal((await robot.call('POST', `/api/routine/jobs/${p.card_job}`, { precheck: true, posts: { [sent.P.id]: { check: { factual: ['x'] } } } }, R)).status, 200);
  assert.equal((await postOf(sent.bt, sent.P)).check.status, 'running');
});

test('a session that took a job over after a photo change: the first session\'s late card is never used', async () => {
  await setup();
  const { bt, P } = await batch();
  const j = await claim(await run(bt, 'compose', [P.id]));               // session 1, given photo a
  await patch(bt, P, { base_image_url: 'https://img.e.com/1b.jpg' });    // the editor picks b, and waits
  await aged(j, 21);                                                     // session 1 looks dead: offered again
  assert.ok((await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs.some(x => x.id === j));
  await claim(j);                                                        // session 2, given photo b
  assert.deepEqual((await getRecord('jobs', j)).made_from[P.id].photos, ['https://img.e.com/1a.jpg', 'https://img.e.com/1b.jpg']);
  // session 1 finishes late with its card on a: refused, the job stays with session 2
  const late = await done(j, { [P.id]: { image_b64: img, caption: 'Card on photo a', photo: 'https://img.e.com/1a.jpg' } });
  assert.equal(late.status, 409, JSON.stringify(late.body));
  let p = await postOf(bt, P);
  assert.deepEqual([p.final_image_url, p.caption, p.job.status], ['', '', 'running']);
  // session 2's card on b is the one used
  assert.equal((await done(j, { [P.id]: { image_b64: img, caption: 'Card on photo b', photo: 'https://img.e.com/1b.jpg' } })).status, 200);
  p = await postOf(bt, P);
  assert.ok(p.final_image_url);
  assert.deepEqual([p.caption, p.job.status, p.card_job], ['Card on photo b', 'done', j]);
  // a session that does not say which photo it used (Python from before): once sessions of the job
  // were given different photos, its card cannot be told apart, so it is not used
  const two = await batch();
  const k = await claim(await run(two.bt, 'compose', [two.P.id]));
  await patch(two.bt, two.P, { base_image_url: 'https://img.e.com/1b.jpg' });
  await aged(k, 21);
  await claim(k);
  assert.equal((await done(k, { [two.P.id]: { image_b64: img, caption: 'Which photo?' } })).status, 200);
  p = await postOf(two.bt, two.P);
  assert.deepEqual([p.final_image_url, p.caption], ['', '']);
  assert.match(p.job.note, /may have been made from an earlier photo/);
});

test('a visual whose line an image search took is still followed: restarted, then failed, never a dead button', async () => {
  await setup();
  const { bt, P } = await batch();
  const jc = await claim(await run(bt, 'compose', [P.id]));
  // the editor searches for photos while the visual is being made; the search finishes
  const ji = await claim(await run(bt, 'images', [P.id], 'Search for photos of: Deebo Samuel'));
  await done(ji, { [P.id]: { images: [{ url: 'https://img.e.com/deebo.jpg', source: 'web' }] } });
  let p = await postOf(bt, P);
  assert.deepEqual([p.job.id, p.job.status, p.under_way], [ji, 'done', [{ id: jc, kind: 'compose', photo: 'https://img.e.com/1a.jpg' }]]);
  // pressed again meanwhile: under way, nothing new
  assert.equal((await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [P.id] })).body.already, true);
  // its session dies: the page's polls restart it every 4 minutes, then tell the editor
  await aged(jc, 21);
  for (let n = 1; n <= 5; n++) {
    await viewed(bt, P);
    assert.equal(nudgesOf(jc), n, `restart ${n}`);
    await aged(jc, 5);
  }
  p = await viewed(bt, P);
  assert.equal((await getRecord('jobs', jc)).status, 'failed');
  assert.deepEqual([p.job.id, p.job.kind, p.job.status, p.under_way], [jc, 'compose', 'failed', undefined]);
  assert.match(p.job.error, /after 5 restarts/);
  assert.ok(await run(bt, 'compose', [P.id]), 'Make visual works again');
  // a job nobody follows any more (left behind before posts kept p.under_way): pressing the button
  // cancels it and starts a new one, so the button never stays a no-op
  const two = await batch();
  const old = await claim(await run(two.bt, 'compose', [two.P.id]));
  await updateRecord('batches', two.bt.id, cur => { cur.posts[0].job = { id: 'x', kind: 'images', status: 'done' }; return cur; });
  await aged(old, 21);
  const fresh = await run(two.bt, 'compose', [two.P.id]);
  assert.ok(fresh && fresh !== old);
  assert.equal((await getRecord('jobs', old)).status, 'cancelled');
});

test('a visual made from a replaced photo and left running is cancelled once no session has it', async () => {
  await setup();
  const { bt, P } = await batch();
  const j1 = await claim(await run(bt, 'compose', [P.id]));
  await patch(bt, P, { base_image_url: 'https://img.e.com/1b.jpg' });
  const j2 = await run(bt, 'compose', [P.id]);
  assert.deepEqual((await postOf(bt, P)).under_way.map(j => j.id), [j1]);
  await viewed(bt, P);
  assert.equal((await getRecord('jobs', j1)).status, 'running', 'its session may still be on it');
  await aged(j1, 21);
  await viewed(bt, P);
  assert.equal((await getRecord('jobs', j1)).status, 'cancelled');
  assert.equal(nudgesOf(j1), 0, 'never restarted: its card would not be used');
  const p = await postOf(bt, P);
  assert.deepEqual([p.job.id, p.under_way], [j2, undefined]);
});

test('a cleared batch\'s jobs are cancelled, never restarted', async () => {
  await setup();
  const { bt, P } = await batch();
  const jc = await claim(await run(bt, 'compose', [P.id]));
  const ji = await run(bt, 'images', [P.id], 'Search for photos of: anyone');   // waiting; the visual runs behind it
  assert.equal((await owner.call('POST', '/api/studio/batches/clear', { ids: [bt.id] })).body.cleared, 1);
  assert.equal((await getRecord('jobs', ji)).status, 'cancelled');
  await aged(jc, 21);                                                    // its session died
  await viewed(bt, P);                                                   // (the watchdog does the same)
  assert.deepEqual([(await getRecord('jobs', jc)).status, nudgesOf(jc)], ['cancelled', 0]);
  assert.equal((await postOf(bt, P)).under_way, undefined);
});

test('a Send cut short does not count as sent: a visual made meanwhile is used', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  // the Send stopped after marking the post (storage refused a read) three minutes ago
  const cut = state => updateRecord('batches', bt.id, cur => {
    cur.posts[0].publish = { draft_id: `${slug}-202610211200-studio-${P.id}`, state, sending_at: Date.now() - 3 * 60e3, mode: 'now' }; return cur; });
  await cut('sending');
  await done(await claim(await run(bt, 'compose', [P.id], 'brighter')), { [P.id]: { image_b64: img, caption: 'Made after the cut Send' } });
  let p = await postOf(bt, P);
  assert.deepEqual([p.caption, p.job.note], ['Made after the cut Send', undefined]);
  // a Send under way right now: a visual queued before it is not used, and the note says so; a new
  // one is refused (its result would be thrown away)
  const before = await claim(await run(bt, 'compose', [P.id]));
  await updateRecord('batches', bt.id, cur => { cur.posts[0].publish.sending_at = Date.now(); return cur; });
  const refused = await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [P.id] });
  assert.deepEqual([refused.status, refused.body.error], [409, 'already sent to SocialPilot: it can no longer be changed here']);
  await done(before, { [P.id]: { image_b64: img, caption: 'During the Send' } });
  p = await postOf(bt, P);
  assert.equal(p.caption, 'Made after the cut Send');
  assert.match(p.job.note, /while the post was being sent/);
  // cut short after its draft reached the publisher (the batch does not say so yet): sent
  await saveDraft({ id: p.publish.draft_id, page: slug, page_name: 'Dummy', caption: 'x', headline: 'h' }, tinyJpeg);
  await setStatus(p.publish.draft_id, 'approved', {});
  await cut('sending');
  await done(await claim(await run(bt, 'compose', [P.id])), { [P.id]: { image_b64: img, caption: 'After it was handed over' } });
  p = await postOf(bt, P);
  assert.equal(p.caption, 'Made after the cut Send');
  assert.match(p.job.note, /after the post was sent/);
});

test('an image search for other words runs beside the one under way; their photos add up', async () => {
  await setup();
  const { bt, P } = await batch();
  const a = await run(bt, 'images', [P.id], 'Search for photos of: Deebo Samuel');
  const same = (await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'images', post_ids: [P.id], feedback: 'Search for photos of: Deebo Samuel' })).body;
  assert.deepEqual([same.job, same.already], [null, true], 'the same search: one');
  const b = await run(bt, 'images', [P.id], 'Search for photos of: 49ers stadium');
  assert.ok(b && b !== a, 'other words: searched too');
  assert.equal((await getRecord('jobs', b)).feedback, 'Search for photos of: 49ers stadium');
  await done(await claim(b), { [P.id]: { images: [{ url: 'https://img.e.com/stadium.jpg', source: 'web' }] } });
  await done(await claim(a), { [P.id]: { images: [{ url: 'https://img.e.com/deebo.jpg', source: 'web' }] } });   // the first lands last
  const p = await postOf(bt, P);
  assert.deepEqual(p.extra_images.map(i => i.url), ['https://img.e.com/deebo.jpg', 'https://img.e.com/stadium.jpg']);
  assert.equal(p.under_way, undefined);
});

test('Make all visuals writes every job, then starts the routine for all of them at once', async () => {
  await setup();
  const { bt, posts } = await batch(3);
  const inner = globalThis.fetch;
  let calls = 0, inflight = 0, peak = 0;
  process.env.STUDIO_FIRE_URL = 'https://fire.test/studio'; process.env.STUDIO_FIRE_TOKEN = 't';
  globalThis.fetch = async (url, opts) => {
    if (String(url) !== process.env.STUDIO_FIRE_URL) return inner(url, opts);
    calls++; inflight++; peak = Math.max(peak, inflight);
    await new Promise(r => setTimeout(r, 100));
    inflight--;
    return new Response('{}', { status: 200 });
  };
  try {
    const r = (await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: posts.map(p => p.id) })).body;
    assert.equal(r.jobs.length, 3);
    assert.deepEqual([calls, peak, r.fired], [3, 3, true]);
    for (const j of r.jobs) assert.equal((await getRecord('jobs', j)).fired, true);
  } finally {
    globalThis.fetch = inner;
    delete process.env.STUDIO_FIRE_URL; delete process.env.STUDIO_FIRE_TOKEN;
  }
});

test('a check of a visual since replaced is not shown; a caption edited during a check makes it out of date', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const checks = { factual: [], timing: [], grammar: [], other: [], guidelines: [] };
  // the editor picks another photo while the check runs: its findings were about the old visual
  let jc = await claim(await run(bt, 'check', [P.id]));
  await patch(bt, P, { base_image_url: 'https://img.e.com/1b.jpg' });
  await done(jc, { [P.id]: { check: checks } });
  let p = await postOf(bt, P);
  assert.equal(p.check.status, 'none');
  assert.match(p.job.note, /checked an earlier visual/);
  // a new visual; the check is claimed, then the editor edits the caption: out of date, whether or
  // not the session says which text it reviewed
  await done(await claim(await run(bt, 'compose', [P.id])), { [P.id]: { image_b64: img, caption: 'Routine caption 2', hashtags: ['#r'] } });
  jc = await claim(await run(bt, 'check', [P.id]));
  await patch(bt, P, { caption: 'Edited while it was checked' });
  await done(jc, { [P.id]: { check: checks } });
  assert.deepEqual([(await postOf(bt, P)).check.status, (await postOf(bt, P)).check.stale], ['done', true]);
  jc = await claim(await run(bt, 'check', [P.id]));
  await patch(bt, P, { caption: 'Edited again' });
  await done(jc, { [P.id]: { check: checks, checked_caption: 'Edited while it was checked\n#r' } });
  assert.equal((await postOf(bt, P)).check.stale, true);
  // edited before the session read it: the session checked the new text
  jc = await run(bt, 'check', [P.id]);
  await patch(bt, P, { caption: 'Final words' });
  await claim(jc);
  await done(jc, { [P.id]: { check: checks } });
  p = await postOf(bt, P);
  assert.deepEqual([p.check.status, p.check.stale], ['done', false]);
  // sending the same caption again (Send saves the fields first) does not make a check out of date
  await patch(bt, P, { caption: 'Final words', hashtags: '#r' });
  assert.equal((await postOf(bt, P)).check.stale, false);
  // and an edit taken back is the text it checked again
  await patch(bt, P, { caption: 'Final words, changed' });
  assert.equal((await postOf(bt, P)).check.stale, true);
  await patch(bt, P, { caption: 'Final words' });
  assert.equal((await postOf(bt, P)).check.stale, false);
});

test('a job line left spinning by a lost write is put right by the page\'s poll', async () => {
  await setup();
  const { bt, posts } = await batch(3);
  const [A, B, C] = posts;
  const jA = await claim(await run(bt, 'compose', [A.id]));
  const jB = await claim(await run(bt, 'compose', [B.id]));
  const jC = await claim(await run(bt, 'compose', [C.id]));
  // A's record says done but its result never reached the batch; B failed; C is still running
  await updateRecord('jobs', jA, cur => ({ ...cur, status: 'done', done_at: Date.now() }));
  await updateRecord('jobs', jB, cur => ({ ...cur, status: 'failed', error: 'The Studio routine could not do this step' }));
  const a = await viewed(bt, A), b = await viewed(bt, B), c = await viewed(bt, C);
  assert.deepEqual([a.job.status, a.job.error], ['done', 'the result did not arrive: press the button again']);
  assert.deepEqual([b.job.status, b.job.error], ['failed', 'The Studio routine could not do this step']);
  assert.deepEqual([c.job.id, c.job.status], [jC, 'running']);
  // and pressing the button again starts a new job (the old one is not open any more)
  assert.ok(await run(bt, 'compose', [A.id]));
});

test('Make all visuals: one job per post, none for a post whose visual is being made', async () => {
  await setup();
  const { bt, posts } = await batch(3);
  const busy = await run(bt, 'compose', [posts[0].id]);
  const r = (await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: posts.map(p => p.id) })).body;
  assert.equal(r.jobs.length, 2);
  assert.deepEqual(r.open, [posts[0].id]);
  for (const [i, j] of r.jobs.entries()) assert.deepEqual((await getRecord('jobs', j)).post_ids, [posts[i + 1].id]);
  assert.equal((await postOf(bt, posts[0])).job.id, busy);
  // two clicks at once: one job
  const two = await batch(1);
  const [x, y] = await Promise.all([run(two.bt, 'ideas', [two.P.id]), run(two.bt, 'ideas', [two.P.id])]);
  const made = [x, y].filter(Boolean);
  assert.equal(made.length, 1, JSON.stringify([x, y]));
  assert.equal((await postOf(two.bt, two.P)).jobs.ideas, made[0]);
});

test('a visual made again keeps the caption the editor wrote; the routine\'s is offered beside it', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  assert.equal((await patch(bt, P, { caption: 'EDITOR FIXED CAPTION', hashtags: '#Mine' })).status, 200);
  const j = await claim(await run(bt, 'compose', [P.id], 'make the headline bigger'));
  await done(j, { [P.id]: { image_b64: img, caption: 'Fresh routine caption', hashtags: ['#A'] } });
  let p = await postOf(bt, P);
  assert.deepEqual([p.caption, p.hashtags], ['EDITOR FIXED CAPTION', ['#Mine']]);
  assert.deepEqual(p.suggested, { caption: 'Fresh routine caption', hashtags: ['#A'] });
  assert.equal(p.card_job, j);
  // its pre-check reviewed the routine's caption, not the editor's: shown as out of date
  await robot.call('POST', `/api/routine/jobs/${j}`, { precheck: true, posts: { [P.id]: { check: { factual: [] }, checked_caption: 'Fresh routine caption\n#A' } } }, R);
  p = await postOf(bt, P);
  assert.deepEqual([p.check.status, p.check.stale], ['done', true]);
  // the editor takes the routine's caption: from then on it is the routine's to replace
  assert.equal((await patch(bt, P, { suggested: 'use' })).status, 200);
  p = await postOf(bt, P);
  assert.deepEqual([p.caption, p.hashtags, p.suggested, p.caption_edited_at], ['Fresh routine caption', ['#A'], undefined, undefined]);
  assert.deepEqual([p.check.status, p.check.stale], ['done', false], 'the caption it checked: no longer out of date');
  await done(await claim(await run(bt, 'compose', [P.id])), { [P.id]: { image_b64: img, caption: 'Third caption', hashtags: ['#B'] } });
  p = await postOf(bt, P);
  assert.deepEqual([p.caption, p.suggested], ['Third caption', undefined]);
  // or dismisses the offer
  await patch(bt, P, { caption: 'Mine again' });
  await done(await claim(await run(bt, 'compose', [P.id])), { [P.id]: { image_b64: img, caption: 'Fourth caption' } });
  assert.equal((await postOf(bt, P)).suggested.caption, 'Fourth caption');
  await patch(bt, P, { suggested: 'dismiss' });
  p = await postOf(bt, P);
  assert.deepEqual([p.caption, p.suggested], ['Mine again', undefined]);
});

test('the post times the page suggested, left as they were, move later to the earliest free slot: never sooner, 45 minutes apart', async () => {
  await setup();
  const min = 60e3;
  // a page of its own: no other test's posts queued for it
  const page = await adminPage(owner, 'Gap Page', '2');
  await robot.call('POST', '/api/routine/feed', { page, stories: [1, 2, 3, 4].map(i => ({ id: `g${i}`, headline: `G ${i}`, summary: 'S.', score: 1,
    published_at: Date.now(), url: `https://e.com/g${i}`, sources: [{ name: 'E', url: `https://e.com/g${i}` }],
    ideas: [{ title: 'A', template: 'headline', description: 'a' }], images: [{ url: `https://img.e.com/g${i}.jpg`, source: 'web' }] })) }, R);
  const bt = (await owner.call('POST', '/api/studio/batches', { page, stories: [1, 2, 3, 4].map(i => ({ id: `g${i}` })) })).body.batch;
  for (const [i, P] of bt.posts.entries()) {
    await patch(bt, P, { idea: { choice: 1 }, base_image_url: `https://img.e.com/g${i + 1}.jpg` });
    await done(await claim(await run(bt, 'compose', [P.id])), { [P.id]: { image_b64: img, caption: `Caption ${i + 1}` } });
  }
  const [A, B, C, D] = bt.posts;
  const send = (P, body) => owner.call('POST', `/api/studio/batches/${bt.id}/publish`, { post_id: P.id, ...body });
  const at = async P => (await postOf(bt, P)).publish.requested_at;
  assert.equal((await send(A, { mode: 'schedule', at: Date.now() + 57 * min })).status, 400, 'a time the editor chose is never moved');
  // the Post step was drawn 6 minutes ago: A at its suggestion, B 45 minutes after it
  const hint = (await owner.call('GET', `/api/studio/schedule-hint?page=${page}`)).body;
  const drawn = hint.next - 6 * min;
  const t0 = Date.now();
  assert.equal((await send(A, { mode: 'schedule', at: drawn, default: true })).status, 200);
  assert.equal((await send(B, { mode: 'schedule', at: drawn + hint.gap_minutes * min, default: true })).status, 200);
  const [a, b] = [await at(A), await at(B)];
  assert.ok(a >= t0 + 60 * min && a <= Date.now() + 60 * min, `A ${(a - t0) / min} min`);
  assert.ok(b - a >= 45 * min, `${(b - a) / min} minutes between A and B`);
  // "Post now" is the earliest slot that keeps the gap too
  assert.equal((await send(C, { mode: 'now' })).status, 200);
  const c = await at(C);
  assert.ok(Math.min(Math.abs(c - a), Math.abs(c - b)) >= 45 * min && c >= t0 + 60 * min, `C ${(c - t0) / min} min`);
  // a suggestion later than every queued post is kept as it is
  const later = c + 3 * 60 * min;
  assert.equal((await send(D, { mode: 'schedule', at: later, default: true })).body.batch.posts[3].publish.requested_at, later);
  const next = (await owner.call('GET', `/api/studio/schedule-hint?page=${page}`)).body;
  assert.equal(next.next, later + 45 * min);
});

test('one machine that keeps failing to load packages does not fail the editor\'s job; three machines do', async () => {
  await setup();
  const { bt, P } = await batch();
  const j = await run(bt, 'compose', [P.id]);
  const leave = session => robot.call('POST', `/api/routine/jobs/${j}`, { left: true, error: 'packages not loading: Pillow', session }, R);
  for (let i = 0; i < 4; i++) assert.equal((await leave('session0one')).body.status, 'queued');
  assert.equal((await leave('session0two')).body.status, 'queued');
  assert.equal((await getRecord('jobs', j)).status, 'queued');
  assert.equal((await leave('session0six')).body.status, 'failed');
  assert.match((await postOf(bt, P)).job.error, /could not start this step/);
});

test('new ideas: a choice of idea 1 or 2 that now means another idea is cleared; the editor\'s own idea stays', async () => {
  await setup();
  const { bt, posts } = await batch(3);
  const [A, B, C] = posts;
  await patch(bt, B, { idea: { choice: 'own', text: 'Ranked list of the top 5' } });
  await patch(bt, C, { idea: { choice: 2 } });
  const j = await claim(await run(bt, 'ideas', [A.id, B.id, C.id]));
  const fresh = [{ title: 'NEW wobbly lantern', template: 'quote', description: 'new' }, { title: 'B', template: 'quote', description: 'b' }];
  await done(j, { [A.id]: { ideas: fresh }, [B.id]: { ideas: fresh }, [C.id]: { ideas: fresh } });
  const after = (await getRecord('batches', bt.id)).posts;
  assert.equal(after[0].idea, null, 'idea 1 is another idea now: picked again');
  assert.equal(after[0].story.ideas[0].title, 'NEW wobbly lantern');
  assert.equal(after[1].idea.text, 'Ranked list of the top 5');
  assert.equal(after[2].idea.title, 'B', 'idea 2 is still the same idea');
});

test('Skip check skips only the checks still to come', async () => {
  await setup();
  const { bt, posts } = await batch(2, true);
  const verdict = { factual: ['Wrong score'], timing: [], grammar: [], other: [], guidelines: [] };
  await done(await claim(await run(bt, 'check', [posts[0].id])), { [posts[0].id]: { check: verdict } });
  const r = await owner.call('POST', `/api/studio/batches/${bt.id}/skip-check`, {});
  const [a, b] = r.body.batch.posts;
  assert.deepEqual([a.check.status, a.check.results.factual], ['done', ['Wrong score']]);
  assert.equal(b.check.status, 'skipped');
});

test('the Post step learns what the page sent lately (for its "posted Xh ago" note)', async () => {
  await setup();
  const stamp = new Date(Date.now() - 5 * 3600e3).toISOString().replace(/\D/g, '').slice(0, 12);
  const id = `${slug}-${stamp}-exempt-jacob-josh-monday`;
  await saveDraft({ id, page: slug, page_name: 'Dummy', caption: 'Josh Jacobs is off the exempt list on Monday.', headline: 'JACOBS CLEARED',
                    brief: { headline_hint: 'Jacobs being removed from the Commissioner Exempt List on Monday' } }, tinyJpeg);
  await setStatus(id, 'approved', {});
  await setStatus(id, 'posted', {});
  const old = `${slug}-${new Date(Date.now() - 40 * 3600e3).toISOString().replace(/\D/g, '').slice(0, 12)}-too-old`;
  await saveDraft({ id: old, page: slug, page_name: 'Dummy', caption: 'x', headline: 'OLD' }, tinyJpeg);
  await setStatus(old, 'posted', {});
  assert.equal((await owner.call('GET', `/api/studio/schedule-hint?page=${slug}`)).body.recent, undefined, 'only when asked (the first draw)');
  const hint = (await owner.call('GET', `/api/studio/schedule-hint?page=${slug}&recent=1`)).body;
  const got = hint.recent.find(x => x.id === id);
  assert.equal(got.headline, 'Jacobs being removed from the Commissioner Exempt List on Monday');
  assert.equal(got.status, 'posted');
  assert.match(got.text, /Josh Jacobs/);
  assert.ok(Date.now() - got.at < 60e3);
  assert.ok(!hint.recent.some(x => x.id === old), 'only the last 36 hours');
});

test('a post sent again after its draft failed points to that draft: the publisher looks in SocialPilot before creating it again', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const old = `${slug}-202610211200-studio-${P.id}`;
  await saveDraft({ id: old, page: slug, page_name: 'Dummy', caption: 'x', headline: 'h' }, tinyJpeg);
  await setStatus(old, 'publishing', {});
  await setStatus(old, 'failed', { error: 'interrupted' });
  await updateRecord('batches', bt.id, cur => { cur.posts[0].publish = { draft_id: old, state: 'failed', error: 'interrupted' }; return cur; });
  // the run broke off: it may be on Facebook, so the editor is asked first (as on the review page),
  // and told that its caption changed since, so the publisher's own check cannot find that copy
  const ask = await owner.call('POST', `/api/studio/batches/${bt.id}/publish`, { post_id: P.id, mode: 'now' });
  assert.deepEqual([ask.status, ask.body.confirm], [409, { facebook: true, note: '', caption_changed: true, old_caption: 'x' }]);
  const r = await owner.call('POST', `/api/studio/batches/${bt.id}/publish`, { post_id: P.id, mode: 'now', confirmed: true });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  const sent = (await postOf(bt, P)).publish;
  assert.notEqual(sent.draft_id, old);
  assert.equal(sent.resent_from, old);
  const item = (await robot.call('GET', '/api/queue', undefined, R)).body.queue.find(x => x.id === sent.draft_id);
  assert.equal(item.ever_sent, true, 'the old draft was claimed once: it may be in SocialPilot already');
});

// ---- a post sent again from the Studio, and the review page (findings 2, 12, 15) ----------------
// the post's draft from an earlier Send (an earlier minute: a Send now makes a new draft), failed
async function failedDraft(bt, P, body) {
  const id = `${slug}-202610211200-studio-${P.id}`;
  await saveDraft({ id, page: slug, page_name: 'Dummy', caption: 'Routine caption 1\n\n#r', headline: 'h', engine: 'studio',
                    studio: { batch: bt.id, post: P.id, user: 'owner@example.com' } }, tinyJpeg);
  await setStatus(id, 'approved', {});
  await tick();
  await setStatus(id, 'failed', body);
  await updateRecord('batches', bt.id, cur => { cur.posts.find(p => p.id === P.id).publish = { draft_id: id, state: 'failed', error: body.error }; return cur; });
  return id;
}
const NO_ACCOUNT = 'page has no SocialPilot account: set it in the admin panel, then retry';
const sendNow = (bt, P, body = {}) => owner.call('POST', `/api/studio/batches/${bt.id}/publish`, { post_id: P.id, mode: 'now', ...body });
const queued = async P => (await robot.call('GET', '/api/queue', undefined, R)).body.queue.map(x => x.id).filter(id => id.endsWith(`-studio-${P.id}`));

test('a post sent again from the Studio retires its failed draft: the review page cannot send it as well', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const old = await failedDraft(bt, P, { error: NO_ACCOUNT });
  const r = await sendNow(bt, P);
  assert.equal(r.status, 200, JSON.stringify(r.body));                 // nothing suggests it reached SocialPilot: no question
  const now = (await postOf(bt, P)).publish.draft_id;
  assert.notEqual(now, old);
  const was = await loadDraft(old);
  assert.deepEqual([was.status, was.statusBody.reason, was.statusBody.withdrawn], ['rejected', `resent from the Studio as ${now}`, true]);
  // Retry all failed on the review page, or Okay on its card: neither sends it too
  assert.equal((await owner.call('POST', '/api/decide', { ids: [old], decision: 'retry', confirmed: true })).body.retried, 0);
  assert.equal((await owner.call('POST', '/api/decide', { id: old, decision: 'approve', confirmed: true })).status, 409);
  assert.deepEqual(await queued(P), [now]);
});

test('a failed Studio draft whose post was sent again before the Studio retired old drafts is refused a retry', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const old = await failedDraft(bt, P, { error: 'SocialPilot was unavailable for over 12h after this was approved: retry it if it is still timely', outage: true });
  const now = `${slug}-202610211300-studio-${P.id}`;                   // sent again, the old draft left in Failed
  await saveDraft({ id: now, page: slug, page_name: 'Dummy', caption: 'x', headline: 'h', resent_from: old, studio: { batch: bt.id, post: P.id } }, tinyJpeg);
  await setStatus(now, 'approved', {});
  await updateRecord('batches', bt.id, cur => { cur.posts[0].publish = { draft_id: now, state: 'approved', resent_from: old }; return cur; });
  const retry = (await owner.call('POST', '/api/decide', { ids: [old], decision: 'retry', confirmed: true })).body;
  assert.deepEqual([retry.retried, retry.refused], [0, [{ id: old, why: `resent from the Studio as ${now}` }]]);
  const one = await owner.call('POST', '/api/decide', { id: old, decision: 'approve', confirmed: true });
  assert.deepEqual([one.status, one.body.error], [409, `not approved again: resent from the Studio as ${now}`]);
  assert.equal((await loadDraft(old)).status, 'failed');
  assert.deepEqual(await queued(P), [now]);
  // the post's own draft, once it fails, can still be retried there
  await tick();
  await setStatus(now, 'failed', { error: NO_ACCOUNT });
  assert.equal((await owner.call('POST', '/api/decide', { ids: [now], decision: 'retry' })).body.retried, 1);
});

test('Studio Retry reads the old draft as the publisher has it: approved again on the review page, it is not sent twice', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const old = await failedDraft(bt, P, { error: NO_ACCOUNT });
  assert.equal((await owner.call('POST', '/api/decide', { ids: [old], decision: 'retry' })).body.retried, 1);
  assert.equal((await postOf(bt, P)).publish.state, 'failed', 'the batch has not caught up yet');
  const r = await sendNow(bt, P);
  assert.deepEqual([r.status, r.body.error], [409, 'already sent: its draft was retried (on the review page) and is approved now']);
  assert.equal((await postOf(bt, P)).publish.draft_id, old);
  assert.deepEqual(await queued(P), [old]);
  assert.equal((await viewed(bt, P)).publish.state, 'approved', 'and the page shows it is with the publisher');
});

test('Studio Retry asks what the review page asks when the last try may be on Facebook or has a red note', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const old = await failedDraft(bt, P, { error: LOOKUPS_FAILED, check_facebook: true });
  const look = 'look in SocialPilot and on the Facebook page: the publisher could not check whether this post is there already';
  await robot.call('POST', '/api/result', { id: old, status: 'needs_action', error: look }, R);
  const ask = await sendNow(bt, P);
  assert.deepEqual([ask.status, ask.body.confirm], [409, { facebook: true, note: look }]);
  let p = await postOf(bt, P);
  assert.deepEqual([p.publish.draft_id, p.publish.state, (await loadDraft(old)).status], [old, 'failed', 'failed'], 'nothing was changed');
  assert.equal((await sendNow(bt, P, { confirmed: true })).status, 200);
  p = await postOf(bt, P);
  assert.equal(p.publish.resent_from, old);
  assert.equal((await loadDraft(old)).status, 'rejected');
  assert.equal(old in await needsAction(), false, 'they looked on Facebook: the note is settled');
  // a red note alone (a copy on the wrong account to delete) is asked about too, and stays until marked done
  const two = await batch(1, true);
  const o2 = await failedDraft(two.bt, two.P, { error: NO_ACCOUNT });
  await robot.call('POST', '/api/result', { id: o2, status: 'needs_action', error: 'delete the copy made on the wrong SocialPilot account' }, R);
  assert.deepEqual((await sendNow(two.bt, two.P)).body.confirm, { facebook: false, note: 'delete the copy made on the wrong SocialPilot account' });
  assert.equal((await sendNow(two.bt, two.P, { confirmed: true })).status, 200);
  assert.ok(o2 in await needsAction(), 'someone still has to delete that copy');
  // the notes cannot be read right now: refused (nothing changed), never sent as if it had none
  const three = await batch(1, true);
  const o3 = await failedDraft(three.bt, three.P, { error: NO_ACCOUNT });
  await robot.call('POST', '/api/result', { id: o3, status: 'needs_action', error: 'delete the copy made on the wrong SocialPilot account' }, R);
  const { resetReads } = await import('../lib/db.js');
  const realGet = mem.get;
  mem.get = function (k) { return String(k).startsWith('db/health/needs_action/') ? undefined : realGet.call(this, k); };
  resetReads();
  try {
    const r = await sendNow(three.bt, three.P);
    assert.equal(r.status, 503, JSON.stringify(r.body));
  } finally { mem.get = realGet; resetReads(); }
  assert.equal((await postOf(three.bt, three.P)).publish.draft_id, o3, 'nothing was sent');
  assert.equal((await sendNow(three.bt, three.P)).body.confirm.note, 'delete the copy made on the wrong SocialPilot account');
});

test('a Send that was sending a post again and was cut short is finished as that resend', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const old = await failedDraft(bt, P, { error: NO_ACCOUNT });
  const cut = `${slug}-202610211230-studio-${P.id}`;                   // marked, then storage refused a read
  await updateRecord('batches', bt.id, cur => {
    cur.posts[0].publish = { draft_id: cut, state: 'sending', sending_at: Date.now() - 3 * 60e3, mode: 'now', resent_from: old }; return cur; });
  assert.equal((await sendNow(bt, P)).status, 200);
  const p = await postOf(bt, P);
  assert.deepEqual([p.publish.draft_id, p.publish.state, p.publish.resent_from], [cut, 'approved', old]);
  assert.equal((await loadDraft(cut)).resent_from, old, 'the publisher still looks for the old one first');
  assert.equal((await loadDraft(old)).status, 'rejected');
  assert.deepEqual(await queued(P), [cut]);
});

// ---- the second review of the resend (studio-2 findings 0, 1, 3, 4, 5) --------------------------
// a Studio draft of the post from an earlier minute, claimed by a publisher run and then failed
async function wentOut(bt, P, minute, error, extra = {}) {
  const id = `${slug}-2026102112${minute}-studio-${P.id}`;
  await saveDraft({ id, page: slug, page_name: 'Dummy', caption: 'Routine caption 1\n\n#r', headline: 'h', engine: 'studio',
                    studio: { batch: bt.id, post: P.id, user: 'owner@example.com' }, ...extra }, tinyJpeg);
  for (const st of ['approved', 'publishing']) { await setStatus(id, st, {}); await tick(); }
  await setStatus(id, 'failed', { error });
  return id;
}
const TIMED_OUT = 'CreatePost: Request timed out';
const itemOf = async id => (await robot.call('GET', '/api/queue', undefined, R)).body.queue.find(x => x.id === id);
// what another request writes the moment this one writes a path matching `when` (two people at once)
function meanwhile(when, write) {
  mem.set = (k, v) => { const r = Map.prototype.set.call(mem, k, v); if (when.test(k)) { delete mem.set; write(); } return r; };
}
let lastAt = 0;
const statusNow = (id, status, body = {}) => {   // a status record written by that other request
  const at = lastAt = Math.max(Date.now(), lastAt + 1);             // (never ahead of what is written after it)
  Map.prototype.set.call(mem, `drafts/${id}/s-${at}-${status}.json`, { buf: Buffer.from(JSON.stringify({ status, at, ...body })),
                                                                       contentType: 'application/json', uploadedAt: new Date() });
  return at;
};

test('sent again twice: the publisher still looks for the first draft that went to SocialPilot, and the editor is asked again', async () => {
  await setup();
  // A timed out in CreatePost (it may be there); the editor looked and sent it again as B (this minute)
  let { bt, P } = await batch(1, true);
  const A = await wentOut(bt, P, '00', TIMED_OUT);
  await updateRecord('batches', bt.id, cur => { cur.posts[0].publish = { draft_id: A, state: 'failed', error: TIMED_OUT }; return cur; });
  assert.equal((await sendNow(bt, P, { confirmed: true })).status, 200);
  const B = (await postOf(bt, P)).publish.draft_id;
  // B fails before any run takes it (no account): sent again (mostly in the same minute: B's id again)
  await setStatus(B, 'failed', { error: NO_ACCOUNT });
  assert.equal((await viewed(bt, P)).publish.state, 'failed');
  const ask = await sendNow(bt, P);
  assert.deepEqual([ask.status, ask.body.confirm.facebook], [409, true], 'A may still be there: asked again');
  assert.equal((await sendNow(bt, P, { confirmed: true })).status, 200);
  const now = (await postOf(bt, P)).publish;
  assert.deepEqual([now.resent_from, (await loadDraft(now.draft_id)).resent_from], [A, A]);
  assert.equal((await itemOf(now.draft_id)).ever_sent, true, 'the publisher looks for A first');
  // the same with drafts of their own (other minutes): A, then B sent again from A and left in
  // Failed (before the Studio retired drafts), then the post sent now as C
  ({ bt, P } = await batch(1, true));
  const A2 = await wentOut(bt, P, '00', TIMED_OUT), B2 = `${slug}-202610211230-studio-${P.id}`;
  await saveDraft({ id: B2, page: slug, page_name: 'Dummy', caption: 'Routine caption 1\n\n#r', headline: 'h', resent_from: A2,
                    studio: { batch: bt.id, post: P.id } }, tinyJpeg);
  await setStatus(B2, 'approved', {}); await tick();
  await setStatus(B2, 'failed', { error: NO_ACCOUNT });
  await updateRecord('batches', bt.id, cur => { cur.posts[0].publish = { draft_id: B2, state: 'failed', error: NO_ACCOUNT, resent_from: A2 }; return cur; });
  assert.equal((await sendNow(bt, P)).body.confirm.facebook, true);
  assert.equal((await sendNow(bt, P, { confirmed: true })).status, 200);
  const C = (await postOf(bt, P)).publish;
  assert.deepEqual([C.resent_from, C.replaces, (await loadDraft(B2)).status], [A2, B2, 'rejected']);
  assert.equal((await itemOf(C.draft_id)).ever_sent, true);
  const retry = (await owner.call('POST', '/api/decide', { ids: [A2, B2], decision: 'retry', confirmed: true })).body;
  assert.equal(retry.retried, 0);
});

test('a resend whose caption changed warns that the publisher cannot find the earlier copy by it', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const A = await wentOut(bt, P, '00', TIMED_OUT);
  await updateRecord('batches', bt.id, cur => { cur.posts[0].publish = { draft_id: A, state: 'failed', error: TIMED_OUT }; return cur; });
  assert.deepEqual((await sendNow(bt, P)).body.confirm, { facebook: true, note: '' }, 'the same caption: SocialPilot is searched for it');
  await patch(bt, P, { caption: 'Fixed after the failure' });
  assert.deepEqual((await sendNow(bt, P)).body.confirm, { facebook: true, note: '', caption_changed: true, old_caption: 'Routine caption 1\n\n#r' });
  assert.equal((await postOf(bt, P)).publish.draft_id, A, 'nothing sent before they answer');
});

test('a resend cut short before its draft exists leaves the old draft in Failed; the next Send finishes and retires it', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const old = await failedDraft(bt, P, { error: NO_ACCOUNT });
  mem.set = (k, v) => { if (k.startsWith('drafts/') && !k.startsWith(`drafts/${old}/`) && k.includes('/card-')) throw new Error('storage refused'); return Map.prototype.set.call(mem, k, v); };
  let r;
  try { r = await sendNow(bt, P); } finally { delete mem.set; }
  assert.deepEqual([r.status, r.body.error], [503, 'storage did not save the post: nothing was sent, press Send again']);
  assert.equal((await loadDraft(old)).status, 'failed', 'reviewers still see that the post did not go out');
  assert.ok((await robot.call('GET', '/api/drafts?status=failed', undefined, R)).body.drafts.some(d => d.id === old));
  assert.equal((await postOf(bt, P)).publish.state, 'sending');
  assert.equal((await sendNow(bt, P)).status, 200);
  const p = (await postOf(bt, P)).publish;
  assert.deepEqual([p.state, p.resent_from, (await loadDraft(old)).status], ['approved', old, 'rejected']);
  assert.deepEqual(await queued(P), [p.draft_id]);
});

test('approved again on the review page at the same moment as a Studio resend: one of the two goes out', async () => {
  await setup();
  const isNew = old => new RegExp(`^drafts/(?!${old}/)[^/]+/s-\\d+-approved\\.json$`);
  // 1. the approval lands while the Studio makes its new draft: the old one is taken back
  let { bt, P } = await batch(1, true);
  let old = await failedDraft(bt, P, { error: NO_ACCOUNT });
  meanwhile(isNew(old), () => statusNow(old, 'approved', { retry: true }));
  assert.equal((await sendNow(bt, P)).status, 200);
  let now = (await postOf(bt, P)).publish.draft_id;
  let was = await loadDraft(old);
  assert.deepEqual([was.status, was.statusBody.reason, await queued(P)], ['rejected', `resent from the Studio as ${now}`, [now]]);
  // 2. a publisher run has the old one already: the Studio's new draft is taken back, the post shows the old one
  ({ bt, P } = await batch(1, true));
  old = await failedDraft(bt, P, { error: NO_ACCOUNT });
  meanwhile(isNew(old), () => { statusNow(old, 'approved', { retry: true }); statusNow(old, 'publishing', {}); });
  const r = await sendNow(bt, P);
  assert.deepEqual([r.status, r.body.error], [409, 'already sent: its draft was retried on the review page at the same moment']);
  const ids = (await robot.call('GET', '/api/queue', undefined, R)).body.in_progress.map(x => x.id);
  assert.ok(ids.includes(old));
  now = [...mem.keys()].map(k => k.split('/')[1]).find(d => d.endsWith(`-studio-${P.id}`) && d !== old);
  assert.equal((await loadDraft(now)).status, 'rejected');
  assert.deepEqual([(await postOf(bt, P)).publish.draft_id, await queued(P)], [old, []]);
  // 3. the review page read the post before the Studio moved it to its new draft, and approved the old
  // one after: it takes its own approval back
  for (const decide of [{ ids: [], decision: 'retry' }, { decision: 'approve' }]) {
    ({ bt, P } = await batch(1, true));
    old = await failedDraft(bt, P, { error: NO_ACCOUNT });
    const C = `${slug}-202610211300-studio-${P.id}`;
    await saveDraft({ id: C, page: slug, page_name: 'Dummy', caption: 'x', headline: 'h', resent_from: old, studio: { batch: bt.id, post: P.id } }, tinyJpeg);
    await setStatus(C, 'approved', {});
    await updateRecord('batches', bt.id, cur => { cur.posts[0].publish = { draft_id: C, state: 'approved', resent_from: old, replaces: old }; return cur; });
    const [k] = [...mem.keys()].filter(x => x.startsWith(`db/batches/${bt.id}/`)).sort().slice(-1);
    const v = mem.get(k);
    mem.delete(k);                                                     // (what the review page read)
    meanwhile(new RegExp(`^drafts/${old}/s-\\d+-approved\\.json$`), () => Map.prototype.set.call(mem, k, v));
    const d = await owner.call('POST', '/api/decide', { ...decide, ...(decide.ids ? { ids: [old] } : { id: old }) });
    if (decide.ids) assert.deepEqual([d.body.retried, d.body.refused], [0, [{ id: old, why: `resent from the Studio as ${C}` }]]);
    else assert.deepEqual([d.status, d.body.error], [409, `not approved again: resent from the Studio as ${C}`]);
    assert.deepEqual([(await loadDraft(old)).status, await queued(P)], ['rejected', [C]]);
  }
});

test('other ways around "one post, one draft": redoing a retired draft, a draft being redone, a direct publish', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const old = await failedDraft(bt, P, { error: NO_ACCOUNT });
  assert.equal((await sendNow(bt, P)).status, 200);
  const now = (await postOf(bt, P)).publish.draft_id;
  const redo = await owner.call('POST', '/api/feedback', { id: old, feedback: 'make it shorter' });
  assert.deepEqual([redo.status, redo.body.error], [409, `not redone: resent from the Studio as ${now}`]);
  // a post whose draft the review page is redoing (feedback) is not sent again from the Studio
  const two = await batch(1, true);
  const o2 = await failedDraft(two.bt, two.P, { error: NO_ACCOUNT });
  assert.equal((await owner.call('POST', '/api/feedback', { id: o2, feedback: 'other photo' })).status, 200);
  await viewed(two.bt, two.P);
  const r = await sendNow(two.bt, two.P);
  assert.deepEqual([r.status, r.body.error], [409, 'its draft is being redone on the review page (feedback): approve or reject it there']);
  // redone, and the new draft turned down there: the Studio may send it again
  const n2 = `${slug}-202610211400-redo`;
  await saveDraft({ id: n2, page: slug, page_name: 'Dummy', caption: 'Redone', headline: 'h' }, tinyJpeg);
  await setStatus(o2, 'regenerated', { new_id: n2 });
  assert.equal((await sendNow(two.bt, two.P)).status, 409, 'its redone draft waits on the review page');
  await setStatus(n2, 'rejected', { reason: 'Weak story' });
  assert.equal((await sendNow(two.bt, two.P)).status, 200);
  // POST /api/publish no longer approves a failed draft without the review page's checks
  const three = await batch(1, true);
  const o3 = await failedDraft(three.bt, three.P, { error: TIMED_OUT, check_facebook: true });
  const pub = await owner.call('POST', '/api/publish', { id: o3 });
  assert.deepEqual([pub.status, (await loadDraft(o3)).status], [400, 'failed']);
});

// ---- the Studio's jobs and buttons (findings 7, 17, 18, 20) --------------------------------------
test('jobs of one batch still waiting are restarted with one session per round, not one each', async () => {
  await setup();
  const { bt, posts } = await batch(3);
  const r = (await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: posts.map(p => p.id) })).body;
  assert.equal(r.jobs.length, 3);
  const inner = globalThis.fetch;
  let calls = 0;
  process.env.STUDIO_FIRE_URL = 'https://fire.test/studio'; process.env.STUDIO_FIRE_TOKEN = 't';
  globalThis.fetch = async (url, opts) => { if (String(url) !== process.env.STUDIO_FIRE_URL) return inner(url, opts); calls++; return new Response('{}', { status: 200 }); };
  try {
    for (const j of r.jobs) await aged(j, 5);                          // nobody started on them
    await viewed(bt, posts[0]);
    assert.deepEqual([r.jobs.map(nudgesOf), calls], [[1, 1, 1], 1]);
    await viewed(bt, posts[0]);
    assert.equal(calls, 1, 'not again within 4 minutes');
    for (const j of r.jobs) await aged(j, 5);
    await viewed(bt, posts[0]);
    assert.deepEqual([r.jobs.map(nudgesOf), calls], [[2, 2, 2], 2], 'one more session each round they still wait');
  } finally {
    globalThis.fetch = inner;
    delete process.env.STUDIO_FIRE_URL; delete process.env.STUDIO_FIRE_TOKEN;
  }
});

test('a job whose storage read fails does not keep the jobs counted before it from their session', async () => {
  await setup();
  const { bt, posts } = await batch(2);
  const [j1, j2] = (await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: posts.map(p => p.id) })).body.jobs;
  const inner = globalThis.fetch;
  let calls = 0;
  process.env.STUDIO_FIRE_URL = 'https://fire.test/studio'; process.env.STUDIO_FIRE_TOKEN = 't';
  globalThis.fetch = async (url, opts) => { if (String(url) !== process.env.STUDIO_FIRE_URL) return inner(url, opts); calls++; return new Response('{}', { status: 200 }); };
  // storage refuses a listing once j1's restart is counted: the second one after it, j2's restarts
  // (the first is j2's record)
  let after = -1;
  mem.set = (k, v) => { if (k.startsWith(`job-nudges/${j1}/`)) after = 0; return Map.prototype.set.call(mem, k, v); };
  mem.entries = () => { if (after >= 0 && ++after === 2) throw new Error('storage is not answering'); return Map.prototype.entries.call(mem); };
  try {
    for (const j of [j1, j2]) await aged(j, 5);
    await viewed(bt, posts[0]);
  } finally {
    delete mem.set; delete mem.entries;
    globalThis.fetch = inner;
    delete process.env.STUDIO_FIRE_URL; delete process.env.STUDIO_FIRE_TOKEN;
  }
  assert.deepEqual([nudgesOf(j1), nudgesOf(j2), calls], [1, 0, 1], 'j1 was counted: its session is started');
});

test('a visual whose session stopped answering: the page is told, Start again replaces it, the failure says how long it took', async () => {
  await setup();
  const { bt, P } = await batch();
  const j = await claim(await run(bt, 'compose', [P.id]));
  assert.equal((await viewed(bt, P)).job.stale, undefined, 'a session has it');
  await aged(j, 21);
  const p = await viewed(bt, P);
  assert.deepEqual([p.job.id, p.job.status, p.job.stale], [j, 'running', true]);
  assert.equal((await postOf(bt, P)).job.stale, undefined, 'only in the answer, never stored');
  // left alone: restarted every 4 minutes, then failed with the time it really took
  for (let n = 2; n <= 5; n++) { await aged(j, 5); await viewed(bt, P); }
  assert.equal(nudgesOf(j), 5);
  await aged(j, 5);
  const f = await viewed(bt, P);
  assert.equal(f.job.status, 'failed');
  assert.match(f.job.error, /after 5 restarts \(about 46 minutes\)/);
  // Start again: the stalled one is cancelled and replaced at once
  const two = await batch();
  const k = await claim(await run(two.bt, 'compose', [two.P.id]));
  await aged(k, 21);
  assert.equal((await viewed(two.bt, two.P)).job.stale, true);
  const fresh = await run(two.bt, 'compose', [two.P.id]);
  assert.ok(fresh && fresh !== k);
  assert.equal((await getRecord('jobs', k)).status, 'cancelled');
  const q = await viewed(two.bt, two.P);
  assert.deepEqual([q.job.id, q.job.status, q.job.stale, q.under_way], [fresh, 'queued', undefined, undefined]);
});

test('a batch a colleague cleared: its page says who did, nothing is queued only to vanish, and it can be restored', async () => {
  await setup();
  const inv = await owner.call('POST', '/api/admin/users', { email: 'colleague@example.com', name: 'C', role: 'editor', workspace: owner.ws, pages: [slug] });
  const ed = client();
  await ed.call('POST', '/api/auth/reset', { token: inv.body.setup_link.split('#reset=')[1], password: 'correct horse 42' });
  assert.equal((await ed.call('POST', '/api/auth/login', { email: 'colleague@example.com', password: 'correct horse 42' })).status, 200);
  const bt = (await ed.call('POST', '/api/studio/batches', { page: slug, stories: [{ id: 's1' }] })).body.batch;
  const P = bt.posts[0];
  await ed.call('PATCH', `/api/studio/batches/${bt.id}/posts/${P.id}`, { idea: { choice: 1 }, base_image_url: 'https://img.e.com/1a.jpg' });
  assert.equal((await owner.call('POST', '/api/studio/batches/clear', { ids: [bt.id] })).body.cleared, 1);   // Select all, Clear
  assert.equal((await ed.call('GET', `/api/studio/batches/${bt.id}`)).body.batch.cleared.by, 'owner@example.com');
  // its editor, who was not on its page, still finds it on the Studio home (for a day), with who cleared it
  const home = async c => (await c.call('GET', `/api/studio/batches?page=${slug}`)).body.batches.find(x => x.id === bt.id);
  assert.equal((await home(ed)).cleared.by, 'owner@example.com');
  assert.equal(await home(owner), undefined, 'not for the one who cleared it');
  const r = await ed.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [P.id] });
  assert.equal(r.status, 409);
  assert.match(r.body.error, /^owner@example\.com cleared this batch just now: restore it/);
  assert.equal(r.body.cleared.by, 'owner@example.com');
  assert.equal((await postOf(bt, P)).job, null, 'no job was queued');
  // its editor restores it: work goes on, and its jobs are not stopped any more
  const back = (await ed.call('POST', `/api/studio/batches/${bt.id}/restore`, {})).body.batch;
  assert.deepEqual([back.cleared, back.restored.by, back.restored.cleared.by], [undefined, 'colleague@example.com', 'owner@example.com']);
  const j = (await ed.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [P.id] })).body.job;
  assert.ok(j);
  await ed.call('GET', `/api/studio/batches/${bt.id}`);
  assert.equal((await getRecord('jobs', j)).status, 'queued');
  assert.equal((await home(ed)).cleared, undefined, 'listed again, as before');
  // cleared again, a day ago: no longer listed
  await owner.call('POST', '/api/studio/batches/clear', { ids: [bt.id] });
  await updateRecord('batches', bt.id, cur => ({ ...cur, cleared: { ...cur.cleared, at: cur.cleared.at - 25 * 3600e3 } }));
  assert.equal(await home(ed), undefined);
});

test('"Post now" is told the time it will get: the page\'s first free slot, not "about an hour"', async () => {
  await setup();
  const min = 60e3;
  const page = await adminPage(owner, 'Now Page', '3');
  await robot.call('POST', '/api/routine/feed', { page, stories: [1, 2].map(i => ({ id: `n${i}`, headline: `N ${i}`, summary: 'S.', score: 1,
    published_at: Date.now(), url: `https://e.com/n${i}`, sources: [{ name: 'E', url: `https://e.com/n${i}` }],
    ideas: [{ title: 'A', template: 'headline', description: 'a' }], images: [{ url: `https://img.e.com/n${i}.jpg`, source: 'web' }] })) }, R);
  const bt = (await owner.call('POST', '/api/studio/batches', { page, stories: [{ id: 'n1' }, { id: 'n2' }] })).body.batch;
  for (const [i, P] of bt.posts.entries()) {
    await patch(bt, P, { idea: { choice: 1 }, base_image_url: `https://img.e.com/n${i + 1}.jpg` });
    await done(await claim(await run(bt, 'compose', [P.id])), { [P.id]: { image_b64: img, caption: `Caption ${i + 1}` } });
  }
  const [A, B] = bt.posts;
  const hint = async () => (await owner.call('GET', `/api/studio/schedule-hint?page=${page}`)).body;
  const empty = await hint();
  assert.ok(Math.abs(empty.now_at - (Date.now() + 60 * min)) < 5000, 'nothing queued: an hour from now');
  assert.equal((await sendNow(bt, A)).status, 200);
  const a = (await postOf(bt, A)).publish.requested_at;
  const full = await hint();
  assert.equal(full.now_at, a + 45 * min, '45 minutes after the post queued in that hour');
  // a label drawn before A was queued: B goes out 45 minutes later than it said, so the editor is asked
  const ask = await sendNow(bt, B, { shown_at: empty.now_at });
  assert.deepEqual([ask.status, ask.body.confirm], [409, { facebook: false, note: '', now_at: full.now_at }]);
  assert.equal((await postOf(bt, B)).publish, null, 'nothing sent before they answer');
  assert.equal((await sendNow(bt, B, { shown_at: full.now_at })).status, 200, 'a label that was right: no question');
  assert.equal((await postOf(bt, B)).publish.requested_at, full.now_at, 'the time the label said');
});

test('a click on the photo already chosen keeps the finished visual and its check', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  await done(await claim(await run(bt, 'check', [P.id])), { [P.id]: { check: { factual: ['The deal is $102M'] } } });
  const before = await postOf(bt, P);
  assert.deepEqual([before.status, before.check.status], ['checked', 'done']);
  assert.equal((await patch(bt, P, { base_image_url: 'https://img.e.com/1a.jpg' })).status, 200);
  const p = await postOf(bt, P);
  assert.deepEqual([p.status, p.final_image_url, p.check], ['checked', before.final_image_url, before.check]);
  // another photo replaces it (the page asks first)
  await patch(bt, P, { base_image_url: 'https://img.e.com/1b.jpg' });
  assert.deepEqual([(await postOf(bt, P)).final_image_url, (await postOf(bt, P)).status], ['', 'ideated']);
});

test('Re-run check keeps the findings until a new check replaces them; a re-check that fails gives them back', async () => {
  await setup();
  const findings = { factual: ['The deal is $102M, not $120M'], other: ['Carter wears #98, not #89'] };
  const { bt, P } = await batch(1, true);
  await done(await claim(await run(bt, 'check', [P.id])), { [P.id]: { check: findings } });
  // pressed again: waiting, with the earlier result kept beside it
  let jc = await run(bt, 'check', [P.id]);
  let p = await postOf(bt, P);
  assert.deepEqual([p.check.status, p.check.previous.results], ['queued', findings]);
  // the re-check fails: the findings are back
  await claim(jc);
  await done(jc, {}, 'failed');
  p = await postOf(bt, P);
  assert.deepEqual([p.check.status, p.check.results, p.status], ['done', findings, 'checked']);
  // handed back three times: the same
  jc = await claim(await run(bt, 'check', [P.id]));
  for (let n = 0; n < 3; n++) {
    await robot.call('POST', `/api/routine/jobs/${jc}`, { status: 'queued', release: true, error: 'KeyError: x' }, R);
    if (n < 2) await claim(jc);
  }
  p = await postOf(bt, P);
  assert.deepEqual([p.job.status, p.check.status, p.check.results], ['failed', 'done', findings]);
  // Skip check while a re-check waits: the findings stay (the check counts as done)
  const waiting = await run(bt, 'check', [P.id]);
  await owner.call('POST', `/api/studio/batches/${bt.id}/skip-check`, {});
  p = await postOf(bt, P);
  assert.deepEqual([p.check.status, p.check.results], ['done', findings]);
  // a new result replaces them
  await done(await claim(waiting), { [P.id]: { check: { factual: [] } } });
  p = await postOf(bt, P);
  assert.deepEqual([p.check.status, p.check.results, p.check.previous], ['done', { factual: [] }, undefined]);
});

test('a check job that fails one post of two leaves nothing waiting for ever', async () => {
  await setup();
  const { bt, posts: [A, B] } = await batch(2, true);
  const jc = await claim(await run(bt, 'check', [A.id, B.id]));
  await done(jc, { [A.id]: { check: { factual: [] } }, [B.id]: { error: 'web search unavailable' } });
  const a = await postOf(bt, A), b = await postOf(bt, B);
  assert.equal(a.check.status, 'done');
  assert.deepEqual([b.check.status, b.job.error], ['none', 'web search unavailable']);
});

test('a post sent to SocialPilot gets no new visual, image search or check: a batch-wide check checks the others', async () => {
  await setup();
  const { bt, posts: [A, B] } = await batch(2, true);
  assert.equal((await owner.call('POST', `/api/studio/batches/${bt.id}/publish`, { post_id: A.id, mode: 'now' })).status, 200);
  for (const kind of ['compose', 'images', 'check']) {
    const r = await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind, post_ids: [A.id], feedback: 'bigger' });
    assert.deepEqual([r.status, r.body.error], [409, 'already sent to SocialPilot: it can no longer be changed here'], kind);
  }
  const all = await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'check', post_ids: [A.id, B.id] });
  assert.equal(all.status, 200);
  assert.deepEqual((await getRecord('jobs', all.body.job)).post_ids, [B.id]);
  assert.notEqual((await postOf(bt, A)).check.status, 'queued');
});

test('a Studio post whose publishing failed after its editor left: its batch is unfinished again on the Studio home', async () => {
  await setup();
  const { bt, posts: [A, B] } = await batch(2, true);
  for (const x of [A, B]) assert.equal((await owner.call('POST', `/api/studio/batches/${bt.id}/publish`, { post_id: x.id, mode: 'now' })).status, 200);
  const listed = async () => (await owner.call('GET', `/api/studio/batches?page=${slug}`)).body.batches.find(x => x.id === bt.id);
  assert.equal((await listed()).done, true);
  const draft = (await postOf(bt, B)).publish.draft_id;
  assert.equal((await robot.call('POST', '/api/result', { id: draft, status: 'failed', error: 'the Facebook page needs to be reconnected' }, R)).status, 200);
  const l = await listed();
  assert.deepEqual([l.done, l.posts.map(p => p.status)], [false, ['scheduled', 'failed']]);
  assert.equal((await postOf(bt, B)).status, 'failed', 'saved, so it is read only until it settles');
});

test('a Studio post withdrawn on the review page says who withdrew it', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  assert.equal((await owner.call('POST', `/api/studio/batches/${bt.id}/publish`, { post_id: P.id, mode: 'now' })).status, 200);
  const draft = (await postOf(bt, P)).publish.draft_id;
  assert.equal((await owner.call('POST', '/api/decide', { id: draft, decision: 'withdraw' })).status, 200);
  const p = await viewed(bt, P);
  assert.deepEqual([p.publish.state, p.publish.withdrawn, p.publish.by], ['rejected', true, 'owner@example.com']);
});

test('the same link pasted twice, or a story picked twice, is one post', async () => {
  await setup();
  const { remote } = await import('./helpers.mjs');
  remote.set('https://news.example.com/carter', { body: '<meta property="og:title" content="Eagles sign Jalen Carter">' });
  const read = async () => (await owner.call('POST', '/api/studio/url', { page: slug, url: 'https://news.example.com/carter' })).body.story;
  const [one, two] = [await read(), await read()];
  assert.equal(one.id, two.id);
  const bt = (await owner.call('POST', '/api/studio/batches', { page: slug, stories: [one, two, { id: 's1' }, { id: 's1' }] })).body.batch;
  assert.deepEqual(bt.posts.map(p => p.story.headline), ['Eagles sign Jalen Carter', 'Story 1']);
});

test('Start again on one post of a shared job that stopped: the other posts keep waiting for it, never stopped', async () => {
  await setup();
  const { bt, posts: [A, B] } = await batch(2);
  const j = await claim(await run(bt, 'ideas', [A.id, B.id]));
  await aged(j, 21);
  const fresh = await run(bt, 'ideas', [A.id]);
  assert.ok(fresh && fresh !== j);
  assert.equal((await getRecord('jobs', j)).status, 'running', 'still B\'s: restarted for it');
  const b = await viewed(bt, B);
  assert.deepEqual([b.job.id, b.job.status], [j, 'running']);
  assert.equal((await viewed(bt, A)).job.id, fresh);
});

test('a caption longer than the limit is refused, never cut; a routine caption is cut on whole characters', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const r = await patch(bt, P, { caption: 'x'.repeat(5001) });
  assert.deepEqual([r.status, r.body.error], [400, 'captions can be at most 5,000 characters (yours has 5,001): shorten it']);
  assert.equal((await postOf(bt, P)).caption, 'Routine caption 1');
  // 5,000 emoji are 5,000 characters (10,000 UTF-16 units): kept whole
  const emoji = '🏀'.repeat(5000);
  assert.equal((await patch(bt, P, { caption: emoji })).status, 200);
  assert.equal((await postOf(bt, P)).caption, emoji);
  const { routineCaption } = await import('../lib/caption.js');
  assert.equal(routineCaption('a'.repeat(4999) + '🏀🏀'), 'a'.repeat(4999) + '🏀');
});

test('Send refuses a visual or caption that changed since the page was drawn', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const shown = await postOf(bt, P);
  // a visual made again since the page was drawn
  await done(await claim(await run(bt, 'compose', [P.id], 'bigger')), { [P.id]: { image_b64: img, caption: 'NEW caption for the new card' } });
  const send = extra => owner.call('POST', `/api/studio/batches/${bt.id}/publish`, { post_id: P.id, mode: 'now', ...extra });
  let r = await send({ visual: shown.final_image_url, caption: shown.caption, hashtags: '#r' });
  assert.deepEqual([r.status, r.body.changed], [409, true]);
  assert.match(r.body.error, /visual changed since this page was drawn/);
  // the caption saved in another window since
  const now = await postOf(bt, P);
  r = await send({ visual: now.final_image_url, caption: 'Routine caption 1', hashtags: '#r' });
  assert.deepEqual([r.status, r.body.changed], [409, true]);
  assert.match(r.body.error, /caption was changed elsewhere/);
  assert.equal((await postOf(bt, P)).publish, null, 'nothing sent');
  r = await send({ visual: now.final_image_url, caption: now.caption, hashtags: 'r' });
  assert.equal(r.status, 200);
});

test('a caption typed over one saved elsewhere since is refused, with theirs; typed over the one kept, it is saved', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  // a colleague fixes the caption; the older page types over the routine's
  assert.equal((await patch(bt, P, { caption: 'Fixed by colleague: the deal is $102M.', base_caption: 'Routine caption 1' })).status, 200);
  let r = await patch(bt, P, { caption: 'Routine caption 1 Wow.', base_caption: 'Routine caption 1' });
  assert.deepEqual([r.status, r.body.changed, r.body.now.caption], [409, true, 'Fixed by colleague: the deal is $102M.']);
  assert.match(r.body.error, /changed elsewhere/);
  assert.equal((await postOf(bt, P)).caption, 'Fixed by colleague: the deal is $102M.');
  // hashtags alike (written as the editor types them)
  assert.equal((await patch(bt, P, { hashtags: 'r, Eagles', base_hashtags: '#r' })).status, 200);
  r = await patch(bt, P, { hashtags: '#r #Birds', base_hashtags: 'r' });
  assert.deepEqual([r.status, r.body.now.hashtags], [409, ['#r', '#Eagles']]);
  // the editor chose theirs, or typed the same: saved
  assert.equal((await patch(bt, P, { caption: 'Mine after all.', base_caption: 'Fixed by colleague:  the deal is $102M.' })).status, 200);
  assert.equal((await patch(bt, P, { caption: 'Mine after all.', base_caption: 'Something older' })).status, 200);
  assert.equal((await postOf(bt, P)).caption, 'Mine after all.');
});

test('hashtags: more than 30 are refused, never cut; Send compares them as they are kept', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  const many = Array.from({ length: 32 }, (_, i) => `#t${i}`).join(' ');
  const r = await patch(bt, P, { hashtags: many });
  assert.deepEqual([r.status, r.body.error], [400, 'posts can have at most 30 hashtags (yours has 32): remove some']);
  assert.deepEqual((await postOf(bt, P)).hashtags, ['#r']);
  const thirty = Array.from({ length: 30 }, (_, i) => `t${i}`).join(', ');
  assert.equal((await patch(bt, P, { hashtags: thirty })).status, 200);
  const p = await postOf(bt, P);
  assert.equal(p.hashtags.length, 30);
  // typed without "#" and with commas: the same tags, sent
  const sent = await owner.call('POST', `/api/studio/batches/${bt.id}/publish`, { post_id: P.id, mode: 'now', visual: p.final_image_url, caption: p.caption, hashtags: thirty });
  assert.equal(sent.status, 200);
});

test('a caption edited while a re-check waits: the check it gives back says it is out of date', async () => {
  await setup();
  const findings = { factual: ['The deal is $102M, not $120M'] };
  for (const how of ['failed', 'skip']) {
    const { bt, P } = await batch(1, true);
    await done(await claim(await run(bt, 'check', [P.id])), { [P.id]: { check: findings } });
    const jc = await run(bt, 'check', [P.id]);
    assert.equal((await patch(bt, P, { caption: 'Fixed: the deal is $102M' })).status, 200);
    assert.equal((await viewed(bt, P)).check.previous.stale, true, 'shown as out of date while it waits');
    if (how === 'failed') await done(await claim(jc), {}, 'failed');
    else await owner.call('POST', `/api/studio/batches/${bt.id}/skip-check`, {});
    const p = await postOf(bt, P);
    assert.deepEqual([how, p.check.status, p.check.results, p.check.stale], [how, 'done', findings, true]);
    // the caption it checked taken back: current again
    await patch(bt, P, { caption: 'Routine caption 1' });
    assert.equal((await postOf(bt, P)).check.stale, false);
  }
});

test('a pasted link of a feed story is one post whichever was picked first: the feed\'s copy', async () => {
  await setup();
  const pasted = { id: 'u-pasted1', from_url: true, url: 'https://e.com/1', headline: 'Story 1 (pasted)', sources: [{ name: 'E', url: 'https://e.com/1' }] };
  for (const stories of [[{ id: 's1' }, pasted], [pasted, { id: 's1' }], [pasted, { id: 's2' }, pasted, { id: 's1' }]]) {
    const bt = (await owner.call('POST', '/api/studio/batches', { page: slug, stories })).body.batch;
    assert.deepEqual(bt.posts.map(p => p.story.headline).sort(), stories.length > 2 ? ['Story 1', 'Story 2'] : ['Story 1']);
  }
});

// storage writes draft.json, and its answer is lost on the way back
const draftAnswerLost = () => {
  mem.set = (k, v) => { const r = Map.prototype.set.call(mem, k, v); if (/^drafts\/[^/]+\/draft\.json$/.test(k)) { delete mem.set; throw new Error('timed out'); } return r; };
};

test('a Send whose draft was written but its answer lost says it may be with the publisher; its words no longer change until it is finished', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  draftAnswerLost();
  let r;
  try { r = await sendNow(bt, P); } finally { delete mem.set; }
  assert.equal(r.status, 503);
  assert.doesNotMatch(r.body.error, /nothing was sent/);
  assert.match(r.body.error, /may already be with the publisher, so press Send again to finish/);
  const id = (await postOf(bt, P)).publish.draft_id;
  assert.equal((await itemOf(id)).caption, 'Routine caption 1\n\n#r', 'the publisher has it');
  // the caption fixed after that is refused: the Studio never shows words that are not the ones sent
  r = await patch(bt, P, { caption: 'CORRECTED caption: the score was 3-1' });
  assert.equal(r.status, 409);
  assert.match(r.body.error, /with the publisher already/);
  assert.equal((await postOf(bt, P)).caption, 'Routine caption 1');
  assert.equal((await sendNow(bt, P)).status, 200);
  assert.deepEqual([(await postOf(bt, P)).publish.state, await queued(P)], ['approved', [id]]);
});

test('a Send finished with other words than its first approval approves those words, so the publisher sends what the Studio shows', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  draftAnswerLost();
  try { await sendNow(bt, P); } finally { delete mem.set; }
  const id = (await postOf(bt, P)).publish.draft_id;
  // (changed before such a change was refused, e.g. by the dashboard of the version before)
  await updateRecord('batches', bt.id, cur => { cur.posts[0].caption = 'CORRECTED caption'; return cur; });
  await tick();
  assert.equal((await sendNow(bt, P)).status, 200);
  assert.equal((await itemOf(id)).caption, 'CORRECTED caption\n\n#r');
  assert.equal((await loadDraft(id)).caption, 'CORRECTED caption\n\n#r');
});

test('a caption refused because the new visual brought its own says so, not another window or a colleague', async () => {
  await setup();
  const { bt, P } = await batch(1, true);
  // Regenerate visual; the editor types over the routine's caption while it is made
  await done(await claim(await run(bt, 'compose', [P.id], 'bigger')), { [P.id]: { image_b64: img, caption: 'Regenerated caption', hashtags: ['#r'] } });
  let r = await patch(bt, P, { caption: 'Routine caption 1 and mine', base_caption: 'Routine caption 1' });
  assert.equal(r.status, 409);
  assert.deepEqual([r.body.now.by, r.body.now.caption], ['visual', 'Regenerated caption']);
  assert.match(r.body.error, /new visual came with its own caption/);
  // a colleague's caption: theirs
  await patch(bt, P, { caption: 'Fixed by a colleague' });
  r = await patch(bt, P, { caption: 'Regenerated caption and mine', base_caption: 'Regenerated caption' });
  assert.deepEqual([r.status, r.body.now.by], [409, 'edit']);
  assert.match(r.body.error, /another window, or a colleague/);
});

test('Send on a post without a visual or caption says which is missing, in plain words', async () => {
  await setup();
  const { bt, P } = await batch(1);
  let r = await sendNow(bt, P);
  assert.deepEqual([r.status, r.body.error], [400, 'No visual yet: make it in step 2 (Visual) first']);
  await updateRecord('batches', bt.id, cur => { cur.posts[0].final_image_url = 'https://x.example/card.jpg'; return cur; });
  r = await sendNow(bt, P);
  assert.deepEqual([r.status, r.body.error], [400, 'No caption yet: write one first']);
});
