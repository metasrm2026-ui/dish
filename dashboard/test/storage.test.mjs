// The record store under load and failure: concurrent writers, storage that refuses reads, and how
// many storage reads each endpoint makes.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
process.env.BLOB_RETRY_MS = '1';                      // retries without the production waits
const { client } = await import('./helpers.mjs');
const db = await import('../lib/db.js');
const { put } = await import('../lib/blob.js');
const { encryptJson, decryptJson } = await import('../lib/crypto.js');
const { updateRecord, putRecord, getRecord, listRecords, resetReads } = db;

const R = { routine: true };
const mem = globalThis.__boreBlobs;
const tinyJpeg = Buffer.from('/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=', 'base64');
const CHECKPOINT = '<!DOCTYPE html><html><head><title>Vercel Security Checkpoint</title></head><body>We\'re verifying your browser</body></html>';
const sleep = ms => new Promise(r => setTimeout(r, ms));

// every storage read goes through here: counted, with a latency (production reads take 50-300 ms)
// and failures to inject (fail(url) may also do something first, while that read is under way)
const reads = { count: 0, inflight: 0, peak: 0, latency: 0, fail: null };
const inner = globalThis.fetch;
globalThis.fetch = async (url, opts) => {
  const u = String(url);
  if (!u.startsWith('mock://')) return inner(url, opts);
  reads.count++; reads.inflight++; reads.peak = Math.max(reads.peak, reads.inflight);
  try {
    if (reads.latency) await sleep(reads.latency * (0.5 + Math.random()));
    const f = reads.fail && await reads.fail(u);
    if (f) return new Response(f.body ?? CHECKPOINT, { status: f.status, headers: { 'content-type': f.type || 'text/html' } });
    return await inner(url, opts);
  } finally { reads.inflight--; }
};
const counting = async fn => { resetReads(); reads.count = 0; reads.peak = 0; const out = await fn(); return { out, count: reads.count, peak: reads.peak }; };
const versions = (coll, id) => [...mem.keys()].filter(k => k.startsWith(`db/${coll}/${id}/`)).sort();
const bump = p => p.replace(/\d{17}\.enc$/, String(parseInt(p.split('/').pop(), 10) + 1).padStart(17, '0') + '.enc');
const age = (prefix, ms) => { for (const [k, v] of mem) if (k.startsWith(prefix)) v.uploadedAt = new Date(Date.now() - ms); };
// the drafts a Studio post was sent as, and a draft's status changes
const studioDrafts = pid => [...new Set([...mem.keys()].map(k => (k.match(/^drafts\/([^/]+-studio-([^/]+))\//) || [])).filter(m => m[2] === pid).map(m => m[1]))];
const statusesOf = id => [...mem.keys()].filter(k => k.startsWith(`drafts/${id}/s-`)).sort().map(k => k.match(/-([a-z]+)\.json$/)[1]);

let owner, slug;
async function setup() {
  if (owner) return;
  owner = client();
  const robot = client();
  const boot = await robot.call('POST', '/api/routine/bootstrap', { email: 'owner@example.com', name: 'O' }, R);
  await owner.call('POST', '/api/auth/reset', { token: boot.body.setup_link.split('#reset=')[1], password: 'correct horse 42' });
  assert.equal((await owner.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  slug = (await owner.call('POST', '/api/admin/pages', { name: 'Dummy', socialpilot_login_id: '1' })).body.page.slug;
  const story = i => ({ id: `s${i}`, headline: `Story ${i}`, summary: 'S.', score: 5, published_at: Date.now(), url: `https://e.com/${i}`,
    sources: [{ name: 'E', url: `https://e.com/${i}` }], ideas: [{ title: 'A', template: 'headline', description: 'a' }, { title: 'B', template: 'quote', description: 'b' }],
    images: [{ url: `https://img.e.com/${i}.jpg`, source: 'web' }] });
  await robot.call('POST', '/api/routine/feed', { page: slug, stories: [1, 2, 3, 4, 5].map(story) }, R);
}

// a Studio post with its visual and caption, ready to send
async function readyPost(robot, n = 1) {
  const bt = (await owner.call('POST', '/api/studio/batches', { page: slug, stories: [{ id: `s${n}` }] })).body.batch;
  const P = bt.posts[0];
  await owner.call('PATCH', `/api/studio/batches/${bt.id}/posts/${P.id}`, { idea: { choice: 1 }, base_image_url: `https://img.e.com/${n}.jpg` });
  await composed(robot, bt, P, 'Ready to go');
  return { bt, P, send: () => owner.call('POST', `/api/studio/batches/${bt.id}/publish`, { post_id: P.id, mode: 'now' }) };
}
async function composed(robot, bt, P, caption) {
  const j = (await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [P.id] })).body.job;
  assert.equal((await robot.call('POST', `/api/routine/jobs/${j}`, { status: 'running' }, R)).status, 200);
  assert.equal((await robot.call('POST', `/api/routine/jobs/${j}`, { status: 'done', posts: { [P.id]: { image_b64: tinyJpeg.toString('base64'), caption } } }, R)).status, 200);
}

test('a record nobody can save (too many writers at once) says so in plain words, without its storage name', async () => {
  await putRecord('batches', 'busy-batch-1', { id: 'busy-batch-1', n: 0 });
  mem.set = function (k, v) {
    if (k.startsWith('db/batches/busy-batch-1/')) throw new Error('This blob already exists');
    return Map.prototype.set.call(this, k, v);
  };
  try { await assert.rejects(updateRecord('batches', 'busy-batch-1', cur => ({ ...cur, n: 1 })), e => e.message === 'someone else is saving this at the same moment: try again'); }
  finally { delete mem.set; }
});

test('a change made while another request saves first is redone on its copy, never lost', async () => {
  // twenty requests at once each add one to the same record: all twenty count
  reads.latency = 5;
  await putRecord('counters', 'c1', { id: 'c1', n: 0, by: [] });
  await Promise.all(Array.from({ length: 20 }, (_, i) => updateRecord('counters', 'c1', cur => ({ ...cur, n: cur.n + 1, by: [...cur.by, i] }))));
  const c = await getRecord('counters', 'c1');
  assert.equal(c.n, 20);
  assert.deepEqual([...c.by].sort((a, b) => a - b), Array.from({ length: 20 }, (_, i) => i));
  reads.latency = 0;

  // another request saves between this one's read and its write: this one reads again and keeps both
  let calls = 0;
  await updateRecord('counters', 'c1', async cur => {
    if (++calls === 1) await updateRecord('counters', 'c1', other => ({ ...other, note: 'saved meanwhile' }));
    return { ...cur, n: cur.n + 1 };
  });
  const c2 = await getRecord('counters', 'c1');
  assert.equal(calls, 2);
  assert.equal(c2.n, 21);
  assert.equal(c2.note, 'saved meanwhile');
  // nothing to change: nothing written
  const before = versions('counters', 'c1').length;
  assert.equal((await updateRecord('counters', 'c1', () => null)).n, 21);
  assert.equal(versions('counters', 'c1').length, before);

  // the put landed but its answer was lost (the client retries and gets "exists"): it counts once,
  // also when another request has already saved on top of it
  const set = mem.set;
  for (const onTop of [false, true]) {
    const nextPath = bump(versions('counters', 'c1').pop());
    mem.set = function (k, v) {
      set.call(this, k, v);
      if (k !== nextPath) return this;
      mem.set = set;
      if (onTop) set.call(this, bump(k), { ...v, buf: encryptJson({ ...decryptJson(v.buf), note: 'on top', _w: 'another' }) });
      throw new Error('socket hang up');
    };
    let applied = 0;
    const saved = await updateRecord('counters', 'c1', cur => { applied++; return { ...cur, n: cur.n + 1 }; });
    mem.set = set;
    assert.equal(applied, 1);
    assert.equal(saved.n, onTop ? 23 : 22);
    assert.equal((await getRecord('counters', 'c1')).n, onTop ? 23 : 22);
    assert.equal(versions('counters', 'c1').pop(), onTop ? bump(nextPath) : nextPath);
  }
  // ... also when the listing shows that write only after a second try of the same version was refused
  const nextPath = bump(versions('counters', 'c1').pop()), entries = mem.entries;
  mem.set = function (k, v) {
    set.call(this, k, v);
    if (k !== nextPath) return this;
    mem.set = set;
    mem.entries = function () { mem.entries = entries; return [...entries.call(this)].filter(([p]) => p !== nextPath)[Symbol.iterator](); };
    throw new Error('This blob already exists');                    // the client's own retry of the put that landed
  };
  await updateRecord('counters', 'c1', cur => ({ ...cur, n: cur.n + 1 }));
  mem.set = set; mem.entries = entries;
  assert.equal((await getRecord('counters', 'c1')).n, 24, 'counted once');
  assert.equal(versions('counters', 'c1').pop(), nextPath);
});

test('versions: after the old time-named ones, either way round; old ones pruned only when no writer can need them', async () => {
  const stamp = () => String(Date.now() * 1000).padStart(17, '0');
  const old = { access: 'public', contentType: 'application/octet-stream', addRandomSuffix: false };
  // a record the old code wrote (<µs stamp>-<rand>.enc) is read and changed
  await put(`db/legacy/r1/${stamp()}-abcd.enc`, encryptJson({ id: 'r1', v: 'old code' }), old);
  await updateRecord('legacy', 'r1', cur => ({ ...cur, v: cur.v + ', then new' }));
  assert.equal((await getRecord('legacy', 'r1')).v, 'old code, then new');
  // a dashboard rolled back to the old code still writes a version that reads as the newest
  await sleep(2);
  await put(`db/legacy/r1/${stamp()}-wxyz.enc`, encryptJson({ id: 'r1', v: 'rolled back' }), old);
  assert.equal((await getRecord('legacy', 'r1')).v, 'rolled back');
  assert.equal((await updateRecord('legacy', 'r1', cur => ({ ...cur, v: 'new again' }))).v, 'new again');
  assert.equal((await getRecord('legacy', 'r1')).v, 'new again');
  assert.equal((await listRecords('legacy')).length, 1);

  // young versions stay (a slow writer may still follow one); old ones go, keeping the newest 3
  for (let i = 0; i < 5; i++) await putRecord('pruned', 'p1', { id: 'p1', i });
  assert.equal(versions('pruned', 'p1').length, 5);
  for (const k of versions('pruned', 'p1')) mem.get(k).uploadedAt = new Date(Date.now() - 10 * 60e3);
  await putRecord('pruned', 'p1', { id: 'p1', i: 5 });
  assert.equal(versions('pruned', 'p1').length, 3);
  assert.equal((await getRecord('pruned', 'p1')).i, 5);
});

test('Studio: two job results, two editor edits and the page poll, with its own writes, at once: all survive', async () => {
  await setup();
  const robot = client();
  const { setStatus } = await import('../lib/store.js');
  const bt = (await owner.call('POST', '/api/studio/batches', { page: slug, stories: [1, 2, 3, 4, 5].map(i => ({ id: `s${i}` })) })).body.batch;
  const [A, B, C, F, G] = bt.posts;
  const path = id => `/api/studio/batches/${bt.id}/posts/${id}`;
  for (const [p, i] of [[A, 1], [B, 2], [C, 3], [F, 4], [G, 5]]) await owner.call('PATCH', path(p.id), { idea: { choice: 1 }, base_image_url: `https://img.e.com/${i}.jpg` });
  // F was sent and SocialPilot has scheduled it since: the poll writes that on the batch
  await composed(robot, bt, F, 'Caption F');
  assert.equal((await owner.call('POST', `/api/studio/batches/${bt.id}/publish`, { post_id: F.id, mode: 'now' })).status, 200);
  const [dF] = studioDrafts(F.id);
  await setStatus(dF, 'scheduled', { result: { scheduled_for: '2026-10-22T15:00:00Z' } });
  // no session could start G's job (restarted 5 times, the last one 10 minutes ago): the poll fails it
  const jG = (await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [G.id] })).body.job;
  await updateRecord('jobs', jG, cur => ({ ...cur, created_at: Date.now() - 40 * 60e3 }));
  for (let i = 0; i < 5; i++) await put(`job-nudges/${jG}/${i}.json`, '{}', { access: 'public', contentType: 'application/json', addRandomSuffix: false });
  age(`job-nudges/${jG}/`, 10 * 60e3);
  // each Make visual click starts its own session
  const jA = (await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [A.id] })).body.job;
  const jB = (await owner.call('POST', `/api/studio/batches/${bt.id}/run`, { kind: 'compose', post_ids: [B.id] })).body.job;
  assert.equal((await robot.call('POST', `/api/routine/jobs/${jA}`, { status: 'running' }, R)).status, 200);
  assert.equal((await robot.call('POST', `/api/routine/jobs/${jB}`, { status: 'running' }, R)).status, 200);
  // the poll's worst case: it has its copy of the batch and is reading F's draft while the four
  // others (racing each other) save; its own write then comes last (an old copy would undo theirs)
  reads.latency = 15;
  let reached, release;
  const atDraft = new Promise(r => { reached = r; }), go = new Promise(r => { release = r; });
  reads.fail = async u => { if (u.includes(`drafts/${dF}/`)) { reached(); await go; } return null; };
  const poll = owner.call('GET', `/api/studio/batches/${bt.id}`);
  await atDraft;
  const img = tinyJpeg.toString('base64');
  const res = await Promise.all([
    robot.call('POST', `/api/routine/jobs/${jA}`, { status: 'done', posts: { [A.id]: { image_b64: img, caption: 'Caption A', hashtags: ['#a'] } } }, R),
    robot.call('POST', `/api/routine/jobs/${jB}`, { status: 'done', posts: { [B.id]: { image_b64: img, caption: 'Caption B', hashtags: ['#b'] } } }, R),
    owner.call('PATCH', path(C.id), { caption: 'Editor typed this for C' }),
    owner.call('PATCH', path(C.id), { idea: { choice: 2 } }),
  ]);
  release();
  res.push(await poll);
  reads.latency = 0; reads.fail = null;
  assert.deepEqual(res.map(r => r.status), [200, 200, 200, 200, 200]);
  const after = Object.fromEntries((await getRecord('batches', bt.id)).posts.map(p => [p.id, p]));
  assert.ok(after[A.id].final_image_url && after[B.id].final_image_url, 'both visuals kept');
  assert.deepEqual([after[A.id].caption, after[B.id].caption], ['Caption A', 'Caption B']);
  assert.deepEqual([after[A.id].job.status, after[B.id].job.status], ['done', 'done'], 'no job line left "running"');
  assert.equal(after[C.id].caption, 'Editor typed this for C', 'the editor\'s edit kept');
  assert.equal(after[C.id].idea.title, 'B', 'and their other edit');
  assert.deepEqual([after[F.id].publish.state, after[F.id].publish.scheduled_for], ['scheduled', '2026-10-22T15:00:00Z'], 'the poll\'s publish update kept');
  assert.equal(after[G.id].job.status, 'failed', 'the poll\'s failed job kept');
  assert.match(after[G.id].job.error, /after 5 restarts/);
  assert.equal((await getRecord('jobs', jG)).status, 'failed');
  // and no route writes a batch or a job any other way: no putRecord, no change that ignores the
  // newest copy (a new batch or job is made with createRecord)
  const dir = new URL('../', import.meta.url);
  for (const f of [...readdirSync(new URL('routes/', dir)).map(n => `routes/${n}`), ...readdirSync(new URL('lib/', dir)).map(n => `lib/${n}`)]) {
    const src = readFileSync(new URL(f, dir), 'utf8');
    assert.doesNotMatch(src, /putRecord\(\s*'(batches|jobs)'/, `${f} writes batches/jobs with updateRecord`);
    assert.doesNotMatch(src, /updateRecord\(\s*'(batches|jobs)'\s*,[^,]+,\s*(\(\s*\)|_)\s*=>/, `${f}: a batch/job change is made on the newest copy`);
  }
});

test('Studio Send while storage refuses reads: nothing saved before the card is read, never sent twice', async () => {
  await setup();
  const robot = client();
  const one = await readyPost(robot, 1);
  // the editor's page has just read the batch, the page and the user, so the Send's record reads
  // come from memory: what it reads from storage is the page's queued posts (for the earliest slot
  // 45 minutes from each) and the card, all before anything is saved
  resetReads();
  await owner.call('GET', `/api/studio/batches/${one.bt.id}`);
  await owner.call('GET', `/api/studio/feed?page=${slug}`);
  const asked = [];
  reads.fail = u => { asked.push(u); return { status: 403 }; };
  let r = await one.send();
  reads.fail = null; resetReads();
  assert.equal(r.status, 503);
  assert.match(r.body.error, /security check.*nothing was changed/);
  assert.ok(asked.length && asked.every(u => u.includes('/card/') || /\/drafts\/[^/]+\/(draft\.json|s-)/.test(u)), 'only the card and the queued posts were read');
  assert.deepEqual(studioDrafts(one.P.id), [], 'no draft, so no security-check page sent as the image');
  // a card that comes back as something other than an image: refused the same way
  reads.fail = u => (u.includes('/card/') ? { status: 200, body: '<html><body>An error occurred</body></html>' } : null);
  r = await one.send();
  reads.fail = null;
  assert.equal(r.status, 503);
  assert.match(r.body.error, /nothing was sent/);
  assert.deepEqual(studioDrafts(one.P.id), []);
  assert.equal((await getRecord('batches', one.bt.id)).posts[0].publish, null);

  // just after the draft was approved, the page's poll saves the batch and storage starts refusing
  // reads (so marking the post sent fails): the editor is not told that nothing changed, and
  // pressing Send again finishes the same draft instead of approving it, or another one, again
  const refuseAfterApproval = ({ bt, P }) => {
    mem.set = function (k, v) {
      Map.prototype.set.call(this, k, v);
      if (!(k.includes(`-studio-${P.id}/s-`) && k.endsWith('-approved.json'))) return this;
      delete mem.set;
      const last = versions('batches', bt.id).pop();
      this.set(bump(last), { ...this.get(last), uploadedAt: new Date() });
      reads.fail = u => (u.includes('db/batches/') ? { status: 403 } : null);
      return this;
    };
  };
  refuseAfterApproval(one);
  r = await one.send();
  delete mem.set; reads.fail = null; resetReads();
  assert.equal(r.status, 503);
  assert.doesNotMatch(r.body.error, /nothing was changed/);
  assert.match(r.body.error, /check whether it went through/);
  const [d1] = studioDrafts(one.P.id);
  assert.deepEqual(statusesOf(d1), ['approved']);
  r = await one.send();
  assert.equal(r.status, 200);
  assert.deepEqual(studioDrafts(one.P.id), [d1], 'one draft');
  assert.deepEqual(statusesOf(d1), ['approved'], 'approved once');
  assert.deepEqual([r.body.batch.posts[0].publish.state, r.body.batch.posts[0].publish.draft_id], ['approved', d1]);
  assert.equal((await one.send()).status, 409);

  // the same, but the page polls before the editor presses Send again: the poll finds the draft
  // approved, and Send says it was sent
  const two = await readyPost(robot, 2);
  refuseAfterApproval(two);
  assert.equal((await two.send()).status, 503);
  delete mem.set; reads.fail = null; resetReads();
  const [d2] = studioDrafts(two.P.id);
  assert.equal((await owner.call('GET', `/api/studio/batches/${two.bt.id}`)).body.batch.posts[0].publish.state, 'approved');
  assert.equal((await two.send()).status, 409);
  assert.deepEqual(studioDrafts(two.P.id), [d2]);

  // a double click: one draft
  const three = await readyPost(robot, 3);
  reads.latency = 5;
  const both = await Promise.all([three.send(), three.send()]);
  reads.latency = 0;
  assert.ok(both.every(x => [200, 409].includes(x.status)), JSON.stringify(both.map(x => x.status)));
  assert.equal(studioDrafts(three.P.id).length, 1);
  assert.equal((await getRecord('batches', three.bt.id)).posts[0].publish.state, 'approved');
});

test('Studio Send whose approval write is refused: its draft never shows as an hourly post waiting for review', async () => {
  const { groupDrafts, listAll } = await import('../lib/store.js');
  await setup();
  const one = await readyPost(client(), 4);
  mem.set = function (k, v) {
    if (k.includes(`-studio-${one.P.id}/s-`) && k.endsWith('-approved.json')) { delete mem.set; throw new Error('injected put failure'); }
    return Map.prototype.set.call(this, k, v);
  };
  const r = await one.send();
  delete mem.set;
  assert.notEqual(r.status, 200);
  // the review page's Waiting tab lists drafts with no status file as pending: there is none
  assert.deepEqual(groupDrafts(await listAll()).filter(d => d.id.endsWith(`-studio-${one.P.id}`)).map(d => d.status), []);
  // Send again makes and approves it once
  assert.equal((await one.send()).status, 200);
  const [d] = studioDrafts(one.P.id);
  assert.deepEqual(statusesOf(d), ['approved']);
  assert.deepEqual(groupDrafts(await listAll()).filter(x => x.id === d).map(x => x.status), ['approved']);

  // its draft.json refused after the approval: plain words, and the next Send does not approve it again
  const two = await readyPost(client(), 5);
  mem.set = function (k, v) {
    if (k.includes(`-studio-${two.P.id}/draft.json`)) { delete mem.set; throw new Error('Vercel Blob: injected put failure'); }
    return Map.prototype.set.call(this, k, v);
  };
  const at = Date.now() + 3 * 3600e3;
  const schedule = () => owner.call('POST', `/api/studio/batches/${two.bt.id}/publish`, { post_id: two.P.id, mode: 'schedule', at });
  const r2 = await schedule();
  delete mem.set;
  assert.deepEqual([r2.status, r2.body.error], [503, 'storage did not save the post: nothing was sent, press Send again']);
  assert.equal((await schedule()).status, 200);
  const [d2] = studioDrafts(two.P.id);
  assert.deepEqual(statusesOf(d2), ['approved']);
});

test('Studio: a photo or idea pick that the newest copy no longer has is refused, not silently changed', async () => {
  await setup();
  const bt = (await owner.call('POST', '/api/studio/batches', { page: slug, stories: [{ id: 's1' }] })).body.batch;
  const P = bt.posts[0];
  const path = `/api/studio/batches/${bt.id}/posts/${P.id}`;
  await updateRecord('batches', bt.id, cur => { cur.posts[0].extra_images = [{ url: 'https://img.e.com/x1.jpg', source: 'web' }]; return cur; });
  // while the PATCH reads the batch, something else saves a change to it first
  const meanwhile = change => {
    let done = false;
    reads.fail = async u => {
      if (!done && u.includes(`db/batches/${bt.id}/`)) { done = true; await updateRecord('batches', bt.id, cur => { change(cur.posts[0]); return cur; }); }
      return null;
    };
  };
  // a new image search no longer has the photo the editor clicked
  meanwhile(p => { p.extra_images = [{ url: 'https://img.e.com/x2.jpg', source: 'web' }]; });
  resetReads();
  let r = await owner.call('PATCH', path, { base_image_url: 'https://img.e.com/x1.jpg' });
  reads.fail = null;
  assert.equal(r.status, 409);
  assert.match(r.body.error, /photos changed/);
  assert.equal((await getRecord('batches', bt.id)).posts[0].base_image, null);
  // new ideas arrived: "idea 1" is no longer the idea the editor read
  meanwhile(p => { p.story.ideas = [{ title: 'New', template: 'quote', description: 'new' }, { title: 'Newer', template: 'quote', description: 'newer' }]; });
  resetReads();
  r = await owner.call('PATCH', path, { idea: { choice: 1 } });
  reads.fail = null;
  assert.equal(r.status, 409);
  assert.match(r.body.error, /ideas changed/);
  assert.equal((await getRecord('batches', bt.id)).posts[0].idea, null);
  // nothing changed meanwhile: saved
  r = await owner.call('PATCH', path, { idea: { choice: 2 }, base_image_url: 'https://img.e.com/x2.jpg' });
  assert.equal(r.status, 200);
  assert.deepEqual([r.body.batch.posts[0].idea.title, r.body.batch.posts[0].base_image.url], ['Newer', 'https://img.e.com/x2.jpg']);
});

test('storage that refuses reads: retried, then a clear 503; a checkpoint page is never taken for data', async () => {
  await setup();
  const robot = client();
  await putRecord('jobs', 'refused-job', { id: 'refused-job', kind: 'ideas', status: 'done', post_ids: [], created_at: 0 });
  // a refusal that passes: retried, the request works
  let refused = 0;
  reads.fail = u => (u.includes('db/users/') && refused++ < 2 ? { status: 403 } : null);
  resetReads();
  assert.equal((await owner.call('GET', '/api/studio/pages')).status, 200);
  assert.equal(refused, 3);
  // a security check that lasts: 503 with a message, for editors and for the routines
  reads.fail = () => ({ status: 403 });
  resetReads();
  const r = await owner.call('GET', '/api/studio/pages');
  assert.equal(r.status, 503);
  assert.match(r.body.error, /security check.*: nothing was changed/);
  // this instance then stops reading for a moment (more reads only prolong a challenge)
  reads.count = 0;
  assert.equal((await robot.call('GET', '/api/routine/jobs', undefined, R)).status, 503);
  assert.equal(reads.count, 0, 'no reads while paused');
  reads.fail = null;
  const { saveDraft } = await import('../lib/store.js');
  await saveDraft({ id: `dummy-${new Date().toISOString().replace(/\D/g, '').slice(0, 12)}-refused`, page: 'dummy', caption: 'x' }, tinyJpeg);
  reads.fail = () => ({ status: 403 });
  resetReads();
  assert.equal((await robot.call('GET', '/api/drafts?status=all', undefined, R)).status, 503, 'routes with their own error handling too');
  // a checkpoint page served with 200 is a refusal too, not a record
  reads.fail = () => ({ status: 200 });
  resetReads();
  assert.equal((await robot.call('GET', '/api/routine/jobs', undefined, R)).status, 503);
  reads.fail = () => ({ status: 429, body: 'slow down' });
  resetReads();
  const busy = await robot.call('GET', '/api/routine/jobs', undefined, R);
  assert.equal(busy.status, 503);
  assert.match(busy.body.error, /not answering \(429\)/);
  reads.fail = null;
  resetReads();
  assert.equal((await robot.call('GET', '/api/routine/jobs', undefined, R)).status, 200);

  // a broken answer that is not a checkpoint page fails that read, and is not kept: the next read
  // fetches the record again
  let broken = 0;
  reads.fail = u => (u.includes('db/users/') && broken++ === 0 ? { status: 200, body: 'garbage', type: 'application/octet-stream' } : null);
  resetReads();
  assert.equal((await owner.call('GET', '/api/studio/pages')).status, 500);
  assert.equal((await owner.call('GET', '/api/studio/pages')).status, 200);
  reads.fail = null;

  // forty reads at once (a list read all together) under a lasting check: about one request each,
  // not four, and every one of them answers "storage is refusing"
  for (let i = 0; i < 40; i++) await put(`burst/${i}.json`, '{}', { access: 'public', contentType: 'application/json', addRandomSuffix: false });
  reads.fail = () => ({ status: 403 });
  resetReads(); reads.count = 0;
  const all = await Promise.allSettled(Array.from({ length: 40 }, (_, i) => db.fetchBlob(`mock://burst/${i}.json`)));
  reads.fail = null; resetReads();
  assert.ok(all.every(x => x.status === 'rejected' && x.reason instanceof db.StorageChallenged));
  assert.ok(reads.count <= 48, `${reads.count} requests for 40 reads`);
  // a refusal or two that pass are still retried
  let twice = 0;
  reads.fail = () => (twice++ < 2 ? { status: 403 } : null);
  assert.deepEqual(JSON.parse(await db.fetchBlob('mock://burst/0.json')), {});
  reads.fail = null;
});

test('state: served only when it is the saved gzip database; 24 copies kept', async () => {
  const robot = client();
  const db1 = gzipSync(Buffer.from('SQLite format 3\0 the saved database'));
  assert.equal((await robot.call('GET', '/api/state', undefined, R)).status, 404);
  // the storage outage of Oct 21: a security-check page where the state should be
  await put('state/bore-1.db.gz', CHECKPOINT, { access: 'public', contentType: 'application/gzip', addRandomSuffix: false });
  let g = await robot.call('GET', '/api/state', undefined, R);
  assert.equal(g.status, 503);
  assert.match(g.body.error, /state unreadable/);
  // anything else that is not the gzipped database (another error page, a database stored raw)
  for (const body of ['<html><body>An error occurred</body></html>', 'SQLite format 3\0 stored without gzip']) {
    for (const k of [...mem.keys()].filter(k => k.startsWith('state/'))) mem.delete(k);
    await put('state/bore-1.db.gz', body, { access: 'public', contentType: 'application/gzip', addRandomSuffix: false });
    resetReads();                                     // (the checkpoint page paused reads for a moment)
    g = await robot.call('GET', '/api/state', undefined, R);
    assert.equal(g.status, 503, body);
    assert.match(g.body.error, /state unreadable: storage returned something that is not the saved database/);
  }
  // a real copy, but storage refuses to hand it over
  for (const k of [...mem.keys()].filter(k => k.startsWith('state/'))) mem.delete(k);
  await put('state/bore-2.db.gz', db1, { access: 'public', contentType: 'application/gzip', addRandomSuffix: false });
  reads.fail = u => (u.includes('state/') ? { status: 403 } : null);
  resetReads();
  g = await robot.call('GET', '/api/state', undefined, R);
  assert.equal(g.status, 503);
  assert.match(g.body.error, /state unreadable: storage is running a security check/);
  reads.fail = null;
  resetReads();
  g = await robot.call('GET', '/api/state', undefined, R);
  assert.equal(g.status, 200);
  assert.deepEqual(Buffer.from(g.body), db1);
  // a push must be the gzipped database; 24 copies are kept
  assert.equal((await pushRaw(Buffer.from('<html>not a database</html>'))).status, 400);
  for (let i = 0; i < 26; i++) {
    assert.equal((await pushRaw(gzipSync(Buffer.from(`SQLite format 3\0 copy ${i}`)))).status, 200);
    await sleep(1);
  }
  assert.equal([...mem.keys()].filter(k => k.startsWith('state/')).length, 24);
});

// PUT /api/state with a raw body (the API takes the routine's gzip as is)
async function pushRaw(body) {
  const { default: main } = await import('../api/main.js');
  const out = { status: 200, body: null };
  const res = { headersSent: false, setHeader() {}, status(s) { out.status = s; return res; },
                json(o) { out.body = o; res.headersSent = true; return res; }, send(b) { out.body = b; return res; }, end() { return res; } };
  const req = { method: 'PUT', query: { route: 'state' }, url: '/api/state',
                headers: { 'content-type': 'application/gzip', authorization: `Bearer ${process.env.ROUTINE_SECRET}` },
                async *[Symbol.asyncIterator]() { yield body; } };
  await main(req, res);
  return out;
}

test('storage reads per request: bounded, at most 8 at once, and an unchanged record is read once', async () => {
  await setup();
  const robot = client();
  const { saveDraft, setStatus, groupDrafts, listAll } = await import('../lib/store.js');
  const at = new Date().toISOString().replace(/\D/g, '').slice(0, 12);
  const ids = Array.from({ length: 40 }, (_, i) => `dummy-${at}-budget-${i}`);
  for (const id of ids) { await saveDraft({ id, page: 'dummy', page_name: 'Dummy', caption: 'x' }, tinyJpeg); await setStatus(id, 'pending', {}); }
  age('drafts/', 11 * 60e3);                         // made a while ago (draft.json is kept once it has settled)
  reads.latency = 3;
  // the routine's repeat check: two reads per draft at most, 8 at a time; the second time from memory
  let r = await counting(() => robot.call('GET', '/api/drafts?status=all&limit=300', undefined, R));
  assert.equal(r.out.status, 200);
  const n = r.out.body.drafts.length;
  assert.ok(n >= 40);
  assert.ok(r.count <= 2 * n, `${r.count} reads for ${n} drafts`);
  assert.ok(r.peak <= 8, `${r.peak} reads at once`);
  assert.equal(r.out.body.truncated, undefined);
  // a repeat reads nothing that did not change
  reads.count = 0;
  await robot.call('GET', '/api/drafts?status=all&limit=300', undefined, R);
  assert.equal(reads.count, 0, 'an unchanged list is read from memory');
  await setStatus(ids[0], 'approved', { caption: 'y' });
  reads.count = 0;
  await robot.call('GET', '/api/drafts?status=all&limit=300', undefined, R);
  assert.equal(reads.count, 1, 'only the new status file');
  // a draft.json written again (a retried upload) is read from storage until it has settled: just
  // after the write the CDN may still hand out the old copy
  await saveDraft({ id: ids[1], page: 'dummy', page_name: 'Dummy', caption: 'x again' }, tinyJpeg);
  for (const want of [1, 1]) {
    reads.count = 0;
    await robot.call('GET', '/api/drafts?status=all&limit=300', undefined, R);
    assert.equal(reads.count, want, 'the new draft.json, every time');
  }
  age(`drafts/${ids[1]}/draft.json`, 11 * 60e3);
  for (const want of [1, 0]) {
    reads.count = 0;
    await robot.call('GET', '/api/drafts?status=all&limit=300', undefined, R);
    assert.equal(reads.count, want, 'settled: kept');
  }
  // a cut list says so
  const cut = (await robot.call('GET', '/api/drafts?status=all&limit=10', undefined, R)).body;
  assert.equal(cut.drafts.length, 10);
  assert.equal(cut.truncated, true);
  assert.ok(cut.total >= 40);

  // Studio jobs and the run log: one read per record or entry, 8 at a time, then from memory
  for (let i = 0; i < 30; i++) await putRecord('jobs', `budget-job-${i}`, { id: `budget-job-${i}`, kind: 'ideas', status: 'done', post_ids: [], created_at: i });
  for (let i = 0; i < 30; i++) await robot.call('POST', '/api/routine/runlog', { routine: 'test', step: `s${i}` }, R);
  // the Post step's hint reads the page's queued posts each time it is drawn, and what the page
  // posted lately only when asked (its first draw)
  for (const id of ids.slice(20, 32)) await setStatus(id, 'posted', {});
  const queued = groupDrafts(await listAll()).filter(d => d.page === slug && ['approved', 'publishing', 'scheduled'].includes(d.status)).length;
  for (const [path, budget] of [['/api/routine/jobs?recent=1', 40], ['/api/routine/runlog', 40], ['/api/routine/jobs', 40], ['/api/studio/pages', 5],
                                [`/api/studio/schedule-hint?page=${slug}`, 3 * queued], [`/api/studio/schedule-hint?page=${slug}&recent=1`, 3 * queued + 12]]) {
    const c = path.startsWith('/api/studio') ? owner : robot;
    r = await counting(() => c.call('GET', path, undefined, path.startsWith('/api/studio') ? {} : R));
    assert.equal(r.out.status, 200, path);
    assert.ok(r.count <= budget, `${path}: ${r.count} reads (budget ${budget})`);
    assert.ok(r.peak <= 8, `${path}: ${r.peak} at once`);
    reads.count = 0;
    await c.call('GET', path, undefined, path.startsWith('/api/studio') ? {} : R);
    assert.equal(reads.count, 0, `${path}: unchanged, read from memory`);
  }
  reads.latency = 0;
});

test('the Studio home list reads each sent post\'s draft listing once, the drafts themselves only when one moved on', async () => {
  await setup();
  const robot = client();
  const home = () => counting(() => owner.call('GET', `/api/studio/batches?page=${slug}`));
  const before = await home();
  const sent = [];
  for (let n = 1; n <= 6; n++) { const x = await readyPost(robot, (n % 5) + 1); assert.equal((await x.send()).status, 200); sent.push(x); }
  reads.latency = 3;
  for (let i = 0; i < 2; i++) {
    const r = await home();
    assert.equal(r.out.status, 200);
    assert.ok(r.count <= before.count + 6 + 6, `${r.count} reads for 6 sent posts (${before.count} with none)`);
    assert.ok(r.peak <= 8, `${r.peak} at once`);
  }
  reads.latency = 0;
  // one failed: read, shown and saved
  const { setStatus } = await import('../lib/store.js');
  const draft = (await getRecord('batches', sent[0].bt.id)).posts[0].publish.draft_id;
  await setStatus(draft, 'failed', { error: 'the Facebook page needs to be reconnected' });
  const l = (await home()).out.body.batches.find(x => x.id === sent[0].bt.id);
  assert.deepEqual([l.done, l.posts.map(p => p.status)], [false, ['failed']]);
});
