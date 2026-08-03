#!/usr/bin/env python3
# DEAD PATH — this script cannot work. It requires an Anthropic Admin API key,
# and Admin API keys cannot be created on individual accounts (they need an
# organization). Kept for reference only; do not try to fix it. Check spend on
# the Console Cost page instead.
"""Cost tracker for the blog-reader Managed Agent.

Queries Anthropic's Usage & Cost Admin API (cost report) and prints total spend
plus a breakdown for a chosen period.

Usage:
  export ANTHROPIC_ADMIN_API_KEY=sk-ant-admin01-...
  python3 tools/cost_report.py day
  python3 tools/cost_report.py week
  python3 tools/cost_report.py month
  python3 tools/cost_report.py ytd
  python3 tools/cost_report.py month --workspace-id wrkspc_01ABC...

Periods are calendar-based, UTC:
  day   = since 00:00 today        week = since Monday 00:00
  month = since the 1st            ytd  = since Jan 1

Requirements and caveats (per the docs):
  - Needs an ADMIN API key (sk-ant-admin01-...), not a standard key. The Admin
    API is unavailable for individual accounts - set up an organization first
    in Console -> Settings -> Organization.
  - Cost data has daily granularity only and ~5 minute freshness, so "day"
    shows today's partial bucket.
  - Amounts are decimal strings in cents; this script converts to USD.
  - Sessions run under the default workspace report workspace_id null. Create
    a dedicated "blog-reader" workspace and use its API key for the agent so
    --workspace-id can isolate this project's spend.
Reference: https://platform.claude.com/docs/en/manage-claude/usage-cost-api
Field names: verify against the Cost API reference if the schema has changed:
https://platform.claude.com/docs/en/api/admin-api/usage-cost/get-cost-report
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

API_URL = "https://api.anthropic.com/v1/organizations/cost_report"
USER_AGENT = "claude-blog-reader-cost/1.0 (personal project)"
CHUNK_DAYS = 31  # daily buckets are capped per request; chunk long ranges


def period_start(period: str, now: datetime) -> datetime:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return midnight
    if period == "week":
        return midnight - timedelta(days=now.weekday())  # Monday
    if period == "month":
        return midnight.replace(day=1)
    if period == "ytd":
        return midnight.replace(month=1, day=1)
    raise ValueError(period)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def date_chunks(start: datetime, end: datetime):
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=CHUNK_DAYS), end)
        yield cur, nxt
        cur = nxt


def fetch(url: str, api_key: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        sys.exit(f"HTTP {e.code} from cost API: {body}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Blog-reader API cost report")
    ap.add_argument("period", choices=["day", "week", "month", "ytd"])
    ap.add_argument("--workspace-id", default=None,
                    help="wrkspc_... to isolate; omit for whole-org totals")
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_ADMIN_API_KEY", "")
    if not api_key.startswith("sk-ant-admin"):
        sys.exit("Set ANTHROPIC_ADMIN_API_KEY to an Admin API key "
                 "(sk-ant-admin01-...). Standard API keys will not work.")

    now = datetime.now(timezone.utc)
    start = period_start(args.period, now)

    total_cents = 0.0
    by_description = defaultdict(float)
    matched_any = False
    saw_null_workspace = False

    for chunk_start, chunk_end in date_chunks(start, now):
        page = None
        while True:
            params = [
                ("starting_at", iso(chunk_start)),
                ("ending_at", iso(chunk_end)),
                ("group_by[]", "workspace_id"),
                ("group_by[]", "description"),
            ]
            if page:
                params.append(("page", page))
            data = fetch(f"{API_URL}?{urllib.parse.urlencode(params)}", api_key)

            for bucket in data.get("data", []):
                for row in bucket.get("results", []):
                    ws = row.get("workspace_id")
                    if ws is None:
                        saw_null_workspace = True
                    if args.workspace_id and ws != args.workspace_id:
                        continue
                    matched_any = True
                    cents = float(row.get("amount") or 0)
                    total_cents += cents
                    by_description[row.get("description") or "other"] += cents

            if not data.get("has_more"):
                break
            page = data.get("next_page")

    label = {"day": "Today", "week": "This week", "month": "This month",
             "ytd": "Year to date"}[args.period]
    scope = args.workspace_id or "all workspaces"
    print(f"{label} ({iso(start)} -> {iso(now)}) | scope: {scope}")
    print(f"Total: ${total_cents / 100:,.4f} USD")
    if by_description:
        print("Breakdown:")
        for desc, cents in sorted(by_description.items(),
                                  key=lambda kv: -kv[1]):
            print(f"  {desc}: ${cents / 100:,.4f}")
    if args.workspace_id and not matched_any:
        hint = (" Note: default-workspace costs report workspace_id null - if "
                "the agent's API key lives in the default workspace, run "
                "without --workspace-id.") if saw_null_workspace else ""
        print(f"No costs matched workspace {args.workspace_id} in this "
              f"period.{hint}")


if __name__ == "__main__":
    main()
