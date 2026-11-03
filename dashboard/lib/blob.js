// Storage backend. Production: Vercel Blob. Tests / local runs (BLOB_MOCK=1): an in-memory
// store with mock:// URLs, served by a patched fetch (see test/helpers.mjs).
import * as vercel from '@vercel/blob';
import { AsyncLocalStorage } from 'node:async_hooks';

const mem = globalThis.__boreBlobs || (globalThis.__boreBlobs = new Map());
const mock = () => process.env.BLOB_MOCK === '1';
const base = () => process.env.BLOB_MOCK_BASE || 'mock://';   // dev server serves blobs over http

// Whether the current request has written anything (api/main.js runs each request in one): a
// storage error after a write must not tell the editor that nothing was changed. A write that
// failed counts too: its answer may have been lost after it landed.
export const requestWrites = new AsyncLocalStorage();
const writing = () => { const s = requestWrites.getStore(); if (s) s.wrote = true; };

// Storage refusing a write, a listing or a delete (an outage, a rate limit): said in plain words, with
// whether anything may already be saved (db.js StorageChallenged), never the storage library's own
// message. "blob exists" is the create-only answer updateRecord and the claims rely on: passed as it is.
async function plain(fn) {
  try { return await fn(); } catch (e) {
    if (/already exists|blob exists/i.test(String(e && e.message))) throw e;
    const { StorageChallenged } = await import('./db.js');      // (db.js imports this module)
    if (e instanceof StorageChallenged) throw e;
    console.error('storage:', e && e.message);
    throw new StorageChallenged(Number(e && e.status) || 0, false);
  }
}

export const put = (pathname, body, opts = {}) => plain(() => putRaw(pathname, body, opts));
export const list = (opts = {}) => plain(() => listRaw(opts));
export const del = urls => plain(() => delRaw(urls));

async function putRaw(pathname, body, opts) {
  writing();
  if (!mock()) return vercel.put(pathname, body, opts);
  // as strict as Vercel Blob: a string, Buffer or stream body, and options with access
  if (!(typeof body === 'string' || Buffer.isBuffer(body) || body instanceof Uint8Array || (body && typeof body.pipe === 'function')))
    throw new Error(`Vercel Blob: Body must be a string, buffer or stream (${pathname})`);
  if (!opts || opts.access !== 'public') throw new Error(`Vercel Blob: access must be "public" (${pathname})`);
  if (mem.has(pathname) && !opts.allowOverwrite && !opts.addRandomSuffix) throw new Error(`blob exists: ${pathname}`);
  const p = opts.addRandomSuffix ? `${pathname}-${Math.random().toString(36).slice(2, 8)}` : pathname;
  const buf = Buffer.isBuffer(body) ? body : Buffer.from(typeof body === 'string' ? body : JSON.stringify(body));
  mem.set(p, { buf, contentType: opts.contentType || 'application/octet-stream', uploadedAt: new Date() });
  return { url: `${base()}${p}`, pathname: p };
}

async function listRaw({ prefix = '', cursor, limit = 1000 } = {}) {
  if (!mock()) return vercel.list({ prefix, cursor, limit });
  const blobs = [...mem.entries()].filter(([k]) => k.startsWith(prefix))
    .map(([k, v]) => ({ pathname: k, url: `${base()}${k}`, uploadedAt: v.uploadedAt, size: v.buf.length }));
  return { blobs, hasMore: false };
}

async function delRaw(urls) {
  writing();
  if (!mock()) return vercel.del(urls);
  for (const u of [].concat(urls)) mem.delete(String(u).replace(base(), '').replace('mock://', ''));
}

export function mockFetch(realFetch) {
  return async (url, opts) => {
    const u = String(url);
    if (u.startsWith('mock://') || (process.env.BLOB_MOCK_BASE && u.startsWith(process.env.BLOB_MOCK_BASE))) {
      const v = mem.get(u.startsWith('mock://') ? u.slice(7) : u.slice(process.env.BLOB_MOCK_BASE.length));
      if (!v) return new Response('not found', { status: 404 });
      return new Response(v.buf, { status: 200, headers: { 'content-type': v.contentType } });
    }
    return realFetch(url, opts);
  };
}
