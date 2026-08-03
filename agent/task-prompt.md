# Daily sync runbook

You are running unattended. Nobody will answer a question, so never ask one —
decide, act, and report at the end.

The repo is mounted at `/workspace/reader`. All paths below are relative to it.
Read `config.json` first; it is the source of truth for the window, limits, and
output paths. Values in angle brackets below refer to it, e.g. `<window_days>`.

## 1. Load state

1. Read `config.json`.
2. Read `data/posts.json`. If it is missing or empty, this is a **first run**:
   treat the existing post set as empty and expect to backfill the full
   `<window_days>`. A missing file is not an error; a file that exists but does
   not parse **is** — go to the failure protocol.
3. Read `data/posts.schema.json`. You will validate against it in step 6.

Compute `cutoff` = today (UTC) minus `<window_days>` days. Posts published
before `cutoff` are out of window.

## 2. Discover

Try `source.feed_candidates` in order, then fall back to scraping
`source.index_url`. Use the first source that yields a parseable list of posts.
Record which one worked as `source_url`.

Follow pagination on the index until you reach posts older than `cutoff`, then
stop. Do not crawl the whole archive.

For each discovered post collect: URL, title, and publication date. Derive `id`
as the slug of the URL path — lowercase, hyphen-separated, matching the schema
pattern. An `id` is permanent: once a post is in `data/posts.json`, never
recompute or change its `id`, even if the source URL changes.

If discovery yields **zero** posts, that is a crawler failure, not an empty
week — go to the failure protocol. A working sync with no new posts is normal
and is handled in step 7.

## 3. Diff

Partition the discovered set against existing `data/posts.json`:

- **New** — `id` not present. Fetch it.
- **Changed** — `id` present, but the post looks materially updated (different
  title or publication date). Re-fetch it; you can only confirm a change by
  comparing `content_hash` after fetching.
- **Unchanged** — `id` present and nothing suggests an update. **Do not fetch.**
  Carry the existing record forward verbatim.
- **Prune** — record in `data/posts.json` whose `published_at` is before
  `cutoff`. Drop it, whether or not it still appears at the source.

Cap fetches at `<max_new_fetches_per_run>`. If more posts qualify, fetch the
most recently published ones first and note the shortfall in `last_sync.json`
`notes`; the next run picks up the rest. Never silently drop the overflow.

## 4. Fetch and extract

For each post to fetch, retrieve the page and extract:

- `body_text` — the article body as plain text. Strip navigation, headers,
  footers, cookie banners, share widgets, and "related posts". Keep paragraph
  breaks as newlines. No HTML, no Markdown.
- `title`, `published_at`, `authors`, `tags` — from the page. Omit `authors` or
  `tags` entirely if the page does not state them. **Never guess a date.** If
  you cannot find a publication date, treat that post as a failure for that
  post only: skip it and record it in `last_sync.json` `skipped`.
- `word_count` — whitespace-delimited count of `body_text`.
- `content_hash` — SHA-256 hex of `body_text`.
- `fetched_at` — now, RFC 3339 UTC.

If a re-fetched post's `content_hash` equals the stored one, it did not change;
keep the stored record and leave `fetched_at` as it was.

## 5. Write TL;DRs

Write a `tldr` for every new post, and for any changed post whose
`content_hash` moved. Between `<tldr.min_words>` and `<tldr.max_words>` words.

- Ground it **only** in that post's `body_text`. No outside knowledge, no
  speculation about what a release "means".
- Lead with what the post announces or argues, not with "This post…".
- Plain prose. No bullets, no headings, no marketing tone.

Reuse the existing `tldr` verbatim for unchanged posts. Never regenerate a TL;DR
you do not have to — it is the main cost driver of a run.

## 6. Validate — the gate

Assemble the full `posts.json` object: `version` 1, `generated_at` now,
`window_days`, `source_url`, and `posts` sorted by `published_at` descending.

Validate it against `data/posts.schema.json`. Also check, because the schema
cannot: every `id` is unique, and every `published_at` is on or after `cutoff`.

**If validation fails, stop. Commit nothing.** Go to the failure protocol. This
is the single most important rule in this runbook: a run that writes nothing is
a good outcome, and a run that commits malformed data is not.

## 7. Commit

Write `data/posts.json` and `data/last_sync.json`:

```json
{
  "ran_at": "2026-08-03T06:00:11Z",
  "source_url": "https://claude.com/blog/rss.xml",
  "window_days": 180,
  "counts": { "discovered": 41, "new": 2, "changed": 1, "unchanged": 38, "pruned": 1, "skipped": 0 },
  "skipped": [],
  "notes": []
}
```

Then commit and push to `main`:

```
<commit_prefix> YYYY-MM-DD (+2 new, 1 changed, 1 pruned)
```

If nothing changed — no new, no changed, no pruned — **do not create an empty
commit.** Write nothing, and report "no changes" at the end of your turn. A
quiet day is the expected case for a daily incremental sync.

## 8. Failure protocol

Triggered by: unparseable existing `data/posts.json`, zero posts discovered, all
sources unreachable, or schema validation failure.

1. **Commit nothing.** Leave the repo exactly as you found it.
2. File a GitHub issue on `sw00t/claude-blog-reader` using the **GitHub MCP
   server** — not `git`, and not the `bash` tool. MCP is authenticated by the
   vault credential attached to this deployment; the repo's own token only
   covers clone and push.
   - Title: `<failure.issue_title_prefix>: YYYY-MM-DD`
   - Labels: `<failure.issue_labels>`
   - Body: what stage failed, the exact error, the source URL tried, and this
     session's ID.
3. Before filing, list open issues and check for an existing open issue with the
   same title prefix. If one exists, add a comment instead of opening a
   duplicate — this runs daily and a persistent breakage must not produce a new
   issue every morning.
4. End your turn reporting the failure and the issue number.

## 9. Report

Close every run with one or two plain sentences: which source you used, the
counts, and whether you committed. If you skipped posts or hit the fetch cap,
say so explicitly rather than reporting a clean run.
