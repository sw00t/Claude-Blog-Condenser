# Setup guide: from zero to a scheduled blog-sync agent

You have never deployed to Claude Managed Agents; this walks the whole path.
Do the phases in order. Commands assume macOS or Linux with git installed.

## Phase 0: Console account, organization, and keys

1. Sign in (or sign up) at the Claude Console: https://platform.claude.com/
2. Set up an organization: Console -> Settings -> Organization. Do this even
   though you are solo - the Admin API that powers the cost tracker is
   unavailable for individual accounts.
3. Add API credits under Billing. Managed Agents bills per token plus a
   per-session-hour charge, drawn from these credits.
4. Create a dedicated workspace named blog-reader (Console -> Workspaces) and
   note its ID (wrkspc_...). Running everything in its own workspace is what
   lets the cost tracker isolate this project's spend.
5. Create two keys and store both in a password manager:
   - A standard API key created INSIDE the blog-reader workspace (used by the
     CLI; resources and costs attribute to that workspace).
   - An Admin API key (sk-ant-admin01-...) for the cost tracker only:
     https://platform.claude.com/docs/en/manage-claude/admin-api-keys
     Never give the Admin key to the agent or commit it anywhere.

## Phase 1: Install and authenticate the ant CLI

6. Install:
   - macOS:      brew install anthropics/tap/ant
   - Linux/WSL:  see https://platform.claude.com/docs/en/managed-agents/quickstart
   Verify: ant --version
7. Authenticate with the workspace-scoped key so everything lands in the
   blog-reader workspace:
   export ANTHROPIC_API_KEY=sk-ant-...   # the blog-reader workspace key
   (An exported API key overrides any ant auth login profile and scopes all
   CLI calls to that key's workspace - which is exactly what you want here.
   Add the export to your shell profile or a local .env you never commit.)
8. Optional: in Claude Code, /claude-api managed-agents-onboard gives an
   interactive walkthrough of the same concepts.

## Phase 2: GitHub repo and token

9. Create a GitHub repo named claude-blog-reader and push this skeleton to
   main. Public repo is simplest (free GitHub Pages); private works on paid
   plans.
10. Create a fine-grained personal access token (GitHub -> Settings ->
    Developer settings -> Fine-grained tokens): repository access = only
    claude-blog-reader; permissions = Contents Read/Write, Issues Read/Write.
    Set an expiry and calendar a reminder to rotate it.

## Phase 3: Create the Managed Agents resources

11. Create the sandbox environment (one time):
    ant beta:environments create \
      --name "blog-reader-env" \
      --config '{type: cloud, networking: {type: unrestricted}}'
    Save the returned environment.id (env_...). Unrestricted networking keeps
    the prototype simple; you can tighten network rules later.
11b. Create the vault holding the GitHub MCP credential (one time). The repo
    resource's authorization_token authenticates git clone/push through the
    Anthropic git proxy only - it does NOT authenticate the MCP server, and
    the failure protocol needs MCP to file issues:
      ant beta:vaults create --display-name "blog-reader-github"
      ant beta:vaults:credentials create --vault-id $VAULT_ID \
        --display-name "GitHub MCP" \
        --auth '{type: static_bearer, mcp_server_url: https://api.githubcopilot.com/mcp/, token: ghp_YOUR_PAT}'
    Save the returned vault.id (vlt_...). Credentials are write-only and are
    never validated until session runtime, so a bad token surfaces as a
    session.error on the first run, not here.
12. Edit agent/setup.sh: fill REPO_URL, GITHUB_TOKEN, ENVIRONMENT_ID, VAULT_ID,
    and your TIMEZONE/CRON if desired. Model is already claude-haiku-4-5.
13. Run it from the agent/ directory: bash setup.sh
    It creates the agent, then the daily scheduled deployment, and prints
    AGENT_ID and DEPLOYMENT_ID - save both. Check the create response's
    schedule.upcoming_runs_at to confirm the cron parsed as intended.

## Phase 4: First run and verification

14. Trigger a manual run before trusting the schedule (this is also your
    6-month backfill, the longest run you will ever pay for):
    ant beta:deployments run --deployment-id $DEPLOYMENT_ID
15. Verify, in order:
    - ant beta:deployment-runs list --deployment-id $DEPLOYMENT_ID
      (a successful run shows a session_id, no error)
    - The repo has a new commit "content: sync YYYY-MM-DD (...)"
    - data/posts.json validates and looks sane; data/last_sync.json shows the
      source used and counts
    - No open "crawler failure" issue on the repo
16. Spot-check quality: read 2-3 TL;DRs against the actual posts. If Haiku's
    extraction or summaries disappoint, switch MODEL to claude-sonnet-5:
    ant beta:agents update --agent-id $AGENT_ID --model '{id: claude-sonnet-5}'
17. Leave the schedule active. Ops commands live in README.md (pause,
    unpause, manual run, failure listing). If the deployment ever
    auto-pauses, fix the underlying resource and unpause - missed runs are
    not backfilled.

## Phase 5: Hosting and cost tracking

18. Enable GitHub Pages on the repo (Settings -> Pages -> deploy from main,
    root). The PWA lands in app/ in milestone 2.
19. Track spend (day | week | month | ytd):
    export ANTHROPIC_ADMIN_API_KEY=sk-ant-admin01-...
    python3 tools/cost_report.py month --workspace-id wrkspc_YOUR_ID
    Notes: cost data is daily-granularity with ~5 minute freshness, so "day"
    is today's partial bucket; run without --workspace-id for org-wide
    totals; costs attributed to the default workspace show workspace_id
    null, which is why step 4 created a dedicated workspace.

## Cost expectations

- Haiku 4.5 is the cheapest current-generation model, and the daily sync is
  incremental: most days fetch and summarize only 0-2 posts, so sessions are
  short. The first backfill is the outlier.
- Watch the first week with cost_report.py day, then drop to a monthly check.
- Managed Agents is in beta; if a CLI command rejects a field, check the
  current reference before debugging anything else:
  https://platform.claude.com/docs/en/managed-agents/reference
