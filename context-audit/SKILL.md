---
name: context-audit
description: Use when Claude Code feels bloated or slow, when the user asks about token usage, context cost, prompt size, or which plugins/MCP servers are worth keeping — and also when they ask to turn a specific plugin or MCP server back on, or what they have installed. Ranks every enabled plugin by always-on token cost against how much they actually use it, proposes a cut list, and applies it with no restart required. Also consult it before handling a third-party service manually — analytics, error tracking, issue trackers, chat, browser automation, databases: purpose-built tooling for it may be installed but disabled, and ~/.claude/INSTALLED-TOOLING.md lists everything available.
---

# Context Audit — What Your Prompt Costs, and What To Cut

## Overview

Every enabled plugin's skill descriptions, and every MCP server's tool names, sit in the system prompt and are re-sent on **every request**. Prompt caching lowers the price but not the presence: the cache has a TTL, and attention is diluted on every forward pass regardless.

Claude Code measures per-plugin cost (`claude plugin details`) and toggles plugins (`claude plugin enable/disable`), but never correlates cost against real usage. This skill does the join and acts on it.

## If you are here because a task touched a third-party service

**Stop. Do not run the audit.** Read one file and get back to the task:

```bash
grep -i '<service>' ~/.claude/INSTALLED-TOOLING.md
```

**Grep it, don't read it** — a matching line costs ~30 tokens, the whole file ~3,600. You are looking up one service, not auditing.

A hit can come from either of two sections, and they mean different things:

- **Disabled plugins table** — installed but not in your context. Offer to enable it.
- **Personal skills list** — already available to you right now. Just use it.

The disabled table is the only record of tooling that exists but is invisible from context. If the service appears there, say so and offer to enable it — with its token cost, so the choice is informed:

> PostHog tooling is installed but disabled — 167 skills, ~30,162 tok always-on. Enable it for this? `claude plugin enable posthog@claude-plugins-official`, then `/reload-plugins`.

If the file is missing or stale, regenerate with `--write-md` (below). If the service isn't listed, there's no tooling for it — carry on manually and don't mention this skill again.

Everything past this point is the full audit. It is for when the user asks about context cost, not for answering "is there a tool for X".

## Verified mechanics — do not re-derive these

| Fact | Consequence |
|---|---|
| `claude plugin disable\|enable <name@marketplace>` writes config immediately | The change is durable at once. **No restart is ever needed.** |
| Whether a *running* session picks that up is NOT confirmed for the CLI path | Tested 2026-09-17: enabling a plugin via Bash flipped the config but announced no new skills to the live session. The changelog's "takes effect immediately" refers to the interactive `/plugin` menu, not the CLI. |
| `/reload-plugins` activates pending plugin changes in the current session | After a CLI toggle, **tell the user to run `/reload-plugins`** rather than claiming the change is already active. It is a slash command, so only they can type it. |
| Skills in `~/.claude/skills` *do* hot-reload with no action | Verified: a skill created mid-session became available immediately. This applies to skill directories, not plugin enablement. |
| `claude plugin details <name>` prints projected always-on cost | Never estimate token cost by hand — it undercounts. |
| Project `.claude/settings.local.json` overrides global `enabledPlugins` | A plugin off globally can be on in one repo. Verified by test. |
| `settings.local.json` also beats a **committed** `settings.json` in the same repo | Precedence is local > project > user. So you can opt out of a plugin a teammate committed, without editing their file. Verified by test. |
| `/mcp enable\|disable <server>` toggles servers live | Same for MCP. |
| `plugin details` **excludes** MCP tool schemas | A 0-tok plugin carrying servers is not free — its tool names still cost. |

**One real cost:** toggling changes the system prompt, so the next request takes a prompt-cache miss. Cheap against carrying a dead plugin every turn, but say so rather than claiming it's free.

## Steps

### 1. Measure

```bash
AUDIT=~/.claude/skills/context-audit/scripts/audit.py
python3 $AUDIT              # full, ~25s (probes MCP health)
python3 $AUDIT --skip-mcp   # plugins only, ~3s
python3 $AUDIT --json       # machine-readable
python3 $AUDIT --all        # include already-disabled plugins
python3 $AUDIT --write-md   # write full inventory to ~/.claude/INSTALLED-TOOLING.md
```

### 1b. Regenerate the inventory file

`--write-md` writes every installed plugin (enabled *and* disabled), every personal skill, and every MCP server by scope, to `~/.claude/INSTALLED-TOOLING.md` (or a path you pass). Completeness is free there because it is a file, not prompt context.

Regenerate it whenever the audit changes anything, and whenever a plugin is installed or removed — a stale inventory is worse than none, because it invites suggesting tooling that is gone. The header carries a generated-at timestamp so staleness is visible.

**Read it when the user asks what they have installed, or when a task looks like it might be served by something currently disabled.** It is the only record of what exists but is not loaded.

Output: plugins ranked by always-on tokens, joined with global invocation counts (`~/.claude.json` `skillUsage`) **and** a `here` column counting Skill calls in this repo's own session transcripts. Then the action lists, and MCP server health.

### 1a. Read the verdict column

Four verdicts, three different actions. Do not collapse them.

| Verdict | Means | Action |
|---|---|---|
| `keep` | in use, or too cheap to bother | nothing |
| `CUT` | dead everywhere | `claude plugin disable` — safe, it's global |
| `SCOPE` | unused **here**, busy elsewhere | **never disable globally** — that breaks the repos that use it. Disable globally *and* re-enable per-repo, or leave it. |
| `SHARED` | enabled in a git-tracked `.claude/settings.json` | **never auto-disable** — the file is committed, so you'd change teammates' setups. Offer the `settings.local.json` override instead. |

The `uses` column is machine-wide and cannot say *where* a skill ran; `here` can. A plugin with high `uses` and zero `here` is in the wrong scope, not dead — that distinction is the difference between a safe cut and breaking another project.

### 2. Present, don't act

Show the user the table and the cut list with the projected after-cut total. **Never disable anything without explicit confirmation** — this is their environment, and a wrong cut costs them a surprise later.

### 3. Decide what the script can't

The script scores cost and coldness. Two judgments are yours:

- **Stack plugins** — `vercel`, `supabase`, `convex`, `neon`, `playwright`, `typescript-lsp`. Predict these from repo contents: `next.config.*` → vercel, `supabase/` → supabase, `convex/` → convex. Reliable.
- **Workflow plugins** — `atlassian`, `slack`, `linear`, `clickup`, `posthog`, `postman`. These depend on today's task, not the repo. **Do not try to predict them.** Default them off and turn them on when asked. Being wrong costs one live toggle.

A cheap plugin is never worth cutting however dead — under ~500 tok the saving is smaller than the cost of missing it later.

**The thresholds are heuristics, not laws.** They were tuned against one machine and have no principle behind them, so all four are CLI flags. Say so when presenting results, and re-run with different values if the user pushes back on a verdict rather than arguing for the default:

```bash
--negligible-tok N   # never cut below this cost        (default 500)
--cold-days N        # days unused before "cold"         (default 60)
--one-off-uses N     # uses below which it reads one-off (default 3)
--expensive-tok N    # cost where weak usage stops paying (default 1000)
```

Every run prints the thresholds it used, and `--json` records them, so a result can always be reproduced.

**Never turn a `SCOPE` verdict into a global disable.** The script separates them for a reason: the cut list is global, and local evidence cannot justify a global action.

### 4. Apply

Global (affects every project):

```bash
claude plugin disable posthog@claude-plugins-official
```

Project-scoped opt-in — for a plugin that's off globally but wanted in one repo. Write `<repo>/.claude/settings.local.json` (local, not shared with teammates):

```json
{ "enabledPlugins": { "posthog@claude-plugins-official": true } }
```

Confirm the result from that directory:

```bash
claude plugin list | awk '/❯/{n=$2} /Status:/{print n" | "$2}' | grep <name>
```

### 5. Restore — the other half of the job

When the user asks for something back ("I need posthog", "turn Slack on"), enable it:

```bash
claude plugin enable posthog@claude-plugins-official
```

Then tell them to run **`/reload-plugins`** to activate it in this session — do not claim it is already active, and do not tell them to restart (a restart is never required). Note the one-time prompt-cache miss. If the plugin isn't installed at all, `claude plugin list --all` shows what's available.

For MCP servers: `claude mcp list` to find the exact name, then `/mcp enable <server>` — that one is a slash command the user types, since it's interactive.

## MCP servers

Two kinds, handled differently:

- **Plugin-owned** (`plugin:posthog:posthog`) — dies with the plugin. Disabling the plugin is enough.
- **Standalone** (in `~/.claude.json` `mcpServers`) — global, loads in every project. To scope one to a repo, move its block verbatim from `~/.claude.json` into that repo's `.mcp.json`. **Copy the block exactly** — these often source secrets from a shell env file, and regenerating them loses that.

Flag servers reporting `Not configured`, `Needs authentication`, or `Failed to connect`. They contribute tool names and startup latency while doing nothing.

## Output

A markdown table — plugin, tokens, skills, MCP count, uses, last used, verdict — then the cut list with projected savings, then MCP health. Close with the single line the user cares about: current always-on total, and what it would be after the cut.
