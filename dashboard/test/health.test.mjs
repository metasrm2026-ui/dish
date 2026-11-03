// The health status (routes/health.js), the run reports bore/ops.py sends, the Studio watchdog and
// the second routine secret used while it is rotated.
import test from 'node:test';
import assert from 'node:assert/strict';
import { client, workspace } from './helpers.mjs';

process.env.BLOB_RETRY_MS = '1';                      // retries without the production waits
const R = { routine: true };
const M = 60e3, HR = 3600e3;
const mem = globalThis.__boreBlobs;
const { put } = await import('../lib/blob.js');
const { putRecord, getRecord, resetReads } = await import('../lib/db.js');
const { setNeedsAction } = await import('../routes/result.js');
const H = await import('../routes/health.js');
const { default: main } = await import('../api/main.js');
const CHECKPOINT = '<!DOCTYPE html><html><head><title>Vercel Security Checkpoint</title></head><body>We\'re verifying your browser</body></html>';

const putJson = (p, o) => put(p, JSON.stringify(o), { access: 'public', contentType: 'application/json', addRandomSuffix: false, allowOverwrite: true });
const backdate = (prefix, t) => { for (const [k, v] of mem) if (k.startsWith(prefix)) v.uploadedAt = new Date(t); };
const stamp = t => new Date(t).toISOString().replace(/\D/g, '').slice(0, 12);
const codes = h => h.reasons.map(r => `${r.level} ${r.code}`).sort();
const PAGE = 'dummy-sports';

async function draft(slugPart, made, statuses) {           // statuses: [[status, at, body?], ...]
  const id = `${PAGE}-${stamp(made)}-${slugPart}`;
  await putJson(`drafts/${id}/draft.json`, { id, page: PAGE, caption: 'c' });
  for (const [s, at, body] of statuses) await putJson(`drafts/${id}/s-${at}-${s}.json`, { status: s, at, ...(body || {}) });
  return id;
}

// a pipeline that works: the hourly routine saved its state and made a draft, the feed is fresh
async function healthy(now) {
  mem.clear();
  await putRecord('pages', PAGE, { slug: PAGE, name: 'Dummy Sports', sources: [{ label: 'SecretSourceXYZ', url: 'https://secret-source.example/feed' }] });
  await put('state/bore-1.db.gz', Buffer.from([0x1f, 0x8b]), { access: 'public', addRandomSuffix: false });
  backdate('state/', now - 50 * M);
  await draft('fresh-story', now - 40 * M, [['pending', now - 40 * M]]);
  await putRecord('feed', PAGE, { page: PAGE, updated_at: now - 30 * M, stories: [] });
  backdate('db/feed/', now - 30 * M);
}

function report(over = {}) {
  const t = Date.now();
  return { run_id: Math.random().toString(16).slice(2, 14), routine: 'hourly', command: 'routine prepare', started_at: t - 60e3,
           ended_at: t, ok: true, degraded: [], summary: { exit: 0 }, ...over };
}

// a request with any Authorization header (helpers' client sends only the current secret)
async function call(method, path, body, auth) {
  const [p, qs] = path.split('?');
  const payload = body === undefined ? Buffer.alloc(0) : Buffer.from(JSON.stringify(body));
  const out = { status: 200, body: null };
  const res = { headersSent: false, setHeader() {}, status(s) { out.status = s; return res; },
                json(o) { out.body = o; res.headersSent = true; return res; }, end() { res.headersSent = true; return res; } };
  await main({ method, url: path, query: { ...Object.fromEntries(new URLSearchParams(qs || '')), route: p.replace(/^\/api\//, '') },
               headers: { 'content-type': 'application/json', host: 'test.local', ...(auth ? { authorization: auth } : {}) },
               async *[Symbol.asyncIterator]() { if (payload.length) yield payload; } }, res);
  return out;
}

test('run reports: one per command, how it ended in the path, a repeat is not stored twice', async () => {
  mem.clear();
  const robot = client();
  const start = report({ ended_at: null, ok: null });
  assert.equal((await robot.call('POST', '/api/routine/runs', start, R)).body.state, 'start');
  const end = { ...start, ended_at: Date.now(), ok: true, degraded: ['recent posts unavailable https://secret-source.example/x'] };
  assert.equal((await robot.call('POST', '/api/routine/runs', end, R)).body.state, 'end-amber');
  assert.equal((await robot.call('POST', '/api/routine/runs', end, R)).body.duplicate, true, 'sent twice, stored once');
  assert.equal((await robot.call('POST', '/api/routine/runs', report({ ok: 'yes' }), R)).body.state, 'end-fail', 'ok is true, or it failed');
  assert.equal((await robot.call('POST', '/api/routine/runs', report({ started_at: 1792000000 }), R)).status, 400, 'seconds are refused');
  assert.equal((await robot.call('POST', '/api/routine/runs', report({ run_id: '' }), R)).status, 400);
  assert.equal((await client().call('POST', '/api/routine/runs', report())).status, 401);
  const listed = await robot.call('GET', '/api/routine/runs?hours=2', undefined, R);
  assert.equal(listed.body.runs.length, 2);
  assert.deepEqual(listed.body.runs.map(r => r.state).sort(), ['amber', 'fail']);
  assert.equal(listed.body.runs[0].command, 'routine prepare');
  const amber = listed.body.reports.find(r => r.run_id === start.run_id);
  assert.deepEqual(amber.degraded, ['recent posts unavailable <url>'], 'no links stored (they can name a source)');
});

test('health: each rule, from what the system already stores and from the run reports', async () => {
  const now = Date.now();
  await healthy(now);
  let h = await H.computeHealth(now);
  assert.equal(h.status, 'green', JSON.stringify(h.reasons));

  // the hourly routine stopped: no state save and no draft for over 2 hours (and so the feed went stale too)
  assert.deepEqual(codes(await H.computeHealth(now + 2 * HR)), ['amber topics_stale', 'red hourly_stale']);
  // it makes drafts but its last step (the state save) fails
  backdate('state/', now - 3 * HR);
  assert.deepEqual(codes(await H.computeHealth(now)), ['amber state_not_saved']);

  // the topic feed: stale after 90 minutes, red after 3 hours (a page that never had one is not stale)
  await healthy(now);
  backdate('db/feed/', now - 2 * HR);
  h = await H.computeHealth(now);
  assert.deepEqual(codes(h), ['amber topics_stale']);
  assert.match(h.reasons[0].text, /Dummy Sports was last refreshed 2h ago/);
  backdate('db/feed/', now - 4 * HR);
  assert.deepEqual(codes(await H.computeHealth(now)), ['red topics_stale']);

  // the publisher: approved and never claimed; held for SocialPilot; claimed and silent; scheduled, past and unconfirmed
  await healthy(now);
  const unclaimed = await draft('approved-story', now - 3 * HR, [['pending', now - 3 * HR], ['approved', now - 2 * HR]]);
  const held = await draft('held-story', now - 4 * HR, [['approved', now - 3 * HR], ['approved', now - 2 * HR], ['approved', now - HR]]);
  const stuck = await draft('stuck-story', now - 3 * HR, [['approved', now - 2 * HR], ['publishing', now - HR]]);
  const late = await draft('late-story', now - 9 * HR, [['scheduled', now - 6 * HR, { result: { scheduled_for: new Date(now - 4 * HR).toISOString() } }]]);
  await draft('soon-story', now - 9 * HR, [['scheduled', now - 6 * HR, { result: { scheduled_for: new Date(now + HR).toISOString() } }]]);
  h = await H.computeHealth(now);
  assert.deepEqual(codes(h), ['amber publisher_held', 'amber publisher_unconfirmed', 'red publisher_stuck', 'red publisher_unclaimed']);
  const ids = c => h.reasons.find(r => r.code === c).ids;
  assert.deepEqual([ids('publisher_unclaimed'), ids('publisher_held'), ids('publisher_stuck'), ids('publisher_unconfirmed')],
                   [[unclaimed], [held], [stuck], [late]]);
  assert.equal(h.reasons[0].level, 'red', 'red reasons first');
  // while SocialPilot is down an approved post waits for it: held, not "the publisher is not running"
  await putRecord('health', 'socialpilot', { down: true, since: now - 3 * HR, error: 'locked' });
  assert.deepEqual(codes(await H.computeHealth(now)).filter(c => /unclaimed|held/.test(c)), ['amber publisher_held']);
  // a held run is not a failed run (bore/ops.py): a long outage turns this red, for the owner's issue
  await draft('long-held', now - 6 * HR, [['approved', now - 5 * HR], ['approved', now - 4 * HR], ['approved', now - HR]]);
  h = await H.computeHealth(now);
  assert.deepEqual(codes(h).filter(c => /held/.test(c)), ['red publisher_held']);
  assert.match(h.reasons.find(r => r.code === 'publisher_held').text, /more than 4 hours for SocialPilot \(SocialPilot is unavailable\): they move to Failed 12 hours/);
  // a day's queue: approved overnight, scheduled 45 minutes apart through the day; the overdue one is the oldest
  await healthy(now);
  const overdue = await draft('overdue-story', now - 10 * HR, [['scheduled', now - 9 * HR, { result: { scheduled_for: new Date(now - 4 * HR).toISOString() } }]]);
  for (let n = 0; n < 24; n++)
    await draft(`later-${n}`, now - 9 * HR, [['scheduled', now - 8 * HR + n * M, { result: { scheduled_for: new Date(now + HR + n * 45 * M).toISOString() } }]]);
  h = await H.computeHealth(now);
  assert.deepEqual(codes(h), ['amber publisher_unconfirmed'], 'more than 20 later posts do not hide it');
  assert.deepEqual(h.reasons[0].ids, [overdue]);

  // Studio: a job nobody took for 10 minutes, one running for 25
  await healthy(now);
  const job = (n, t, extra) => putRecord('jobs', `${PAGE}-${t}-j${n}`, { id: `${PAGE}-${t}-j${n}`, kind: 'compose', page: PAGE,
                                                                         created_at: t, fired: true, fire_status: 200, ...extra });
  await job(1, now - 15 * M, { status: 'queued' });
  await job(2, now - 40 * M, { status: 'running', started_at: now - 30 * M });
  await job(3, now - 50 * M, { status: 'done', fired: false });                       // its wake call failed, then a nudge worked
  await job(4, now - 5 * M, { status: 'queued' });
  await job(5, now - 30 * M, { status: 'queued', released_at: now - 2 * M });          // handed back: waits again
  await job(6, now - 30 * HR, { status: 'queued' });                                  // older than a day: not read
  await job(8, now - 20 * M, { status: 'queued', kind: 'templates' });                // a conversion: no editor is waiting for it
  h = await H.computeHealth(now);
  assert.deepEqual(codes(h), ['red studio_queued', 'red studio_stuck'], 'a failed wake call of a job since done is no reason (it was, for 24 h)');
  assert.deepEqual(h.reasons.find(r => r.code === 'studio_queued').ids, [`${PAGE}-${now - 15 * M}-j1`]);
  // ids end in base64url, which can hold '-' (about 1 in 12): those are watched too
  const dashed = `${PAGE}-${now - 16 * M}--x0-a`;
  await putRecord('jobs', dashed, { id: dashed, kind: 'compose', page: PAGE, created_at: now - 16 * M, status: 'queued', fired: true, fire_status: 200 });
  h = await H.computeHealth(now);
  assert.deepEqual(h.reasons.find(r => r.code === 'studio_queued').ids, [`${PAGE}-${now - 15 * M}-j1`, dashed]);
  // a wake call that failed for a job still waiting, in its first hour: editors are told it could not
  // start, the admin which settings to check
  await job(7, now - 3 * M, { status: 'queued', fired: false, fire_status: 401 });
  await job(8, now - 2 * M, { status: 'queued', fired: false, fire_status: null, fire_error: 'The operation was aborted due to timeout' });
  await job(9, now - 90 * M, { status: 'queued', fired: false, fire_status: null });                     // the nudges have had their hour
  h = await H.computeHealth(now);
  const fire = h.reasons.find(r => r.code === 'studio_fire_failed');
  assert.deepEqual([fire.level, fire.ids], ['amber', [`${PAGE}-${now - 2 * M}-j8`, `${PAGE}-${now - 3 * M}-j7`]]);
  assert.equal(fire.text, '2 Studio job(s) are waiting: the Studio routine could not be started: the admin has been told.');
  assert.equal(fire.admin, 'The trigger call answered 401, failed (The operation was aborted due to timeout): check STUDIO_FIRE_URL '
                           + 'and STUDIO_FIRE_TOKEN in Vercel, then deploy.');

  // editors' links and regeneration requests waiting
  await healthy(now);
  await putJson(`links/${PAGE}-${now - 3 * HR}-ab/link.json`, { id: 'x', url: 'https://example.com/a', page: PAGE, at: now - 3 * HR });
  await putJson(`links/${PAGE}-${now - 3 * HR}-ab/s-${now - 3 * HR}-waiting.json`, { status: 'waiting' });
  await putJson(`links/${PAGE}-${now - 9 * HR}-cd/s-${now - 9 * HR}-waiting.json`, { status: 'waiting' });
  await putJson(`links/${PAGE}-${now - 9 * HR}-cd/s-${now - 8 * HR}-done.json`, { status: 'done' });
  await draft('regen-story', now - 8 * HR, [['pending', now - 8 * HR], ['regenerate', now - 7 * HR]]);
  assert.deepEqual(codes(await H.computeHealth(now)), ['amber link_waiting', 'red regen_waiting']);
});

test('health from the run reports: failed, degraded, the same warning 3 runs in a row, a start with no end', async () => {
  const now = Date.now();
  await healthy(now);
  const save = (over, ago) => H.saveRun(report({ started_at: now - ago, ended_at: now - ago + 30e3, ...over }), now);
  await save({ command: 'studio feed', routine: 'topics', ok: false, summary: { error: 'SSLError: HTTPSConnectionPool(host=https://x.example)' } }, 70 * M);
  let h = await H.computeHealth(now);
  assert.deepEqual(codes(h), ['amber run_failed']);
  assert.match(h.reasons[0].text, /"studio feed" \(topics\) failed \(latest \d\d:\d\d UTC\): SSLError\./);
  await save({ command: 'studio feed', routine: 'topics', ok: false, summary: { error: 'SSLError: x' } }, 10 * M);
  assert.deepEqual(codes(await H.computeHealth(now)), ['red run_failed'], 'twice in a row');
  await save({ command: 'studio feed', routine: 'topics', ok: true }, 5 * M);
  assert.deepEqual(codes(await H.computeHealth(now)), [], 'a run that worked ends it');

  for (const [i, reasons] of [[3, ['faces not compared (ImportError)']], [2, ['faces not compared (ImportError)', 'lessons unavailable']],
                              [1, ['faces not compared (ImportError)']]].entries())
    await save({ degraded: reasons[1] }, (reasons[0] * 60 + i) * M);
  h = await H.computeHealth(now);
  assert.deepEqual(codes(h), ['red run_degraded_repeat']);
  assert.match(h.reasons[0].text, /"routine prepare" \(hourly\) ran degraded in 3 runs in a row: faces not compared \(ImportError\)/);
  await save({ degraded: ['lessons unavailable'] }, 1);
  assert.deepEqual(codes(await H.computeHealth(now)), ['amber run_degraded'], 'a different warning: amber');

  await H.saveRun(report({ command: 'routine state pull', routine: 'setup', started_at: now - 50 * M, ended_at: null, ok: null }), now);
  await H.saveRun(report({ command: 'publish plan', routine: 'publisher', started_at: now - 20 * M, ended_at: null, ok: null }), now);
  await H.saveRun(report({ command: 'hourly', routine: 'manual', ok: false, started_at: now - 9 * M }), now);   // a developer's
  h = await H.computeHealth(now);
  assert.deepEqual(codes(h), ['amber run_degraded', 'amber run_unfinished'], 'a start older than 45 minutes with no end');
  assert.match(h.reasons.find(r => r.code === 'run_unfinished').text, /"routine state pull" \(setup\) started at/);

  // the trigger prompts' run log, until they are replaced: a failed step there shows too
  const robot = client();
  await robot.call('POST', '/api/routine/runlog', { routine: 'topics', step: 'sync', ok: false, detail: 'SSLEOFError https://secret-source.example' }, R);
  h = await H.computeHealth(now + 1000);
  assert.ok(codes(h).includes('amber runlog_failed'));
  assert.ok(!JSON.stringify(h).includes('secret-source') && !JSON.stringify(h).includes('SecretSourceXYZ'), 'no source names or links');
});

test('health counts routine runs, not command runs; what needs a person is its own reason', async () => {
  const now = Date.now();
  await healthy(now);
  const save = (session, over, ago) => H.saveRun(report({ started_at: now - ago, ended_at: now - ago + 30e3, ...over,
                                                          summary: { session, ...(over.summary || {}) } }), now);
  // one hourly session: three uploads hit one dashboard blip; that is one run, not "failed 3 times in a row"
  for (const ago of [12, 11, 10]) await save('s3', { command: 'routine upload', ok: false, summary: { error: 'HTTPError: 502' } }, ago * M);
  let h = await H.computeHealth(now);
  assert.deepEqual(codes(h), ['amber run_failed']);
  assert.deepEqual(h.reasons[0].ids.length, 1);
  // its retry worked: the session's last upload is what counts
  await save('s3', { command: 'routine upload', ok: true }, 9 * M);
  assert.deepEqual(codes(await H.computeHealth(now)), []);
  // and the session before failed too: two runs in a row
  await save('s3', { command: 'routine upload', ok: false, summary: { error: 'HTTPError: 502' } }, 8 * M);
  await save('s2', { command: 'routine upload', ok: false, summary: { error: 'HTTPError: 502' } }, 70 * M);
  h = await H.computeHealth(now);
  assert.deepEqual(codes(h), ['red run_failed']);
  assert.match(h.reasons[0].text, /"routine upload" \(hourly\) failed in 2 runs in a row/);
  const listed = await H.listRuns(2, now);
  assert.deepEqual([...new Set(listed.runs.map(r => r.session))].sort(), ['s2', 's3'], 'the session is in the path');

  // the same warning on every post of one session: amber; in 3 routine runs in a row: red
  await healthy(now);
  const stills = ['video stills unavailable'];
  for (const ago of [12, 11, 10]) await save('s3', { command: 'routine photos', degraded: stills }, ago * M);
  assert.deepEqual(codes(await H.computeHealth(now)), ['amber run_degraded']);
  await save('s2', { command: 'routine photos', degraded: stills }, 70 * M);
  await save('s1', { command: 'routine photos', degraded: stills }, 130 * M);
  assert.deepEqual(codes(await H.computeHealth(now)), ['red run_degraded_repeat']);

  // publish plan: an outdated original to delete in SocialPilot, a page with no account. The plan
  // ran fine (no degraded run, however many runs say it); a person has something to do
  await healthy(now);
  for (const [i, ago] of [50, 30, 10].entries())
    await H.saveRun(report({ command: 'publish plan', routine: 'publisher', started_at: now - ago * M, ended_at: now - ago * M + 20e3,
                             summary: { session: `p${i}`, delete_in_socialpilot: ['demo-orig'], no_account: i === 2 ? ['newpage-1'] : [] } }), now);
  h = await H.computeHealth(now);
  assert.deepEqual(codes(h), ['amber publisher_needs_person']);
  assert.deepEqual(h.reasons[0].ids, ['demo-orig', 'newpage-1']);
  assert.match(h.reasons[0].text, /1 outdated original post\(s\) are still scheduled in SocialPilot: a person deletes them there/);
  await H.saveRun(report({ command: 'publish plan', routine: 'publisher', started_at: now - 2 * M, ended_at: now - M, summary: { session: 'p9' } }), now);
  assert.deepEqual(codes(await H.computeHealth(now)), [], 'done: gone with the next plan');
});

test("the publisher's notes for a person: red unless the code only asks someone to look, until marked done", async () => {
  const now = Date.now();
  await healthy(now);
  const id = s => `${PAGE}-${stamp(now - HR)}-${s}`;
  // routes/result.js health/needs_action, which only the review page used to read. The code's own
  // notes asking a person to look (bore/cli.py unconfirmed, result.js missing and hold): amber
  await setNeedsAction(id('unseen'), PAGE, "SocialPilot's queue did not show it after CreatePost: check in SocialPilot that it is "
                                           + 'scheduled once, on account 900001, for 2026-10-22 18:30 (Asia/Kolkata)');
  await setNeedsAction(id('lost'), PAGE, 'look on the Facebook page: SocialPilot has no record of this post, it may or may not have gone out');
  await setNeedsAction(id('unknown'), PAGE, 'look in SocialPilot and on the Facebook page: the publisher could not check whether this post is there already');
  let h = await H.computeHealth(now);
  assert.deepEqual(codes(h), ['amber publisher_needs_person']);
  assert.deepEqual(h.reasons[0].ids, [id('unseen'), id('lost'), id('unknown')]);
  assert.equal(h.reasons[0].text, '3 post(s) need a person to check them in SocialPilot or on the Facebook page (the review page lists them).');
  // a copy on the account the publisher must never use, and a second copy (bore/publish.py verify_queued's words): red, first
  await setNeedsAction(id('copy'), PAGE, 'a copy is on the wrong SocialPilot account (Demo Person, loginId 900002): delete it in SocialPilot; '
                                         + '2 copies are queued in SocialPilot: delete all but one');
  // an outdated original, noted by the plan and in its report: counted once
  await setNeedsAction(id('orig'), PAGE, 'delete it in SocialPilot: it is scheduled for 2026-10-22 18:30 (Asia/Kolkata) and its update x replaces it',
                       'superseded_original');
  await H.saveRun(report({ command: 'publish plan', routine: 'publisher', started_at: now - 10 * M, ended_at: now - 9 * M,
                           summary: { session: 'p1', delete_in_socialpilot: [id('orig')], no_account: [] } }), now);
  h = await H.computeHealth(now);
  assert.deepEqual(codes(h), ['red publisher_needs_person']);
  assert.deepEqual(h.reasons[0].ids, [id('copy'), id('unseen'), id('lost'), id('unknown'), id('orig')]);
  assert.match(h.reasons[0].text, /^1 post\(s\) need a person in SocialPilot now: what the publisher found there goes out against the publishing rules unless someone fixes or deletes it \(e\.g\. a second copy, the wrong account or time\); 3 post\(s\) need a person .*; 1 outdated original post\(s\) are still scheduled/);
  assert.ok(!/Demo Person|900002|900001/.test(JSON.stringify(h)), "counts in the text, never the notes' words");
  // marked done on the review page: amber for what is left, then nothing
  await setNeedsAction(id('copy'), PAGE, '');
  assert.deepEqual(codes(await H.computeHealth(now)), ['amber publisher_needs_person']);
  for (const s of ['unseen', 'lost', 'unknown', 'orig']) await setNeedsAction(id(s), PAGE, '');
  await H.saveRun(report({ command: 'publish plan', routine: 'publisher', started_at: now - 2 * M, ended_at: now - M, summary: { session: 'p2' } }), now);
  assert.deepEqual(codes(await H.computeHealth(now)), []);

  // what a publisher session records itself (PUBLISH.md step 5: "a second copy of a post you find in
  // SocialPilot", in its own words): red at once, not amber until the copy has gone out
  const robot = client();
  for (const [s, text] of [['found', 'delete the copy scheduled for 2026-10-21 18:30'],               // PUBLISH.md's own example
                           ['twice', '2 copies of this post are queued in SocialPilot: delete one'],
                           ['person', 'a second copy is scheduled on Demo Person (900002): delete it']]) {
    const d = await draft(s, now - 2 * HR, [['approved', now - 2 * HR], ['publishing', now - HR], ['scheduled', now - HR + M]]);
    assert.equal((await robot.call('POST', '/api/result', { id: d, status: 'needs_action', error: text }, R)).status, 200);
    h = await H.computeHealth(now);
    assert.deepEqual(codes(h), ['red publisher_needs_person'], text);
    assert.equal(h.reasons[0].ids.at(-1), d);
    await setNeedsAction(d, PAGE, '');
  }
  // a changed caption goes out unapproved: red too
  await setNeedsAction(id('caption'), PAGE, 'its caption in SocialPilot is not the approved one: check it in SocialPilot');
  assert.deepEqual(codes(await H.computeHealth(now)), ['red publisher_needs_person']);
  await setNeedsAction(id('caption'), PAGE, '');

  // a run report is whatever the command sent: one whose lists are not lists is left out, never a
  // crash of the whole status (a 500 on /api/health and on the watchdog)
  await H.saveRun(report({ command: 'publish plan', routine: 'publisher', started_at: now - M, ended_at: now - 30e3,
                           summary: { session: 'p3', delete_in_socialpilot: 'x', no_account: { a: 1 } } }), now);
  assert.deepEqual(codes(await H.computeHealth(now)), []);
  await H.saveRun(report({ command: 'publish plan', routine: 'publisher', started_at: now - 20e3, ended_at: now - 10e3,
                           summary: { session: 'p4', delete_in_socialpilot: ['demo-orig', 7], no_account: null } }), now);
  h = await H.computeHealth(now);
  assert.deepEqual([codes(h), h.reasons[0].ids], [['amber publisher_needs_person'], ['demo-orig']]);
});

test('health reads are bounded: at most 6 storage reads at a time, at most 60 jobs, and kept 5 minutes', async () => {
  const now = Date.now();
  await healthy(now);
  for (let n = 0; n < 90; n++) {
    const id = `${PAGE}-${now - 20 * HR + n * 60e3}-b${n}`;
    await putRecord('jobs', id, { id, kind: 'ideas', page: PAGE, status: 'done', created_at: now - 20 * HR + n * 60e3, fired: true, fire_status: 200 });
  }
  const real = globalThis.fetch;
  let live = 0, most = 0, jobsRead = 0;
  globalThis.fetch = async (u, o) => {
    live++; most = Math.max(most, live);
    if (String(u).includes('db/jobs/')) jobsRead++;
    try { await new Promise(r => setTimeout(r, 2)); return await real(u, o); } finally { live--; }
  };
  try {
    assert.equal((await H.computeHealth(now)).status, 'green');
  } finally { globalThis.fetch = real; }
  assert.ok(most <= 6, `at most 6 at a time (saw ${most})`);
  assert.equal(jobsRead, 60, 'the newest 60 jobs only');

  const a = await H.health({ now }), b = await H.health({ now: now + 4 * M });
  assert.equal(b.checked_at, a.checked_at, 'kept 5 minutes');
  assert.ok((await H.health({ now: now + 6 * M })).checked_at > a.checked_at, 'then worked out again');
  assert.equal((await getRecord('health', 'status')).checked_at, now + 6 * M, 'shared by every instance');
});

test('health is worked out once at a time: concurrent stale requests on one instance or several', async () => {
  const now = Date.now() + 30 * M;                     // past what earlier tests left in this instance's memory
  await healthy(now);
  for (let n = 0; n < 30; n++) {
    const id = `${PAGE}-${now - 5 * HR + n * 60e3}-c${n}`;
    await putRecord('jobs', id, { id, kind: 'ideas', page: PAGE, status: 'done', created_at: now - 5 * HR + n * 60e3, fired: true, fire_status: 200 });
  }
  const first = await H.health({ now });
  const other = await import('../routes/health.js?instance=2');          // another serverless instance: its own memory
  resetReads();                                        // (a computation then reads every job: the instance kept the first's)
  const real = globalThis.fetch;
  let jobsRead = 0;
  globalThis.fetch = async (u, o) => {
    if (String(u).includes('db/jobs/')) jobsRead++;
    await new Promise(r => setTimeout(r, 2));
    return real(u, o);
  };
  const later = now + 6 * M;
  let all;
  try {
    all = await Promise.all([...Array(5)].flatMap(() => [H.health({ now: later }), other.health({ now: later })]));
  } finally { globalThis.fetch = real; }
  assert.equal(jobsRead, 30, 'ten stale requests, one computation');
  assert.ok(all.every(h => [first.checked_at, later].includes(h.checked_at)), 'the others are given the last status meanwhile');
  assert.ok(all.some(h => h.checked_at === later));
  assert.equal((await getRecord('health', 'status')).checked_at, later);
  assert.deepEqual([...mem.keys()].filter(k => k.startsWith('health-lease/')), [], 'the lease is let go');
  assert.equal((await other.health({ now: later + M })).checked_at, later, 'then fresh for every instance');
});

test('storage refusing reads: red at once, and no instance scans a store that refuses every read', async () => {
  const now = Date.now() + 60 * M;                     // past what earlier tests left in this instance's memory
  await healthy(now);
  const waiting = `${PAGE}-${now - 25 * M}-wait`;      // an editor's job nobody took
  await putRecord('jobs', waiting, { id: waiting, kind: 'compose', page: PAGE, status: 'queued', created_at: now - 25 * M, fired: true, fire_status: 200 });
  for (let n = 0; n < 30; n++) {
    const id = `${PAGE}-${now - 5 * HR + n * M}-r${n}`;
    await putRecord('jobs', id, { id, kind: 'ideas', page: PAGE, status: 'done', created_at: now - 5 * HR + n * M, fired: true, fire_status: 200 });
  }
  await putRecord('health', 'status', { status: 'green', reasons: [], checked_at: now - 6 * M });     // due to be worked out again
  const known = await import('../routes/health.js?challenged=0');            // an instance that had the status before
  assert.equal((await known.health({ now: now - 4 * M })).checked_at, now - 6 * M);
  resetReads();
  const real = globalThis.fetch;
  let reads = 0;
  globalThis.fetch = async (u, o) => {                 // Oct 21: every CDN read answered the security checkpoint page
    if (!String(u).startsWith('mock://')) return real(u, o);
    reads++;
    return new Response(CHECKPOINT, { status: 403, headers: { 'content-type': 'text/html' } });
  };
  try {
    // worked out anyway (the hourly watchdog): red, not a quieter amber with the waiting editor left out
    const h = await H.computeHealth(now);
    assert.ok(codes(h).includes('red storage_unreadable'), JSON.stringify(codes(h)));
    assert.match(h.reasons.find(r => r.code === 'storage_unreadable').text, /security check/);
    assert.ok(reads <= 8, `${reads} refused reads, not one per job`);
    // asked for by open pages on four instances: each reads the stored status (refused: retried,
    // then this instance pauses its reads) and answers red, without a scan; for 2 minutes
    for (const k of [1, 2, 3, 4]) {
      const inst = await import(`../routes/health.js?challenged=${k}`);
      resetReads(); reads = 0;                         // each instance pauses on its own
      assert.deepEqual(codes(await inst.health({ now })), ['red storage_unreadable']);
      assert.ok(reads <= 4, `instance ${k}: ${reads} reads`);
      reads = 0;
      assert.deepEqual(codes(await inst.health({ now: now + M })), ['red storage_unreadable']);
      assert.equal(reads, 0, 'kept 2 minutes');
    }
    // one that knew a status (green, 6 minutes old) gives it under the reason, never alone: to the
    // health workflow a green would hide the outage, and close the owner's issue
    resetReads();
    const last = await known.health({ now });
    assert.deepEqual([last.status, codes(last), last.checked_at], ['red', ['red storage_unreadable'], now - 6 * M]);
  } finally { globalThis.fetch = real; resetReads(); }
  assert.ok(codes(await H.computeHealth(now)).includes('red studio_queued'), 'readable again: the waiting editor shows');
});

test('a blip is not an outage: a read failing through its retries is amber; a broken stored status is worked out again', async () => {
  const now = Date.now() + 90 * M;                     // past what earlier tests left in this instance's memory
  await healthy(now);
  const job = `${PAGE}-${now - 5 * M}-blip`;
  await putRecord('jobs', job, { id: job, kind: 'ideas', page: PAGE, status: 'done', created_at: now - 5 * M, fired: true, fire_status: 200 });
  await putRecord('health', 'status', { status: 'green', reasons: [], checked_at: now - M });
  resetReads();
  const real = globalThis.fetch;
  const failing = (part, status, n = Infinity) => {
    globalThis.fetch = async (u, o) => (String(u).includes(part) && n-- > 0 ? new Response('no', { status }) : real(u, o));
  };
  let h;
  try {
    // one job read answering 503 through its 4 tries (about 2 s in production): that check is
    // unreadable, but red storage_unreadable (which opens the owner's issue) is for storage refusing reads
    failing('db/jobs/', 503, 4);
    assert.deepEqual(codes(await H.computeHealth(now)), ['amber studio_jobs_unreadable']);
    // the stored status answering 404 (or not decrypting): worked out again and written anew, not a
    // red status served by every instance until the watchdog writes one
    failing('db/health/status/', 404);
    h = await (await import('../routes/health.js?broken=1')).health({ now });
    assert.deepEqual([h.checked_at, codes(h)], [now, []]);
    // the stored status not answering (5xx through its retries) on an instance with no status of its own: amber
    failing('db/health/status/', 503);
    h = await (await import('../routes/health.js?broken=2')).health({ now });
    assert.deepEqual(codes(h), ['amber storage_unreadable']);
  } finally { globalThis.fetch = real; resetReads(); }
  assert.equal((await getRecord('health', 'status')).checked_at, now, 'the status worked out again was saved');
});

test('old run reports: a report prunes a few at a time, the hourly watchdog whole days', async () => {
  const now = Date.now();
  await healthy(now);
  const day = n => new Date(now - n * 86400e3).toISOString().slice(0, 10);
  const left = n => [...mem.keys()].filter(k => k.startsWith(`runs/${day(n)}/`)).length;
  for (let i = 0; i < 250; i++) await put(`runs/${day(8)}/a${i}/end-ok.enc`, 'x', { access: 'public', addRandomSuffix: false });
  for (let i = 0; i < 50; i++) await put(`runs/${day(9)}/b${i}/end-ok.enc`, 'x', { access: 'public', addRandomSuffix: false });
  const rec = report(), random = Math.random;
  Math.random = () => 0;                               // this report draws the prune
  try { assert.equal((await H.saveRun(rec, now)).saved, true); } finally { Math.random = random; }
  assert.deepEqual([left(8), left(9)], [150, 50], 'one listing page and one delete: the report is answered in seconds');
  assert.equal((await client().call('POST', '/api/routine/watchdog', {}, R)).status, 200);
  assert.deepEqual([left(8), left(9), left(0)], [0, 0, 1], 'the watchdog prunes whole days; the week is kept');
});

test('watchdog: stalled Studio jobs restart and then fail with no batch open; the streak of bad checks', async () => {
  const now = Date.now();
  await healthy(now);
  const id = `${PAGE}-${now - 11 * M}--wd-1`;                          // a '-' in its random part too
  const bt = { id: 'bt-wd', page: PAGE, posts: [{ id: 'p1', story: { headline: 'H' }, job: { id, kind: 'ideas', status: 'queued' } }] };
  await putRecord('batches', bt.id, bt);
  await putRecord('jobs', id, { id, kind: 'ideas', batch_id: bt.id, page: PAGE, post_ids: ['p1'], status: 'queued', created_at: now - 11 * M });
  backdate(`db/jobs/${id}/`, now - 11 * M);
  const robot = client();
  const r1 = await robot.call('POST', '/api/routine/watchdog', {}, R);
  assert.equal(r1.status, 200);
  assert.equal(r1.body.open_jobs, 1);
  assert.equal(mem.has(`job-nudges/${id}/0.json`), true, 'restarted, though no editor has the batch open');
  assert.equal(r1.body.health.status, 'red');
  assert.equal(r1.body.streak, 1);
  // restarted 5 times and still nobody took it: the editor is told
  for (let n = 1; n < 5; n++) await put(`job-nudges/${id}/${n}.json`, '{}', { access: 'public', addRandomSuffix: false });
  for (let n = 0; n < 5; n++) mem.get(`job-nudges/${id}/${n}.json`).uploadedAt = new Date(Date.now() - (25 - 4 * n) * M);
  const r2 = await robot.call('POST', '/api/routine/watchdog', {}, R);
  assert.equal(r2.body.failed_batches, 1);
  assert.equal((await getRecord('jobs', id)).status, 'failed');
  assert.match((await getRecord('batches', bt.id)).posts[0].job.error, /after 5 restarts/);
  assert.equal(r2.body.health.status, 'green', 'nobody is waiting any more');
  assert.equal(r2.body.streak, 0, 'green ends the streak');
  backdate('state/', now - 3 * HR);                                   // then the hourly routine stops
  for (const k of [...mem.keys()].filter(k => k.startsWith('drafts/'))) mem.delete(k);
  assert.equal((await robot.call('POST', '/api/routine/watchdog', {}, R)).body.streak, 1);
  assert.equal((await robot.call('POST', '/api/routine/watchdog', {}, R)).body.streak, 2, 'bad checks in a row');
  // the watchdog's own silence is a reason
  await putRecord('health', 'watchdog', { at: now - 3 * HR, status: 'green', streak: 0 });
  assert.ok(codes(await H.computeHealth(now)).includes('amber watchdog_silent'));
});

test('health for people and for the workflow; the second routine secret while it is rotated', async () => {
  mem.clear();
  const robot = client(), admin = client();
  const boot = await robot.call('POST', '/api/routine/bootstrap', { email: 'owner@example.com', name: 'Owner' }, R);
  await admin.call('POST', '/api/auth/reset', { token: boot.body.setup_link.split('#reset=')[1], password: 'correct horse 42' });
  assert.equal((await client().call('GET', '/api/health')).status, 401, 'signed-in users only');
  const h = await admin.call('GET', '/api/health');
  assert.equal(h.status, 200);
  assert.ok(['green', 'amber', 'red'].includes(h.body.status) && Array.isArray(h.body.reasons) && h.body.checked_at);
  assert.equal((await robot.call('GET', '/api/routine/health', undefined, R)).body.checked_at, h.body.checked_at, 'the same status');

  // the settings to check are the admin's: editors get the reason without them
  const WS = (await workspace(admin)).id;
  await putRecord('pages', PAGE, { slug: PAGE, name: 'Dummy Sports', workspace: WS, status: 'live' });
  const job = `${PAGE}-${Date.now() - 2 * M}-nf`;
  await putRecord('jobs', job, { id: job, kind: 'ideas', page: PAGE, status: 'queued', created_at: Date.now() - 2 * M, fired: false, fire_status: 403 });
  await H.health({ fresh: true });
  const inv = await admin.call('POST', '/api/admin/users', { email: 'ed@example.com', role: 'editor', workspace: WS, pages: [PAGE] });
  const editor = client();
  await editor.call('POST', '/api/auth/reset', { token: inv.body.setup_link.split('#reset=')[1], password: 'editor pass 77' });
  const [forAdmin, forEditor] = [(await admin.call('GET', '/api/health')).body, (await editor.call('GET', '/api/health')).body];
  assert.match(forAdmin.reasons.find(r => r.code === 'studio_fire_failed').admin, /answered 403: check STUDIO_FIRE_URL/);
  // the editor's words never carry the product-wide count (other workspaces' jobs are in it)
  assert.equal(forEditor.reasons.find(r => r.code === 'studio_fire_failed').text,
               'Some Studio jobs are waiting: the Studio routine could not be started: the admin has been told.');
  assert.ok(!JSON.stringify(forEditor).includes('STUDIO_FIRE'), 'no setting names on an editor\'s banner');

  process.env.ROUTINE_SECRET_NEXT = 'routine-next-secret';
  try {
    assert.equal((await call('GET', '/api/routine/health', undefined, 'Bearer routine-next-secret')).status, 200);
    assert.equal((await call('GET', '/api/routine/health', undefined, `Bearer ${process.env.ROUTINE_SECRET}`)).status, 200, 'the old one still works');
    assert.equal((await call('GET', '/api/routine/health', undefined, 'Bearer wrong')).status, 401);
    assert.equal((await call('GET', '/api/routine/health', undefined, 'Bearer ')).status, 401);
  } finally { delete process.env.ROUTINE_SECRET_NEXT; }
  assert.equal((await call('GET', '/api/routine/health', undefined, 'Bearer routine-next-secret')).status, 401, 'only while it is set');
});

test('banners say what a problem means for editors; the machinery is the admin\'s alone', async () => {
  const { plain } = await import('../routes/health.js');
  const r = (code, text, level = 'amber') => ({ code, level, text });
  assert.equal(plain(r('run_unfinished', '"routine prepare" (hourly) started at 13:12 UTC and never finished')), null);
  assert.equal(plain(r('run_degraded', '"studio feed" (topics) ran degraded at 14:27 UTC: other outlets\' photos unavailable.')),
               'Photo suggestions may be fewer than usual right now.');
  assert.match(plain(r('run_failed', '"routine upload" (hourly) failed in 2 runs in a row', 'red')), /^New drafts may be late/);
  assert.equal(plain(r('watchdog_silent', 'the hourly health check has not run')), null);
  const t = 'The Studio story list for Page was last refreshed 2 h ago.';
  assert.equal(plain(r('topics_stale', t)), t);
});

test('a run with only the shared Krea count unavailable is the admin\'s: editors still get every post', async () => {
  const { plain } = await import('../routes/health.js');
  const now = Date.now() + 3 * 3600e3;                           // after the reports of the tests above
  await healthy(now);
  const save = (degraded, ago) => H.saveRun(report({ command: 'routine krea-request', started_at: now - ago, ended_at: now - ago + 30e3, degraded }), now);
  await save(['shared Krea allowance unavailable (HTTPError)'], 5 * M);
  let r = (await H.computeHealth(now)).reasons.find(x => x.code === 'run_degraded');
  assert.equal(r.admin_only, true);
  assert.equal(plain(r), null, 'no editor banner');
  await save(['shared Krea allowance unavailable (HTTPError)', 'lessons unavailable'], 2 * M);
  r = (await H.computeHealth(now)).reasons.find(x => x.code === 'run_degraded');
  assert.equal(r.admin_only, undefined);
  assert.match(plain(r), /^New drafts may be late/, 'anything else in it is still said');
});

test('three hours of the shared Krea count unavailable are still the admin\'s; the latest run\'s other problems are said', async () => {
  const { plain } = await import('../routes/health.js');
  const now = Date.now() + 6 * 3600e3;
  const K = 'shared Krea allowance unavailable (HTTPError)';
  const inSession = (session, ago, degraded) => H.saveRun(report({ command: 'routine prepare', started_at: now - ago, ended_at: now - ago + 30e3,
                                                                     degraded, summary: { session } }), now);
  await healthy(now);
  await inSession('s1', 130 * M, [K]); await inSession('s2', 70 * M, [K]); await inSession('s3', 10 * M, [K]);
  let h = await H.computeHealth(now);
  let r = h.reasons.find(x => x.code === 'run_degraded_repeat');
  assert.deepEqual([r.admin_only, plain(r)], [true, null], 'three hours of it is still the admin\'s alone');
  assert.equal(h.reasons.filter(x => x.code === 'run_degraded').length, 0);
  await inSession('s4', 2 * M, [K, 'lessons unavailable']);
  h = await H.computeHealth(now);
  r = h.reasons.find(x => x.code === 'run_degraded_repeat');
  assert.equal(r.admin_only, true);
  r = h.reasons.find(x => x.code === 'run_degraded');
  assert.equal(r.admin_only, undefined);
  assert.match(r.text, /lessons unavailable/);
  assert.match(plain(r), /^New drafts may be late/, 'what else the latest run lacked is still said');
});

test('an amber run whose report cannot be read is not the admin\'s alone', async () => {
  const { plain } = await import('../routes/health.js');
  const now = Date.now() + 9 * 3600e3;
  await healthy(now);
  await H.saveRun(report({ command: 'routine krea-request', started_at: now - 5 * M, ended_at: now - 5 * M + 30e3,
                           degraded: ['shared Krea allowance unavailable (HTTPError)'] }), now);
  for (const [k, v] of mem) if (k.startsWith('runs/') && k.endsWith('/end-amber.enc')) v.buf = Buffer.from('garbage');
  const r = (await H.computeHealth(now)).reasons.find(x => x.code === 'run_degraded');
  assert.equal(r.admin_only, undefined);
  assert.match(plain(r), /^New drafts may be late/);
});
