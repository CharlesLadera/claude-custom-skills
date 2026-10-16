---
name: resume-ticket
description: Use when picking up an in-progress ticket inside its git worktree — re-reads the ticket task file, recaps done-vs-pending from git, and launches the project's dev server. Works in any repo; resolves conventions from config or by detecting.
---

# Resume Ticket — Enter & Work

## Overview
Restores context for an in-progress ticket and starts its dev server. Run this **inside the ticket's worktree**. The ticket reference is provided by the user. Project-agnostic — resolves conventions the same way as **start-ticket**.

## Steps

### 0. Resolve project conventions
Find and read the config the way **start-ticket** step 0 does — the main checkout's copy first, since this worktree may not carry `docs/custom-agents/` at all (same keys as start-ticket): `worktreeDir`, `baseBranch`, `branchFrom`, `taskFilesDir`, `project`, `devCommand`, `contextBranch`. Detect anything missing. `project` is the folder under `taskFilesDir` holding this ticket's file. If `contextBranch` is set or local mode is on, apply start-ticket's **Context home** rule: task files live under `$HUB/<taskFilesDir>`, not in this worktree.

### 1. Locate the worktree
```bash
git worktree list | grep -i "<ticket-id>" || echo "no worktree for <ticket-id>"
```
- If none exists, tell the user to run **start-ticket** from the main checkout first. Stop.
- If you are not already inside it, print `cd <worktree-path>` for the user (a skill can't relocate the session), then continue once they're there.

Worktrees created before seeding existed have no `.claude/settings.local.json` — it is gitignored, so `git worktree add` never copied it, and locally enabled plugins plus the `permissions.allow` list go missing. Repair once you are inside:
```bash
MAIN=$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)
SRC="$MAIN/.claude/settings.local.json"
DST="$(git rev-parse --show-toplevel)/.claude/settings.local.json"
if [ -f "$SRC" ] && [ ! -f "$DST" ]; then
  mkdir -p "$(dirname "$DST")" && cp "$SRC" "$DST"
  echo "seeded local Claude settings from the main checkout"
fi
```
From inside a worktree `git rev-parse --show-toplevel` is the worktree, not the main checkout — derive the latter from `--git-common-dir`. The `! -f` guard never overwrites an existing file, so a worktree whose settings have deliberately diverged keeps its own. **start-ticket** seeds this at creation; this is the catch-up for worktrees that predate it.

### 2. Re-read the ticket file
Find and read `<taskFilesDir>/<project>/in-progress/<ticket-id>-*.md` for full context (acceptance criteria, scope notes, hypotheses) — resolved against `$HUB` when `contextBranch` is set or local mode is on, else this worktree. If you update the file during the session, commit it to `contextBranch` per **Context home**, never onto this ticket's branch. In local mode, commit nothing.

### 3. Done-vs-pending recap
```bash
git status
git diff --stat
git log --oneline origin/<branchFrom or baseBranch>..HEAD
```
Compare against the branch the ticket was cut from (`branchFrom` when set). Against a different `baseBranch`, the log would count that branch's own commits as ticket work.
Summarize: committed work on this branch, uncommitted tree changes, and remaining acceptance criteria.

### 4. Launch the dev server
Resolve the dev command:
- From config `devCommand`. For a monorepo, this may be a template like `pnpm --filter <pkg> dev` — fill in the package for this ticket's app.
- Else detect from `package.json` scripts (`dev`, then `start`), Cargo/Make/etc.

Start it in the **background** and capture the process id so you can tail logs. Read the port from the server's ready/listening log line and report it (don't assume a port). If the port is already bound (another worktree's server), warn and ask before starting a second one.

If the app depends on a separate backend/codegen dev process (e.g. a shared database/codegen watcher) that runs from the main checkout, and the app errors with "function/route not found" style messages, tell the user to ensure that process is running.

### 5. Brief
Report where the ticket stands, the dev URL, and the next pending step. The user drives the browser; you read the log.
