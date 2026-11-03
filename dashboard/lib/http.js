// Small helpers shared by the routes.
export const ok = (res, body = {}) => res.status(200).json({ ok: true, ...body });
export const bad = (res, error, status = 400) => res.status(status).json({ error });
export const method = (req, ...m) => m.includes(req.method);
export const clean = (s, n = 500) => String(s ?? '').trim().slice(0, n);
export const slugify = s => clean(s, 60).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
export function siteOrigin(req) {
  const host = req.headers['x-forwarded-host'] || req.headers.host;
  return `https://${host}`;
}
