---
name: start-of-day
description: Use when starting a work session to restore context. Lists per-session journals (docs/custom-agents/memory/sessions/) so you can pick which thread to resume; supports focusing one session, including finished ones, or falling back to the legacy dated journals.
---

# Start of Day Brief

## Overview
Sessions are per-thread journals at `docs/custom-agents/memory/sessions/<slug>.md` (written by `/end-of-day`). With no argument this **lists** the active sessions and asks which to focus — it does **not** auto-open one. Legacy `work-journal-YYYY-MM-DD.md` files stay readable via the `legacy` argument.

## Argument Reference
| Argument | Behavior |
|----------|---------|
| *(none)* | List active + paused sessions; ask which to focus |
| `<slug>` | Open that session's journal (full history, latest entry highlighted) |
| `all` | List sessions including `done` ones |
| `legacy` | Legacy dated-journal briefing (most recent file) |
| `legacy <n \| date \| last-week>` | Legacy briefing with the old count / ISO-date / last-week filter |

(`all` and `legacy` are reserved words; a session should not be named either.)

## Steps

### 0. Resolve ticket-workflow conventions
Find the config the way **start-ticket** step 0 does — the main checkout's copy first:
```bash
HUB=$(git worktree list --porcelain | sed -n 's/^worktree //p' | head -1)   # main checkout is always first
CFG="$HUB/docs/custom-agents/ticket-workflow.config.json"
[ -f "$CFG" ] || CFG="$(git rev-parse --show-toplevel)/docs/custom-agents/ticket-workflow.config.json"
```
Read `$CFG` if it exists: `taskFilesDir` (default `docs/custom-agents/tasks`), `project` (the folder under it), `worktreeDir` (default `.worktrees`), `contextBranch`. If the file is absent, use the defaults; if `$ROOT/<taskFilesDir>` (`ROOT` below) doesn't exist either, **skip step 2.5 entirely** rather than searching the repo for task files.

Then the **context root** every step below reads, per start-ticket's **Context home** rule: with `contextBranch` set, `CA="$HUB/docs/custom-agents"` (and `ROOT="$HUB"` for repo-relative keys like `taskFilesDir`) — verify the hub is on `contextBranch` and **STOP** if not; in local mode (folder gitignored), the same `CA`/`ROOT` with no branch check; otherwise `CA="$(pwd)/docs/custom-agents"`, `ROOT="$(pwd)"`, exactly as before.

### 1. Parse the argument
- **none** → list mode (active + paused).
- **`all`** → list mode, including `done`.
- **`legacy [...]`** → legacy daily briefing (step 5).
- **anything else** → treat as a `<slug>`; if it matches a `sessions/*.md`, focus it (step 4), otherwise show the list and note no such session.

### 2. List session files
```bash
ls "$CA/memory/sessions/"*.md 2>/dev/null | sort
```
- **If none found**: tell the user there are no per-session journals yet; suggest `/end-of-day` to create one, and mention `/start-of-day legacy` if old dated journals exist.
- **If files exist**: read each file's frontmatter (`session`, `status`, `branch`, `pr`, `last_updated`) and its latest `### 🎯 Next Session Plan` bullet.

### 2.5. Reconcile ticket delivery status
Task files carry **two status axes**: `status:` (implementation — the author's work: `in progress`/`complete`/`blocked`) and `ticket:` (delivery — the PR's journey to prod: `in-review` → `merged` → `done`). While you were away, PRs that were `in-review` may have merged. Reconcile the local `ticket:` field before briefing so the day starts from truth:

```bash
# task files still awaiting merge (delivery axis open) — <taskFilesDir> from step 0
grep -rl "^ticket: in-review" "$ROOT/<taskFilesDir>" 2>/dev/null
```
For each match, read its `pr:` field and check the merge state:
```bash
gh pr view <pr> --json state -q '.state'   # MERGED | OPEN | CLOSED
```
- **MERGED** → set that task file's `ticket: merged` (or `done` if the tracker shows it shipped), bump `synced_at:`, and **offer worktree cleanup** — `finish-ticket` deferred it until merge, so this is the moment: `git worktree remove <worktreeDir>/<slug>` (offer, never auto-remove; skip if the worktree has uncommitted work).
- **OPEN** → leave `in-review`.
- If `gh` or `pr:` is unavailable, fall back to the tracker (the task file's `url`/`id`), else skip that one cleanly.

Surface a one-line summary of anything that flipped (e.g. "ABC-42 / PR #123 merged → task file updated, offered worktree cleanup"). A task file with **no `ticket:` field** is still in implementation (pre-PR) — ignore it here; **finish-ticket** is what writes `ticket:` + `pr:` when it opens the PR, so pre-PR files legitimately lack them. This reconciliation operates on the task files under `$ROOT` — the current checkout, or the hub when `contextBranch` is set or local mode is on. In local mode, commit nothing. With `contextBranch`, commit every file it flipped to `contextBranch` in one commit (`docs(task): reconcile delivery status`), explicit paths only, and push — per start-ticket's **Context home** rule.

### 3. Present the session list (list mode)
Exclude `status: done` unless `all` was passed. Sort by `last_updated`, newest first:

> **Active sessions:**
>
> | Session | Status | Last updated | Branch / PR | Next up |
> |---------|--------|--------------|-------------|---------|
> | `csv-export` | active | 2026-07-08 | `feat/…` · #47 | Merge PR #47 |
> | … | | | | |
>
> Which session do you want to focus on? (`all` to include finished ones · `legacy` for the old daily journals)

Wait for the user to choose — do not auto-open a session.

### 4. Focus a session
For the chosen `<slug>`, Read `sessions/<slug>.md` and present its **latest** dated entry as the brief (earlier entries remain in the file for scrollback):

> **Session `<slug>` — [latest date]**  ·  status `[status]` · branch `[branch]` · PR [pr]
>
> ✅ **What was done:**
> [Accomplished bullets]
>
> 🔄 **Still in progress:**
> [In Progress bullets]
>
> 🎯 **Planned to start with:**
> [Next Session Plan bullets]
>
> 📝 **Notes to remember:**
> [Notes bullets]

Skip any section the entry doesn't have — a day whose only entries came from `/log-session` has `#### + Side session [time]` blocks instead of the four ✅/🔄/🎯/📝 sections. Show those verbatim under a **Side sessions** heading.

Then ask: "Ready to pick up here, or has anything changed?"

### 5. Legacy daily briefing (`legacy [...]`)
Fall back to the original dated-journal behavior:
```bash
ls "$CA/memory/work-journal-"*.md 2>/dev/null | sort
```
Apply the filter: default = most recent 1; `<n>` = last N by count; `last-week` = past 7 days; ISO date (e.g. `2026-03-30`) = that date forward. Read oldest → newest and present each with the ✅ / 🔄 / 🎯 / 📝 sections (skip any missing section cleanly). If no legacy files exist, say so and point to the session list.
