import test from 'node:test';
import assert from 'node:assert/strict';
import { client } from './helpers.mjs';

const R = { routine: true };

// The topic feed remembers what the next hour compares with (bore/studio.py prepare_feed): the
// posts each story's text was written from, and the writer's 'not news' verdict.
test('the feed keeps written_from and the not-news verdict for the next hour', async () => {
  const admin = client(), robot = client();
  const boot = await robot.call('POST', '/api/routine/bootstrap', { email: 'owner@example.com', name: 'Owner' }, R);
  const token = boot.body.setup_link.split('#reset=')[1];
  assert.equal((await admin.call('POST', '/api/auth/reset', { token, password: 'correct horse 42' })).status, 200);
  const slug = (await admin.call('POST', '/api/admin/pages', { name: 'Dummy Feed' })).body.page.slug;

  const urls = Array.from({ length: 40 }, (_, i) => `https://x.com/a/status/${i}`);
  const stories = [
    { id: 'k1', headline: 'Written', summary: 'S.', score: 5, published_at: Date.now(), url: urls[0], sources: [],
      written_from: urls },
    { id: 'k2', headline: 'ordered grubhub at 6am', summary: '', score: 4, published_at: Date.now(), url: 'u', sources: [],
      written_from: ['https://reddit.com/r/x/1'], not_news: 'a personal anecdote, no news' },
  ];
  assert.equal((await robot.call('POST', '/api/routine/feed', { page: slug, stories }, R)).status, 200);
  const got = (await robot.call('GET', `/api/routine/feed?page=${slug}`, undefined, R)).body.feed.stories;
  assert.deepEqual(got[0].written_from, urls.slice(0, 30));
  assert.equal(got[0].not_news, undefined);
  assert.equal(got[1].not_news, 'a personal anecdote, no news');
});
