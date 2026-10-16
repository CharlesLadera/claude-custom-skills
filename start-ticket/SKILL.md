---
name: start-ticket
description: Use when starting work on a ticket — provisions an isolated git worktree on a fresh branch, scaffolds a ticket task file, and (if a tracker is configured) moves the ticket to In Progress. Works in any repo; resolves project conventions from config or by detecting/asking.
---

# Start Ticket — Provision Worktree

## Overview
Provisions an isolated worktree for a ticket so you can work multiple tickets in parallel without branch-switch churn. Project-agnostic: it resolves a small set of conventions (worktree location, base branch, optional integration branch, tracker, task-file dir, dev command) from an optional config file, or by detecting/asking. The ticket reference (e.g. `PROJ-123`, a GitHub issue number, or a freeform name) is provided by the user.

Run this from the **main checkout** (the repo root), not inside an existing worktree — syncing branches requires them to be checked out, and a branch can only live in one working tree.

## Steps

### 0. Resolve project conventions
Find the config — the **main checkout's** copy first, because a worktree branched from the base may not carry `docs/custom-agents/` at all (see **Context home** below):
```bash
HUB=$(git worktree list --porcelain | sed -n 's/^worktree //p' | head -1)   # main checkout is always first
CFG="$HUB/docs/custom-agents/ticket-workflow.config.json"
[ -f "$CFG" ] || CFG="$(git rev-parse --show-toplevel)/docs/custom-agents/ticket-workflow.config.json"
```
Read `$CFG` if it exists. Expected keys (all optional):
```json
{
  "worktreeDir": ".worktrees",
  "baseBranch": "main",
  "branchFrom": null,
  "integrationBranch": null,
  "contextBranch": null,
  "contextRemote": "origin",
  "tracker": "linear",
  "branchPrefix": "",
  "taskFilesDir": "docs/custom-agents/tasks",
  "project": "web",
  "devCommand": "pnpm dev"
}
```
For any key not in the config, detect or ask:
- **worktreeDir** — use an existing `.worktrees/` or `.claude/worktrees/`; else default `.worktrees/`. Ensure it's gitignored (`git check-ignore`); if not, add + commit the ignore entry.
- **baseBranch** — the PR target. Detect: prefer `origin/staging`, else `origin/develop`, else the repo default (`git symbolic-ref refs/remotes/origin/HEAD`). Confirm if ambiguous.
- **branchFrom** — OPTIONAL. The branch new ticket branches are cut from, when that differs from the PR target. Example: PRs go to `qa`, but `qa` holds unreleased work, so branches are cut from `master` to keep that work out of the PR. Unset ⇒ `baseBranch`. When it differs from `baseBranch`, **never merge `baseBranch` into the ticket branch**, not even to fix PR conflicts; ask the user how the team resolves those.
- **integrationBranch** — OPTIONAL personal test-bed branch that all in-flight work is merged into for combined testing. If unset, skip every integration-branch step.
- **tracker** — `linear` (MCP), `github` (`gh issue view`), or `none` (user provides title/branch).
- **branchPrefix** — optional namespace prepended to branch names (e.g. `yourname/`).
- **taskFilesDir** — where ticket markdown lives; default `docs/custom-agents/tasks`.
- **project** — the app/sub-project this ticket belongs to. It is a folder under `taskFilesDir`, so every task-file path is `<taskFilesDir>/<project>/<status-folder>/<ticket-id>-<slug>.md`. Resolve: config, else the monorepo package the ticket touches, else the repo name. Ask once when ambiguous, and pin it in the config. **Every ticket skill reads this same key** — if it is missing they will each guess differently and lose track of the file.

- **contextBranch** — OPTIONAL. Set it (e.g. `"dev-yourname"`) when `docs/custom-agents/` must live **only** on that branch, in the main checkout, and never on the base branch or a feature branch. Unset ⇒ everything below behaves exactly as before, unless the folder is gitignored (local mode, below). **contextRemote** is the remote it pushes to (default `origin`).

If the config file was absent, offer to write it with the resolved values so future runs are zero-friction.

#### Context home (`contextBranch`, local mode) — the rule every skill that touches `docs/custom-agents/` follows
This skill owns the rule; `resume-ticket`, `finish-ticket`, `ticket-status`, `end-of-day`, `log-session`, `start-of-day`, `save-conversation` and `daily-report` apply it by reference, as does any skill that reads journals.

- **Unset, folder not gitignored** → `CA` is the current checkout's `docs/custom-agents/` — today's behavior, nothing else changes.
- **Unset, folder gitignored** → **local mode**: the folder is private to this machine and never reaches git. Detect it from the patterns alone (`--no-index`, so a folder that is still tracked mid-migration counts too):
  ```bash
  git -C "$HUB" check-ignore -q --no-index docs/custom-agents/ && CTX_MODE=local
  CA="$HUB/docs/custom-agents"
  ```
  Paths resolve exactly as in the **Set** case below — everything under `$CA`, repo-relative keys against `$HUB`, because a worktree never inherits ignored files. There is no branch to check. **Nothing under `$CA` is ever staged, committed, pushed or PR'd** — every sibling's commit step is skipped, and its report says "local only, not committed" instead of a sha. When git refuses a path as ignored, that refusal is the point: never `git add -f` it.
- **Set** → the folder's only home is the main checkout ("hub"), which must have `contextBranch` checked out:
  ```bash
  CTX_BRANCH=<contextBranch>; CTX_REMOTE=<contextRemote, default origin>
  CA="$HUB/docs/custom-agents"
  [ "$(git -C "$HUB" branch --show-current)" = "$CTX_BRANCH" ] || echo "STOP: main checkout is on '$(git -C "$HUB" branch --show-current)', not '$CTX_BRANCH'"
  ```
  If the check fails, **STOP and tell the user.** Never switch the hub's branch to fix it — other sessions share that checkout. Every read and write of `sessions/`, `conversations/`, `tasks/`, `notes/`, `standups/` and the config itself goes under `$CA`, never the current worktree's `docs/custom-agents/`. Config path keys like `taskFilesDir` are repo-relative, so resolve them against `$HUB`.
- **Committing under `$CA`** (`contextBranch` set only — never in local mode) — the folder is gitignored on the base branch (and so on `contextBranch` too, which merges it), so stage with force, name every path, and commit **only** those paths so a parallel session's staged work is never swept in. For a move, pass both the old and the new path:
  ```bash
  git -C "$HUB" add -f -A -- <paths relative to $HUB>
  git -C "$HUB" commit -m "<msg>" -- <same paths>
  git -C "$HUB" push "$CTX_REMOTE" "$CTX_BRANCH"
  ```
  If the push is rejected (remote ahead), stop and report — don't pull, rebase or force in the shared checkout. **Never** open a PR for these files, and never commit `docs/custom-agents/` into a feature branch.
- **Feature branches (`contextBranch` set or local mode)** — before any feature-branch push or PR, run `git diff --name-only origin/<baseBranch>...HEAD -- docs/custom-agents`; if it lists anything, warn that the branch carries context files (they belong on `contextBranch`, or nowhere in local mode), and don't push until the user has dealt with them. In local mode, if the folder is still tracked, tell the user to finish the migration (`git rm -r --cached docs/custom-agents`, gitignore entry) in its own commit.

### 1. Guardrail — not inside a worktree
```bash
TOP=$(git rev-parse --show-toplevel); CDIR=$(cd "$(git rev-parse --git-common-dir)" && pwd -P)
[ "$CDIR" = "$TOP/.git" ] && echo "main checkout OK" || echo "looks like a worktree — run from the main checkout"
```
If in a worktree, STOP and tell the user to run from the main checkout.

### 2. Get the ticket
- **linear** → fetch via Linear MCP by identifier; capture title, `gitBranchName`, and any app/component label; lift description + acceptance criteria.
- **github** → `gh issue view <number> --json title,body,labels,url`.
- **none** → ask the user for the title and a short slug.

Derive the branch name: use the tracker's suggested branch name if present, else `<branchPrefix><ticket-id>-<kebab-title>`. Derive a short worktree **dir** slug `<ticket-id>-<2-4 keywords>` and **ask the user to confirm or edit** it before creating anything.

### 3. Duplicate-work check
```bash
git fetch origin
gh pr list --search "<ticket-id>" --state all 2>/dev/null || true
git log --oneline origin/<baseBranch> | grep -i "<ticket-id>" || true
```
Use content-based checks, never `git branch --contains` (squash-merges hide the SHA). Also check the integration branch if configured. If the ticket already shipped or is in flight, STOP and report before creating a worktree.

### 4. Sync the base (and integration) branch
```bash
git checkout <integrationBranch or baseBranch>
git merge origin/<baseBranch> --no-edit
```
If an integration branch is configured, this is where you check it out and bring it up to date. If a generated/lockfile conflict blocks the merge, reset that path and re-merge.

**With `contextBranch` set, never check out anything in the main checkout** — it must stay on `contextBranch`. Just `git fetch origin` (step 5 branches from `origin/<branchFrom or baseBranch>`, so the local base needn't move). If `integrationBranch` equals `contextBranch`, the hub already has it checked out: `git merge origin/<baseBranch> --no-edit` there is the sync. If it names a different branch, skip the sync and say so rather than switching the hub.

### 5. Create the worktree off the base branch
Base the new branch on **`branchFrom`** when set, else the **base branch** (the PR target). Never base it on the integration branch's tip, which would drag other in-flight work into the PR.
```bash
git worktree add <worktreeDir>/<slug> -b <branch-name> origin/<branchFrom or baseBranch>
```

A worktree is a fresh directory, so it does **not** inherit `.claude/settings.local.json` — that file is gitignored, and gitignored files don't come along. Everything the main checkout has enabled locally (plugin opt-ins, the `permissions.allow` list) is silently absent until seeded:
```bash
WT=<worktreeDir>/<slug>
SRC="$(git rev-parse --show-toplevel)/.claude/settings.local.json"
if [ -f "$SRC" ]; then
  mkdir -p "$WT/.claude" && cp "$SRC" "$WT/.claude/settings.local.json"
  echo "seeded local Claude settings into $WT"
fi
```
No-op when the main checkout has no local settings. The copy stays gitignored inside the worktree, so it adds nothing to `git status`. Copy the **whole file**, not just one key — a worktree missing `permissions.allow` re-prompts for commands you already approved.

### 6. Scaffold the ticket task file
`ls <taskFilesDir>/<project>/{backlog,in-progress,complete,canceled}/` FIRST — you may be enriching an existing stub, not creating one. Write `<taskFilesDir>/<project>/in-progress/<ticket-id>-<slug>.md`:
```yaml
---
id: <ticket-id>
title: <title verbatim>
status: in progress
source: <tracker>
url: <ticket URL, if any>
project: <project/app>
synced_at: <today YYYY-MM-DD>
git_branch: <branch-name>
---
```
Body: lift the tracker description + acceptance criteria, add scope notes / technical hints. Rich is good — this is the offline pickup context for a cold start.

**With `contextBranch` set**, the task file goes under `$HUB/<taskFilesDir>/…` (never into the new worktree), and is committed and pushed to `contextBranch` right away per **Context home** — `git -C "$HUB" add -f -A -- <taskFilesDir>/<project>/in-progress/<ticket-id>-<slug>.md`, commit those paths only with `docs(task): <ticket-id> start`, push. Enriching an existing stub is the same commit. In local mode, write it under `$HUB/<taskFilesDir>/…` the same way and commit nothing. Otherwise, leave it as today.

### 7. Update the tracker
If `tracker` is `linear` or `github`, set the ticket's status to In Progress (Linear MCP, or `gh issue edit`/project status).

### 8. Report
Print: worktree path, branch (and the branch it was cut from: `branchFrom` or the base branch), ticket file path (and, with `contextBranch`, the commit that landed it there; in local mode, "local only, not committed"), and the next step — "Open a terminal in the worktree, run `claude`, then use **resume-ticket** for this ticket." Do NOT start a dev server here; if an integration branch / shared backend dev process runs from the main checkout, leave the main checkout free for it.
