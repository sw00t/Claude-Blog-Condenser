# Daily sync runbook

> **STUB — not production content.** The real runbook has not been written yet.
> Because this file is read from the mounted repo on every run, replacing it is
> a plain commit — no redeploy, no `agents update`.

## Halt condition (remove once this runbook is real)

This runbook is a placeholder. Do **not** attempt a sync.

Take exactly these actions and no others:

1. Do not fetch anything from the network.
2. Do not modify, commit, or push any file in the repository.
3. Do not open a GitHub issue — this is an expected stub, not a crawler failure.
4. End your turn with a one-line report: `task-prompt.md is a stub; no work performed.`

## Still to write

The real runbook needs to specify, at minimum:

- **Discovery** — how to enumerate posts at https://claude.com/blog and what
  counts as the configured time window (read from `config.json`).
- **Diffing** — compare discovered posts against `data/posts.json`; fetch only
  new or changed posts; prune records that age out of the window.
- **Extraction** — per-post fields and the TL;DR format, matching
  `data/posts.schema.json`.
- **Validation** — validate against the schema before any write. Invalid data
  is never committed.
- **Commit** — write `data/posts.json` and `data/last_sync.json` (source used,
  counts), then commit as `content: sync YYYY-MM-DD (...)` and push.
- **Failure protocol** — on parse/validation/fetch failure, commit nothing and
  file a GitHub issue on this repo titled `crawler failure`, including the
  error and the run's session ID. This path needs the GitHub MCP server, which
  is authenticated by the vault credential attached to the deployment — not by
  the repo's `authorization_token`.
