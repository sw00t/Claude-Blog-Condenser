#!/usr/bin/env bash
# One-time setup: creates the ingestion agent and its daily scheduled deployment.
#
# Prereqs:
#   - ant CLI installed and authenticated (sets the managed-agents-2026-04-01
#     beta header automatically): https://platform.claude.com/docs/en/managed-agents/quickstart
#   - jq installed
#   - A cloud environment created once; put its ID below:
#     https://platform.claude.com/docs/en/managed-agents/environments
#   - A vault holding the GitHub MCP credential; put its ID below. The repo
#     resource's authorization_token only covers git clone/pull/push via the
#     Anthropic git proxy - it does NOT authenticate the MCP server, which is
#     what the failure protocol needs in order to file issues.
#     https://platform.claude.com/docs/en/managed-agents/vaults
#   - Run from the agent/ directory of the content repo.
set -euo pipefail

# ── Secrets ──────────────────────────────────────────────────────────────────
# GITHUB_TOKEN is NOT stored in this file - this script is committed to the
# content repo, which is public. Put the fine-grained PAT (this repo only,
# Contents RW + Issues RW) in ../.env, which .gitignore excludes:
#     GITHUB_TOKEN=ghp_xxx
# shellcheck source=/dev/null
[ -f ../.env ] && . ../.env
: "${GITHUB_TOKEN:?set GITHUB_TOKEN in ../.env or the environment}"
case "$GITHUB_TOKEN" in
  ghp_xxx|*REPLACE_ME*)
    echo "error: GITHUB_TOKEN is still the .env.example placeholder." >&2
    exit 1
    ;;
esac

# ── Fill these in ────────────────────────────────────────────────────────────
REPO_URL="https://github.com/sw00t/claude-blog-reader"
ENVIRONMENT_ID="env_01Ds1hNehF4UCQY25gsvxQKx"
VAULT_ID="vlt_011Cdetr9ikJ1GQuQkYa1XS3"          # vault containing the GitHub MCP credential
TIMEZONE="Etc/UTC"                 # IANA tz; avoid 1-3 AM local if you change it (DST)
CRON="0 6 * * *"                   # daily 06:00 in $TIMEZONE
MODEL="claude-haiku-4-5"           # cheapest fit; if extraction/TLDR quality lags, bump to claude-sonnet-5 (one-line change + ant beta:agents update)
# ─────────────────────────────────────────────────────────────────────────────

# Fail fast on unfilled placeholders rather than creating a half-wired deployment.
for var in REPO_URL ENVIRONMENT_ID VAULT_ID; do
  case "${!var}" in
    *REPLACE_ME*|*YOUR_USER*)
      echo "error: $var is still a placeholder - edit the block above." >&2
      exit 1
      ;;
  esac
done

echo "Creating agent..."
# `tools` goes in the stdin body, not --tool flags, for two reasons:
#   1. permission_policy MUST be always_allow. An mcp_toolset left at its
#      default evaluates to "ask": the session goes idle with
#      stop_reason=requires_action waiting for a user.tool_confirmation that
#      nobody sends on a cron run, so the failure protocol hangs instead of
#      filing an issue. Verified by smoke test - see README "Known caveats".
#   2. The --tool flag's relaxed-YAML parser rejects this depth of nesting
#      ("Failed to parse request body: unexpected token {").
# Flags below still win over stdin on the keys they set.
agent=$(ant beta:agents create \
  --name "Blog Reader Ingest" \
  --model "{id: $MODEL}" \
  --system "$(cat system-prompt.md)" \
  --mcp-server '{type: url, name: github, url: https://api.githubcopilot.com/mcp/}' \
  --format json <<'YAML'
tools:
  - type: agent_toolset_20260401
    default_config:
      enabled: true
      permission_policy:
        type: always_allow
  - type: mcp_toolset
    mcp_server_name: github
    default_config:
      enabled: true
      permission_policy:
        type: always_allow
YAML
)
AGENT_ID=$(jq -r '.id' <<<"$agent")
echo "AGENT_ID=$AGENT_ID"

echo "Creating scheduled deployment..."
# `resources` and `vault_ids` are both valid on Create Deployment - confirmed
# against `ant beta:deployments create --help` (--resource: "Resources (e.g.
# repositories, files) to mount into each session's container"; --vault-id:
# "Vault IDs for stored credentials the agent can use during sessions created
# from this deployment"). Both take the same shapes as on Create Session.
DEPLOYMENT_ID=$(ant beta:deployments create <<YAML | jq -er '.id'
name: Blog reader daily sync
agent: $AGENT_ID
environment_id: $ENVIRONMENT_ID
vault_ids:
  - $VAULT_ID
resources:
  - type: github_repository
    url: $REPO_URL
    mount_path: /workspace/reader
    authorization_token: $GITHUB_TOKEN
initial_events:
  - type: user.message
    content:
      - type: text
        text: Run the daily blog sync. Read /workspace/reader/agent/task-prompt.md and follow it exactly.
schedule:
  type: cron
  expression: "$CRON"
  timezone: $TIMEZONE
YAML
)
echo "DEPLOYMENT_ID=$DEPLOYMENT_ID"

echo
echo "Verify the schedule (upcoming_runs_at), then test with a manual run:"
echo "  ant beta:deployments run --deployment-id $DEPLOYMENT_ID"
echo "Watch for failures:"
echo "  ant beta:deployment-runs list --deployment-id $DEPLOYMENT_ID --has-error"
