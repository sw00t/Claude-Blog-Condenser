# Daily sync runbook

You are running unattended. Nobody will answer a question, so never ask one —
decide, act, and report at the end.

The repo is mounted at `/workspace/reader`. All paths below are relative to it.
Read `config.json` first; it is the source of truth for the window, limits, and
output paths. Values in angle brackets below refer to it, e.g. `<window_days>`.

## 0. Run budget — read this first

Sessions are billed by wall-clock time as well as tokens, so a run that wanders
is expensive even when it eventually succeeds. Hard limits for one run:

- At most `<max_page_fetches_per_run>` HTTP fetches, total, including the index.
- At most `<max_new_fetches_per_run>` post bodies fetched and summarized.
- Target under ten minutes of work. If you are past that and still going, stop:
  commit what is fully processed, record the remainder in `notes`, end the turn.

**Keep page HTML out of your context.** `claude.com` pages carry a large
navigation, footer, and prompt-library shell around a small article. Fetch with
`curl` in the sandbox, write the response to a file, and strip it to article
text with a script — read only the stripped result. Never paste a raw page into
your reasoning.

**Do not load whole files you only need part of.** `data/posts.json` holds every
stored `body_text` and is the largest thing in this repo. Use `jq` or python to
read the fields you need (ids, hashes, dates, tldrs) and to write the file back.
Reading it end to end each run is the single biggest avoidable token cost here.

**Do not retry a failing approach more than twice.** Two attempts, then take the
documented alternative or stop. Loops are what make a run expensive.

Varying the request is **the same approach**, not a new one. Adding or removing
a trailing slash, swapping the user agent, changing headers, or re-requesting
the same URL another way all count against the same two attempts — they do not
start a fresh pair. Two failed fetches of a URL means that URL is unreachable:
go to the failure protocol instead of cycling variants. State the number of
attempts you actually made when you report or file an issue.

## 1. Load state

1. Read `config.json`.
2. Read `data/posts.json`. If it is missing or empty, this is a **first run**:
   treat the existing post set as empty and expect to backfill the full
   `<window_days>`. A missing file is not an error; a file that exists but does
   not parse **is** — go to the failure protocol.
3. Read `data/posts.schema.json`. You will validate against it in step 6.

Compute `cutoff` = today (UTC) minus `<window_days>` days. Posts published
before `cutoff` are out of window.

### 1b. Repair stored TL;DRs (before touching the network)

Check every stored post's `tldr` against the `<tldr>` word bounds. Regenerate
any that fall outside them from that post's **stored `body_text`**. This needs
no network access, so do it here, before discovery — otherwise a source outage
blocks repairs that have nothing to do with the source.

If discovery later fails and you enter the failure protocol, **still commit
these repairs**. They are independent of the source, and the "commit nothing"
rule exists to prevent publishing bad *sync* data, not to discard valid local
fixes. Commit them as `<commit_prefix> YYYY-MM-DD (N tldrs repaired)` and say
so in your report.

## 2. Discover — first page only

Fetch `source.index_url` **once**. That single response is your entire
discovery set. Record `source_url` as that URL.

`scope.follow_pagination` is `false`. This is a deliberate cost limit, not an
oversight:

- **Do not** follow "View more", "Next", or any pagination link.
- **Do not** construct paginated URLs. The site randomizes the pagination
  query-param prefix on every render (the same page emits both
  `?b7eea976_page=2` and `?d7430fcd_page=2`), so a constructed URL silently
  returns page 1 again. Guessing here costs a fetch and yields nothing.
- **Do not** fetch category archives, sitemaps, or feeds. None are in scope.
- Seeing far fewer posts than `window_days` would suggest is **expected and
  correct**. Do not treat it as a failure, do not note it as a shortfall, and
  do not file an issue about incomplete coverage.

Coverage of the full window builds up on its own: posts that scroll off page 1
stay in `data/posts.json` and are carried forward until they age past `cutoff`.
The dataset deepens week over week without ever crawling the archive.

For each discovered post collect: URL, title, and publication date. Derive `id`
as the slug of the URL path — lowercase, hyphen-separated, matching the schema
pattern. An `id` is permanent: once a post is in `data/posts.json`, never
recompute or change its `id`, even if the source URL changes.

Also collect `category` — the category label printed on that post's card on the
index page, copied verbatim (for example `Product announcements`, `Enterprise
AI`, `Claude Code`, `Agents`). If a card shows no category, omit the field.
Never infer a category from the title or body.

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

**Absence from page 1 is never a reason to drop a post.** Under
`index_first_page_only` scope, most stored posts will not appear in today's
discovery set at all. Date is the only prune criterion. A post disappears from
`data/posts.json` when it ages out, never because you stopped seeing it.

Cap fetches at `<max_new_fetches_per_run>`. If more posts qualify, fetch the
most recently published ones first and note the shortfall in `last_sync.json`
`notes`; the next run picks up the rest. Never silently drop the overflow.

**A shortfall is a success, not a failure.** On a first run the whole window is
new, so the backfill is *designed* to take several runs — each one commits its
batch and leaves the rest for the next. Having more work than fits in one run
is the normal case, never a reason to invoke the failure protocol. Commit what
you fetched and stop cleanly. The same applies if you are running low on room
mid-run: stop fetching, commit the posts you have fully processed, record how
many remain, and end the turn. Partial progress that is committed and recorded
always beats an all-or-nothing run that lands nothing.

## 4. Fetch and extract

For each post to fetch, retrieve the page and extract:

- `body_text` — the article body as plain text, and **only** the article body.
  Keep paragraph breaks as newlines. No HTML, no Markdown.

  A `claude.com/blog` page wraps the article in chrome you must remove. The
  page begins with a metadata block that looks like this — drop all of it,
  including the repeated title:

  ```
  <title>
  Category
  Product announcements        <- one or more category/product lines
  Date
  July 28, 2026
  Reading time
  5
  min
  Share
  Copy link
  https://claude.com/blog/...
  ```

  `body_text` starts at the **first sentence of actual prose** after that
  block. It ends at the **last sentence of the article**: drop any trailing
  "Related posts", "Subscribe", newsletter signup, or footer navigation.

  **Self-check before you accept the extraction.** None of these strings may
  appear anywhere in `body_text`: `Reading time`, `Copy link`, `Share`,
  `Category`, `Related posts`, `Subscribe`. If any does, you have captured
  chrome — re-clean it. Also sanity-check that `body_text` does not begin by
  repeating `title`. A post whose body you cannot clean is a skip (record it in
  `skipped`), not a failure.

  **Figures.** When you hit an image inside the article body, do not drop it.
  Emit a line containing only `[[figure:N]]` at that exact position in
  `body_text`, numbering from 1 in document order, and append an entry to
  `figures` with the absolute image URL as `src`, the image's alt text as `alt`
  if present, and the `figcaption` text as `caption` if present. Omit `alt` or
  `caption` rather than inventing them.

  Only article images count. Do not capture the page's hero or cover art,
  author avatars, logos in the site chrome, newsletter or footer graphics, or
  tracking pixels. If you cannot tell whether an image is part of the article,
  leave it out — a missing figure is a much smaller problem than a page-chrome
  image appearing mid-article.

  `[[figure:N]]` markers are the only markup permitted in `body_text`. The
  chrome self-check still applies to everything else.
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

**Write it yourself, in your own words.** Never produce a `tldr` with bash,
python, string slicing, truncation, or any other mechanical transformation of
`body_text`. A summary assembled by code is invalid no matter what length it
comes out at. Specifically forbidden: `title` concatenated with the opening of
`body_text`; any span of more than eight consecutive words copied from
`body_text`; any string that ends mid-word or mid-sentence.

Scripting is correct for the mechanical parts of this runbook — hashing,
counting words, diffing, assembling and validating JSON — and you should use it
there. Summarizing is the one step that must come from you.

- Ground it **only** in that post's `body_text`. No outside knowledge, no
  speculation about what a release "means".
- Lead with what the post announces or argues, not with "This post…".
- Plain prose. No bullets, no headings, no marketing tone.

Reuse the existing `tldr` verbatim for unchanged posts. Never regenerate a TL;DR
you do not have to — it is the main cost driver of a run.

**Count the words before you accept each TL;DR.** If it falls outside the
bounds, rewrite it until it fits. This is a quality rule you fix in place, not
a validation failure: a short TL;DR is never a reason to abort the run or file
an issue.

**Self-heal on load.** Also check the TL;DRs of posts already in
`data/posts.json`. Any that fall outside the bounds — from an earlier run or a
config change — get regenerated from their **stored `body_text`**. That needs
no re-fetch and no network, so it is cheap; do it as part of the normal run and
count those posts as `changed`.

## 6. Validate — the gate

Assemble the full `posts.json` object: `version` 1, `generated_at` now,
`window_days`, `source_url`, and `posts` sorted by `published_at` descending.

Validate it against `data/posts.schema.json`. Also check, because the schema
cannot:

- every `id` is unique
- every `published_at` is on or after `cutoff`
- **no `tldr` is a mechanical slice of its `body_text`.** Script this: strip
  the title prefix if present, then confirm the first forty characters of what
  remains do not appear verbatim in that post's `body_text`. A `tldr` that
  fails this is a generation failure — rewrite it yourself (step 5), do not
  commit it, and do not escalate it as a schema failure.
- **`[[figure:N]]` markers and `figures` entries correspond one-to-one.** Every
  marker in `body_text` has a matching `n` in that post's `figures`, and every
  entry in `figures` has a matching marker. A mismatch is a per-post skip
  (record it in `skipped`), not a run failure.

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

Triggered by exactly four things: unparseable existing `data/posts.json`, zero
posts discovered, all sources unreachable, or schema validation failure.

"Schema validation failure" means **the document fails a structural check
against `data/posts.schema.json`** — a missing required field, a wrong type, a
malformed hash or date, an unknown property. It does not mean "something about
the data looks wrong to me". If you can state the problem only in prose and not
as a schema rule the document violates, it is not a validation failure.

**Nothing else is a failure.** Specifically, none of these are — do not file an
issue for any of them:

- More posts to fetch than fit in one run (step 3 — commit the batch and stop).
- Individual posts skipped for a missing date (step 4 — record in `skipped`).
- No new posts today (step 7 — report "no changes" and commit nothing).
- A TL;DR outside the configured word bounds (step 5 — rewrite it in place).
  The bounds live in `config.json`, not in the schema; a short TL;DR validates
  fine and is yours to fix, not to escalate.

If you are tempted to file an issue recommending that the pipeline be
redesigned, batched, or resumed across runs: don't. That is already how it
works. Commit your batch and end the turn.

1. **Commit no sync data.** The one exception is the TL;DR repairs from step
   1b, which are source-independent and should still be committed.
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
