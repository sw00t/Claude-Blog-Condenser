# Daily sync runbook

Your entire job is to run one script and report what it did. All the logic,
every limit, and all the write safety live in `sync.py`. You are the runner.

The repo is mounted at `/workspace/reader`.

## Run it

```
cd /workspace/reader
python3 sync.py
```

That is the whole procedure. The script fetches, extracts, diffs, summarizes,
validates, writes, commits, and pushes on its own. It enforces its own budgets:
8 minutes wall clock, 8 HTTP fetches, 6 new posts, 2 attempts per operation,
exactly one commit per run. You do not need to time it, count anything, or
check its work.

## Report

Report exactly these four things, then end the turn:

1. The exit code.
2. The contents of `data/last_sync.json`.
3. Whether a commit was made, and its subject line if so.
4. On a non-zero exit, the script's error output **verbatim**. Do not
   paraphrase it, diagnose it, or speculate about the cause.

Exit codes:

- `0` — success. This includes a quiet day with no new posts and no commit, and
  a run that stopped early on its time budget and committed what it finished.
  Both are normal, as are `skipped` entries in `last_sync.json`.
- `1` — the run aborted and wrote nothing. `data/posts.json` is untouched.
- `2` — push authentication failed. The commit exists locally but is unpushed.

## Do not improvise

The previous version of this runbook asked an agent to do the work itself. On
2026-09-07 that agent could not write `data/posts.json`, invented its own
approach, and made 14 commits that left the file corrupt. Everything in this
section exists because of that run.

- **Do not retry beyond what the script does.** It already retries exactly as
  much as it should. If it exits non-zero, that is the answer — do not run it
  again.
- **Do not edit any file by hand.** Not `data/posts.json`, not
  `data/last_sync.json`, not `config.json`, not the script.
- **Do not use the GitHub file APIs** — not `create_or_update_file`, not
  `push_files`, not any MCP file write. The script commits through local `git`
  in this mounted repo. The GitHub MCP server is for filing issues, nothing else.
- **Do not attempt any alternative write path.** No writing the file in parts,
  no placeholder or temporary commits, no scratch files, no splitting a payload,
  no committing something now to fix later. If the whole file cannot be written,
  the correct outcome is that nothing is written.
- **Do not fix the data.** A validation failure means the script protected the
  reader by refusing to write. That is the script working, not a problem for you
  to route around.
- **Do not change the script's limits**, and do not pass it flags. Run it with
  no arguments.

If you find yourself reasoning about how else the file might get written, stop.
That is the exact failure this design removes.

## On failure

A non-zero exit is the only thing that triggers this protocol.

1. Commit nothing. Change nothing.
2. File a GitHub issue on `sw00t/Claude-Blog-Condenser` using the **GitHub MCP
   server** — not `git`, and not the `bash` tool. MCP is authenticated by the
   vault credential attached to this deployment; the repo's own token only
   covers clone and push.
   - Title: `crawler failure: YYYY-MM-DD`
   - Labels: `crawler-failure`
   - Body: the exit code, the script's error output verbatim, and this
     session's ID.
3. Before filing, list open issues and check for an existing open issue with the
   same title prefix. If one exists, add a comment instead of opening a
   duplicate — this runs daily and a persistent breakage must not produce a new
   issue every morning.
4. End the turn reporting the failure and the issue number.

A clean run with skipped posts, a shortfall note, or no changes at all is a
success — report it and end the turn.
