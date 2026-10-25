---
name: ticket-status
description: Use to see all in-flight ticket worktrees at a glance — a dashboard table of each worktree's ticket, branch, PR, integration status, running dev server, and task-file status. Works in any repo; resolves conventions from config or by detecting.
---

# Ticket Status — Dashboard

## Overview
Produces one table across every active worktree so you can see all parallel ticket work at a glance. Run anywhere. Project-agnostic — resolves conventions the same way as **start-ticket**.

## Steps

### 0. Resolve project conventions
Find and read the config the way **start-ticket** step 0 does (the main checkout's copy first): `worktreeDir`, `baseBranch`, `integrationBranch`, `taskFilesDir`, `project`, `contextBranch`. Detect anything missing. `project` is the folder under `taskFilesDir` holding the ticket files — resolve it the same way **start-ticket** does. With `contextBranch` set or in local mode, task files are read from `$HUB/<taskFilesDir>` (start-ticket's **Context home** rule), not from each worktree.

### 1. Gather
```bash
git fetch origin 2>/dev/null
git worktree list
```

For each worktree under `worktreeDir` (skip the main checkout, and the integration worktree `<worktreeDir>/<integrationBranch>`, which holds no ticket):
1. **Ticket** — derive the ticket id from the branch name.
2. **Dir** — the worktree directory basename.
3. **Branch** — the checked-out branch.
4. **PR** — `gh pr list --head <branch> --state all --json number,state,title` → number + state (or "none").
5. **In integration branch?** — if `integrationBranch` is set: `git merge-base --is-ancestor "$(git rev-parse <branch>)" <integrationBranch> && echo ✓ || echo ✗`. Omit this column if no integration branch is configured.
6. **Dev server** — detect whether a dev server for this worktree's app is running (e.g. check the app's expected port via `lsof -ti`, or look for a running process in that worktree path).
7. **File status** — read `<taskFilesDir>/<project>/*/<ticket-id>-*.md` (under `$HUB` when `contextBranch` is set or local mode is on) if present and report **both axes**: `status:` (implementation — `in progress`/`complete`/`blocked`) and `ticket:` (delivery — `in-review`/`merged`/`done`). A file with no `ticket:` field is still pre-PR; show `—`.

### 2. Output
One markdown table, one row per worktree:

| Ticket | Dir | Branch | PR# | PR state | In integration | Dev server | Impl status | Delivery |
|---|---|---|---|---|---|---|---|---|

(Drop the "In integration" column if no integration branch is configured.)

Below the table, call out anything actionable:
- branches missing from the integration branch;
- **integration drift** (if `integrationBranch` is set) — commits on it that neither the base branch nor any open ticket branch carries. They come from squash-merged, closed or rewritten branches, so the integration branch no longer matches what will ship:
  ```bash
  open_refs() {   # open PR branches that exist on origin; a function, since zsh won't word-split a variable
    gh pr list --state open --author @me --json headRefName -q '.[].headRefName' |
      while read -r b; do git rev-parse -q --verify "origin/$b" >/dev/null && echo "origin/$b"; done
  }
  git rev-list --no-merges --count origin/<integrationBranch> --not origin/<baseBranch> $(open_refs)
  ```
  Non-zero ⇒ say how many and point at `/rebuild-integration`. Exclude `docs/custom-agents/` commits from the count when the integration branch is also the context branch (the old dual-role layout), since those are meant to stay;
- merged PRs whose worktrees can be removed (`git worktree remove ...`);
- port collisions (two worktrees claiming the same app);
- with `contextBranch` set or in local mode, any branch whose diff vs `origin/<baseBranch>` touches `docs/custom-agents/` — those files belong on `contextBranch` and would put the folder back on the base branch when the PR merges;
- **stale delivery status** — a task file still at `ticket: in-review` whose PR shows `MERGED`. Say so and point at `/start-of-day`, which reconciles the field and offers the worktree cleanup. This is a read-only dashboard — don't rewrite task files here.
