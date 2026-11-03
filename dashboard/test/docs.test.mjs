// Reference documents of any realistic size: the browser sends them in parts under the 4.5 MB
// request limit, the server joins and reads them on the last call, and the routine downloads them
// part by part. Every document here is made up (the pages' own documents are confidential).
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
const { client } = await import('./helpers.mjs');
const { PART_BYTES } = await import('../routes/admin.js');
const { putRecord } = await import('../lib/db.js');
const mem = globalThis.__boreBlobs;

const R = { routine: true };
const REQUEST_LIMIT = 4.5e6;       // Vercel refuses request (and response) bodies over 4.5 MB

// A valid PDF: one page per entry, with text (a made-up template's zones) or only shapes (a page
// saved as a picture); pad adds an unreferenced stream of that many random bytes (example photos).
function makePdf(pages, pad = 0) {
  const objs = [`<< /Type /Catalog /Pages 2 0 R >>`,
    `<< /Type /Pages /Kids [${pages.map((_, i) => `${4 + 2 * i} 0 R`).join(' ')}] /Count ${pages.length} >>`,
    `<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>`];
  const stream = body => Buffer.concat([Buffer.from(`<< /Length ${body.length} >>\nstream\n`), body, Buffer.from('\nendstream')]);
  pages.forEach((text, i) => {
    objs.push(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents ${5 + 2 * i} 0 R >>`);
    objs.push(stream(Buffer.from(text ? `BT /F1 20 Tf 60 700 Td (${text}) Tj ET` : '0.2 0.4 0.8 rg 60 60 480 660 re f 1 1 1 rg 100 500 300 80 re f')));
  });
  if (pad) objs.push(stream(crypto.randomBytes(pad)));
  const out = [Buffer.from('%PDF-1.4\n')], at = [];
  let off = out[0].length;
  objs.forEach((o, i) => {
    const b = Buffer.concat([Buffer.from(`${i + 1} 0 obj\n`), Buffer.isBuffer(o) ? o : Buffer.from(o), Buffer.from('\nendobj\n')]);
    at.push(off); out.push(b); off += b.length;
  });
  out.push(Buffer.from(`xref\n0 ${objs.length + 1}\n0000000000 65535 f \n${at.map(o => `${String(o).padStart(10, '0')} 00000 n \n`).join('')}`
    + `trailer\n<< /Size ${objs.length + 1} /Root 1 0 R >>\nstartxref\n${off}\n%%EOF\n`));
  return Buffer.concat(out);
}
const sha = buf => crypto.createHash('sha256').update(buf).digest('hex');

let admin, robot, slug;
async function setup() {
  if (admin) return;
  admin = client(); robot = client();
  const boot = await robot.call('POST', '/api/routine/bootstrap', { email: 'owner@example.com', name: 'O' }, R);
  await admin.call('POST', '/api/auth/reset', { token: boot.body.setup_link.split('#reset=')[1], password: 'correct horse 42' });
  assert.equal((await admin.call('POST', '/api/auth/login', { email: 'owner@example.com', password: 'correct horse 42' })).status, 200);
  slug = (await admin.call('POST', '/api/admin/pages', { name: 'Dummy Docs', socialpilot_login_id: '1' })).body.page.slug;
}

// what app.js does: start, every part (base64), finish; each request under the limit
async function upload(type, pdf, { skip = [], filename = 'book.pdf', hash = sha(pdf) } = {}) {
  const base = `/api/admin/pages/${slug}/docs/${type}`;
  const st = await admin.call('POST', `${base}/start`, { filename, size: pdf.length });
  if (st.status !== 200) return st;
  const { upload: id, part_bytes, parts } = st.body;
  for (let i = 0; i < parts; i++) {
    if (skip.includes(i)) continue;
    const body = { upload: id, index: i, data_b64: pdf.subarray(i * part_bytes, (i + 1) * part_bytes).toString('base64') };
    assert.ok(JSON.stringify(body).length < REQUEST_LIMIT, 'every part fits in one request');
    const r = await admin.call('POST', `${base}/part`, body);
    if (r.status !== 200) return r;
  }
  const finish = () => admin.call('POST', `${base}/finish`, { upload: id, parts, size: pdf.length, sha256: hash, filename });
  return Object.assign(await finish(), { id, parts, finish, base });
}

const auditActions = async () => (await admin.call('GET', '/api/admin/audit')).body.entries;

test('a 6 MB templates document goes up in parts, becomes the active version and comes back whole', async () => {
  await setup();
  const pdf = makePdf(['Made-up Card One: title area up to 8 words, photo area full width',
                       'Made-up Card Two: big figure area, tag line up to 30 characters'], 6_000_000);
  assert.ok(pdf.length > REQUEST_LIMIT);
  const one = await admin.call('POST', `/api/admin/pages/${slug}/docs/templates`, { filename: 'book.pdf', data_b64: pdf.toString('base64') });
  assert.equal(one.status, 413, 'too big for one request: the parts are the way');
  assert.match(one.body.error, /in parts/);

  // a part that never arrived: nothing changes, and the message says what to do
  const gap = await upload('templates', pdf, { skip: [2] });
  assert.equal(gap.status, 400);
  assert.equal(gap.body.error, 'part 3 of 3 did not arrive: upload the file again');
  assert.equal((await admin.call('GET', '/api/admin/pages')).body.pages.find(p => p.slug === slug).docs.templates.active, null);

  const up = await upload('templates', pdf);
  assert.equal(up.parts, Math.ceil(pdf.length / PART_BYTES));
  assert.equal(up.status, 200, JSON.stringify(up.body));
  const v = up.body.version;
  assert.deepEqual([v.v, v.size, v.pages, v.image_only, v.warning], [1, pdf.length, 2, false, '']);
  assert.ok(v.chars > 80 && up.body.extracted_chars === v.chars, `the characters of text the pages hold: ${v.chars}`);
  assert.equal(up.body.page.docs.templates.active, 1);
  assert.ok(!JSON.stringify(up.body).includes('.enc') && !JSON.stringify(up.body).includes(up.id), 'no storage paths reach the browser');

  assert.ok(up.body.conversion.job, 'its templates are queued for conversion');
  // its answer was lost and the browser asks again: the same version, not a second one, and no second
  // conversion (the answer still says its templates are converted, as the first one did)
  const convJobs = async () => (await robot.call('GET', '/api/routine/jobs', undefined, R)).body.jobs.filter(j => j.kind === 'templates').length;
  const jobsBefore = await convJobs();
  const again = await up.finish();
  assert.equal(again.status, 200);
  assert.equal(again.body.version.v, 1);
  assert.equal(again.body.page.docs.templates.versions.length, 1);
  assert.deepEqual([again.body.conversion, await convJobs()], [{ job: up.body.conversion.job }, jobsBefore]);
  // the first try saved the version, then died before queueing the conversion (a slow PDF read): the retry queues it
  await putRecord('templates', slug, { page: slug, specs: {} });
  const late = await up.finish();
  assert.deepEqual([late.status, late.body.version.v, !!late.body.conversion.job], [200, 1, true]);

  // the routine: the text in config, the PDF in parts that join to the same file
  const cfg = (await robot.call('GET', '/api/routine/config', undefined, R)).body.pages.find(p => p.slug === slug).docs.templates;
  assert.deepEqual([cfg.version, cfg.size, cfg.parts, cfg.sha256, cfg.chars], [1, pdf.length, up.parts, sha(pdf), v.chars]);
  assert.match(cfg.text, /Made-up Card One/);
  assert.equal((await robot.call('GET', `/api/routine/doc?page=${slug}&type=templates`, undefined, R)).status, 400, 'a part is asked for by number');
  const got = [], reads = async () => (await auditActions()).filter(e => e.action === 'reference_read').length;
  const before = await reads();
  for (let i = 0; i < cfg.parts; i++) {
    const r = await robot.call('GET', `/api/routine/doc?page=${slug}&type=templates&part=${i}`, undefined, R);
    assert.equal(r.headers['x-doc-parts'], String(cfg.parts));
    assert.ok(r.body.length < REQUEST_LIMIT, 'every part fits in one response');
    got.push(Buffer.from(r.body));
  }
  assert.ok(Buffer.concat(got).equals(pdf));
  assert.equal(await reads(), before + 1, 'one audit entry per download, not one per part');
  assert.equal((await robot.call('GET', `/api/routine/doc?page=${slug}&type=templates&part=${cfg.parts}`, undefined, R)).status, 404);

  // rolling back keeps working with versions in parts, and an old one-request upload still works
  const small = makePdf(['Made-up Card Three: quote area']);
  const s = await admin.call('POST', `/api/admin/pages/${slug}/docs/templates`, { filename: 's.pdf', data_b64: small.toString('base64') });
  assert.equal(s.status, 200);
  assert.equal(s.body.version.v, 2);
  const s1 = await robot.call('GET', `/api/routine/doc?page=${slug}&type=templates`, undefined, R);
  assert.ok(Buffer.from(s1.body).equals(small), 'a one-part document needs no part number');
  const rb = await admin.call('PATCH', `/api/admin/pages/${slug}/docs/templates`, { active: 1 });
  assert.equal(rb.body.page.docs.templates.active, 1);

  const log = await auditActions();
  for (const a of ['reference_upload_started', 'reference_uploaded', 'reference_upload_failed', 'reference_read'])
    assert.ok(log.some(e => e.action === a), a);
  assert.equal(log.filter(e => e.action === 'reference_uploaded').length, 2, 'a finish sent twice is one upload');
  assert.ok(log.some(e => e.action === 'reference_upload_failed' && e.reason === 'missing_part'));
});

test('a part sent again after its answer was lost replaces itself', async () => {
  await setup();
  const pdf = makePdf(['Made-up Card Four: two photos side by side'], 3_000_000);
  const base = `/api/admin/pages/${slug}/docs/visual`;
  const { upload: id, part_bytes, parts } = (await admin.call('POST', `${base}/start`, { filename: 'v.pdf', size: pdf.length })).body;
  for (let i = 0; i < parts; i++) {
    const body = { upload: id, index: i, data_b64: pdf.subarray(i * part_bytes, (i + 1) * part_bytes).toString('base64') };
    assert.equal((await admin.call('POST', `${base}/part`, body)).status, 200);
    assert.equal((await admin.call('POST', `${base}/part`, body)).status, 200, 'the same part twice');
  }
  const r = await admin.call('POST', `${base}/finish`, { upload: id, parts, size: pdf.length, sha256: sha(pdf), filename: 'v.pdf' });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.equal(r.body.version.size, pdf.length);
});

test('an upload that is too big, not a PDF or does not add up is refused with what to do, and the try is audited', async () => {
  await setup();
  const base = `/api/admin/pages/${slug}/docs/tone`;
  const big = await admin.call('POST', `${base}/start`, { filename: 'huge.pdf', size: 60_000_000 });
  assert.equal(big.status, 413);
  assert.equal(big.body.error, 'this PDF is 60.0 MB and the limit is 50.0 MB: export it with smaller images, or split it into two documents');
  assert.equal((await admin.call('POST', `${base}/start`, { filename: 'e.pdf', size: 0 })).status, 400);

  const st = (await admin.call('POST', `${base}/start`, { filename: 'notes.docx', size: 2000 })).body;
  const notPdf = await admin.call('POST', `${base}/part`, { upload: st.upload, index: 0, data_b64: Buffer.from('PK\x03\x04 a word file').toString('base64') });
  assert.equal(notPdf.status, 400);
  assert.match(notPdf.body.error, /not a PDF: export the document as a PDF/);
  assert.equal((await admin.call('POST', `${base}/part`, { upload: 'made-up', index: 0, data_b64: 'JVBERi0=' })).status, 400, 'an upload start gave');
  assert.equal((await admin.call('POST', `${base}/part`, { upload: st.upload, index: 99, data_b64: 'JVBERi0=' })).status, 400);

  const pdf = makePdf(['Made-up tone notes: short sentences']);
  const bad = await upload('tone', pdf, { hash: sha(Buffer.from('another file')) });
  assert.equal(bad.status, 400);
  assert.equal(bad.body.error, 'the parts that arrived do not add up to the file you picked: upload it again');
  const page = (await admin.call('GET', '/api/admin/pages')).body.pages.find(p => p.slug === slug);
  assert.equal(page.docs.tone.active, null, 'nothing was saved');

  const reasons = (await auditActions()).filter(e => e.action === 'reference_upload_failed' && e.type === 'tone').map(e => e.reason);
  for (const r of ['too_large', 'not_pdf', 'mismatch']) assert.ok(reasons.includes(r), r);
});

test('a PDF whose pages are pictures is flagged: almost no text for the ideas step', async () => {
  await setup();
  const up = await upload('visual', makePdf(['', '', '', ''], 400_000));
  assert.equal(up.status, 200, JSON.stringify(up.body));
  assert.deepEqual([up.body.version.pages, up.body.version.chars, up.body.version.image_only], [4, 0, true]);
  const listed = (await admin.call('GET', '/api/admin/pages')).body.pages.find(p => p.slug === slug).docs.visual;
  assert.equal(listed.versions.find(v => v.v === listed.active).image_only, true);
  // a document with its text is not
  const text = await upload('visual', makePdf(Array.from({ length: 4 }, (_, i) => `Made-up reference ${i + 1}: a photo on a flat colour, the page name top left`)));
  assert.equal(text.body.version.image_only, false);
});

test('parts of an upload that never finished are removed a day later; finished ones stay', async () => {
  await setup();
  const done = await upload('templates', makePdf(['Made-up Card Five: dates in a row'], 2_600_000));
  assert.equal(done.status, 200);
  const base = `/api/admin/pages/${slug}/docs/templates`;
  const left = (await admin.call('POST', `${base}/start`, { filename: 'x.pdf', size: 3_000_000 })).body;
  await admin.call('POST', `${base}/part`, { upload: left.upload, index: 0, data_b64: makePdf(['x']).toString('base64') });
  const dayAgo = new Date(Date.now() - 25 * 3600e3);
  for (const [k, v] of mem) if (k.startsWith(`docs/${slug}/templates/`)) v.uploadedAt = dayAgo;
  await admin.call('POST', `${base}/start`, { filename: 'y.pdf', size: 1000 });
  const kept = [...mem.keys()].filter(k => k.startsWith(`docs/${slug}/templates/`));
  assert.ok(!kept.some(k => k.includes(left.upload)), 'the unfinished upload is gone');
  assert.ok(kept.some(k => k.includes(done.id)), 'the version made from parts keeps them');
  const cfg = (await robot.call('GET', '/api/routine/config', undefined, R)).body.pages.find(p => p.slug === slug).docs.templates;
  const r = await robot.call('GET', `/api/routine/doc?page=${slug}&type=templates&part=0`, undefined, R);
  assert.equal(r.status, 200);
  assert.equal(cfg.parts, 2);
});

test('a templates version made active whose conversion cannot be queued says so in a plain sentence', async () => {
  await setup();
  for (const [f, text] of [['a.pdf', 'Made-up Card Six: name area'], ['b.pdf', 'Made-up Card Seven: score area']])
    assert.equal((await admin.call('POST', `/api/admin/pages/${slug}/docs/templates`, { filename: f, data_b64: makePdf([text]).toString('base64') })).status, 200);
  const v = (await admin.call('GET', '/api/admin/pages')).body.pages.find(p => p.slug === slug).docs.templates.active - 1;
  await putRecord('templates', slug, { page: slug, specs: {} });          // none of its templates converted yet
  // storage refuses the conversion job (an outage)
  const set = mem.set;
  mem.set = function (k, val) { if (String(k).startsWith('db/jobs/')) throw new Error('made-up storage refusal 503'); return set.call(this, k, val); };
  let r;
  try { r = await admin.call('PATCH', `/api/admin/pages/${slug}/docs/templates`, { active: v }); } finally { mem.set = set; }
  assert.equal(r.status, 200);
  assert.equal(r.body.page.docs.templates.active, v, 'the version is made active all the same');
  assert.deepEqual(r.body.conversion, { job: null, error: 'The templates could not be queued for conversion: press Convert templates again under Templates.' });
});
