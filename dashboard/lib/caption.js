// A Studio post's text: the caption and hashtags as the dashboard keeps a routine's, and the text
// that is posted. No packages, so the Python side's test can check its own rendering (the text a
// reviewer is given, sent back as checked_caption) against this one (tests/test_studio_jobs.py).
import { clean } from './http.js';

// the longest caption kept, in characters (code points: an emoji is never cut in half)
export const CAPTION_MAX = 5000;
export const cutText = (s, n) => Array.from(String(s ?? '').trim()).slice(0, n).join('');
export const routineCaption = c => cutText(c, CAPTION_MAX);
// (bore/studio.py _tags renders them the same way)
export const routineTags = tags => tags.map(t => clean(t, 200)).filter(Boolean).map(t => (t.startsWith('#') ? t : `#${t}`).slice(0, 60)).slice(0, 30);
export const postText = p => [p.caption || '', (p.hashtags || []).join(' ')].join('\n').trim();
