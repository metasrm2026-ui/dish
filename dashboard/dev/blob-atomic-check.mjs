// One-off check on the real Blob store that a create-only put is atomic: of 10 puts of one new path
// at once, exactly one may succeed, and the stored file must be that one's. updateRecord (lib/db.js)
// and the Studio job and publisher claims depend on it.
//   cd dashboard && BLOB_READ_WRITE_TOKEN=... node dev/blob-atomic-check.mjs
// About 25 storage requests (2 rounds of 10 puts, 2 reads, 1 list, 1 delete), only under selftest/,
// deleted at the end. Exit 0: atomic; 1: not atomic (do not rely on updateRecord); 2: could not run,
// or every put of a round failed (read the errors it prints: storage refusing, not two winners).
import { put, list, del } from '@vercel/blob';
import { randomBytes } from 'node:crypto';

process.env.VERCEL_BLOB_RETRIES = '0';      // one request per put (the client would retry up to 10 times)
if (!process.env.BLOB_READ_WRITE_TOKEN) { console.error('set BLOB_READ_WRITE_TOKEN'); process.exit(2); }
const dir = `selftest/${randomBytes(8).toString('hex')}`;
let atomic = true, urls = null, rounds = 0;
try {
  for (const round of [1, 2]) {
    const path = `${dir}/x${round}.json`;
    const res = await Promise.allSettled(Array.from({ length: 10 }, (_, i) => put(path, JSON.stringify({ writer: i }),
      { access: 'public', contentType: 'application/json', addRandomSuffix: false, allowOverwrite: false })));
    const won = res.flatMap((r, i) => (r.status === 'fulfilled' ? [{ i, url: r.value.url }] : []));
    const lost = [...new Set(res.filter(r => r.status === 'rejected').map(r => `${r.reason.constructor.name}: ${r.reason.message}`))];
    const stored = won.length === 1 ? await (await fetch(won[0].url, { cache: 'no-store' })).json() : null;
    console.log(`round ${round}: ${won.length} of 10 succeeded; stored ${JSON.stringify(stored)}; the others got: ${lost.join(' | ') || '-'}`);
    if (won.length) { rounds++; atomic = atomic && won.length === 1 && stored.writer === won[0].i; }
  }
  urls = (await list({ prefix: `${dir}/` })).blobs.map(b => b.url);
  console.log(`files under ${dir}: ${urls.length} (${rounds} expected)`);
  atomic = atomic && urls.length >= rounds;                // each round with a winner stored its file
  if (atomic && rounds < 2) atomic = null;     // a round where no put succeeded proves nothing
} catch (e) {
  console.error(`could not finish: ${e.message}`);
  atomic = null;
} finally {
  if (!urls) urls = (await list({ prefix: `${dir}/` }).catch(() => ({ blobs: [] }))).blobs.map(b => b.url);
  if (urls.length) await del(urls).catch(e => console.error(`delete ${dir} by hand: ${e.message}`));
}
console.log(atomic ? 'ATOMIC: create-only puts let exactly one writer win' : atomic === null ? 'NOT CHECKED' : 'NOT ATOMIC');
process.exit(atomic ? 0 : atomic === null ? 2 : 1);
