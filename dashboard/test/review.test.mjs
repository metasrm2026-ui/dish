// What the review page (public/review.html) relies on: one verdict per post when editors decide it
// at the same moment, retries with the caption shown, the lists and counts its tabs show, failed
// redos and links, needs-a-person notes marked done.
import test from 'node:test';
import assert from 'node:assert/strict';
import { client, adminPage } from './helpers.mjs';

const R = { routine: true };
const tinyJpeg = Buffer.from('/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=', 'base64');
const tick = () => new Promise(r => setTimeout(r, 5));      // status records are named by the millisecond
const { saveDraft, setStatus } = await import('../lib/store.js');
const stamp = (t = Date.now()) => new Date(t).toISOString().replace(/\D/g, '').slice(0, 12);
const robot = client(), owner = client(), editor = client();

async function signIn(c, link, email, pw) {
  await c.call('POST', '/api/auth/reset', { token: link.split('#reset=')[1], password: pw });
  assert.equal((await c.call('POST', '/api/auth/login', { email, password: pw })).status, 200);
}
async function draft(slug, status = 'pending', body = {}, made = Date.now()) {
  const id = `dummy-sports-${stamp(made)}-${slug}`;
  await saveDraft({ id, page: 'dummy-sports', page_name: 'Dummy', caption: `Caption of ${slug}` }, tinyJpeg);
  await setStatus(id, status, body);
  await tick();
  return id;
}
const status = async id => (await robot.call('GET', '/api/drafts?status=all', undefined, R)).body.drafts.find(d => d.id === id);

test.before(async () => {
  const boot = await robot.call('POST', '/api/routine/bootstrap', { email: 'owner@example.com', name: 'Owner' }, R);
  await signIn(owner, boot.body.setup_link, 'owner@example.com', 'correct horse 42');
  await adminPage(owner, 'Dummy Sports', '999', { fb_page_id: '123' });
  const inv = await owner.call('POST', '/api/admin/users', { email: 'ed@example.com', role: 'editor', workspace: owner.ws, pages: ['dummy-sports'] });
  await signIn(editor, inv.body.setup_link, 'ed@example.com', 'editor pass 77');
});

test('two editors deciding one post at the same moment: one wins, the other is told who did', async () => {
  // Not okay and Okay together: the rejected post must never go out with both told it worked
  const a = await draft('race-reject');
  const [no, ok] = await Promise.all([
    editor.call('POST', '/api/decide', { id: a, decision: 'reject', reason: 'Wrong / unverified facts' }),
    owner.call('POST', '/api/decide', { id: a, decision: 'approve', caption: 'Caption of race-reject' })]);
  assert.deepEqual([no.status, ok.status].sort(), [200, 409], JSON.stringify([no.body, ok.body]));
  const lost = no.status === 409 ? no : ok, won = no.status === 409 ? 'approved' : 'rejected';
  // (the status check answers "already <status>" when the winner's write landed before it looked)
  assert.match(lost.body.error, won === 'approved' ? /^already approved( by owner@example\.com just now)?$/ : /^already (marked Not okay by ed@example\.com just now|rejected)$/);
  assert.equal((await status(a)).status, won);
  const verdicts = (await robot.call('GET', '/api/routine/reviews?days=1', undefined, R)).body.entries.filter(e => e.draft_id === a);
  assert.equal(verdicts.length, 1, 'one verdict in the ledger');

  // two Okays with different fixes: one caption, and the other editor knows theirs did not stand
  const b = await draft('race-okay');
  const two = await Promise.all([owner.call('POST', '/api/decide', { id: b, decision: 'approve', caption: 'Admin fix' }),
                                 editor.call('POST', '/api/decide', { id: b, decision: 'approve', caption: 'Editor fix' })]);
  assert.deepEqual(two.map(r => r.status).sort(), [200, 409]);
  assert.equal((await status(b)).status_info.caption, two[0].status === 200 ? 'Admin fix' : 'Editor fix');

  // Regenerate against Okay, and Clear selected against Okay
  const c = await draft('race-redo'), d = await draft('race-clear');
  const [redo, okc] = await Promise.all([editor.call('POST', '/api/feedback', { id: c, feedback: 'use the other photo' }),
                                          owner.call('POST', '/api/decide', { id: c, decision: 'approve' })]);
  assert.deepEqual([redo.status, okc.status].sort(), [200, 409]);
  assert.equal((await status(c)).status, redo.status === 200 ? 'regenerate' : 'approved');
  const [clr, okd] = await Promise.all([editor.call('POST', '/api/drafts?action=clear', { ids: [d] }),
                                         owner.call('POST', '/api/decide', { id: d, decision: 'approve' })]);
  assert.equal(clr.body.cleared + (okd.status === 200 ? 1 : 0), 1, JSON.stringify([clr.body, okd.body]));
  assert.equal((await status(d)).status, okd.status === 200 ? 'approved' : 'cleared');

  // a status written later (back to Waiting) is a new state: it can be decided again
  await setStatus(a, 'pending', { restored_by: 'test' });
  assert.equal((await editor.call('POST', '/api/decide', { id: a, decision: 'reject', reason: 'Duplicate' })).status, 200);
});

test('Retry selected sends the caption the editor fixed in the Failed tab', async () => {
  const a = await draft('failed-fixed', 'approved', { caption: 'Caption with a blocked link' });
  await setStatus(a, 'failed', { error: 'caption contains a blocked link' }); await tick();
  const b = await draft('failed-same', 'approved', { caption: 'Approved caption' });
  await setStatus(b, 'failed', { error: 'SocialPilot said no' }); await tick();
  const r = await editor.call('POST', '/api/decide', { ids: [a, b], decision: 'retry', captions: { [a]: '  Fixed caption without the link ' } });
  assert.equal(r.body.retried, 2, JSON.stringify(r.body));
  assert.equal((await status(a)).status_info.caption, 'Fixed caption without the link');
  assert.equal((await status(b)).status_info.caption, 'Approved caption', 'not edited: its approved caption');
});

test('Queued lists the posts the publisher is scheduling; the badges count every post their tab lists', async () => {
  const a = await draft('queued-approved', 'approved', { caption: 'x' });
  const b = await draft('queued-publishing', 'approved', { caption: 'x' });
  await setStatus(b, 'publishing', {}); await tick();
  const queued = (await editor.call('GET', '/api/drafts?status=approved')).body.drafts.map(d => d.id);
  assert.ok(queued.includes(a) && queued.includes(b), JSON.stringify(queued));
  // a post made before the day's start still waits in the list: the badge counts it too
  const old = await draft('waiting-from-yesterday', 'pending', {}, Date.now() - 2 * 3600e3);
  const s = (await editor.call('GET', `/api/stats?since=${Math.floor(Date.now() / 1000) - 3600}`)).body;
  const listed = (await editor.call('GET', '/api/drafts?status=pending')).body.drafts;
  assert.ok(listed.some(d => d.id === old));
  assert.equal(s.totals.pending, listed.length);
  assert.ok((s.counts['dummy-sports'].pending || 0) < listed.length, 'the today tiles still count today only');
});

test('a failed redo comes back to Waiting with the caption the editor had fixed', async () => {
  const a = await draft('redo-caption');
  assert.equal((await editor.call('POST', '/api/feedback', { id: a, feedback: 'both faces please', caption: 'Editor fixed caption: 3-2 final' })).status, 200);
  await tick();
  assert.equal((await robot.call('POST', '/api/feedback', { id: a, action: 'failed', error: 'no photo with both faces' }, R)).status, 200);
  const d = await status(a);
  assert.equal(d.status, 'pending');
  assert.equal(d.status_info.caption, 'Editor fixed caption: 3-2 final');
  assert.equal(d.status_info.regen_error, 'no photo with both faces');
});

test('Try again only puts back a link that failed: a link already made is not made twice', async () => {
  const add = await editor.call('POST', '/api/links', { url: 'https://example.com/story-1', page: 'dummy-sports' });
  const id = add.body.link.id;
  assert.equal((await editor.call('POST', '/api/links', { id, action: 'retry' })).status, 409, 'still waiting');
  await tick();
  await robot.call('POST', '/api/links', { id, action: 'failed', error: 'page not readable' }, R); await tick();
  assert.equal((await editor.call('POST', '/api/links', { id, action: 'retry' })).status, 200);
  await tick();
  await draft('from-link', 'pending');
  await robot.call('POST', '/api/drafts', { id: `dummy-sports-${stamp()}-made-from-link`, page: 'dummy-sports', caption: 'c',
                                            image_b64: tinyJpeg.toString('base64'), link_request: id }, R);
  await tick();
  const again = await editor.call('POST', '/api/links', { id, action: 'retry' });         // a second editor's old page
  assert.equal(again.status, 409);
  assert.equal(again.body.error, 'already made: the post is in Waiting');
  assert.ok(!(await robot.call('GET', '/api/links?status=waiting', undefined, R)).body.links.some(l => l.id === id));
  assert.equal((await editor.call('POST', '/api/links', { id: 'no-such-link', action: 'retry' })).status, 404);
});

test('a failure reported without a reason says so', async () => {
  const a = await draft('failed-no-reason', 'approved', { caption: 'x' });
  await robot.call('POST', '/api/result', { id: a, status: 'publishing' }, R); await tick();
  assert.equal((await robot.call('POST', '/api/result', { id: a, status: 'failed' }, R)).status, 200);
  assert.equal((await status(a)).status_info.error, 'the publisher gave no reason');
});

test('marking a needs-a-person note done takes it out of the health banner at once', async () => {
  const a = await draft('double-copy', 'approved', { caption: 'x' });
  await robot.call('POST', '/api/result', { id: a, status: 'publishing' }, R); await tick();
  await robot.call('POST', '/api/result', { id: a, status: 'scheduled', scheduled_for: '2026-10-21T13:00:00Z', post_id: 'cp' }, R);
  await robot.call('POST', '/api/result', { id: a, status: 'needs_action', error: '2 copies are queued in SocialPilot: delete one' }, R);
  const { health } = await import('../routes/health.js');
  await health({ fresh: true });
  const codes = async () => (await editor.call('GET', '/api/health')).body.reasons.map(r => r.code);
  assert.ok((await codes()).includes('publisher_needs_person'));
  assert.equal((await editor.call('POST', '/api/decide', { id: a, decision: 'done' })).status, 200);
  assert.ok(!(await codes()).includes('publisher_needs_person'));
});

test('a decision whose status write failed does not lock the post: it can be decided again', async () => {
  const mem = globalThis.__boreBlobs, realSet = mem.set;
  let failing = null;                 // the post whose next status write storage refuses
  mem.set = function (k, v) { if (failing && k.startsWith(`drafts/${failing}/s-`)) { failing = null; throw new Error('storage unavailable'); } return realSet.call(this, k, v); };
  try {
    for (const [slug, call, after] of [
      ['lock-approve', id => editor.call('POST', '/api/decide', { id, decision: 'approve', caption: 'x' }), 'approved'],
      ['lock-reject', id => editor.call('POST', '/api/decide', { id, decision: 'reject', reason: 'Duplicate' }), 'rejected'],
      ['lock-redo', id => editor.call('POST', '/api/feedback', { id, feedback: 'other photo' }), 'regenerate'],
      ['lock-clear', id => editor.call('POST', '/api/drafts?action=clear', { ids: [id] }), 'cleared']]) {
      const id = await draft(slug);
      failing = id;
      const first = await call(id);
      assert.ok(first.status >= 500, `${slug}: ${JSON.stringify(first.body)}`);
      const again = await call(id);
      assert.equal(again.status, 200, `${slug}: ${JSON.stringify(again.body)}`);
      if (slug === 'lock-clear') assert.deepEqual(again.body, { ok: true, cleared: 1, ids: [id] });
      assert.equal((await status(id)).status, after, slug);
    }
    // the midnight clean-up, its write refused once, clears the post at the next look
    const old = await draft('lock-midnight', 'pending', {}, Date.now() - 72 * 3600e3);
    failing = old;
    await robot.call('GET', '/api/drafts?status=all', undefined, R);
    assert.equal((await status(old)).status, 'cleared');
  } finally { mem.set = realSet; }
});

test('a claim abandoned by a request that died is taken over; a lost claim says "just now" only when it is recent', async () => {
  const { decidedBy } = await import('../lib/store.js');
  const a = await draft('claim-abandoned');
  const at = (await status(a)).status_info.at;
  globalThis.__boreBlobs.set(`decisions/${a}/${at}.json`, { buf: Buffer.from(JSON.stringify({ by: 'owner@example.com', decision: 'approve', at: Date.now() - 5 * 60e3 })),
                                                          contentType: 'application/json', uploadedAt: new Date() });
  assert.equal((await editor.call('POST', '/api/decide', { id: a, decision: 'reject', reason: 'Duplicate' })).status, 200);
  assert.equal((await status(a)).status, 'rejected');
  assert.equal(decidedBy({ by: 'ed@example.com', decision: 'approve', at: Date.now() - 3 * 86400e3 }), 'already approved by ed@example.com');
  assert.equal(decidedBy({ by: 'ed@example.com', decision: 'approve', at: Date.now() }), 'already approved by ed@example.com just now');
});

test('Not okay, Clear and Retry for several posts name the posts they applied to (another editor had one)', async () => {
  const { claimDecision } = await import('../lib/store.js');
  const a = await draft('bulk-mine'), b = await draft('bulk-taken'), c = await draft('clear-mine'), d = await draft('clear-taken');
  for (const id of [b, d]) assert.equal(await claimDecision(id, (await status(id)).status_info.at, 'owner@example.com', 'approve'), null);
  const no = await editor.call('POST', '/api/decide', { ids: [a, b], decision: 'reject_many', reason: 'Duplicate' });
  assert.deepEqual([no.body.rejected, no.body.ids], [1, [a]]);
  const clr = await editor.call('POST', '/api/drafts?action=clear', { ids: [c, d] });
  assert.deepEqual([clr.body.cleared, clr.body.ids], [1, [c]]);
  const f = await draft('retry-applied', 'approved', { caption: 'x' });
  await setStatus(f, 'failed', { error: 'SocialPilot said no' }); await tick();
  const r = await editor.call('POST', '/api/decide', { ids: [f, a], decision: 'retry' });
  assert.deepEqual(r.body.ids, [f], 'a post no longer failed is not retried, nor named');
});

test('a note marked done on one server is gone from the health banner on another that had the old status', async () => {
  const other = await import('../routes/health.js?instance=2');       // a second warm instance, its own copy
  const a = await draft('done-elsewhere', 'approved', { caption: 'x' });
  await robot.call('POST', '/api/result', { id: a, status: 'publishing' }, R); await tick();
  await robot.call('POST', '/api/result', { id: a, status: 'scheduled', scheduled_for: '2026-10-21T13:00:00Z', post_id: 'cp2' }, R);
  await robot.call('POST', '/api/result', { id: a, status: 'needs_action', error: '2 copies are queued in SocialPilot: delete one' }, R);
  const has = h => h.reasons.some(r => r.code === 'publisher_needs_person' && r.ids.includes(a));
  assert.ok(has(await other.health({ fresh: true })));
  const done = await editor.call('POST', '/api/decide', { id: a, decision: 'done' });
  assert.ok(done.body.health_at > 0, JSON.stringify(done.body));
  assert.ok(has(await other.health()), 'its own copy, as before');
  assert.ok(!has(await other.health({ after: done.body.health_at })));
  const res = { status() { return res; }, json(o) { res.body = o; return res; }, setHeader() {} };
  await other.default({ method: 'GET', user: { email: 'ed@example.com' }, query: { after: String(done.body.health_at) } }, res);
  assert.ok(!res.body.reasons.some(r => r.code === 'publisher_needs_person'));
});

test("the review page's today tiles start at New York's midnight, also on the days the clocks change", async () => {
  const { readFile } = await import('node:fs/promises');
  const html = await readFile(new URL('../public/review.html', import.meta.url), 'utf8');
  const nyMidnight = new Function(html.match(/function nyMidnight[\s\S]*?\n}\n/)[0] + 'return nyMidnight;')();
  for (const [now, midnight] of [['2026-11-01T17:00:00Z', '2026-11-01T04:00:00Z'],      // clocks back at 2am
                                 ['2026-03-08T16:00:00Z', '2026-03-08T05:00:00Z'],      // clocks forward
                                 ['2026-10-23T03:30:00Z', '2026-10-22T04:00:00Z'],
                                 ['2026-12-01T05:00:00Z', '2026-12-01T05:00:00Z']])
    assert.equal(nyMidnight(Date.parse(now)), Date.parse(midnight) / 1000, now);
});
