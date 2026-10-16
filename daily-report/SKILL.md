---
name: daily-report
description: Use when the user wants to prepare for a daily standup meeting, is about to join a standup, or asks for a summary of recent ticket and email activity to share with their team.
---

# Daily Report (Standup) Agent

## Overview
Gathers project management ticket data and Gmail updates via MCPs, synthesizes them into a standup document saved to `docs/custom-agents/standups/YYYY-MM-DD.md` (in the main checkout, committed to `contextBranch`, when the repo sets one; in the main checkout and never committed in local mode — see step 5). Two output sections: structured breakdown for personal reference, short narrative to read aloud.

## Steps

### 1. Intake

Ask the user these questions one at a time, waiting for each answer before asking the next. These are guidance prompts — adapt the wording naturally to the conversation.

Ask the user for their standup focus (e.g., "What's today's standup about — a specific project, topic, or should I do a broad sweep?").

Ask the user which project management tool they're using (e.g., "Which PM tool are you using — Jira, Linear, GitHub Issues, Monday.com, Asana, or something else?").

Ask the user for any Gmail filters to apply (e.g., "Any Gmail filters? For example a sender, label, keyword, or just 'all recent'.").

---

### 2. MCP Check

Before fetching anything, verify each required MCP is reachable by attempting a lightweight call. Do not rely on introspection — make the call and treat a tool-not-found or connection error as "not connected".

**PM tool → MCP mapping and connectivity test:**

| Tool | MCP identifier | Connectivity test call |
|------|----------------|------------------------|
| Jira / Atlassian | `mcp__plugin_atlassian_atlassian` | `mcp__plugin_atlassian_atlassian__authenticate` with minimal args |
| Monday.com | `mcp__claude_ai_monday_com` | `mcp__claude_ai_monday_com__get_user_context` |
| Linear | Linear MCP (unknown — see below) | Ask user for prefix, then attempt a lightweight list call |
| GitHub Issues | GitHub MCP (unknown — see below) | Ask user for prefix, then attempt a lightweight list call |
| Asana | Asana MCP (unknown — see below) | Ask user for prefix, then attempt a lightweight list call |
| Other | Unknown — see below | Ask user for prefix, then attempt a lightweight list call |

**For tools without a known MCP identifier (Linear, GitHub Issues, Asana, Other):** Ask the user: "What is the MCP tool prefix for [tool name] in your session?" Use their answer as the MCP identifier for all subsequent calls.

If a PM tool connectivity test fails with a tool-not-found or connection error, halt and tell the user:
> "The [tool name] MCP isn't connected. Enable it in your MCP settings and re-run /daily-report."

**Gmail connectivity test:** Attempt a search for a single recent email as a connectivity test.

If the Gmail connectivity test fails, do NOT halt the skill. Instead, ask the user:
> "Gmail MCP isn't connected. Want to continue with just [PM tool] data, or stop here to enable it?"

If the user says continue, proceed without email data. Note "Gmail not available — email data skipped." in the output document.

If the Gmail connectivity test succeeds (even with empty results), Gmail is available — proceed.

Do not proceed to data fetching until the PM tool MCP is confirmed available.

---

### 3. Resolve Current User Identity

Before fetching tickets, resolve who the current user is so queries can be scoped to "assigned to me" and "commented by me":

- **Atlassian**: Call `mcp__plugin_atlassian_atlassian__authenticate` to get the current user's account ID or email.
- **Monday.com**: Call `mcp__claude_ai_monday_com__get_user_context` to get the current user's ID.
- **Other tools**: If the MCP doesn't provide a self-identity endpoint, ask the user: "What is your username or email in [tool name]?"

Use the resolved identity to scope all "assigned to me" and "commented by me" queries in the next step.

---

### 4. Fetch Data

Fetch both sources in parallel (skip Gmail if unavailable per Step 2).

**PM data** (via the appropriate MCP):
- Tickets assigned to the resolved user
- Tickets the resolved user commented on or whose status changed in the last 24 hours
- Scope to the focus topic/project if one was specified; otherwise fetch broadly

**Gmail data** (via Gmail MCP, if available):
- Emails from the last 24 hours matching the user's specified filters
- If the user said 'all recent', fetch the last 24 hours with no additional filter

If no PM tickets are found, note: "No recent ticket activity found."
If no emails are found, note: "No matching emails in the last 24h."

**Classifying tickets as Yesterday vs. Today:**
- **Yesterday**: tickets whose status moved to done/closed/completed within the last 24 hours, OR emails that were acted upon (replied to, resolved)
- **Today**: tickets currently in an in-progress/active/open state assigned to the user, OR emails awaiting a response
- If a ticket is both (e.g., was in-progress yesterday and is still in-progress today), place it under **Today** only

---

### 5. Synthesize and Save

**Determine the output path:**
1. Get today's date in YYYY-MM-DD format
2. Run `git rev-parse --show-toplevel` via Bash to find the project root. If that fails (not a git repo), fall back to the current working directory and warn the user: "Could not detect project root — saving to current directory."
3. If the repo sets `contextBranch` (start-ticket's config discovery — the main checkout's `docs/custom-agents/ticket-workflow.config.json` first), the project root for this file is the **main checkout** (`git worktree list --porcelain | sed -n 's/^worktree //p' | head -1`), which must be on `contextBranch` — **STOP** if not; never switch it. In local mode (no `contextBranch`, folder gitignored — same rule) it's also the main checkout, with no branch check. Otherwise it's the root from item 2.
4. Check if `docs/custom-agents/standups/` exists in that root — create it if not
5. Output path: `<root>/docs/custom-agents/standups/YYYY-MM-DD.md`

**Write the standup document:**

```
# Standup — YYYY-MM-DD
## Focus: [topic the user specified, or "General"]

---

## For Me (Yesterday / Today / Blockers)

**Yesterday**
- [one bullet per completed ticket or email action from the last 24h]

**Today**
- [one bullet per in-progress ticket or planned next action]

**Blockers**
- [one bullet per blocked ticket, awaiting reply, or unresolved dependency]
- Write "None" if no blockers are found

---

## When It's My Turn (Read Aloud)

[Write 2–4 sentences in first person. Cover: what was completed, what's being worked on today, and any blockers. Keep it under 30 seconds when read aloud. Example: "Yesterday I finished X and reviewed Y. Today I'm focused on Z. No blockers."]
```

If Gmail was skipped, add a note at the bottom: "Gmail not available — email data skipped."

**With `contextBranch` set**, commit just this file onto it in the main checkout and push, per start-ticket's **Context home** rule — `git -C "$HUB" add -f -- <path>`, `git -C "$HUB" commit -m "docs(standup): YYYY-MM-DD" -- <path>`, `git -C "$HUB" push <contextRemote, default origin> <contextBranch>`. Without it, don't commit, as before.

**After saving**, tell the user:
> "Standup saved to <root>/docs/custom-agents/standups/[YYYY-MM-DD].md" (plus the `contextBranch` commit, if one was made; in local mode, "local only, not committed")
