// The review page's routes for an editor (third audit): a page whose slug holds '-2', other pages'
// notes and links, an emptied caption, red notes that cannot be read, lost answers on a claim, a
// withdraw or hold whose status write failed, and bulk actions on posts decided meanwhile.
import test from 'node:test';
import assert from 'node:assert/strict';
import { client, makeLive, workspace } from './helpers.mjs';

const R = { routine: true };
const tinyJpeg = Buffer.from('/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=', 'base64');
const tick = () => new Promise(r => setTimeout(r, 5));      // status records are named by the millisecond
const { saveDraft, setStatus, loadDraft, pageOf, claimDecision, decidedBy } = await import('../lib/store.js');
const { setNeedsAction } = await import('../routes/result.js');
const { resetReads } = await import('../lib/db.js');
const mem = globalThis.__boreBlobs;
const stamp = (t = Date.now()) => new Date(t).toISOString().replace(/\D/g, '').slice(0, 12);
const robot = client(), owner = client(), editor = client();
let top20, other;

async function signIn(c, link, email, pw) {
  await c.call('POST', '/api/auth/reset', { token: link.split('#reset=')[1], password: pw });
  assert.equal((await c.call('POST', '/api/auth/login', { email, password: pw })).status, 200);
}
async function draft(slug, status = 'pending', body = {}, page = 'dummy-sports') {
  const id = `${page}-${stamp()}-${slug}`;
  await saveDraft({ id, page, page_name: page, caption: `Caption of ${slug}` }, tinyJpeg);
  await setStatus(id, status, body);
  await tick();
  return id;
}
const status = async id => (await robot.call('GET', '/api/drafts?status=all', undefined, R)).body.drafts.find(d => d.id === id);
// storage refusing writes of keys that match (once each, unless `always`), or landing them and losing the answer
function refuse(match, { lost = false, always = false } = {}) {
  const realSet = mem.set;
  let on = true;
  mem.set = function (k, v) {
    if (on && match(k)) { if (!always) on = false; if (lost) realSet.call(this, k, v); throw new Error('storage unavailable'); }
    return realSet.call(this, k, v);
  };
  return () => { mem.set = realSet; };
}

test.before(async () => {
  const boot = await robot.call('POST', '/api/routine/bootstrap', { email: 'owner@example.com', name: 'Owner' }, R);
  await signIn(owner, boot.body.setup_link, 'owner@example.com', 'correct horse 42');
  const WS = (await workspace(owner)).id;
  await owner.call('POST', '/api/admin/pages', { name: 'Dummy Sports', fb_page_id: '123', workspace: WS });
  top20 = (await owner.call('POST', '/api/admin/pages', { name: 'Top 20 Demo', fb_page_id: '124', workspace: WS })).body.page.slug;
  other = (await owner.call('POST', '/api/admin/pages', { name: 'Other Page', fb_page_id: '125', workspace: WS })).body.page.slug;
  for (const [slug, login] of [['dummy-sports', '999'], [top20, '998'], [other, '997']]) await makeLive(slug, login);
  const inv = await owner.call('POST', '/api/admin/users', { email: 'ed@example.com', role: 'editor', workspace: WS, pages: ['dummy-sports', top20] });
  await signIn(editor, inv.body.setup_link, 'ed@example.com', 'editor pass 77');
});

test("a page whose slug holds '-2' (Top 20 Demo) gets its posts in Waiting and its own counts", async () => {
  assert.equal(top20, 'top-20-demo');
  assert.equal(pageOf('top-20-demo-202610211200-demo-story'), 'top-20-demo');
  assert.equal(pageOf('dummy-sports-202610211200-x-2-1'), 'dummy-sports');
  const id = await draft('demo-story', 'pending', {}, top20);
  assert.ok((await editor.call('GET', '/api/drafts?status=pending')).body.drafts.some(d => d.id === id), 'the editor sees it');
  const s = (await robot.call('GET', '/api/stats?since=0', undefined, R)).body;
  assert.equal((s.counts[top20] || {}).pending, 1);
  assert.equal(s.counts.top, undefined);
  assert.equal((await robot.call('GET', `/api/stats?since=0&page=${top20}`, undefined, R)).body.counts[top20].pending, 1, 'the daily cap counts it');
});

test("an editor sees only their own pages' notes, counts and links, and cannot retry another page's link", async () => {
  const theirs = await draft('other-sched', 'scheduled', {}, other);
  await setNeedsAction(theirs, other, 'delete the second copy in SocialPilot');
  const link = (await owner.call('POST', '/api/links', { url: 'https://example.com/other-story', page: other })).body.link;
  const { setLinkStatus } = await import('../routes/links.js');
  await setLinkStatus(link.id, 'failed', { error: 'no article text' });
  const s = (await editor.call('GET', '/api/stats?since=0')).body;
  assert.equal(theirs in s.publisher.needs_action, false);
  assert.equal(s.totals.scheduled, undefined, 'the Scheduled badge counts only their pages');
  assert.ok(theirs in (await owner.call('GET', '/api/stats?since=0')).body.publisher.needs_action, 'admins: every page');
  assert.ok(!(await editor.call('GET', '/api/links')).body.links.some(l => l.id === link.id));
  assert.ok((await owner.call('GET', '/api/links')).body.links.some(l => l.id === link.id));
  assert.equal((await editor.call('POST', '/api/links', { id: link.id, action: 'retry' })).status, 404);
  const { health } = await import('../routes/health.js');
  await health({ fresh: true });
  const needs = async c => (await c.call('GET', '/api/health')).body.reasons.some(r => r.code === 'publisher_needs_person');
  assert.equal(await needs(editor), false, "another page's note is not the editor's banner");
  assert.equal(await needs(owner), true);
  const mine = await draft('mine-sched', 'scheduled');
  await setNeedsAction(mine, 'dummy-sports', 'delete the second copy in SocialPilot');
  await health({ fresh: true });
  assert.equal(await needs(editor), true, 'their own page: shown');
  await setNeedsAction(mine, 'dummy-sports', '');
  await setNeedsAction(theirs, other, '');
});

test("an editor's health reason carries only their own posts' ids and level, however many other notes there are", async () => {
  const { health } = await import('../routes/health.js');
  const reason = async c => (await c.call('GET', '/api/health')).body.reasons.find(r => r.code === 'publisher_needs_person');
  const many = [];
  for (let i = 0; i < 13; i++) {
    many.push(await draft(`other-${i}`, 'scheduled', {}, other));
    await setNeedsAction(many[i], other, 'delete the second copy in SocialPilot');
  }
  const mine = await draft('mine-look', 'scheduled');
  await setNeedsAction(mine, 'dummy-sports', 'look on the Facebook page: SocialPilot has no record of this post, it may or may not have gone out');
  await health({ fresh: true });
  const r = await reason(editor);
  assert.deepEqual([r && r.level, r && r.ids], ['amber', [mine]], "after the 12th id, and only a look note of theirs: amber, never the other page's red");
  assert.equal((await editor.call('GET', '/api/health')).body.status, 'amber');
  assert.ok(!JSON.stringify((await editor.call('GET', '/api/health')).body).includes(other), "no other page's draft id");
  const all = await reason(owner);
  assert.deepEqual([all.level, all.ids.length, 'person' in all], ['red', 12, false]);
  // their own red note: red
  const copy = await draft('mine-copy', 'scheduled');
  await setNeedsAction(copy, 'dummy-sports', 'delete the second copy in SocialPilot');
  await health({ fresh: true });
  assert.deepEqual([(await reason(editor)).level, (await reason(editor)).ids], ['red', [copy, mine]]);
  for (const id of [...many, mine, copy]) await setNeedsAction(id, pageOf(id), '');
});

test("a note stored before the '-2' fix, with the wrong page, still reaches its page's editor", async () => {
  const id = await draft('old-note', 'scheduled', {}, top20);
  await setNeedsAction(id, 'top', 'delete the second copy in SocialPilot');     // what groupDrafts' old split('-2') gave
  assert.ok(id in (await editor.call('GET', '/api/stats?since=0')).body.publisher.needs_action);
  assert.equal((await editor.call('POST', '/api/decide', { id, decision: 'done' })).status, 200);
  assert.ok(!(id in (await editor.call('GET', '/api/stats?since=0')).body.publisher.needs_action), 'and they can mark it done');
});

test('Okay with the caption emptied (or only spaces) is refused; Okay with no caption sent keeps the routine one', async () => {
  for (const caption of ['', '   \n ', '\u200b\u200b', ' \u200d\ufeff\u2060 ']) {      // (zero-width characters: trim() keeps them)
    const id = await draft('blank-caption');
    const r = await editor.call('POST', '/api/decide', { id, decision: 'approve', caption });
    assert.deepEqual([r.status, r.body.error], [400, 'the caption is empty: write one first']);
    assert.equal((await status(id)).status, 'pending');
  }
  const id = await draft('no-caption-sent');
  assert.equal((await editor.call('POST', '/api/decide', { id, decision: 'approve' })).status, 200);
  assert.equal((await status(id)).status_info.caption, 'Caption of no-caption-sent');
});

test('a red note that cannot be read stops a failed post being approved again, and the page is told', async () => {
  const id = await draft('red-note', 'failed', { error: 'scheduled on the wrong SocialPilot account' });
  await setNeedsAction(id, 'dummy-sports', 'a copy is on the wrong SocialPilot account: delete it there');
  const realGet = mem.get;
  mem.get = function (k) { return String(k).startsWith('db/health/needs_action/') ? undefined : realGet.call(this, k); };
  resetReads();
  try {
    const one = await editor.call('POST', '/api/decide', { id, decision: 'approve', caption: 'x' });
    assert.equal(one.status, 503, JSON.stringify(one.body));
    assert.match(one.body.error, /nothing was changed/);
    assert.equal((await editor.call('POST', '/api/decide', { ids: [id], decision: 'retry' })).status, 503);
    assert.equal((await editor.call('POST', '/api/decide', { id, decision: 'done' })).status, 503);
    assert.equal((await status(id)).status, 'failed');
    const pub = (await editor.call('GET', '/api/stats?since=0')).body.publisher;
    assert.deepEqual([pub.needs_action_unknown, pub.needs_action], [true, {}]);
  } finally { mem.get = realGet; resetReads(); }
  const back = await editor.call('POST', '/api/decide', { id, decision: 'approve', caption: 'x' });
  assert.deepEqual([back.status, back.body.error], [409, 'not approved again: do what its red note says first']);
  assert.equal((await editor.call('GET', '/api/stats?since=0')).body.publisher.needs_action_unknown, undefined);
  await setNeedsAction(id, 'dummy-sports', '');
});

test("a decision claim that landed although its answer was lost is the editor's own: the post is decided", async () => {
  const id = await draft('lost-claim');
  const undo = refuse(k => k.startsWith(`decisions/${id}/`), { lost: true });
  try {
    const r = await editor.call('POST', '/api/decide', { id, decision: 'approve', caption: 'Lost answer' });
    assert.equal(r.status, 200, JSON.stringify(r.body));
  } finally { undo(); }
  assert.equal((await status(id)).status, 'approved');
  // the lost answer could not be listed either (the press answered 503): their own claim, under a
  // minute old, with no status after it, is theirs when they press again
  const c = await draft('lost-claim-unlisted');
  const at = (await status(c)).status_info.at;
  mem.set(`decisions/${c}/${at}.json`, { buf: Buffer.from(JSON.stringify({ by: 'ed@example.com', decision: 'approve', at: Date.now(), w: 'gone' })),
                                         contentType: 'application/json', uploadedAt: new Date() });
  assert.equal((await editor.call('POST', '/api/decide', { id: c, decision: 'approve', caption: 'Again' })).status, 200);
  assert.equal((await status(c)).status, 'approved');
  // once a status came after it (their other press won and wrote it), it is not theirs a second time
  assert.match(decidedBy(await claimDecision(c, at, 'ed@example.com', 'approve')), /^already approved by ed@example\.com/);
  // someone else's claim is still someone else's
  const b = await draft('claimed-by-other');
  assert.equal(await claimDecision(b, (await status(b)).status_info.at, 'owner@example.com', 'approve'), null);
  assert.match((await editor.call('POST', '/api/decide', { id: b, decision: 'reject', reason: 'Duplicate' })).body.error, /^already approved by owner@example\.com/);
});

test('a withdraw whose status write failed can be pressed again; the publisher can still claim the post meanwhile', async () => {
  const id = await draft('withdraw-fail', 'approved', { caption: 'w' });
  let undo = refuse(k => k.startsWith(`drafts/${id}/s-`) && k.endsWith('-rejected.json'));
  try { assert.ok((await editor.call('POST', '/api/decide', { id, decision: 'withdraw' })).status >= 500); }
  finally { undo(); }
  assert.equal((await status(id)).status, 'approved');
  assert.equal((await editor.call('POST', '/api/decide', { id, decision: 'withdraw' })).status, 200);
  assert.equal((await status(id)).status, 'rejected');
  // the claim could not be given back either (storage down): the next withdraw finishes it
  const id2 = await draft('withdraw-stuck', 'approved', { caption: 'w' });
  const d2 = await loadDraft(id2);
  mem.set(`claims/${id2}/${d2.approved_ms}.json`, { buf: Buffer.from(JSON.stringify({ at: Date.now(), run: 'withdrawn by ed@example.com' })),
                                                   contentType: 'application/json', uploadedAt: new Date() });
  assert.equal((await editor.call('POST', '/api/decide', { id: id2, decision: 'withdraw' })).status, 200);
  assert.equal((await status(id2)).status, 'rejected');
  // or the next publisher run that finds it finishes it, with the withdraw's reason
  const id5 = await draft('withdraw-stuck-run', 'approved', { caption: 'w' });
  const d5 = await loadDraft(id5);
  mem.set(`claims/${id5}/${d5.approved_ms}.json`, { buf: Buffer.from(JSON.stringify({ at: Date.now(), run: 'withdrawn by ed@example.com', reason: 'Withdrawn' })),
                                                   contentType: 'application/json', uploadedAt: new Date() });
  const run = await robot.call('POST', '/api/result', { id: id5, status: 'publishing' }, R);
  assert.deepEqual([run.status, run.body.error], [409, 'withdrawn by an editor: never schedule it']);
  assert.deepEqual([(await status(id5)).status, (await status(id5)).status_info.by, (await status(id5)).status_info.withdrawn], ['rejected', 'ed@example.com', true]);
  // a run's claim is a run's: too late
  const id3 = await draft('withdraw-run', 'approved', { caption: 'w' });
  assert.equal((await robot.call('POST', '/api/result', { id: id3, status: 'publishing' }, R)).status, 200);
  assert.equal((await editor.call('POST', '/api/decide', { id: id3, decision: 'withdraw' })).status, 409);
  // a hold whose status write failed gives its claim back too: the next run can have the post
  const id4 = await draft('hold-fail', 'approved', { caption: 'h' });
  undo = refuse(k => k.startsWith(`drafts/${id4}/s-`));
  try { assert.ok((await robot.call('POST', '/api/result', { id: id4, status: 'held', error: 'connector not attached', scope: 'run' }, R)).status >= 500); }
  finally { undo(); }
  assert.equal((await robot.call('POST', '/api/result', { id: id4, status: 'publishing' }, R)).status, 200);
});

test('Clear and Not okay on selected posts someone decided meanwhile say why they were not changed', async () => {
  const a = await draft('bulk-approved'), b = await draft('bulk-claimed'), c = await draft('bulk-fine');
  assert.equal((await owner.call('POST', '/api/decide', { id: a, decision: 'approve', caption: 'x' })).status, 200);
  assert.equal(await claimDecision(b, (await status(b)).status_info.at, 'owner@example.com', 'approve'), null);
  const clr = await editor.call('POST', '/api/drafts?action=clear', { ids: [a, b] });
  assert.equal(clr.body.cleared, 0);
  assert.deepEqual(clr.body.skipped.sort((x, y) => x.id.localeCompare(y.id)),
                   [{ id: a, why: 'Someone already approved this post (see Queued)' }, { id: b, why: 'already approved by owner@example.com just now' }]);
  const no = await editor.call('POST', '/api/decide', { ids: [a, b, c], decision: 'reject_many', reason: 'Duplicate' });
  assert.deepEqual([no.body.rejected, no.body.ids], [1, [c]]);
  assert.deepEqual(no.body.skipped, [{ id: a, why: 'Someone already approved this post (see Queued)' }, { id: b, why: 'already approved by owner@example.com just now' }]);
});
