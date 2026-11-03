// The routine's saved database (routes/state.js): a product that never saved one says so (the first
// hourly run then starts a new one), and once one was saved, finding none stops the run.
import test from 'node:test';
import assert from 'node:assert/strict';
import { client } from './helpers.mjs';

const { default: state } = await import('../routes/state.js');
const gz = Buffer.from([0x1f, 0x8b, 8, 0, 1, 2, 3]);

async function call(method, body) {
  const req = { method, headers: { authorization: `Bearer ${process.env.ROUTINE_SECRET}` },
    async *[Symbol.asyncIterator]() { if (body) yield body; } };
  const out = { status: 200, body: null, headers: {} };
  const res = { setHeader(k, v) { out.headers[k] = v; }, status(s) { out.status = s; return res; },
    json(o) { out.body = o; return res; }, send(b) { out.body = b; return res; }, end() { return res; } };
  await state(req, res);
  return out;
}

test('never saved: 404 never_saved; saved then lost: 503, never a first run again', async () => {
  const first = await call('GET');
  assert.equal(first.status, 404);
  assert.equal(first.body.never_saved, true);
  assert.equal((await call('PUT', gz)).status, 200);
  assert.equal((await call('PUT', gz)).status, 200, 'the second save keeps working');
  const back = await call('GET');
  assert.equal(back.status, 200);
  const { list, del } = await import('../lib/blob.js');
  const { blobs } = await list({ prefix: 'state/' });
  await del(blobs.map(b => b.url));
  const lost = await call('GET');
  assert.equal(lost.status, 503);
  assert.match(lost.body.error, /state missing.*Do not run/);
  assert.equal(lost.body.never_saved, undefined);
  void client;
});
