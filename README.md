# Claude-Blog-Condenser

A Claude Managed Agent syncs the first page of [claude.com/blog](https://claude.com/blog)
into `data/posts.json` on a daily schedule and commits the result to this repo. A static
PWA in `app/` reads that file and renders it as dense, text-first reading. GitHub Pages
serves both from the same repo. There is no backend and no database — the git repo *is*
the state store.

Live at **https://sw00t.github.io/Claude-Blog-Condenser/**

## Repo layout

```
Claude-Blog-Condenser/
├── README.md                # this file — the only prose document in the repo
├── config.json              # runtime config the agent reads each run
├── .env.example             # template for .env (gitignored; agent/setup.sh reads it)
├── index.html               # redirects to app/ so the bare Pages URL opens the reader
├── agent/
│   ├── system-prompt.md     # agent persona (set once, at agent creation)
│   ├── task-prompt.md       # per-run runbook (versioned here, read by the agent)
│   └── setup.sh             # one-time: create the agent + scheduled deployment
├── data/
│   ├── posts.json           # generated content (agent-owned, never hand-edit)
│   ├── posts.schema.json    # data contract; the agent validates before committing
│   └── last_sync.json       # generated run metadata
└── app/                     # the PWA
    ├── index.html           # whole reader: markup, styles, logic
    ├── sw.js                # service worker (shell + data + image caches)
    ├── manifest.webmanifest
    └── icon-*.png
```

## How it works

- **The runbook lives in the repo, not the system prompt.** The deployment's initial
  message is a one-line pointer to `agent/task-prompt.md` in the mounted repo, so
  changing what the agent does each run is a plain commit — no `agents update`, no
  re-pin, no redeploy. Every run reads the latest committed version.
- **The repo doubles as the state store.** The agent diffs discovered posts against
  `data/posts.json`, fetches only new or changed posts, and prunes records that age out
  of `window_days`.
- **The agent never commits invalid data.** `data/posts.json` must validate against
  `data/posts.schema.json` before it is written. On a parse, fetch, or validation
  failure the agent commits nothing and files a GitHub issue instead (see the failure
  protocol in `agent/task-prompt.md`). A run that writes nothing is a good outcome; a
  run that commits malformed data is not.

## First-time setup

To run your own copy. Commands assume macOS or Linux with git installed.

**1. Anthropic API key.** Sign in at [platform.claude.com](https://platform.claude.com/)
and add API credits under Billing — Managed Agents bills per token plus a
per-session-hour charge. Create a standard API key and export it:

```sh
export ANTHROPIC_API_KEY=sk-ant-...
```

An exported key overrides any `ant auth login` profile and scopes every CLI call to that
key. Creating a dedicated workspace first is optional but makes the Console Cost page
easier to read, since spend is attributed per workspace.

**2. Install and authenticate the `ant` CLI.**

```sh
brew install anthropics/tap/ant     # macOS; Linux/WSL: see the quickstart docs
ant --version
```

**3. GitHub repo and token.** Fork or push this repo to your own account. Create a
fine-grained personal access token (Settings → Developer settings → Fine-grained
tokens) with repository access limited to this repo and permissions **Contents
read/write** and **Issues read/write**. Set an expiry and calendar a rotation reminder.
Copy `.env.example` to `.env` and put the token there — `.env` is gitignored and
`agent/setup.sh` reads it. Never put the token in `setup.sh` itself; this repo is
public.

**4. Create the sandbox environment** (once):

```sh
ant beta:environments create \
  --name "blog-reader-env" \
  --config '{type: cloud, networking: {type: unrestricted}}'
```

Save the returned `env_...` ID. Unrestricted networking keeps the prototype simple; you
can tighten the rules later.

**5. Create the vault holding the GitHub MCP credential** (once). The repo resource's
`authorization_token` authenticates git clone/push through the Anthropic git proxy
only — it does **not** authenticate the MCP server, and the failure protocol needs MCP
to file issues:

```sh
ant beta:vaults create --display-name "blog-reader-github"
ant beta:vaults:credentials create --vault-id $VAULT_ID \
  --display-name "GitHub MCP" \
  --auth '{type: static_bearer, mcp_server_url: https://api.githubcopilot.com/mcp/, token: ghp_YOUR_PAT}'
```

Save the returned `vlt_...` ID. Credentials are write-only and are never validated until
session runtime, so a bad token surfaces as a `session.error` on the first run rather
than here.

**6. Fill in and run `agent/setup.sh`.** Set `REPO_URL`, `ENVIRONMENT_ID`, `VAULT_ID`,
and your `TIMEZONE`/`CRON` if you want something other than daily at 06:00 UTC, then:

```sh
cd agent && bash setup.sh
```

It creates the agent and the scheduled deployment and prints `AGENT_ID` and
`DEPLOYMENT_ID` — save both. Check the response's `schedule.upcoming_runs_at` to confirm
the cron parsed as you intended.

**7. First run.** Trigger manually before trusting the schedule. This is also your
backfill, which takes several runs: `max_new_fetches_per_run` caps each run, and the
remainder is recorded in `last_sync.json` `notes` for the next one.

```sh
ant beta:deployments run --deployment-id $DEPLOYMENT_ID
```

Then verify, in order:

- `ant beta:deployment-runs list --deployment-id $DEPLOYMENT_ID` — a successful run
  shows a `session_id` and no error
- a new `content: sync YYYY-MM-DD (...)` commit exists
- `data/last_sync.json` shows the source used and the counts
- no open `crawler failure` issue on the repo
- spot-check two or three TL;DRs against the actual posts

## Deploying the reader

**Hosting.** Settings → Pages → Deploy from a branch → `main`, folder `/ (root)`. The
root `index.html` redirects to `app/`, so the bare URL opens the reader. This works
because the repo is public. On a free plan a private repo simply stops being served;
if you need the repo private, Cloudflare Pages is the drop-in replacement.

**Publishing a reader change.** Every change to `app/index.html` requires bumping
`SHELL_V` in `app/sw.js`. Without it, installed clients keep serving the cached shell
indefinitely and never see the update toast. Current values: `SHELL_V = "shell-v3"`,
`IMG_V = "img-v1"`, `DATA_V = "data-v1"`.

**Verifying a deploy.** Wait for the Pages build, then confirm the live `app/sw.js`
serves the expected `SHELL_V`. On desktop: DevTools → Application → Service Workers
shows `activated`; Network → Offline → reload still renders posts. Load the page once
online before testing offline, or nothing is cached yet.

**Installing.** Android Chrome → open the URL → menu → Add to Home screen. It launches
standalone and works offline.

**Caching model.** Cache-first for the shell, stale-while-revalidate for
`data/posts.json`, cache-first runtime caching for cross-origin figure images. Because
content is served from cache, the header's `synced Nh ago` and the Refresh button are
the only reliable signals that the agent actually ran.

## Operations

```sh
ant beta:deployment-runs list --deployment-id $DEPLOYMENT_ID              # run history
ant beta:deployment-runs list --deployment-id $DEPLOYMENT_ID --has-error  # failures only
ant beta:deployments pause   --deployment-id $DEPLOYMENT_ID
ant beta:deployments unpause --deployment-id $DEPLOYMENT_ID
ant beta:deployments run     --deployment-id $DEPLOYMENT_ID               # manual run
```

**Update the runbook:** just commit `agent/task-prompt.md`. The next run picks it up.
This is the whole reason the runbook lives in the repo — it sidesteps both `agents
update` and the re-pin below.

**Update the persona or the model:** edit `agent/system-prompt.md`, then run **both** of:

```sh
ant beta:agents update --agent-id $AGENT_ID --system "$(cat agent/system-prompt.md)"
ant beta:deployments update --deployment-id $DEPLOYMENT_ID --agent $AGENT_ID  # re-pin!
```

> **The deployment pins an agent version and does not follow `latest`.** Passing a
> bare agent ID to `deployments create` resolves it to a concrete version *once*, at
> creation, and freezes it — unlike `sessions.create`, where a bare ID means "latest at
> session start". Skip the re-pin and every `agents update` is silently ignored by the
> schedule: runs keep using the version pinned on day one, with no error anywhere.
> Confirm with
> `ant beta:deployments retrieve --deployment-id $DEPLOYMENT_ID --transform agent`.

## Known behaviours and caveats

- **Scope is the first page of the blog index only**, by deliberate cost control. The
  blog paginates to many pages, but coverage of `window_days` accumulates on its own as
  posts scroll off page 1 and are carried forward; absence from page 1 never prunes a
  post. The reader's `N days not captured` markers show where the holes are. Widening
  this is a config change *and* a runbook change, not a runbook change alone.
- **Deployments pin a concrete agent version and do not follow latest.** After any
  `agents update`, re-pin with `deployments update` or the schedule silently keeps
  running the old version. See the warning above.
- **MCP tools must be `always_allow` for unattended runs.** An `mcp_toolset` left at its
  default evaluates to `ask`: the session goes idle with `stop_reason: requires_action`
  waiting for a confirmation nobody sends. The built-in `agent_toolset` does not have
  this problem, so the symptom only appears on the failure path — the path you least
  want to discover broken. `setup.sh` sets the policy explicitly.
- **GitHub MCP auth comes from the vault, not the repo resource token.** The repo's
  `authorization_token` covers clone/pull/push only. Without a vault credential the
  session starts and then fails mid-run with a `session.error`, so the issue-on-failure
  protocol silently never fires.
- **Changing the extraction rules does not re-extract existing posts.** The diff
  re-fetches only on a title or date change, so stored posts keep whatever `body_text`
  they were captured with. After editing the extraction spec in `task-prompt.md`, delete
  `data/posts.json` and let the next runs rebuild it at `max_new_fetches_per_run` posts
  per run. There is deliberately no `extraction_version` auto-invalidation — it would
  add machinery for something that changes rarely — but the tradeoff is that you must
  remember the reset.
- **Read state is per-device**, stored in browser localStorage keyed on post `id`.
  Deleting `data/posts.json` does not lose it, because ids are stable slugs. There is no
  cross-device sync and none is planned.
- **Summaries mode changes what the first tap does.** With Summaries on, posts render
  already open, so the first tap on a title closes rather than opens it, and does not
  mark the post read; tapping Full text still does. The alternative — marking every
  visible post read the instant the toggle flips — is worse. Documented, not fixed.
- **`[[figure:N]]` is the only markup permitted in `body_text`.** Figures are hot-linked
  from the source CDN, never re-hosted, and the reader removes any figure whose image
  fails to load. Markers and `figures` entries must correspond one-to-one; a mismatch
  is a per-post skip, not a run failure.
- **Cost is checked on the Console Cost page**, because the Usage and Cost Admin API
  requires an Admin API key that individual accounts cannot provision. Effort level
  tuning moves token spend more than wall-clock: a run's duration is dominated by
  fetching and summarizing, not reasoning depth, so do not judge a cost change by how
  long the run took.
- **If the deployment auto-pauses** (archived environment or vault, etc.), fix the
  underlying resource and `unpause`. Missed triggers are not backfilled.
- **Managed Agents is in beta** (`managed-agents-2026-04-01` header; the CLI and SDK set
  it automatically). Field names can change behind new dated headers — this repo is the
  version-controlled source of truth so the setup can be re-created. If a CLI command
  rejects a field, check the
  [current reference](https://platform.claude.com/docs/en/managed-agents/reference)
  before debugging anything else.
