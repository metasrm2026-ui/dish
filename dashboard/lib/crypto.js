// AES-256-GCM for everything protected (reference documents, sources, users, batches, logs).
// Blob URLs are public-but-unguessable, so protected content is only ever stored encrypted.
// DATA_KEY (32 bytes, base64) lives only in the hosting provider's secret settings.
import crypto from 'node:crypto';

function key() {
  const k = Buffer.from(process.env.DATA_KEY || '', 'base64');
  if (k.length !== 32) throw new Error('DATA_KEY is not set (32 bytes, base64)');
  return k;
}

export function encrypt(data) {
  const iv = crypto.randomBytes(12);
  const c = crypto.createCipheriv('aes-256-gcm', key(), iv);
  const body = Buffer.concat([c.update(Buffer.isBuffer(data) ? data : Buffer.from(String(data))), c.final()]);
  return Buffer.concat([Buffer.from('BE1'), iv, c.getAuthTag(), body]);
}

export function decrypt(buf) {
  buf = Buffer.from(buf);
  if (buf.subarray(0, 3).toString() !== 'BE1') throw new Error('not an encrypted record');
  const iv = buf.subarray(3, 15), tag = buf.subarray(15, 31);
  const d = crypto.createDecipheriv('aes-256-gcm', key(), iv);
  d.setAuthTag(tag);
  return Buffer.concat([d.update(buf.subarray(31)), d.final()]);
}

export const encryptJson = obj => encrypt(JSON.stringify(obj));
export const decryptJson = buf => JSON.parse(decrypt(buf).toString('utf8'));
export const randomId = (n = 10) => crypto.randomBytes(n).toString('base64url');
