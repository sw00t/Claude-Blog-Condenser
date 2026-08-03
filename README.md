# Claude Blog Reader — ingestion skeleton

Personal PWA that mirrors https://claude.com/blog as dense, text-first reading.
A Claude Managed Agent runs on a daily schedule, syncs posts within the configured
time window into `data/posts.json`, and commits to this repo. GitHub Pages (or
Cloudflare Pages) serves the static PWA from the same repo. No backend, no database.

## Repo layout

```
claude-blog-reader/
├── README.md
├── SETUP-GUIDE.md           # ground-up walkthrough (start here if new to Managed Agents)
├── config.json              # runtime config the agent reads each run
├── tools/
│   └── cost_report.py       # API spend tracker: day | week | month | ytd
├── agent/
│   ├── system-prompt.md     # agent persona (set once at agent creation)
│   ├── task-prompt.md       # per-run runbook (versioned here, read by the agent)
│   └── setup.sh             # one-time: create agent + scheduled deployment
├── data/
│   ├── posts.json           # generated content (agent-owned, do not hand-edit)
│   ├── posts.schema.json    # data contract; agent validates before committing
│   └── last_sync.json       # generated run metadata
└── app/                     # PWA (milestone 2 — not built yet)
```

## Design notes

- The deployment's initial user message is a one-line pointer to
  `agent/task-prompt.md` in the mounted repo. The real runbook lives in git, so
  prompt changes are a commit, not a redeploy, and every run uses the latest version.
- The repo doubles as the state store: the agent diffs discovered posts against
  `data/posts.json`, fetches only new/changed posts, and prunes records that age
  out of the window.
- Failure mode: the agent never commits invalid data. On parse/validation failure
  it files a GitHub issue on this repo instead (see task-prompt.md, Failure protocol).

## One-time setup

First time on Claude Managed Agents? Follow SETUP-GUIDE.md instead — it covers
everything below starting from Console account and organization creation, plus
cost tracking. The short version for repeat setups:

1. Create this repo on GitHub (private is fine for GitHub Pages on paid plans;
   public otherwise) and push this skeleton.
2. Create a fine-grained GitHub PAT scoped to this repo only:
   Contents read/write + Issues read/write.
3. Install and authenticate the `ant` CLI:
   https://platform.claude.com/docs/en/managed-agents/quickstart
4. Create a cloud environment once and note its `env_...` ID:
   https://platform.claude.com/docs/en/managed-agents/environments
5. Fill in the variables at the top of `agent/setup.sh`, then run it.
   It creates the agent and the daily scheduled deployment.
6. Test before trusting the schedule:
   `ant beta:deployments run --deployment-id "$DEPLOYMENT_ID"`
   then check the commit and `data/last_sync.json`.
7. Enable GitHub Pages on the repo (deploy from branch, root). The PWA in `app/`
   will be served once milestone 2 lands.

## Operations

- Run history:        `ant beta:deployment-runs list --deployment-id $ID`
- Failed runs only:   `ant beta:deployment-runs list --deployment-id $ID --has-error`
- Pause / resume:     `ant beta:deployments pause|unpause --deployment-id $ID`
- Manual run:         `ant beta:deployments run --deployment-id $ID`
- Update the persona: edit `agent/system-prompt.md`, then
  `ant beta:agents update --agent-id $AGENT_ID --system "$(cat agent/system-prompt.md)"`
- Update the runbook: just commit `agent/task-prompt.md`; next run picks it up.

## Known caveats

- Managed Agents is in beta (`managed-agents-2026-04-01` header; the CLI/SDK set
  it automatically). Field names can change behind new dated headers — this repo
  IS the version-controlled source of truth so the setup can be re-created.
- Verify the deployment `resources` block against the Create Deployment reference
  before first run (see comment in setup.sh):
  https://platform.claude.com/docs/en/api/beta/deployments/create
- Billing is API-side: per token plus a per-session-hour charge. A daily
  incremental sync of this blog is a short session; the first backfill
  (~6 months of posts) is the only long one. Track spend with
  tools/cost_report.py (day | week | month | ytd; needs an Admin API key and
  an organization — see SETUP-GUIDE.md Phase 0).
- The agent runs claude-haiku-4-5 for cost. If extraction fidelity or TL;DR
  quality lags, switch to claude-sonnet-4-6:
  `ant beta:agents update --agent-id $AGENT_ID --model '{id: claude-sonnet-4-6}'`
- If the deployment auto-pauses (archived environment/vault etc.), fix the
  resource and `unpause` — missed triggers are not backfilled.
# claude-blog-reader
# claude-blog-reader
