// /api/routine/*  (routines only: Bearer ROUTINE_SECRET) the server side of the AI steps.
//   GET  config            active pages: sources, guidance, the text of each reference document and the
//                          approved template specs (routes/templates.js)
//   GET  doc?page&type&part  the active reference PDF itself, part by part (config's docs.<type>.parts)
//   GET  feed?page         the page's current story feed;  POST feed {page, stories}
//   GET  jobs              queued Studio jobs, with the batch posts they act on (a templates job: the page's templates so far)
//   POST jobs/<id>         {status: running|done|failed, error, posts: {post_id: {...}}}; {release: true} hands a claim back;
//                          {left: true, error, session} a session whose machine cannot do the job; anything else: 400
//   POST templates         {page, job, doc_version, doc_pages, spec, preview_b64, pages: [{n, image_b64}]} one converted
//                          template, waiting for the editors' approval (routes/templates.js)
//   POST usage             {entries: [{tool, page, user, units, unit, cost_usd, detail}]}
//   POST runs              a run report from bore/ops.py, one per command: {run_id, routine, command, started_at,
//                          ended_at, ok, degraded, summary};  GET runs?hours=  the last hours' reports (diagnostics)
//   GET  health            the pipeline's health (routes/health.js), for the GitHub health workflow
//   POST watchdog          restart or fail stalled Studio jobs (all open jobs), then a fresh health status
//   POST pages             migration: upsert pages (sources, guidance, theme) from the old config
//   POST bootstrap         create the first admin when no user exists
// Every read of protected content is written to the audit log.
import { getRecord, putRecord, updateRecord, listRecords, listBlobs, getFile, putImage, appendLog, readLogs } from '../lib/db.js';
import { put, del } from '../lib/blob.js';
import { isRoutine } from '../lib/auth.js';
import { userId, hashPassword, allUsers } from '../lib/session.js';
import { issueReset } from '../lib/resets.js';
import { audit, usage } from '../lib/audit.js';
import { postStatus, faceId, postText, JOB_STALE_MS, failStalled, sentOrSending, isSent, sendingNow, wanted, madeFrom, SENT, checkBack } from './studio.js';
import { loadDraft } from '../lib/store.js';
import { routineCaption, routineTags } from '../lib/caption.js';
import { DOC_TYPES } from './admin.js';
import { readReviews, reviewEntry, reviewKey } from '../lib/reviews.js';
import { saveRun, listRuns, health, watchdog } from './health.js';
import { approvedSpecs, templatesForJob, saveFromRoutine, markConverted } from './templates.js';
import { ok, bad, clean, siteOrigin } from '../lib/http.js';

const ACTOR = 'routine';
// template: the name the page's templates document uses; layout: how the renderer draws it
// a candidate image and where it came from (editors see the source of every image)
const imageIn = i => ({ url: clean(i.url, 800), source: clean(i.source, 40), credit: clean(i.credit, 120), size: clean(i.size, 20),
  via: clean(i.via, 160), post_url: clean(i.post_url, 400), note: clean(i.note, 30), consensus: Number(i.consensus) || 0,
  flags: (Array.isArray(i.flags) ? i.flags : []).slice(0, 4).map(f => clean(f, 120)), ...faceId(i) });
// template_spec: an approved template spec's id (routes/templates.js), drawn as it is
const ideaIn = i => ({ title: clean(i.title, 120), template: clean(i.template, 60), layout: clean(i.layout, 30), description: clean(i.description, 900),
                       ...(i.template_spec ? { template_spec: clean(i.template_spec, 48) } : {}) });

// Why a job's result for a post is no longer wanted (null: it is). Sessions restart, take jobs over
// and finish late, so a result can land after the editor moved on: the post was sent (or is being
// sent), a newer job of the same kind was queued for it (its result is the one that counts: no
// note), or the photo (for a check, the visual) the session was given has been replaced. The photo a
// visual was made on is the one the session says it used (r.photo), else the one it was given when
// it claimed the job, unless sessions that took the job over were given different photos (then it
// cannot be told). The note goes on the job line. handed: the post's draft is with the publisher
// although the batch does not say so yet (a Send cut short after the approval).
function outOfDate(p, job, r, from, handed) {
  if (isSent(p) || handed) return 'It finished after the post was sent, so it was not used.';
  if (sendingNow(p)) return 'It finished while the post was being sent, so it was not used.';
  if (!wanted(p, job)) return '';
  const photo = typeof r.photo === 'string' ? r.photo : from ? (from.photos ? null : from.photo) : undefined;
  if (job.kind === 'compose' && photo !== undefined && photo !== (p.base_image ? p.base_image.url : ''))
    return photo === null ? 'It may have been made from an earlier photo, so it was not used: press Make visual for the photo you picked.'
      : 'It was made from the earlier photo, so it was not used: press Make visual for the photo you picked.';
  if (job.kind === 'check' && from && from.visual !== (p.final_image_url || ''))
    return 'It checked an earlier visual, so it is not shown: check the post again.';
  return null;
}

// a Krea reservation not said to be made within this lapses (Krea failed, the card went to the template)
const KREA_LAPSE = 45 * 60e3;
// the month's Krea images: the hourly routine's (its ledger a floor) and Studio's (those before a reset
// in studioBase), each also today's
function kreaCounts(rs, { hU, hT, base, baseToday, studioBase }, onDay, cap, daysLeft) {
  const h = rs.filter(r => r.who === 'hourly'), s = rs.filter(r => r.who === 'studio');
  const studio = studioBase + s.length, used = Math.max(hU, base + h.length) + studio, studioToday = s.filter(onDay).length;
  const today = Math.max(hT, baseToday + h.filter(onDay).length) + studioToday;
  const allowed = Math.max(0, Math.floor((cap - Math.max(0, used - today)) / daysLeft));   // (today is part of the month)
  return { used, cap, today, allowed, studio, studio_today: studioToday };
}
const templatesResult = t => ({ sent: (Array.isArray(t.sent) ? t.sent : []).slice(0, 80).map(x => clean(x, 48)),
  failed: Object.fromEntries(Object.entries(t.failed && typeof t.failed === 'object' ? t.failed : {}).slice(0, 80).map(([k, v]) => [clean(k, 48), clean(v, 300)])) });

export default async function handler(req, res) {
  if (!isRoutine(req)) return bad(res, 'routine credential required', 401);
  const [what, id] = req.subpath;
  const b = req.body || {};

  if (what === 'config' && req.method === 'GET') {
    const pages = (await listRecords('pages')).filter(p => !p.archived);
    const out = [];
    for (const p of pages) {
      const docs = {};
      for (const t of Object.keys(DOC_TYPES)) {
        const d = (p.docs || {})[t];
        const v = d && (d.versions || []).find(x => x.v === d.active);
        // parts: how many `doc?part=` downloads the PDF takes; size and sha256 check the joined file
        if (v) docs[t] = { version: v.v, filename: v.filename, pages: v.pages, size: v.size, parts: (v.parts || [v.pdf]).length,
                           sha256: v.sha256 || '', chars: v.chars ?? null, text: ((await getFile(v.txt)) || '').toString('utf8') };
      }
      out.push({ slug: p.slug, name: p.name, fb_page_id: p.fb_page_id, socialpilot_login_id: p.socialpilot_login_id,
                 timezone: p.timezone, theme: p.theme || {}, posts_per_day: p.posts_per_day || 8,
                 sources: (p.sources || []).filter(s => s.active !== false), pipeline: p.pipeline || null, playbook: p.playbook || null, docs,
                 // only what an editor approved is ever drawn (`studio sync` writes private/templates/<page>/)
                 templates: approvedSpecs(await getRecord('templates', p.slug)) });
    }
    await audit(ACTOR, 'config_read', { pages: out.map(p => p.slug) });
    return ok(res, { pages: out });
  }

  if (what === 'doc' && req.method === 'GET') {
    const p = await getRecord('pages', String(req.query.page || ''));
    const d = p && (p.docs || {})[String(req.query.type || '')];
    const v = d && (d.versions || []).find(x => x.v === d.active);
    if (!v) return bad(res, 'no such document', 404);
    // a large document is stored in parts (routes/admin.js) and sent one per request (4.5 MB per response)
    const files = v.parts || [v.pdf];
    if (req.query.part === undefined && files.length > 1) return bad(res, `this document comes in ${files.length} parts: ask for part=0..${files.length - 1}`);
    const i = Number(req.query.part || 0);
    if (!Number.isInteger(i) || i < 0 || i >= files.length) return bad(res, 'no such part', 404);
    const buf = await getFile(files[i]);
    if (!buf) throw new Error(`part ${i + 1} of the ${req.query.type} document is missing from storage`);
    // one entry per download (its first part), not one per part: the audit view reads every entry
    if (i === 0) await audit(ACTOR, 'reference_read', { page: p.slug, type: req.query.type, version: v.v, ...(files.length > 1 ? { parts: files.length } : {}) });
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('X-Doc-Parts', String(files.length));
    res.setHeader('X-Doc-Version', String(v.v));
    return res.status(200).send(buf);
  }

  if (what === 'feed') {
    if (req.method === 'GET') return ok(res, { feed: await getRecord('feed', String(req.query.page || '')) });
    if (req.method === 'POST') {
      const page = clean(b.page, 60);
      if (!await getRecord('pages', page)) return bad(res, 'no such page', 404);
      const stories = (b.stories || []).slice(0, 120).map(s => ({
        id: clean(s.id, 80), headline: clean(s.headline, 400), summary: clean(s.summary, 800),
        sources: (s.sources || []).slice(0, 12).map(x => ({ name: clean(x.name, 80), url: clean(x.url, 600) })),
        published_at: Number(s.published_at) || null, url: clean(s.url, 600), score: Number(s.score) || 0,
        kind: clean(s.kind, 40), learned: clean(s.learned, 80), subject: clean(s.subject, 120),
        ideas: (s.ideas || []).slice(0, 2).map(ideaIn),
        images: (s.images || []).slice(0, 6).map(imageIn), images_v: Number(s.images_v) || 0,
        images_at: Number(s.images_at) || 0,
        // what the next hour's feed compares with: the posts the text was written from, and the
        // writer's 'not news' verdict (so the same memes are not judged again every hour)
        written_from: (s.written_from || []).slice(0, 30).map(u => clean(u, 600)).filter(Boolean),
        ...(s.not_news ? { not_news: clean(s.not_news, 200) } : {}),
      }));
      await putRecord('feed', page, { page, updated_at: Date.now(), stories });
      return ok(res, { stories: stories.length });
    }
  }

  if (what === 'jobs') {
    if (req.method === 'GET' && !id && req.query.recent) {          // timing of the last jobs (diagnostics)
      const all = (await listRecords('jobs')).sort((a, c) => c.created_at - a.created_at).slice(0, 40);
      return ok(res, { jobs: all.map(j => ({ id: j.id, kind: j.kind, page: j.page, status: j.status, created_at: j.created_at,
        started_at: j.started_at || null, done_at: j.done_at || null, fired: j.fired, fire_status: j.fire_status, fire_error: j.fire_error, error: j.error || '' })) });
    }
    if (req.method === 'GET' && !id) {
      // the editors' jobs first: a session an editor's click started never takes a templates
      // conversion (a long one) while their job waits; oldest first within each
      const jobs = (await listRecords('jobs')).filter(j => j.status === 'queued' || (j.status === 'running' && Date.now() - (j.started_at || 0) > JOB_STALE_MS))
        .sort((a, c) => (a.kind === 'templates') - (c.kind === 'templates') || a.created_at - c.created_at);
      const out = [];
      for (const j of jobs) {
        // a page's templates to convert: no batch, the templates converted so far (names, notes, specs)
        if (j.kind === 'templates') { out.push({ ...j, posts: [], templates: templatesForJob(await getRecord('templates', j.page)) }); continue; }
        const bt = await getRecord('batches', j.batch_id);
        if (!bt) continue;
        out.push({ ...j, posts: bt.posts.filter(p => j.post_ids.includes(p.id)) });
      }
      return ok(res, { jobs: out });
    }
    const job = id && await getRecord('jobs', id);
    if (!job) return bad(res, 'no such job', 404);
    if (req.method === 'POST' && b.left) {
      // a session left the job: its machine cannot do this kind (packages not loading). When three
      // sessions have, the editor is told instead of waiting (each leave is a file only one can make).
      // A session that leaves it again after trying a repair is the same machine: counted once
      // (sessions send their id; one without an id counts each time, as before)
      if (job.status !== 'queued') return ok(res, { status: job.status });
      const session = clean(b.session, 40).replace(/[^A-Za-z0-9]/g, '');
      const leaves = await listBlobs(`job-leaves/${job.id}/`);
      if (session && leaves.some(l => l.pathname === `job-leaves/${job.id}/s-${session}.json`)) return ok(res, { status: 'queued' });
      try {
        await put(`job-leaves/${job.id}/${session ? `s-${session}` : leaves.length}.json`, JSON.stringify({ at: Date.now(), error: clean(b.error, 600) }),
                  { access: 'public', contentType: 'application/json', addRandomSuffix: false, allowOverwrite: false });
      } catch (e) {
        return ok(res, { status: 'queued' });
      }
      // the editor reads a plain sentence; the machine's own error is kept for the admin (job.detail)
      if (leaves.length + 1 >= 3 && await failStalled(job.id, 'The Studio machines could not start this step. '
                                                         + 'Press the button again in a few minutes; if it keeps failing, tell the admin.', clean(b.error, 300)))
        return ok(res, { status: 'failed' });
      return ok(res, { status: 'queued' });
    }
    if (req.method === 'POST' && b.release) {
      // the session that claimed the job could not prepare it (a problem on its machine, not the
      // editor's): the job is queued again and can be claimed at once, after its fix or by another.
      // The third time it fails, so that one job can never hold up the others: the editor is told.
      if (job.status !== 'running') return res.status(409).json({ error: `not running (${job.status})` });
      const releases = (job.releases || 0) + 1, failed = releases >= 3;
      const error = failed ? `This post could not be prepared (tried on ${releases} Studio machines). `
                             + 'Press the button again to retry; if it fails again, tell the admin.' : '';
      const detail = failed ? clean(b.error, 300) : '';                // (for the admin: the machine's own error)
      const claims = await listBlobs(`job-claims/${job.id}/`);
      if (claims.length && !failed) await del(claims.map(c => c.url));
      if (job.batch_id) await updateRecord('batches', job.batch_id, bt => {
        if (!bt) return null;
        for (const p of bt.posts) if (p.job && p.job.id === job.id) {
          p.job = { ...p.job, status: failed ? 'failed' : 'queued', error, ...(detail ? { detail } : {}) };
          if (failed && job.kind === 'check' && p.check && p.check.status === 'queued') p.check = checkBack(p.check);
          p.status = postStatus(p);
        }
        return bt;
      });
      await updateRecord('jobs', job.id, cur => cur && { ...cur, status: failed ? 'failed' : 'queued', started_at: failed ? cur.started_at : null,
                                                        done_at: failed ? Date.now() : null, error, ...(detail ? { detail } : {}), released_at: Date.now(), releases,
                                                        release_errors: [...(cur.release_errors || []), clean(b.error, 300)].slice(-3) });
      return ok(res, { status: failed ? 'failed' : 'queued' });
    }
    if (req.method === 'POST') {
      // a request this dashboard does not know (Python newer than the deployed dashboard) is refused,
      // never taken for "done": the session sees the error, not the editor a job done with nothing
      if (!['running', 'done', 'failed'].includes(b.status) && !b.precheck) return bad(res, 'unknown request: deploy the dashboard');
      const status = b.status;
      const claim = status === 'running' && !b.posts;
      if (claim) {
        // a session claims the job: one session per job, even when several start at once (every
        // editor click starts one). A claim is a file only one session can create,
        // job-claims/<id>/<n>.json. A claim 20 minutes old on an unfinished job is dropped (the
        // session died) and the next claim is n+1, whatever the job record says.
        if (!['queued', 'running'].includes(job.status) || (job.status === 'running' && Date.now() - (job.started_at || 0) < JOB_STALE_MS))
          return res.status(409).json({ error: `already ${job.status}: another session has it` });
        const claims = (await listBlobs(`job-claims/${job.id}/`)).sort((x, y) => new Date(y.uploadedAt) - new Date(x.uploadedAt));
        if (claims.length && Date.now() - new Date(claims[0].uploadedAt) < JOB_STALE_MS)
          return res.status(409).json({ error: 'claimed by another session' });
        try {
          await put(`job-claims/${job.id}/${claims.length}.json`, JSON.stringify({ at: Date.now() }),
                    { access: 'public', contentType: 'application/json', addRandomSuffix: false, allowOverwrite: false });
        } catch (e) {
          return res.status(409).json({ error: 'claimed by another session' });
        }
      }
      const bt = await getRecord('batches', job.batch_id);
      const from = job.made_from || {};
      // a post whose Send was cut short after its draft reached the publisher still says 'sending'
      // until a page reads the batch again: its draft says whether it was sent
      const handed = new Set();
      if (!claim) for (const p of (bt ? bt.posts : []).filter(x => job.post_ids.includes(x.id) && x.publish && x.publish.draft_id
                                                             && ['sending', 'pending'].includes(x.publish.state) && !sendingNow(x)))
        if (SENT.includes(((await loadDraft(p.publish.draft_id).catch(() => null)) || {}).status)) handed.add(p.id);
      if (b.precheck) {
        // the check made right after composing: the posts' checks only, and only of the visual this
        // job made. Dropped if the editor has made another visual, picked another photo, or skipped
        // or started a check meanwhile; out of date if they changed the caption or hashtags.
        if (bt) await updateRecord('batches', bt.id, cur => {
          if (!cur) return null;
          for (const [pid, r] of Object.entries(b.posts || {})) {
            const p = cur.posts.find(x => x.id === pid);
            if (!p || !r || !r.check || sentOrSending(p) || handed.has(pid)) continue;
            if (p.card_job !== job.id || !p.final_image_url || !(p.check && p.check.auto && p.check.status === 'running')) continue;
            const seen = r.checked_caption !== undefined ? String(r.checked_caption).trim() : p.card_text;   // the text it reviewed
            p.check = { status: 'done', at: Date.now(), auto: true, results: r.check, stale: postText(p) !== seen,
                        ...(typeof seen === 'string' ? { text: seen } : {}) };
            p.status = postStatus(p);
          }
          return cur;
        });
        await updateRecord('jobs', job.id, cur => cur && { ...cur, precheck_at: Date.now() });
        return ok(res, { status: job.status, precheck: true });
      }
      // a finished job is not finished again (a repeated `studio done` would overwrite the editor's edits)
      if (job.status === 'done' && status === 'done' && Object.values(b.posts || {}).some(r => r && (r.image_b64 || r.caption !== undefined)))
        return res.status(409).json({ error: 'this job is already done: results were sent before (for a pre-check use `studio precheck --send`)' });
      // a session the job was taken from (it took over 20 minutes, and another session has claimed it
      // since, given another photo) finishing late: its card is not used, and the job stays with the
      // session that has it now (whose card would otherwise be refused as "already done")
      if (job.kind === 'compose' && status === 'done' && job.status === 'running' && Date.now() - (job.started_at || 0) < JOB_STALE_MS
          && Object.entries(b.posts || {}).some(([pid, r]) => r && typeof r.photo === 'string' && from[pid] && r.photo !== from[pid].photo))
        return res.status(409).json({ error: 'another session has taken this job over (this one took more than 20 minutes): leave it' });
      // the cards are stored first, so the batch is then changed in one quick step on its newest
      // copy: an editor's edit or another job's result saved meanwhile is kept
      const cards = {};
      for (const [pid, r] of Object.entries(b.posts || {})) {
        if (r && r.image_b64 && bt && bt.posts.some(x => x.id === pid))
          cards[pid] = await putImage(`studio/${bt.id}/card`, Buffer.from(r.image_b64, 'base64'), 'image/jpeg');
      }
      let notes = {}, made = {};
      if (bt) await updateRecord('batches', bt.id, cur => {
        notes = {}; made = {};
        if (!cur) return null;
        for (const [pid, r] of Object.entries(b.posts || {})) {
          const p = cur.posts.find(x => x.id === pid);
          if (!p || !r) continue;
          const why = outOfDate(p, job, r, from[pid], handed.has(pid));
          if (why !== null) { notes[pid] = why; continue; }
          if (r.summary) p.story.summary = clean(r.summary, 800);
          if (r.headline && p.story.from_url) p.story.headline = clean(r.headline, 400);
          if (r.ideas) {
            const ideas = r.ideas.slice(0, 2).map(ideaIn);
            // "idea 1" now points at another idea: the editor picks again (the screen shows the choice by position)
            const was = p.idea && [1, 2].includes(p.idea.choice) && ideas[p.idea.choice - 1];
            if (p.idea && [1, 2].includes(p.idea.choice) && !(was && was.title === p.idea.title && was.description === p.idea.text)) p.idea = null;
            p.story.ideas = ideas;
          }
          if (r.images) {                    // a new search adds to the earlier ones (newest first)
            const fresh = r.images.slice(0, 8).map(imageIn), seen = new Set(fresh.map(i => i.url));
            p.extra_images = [...fresh, ...(p.extra_images || []).filter(i => !seen.has(i.url))].slice(0, 16);
          }
          if (cards[pid]) {
            p.final_image_url = cards[pid];
            // a new visual: the session checks it next (pre-check); the Check step shows it is under way
            p.check = job.kind === 'compose' ? { status: 'running', auto: true, at: Date.now() } : { status: 'none' };
          }
          if (r.card) p.card = r.card;
          if (r.caption !== undefined || r.hashtags) {
            const caption = r.caption !== undefined ? routineCaption(r.caption) : p.caption;
            const hashtags = r.hashtags ? routineTags(r.hashtags) : p.hashtags;
            const theirs = { caption, hashtags };
            // a caption the editor wrote or edited is never replaced by a visual made again: the
            // routine's is offered beside it (Caption step)
            if (p.caption_edited_at && job.kind === 'compose') { if (postText(theirs) !== postText(p)) p.suggested = theirs; else delete p.suggested; }
            else { p.caption = caption; p.hashtags = hashtags; delete p.suggested; }
            if (cards[pid] && job.kind === 'compose') { p.card_job = job.id; p.card_text = postText(theirs); }   // what its pre-check checks
          } else if (cards[pid] && job.kind === 'compose') { p.card_job = job.id; p.card_text = postText(p); }
          if (r.check) {
            // a check of an older caption is out of date: the text it reviewed (sent by the session,
            // else the post's text when the session claimed the job)
            const seen = r.checked_caption !== undefined ? String(r.checked_caption).trim() : from[pid] ? from[pid].text : undefined;
            p.check = { status: 'done', at: Date.now(), stale: seen !== undefined && seen !== postText(p), auto: !!r.auto, results: r.check,
                        ...(seen !== undefined ? { text: seen } : {}) };
          }
          if (r.error) p.job = { ...(p.job || {}), error: clean(r.error, 600) };
          p.status = postStatus(p);
        }
        for (const p of cur.posts) if (job.post_ids.includes(p.id)) {
          const r = (b.posts && b.posts[p.id]) || {}, note = notes[p.id];
          if (claim) {
            // what this session was given; and every photo the sessions of this job were given
            const was = from[p.id], now = madeFrom(p);
            const photos = [...new Set([...(was ? was.photos || [was.photo] : []), now.photo])];
            made[p.id] = photos.length > 1 ? { ...now, photos } : now;
          }
          // a job under way behind another job's line (p.under_way) is followed until it ends; then
          // it takes the line back if that one's job has ended too and it has something to say
          const behind = (p.under_way || []).find(j => j.id === job.id);
          if (behind && claim && job.kind === 'compose' && p.base_image) behind.photo = p.base_image.url;
          if (behind && status !== 'running') { p.under_way = p.under_way.filter(j => j !== behind); if (!p.under_way.length) delete p.under_way; }
          const back = behind && status !== 'running' && !(p.job && ['queued', 'running'].includes(p.job.status)) && wanted(p, job)
            && (status === 'failed' || !!note || !!r.error);
          if (p.job && p.job.id !== job.id && !back) continue;           // never another job's line
          p.job = { id: job.id, kind: job.kind, status, error: note !== undefined ? '' : clean(r.error, 600) || (status === 'failed' ? clean(b.error, 600) : ''),
                    ...(note ? { note } : {}),
                    // the photo its visual is made from: a new pick makes Make visual available again
                    ...(claim && job.kind === 'compose' && p.base_image ? { photo: p.base_image.url } : {}) };
          // (a check that failed for this post alone, in a job that checked the others, too)
          if (job.kind === 'check' && (status === 'failed' || note !== undefined || (r.error && !r.check)) && p.check && p.check.status === 'queued') p.check = checkBack(p.check);
          p.status = postStatus(p);
        }
        return cur;
      });
      await updateRecord('jobs', job.id, cur => cur && { ...cur, status, error: clean(b.error, 600),
                                                        started_at: status === 'running' ? Date.now() : cur.started_at, done_at: status === 'running' ? null : Date.now(),
                                                        ...(claim ? { made_from: made } : {}),
                                                        // a templates job: which templates it sent, and why any could not be converted
                                                        ...(job.kind === 'templates' && b.templates ? { result: templatesResult(b.templates) } : {}),
                                                        ...(Object.keys(notes).length ? { dropped: notes } : {}) });
      if (job.kind === 'templates' && status === 'done' && job.only == null) await markConverted(job, templatesResult(b.templates || {}));
      return ok(res, { status, ...(Object.keys(notes).length ? { dropped: Object.keys(notes) } : {}) });
    }
  }

  // One Krea allowance for the month, shared by the hourly routine and Studio sessions (a Studio session
  // keeps no state of its own, so its images were invisible to the hourly routine's count). Each image is
  // reserved here before it is made, and `krea-result` says when it was (made). The count is the images
  // the hourly routine made before its first report of the month (base: it made images before this
  // count existed), plus every reservation made or asked less than 45 minutes ago (one never made
  // lapses). The hourly ledger, sent with every hourly ask and at every `routine prepare` (report_only),
  // is a floor only: a ledger restored from a run that never saved its state lacks that run's images,
  // which their reservations still count. The month's images are spread over its days as before.
  // A Studio session on a routine older than `krea-result`'s confirmation (no `confirms`; a session
  // keeps the code it started with) never says what was made: its reservations count as made.
  // {who: 'hourly'|'studio', cap, key?, report_only?, made?, confirms?, and from the hourly routine:
  //  hourly_used, hourly_today, month, day (when it counted them)}
  if (what === 'krea' && req.method === 'POST') {
    const cap = Math.max(0, Math.floor(Number(b.cap) || 0)), who = b.who === 'studio' ? 'studio' : 'hourly';
    if (!cap) return bad(res, 'cap missing');
    // a routine older than this count sends its ledger without the month: refused, it then counts on
    // its own ledger as it did before (a month-less count could be last month's, kept all month)
    if (who === 'hourly' && b.hourly_used !== undefined && b.month === undefined)
      return bad(res, 'the hourly count must say which month it was counted in: update the routine');
    const t = Date.now(), iso = new Date(t).toISOString(), month = iso.slice(0, 7), day = iso.slice(0, 10);
    const now = new Date(t), daysLeft = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0)).getUTCDate() - now.getUTCDate() + 1;
    const key = clean(b.key, 40) || null;
    if (b.made && !key) return bad(res, 'key missing');
    // a count is only for the month (and day) it was counted in: one counted at 23:59:59 and received
    // after midnight would otherwise stay all month (a count only moves up)
    const report = who === 'hourly' && b.hourly_used !== undefined && b.month === month;
    const reportDay = report && b.day === day;
    let out = null;
    // checked and reserved in one write: of two asks on the same version only one is saved (db.js), the
    // other is checked again on top of it, so two sessions never both take the last image
    await updateRecord('krea', month, cur => {
      const { hourly_seq: legacy, ...c } = cur || { month, hourly_used: 0, hourly_today: {}, seq: 0, reported: false, res: [] };
      // a record of the earlier count (hourly_seq) never said what was made: its Studio images were
      // all counted, and still are
      const kept = (c.res || []).map(r => (legacy !== undefined && r.who === 'studio' && !r.made ? { ...r, made: true } : r))
        .filter(r => r.made || t - r.at < KREA_LAPSE);
      const onDay = r => r.day === day, mine = r => r.who === 'hourly';
      let hU = c.hourly_used || 0, hT = (c.hourly_today || {})[day] || 0, base = c.base, baseToday = c.base_today || {};
      if (report) {
        const u = Math.floor(Number(b.hourly_used) || 0), d = reportDay ? Math.floor(Number(b.hourly_today) || 0) : 0;
        hU = Math.max(hU, u);
        if (reportDay) hT = Math.max(hT, d);
        // its first report: the images it made before any reservation (those reserved before it
        // reported count by their reservation)
        if (base === undefined) {
          base = Math.max(0, hU - kept.filter(mine).length);
          baseToday = reportDay ? { [day]: Math.max(0, d - kept.filter(r => mine(r) && onDay(r)).length) } : {};
        }
      }
      const rs = key && b.made && !kept.some(r => r.key === key)        // made after its reservation lapsed
        ? [...kept, { n: (c.seq || 0) + 1, at: t, who, day, key, made: true }]
        : kept.map(r => (b.made && r.key === key && !r.made ? { ...r, made: true } : r));
      const counts = kreaCounts(rs, { hU, hT, base: base || 0, baseToday: baseToday[day] || 0, studioBase: c.studio_base || 0 }, onDay, cap, daysLeft);
      const next = { ...c, hourly_used: hU, hourly_today: { [day]: hT }, ...(base !== undefined ? { base, base_today: baseToday } : {}),
                     seq: Math.max(c.seq || 0, ...rs.map(r => r.n)), reported: !!c.reported || report, res: rs };
      const changed = !cur || legacy !== undefined || hU !== c.hourly_used || hT !== ((c.hourly_today || {})[day] || 0) || base !== c.base
        || next.reported !== !!c.reported || rs.length !== (c.res || []).length || rs.some((r, i) => r !== kept[i]);
      const keep = () => (changed ? next : null);
      // a made image is counted whatever the count says: it is made
      if (b.report_only || b.made) { out = { ...counts }; return keep(); }
      // asked again for the same image (a lost answer, a step run twice): it has its reservation
      if (key && rs.some(r => r.key === key)) { out = { ...counts, again: true }; return keep(); }
      const { used, today, allowed } = counts;
      const no = used >= cap ? `KREA CAP: ${used}/${cap} images this month`
        : today >= allowed ? `KREA PACE: ${today}/${allowed} images today`
        // until the hourly routine has counted this month (its next run reports at prepare), Studio
        // would be checked against its own images only
        : !next.reported && who === 'studio' ? 'KREA UNCHECKED: the hourly routine has not counted this month\'s images yet' : '';
      if (no) { out = { error: no, ...counts }; return keep(); }
      out = { ...counts, used: used + 1, today: today + 1,
              ...(who === 'studio' ? { studio: counts.studio + 1, studio_today: counts.studio_today + 1 } : {}) };
      const n = next.seq + 1;
      return { ...next, seq: n, res: [...rs, { n, at: t, who, day, ...(key ? { key } : {}), ...(who === 'studio' && !b.confirms ? { made: true } : {}) }] };
    });
    return out.error ? res.status(409).json(out) : ok(res, out);
  }

  if (what === 'templates' && req.method === 'POST') {
    try { return ok(res, await saveFromRoutine(b)); } catch (e) { if (e.status) return bad(res, e.message, e.status); throw e; }
  }

  if (what === 'usage' && req.method === 'POST') {
    const entries = (b.entries || []).slice(0, 500);
    for (const e of entries) await usage({ tool: clean(e.tool, 60), page: clean(e.page, 60), user: clean(e.user, 200),
      units: Number(e.units) || 0, unit: clean(e.unit, 30), cost_usd: Number(e.cost_usd) || 0, detail: clean(e.detail, 300),
      when: Number(e.when) || Date.now() });
    return ok(res, { logged: entries.length });
  }

  if (what === 'pages' && req.method === 'POST') {                   // one-time migration from the old config
    const done = [];
    for (const p of (b.pages || [])) {
      const slug = clean(p.slug, 60);
      if (!slug) continue;
      const cur = (await getRecord('pages', slug)) || { slug, docs: {}, created_at: Date.now() };
      await putRecord('pages', slug, { ...cur, name: clean(p.name, 100) || cur.name, fb_page_id: clean(p.fb_page_id, 60) || cur.fb_page_id || '',
        socialpilot_login_id: clean(p.socialpilot_login_id, 30) || cur.socialpilot_login_id || '', timezone: p.timezone || cur.timezone || 'America/New_York',
        theme: { ...(cur.theme || {}), ...(p.theme || {}) }, posts_per_day: Number(p.posts_per_day) || cur.posts_per_day || 8,
        sources: p.sources || cur.sources || [], pipeline: p.pipeline !== undefined ? p.pipeline : cur.pipeline || null, playbook: p.playbook !== undefined ? p.playbook : cur.playbook || null, archived: false });
      done.push(slug);
    }
    await audit(ACTOR, 'pages_imported', { pages: done });
    return ok(res, { pages: done });
  }

  // run log: routines report each step and any error, so a run that dies early is visible
  // the review ledger (every editor verdict) and the per-page lessons learned from it
  if (what === 'reviews') {
    if (req.method === 'GET') return ok(res, { entries: await readReviews(Number(req.query.days) || 180) });
    if (req.method === 'POST') {                                   // one-time backfill from stored drafts
      const have = new Set((await readReviews(365)).map(reviewKey));
      let added = 0;
      for (const e of (Array.isArray(b.entries) ? b.entries : []).slice(0, 500)) {
        if (!['approved', 'rejected', 'cleared', 'restored', 'withdrawn', 'studio_pick', 'studio_sent', 'feedback'].includes(e.verdict)) continue;
        const k = reviewKey(e);
        if (!e.draft_id || (have.has(k) && b.replace !== true)) continue;
        have.add(k);
        await appendLog('review', reviewEntry(e.verdict, { ...e, id: e.draft_id },
          { reason: e.reason, detail: e.detail, note: e.note, by: e.by, decided_at: Number(e.decided_at) || null, backfill: true,
            kind_rank: e.kind_rank }));
        // (replace: true re-adds entries that exist; readers keep the newest one per post and verdict)
        added++;
      }
      await audit(ACTOR, 'reviews_backfilled', { added });
      return ok(res, { added });
    }
  }
  if (what === 'lessons') {
    const page = clean(req.method === 'GET' ? req.query.page : b.page, 60);
    if (!page) return bad(res, 'page required');
    const prev = await getRecord('lessons', page);
    if (req.method === 'GET') return ok(res, { lessons: prev });
    if (req.method === 'POST') {
      const rec = b.record || {};
      if (JSON.stringify(rec).length > 60000) return bad(res, 'lessons record too large');
      // admins' settings always survive a rebuild: the routine never decides them
      // (from the newest copy: an admin's note saved while this was sent is kept)
      await updateRecord('lessons', page, cur => ({ ...rec, ...(cur ? { admin_notes: cur.admin_notes || '', reset_at: cur.reset_at || null,
                                                                        overrides: cur.overrides || {} } : {}), page, built_at: Date.now() }));
      await audit(ACTOR, 'lessons_built', { page, reviews_used: rec.reviews_used || 0, avoid: (rec.avoid || []).slice(0, 10) });
      return ok(res, { saved: true });
    }
  }

  if (what === 'runlog') {
    if (req.method === 'POST') {
      await appendLog('runlog', { routine: clean(b.routine, 60), step: clean(b.step, 80), ok: b.ok !== false, detail: clean(b.detail, 2000) });
      return ok(res, {});
    }
    if (req.method === 'GET') return ok(res, { entries: (await readLogs('runlog', 2)).slice(0, 200) });
  }

  // run reports written by the code (bore/ops.py), one per command: ok is never the session's own word
  if (what === 'runs') {
    if (req.method === 'POST') {
      const r = await saveRun(b);
      return r.error ? bad(res, r.error) : ok(res, r);
    }
    if (req.method === 'GET') return ok(res, await listRuns(Number(req.query.hours) || 24));
  }
  if (what === 'health' && req.method === 'GET') return res.status(200).json(await health());
  if (what === 'watchdog' && req.method === 'POST') return ok(res, await watchdog());

  if (what === 'fire-check' && req.method === 'POST') {          // does approving still wake the publisher?
    let r;
    if (req.query.which === 'STUDIO') {                            // the Studio jobs routine
      const { fire } = await import('../lib/fire.js');
      r = await fire('STUDIO', 'Health check: process any queued Studio jobs (STUDIO.md), otherwise finish.');
    } else {
      const { fireRoutine } = await import('../lib/publish.js');
      r = await fireRoutine('none (health check: schedule anything approved, otherwise finish)');
    }
    await audit(ACTOR, 'publisher_fire_check', { fired: r.fired, status: r.status || null });
    return ok(res, { fired: r.fired, status: r.status || null, note: r.note || '', response: r.fired ? '' : String(r.response || '').slice(0, 300) });
  }

  if (what === 'bootstrap' && req.method === 'POST') {
    if ((await allUsers()).length) return bad(res, 'users already exist', 409);
    const email = clean(b.email, 200).toLowerCase();
    const u = { id: userId(email), email, name: clean(b.name, 100), role: 'admin', pages: [], active: true, must_change: true,
                created_at: Date.now(), pw: hashPassword(crypto.randomUUID()) };
    await putRecord('users', u.id, u);
    const link = await issueReset(u, siteOrigin(req));
    await audit(ACTOR, 'bootstrap_admin', { target: email });
    return ok(res, { setup_link: link });
  }
  return bad(res, 'not found', 404);
}
