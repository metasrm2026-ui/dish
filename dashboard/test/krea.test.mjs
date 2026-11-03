// One Krea allowance for the month, shared by the hourly routine and Studio sessions (Oct 23): a Studio
// session keeps no ledger, so its images were invisible to the hourly routine's cap.
import test from 'node:test';
import assert from 'node:assert/strict';
import { client } from './helpers.mjs';
import { getRecord, putRecord } from '../lib/db.js';

const R = { routine: true };
const robot = client();
// as the routine sends it: a reservation says it will be confirmed (an older routine's does not)
const ask = body => robot.call('POST', '/api/routine/krea', { confirms: true, ...body }, R);
const realNow = Date.now;
const stamp = (t = Date.now()) => ({ month: new Date(t).toISOString().slice(0, 7), day: new Date(t).toISOString().slice(0, 10) });
const daysLeftOf = (t = Date.now()) => { const n = new Date(t); return new Date(Date.UTC(n.getUTCFullYear(), n.getUTCMonth() + 1, 0)).getUTCDate() - n.getUTCDate() + 1; };
const hourly = (used, today, extra = {}) => ({ who: 'hourly', hourly_used: used, hourly_today: today, ...stamp(), ...extra });
// a day in the middle of this month: on its last day the month's cap and the day's share are the same
// images, and the cap is said first (these tests are about the day's share)
async function midMonth(fn) {
  const n = new Date(realNow());
  Date.now = () => Date.UTC(n.getUTCFullYear(), n.getUTCMonth(), 15, 12);
  try { return await fn(); } finally { Date.now = realNow; }
}
async function fresh(t = Date.now()) {                 // a month nobody has counted yet
  await putRecord('krea', stamp(t).month, { month: stamp(t).month, hourly_used: 0, hourly_today: {}, seq: 0, reported: false, res: [] });
}

test('the hourly routine and Studio draw from one monthly allowance, spread over the days', async () => {
  await fresh();
  const cap = 100 * daysLeftOf();                                // 100 images a day, whatever the date
  // the hourly routine's ledger: 50 images this month before today, 3 today
  let r = await ask({ ...hourly(53, 3), cap });
  assert.equal(r.status, 200);
  assert.deepEqual([r.body.used, r.body.today], [54, 4]);
  // a Studio image counts on top of the hourly ledger (which does not know it), and so does the
  // hourly image reserved since the ledger's report (not made yet when it reported)
  r = await ask({ who: 'studio', cap });
  assert.deepEqual([r.body.used, r.body.today, r.body.studio], [55, 5, 1]);
  // the hourly routine reports again: its ledger now has that image (54, 4); the ledger never goes back down
  r = await ask({ ...hourly(54, 4), cap });
  assert.deepEqual([r.body.used, r.body.today], [56, 6]);
  r = await ask({ ...hourly(10, 1), cap });
  assert.deepEqual([r.body.used, r.body.today], [57, 7]);
  // the month's cap: refused, whoever asks
  r = await ask({ ...hourly(53, 3), cap: 57 });
  assert.equal(r.status, 409);
  assert.match(r.body.error, /^KREA CAP: 57\/57/);
  assert.equal((await ask({ who: 'studio' })).status, 400, 'the cap comes from the routine config');
});

test('the day\'s share is spread over the days left, and earlier days count for the month only', async () => {
  await midMonth(async () => {
    const t = Date.now(), { month, day } = stamp(t), daysLeft = daysLeftOf(t);
    const cap = 10 * daysLeft + 40;                                  // 40 used before today: 10 a day left
    // Studio images of earlier days (the month's record keeps every Studio image)
    await putRecord('krea', month, { month, base: 30, hourly_used: 30, hourly_today: {}, seq: 10, reported: true,
      res: Array.from({ length: 10 }, (_, i) => ({ n: i + 1, at: t - 86400e3 - i, who: 'studio', day: '1999-01-01', made: true })) });
    const answers = [];
    for (let i = 0; i < 12; i++) answers.push(await ask({ who: 'studio', cap }));
    assert.deepEqual(answers.map(a => a.status), [...Array(10).fill(200), 409, 409]);
    assert.deepEqual([answers[9].body.used, answers[9].body.today, answers[9].body.allowed], [50, 10, 10]);
    assert.match(answers[10].body.error, /^KREA PACE: 10\/10 images today/);
    assert.equal((await getRecord('krea', month)).res.filter(r => r.day === day).length, 10);
  });
});

test('two sessions never both take the last image (checked and reserved in one write)', async () => {
  await midMonth(async () => {
    await fresh();
    const cap = 100 * daysLeftOf();
    await ask({ ...hourly(97, 97), report_only: true, cap });         // 3 left today
    const answers = await Promise.all(Array.from({ length: 12 }, (_, i) => ask({ who: i % 2 ? 'studio' : 'hourly', cap, key: `k${i}` })));
    const granted = answers.filter(a => a.status === 200);
    assert.equal(granted.length, 3, JSON.stringify(answers.map(a => [a.status, a.body.error || a.body.today])));
    assert.ok(answers.every(a => a.status === 200 || /^KREA PACE|being changed/.test(a.body.error)));
    assert.equal((await getRecord('krea', stamp().month)).res.length, 3);
    const after = await ask({ who: 'studio', cap });
    assert.match(after.body.error, /^KREA PACE: 100\/100/);
  });
});

test('the same ask twice (a lost answer, the step run again) is one reservation', async () => {
  await fresh();
  const cap = 100 * daysLeftOf();
  await ask({ ...hourly(0, 0), report_only: true, cap });
  const a = await ask({ who: 'studio', cap, key: 'abc-0' });
  const b = await ask({ who: 'studio', cap, key: 'abc-0' });
  assert.deepEqual([a.status, b.status, b.body.again], [200, 200, true]);
  assert.deepEqual([a.body.used, b.body.used], [1, 1]);
  const c = await ask({ who: 'studio', cap, key: 'abc-1' });       // the next attempt is a new image
  assert.equal(c.body.used, 2);
  assert.equal((await getRecord('krea', stamp().month)).res.length, 2);
});

test('Studio waits for the hourly routine to count the month; a report reserves nothing', async () => {
  await fresh();
  const cap = 100 * daysLeftOf();
  let r = await ask({ who: 'studio', cap });
  assert.equal(r.status, 409);
  assert.match(r.body.error, /^KREA UNCHECKED/);
  r = await ask({ who: 'hourly', cap });                           // an hourly ask without its ledger is no count either
  assert.equal(r.status, 200);
  assert.equal((await ask({ who: 'studio', cap })).status, 409);
  r = await ask({ ...hourly(7, 2), report_only: true, cap });     // its ledger has the image reserved above
  assert.deepEqual([r.status, r.body.used, r.body.today], [200, 7, 2]);
  const rec = await getRecord('krea', stamp().month);
  assert.deepEqual([rec.reported, rec.res.length, rec.base], [true, 1, 6], 'the reservation is not counted twice');
  assert.equal((await ask({ who: 'studio', cap })).status, 200);
});

test('a count from another month or day is not this one\'s (received after midnight)', async () => {
  const t0 = Date.UTC(2026, 10, 1, 0, 0, 1);                       // Nov 1, 00:00:01 UTC
  Date.now = () => t0;
  try {
    await fresh(t0);
    const cap = 1260;
    // counted at 23:59:59 on Oct 31: the whole of October, 41 of them that day
    let r = await ask({ who: 'hourly', cap, hourly_used: 1255, hourly_today: 41, month: '2026-10', day: '2026-10-31' });
    assert.deepEqual([r.status, r.body.used, r.body.today], [200, 1, 1]);
    // the same day's count, sent with yesterday's date: the month's count is kept, today's is not
    Date.now = () => t0 + 3600e3;
    r = await ask({ who: 'hourly', cap, hourly_used: 5, hourly_today: 5, month: '2026-11', day: '2026-10-31', report_only: true });
    assert.deepEqual([r.body.used, r.body.today], [5, 0]);
    r = await ask({ who: 'hourly', cap, hourly_used: 5, hourly_today: 5, month: '2026-11', day: '2026-11-01', report_only: true });
    assert.deepEqual([r.body.used, r.body.today], [5, 5]);
    assert.equal((await ask({ who: 'studio', cap })).status, 200);
  } finally {
    Date.now = realNow;
  }
});

const made = (who, key, cap) => ask({ who, cap, key, made: true });
async function pinned(t, fn) {                                     // Date.now held at t (and moved by fn)
  let now = t;
  Date.now = () => now;
  try { await fn(ms => { now += ms; }); } finally { Date.now = realNow; }
}

test('a reservation never said to be made lapses after 45 minutes; a made image counts all month', async () => {
  const t = Date.UTC(2026, 9, 10, 9);
  await pinned(t, async later => {
    await fresh(t);
    const cap = 100 * daysLeftOf(t);
    await ask({ ...hourly(10, 0, stamp(t)), report_only: true, cap });
    for (let i = 0; i < 3; i++) await ask({ who: 'hourly', cap, key: `h${i}` });   // three asked, one made
    await ask({ who: 'studio', cap, key: 's0' });
    await ask({ who: 'studio', cap, key: 's1' });
    assert.equal((await made('hourly', 'h0', cap)).body.used, 15);
    assert.equal((await made('hourly', 'h0', cap)).body.used, 15, 'said twice: one image');
    await made('studio', 's0', cap);
    later(44 * 60e3);
    assert.equal((await ask({ ...hourly(10, 0, stamp(t)), report_only: true, cap })).body.used, 15, 'not yet');
    later(2 * 60e3);
    let r = await ask({ ...hourly(10, 0, stamp(t)), report_only: true, cap });
    assert.deepEqual([r.body.used, r.body.today, r.body.studio, r.body.studio_today], [12, 2, 1, 1]);
    assert.deepEqual((await getRecord('krea', stamp(t).month)).res.map(x => x.key), ['h0', 's0']);
    // an image made after its reservation lapsed still counts, and saying so is never refused
    r = await made('hourly', 'h1', 12);
    assert.deepEqual([r.status, r.body.used], [200, 13]);
    later(20 * 86400e3);                                           // later in the month: still counted
    r = await ask({ ...hourly(10, 0, stamp(Date.now())), report_only: true, cap });
    assert.deepEqual([r.body.used, r.body.today], [13, 0]);
    assert.equal((await made('studio', '', cap)).status, 400, 'which image: its key');
  });
});

test('images made by a run whose state was never saved still count (its ledger is restored without them)', async () => {
  const t = Date.UTC(2026, 9, 10, 9);
  await pinned(t, async later => {
    await fresh(t);
    const cap = 1260;
    const card = async (ledger, k) => {                            // ROUTINE.md step 4: one card at a time
      const r = await ask({ ...hourly(ledger, ledger - 90, stamp(Date.now())), cap, key: k });
      await made('hourly', k, cap);
      later(10 * 60e3);
      return r.body.used;
    };
    await ask({ ...hourly(100, 10, stamp(t)), report_only: true, cap });
    // run N makes 3 images and dies before `state push`
    assert.deepEqual([await card(100, 'a0'), await card(101, 'a1'), await card(102, 'a2')], [101, 102, 103]);
    // run N+1 restores the ledger of 100 and makes 5
    const got = [];
    for (let i = 0; i < 5; i++) got.push(await card(100 + i, `b${i}`));
    assert.deepEqual(got, [104, 105, 106, 107, 108]);
    later(3600e3);
    const r = await ask({ ...hourly(105, 15, stamp(Date.now())), report_only: true, cap });
    assert.deepEqual([r.body.used, r.body.today], [108, 18], 'every image made, and every one today');
    // two runs at once from the same saved state: each one's images count
    const a = await ask({ ...hourly(105, 15, stamp(Date.now())), cap, key: 'c0' });
    const b = await ask({ ...hourly(105, 15, stamp(Date.now())), cap, key: 'd0' });
    await made('hourly', 'd0', cap); await made('hourly', 'c0', cap);
    later(3600e3);
    assert.deepEqual([a.body.used, b.body.used, (await ask({ ...hourly(106, 16, stamp(Date.now())), report_only: true, cap })).body.used],
                     [109, 110, 110]);
  });
});

test('images made without the count (the dashboard down) are counted from the ledger once it passes', async () => {
  const t = Date.UTC(2026, 9, 10, 9);
  await pinned(t, async () => {
    await fresh(t);
    const cap = 1260;
    await ask({ ...hourly(100, 10, stamp(t)), report_only: true, cap });
    await ask({ who: 'hourly', cap, key: 'a0' }); await made('hourly', 'a0', cap);
    const r = await ask({ ...hourly(120, 30, stamp(t)), report_only: true, cap });   // 19 more on its own ledger
    assert.deepEqual([r.body.used, r.body.today], [120, 30]);
  });
});

test('the README reset: every image of the month counted as made before it, the count right after it', async () => {
  const t = Date.UTC(2026, 9, 20, 9), { month } = stamp(t);
  await pinned(t, async later => {
    // hourly ledger 800 (10 today), Studio 100: Krea's usage page says 900
    await putRecord('krea', month, { month, base: 800, studio_base: 100, base_today: {}, hourly_used: 0, hourly_today: {}, seq: 0, reported: true, res: [] });
    const cap = 1260 + 900;                                        // the pace never stops this test
    let r = await ask({ ...hourly(800, 10, stamp(t)), report_only: true, cap });
    assert.deepEqual([r.body.used, r.body.today], [900, 10]);
    for (let i = 0; i < 120; i++) {
      r = await ask({ ...hourly(800 + i, 10 + i, stamp(Date.now())), cap, key: `k${i}` });
      await made('hourly', `k${i}`, cap);
      if (i % 3 === 2) { await ask({ who: 'studio', cap, key: `s${i}` }); await made('studio', `s${i}`, cap); }
      later(5 * 60e3);
    }
    r = await ask({ ...hourly(920, 130, stamp(Date.now())), report_only: true, cap });
    assert.deepEqual([r.body.used, r.body.studio], [900 + 120 + 40, 100 + 40]);
  });
});

test('after the README reset, images the hourly routine makes while the dashboard is down count at once', async () => {
  const t = Date.UTC(2026, 9, 20, 9), { month } = stamp(t);
  await pinned(t, async later => {
    await putRecord('krea', month, { month, base: 800, studio_base: 100, base_today: {}, hourly_used: 0, hourly_today: {}, seq: 0, reported: true, res: [] });
    const cap = 1260 + 900;
    let r = await ask({ ...hourly(800, 10, stamp(t)), report_only: true, cap });
    assert.deepEqual([r.body.used, r.body.studio], [900, 100]);
    later(3600e3);                                                 // 30 made on its own ledger, unreserved
    r = await ask({ ...hourly(830, 40, stamp(Date.now())), report_only: true, cap });
    assert.deepEqual([r.body.used, r.body.today], [930, 40]);
  });
});

test('a Studio session on an older routine (it never confirms) has its images counted all month', async () => {
  const t = Date.UTC(2026, 9, 10, 9);
  await pinned(t, async later => {
    await fresh(t);
    const cap = 100 * daysLeftOf(t);
    await ask({ ...hourly(10, 0, stamp(t)), report_only: true, cap });
    await robot.call('POST', '/api/routine/krea', { who: 'studio', cap, key: 'old' }, R);
    await ask({ who: 'studio', cap, key: 'new' });                   // a newer session, never made
    later(46 * 60e3);
    const r = await ask({ ...hourly(10, 0, stamp(t)), report_only: true, cap });
    assert.deepEqual([r.body.used, r.body.studio], [11, 1]);
    assert.deepEqual((await getRecord('krea', stamp(t).month)).res.map(x => x.key), ['old']);
  });
});

test('a record of the earlier count keeps its Studio images; its pending hourly ones lapse', async () => {
  const t = Date.UTC(2026, 9, 23, 9), { month, day } = stamp(t);
  await pinned(t, async later => {
    await putRecord('krea', month, { month, hourly_used: 500, hourly_today: { [day]: 5 }, seq: 30, hourly_seq: 28, reported: true,
      res: [...Array.from({ length: 28 }, (_, i) => ({ n: i + 1, at: t - 86400e3, who: 'studio', day: '2026-10-22' })),
            { n: 29, at: t, who: 'hourly', day }, { n: 30, at: t, who: 'studio', day }] });
    const cap = 1260;
    assert.equal((await ask({ who: 'studio', cap, key: 's' })).body.used, 500 + 29 + 1);   // its ledger had the hourly one
    later(3600e3);
    const r = await ask({ ...hourly(501, 6, stamp(Date.now())), report_only: true, cap });
    assert.deepEqual([r.body.used, r.body.studio], [501 + 29, 29], 'the new Studio ask was never made');
    assert.equal((await getRecord('krea', month)).res.filter(x => x.who === 'hourly').length, 0);
    assert.equal((await getRecord('krea', month)).hourly_seq, undefined);
  });
});

test('a routine older than this count (its ledger without the month) is refused: it counts on its own', async () => {
  await fresh();
  const r = await ask({ who: 'hourly', cap: 1260, hourly_used: 1260, hourly_today: 40, key: 'old-0' });
  assert.equal(r.status, 400);
  assert.match(r.body.error, /which month/);
  assert.equal((await getRecord('krea', stamp().month)).res.length, 0);
});

