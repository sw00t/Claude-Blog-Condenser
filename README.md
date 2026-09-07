# Claude Blog Condenser

A daily agent mirrors the [claude.com/blog](https://claude.com/blog) front page into a
JSON file, writes a 25 to 60 word summary for each new post, and commits the result. A
static PWA renders that file as dense, text-first reading that works offline. There is
no backend, no database, and no server: **the git repo is the state store**, and GitHub
Pages serves both the data and the reader from the same commit.

**Live at [sw00t.github.io/Claude-Blog-Condenser](https://sw00t.github.io/Claude-Blog-Condenser/)**

---

## Why this exists

I wanted to read the Claude blog the way I actually read: quickly, offline, on a phone,
without a 500 KB page of chrome around each article. That is a small problem. The
interesting problem was the one behind it: **how much of a recurring job should an LLM
actually be doing?**

The first version answered "most of it." An agent read a runbook and did the whole job:
fetch, extract, diff, summarize, validate, commit. It worked for a month, then failed in
a way that was expensive and instructive. The current version answers "as little as
possible": one API call per new post, and nothing else.

That rewrite is the substance of this repo. The incident that motivated it is documented
below rather than quietly fixed, because the failure is more interesting than the code.

---

## Architecture

```
  cron ──▶ Managed Agent ──▶ python3 sync.py ──▶ git commit ──▶ GitHub Pages ──▶ PWA
           (Haiku 4.5)       │                   (local git)
           runs it and       │
           reports exit code └──▶ 1 API call per new post (Haiku 4.5, validated)

           ── judgment ──▶   ── deterministic ──────────────────────────────────▶
```

The split is the whole design. Everything that is deterministic (fetching, stripping
HTML, diffing, validating, writing, committing) is code in `sync.py`. The only step
that genuinely needs a model is writing prose, and that is one bounded, validated call.

The agent's entire job is:

```sh
cd /workspace/reader && python3 sync.py
```

…and then reporting the exit code and `data/last_sync.json`. Its runbook
(`agent/task-prompt.md`) is 87 lines, most of which is a list of things it must *not*
improvise. It has no judgment left to exercise, which is the point.

`sync.py` is ~1,190 lines of **dependency-free Python 3.9**: `urllib`, `html.parser`,
`hashlib`, `subprocess`, and a small JSON Schema validator written against the subset
the contract actually uses. No `requests`, no `bs4`, no `jsonschema`, no SDK. A cloud
sandbox with a bare interpreter can run it.

### Every budget is enforced in code

| Limit | Value | On breach |
|---|---|---|
| Wall clock | 8 min, checked between stages | Commit what finished, write `last_sync.json`, exit 0 |
| HTTP fetches | 8 per run, including the index | Stop fetching, note the shortfall, continue |
| New posts summarized | 6 per run | Remainder deferred to the next run |
| Attempts per operation | 2 | Abort the whole run, never a third try |
| API calls per post | 1, at most 2 | Skip that post, record it in `skipped` |
| Commits per run | Exactly 1 | n/a |

None of these is a retry loop. Each one exits cleanly. This matters because the previous
version expressed the same budgets as prose in a prompt, where they were advisory.

### Writes are all-or-nothing

The complete `posts.json` is built in memory, validated against
`data/posts.schema.json`, written to a temp file, re-parsed, and `os.replace()`d into
position. There is no code path that writes a partial file, splits a payload, or stages
a placeholder. If the whole file cannot be written, the previous file is left untouched
and the run exits non-zero having committed nothing.

Commits go through **local `git` in the mounted repo**, never the GitHub file API.

---

## The incident that shaped it

On **2026-09-07**, the scheduled run made **14 commits between 07:14 and 07:41**, drained
the API balance, and left `data/posts.json` truncated at 51,517 bytes, ending mid-array
on a trailing comma, with double-escaped newlines. It did not parse. The live reader
broke.

The commit log is the clearest account of what happened:

```
07:14  content: sync 2026-09-07 (+2 new, 0 changed, 0 pruned)
07:17  WIP placeholder - will be replaced
07:17  temp scratch - to be deleted
07:27  temp part 1
07:31  temp part 2 - includes 3rd post to verify
07:34  placeholder - checking push_files does not need sha
07:38  wip part A
07:41  content: sync 2026-09-07 (+2 new, 0 changed, 0 pruned) [part 1/2]
```

The agent could not write `posts.json` in one call, so it started reverse-engineering the
GitHub file-write API: splitting the payload into parts, committing placeholders to test
whether `push_files` needed a `sha`, cleaning up after itself, trying again. It behaved
reasonably at every individual step. The aggregate was a corrupted dataset and a spent
balance.

**Recovery** was one command. The last good state was 44 posts at `b192338`:

```sh
git checkout b192338 -- data/posts.json
```

The 14 junk commits were left in history. Rewriting published history to hide a bad
morning is not worth it, and the log above is a better artifact than a clean one.

### Three root causes, all architectural

**1. The run budget was prose in a prompt.** The runbook said "hard stop at ten minutes"
and "do not retry more than twice." Those are advisory. A confused model walks straight
through them while believing it is complying. *Fix: every limit is now a measured value
in code that causes a clean exit.*

**2. A reasoning loop owned file I/O and git.** Those operations are deterministic and
have exactly one correct implementation. Giving a model discretion there means it will
eventually exercise that discretion. *Fix: they are now functions, and the model is not
in that call path.*

**3. The write went through the GitHub MCP file API**, which hit a payload limit at
roughly 44 posts. That limit is what triggered the improvisation. *Fix: local git in the
mounted repo, which does not care about file size.*

A fourth lesson, softer: **the model that fails is not always the model to blame.** Haiku
had been tried for this job in August and produced mechanical, sliced summaries. The
conclusion at the time was "Haiku can't do this." The real cause was structural. It was
doing extraction, diffing, summarizing *and* committing, under an explicit
cost-discipline instruction, with `bash` available. It found a cheap deterministic path
and took it. Given one narrow job, no tools, and a validated output, Haiku 4.5 writes
these summaries well. It runs both roles today.

---

## Design decisions

**Scope is page 1 of the blog index. Deliberately.** No pagination, no feeds, no
sitemaps, no category archives. The site randomizes its pagination query-param prefix per
render, so constructed page URLs silently return page 1 again, so guessing costs a fetch
and yields nothing. Coverage accumulates instead: posts that scroll off page 1 are
carried forward from storage until they age out. **Date is the only prune criterion;
absence from page 1 never removes a post.**

**The runbook lives in the repo, not the system prompt.** The deployment's initial
message is a one-line pointer to `agent/task-prompt.md` in the mounted checkout, so
changing the procedure is a plain commit, with no `agents update`, no re-pin,
and no redeploy.

**The summary prompt is given stricter limits than the validator enforces.** Models drift
long, and a summary aimed at the exact ceiling lands just over it about half the time
(observed: 71, 70, 64 words against a 60-word cap; 604 characters against a 600
maximum). The prompt asks for ~38 words in at most two sentences; validation still
enforces the real contract. Headroom absorbs the drift without loosening the rules.

**An anti-slice check on every summary.** Strip any title prefix, then confirm the first
40 characters do not appear verbatim in `body_text`. This is the specific regression that
made Haiku look incapable in August, so it is now a validation gate rather than an
instruction.

**Prompt caching was implemented, measured, and switched off.** The only repeated prefix
is a corrective retry. Haiku 4.5 requires a 4,096-token minimum cacheable prefix; the
longest post at the input cap measures ~3,707 tokens, so the marker silently no-ops.
Clearing the threshold would mean sending *more* body text on every call to enable a
cache that only pays back on a minority of retries. Break-even sits near a 28% retry
rate; the measured rate on a full run is 0%. `ENABLE_PROMPT_CACHE = False`, with the
arithmetic recorded next to the flag and per-run usage logged so the decision stays
observable rather than assumed.

**Models.** Haiku 4.5 for both roles. Note that Haiku 4.5 does not support the `effort`
parameter at all (it errors), so "run the cheap model at low effort" is not available
here; dropping the reasoning model *is* the saving. Changing `SUMMARY_MODEL` in `sync.py`
is a one-line experiment with no effect on the runner.

**Credentials degrade gracefully.** `sync.py` resolves `ANTHROPIC_API_KEY`, then
`ANTHROPIC_AUTH_TOKEN`, then the `ant` CLI's stored OAuth profile, which it refreshes
itself, so a local run or a local schedule needs no API key at all. A cloud sandbox
cannot use the profile (`ant auth login` is interactive), so a scheduled run needs a key
supplied through the vault. With no credential the script aborts cleanly and commits
nothing.

---

## The data contract

`data/posts.schema.json` is the authority, and `sync.py` validates the assembled document
against it *before* anything touches disk. Beyond what JSON Schema can express, each run
also checks: id uniqueness, every `published_at` within the retention window, `word_count`
and `content_hash` consistent with `body_text`, the anti-slice rule, and one-to-one
correspondence between `[[figure:N]]` markers and `figures` entries.

```jsonc
{
  "id": "how-anthropic-employees-use-claude-tag",  // stable slug, never changes
  "url": "https://claude.com/blog/...",
  "title": "How Anthropic employees use Claude Tag",
  "published_at": "2026-08-28",                    // never inferred; no date = skip
  "category": "Enterprise AI",                     // verbatim from the index card
  "authors": ["..."], "tags": ["..."],             // omitted entirely if absent
  "tldr": "...",                                   // 25-60 words, model-written
  "body_text": "...[[figure:1]]...",               // article prose only
  "word_count": 1558,
  "content_hash": "7df37f4d...",                   // SHA-256, drives change detection
  "figures": [{ "n": 1, "src": "https://...", "caption": "..." }]
}
```

`[[figure:N]]` is the only markup permitted in `body_text`. Figures are hot-linked from
the source CDN, never re-hosted; the reader drops any figure whose image fails to load.

Extraction targets the article container and drops the page's metadata block and trailing
chrome. A self-check rejects any body where a chrome label (`Category`, `Reading time`,
`Copy link`, …) survives **as a standalone line**, matched per-line rather than as a
substring, because "Category Management" is legitimate prose and a substring match
silently discarded a real post.

---

## Repo layout

```
Claude-Blog-Condenser/
├── sync.py                  # the whole sync: fetch, diff, summarize, validate, commit
├── config.json              # runtime config (window, caps, paths)
├── index.html               # redirects to app/ so the bare Pages URL opens the reader
├── agent/
│   ├── system-prompt.md     # runner persona (set at agent creation)
│   ├── task-prompt.md       # 87-line runbook: run the script, report the outcome
│   └── setup.sh             # one-time: create the agent + scheduled deployment
├── data/
│   ├── posts.json           # generated content (script-owned, never hand-edit)
│   ├── posts.schema.json    # the data contract
│   └── last_sync.json       # per-run metadata: counts, skipped, notes
└── app/                     # the PWA
    ├── index.html           # whole reader: markup, styles, logic
    ├── sw.js                # service worker (shell + data + image caches)
    └── manifest.webmanifest, icon-*.png
```

---

## Running your own copy

Requires `git`, Python 3.9+, and the [`ant` CLI](https://platform.claude.com/). The
script alone runs anywhere; the Managed Agent is only needed for cloud scheduling.

```sh
git clone https://github.com/sw00t/Claude-Blog-Condenser && cd Claude-Blog-Condenser
ant auth login          # or: export ANTHROPIC_API_KEY=sk-ant-...
python3 sync.py
```

That is the entire local setup. It will fetch, summarize, validate, commit, and push.

<details>
<summary><b>Cloud scheduling via Managed Agents</b> (optional)</summary>

1. **GitHub token.** Fine-grained PAT scoped to this repo, **Contents read/write** +
   **Issues read/write**. Copy `.env.example` to `.env` and put it there. `.env` is
   gitignored and `agent/setup.sh` reads it. Never put it in `setup.sh`; this repo is
   public.

2. **Environment** (once):
   ```sh
   ant beta:environments create --name "blog-reader-env" \
     --config '{type: cloud, networking: {type: unrestricted}}'
   ```

3. **Vault for the GitHub MCP credential** (once). The repo resource's
   `authorization_token` authenticates git clone/push through the Anthropic git proxy
   only. It does **not** authenticate the MCP server, and the failure protocol needs MCP
   to file issues:
   ```sh
   ant beta:vaults create --display-name "blog-reader-github"
   ant beta:vaults:credentials create --vault-id $VAULT_ID \
     --display-name "GitHub MCP" \
     --auth '{type: static_bearer, mcp_server_url: https://api.githubcopilot.com/mcp/, token: ghp_...}'
   ```
   Credentials are write-only and unvalidated until session runtime, so a bad token
   surfaces as a `session.error` on the first run rather than here.

4. **The sandbox needs its own Anthropic credential.** `sync.py` calls the Messages API,
   and the agent's inference credential is *not* exposed to the `bash` tool. Supply
   `ANTHROPIC_API_KEY` through the vault and confirm it is visible in the sandbox before
   trusting the schedule.

5. **Create the agent and deployment.** Fill in `REPO_URL`, `ENVIRONMENT_ID`, `VAULT_ID`
   in `agent/setup.sh`, then `cd agent && bash setup.sh`. Save the printed `AGENT_ID` and
   `DEPLOYMENT_ID`.

6. **Trigger manually before trusting the schedule:**
   ```sh
   ant beta:deployments run --deployment-id $DEPLOYMENT_ID
   ```
</details>

<details>
<summary><b>Hosting the reader</b></summary>

Settings → Pages → Deploy from a branch → `main`, folder `/ (root)`. The root
`index.html` redirects to `app/`. This works because the repo is public; on a free plan a
private repo simply stops being served, and Cloudflare Pages is the drop-in replacement.

**Every change to `app/index.html` requires bumping `SHELL_V` in `app/sw.js`**, or
installed clients keep serving the cached shell forever and never see the update toast.
Current: `SHELL_V = "shell-v4"`, `DATA_V = "data-v1"`, `IMG_V = "img-v1"`.

Caching is cache-first for the shell, stale-while-revalidate for `data/posts.json`, and
cache-first for cross-origin figure images. Because content is served from cache, the
header's `synced Nh ago` and the Refresh button are the only reliable signals that the
sync actually ran.
</details>

---

## Operations

```sh
ant beta:deployment-runs list --deployment-id $DEPLOYMENT_ID --has-error  # failures only
ant beta:deployments pause   --deployment-id $DEPLOYMENT_ID
ant beta:deployments run     --deployment-id $DEPLOYMENT_ID               # manual run
```

**Change the procedure:** commit `agent/task-prompt.md`. The next run picks it up.

**Change the persona or model:** edit `agent/system-prompt.md`, then run **both**:

```sh
ant beta:agents update --agent-id $AGENT_ID --system "$(cat agent/system-prompt.md)" \
  --model '{id: claude-haiku-4-5}'
ant beta:deployments update --deployment-id $DEPLOYMENT_ID \
  --agent '{id: '$AGENT_ID', version: N, type: agent}'    # re-pin!
```

> **Deployments pin a concrete agent version and do not follow `latest`.** A bare agent
> ID passed to `deployments create` resolves *once*, at creation, and freezes, unlike
> `sessions.create`, where a bare ID means "latest at session start". Skip the re-pin and
> every `agents update` is silently ignored by the schedule, with no error anywhere.
> Confirm with
> `ant beta:deployments retrieve --deployment-id $DEPLOYMENT_ID --transform agent`.

---

## Known behaviours and caveats

- **Retention is `window_days` in `config.json`** (currently 35). Because that window is
  shorter than the depth of page 1, posts age out while still visible on the blog, so
  most days produce a prune commit even with no new posts. That is the rolling-window
  design working, not a fault.
- **MCP tools must be `always_allow` for unattended runs.** An `mcp_toolset` left at its
  default evaluates to `ask`: the session goes idle with `stop_reason: requires_action`
  waiting for a confirmation nobody sends. The built-in `agent_toolset` is unaffected, so
  the symptom only appears on the failure path, the path you least want to find broken.
  `setup.sh` sets the policy explicitly.
- **Changing the extraction rules does not re-extract stored posts.** The diff re-fetches
  only on a title or date change, so existing records keep whatever `body_text` they were
  captured with. After editing extraction in `sync.py`, delete `data/posts.json` and let
  successive runs rebuild it 6 posts at a time. There is deliberately no
  `extraction_version` auto-invalidation, which would be machinery for something
  that changes rarely, but you must remember the reset.
- **Read state is per-device**, in `localStorage` keyed on post `id`. Deleting
  `data/posts.json` does not lose it, because ids are stable slugs. No cross-device sync,
  and none planned.
- **Summaries mode changes what the first tap does.** With Summaries on, posts render
  already open, so the first tap on a title closes rather than opens it and does not mark
  the post read; tapping Full text still does. The alternative, marking every visible
  post read the instant the toggle flips, is worse. Documented, not fixed.
- **Cost is read from the Console Cost page**, because the Usage and Cost Admin API needs
  an Admin API key that individual accounts cannot provision. Note that a *rate* limit
  (tokens/min) is not a *spend* limit: it caps throughput, not total cost. At 250k
  tokens/min a runaway loop can still burn 15M tokens in an hour. A small credit balance
  is the only limit that is genuinely hard.
- **Managed Agents is in beta** (`managed-agents-2026-04-01`; the CLI sets it
  automatically). Field names can move behind new dated headers. This repo is the
  version-controlled source of truth so the setup can be re-created; if a CLI command
  rejects a field, check the
  [current reference](https://platform.claude.com/docs/en/managed-agents/reference)
  first.

---

## Deliberately not built

- **Pagination, feeds, sitemaps, category archives.** Cost control. Coverage accumulates.
- **Splitting `posts.json` into an index plus per-post bodies.** It would help PWA load
  time as the file grows, and it was the *trigger* for the payload limit, but local git
  handles a file this size without difficulty, and the script never loads it into a model
  context. Solving it now would be solving the symptom of a bug that no longer exists.
- **Retry logic beyond two attempts.** Every loop in this system was once a good idea.
- **Cross-device read-state sync.** Needs a backend. The absence of one is the feature.
