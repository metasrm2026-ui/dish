// GET /api/health (any signed-in user): is the pipeline working?
//   {status: 'green'|'amber'|'red', reasons: [{code, level, text, since, ids}], checked_at}
// Worked out first from what the system already stores (draft statuses, state saves, story feeds,
// Studio jobs, editors' requests), so it works even when a routine dies without a word; then from
// the run reports bore/ops.py writes for every `python -m bore.cli` command (saveRun below).
// Reads are bounded: listings, a few small records, a capped number of reports, at most 6 at a
// time (lib/db.js mapLimit: a burst of parallel storage reads got the store challenged on Oct 21);
// the result is kept 5 minutes and worked out by one request at a time in the whole deployment
// (the others are given the last status meanwhile), so open pages polling it never add up to a
// burst of reads.
// Texts carry page names, counts and times; ids go in `ids`. Never source names or document text.
// `admin`: what an admin checks (a setting's name), shown in the admin Health tab only.
// The routines' side (routes/routine.js): POST runs, GET health (the GitHub health workflow) and
// POST watchdog (Studio jobs restarted or failed whether or not an editor has the batch open).
import { getRecord, putRecord, listRecords, listBlobs, putFile, readBlob, blobKey, mapLimit, StorageChallenged } from '../lib/db.js';
import { put, del, list } from '../lib/blob.js';
import { listAll, groupDrafts, getJson, pageOf } from '../lib/store.js';
import { decryptJson } from '../lib/crypto.js';
import { clean } from '../lib/http.js';
import { isAdmin } from '../lib/auth.js';
import { pageScope, idPage } from '../lib/tenancy.js';
import { socialpilotHealth } from './result.js';
import { nudgeJobs } from './studio.js';
import { nudgeTemplates } from './templates.js';

const M = 60e3, H = 3600e3;
const CACHE_MS = 5 * M;

// one record, report or log entry by its listing (no second listing). Through lib/db.js: these
// never change once written, so each instance keeps the ones it read; refusals are retried, and
// reads pause while storage runs a security check (more reads only prolong it)
const readAt = x => readBlob(x.url, blobKey(x), decryptJson);
// a read that failed on its own (a 404, a broken entry) is left out; storage refusing reads is
// not: that check is then unreadable, and says so (computeHealth), instead of quietly finding less
const soft = p => p.catch(e => { if (e instanceof StorageChallenged) throw e; return null; });
const why = e => (e instanceof StorageChallenged ? (e.checkpoint ? 'storage is running a security check on its reads'
  : `storage is not answering (${e.status || 'no connection'})`) : clean(noUrls(e && e.message), 80));
// storage refusing reads (a security check, a rate limit: lib/db.js then pauses reads), not a 5xx
// that lasted through a read's retries (one blip must not open the owner's issue)
const refusing = e => e instanceof StorageChallenged && (e.checkpoint || e.status === 429);

const dayOf = t => new Date(t).toISOString().slice(0, 10);
const hhmm = t => new Date(t).toISOString().slice(11, 16);
function age(ms) {
  const m = Math.max(0, Math.round(ms / M));
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)}h${m % 60 ? ` ${m % 60}m` : ''}`;
}
const noUrls = s => String(s || '').replace(/https?:\/\/\S+/g, '<url>');

// ------------------------------------------------------------------ run reports (bore/ops.py)
//   runs/<day>/<started ms>_<routine>_<command>_<run id>_<session>/start.enc | end-<ok|amber|fail>.enc
// How each run ended, and the routine session it was part of, are in its path, so the health rules
// read listings, not every report.
const RUN_RE = /^runs\/\d{4}-\d{2}-\d{2}\/(\d{13})_([a-z0-9-]+)_([a-z0-9.-]+)_([A-Za-z0-9]+)(?:_([A-Za-z0-9]+))?\/(start|end-(ok|amber|fail))\.enc$/;
const slug = (s, n) => clean(s, n).toLowerCase().replace(/[^a-z0-9.-]+/g, '.').replace(/^\.+|\.+$/g, '');
const cmdText = r => r.cmd.replace(/\./g, ' ');

export async function saveRun(b, now = Date.now()) {
  const runId = clean(b.run_id, 40).replace(/[^A-Za-z0-9]/g, '');
  const session = clean((b.summary && b.summary.session) || b.session, 20).replace(/[^A-Za-z0-9]/g, '') || runId;
  const started = Number(b.started_at) || 0, ended = b.ended_at ? Number(b.ended_at) || now : null;
  const command = slug(b.command, 60);
  if (!runId || !command) return { error: 'run_id and command are required' };
  if (!(started > 1e12 && started < now + H)) return { error: 'started_at must be a time in milliseconds' };
  const routine = slug(b.routine, 20).replace(/\./g, '-') || 'setup';
  const degraded = (Array.isArray(b.degraded) ? b.degraded : []).slice(0, 20).map(x => clean(noUrls(x), 120)).filter(Boolean);
  const ok = ended ? b.ok === true : null;             // computed by the code: anything else is a failure
  let summary = b.summary && typeof b.summary === 'object' ? b.summary : {};
  if (JSON.stringify(summary).length > 8000) summary = { truncated: true };
  const state = !ended ? 'start' : `end-${!ok ? 'fail' : degraded.length ? 'amber' : 'ok'}`;
  const rec = { run_id: runId, routine, command: clean(b.command, 60), started_at: started, ended_at: ended, ok, degraded, summary };
  const path = `runs/${dayOf(started)}/${started}_${routine}_${command}_${runId}_${session}/${state}.enc`;
  try {
    await putFile(path, Buffer.from(JSON.stringify(rec)));
  } catch (e) {
    if (!/exist/i.test(String(e.message))) throw e;
    return { saved: false, duplicate: true };          // a report sent twice
  }
  if (Math.random() < 0.1) await pruneRuns(now, { some: true }).catch(() => {});
  return { saved: true, state };
}

// A week of reports is kept. A report prunes a little: one listing page of the oldest day due and
// one delete, a few storage calls (a whole day's 1,700 reports took 22 in a row, past the 6 seconds
// bore/ops.py waits for its answer, and it then stops reporting for 10 minutes). The hourly
// watchdog prunes whole days; the reports alone keep up too (about 170 draws of 100 a day).
async function pruneRuns(now, { some = false } = {}) {
  for (const n of [8, 9, 10]) {
    const prefix = `runs/${dayOf(now - n * 86400e3)}/`;
    if (some) {
      const page = (await list({ prefix, limit: 100 })).blobs.slice(0, 100);     // (the limit, also where a store ignores it)
      if (page.length) return del(page.map(x => x.url));
      continue;
    }
    const old = await listBlobs(prefix);
    for (let i = 0; i < old.length; i += 100) await del(old.slice(i, i + 100).map(x => x.url));
  }
}

export async function runIndex(now, hours) {
  const days = [];
  for (let t = now - hours * H; dayOf(t) <= dayOf(now); t += 86400e3) days.push(dayOf(t));   // each UTC day the hours touch
  const runs = {};
  for (const d of days) for (const x of await listBlobs(`runs/${d}/`)) {
    const m = x.pathname.match(RUN_RE);
    if (!m) continue;
    const r = (runs[`${m[1]}_${m[4]}`] ||= { started: Number(m[1]), routine: m[2], cmd: m[3], run_id: m[4], session: m[5] || m[4],
                                             start: false, end: null, blob: null });
    if (m[6] === 'start') r.start = true;
    else { r.end = m[7]; r.blob = x; }
  }
  return Object.values(runs).filter(r => r.started >= now - hours * H).sort((a, c) => c.started - a.started);
}

// GET /api/routine/runs?hours=: the reports of the last hours (at most 48), the newest 20 that
// did not end well in full (diagnostic sessions read this, not many records in parallel)
export async function listRuns(hours, now = Date.now()) {
  const runs = await runIndex(now, Math.min(Math.max(hours, 1), 48));
  const bad = runs.filter(r => r.end && r.end !== 'ok').slice(0, 20);
  const reports = (await mapLimit(bad, 4, r => soft(readAt(r.blob)))).filter(Boolean);
  return { runs: runs.map(r => ({ run_id: r.run_id, routine: r.routine, command: cmdText(r), session: r.session, started_at: r.started,
                                  state: r.end || (r.start ? 'started' : '') })), reports };
}

// ------------------------------------------------------------------ the rules
function whyFailed(error) {
  const e = noUrls(error).trim();
  const m = e.match(/^([A-Z]\w*(Error|Exception|Exit|Interrupt|Exceeded))\b/);
  return m ? m[1] : clean(e, 100);
}

async function hourlyRules(now, add, drafts) {
  const states = await listBlobs('state/');
  const lastPush = Math.max(0, ...states.map(x => +new Date(x.uploadedAt)));
  const made = drafts.filter(d => !/-studio-/.test(d.id)).map(d => d.createdAt * 1000);
  const newest = Math.max(0, ...made);
  const last = Math.max(lastPush, newest);
  if (!last) return;                                   // a new install: nothing to compare with yet
  if (now - last > 2 * H)
    add('hourly_stale', 'red', `No hourly post run seen for ${age(now - last)}: the newest draft is ${newest ? `${age(now - newest)} old` : 'not there'}`
        + ` and the state ${lastPush ? `was last saved ${age(now - lastPush)} ago` : 'has not been saved yet'}.`, last + 2 * H);
  else if (lastPush && now - lastPush > 2 * H)
    add('state_not_saved', 'amber', `The hourly routine made drafts but has not saved its state for ${age(now - lastPush)}: its last step is failing.`,
        lastPush + 2 * H);
}

async function topicRules(now, add, pages) {
  const newest = {};
  for (const x of await listBlobs('db/feed/')) {
    const m = x.pathname.match(/^db\/feed\/([^/]+)\//);
    if (m) newest[m[1]] = Math.max(newest[m[1]] || 0, +new Date(x.uploadedAt));
  }
  for (const p of pages) {
    const t = newest[p.slug];
    if (!t || now - t <= 90 * M) continue;              // a page that never had a feed is not a stale one
    add('topics_stale', now - t > 3 * H ? 'red' : 'amber', `The Studio story list for ${p.name || p.slug} was last refreshed ${age(now - t)} ago.`,
        t + 90 * M, [p.slug]);
  }
}

// The publisher's notes for a person are urgent: what `publish mark` found in SocialPilot's queue
// (bore/publish.py verify_queued: a copy on an account it must never use, a second copy, the wrong
// time, a changed caption) and what a publisher session recorded itself (PUBLISH.md step 5, e.g. a
// second copy it found) go out unless someone fixes or deletes them. Only the code's own notes that
// ask a person to look are not, in their words (bore/cli.py unconfirmed, routes/result.js hold and
// missing; tests/test_ops.py checks they still match): a note in any other words is urgent.
export const CHECK_NOTE = /^(SocialPilot's queue did not show it after CreatePost|look on the Facebook page: SocialPilot has no record of this post|look in SocialPilot and on the Facebook page: the publisher could not check)/;
const HOLD_HOURS = () => Number(process.env.PUBLISH_HOLD_HOURS || 12);     // routes/result.js: then a held post fails

async function publisherRules(now, add, drafts, person) {
  const sp = await socialpilotHealth();
  const unclaimed = [], held = [], stuck = [], late = [];
  for (const d of drafts) {
    const st = d.statuses, last = st[st.length - 1];
    if (d.status === 'approved') {
      let k = st.length - 1;
      while (k > 0 && st[k - 1].status === 'approved') k--;
      const first = st[k].at;                          // a held draft is approved again at each try
      if (st.length - k >= 2 || sp.down) { if (now - first > 2 * H) held.push([d.id, first]); }
      else if (now - last.at > 90 * M) unclaimed.push([d.id, last.at]);
    }
    if (d.status === 'publishing' && now - last.at > 45 * M) stuck.push([d.id, last.at]);
    if (d.status === 'scheduled' && now - last.at > 3 * H) late.push(d);
  }
  // scheduled posts: their time is in the status record. The oldest 20 are read: a post is scheduled
  // for a time after its status was written, so the oldest status is the first that can be overdue
  // (a day's queue of later posts must not hide it)
  const maybe = late.sort((a, c) => a.statuses.at(-1).at - c.statuses.at(-1).at).slice(0, 20);
  const bodies = await mapLimit(maybe, 4, d => soft(getJson(d.statuses.at(-1).url, d.statuses.at(-1).key)));
  const unconfirmed = maybe.map((d, i) => [d.id, Date.parse(((bodies[i] || {}).result || {}).scheduled_for || '') || 0])
    .filter(([, t]) => t && now - t > 3 * H);
  const ids = xs => xs.map(x => x[0]), since = xs => Math.min(...xs.map(x => x[1]));
  if (unclaimed.length)
    add('publisher_unclaimed', 'red', `${unclaimed.length} approved post(s) waited more than 90 minutes without the publisher picking them up: `
        + 'the publisher routine may not be running.', since(unclaimed) + 90 * M, ids(unclaimed));
  if (stuck.length)
    add('publisher_stuck', 'red', `${stuck.length} post(s) have been "publishing" for more than 45 minutes: a publisher run claimed them and `
        + 'never reported back (check SocialPilot before anyone retries them).', since(stuck) + 45 * M, ids(stuck));
  if (unconfirmed.length)
    add('publisher_unconfirmed', 'amber', `${unconfirmed.length} scheduled post(s) are more than 3 hours past their time and not confirmed as posted.`,
        since(unconfirmed) + 3 * H, ids(unconfirmed));
  // a held run is the publisher's answer, not a failed run (bore/ops.py): this is how an outage
  // reaches the owner, red after 4 hours (a held post fails HOLD_HOURS after its approval)
  const long = held.length && now - since(held) > 4 * H;
  if (held.length)
    add('publisher_held', long ? 'red' : 'amber', `${held.length} approved post(s) have waited more than ${long ? 4 : 2} hours for SocialPilot`
        + `${sp.down ? ' (SocialPilot is unavailable)' : ''}${long ? `: they move to Failed ${HOLD_HOURS()} hours after approval` : ''}.`,
        since(held) + 2 * H, ids(held));
  // what the publisher left for a person (routes/result.js needsAction, one small record), read
  // last: storage refusing it must not cost the rules above
  const rec = await getRecord('health', 'needs_action');
  const week = now - 7 * 86400e3;                       // as needsAction: a note nobody marked done drops out after a week
  person.notes = Object.entries((rec && rec.items) || {}).filter(([, x]) => x && x.at >= week);
}

// What only a person can do, the publisher's own reason (never a degraded run): its notes - red
// unless the code only asks a person to look (CHECK_NOTE) - kept until someone marks them done on
// the review page; and the latest `publish plan` report's outdated originals and pages with no
// account. Counts in the text, draft ids in ids, never the notes' words (they name SocialPilot accounts).
function personRule(add, { notes = [], plan = null }) {
  const noted = new Set(notes.map(([id]) => id));
  const orig = notes.filter(([, x]) => x.kind === 'superseded_original').map(([id]) => id)
    .concat(((plan && plan.orig) || []).filter(id => !noted.has(id)));
  const look = notes.filter(([, x]) => x.kind !== 'superseded_original' && CHECK_NOTE.test(String(x.text || ''))).map(([id]) => id);
  const urgent = [...noted].filter(id => !orig.includes(id) && !look.includes(id));
  const none = ((plan && plan.none) || []).filter(id => !noted.has(id));
  if (!urgent.length && !look.length && !orig.length && !none.length) return;
  add('publisher_needs_person', urgent.length ? 'red' : 'amber', [
    urgent.length && `${urgent.length} post(s) need a person in SocialPilot now: what the publisher found there goes out against the publishing `
      + 'rules unless someone fixes or deletes it (e.g. a second copy, the wrong account or time)',
    look.length && `${look.length} post(s) need a person to check them in SocialPilot or on the Facebook page`,
    orig.length && `${orig.length} outdated original post(s) are still scheduled in SocialPilot: a person deletes them there (their updates replace them)`,
    none.length && `${none.length} approved post(s) are for a page with no SocialPilot account: an admin sets it in the admin panel`,
  ].filter(Boolean).join('; ') + ' (the review page lists them).',
  Math.min(...notes.map(([, x]) => x.at), plan ? plan.at : Infinity), [...urgent, ...look, ...orig, ...none], '',
  { person: { urgent, other: [...look, ...orig, ...none] } });   // every id, for forUser (ids is cut to 12)
}

// Studio jobs made in the last 24 hours (ids carry their creation time: <page>-<ms>-<rand>, where
// <rand> is base64url and can hold '-' too), the newest 60, read 6 at a time. olderThan: only jobs
// not written for that long (a job written since cannot be overdue).
export async function recentJobs(now, olderThan = 0) {
  const slugs = [...new Set((await listBlobs('db/pages/')).map(x => (x.pathname.match(/^db\/pages\/([^/]+)\//) || [])[1]).filter(Boolean))];
  const prefixes = [...new Set([now - 24 * H, now].map(t => String(Math.floor(t / 1e8))))];
  const latest = {};
  for (const s of slugs) for (const p of prefixes) for (const x of await listBlobs(`db/jobs/${s}-${p}`)) {
    const m = x.pathname.match(/^db\/jobs\/([^/]+)\//);
    if (m && (!latest[m[1]] || latest[m[1]].x.pathname < x.pathname)) latest[m[1]] = { x, s };
  }
  const cands = Object.entries(latest).map(([id, { x, s }]) => ({ x, created: Number((id.slice(s.length + 1).match(/^(\d{13})-/) || [])[1]) || 0 }))
    .filter(c => c.created >= now - 24 * H && (!olderThan || now - new Date(c.x.uploadedAt) >= olderThan))
    .sort((a, c) => c.created - a.created).slice(0, 60);
  return (await mapLimit(cands, 6, c => soft(readAt(c.x)))).filter(j => j && !j._deleted);
}

async function studioRules(now, add) {
  const jobs = await recentJobs(now);
  const waitFrom = j => Math.max(j.created_at || 0, j.released_at || 0);          // a handed-back job waits again
  // a templates conversion waiting is no editor waiting: it is restarted and failed on its own (routes/templates.js)
  const queued = jobs.filter(j => j.status === 'queued' && j.kind !== 'templates' && now - waitFrom(j) > 10 * M);
  const stuck = jobs.filter(j => j.status === 'running' && now - (j.started_at || 0) > 25 * M);
  // a wake call that failed (made when the job was queued) matters while that job still waits, in
  // its first hour: the 4-minute nudge starts the routine again, and a job nobody takes fails after
  // 5 of them. A job that was taken and finished is no reason for a day-long banner.
  const unwoken = jobs.filter(j => j.status === 'queued' && now - (j.created_at || 0) < H
                                   && (j.fired === false || (j.fire_status && !(j.fire_status >= 200 && j.fire_status < 300))));
  const first = (xs, f) => Math.min(...xs.map(f));
  if (queued.length)
    add('studio_queued', 'red', `${queued.length} Studio job(s) have waited more than 10 minutes for a Studio session: an editor is waiting.`,
        first(queued, waitFrom) + 10 * M, queued.map(j => j.id));
  if (stuck.length)
    add('studio_stuck', 'red', `${stuck.length} Studio job(s) have been running for more than 25 minutes: the session may have died.`,
        first(stuck, j => j.started_at || 0) + 25 * M, stuck.map(j => j.id));
  // what the trigger call got (lib/fire.js): an answer, an error (e.g. its 8 s timeout), or not made at all (not set up)
  const answers = [...new Set(unwoken.map(j => j.fire_status).filter(Boolean))];
  const errors = [...new Set(unwoken.map(j => !j.fire_status && j.fire_error && clean(noUrls(j.fire_error), 60)).filter(Boolean))];
  const got = [answers.length && `answered ${answers.join(', ')}`, errors.length && `failed (${errors.join('; ')})`,
               unwoken.some(j => !j.fire_status && !j.fire_error) && 'was not made (no trigger set up)'].filter(Boolean).join(', ');
  if (unwoken.length)                                  // editors see the text; the settings to check are the admin's
    add('studio_fire_failed', 'amber', `${unwoken.length} Studio job(s) are waiting: the Studio routine could not be started: the admin has been told.`,
        first(unwoken, j => j.created_at || now), unwoken.map(j => j.id),
        `The trigger call ${got}: check STUDIO_FIRE_URL and STUDIO_FIRE_TOKEN in Vercel, then deploy.`);
}

async function requestRules(now, add, drafts) {
  const links = {};
  for (const x of await listAll('links/')) {
    const m = x.pathname.match(/^links\/([^/]+)\/s-(\d+)-([a-z]+)\.json$/);
    if (m && (!links[m[1]] || links[m[1]].at < Number(m[2]))) links[m[1]] = { at: Number(m[2]), status: m[3] };
  }
  const waiting = Object.entries(links).filter(([, s]) => s.status === 'waiting' && now - s.at > 2 * H).map(([id, s]) => [id, s.at]);
  const regens = drafts.filter(d => d.status === 'regenerate' && now - d.statuses.at(-1).at > 2 * H).map(d => [d.id, d.statuses.at(-1).at]);
  for (const [code, xs, what] of [['link_waiting', waiting, "editors' link(s)"], ['regen_waiting', regens, 'regeneration request(s)']]) {
    if (!xs.length) continue;
    const first = Math.min(...xs.map(x => x[1]));
    add(code, now - first > 6 * H ? 'red' : 'amber', `${xs.length} ${what} have waited more than 2 hours (the oldest ${age(now - first)}): `
        + 'the hourly routine has not made them or said why not.', first + 2 * H, xs.map(x => x[0]));
  }
}

async function runRules(now, add, person) {
  const runs = (await runIndex(now, 6)).filter(r => r.routine !== 'manual');      // a developer's runs are not the pipeline
  // a session that went on to other commands did not die: only the report of this one's end was lost
  const lastOf = {};
  for (const r of runs) lastOf[r.session] = Math.max(lastOf[r.session] || 0, r.started);
  for (const r of runs) if (r.start && !r.end && now - r.started > 45 * M && !(lastOf[r.session] > r.started))
    add('run_unfinished', 'amber', `"${cmdText(r)}" (${r.routine}) started at ${hhmm(r.started)} UTC and never finished: its session may have died.`,
        r.started + 45 * M, [r.run_id]);
  const groups = {};
  for (const r of runs) if (r.end) (groups[`${r.routine} ${r.cmd}`] ||= []).push(r);        // newest first
  let budget = 24;                                                                          // reports read, in all
  const body = r => (budget-- > 0 ? soft(readAt(r.blob)) : null);
  // what the publisher cannot do without a person, from the latest publish plan's report (with its
  // notes: personRule), until it is done
  const plan = runs.find(r => r.routine === 'publisher' && r.cmd === 'publish.plan' && r.end && r.end !== 'fail');
  const s = plan && ((await body(plan)) || {}).summary;
  const listed = xs => (Array.isArray(xs) ? xs.filter(x => typeof x === 'string') : []);   // a summary is what the run sent
  if (s) person.plan = { orig: listed(s.delete_in_socialpilot), none: listed(s.no_account), at: plan.started };
  for (const all of Object.values(groups)) {
    // "in a row" counts routine runs (sessions), each by how its last run of the command ended: a
    // command run once per post or per job (upload, photos, studio done) must not turn red in one session
    const seen = new Set(), g = all.filter(r => !seen.has(r.session) && seen.add(r.session));
    const head = g[0], name = `"${cmdText(head)}" (${head.routine})`;
    let fails = g.findIndex(r => r.end !== 'fail');
    if (fails < 0) fails = g.length;
    if (fails) {
      const b = await body(head);
      const why = b && b.summary && b.summary.error ? `: ${whyFailed(b.summary.error)}` : '';
      add('run_failed', fails >= 2 ? 'red' : 'amber', `${name} failed${fails >= 2 ? ` in ${fails} runs in a row` : ''} (latest ${hhmm(head.started)} UTC)${why}.`,
          g[fails - 1].started, g.slice(0, fails).map(r => r.run_id));
    } else if (head.end === 'amber') {
      const last3 = g.slice(0, 3), streak = last3.length === 3 && last3.every(r => r.end === 'amber');
      const lists = (await mapLimit(streak ? last3 : [head], 3, body)).map(x => (x && x.degraded) || []);
      const same = streak ? lists[0].filter(x => lists[1].includes(x) && lists[2].includes(x)) : [];
      const rest = lists[0].filter(x => !same.includes(x));
      if (same.length) {
        add('run_degraded_repeat', 'red', `${name} ran degraded in 3 runs in a row: ${same.slice(0, 3).join('; ')}.`, last3[2].started, last3.map(r => r.run_id),
            '', adminOnly(same));
        // a repeat that is the admin's alone does not hide what else the latest run ran without
        if (adminOnly(same).admin_only && rest.length && !adminOnly(rest).admin_only)
          add('run_degraded', 'amber', `${name} ran degraded at ${hhmm(head.started)} UTC: ${rest.slice(0, 3).join('; ')}.`, head.started, [head.run_id]);
      } else
        add('run_degraded', 'amber', `${name} ran degraded at ${hhmm(head.started)} UTC${lists[0].length ? `: ${lists[0].slice(0, 3).join('; ')}` : ''}.`,
            head.started, [head.run_id], '', adminOnly(lists[0]));
    }
  }
}

// the run log the trigger prompts still write until the repo prompts replace them (routine.js)
async function runlogRules(now, add) {
  const since = now - 2 * H;
  const blobs = [];
  for (const d of [...new Set([since, now].map(dayOf))]) blobs.push(...await listBlobs(`log/runlog/${d}/`));
  const recent = blobs.map(x => ({ x, at: Number((x.pathname.match(/\/(\d{14})-[^/]+\.enc$/) || [])[1]) || 0 }))
    .filter(e => e.at >= since).sort((a, c) => c.at - a.at).slice(0, 20);
  const bad = (await mapLimit(recent, 4, e => soft(readAt(e.x)))).filter(e => e && e.ok === false);
  if (!bad.length) return;
  const w = s => clean(s, 30).replace(/[^\w +-]/g, '');
  add('runlog_failed', 'amber', `The run log has ${bad.length} failed step(s) in the last 2 hours (latest: ${w(bad[0].routine)} "${w(bad[0].step)}" at ${hhmm(bad[0].at)} UTC).`,
      bad.at(-1).at);
}

async function watchdogRules(now, add) {
  const w = await getRecord('health', 'watchdog');
  if (w && now - (w.at || 0) > 2 * H)
    add('watchdog_silent', 'amber', `The hourly health check (GitHub Actions) has not run for ${age(now - (w.at || 0))}.`, (w.at || 0) + 2 * H);
}

export async function computeHealth(now = Date.now()) {
  const reasons = [], unread = [], person = {};
  let challenged = null;
  const add = (code, level, text, since = null, ids = [], admin = '', extra = {}) =>
    reasons.push({ code, level, text, since: since ? Math.round(since) : null, ids: ids.slice(0, 12), ...(admin ? { admin } : {}), ...extra });
  // each area on its own: one that cannot be read is itself a reason, never a crash
  const area = async (name, fn) => {
    try { await fn(); } catch (e) {
      unread.push(name);
      if (refusing(e)) challenged ||= e;
      add(`${name}_unreadable`, 'amber', `Could not check the ${name.replace(/_/g, ' ')} (${why(e)}).`, now);
    }
  };
  let drafts = [], pages = [];
  await area('drafts', async () => { drafts = groupDrafts(await listAll()); });
  await area('pages', async () => { pages = (await listRecords('pages')).filter(p => !p.archived); });
  // a page the routines do not work (paused, not live yet, or failed to go live: docs/PRODUCT.md "A
  // page's life") makes no drafts, has no story feed and publishes nothing: none of that is a fault
  const idle = new Set(pages.filter(p => p.status && !['live', 'going_live'].includes(p.status)).map(p => p.slug));
  const worked = drafts.filter(d => !idle.has(d.page));
  if (!pages.length || pages.some(p => !idle.has(p.slug))) await area('hourly_routine', () => hourlyRules(now, add, worked));
  await area('topic_feed', () => topicRules(now, add, pages.filter(p => !idle.has(p.slug))));
  await area('publisher', () => publisherRules(now, add, worked, person));
  await area('studio_jobs', () => studioRules(now, add));
  await area('editor_requests', () => requestRules(now, add, drafts));
  await area('run_reports', () => runRules(now, add, person));
  await area('run_log', () => runlogRules(now, add));
  await area('health_check', () => watchdogRules(now, add));
  await area('publisher_notes', async () => personRule(add, person));
  // storage refusing reads hides what those checks would have found (an editor waiting on a
  // Studio job): red at once. Other read failures (a 5xx through a read's retries) are red from 3 checks on.
  if (challenged || unread.length >= 3)
    add('storage_unreadable', 'red', `The dashboard could not read its storage (${unread.length} check(s) failed${challenged ? `: ${why(challenged)}` : ''}).`, now);
  const level = l => reasons.some(r => r.level === l);
  const order = { red: 0, amber: 1 };
  reasons.sort((a, c) => order[a.level] - order[c.level]);
  return { status: level('red') ? 'red' : level('amber') ? 'amber' : 'green', reasons, checked_at: now };
}

// this instance's last result, until when it is given out, and the one being worked out; known:
// the newest status it worked out or read (never one made up by unreadable below)
let memo = null, memoUntil = 0, pending = null, known = null;
const view = h => ({ status: h.status, reasons: h.reasons || [], checked_at: h.checked_at });
const remember = (h, until) => { memo = h; memoUntil = until; return h; };
const knew = h => { if (!known || h.checked_at >= known.checked_at) known = h; return h; };

async function work(now) {
  const h = knew(view(await computeHealth(now)));
  await putRecord('health', 'status', h).catch(() => {});
  return remember(h, now + CACHE_MS);
}

// Storage did not give the stored status: a reason saying the health cannot be checked now, over
// what this instance last knew (from the last half hour). Never that status alone: a green from
// before would hide the outage, and close the owner's issue.
function unreadable(e, now) {
  const last = known && now - known.checked_at < 30 * M ? known : null;
  const reasons = [{ code: 'storage_unreadable', level: refusing(e) ? 'red' : 'amber', since: now, ids: [],
                     text: `The dashboard could not read its storage (${why(e)}): the pipeline's health could not be checked now.` },
                   ...(last ? last.reasons.filter(r => r.code !== 'storage_unreadable') : [])]
    .sort((a, c) => (a.level === 'red' ? 0 : 1) - (c.level === 'red' ? 0 : 1));     // red first, as computeHealth
  return { status: reasons[0].level === 'red' ? 'red' : 'amber', reasons, checked_at: last ? last.checked_at : now };
}

// The stored status if it is fresh; else one request in the whole deployment works it out (a
// create-only lease per 5 minutes) and the others are given the last status meanwhile. Storage
// not answering for the stored status: unreadable, for 2 minutes, never a full scan of a store
// that refuses reads from every instance at once (each made about 90 refused reads in a test of
// it). A status that is missing or broken (a 404, an entry that does not decrypt) is worked out
// again, as when there is none.
async function latest(now) {
  let rec = null;
  try {
    rec = await getRecord('health', 'status');
  } catch (e) {
    if (e instanceof StorageChallenged) return remember(unreadable(e, now), now + 2 * M);
  }
  if (rec) knew(view(rec));
  if (rec && now - rec.checked_at < CACHE_MS) return remember(view(rec), rec.checked_at + CACHE_MS);
  const name = `health-lease/${Math.floor(now / CACHE_MS)}.json`;
  const lease = await put(name, '{}', { access: 'public', contentType: 'application/json', addRandomSuffix: false, allowOverwrite: false })
    .catch(() => null);
  if (!lease && rec) return view(rec);                 // (a new install, with no status yet, works it out)
  try {
    return await work(now);
  } finally {                                          // this lease, and any a request that died left behind
    if (lease) await listBlobs('health-lease/').then(xs => del(xs.filter(x => x.pathname <= name).map(x => x.url))).catch(() => {});
  }
}

// The health status, at most 5 minutes old (or the last one while it is being worked out; fresh:
// worked out now, for the hourly watchdog). after: a status worked out then (an editor marked a note
// done, routes/decide.js), maybe by another instance: this one's older copy is not given out.
export async function health({ fresh = false, now = Date.now(), after = 0 } = {}) {
  if (fresh) return work(now);
  if (Math.min(after, now) > (memo ? memo.checked_at : 0)) {
    const rec = await getRecord('health', 'status').catch(() => null);
    if (rec && rec.checked_at >= Math.min(after, now)) return remember(knew(view(rec)), rec.checked_at + CACHE_MS);
  }
  if (memo && now < memoUntil) return memo;
  if (pending) return memo || pending;                 // being worked out here already: never a second time
  pending = latest(now).finally(() => { pending = null; });
  return pending;
}

// POST /api/routine/watchdog (the GitHub health workflow, hourly): the Studio nudge/fail logic of
// GET batch (routes/studio.js nudgeJobs) over every open job, then a fresh health status, then
// the old run reports pruned (nobody waits on this call).
// `streak`: how many watchdog runs in a row saw it amber or red (the workflow opens an issue for
// red at once, for amber after 3).
export async function watchdog(now = Date.now()) {
  const prev = await getRecord('health', 'watchdog').catch(() => null);
  await putRecord('health', 'watchdog', { ...(prev || {}), at: now });          // its own heartbeat first
  // storage refusing reads: nothing is restarted this hour, and the status says why
  const open = (await recentJobs(now, 4 * M).catch(() => [])).filter(j => ['queued', 'running'].includes(j.status));
  const batches = [...new Set(open.map(j => j.batch_id).filter(Boolean))];
  let failed = 0;
  for (const id of batches) {                          // one at a time: each nudge may start a session
    const bt = await getRecord('batches', id).catch(() => null);
    if (bt && await nudgeJobs(bt).catch(() => false)) failed++;
  }
  // a page's templates job has no batch (routes/templates.js)
  for (const j of open.filter(x => x.kind === 'templates')) if (await nudgeTemplates(j).catch(() => false)) failed++;
  const h = await health({ fresh: true, now });
  const streak = h.status === 'green' ? 0 : ((prev && prev.status && prev.status !== 'green' ? prev.streak || 0 : 0) + 1);
  await putRecord('health', 'watchdog', { at: now, status: h.status, streak });
  await pruneRuns(now).catch(() => {});
  return { open_jobs: open.length, batches: batches.length, failed_batches: failed, streak, health: h };
}

// the admin's details (which setting to check) are for the admin Health tab, not every editor's banner
// What a banner says: what the problem means for the editors' work, in plain words (editors asked what
// "routine prepare ... session may have died" meant, Oct 23). The technical text stays for the admin's
// Health tab. Problems inside the machinery that do not change an editor's work (a lost report, the
// watchdog, the saved state) are the admin's alone: editors never see them, and their status is green.
// what ran with less but changes nothing an editor sees or does (posts still come, on the template
// card): the admin's Health tab only
const ADMIN_ONLY = /shared Krea allowance/i;
const adminOnly = xs => (xs.length && xs.every(x => ADMIN_ONLY.test(x)) ? { admin_only: true } : {});
const EFFECT = { hourly: 'New drafts may be late or fewer than usual', topics: 'The Studio story list may not be up to date',
                 publisher: 'Approved posts may be scheduled late', studio: 'Studio jobs may be slow or fail' };
export function plain(r) {
  const routine = (String(r.text).match(/\((hourly|topics|publisher|studio)\)/) || [])[1];
  switch (r.code) {
    case 'hourly_stale': return 'New drafts are late: the hourly post routine has not run for 2 hours. The admin has been alerted.';
    case 'publisher_unclaimed': return 'Some approved posts have not been scheduled after 90 minutes. The admin has been alerted.';
    case 'publisher_stuck': return 'A post is stuck while being scheduled: do not retry it until the admin has checked SocialPilot.';
    case 'publisher_unconfirmed': return 'Some scheduled posts are not confirmed as published yet: check them on Facebook.';
    case 'publisher_needs_person': return 'Some posts need a person: something to fix or check in SocialPilot or on Facebook, or a page whose SocialPilot account an admin must set.';
    case 'studio_queued': return 'Studio is slow to start: jobs have waited over 10 minutes. It is restarted automatically.';
    case 'studio_stuck': return 'A Studio job is taking over 25 minutes: it is restarted or marked failed automatically.';
    case 'storage_unreadable': return 'The dashboard is having trouble reading its data: pages may load slowly. The admin has been alerted.';
    case 'run_degraded': case 'run_degraded_repeat':
      if (r.admin_only) return null;
      if (/photo/i.test(r.text)) return 'Photo suggestions may be fewer than usual right now.';
      return routine ? `${EFFECT[routine]}: part of it ran with problems. The admin has been alerted.` : null;
    case 'run_failed': return routine ? `${EFFECT[routine]}: it stopped with an error. The admin has been alerted.` : null;
    case 'run_unfinished': case 'runlog_failed': case 'watchdog_silent': case 'state_not_saved': return null;
    default: return r.text;            // written for editors already (story list age, SocialPilot waits, links, Studio wake-up)
  }
}
// an editor's posts needing a person: only those of their own pages (draft ids name the page), with
// only their ids, red only for one of theirs that is urgent
const yours = (r, scope) => {
  if (r.code !== 'publisher_needs_person') return r;
  const all = r.person || { urgent: [], other: r.ids };      // (a status stored before r.person: amber)
  const mine = k => (all[k] || []).filter(id => scope.has(pageOf(id)));
  const urgent = mine('urgent'), ids = [...urgent, ...mine('other')];
  return ids.length ? { ...r, level: urgent.length ? 'red' : 'amber', ids: ids.slice(0, 12) } : null;
};
// Reasons about some pages' drafts, jobs, links or feeds (their ids name the page: lib/tenancy.js
// idPage; a feed's is the page itself): a person who is not the product owner sees one only when it
// is about pages they may use, with only those ids, and never the product-wide count in its words.
const PAGE_REASONS = ['topics_stale', 'publisher_unclaimed', 'publisher_stuck', 'publisher_unconfirmed', 'publisher_held',
                      'studio_queued', 'studio_stuck', 'studio_fire_failed', 'link_waiting', 'regen_waiting'];
const SCOPED_TEXT = {
  publisher_held: 'Some approved posts are waiting for SocialPilot: they are tried again about every hour.',
  studio_fire_failed: 'Some Studio jobs are waiting: the Studio routine could not be started: the admin has been told.',
  link_waiting: 'Some of your links have waited more than 2 hours: the hourly routine has not made them or said why not.',
  regen_waiting: 'Some posts sent back to be redone have waited more than 2 hours: the hourly routine has not made them or said why not.',
};
function scoped(r, scope) {
  if (r.code === 'publisher_needs_person') return yours(r, scope);
  if (!PAGE_REASONS.includes(r.code)) return r;
  const ids = (r.ids || []).filter(id => scope.has(id) || scope.has(idPage(id)));
  if (!ids.length) return null;
  return { ...r, ids, ...(SCOPED_TEXT[r.code] ? { text: SCOPED_TEXT[r.code] } : {}) };
}
const forUser = (h, admin, scope = new Set()) => {
  const reasons = h.reasons.map(({ person: _, ...r }) => ({ ...r, plain: plain(r) }));
  if (admin) return { ...h, reasons };
  const shown = h.reasons.map(r => scoped(r, scope)).filter(Boolean)
    .map(({ person: _, ...r }) => ({ ...r, plain: plain(r) }))
    .filter(r => r.plain).map(({ admin: _, plain: p, ...r }) => ({ ...r, text: p }));
  return { ...h, reasons: shown, status: !shown.length ? 'green' : shown.some(r => r.level === 'red') ? 'red' : 'amber' };
};

export default async function handler(req, res) {
  if (!req.user) return res.status(401).json({ error: 'sign in first' });
  if (req.method !== 'GET') { res.setHeader('Allow', 'GET'); return res.status(405).end(); }
  return res.status(200).json(forUser(await health({ after: Number(req.query.after) || 0 }), isAdmin(req), await pageScope(req)));
}
