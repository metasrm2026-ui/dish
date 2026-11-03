// Encrypted record store on Vercel Blob.
//   db/<collection>/<id>/<version>.enc   one file per version; the newest is the record
// Versioned paths mean a cached CDN copy can never be served as current (images and blobs are
// cached for 30 days). Old versions are pruned on write, keeping a few for recovery.
// Every write goes through updateRecord: it reads the newest version n and creates version n+1
// with a create-only put, so of two requests that changed the same version only one lands; the
// other reads again and makes its change on top (an editor's edit and a job result both survive).
// Versions are 17-digit numbers (older ones were the write time in µs plus a random suffix,
// <stamp>-<rand>.enc): n+1 sorts after either, and so would a write by the old code.
import { put, list, del, requestWrites } from './blob.js';
import { encryptJson, decryptJson, encrypt, decrypt, randomId } from './crypto.js';

const KEEP_VERSIONS = 3;
// a version is pruned only once no request can still be about to write on top of it (a request
// runs at most 60 s): otherwise its number could be taken again by a writer that read it
const PRUNE_AFTER_MS = 5 * 60e3;
const WRITE_TRIES = 8;
export const READ_CONCURRENCY = 8;       // storage reads at once per request (hundreds at once get challenged)
const sleep = ms => new Promise(r => setTimeout(r, ms));

export async function listBlobs(prefix) {
  const out = [];
  let cursor;
  do {
    const r = await list({ prefix, cursor, limit: 1000 });
    out.push(...r.blobs);
    cursor = r.hasMore ? r.cursor : undefined;
  } while (cursor);
  return out;
}

// At most n at a time, in order. Like Promise.all, it fails on the first failure (and starts no
// more), but a list of hundreds no longer fires hundreds of storage reads at once.
export async function mapLimit(items, n, fn) {
  const out = new Array(items.length);
  let next = 0, failed = false;
  const worker = async () => {
    while (!failed && next < items.length) {
      const i = next++;
      try { out[i] = await fn(items[i], i); } catch (e) { failed = true; throw e; }
    }
  };
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, worker));
  return out;
}

// Storage refused a read (a Vercel security check, a rate limit, an outage) and kept refusing it:
// the request answers 503 with this message (main.js), never a 500 or a web page taken for data.
// "Nothing was changed" only when this request has not written anything yet (lib/blob.js): a read
// after a write (a batch updated after the draft was approved) can fail too.
export class StorageChallenged extends Error {
  constructor(status, checkpoint) {
    const s = requestWrites.getStore();
    super(`${checkpoint ? 'storage is running a security check on the dashboard\'s reads' : `storage is not answering (${status || 'no connection'})`}: `
          + (s && !s.wrote ? 'nothing was changed, try again in a few minutes'
                           : 'part of this may already be saved, so check whether it went through before trying again in a few minutes'));
    this.name = 'StorageChallenged';
    this.status = status;
    this.checkpoint = checkpoint;
  }
}
export const errorStatus = e => (e instanceof StorageChallenged ? 503 : 500);

// Record versions, log entries and status files never change once written, so each instance keeps
// the ones it has read (readBlob): a poll or a repeated list fetches only what changed. The key
// holds the upload time and size from the listing, so a path written again is never served from here.
const CACHE_BYTES = 32 * 1024 * 1024, CACHE_ITEM = 1024 * 1024;
const cache = new Map();
let cachedBytes = 0, paused = { until: 0 }, refusals = 0;
export const blobKey = b => `${b.url}|${new Date(b.uploadedAt).getTime()}|${b.size}`;

function remember(key, buf) {
  if (!key || buf.length > CACHE_ITEM || cache.has(key)) return;
  cache.set(key, buf);
  cachedBytes += buf.length;
  for (const [k, v] of cache) {                      // oldest first
    if (cachedBytes <= CACHE_BYTES) break;
    cache.delete(k);
    cachedBytes -= v.length;
  }
}

// tests: forget what was read, and any pause after a security check
export function resetReads() { cache.clear(); cachedBytes = 0; paused = { until: 0 }; refusals = 0; }

const isCheckpoint = text => /Vercel Security Checkpoint/i.test(text);
const RETRY_STATUS = s => s === 403 || s === 429 || s >= 500;

// One blob from the CDN, as it is now (never from memory). 403/429/5xx and dropped connections are
// retried with jitter. A security check that lasts pauses this instance's reads for a moment: more
// reads only prolong it. So does a run of refusals across this instance's reads (a list read all at
// once): the reads still waiting to retry give up, instead of each trying four times.
export async function fetchBlob(url) {
  const base = Number(process.env.BLOB_RETRY_MS || 250);
  for (let i = 0; ; i++) {
    if (Date.now() < paused.until) throw new StorageChallenged(paused.status, paused.checkpoint);
    let r = null, text = '';
    try { r = await fetch(url, { cache: 'no-store' }); } catch (e) { r = null; }
    if (r && r.ok) {
      const buf = Buffer.from(await r.arrayBuffer());
      // a checkpoint page served as the blob (none of ours starts with "<")
      if (!(buf[0] === 0x3c && isCheckpoint(buf.toString('utf8', 0, 16384)))) { refusals = 0; return buf; }
      text = 'Vercel Security Checkpoint';
    } else if (r && !RETRY_STATUS(r.status)) {
      throw new Error(`blob fetch ${r.status}`);   // e.g. 404: a missing blob is not a storage problem
    } else if (r) text = await r.text().catch(() => '');
    const checkpoint = isCheckpoint(text), status = r ? r.status : 0;
    if ((checkpoint || status === 429) && (i >= 3 || ++refusals >= READ_CONCURRENCY)) {
      paused = { until: Date.now() + 20e3, status, checkpoint };
      throw new StorageChallenged(status, checkpoint);
    }
    if (i >= 3) throw new StorageChallenged(status, checkpoint);
    await sleep(base * 2 ** i * (0.5 + Math.random()));
  }
}

// A blob that never changes once written, decoded. key: its listing key (blobKey), or none to read
// it from storage every time. Kept in memory only once it decoded: a broken answer is never kept.
export async function readBlob(url, key, decode = buf => buf) {
  const hit = key && cache.get(key);
  if (hit) { cache.delete(key); cache.set(key, hit); return decode(hit); }
  const buf = await fetchBlob(url);
  const out = decode(buf);
  remember(key, buf);
  return out;
}

const newestFirst = (a, b) => (a.pathname < b.pathname ? 1 : -1);
const versionOf = b => parseInt(b.pathname.split('/').pop(), 10) || 0;
const versionsOf = async (coll, id) => (await listBlobs(`db/${coll}/${id}/`)).sort(newestFirst);
const readVersion = b => readBlob(b.url, blobKey(b), decryptJson);

// Change a record safely when several requests may change it at once: mutate(current) gets the
// newest copy (null when there is none) and returns the record to save, or null to leave it as it
// is. When another request saved first, mutate runs again on its copy, so keep slow work (image
// uploads, waking a routine) outside it. Returns the saved record (or the unchanged current one).
export async function updateRecord(coll, id, mutate, { read = true } = {}) {
  // this call's earlier tries (paths and write tokens): one whose answer was lost may have landed,
  // and may show in the listing only after a later try of the same path was refused
  const tried = new Set(), tokens = new Set();
  for (let attempt = 1; ; attempt++) {
    const versions = await versionsOf(coll, id);
    for (const b of versions.filter(v => tried.has(v.pathname))) {
      if (tokens.has((await readVersion(b))._w)) return readVersion(versions[0]);   // it did: saved once
    }
    let cur = null;
    if (read && versions.length) {
      cur = await readVersion(versions[0]);
      if (cur._deleted) cur = null;
    }
    if (attempt > WRITE_TRIES) throw new Error('someone else is saving this at the same moment: try again');
    const next = await mutate(cur);
    if (next == null) return cur;
    const w = randomId(6);
    const rec = { ...next, _updated: Date.now(), _w: w };
    const path = `db/${coll}/${id}/${String((versions.length ? versionOf(versions[0]) : 0) + 1).padStart(17, '0')}.enc`;
    tried.add(path); tokens.add(w);
    try {
      await put(path, encryptJson(rec), { access: 'public', contentType: 'application/octet-stream',
                                          addRandomSuffix: false, allowOverwrite: false });
    } catch (e) {
      // taken: another request saved this version first (or this one did and its answer was lost):
      // read again and make the change on top of what is there
      if (!/already exists|blob exists/i.test(String(e && e.message)) && !(await listBlobs(path)).some(b => b.pathname === path)) throw e;
      await sleep(Math.min(500, 20 * 2 ** attempt) * (0.5 + Math.random()));
      continue;
    }
    const old = versions.slice(KEEP_VERSIONS - 1).filter(b => Date.now() - new Date(b.uploadedAt).getTime() > PRUNE_AFTER_MS);
    if (old.length) await del(old.map(b => b.url)).catch(() => {});
    return rec;
  }
}

// Saves a whole record, whatever it holds now. Only for records with a single writer: a change to a
// record others may be changing at the same time goes through updateRecord, a new one createRecord.
export async function putRecord(coll, id, obj) {
  await updateRecord(coll, id, () => obj, { read: false });
  return obj;
}

// A new record (a batch, a job): saved only while no record has that id.
export async function createRecord(coll, id, obj) {
  return updateRecord(coll, id, cur => { if (cur) throw new Error(`${coll} ${id} exists already`); return obj; });
}

export async function getRecord(coll, id) {
  const versions = await versionsOf(coll, id);
  if (!versions.length) return null;
  const rec = await readVersion(versions[0]);
  return rec._deleted ? null : rec;
}

export async function listRecords(coll) {
  const latest = {};
  for (const b of await listBlobs(`db/${coll}/`)) {
    const m = b.pathname.match(/^db\/[^/]+\/([^/]+)\/(.+)$/);
    if (m && (!latest[m[1]] || latest[m[1]].pathname < b.pathname)) latest[m[1]] = b;
  }
  const recs = await mapLimit(Object.values(latest), READ_CONCURRENCY, readVersion);
  return recs.filter(r => !r._deleted);
}

export async function deleteRecord(coll, id) {
  return putRecord(coll, id, { id, _deleted: true });
}

// Encrypted files (reference documents and their extracted text). overwrite: a part of an upload
// sent again after its answer was lost (the same bytes: a stale cached copy reads the same).
export async function putFile(path, buf, { overwrite = false } = {}) {
  await put(path, encrypt(buf), { access: 'public', contentType: 'application/octet-stream', addRandomSuffix: false,
                                  ...(overwrite ? { allowOverwrite: true } : {}) });
  return path;
}

export async function getFile(path) {
  const [b] = await listBlobs(path);
  if (!b || b.pathname !== path) return null;
  return readBlob(b.url, blobKey(b), decrypt);
}

// Public, unguessable images (base images, finished cards): not secret, never overwritten.
export async function putImage(prefix, buf, contentType = 'image/jpeg') {
  const ext = contentType.includes('png') ? 'png' : contentType.includes('webp') ? 'webp' : 'jpg';
  const r = await put(`${prefix}/${Date.now()}-${randomId(6)}.${ext}`, buf, { access: 'public', contentType, addRandomSuffix: false });
  return r.url;
}

// Append-only logs: log/<kind>/<yyyy-mm-dd>/<ts>-<rand>.enc
export async function appendLog(kind, entry) {
  const now = new Date();
  const path = `log/${kind}/${now.toISOString().slice(0, 10)}/${String(now.getTime()).padStart(14, '0')}-${randomId(4)}.enc`;
  await put(path, encryptJson({ at: now.getTime(), ...entry }), { access: 'public', contentType: 'application/octet-stream', addRandomSuffix: false });
}

export async function readLogs(kind, days = 7) {
  const blobs = [];
  for (let i = 0; i < days; i += 15) {              // days listed in parallel, 15 at a time
    const chunk = await Promise.all(Array.from({ length: Math.min(15, days - i) }, (_, j) =>
      listBlobs(`log/${kind}/${new Date(Date.now() - (i + j) * 86400e3).toISOString().slice(0, 10)}/`)));
    for (const c of chunk) blobs.push(...c);
  }
  const out = await mapLimit(blobs, READ_CONCURRENCY, readVersion);   // entries never change: cached
  return out.sort((a, b) => b.at - a.at);
}
