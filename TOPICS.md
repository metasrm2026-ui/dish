# Topic feed: instructions for the Claude session

You prepare the stories editors pick from in the Post Studio's topic selection ("Pick up to 5
stories", windows 3 / 12 / 24 hours). Every page gets a fresh, ranked list each hour: near-
duplicates from different sources merged into one story, each with a headline, a 2-3 line
summary, two visual ideas and candidate photos. Nothing is posted from here.

## 0. Setup

```bash
bash scripts/ensure-deps.sh      # packages, only if missing or not loading (python -m pip, never bare pip)
python -m bore.cli studio sync                     # pages, sources, playbooks, reference documents
python -m bore.cli routine state --action pull     # the stories the hourly routine collected
```

Never run `routine state --action push`: this session only reads the collected stories (the
hourly post routine owns the database). If the pull fails, run it once more; if it fails twice,
report it and stop (`studio feed` refuses to run until a pull has restored the database). If
`ensure-deps.sh` ends with "still not working", report its lines and stop: the photo checks cannot
run on this machine (`studio feed` refuses anyway). If it ends with "the card browser still does not
start", carry on: the topic feed draws no cards.

## 1. Build the feed

```bash
python -m bore.cli studio feed            # work/<run>/feed/<page>.json for every page
```

It checks each new story's candidate photos (graphics, duplicates, other outlets' photos), so it
can take several minutes: run it with a 10-minute command timeout. Among candidates that passed
the same checks, the kinds of photo editors picked before come first (the page's lessons; the
Studio records which candidate each editor chose). Each page gets up to 40
stories: the best of the last 3 hours, then 12, then 24; the same news found twice is merged into
one story. A story already in the current feed keeps its text (or your "not news" verdict) only
while its posts are still mostly the same ones: a story that split or grew into something else is
marked `needs_writing` again. A photo search by name runs only for a person whose face is known
(never on a crime or court story) and keeps only photos of that face. If it prints `PHOTO CHECKS
FAILED`, those stories stay unchecked and are tried next run: say so in your summary.

## 2. Write the new stories

For every story with `"needs_writing": true` (at most 15 per page), fill in, in that file:
- `headline`: one clear factual line (no emoji, no hashtags, no "BREAKING")
- `summary`: 2-3 short lines saying what happened, only from `posts`
- `ideas`: exactly two different visual ideas, as STUDIO.md "IDEAS" describes (`title`,
  `template_spec` and `template` naming one of the page's approved `templates` in the feed file
  when one fits the story, else `template` from the page's templates document `docs.templates`
  and `layout`; `description`), following the visual references document `docs.visual` (its
  text, and its page images when this machine has drawn them)

A post with a `link` is a Reddit post sharing that article: open the article for the facts (the
post itself is often only a title).

When a story is not news (a meme, a joke, a personal anecdote, a thread, a promo, a page that is
not a story such as a class schedule or an FAQ), don't write it: add `"not_news": "<why, a few
words>"` instead. The verdict is kept for as long as the story stays in the feed, so it is not
judged again every hour, and the Studio lists it last. Leave a story unwritten with no verdict
only when it is a private person's serious health crisis they have not made public. Crime, abuse,
terrorism and suicide stories are news: write them by ROUTINE.md's "Crime, abuse and tragedy are
news" rules. Work fast: short, accurate text beats long text.

## 3. Publish

```bash
python -m bore.cli studio feed-publish
```

End with one line per page: stories published, how many written this run, how many judged not
news.
