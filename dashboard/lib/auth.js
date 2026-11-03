// Who is calling.
//   Routines: Authorization: Bearer ROUTINE_SECRET (machine access, no user). While the secret is
//             rotated, ROUTINE_SECRET_NEXT is valid too: the routines move to it one by one, then
//             it becomes ROUTINE_SECRET and the old value is dropped (README, operations).
//   People:   a signed-in session (lib/session.js), resolved by the router into req.user.
export function isRoutine(req) {
  const h = req.headers.authorization || '';
  return [process.env.ROUTINE_SECRET, process.env.ROUTINE_SECRET_NEXT].some(s => !!s && h === `Bearer ${s}`);
}
export const isReviewer = req => !!req.user;
// the product owner (docs/PRODUCT.md "Roles"; lib/tenancy.js)
export const isAdmin = req => !!req.user && (req.user.role === 'super' || req.user.role === 'admin');
export function deny(res, msg = 'sign in first') {
  res.status(401).json({ error: msg });
}
