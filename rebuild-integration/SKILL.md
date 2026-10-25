---
name: rebuild-integration
description: Use when the personal integration branch (the configured integrationBranch, e.g. dev-yourname) has drifted from what will ship — phrases like "rebuild my dev branch", "reset my integration branch", "the integration branch is stale", /rebuild-integration, or when /ticket-status reports integration drift. Resets it to the base branch, re-merges the open ticket branches the user approves, builds, and force-pushes only after approval. Never touches contextBranch or the base branch. Works in any repo; resolves conventions like start-ticket.
---

# Rebuild Integration

## Overview
The integration branch collects in-flight ticket branches for combined testing. Merging alone makes it drift from what will ship: a squash merge lands on the base branch as a new commit, so the original commits never look merged; a closed PR's branch stays merged in; a reworked branch stays in its old form. A rebuild starts the branch over from the base branch and merges back only what is still open.

Project-agnostic: conventions come from **start-ticket**'s config (its **integrationBranch** key says why the branch is disposable).

## Steps

### 0. Resolve and guard
Read the config the way **start-ticket** step 0 does (main checkout's copy first): `baseBranch`, `branchFrom`, `integrationBranch`, `contextBranch`, `worktreeDir`, `buildCommand`. Pick the integration worktree `IW` the way **finish-ticket** step 4 does: with `contextBranch` set it is `$HUB/<worktreeDir>/<integrationBranch>`, otherwise the hub with `<integrationBranch>` checked out.

STOP and tell the user when:
- `integrationBranch` is unset;
- it equals `contextBranch`, `baseBranch` or `branchFrom` — a rebuild would erase context history or a shared branch;
- `git -C "$IW" status --porcelain` prints anything — ask what to do with it, never stash or discard it;
- another session is working in `IW`; ask before touching it.

### 1. Show what only the integration branch carries
```bash
cd "$HUB" && git fetch origin
open_refs() {   # open PR branches that exist on origin; a function, since zsh won't word-split a variable
  gh pr list --state open --author @me --json headRefName -q '.[].headRefName' |
    while read -r b; do git rev-parse -q --verify "origin/$b" >/dev/null && echo "origin/$b"; done
}
git log --no-merges --format='%h %cs %an  %s' origin/<integrationBranch> --not origin/<baseBranch> $(open_refs)
```
Each listed commit disappears from the branch after the rebuild (the archive tag in step 3 keeps it reachable). Most are squash-merged or closed work. Point out any that look like work with no home elsewhere, such as a direct commit, and get the user's OK before going on.

### 2. Agree the keep-list
Candidates are the user's open PR branches plus worktree branches whose tip isn't in the base branch yet (`git merge-base --is-ancestor <tip> origin/<baseBranch>` fails) and whose PR isn't closed. Show one table — **branch → PR# → PR state → in current integration (✓/✗)** — and let the user drop or add branches.

### 3. Archive and rebuild
```bash
OLD=$(git -C "$HUB" rev-parse origin/<integrationBranch>)
TAG=archive/<integrationBranch>-$(date +%Y-%m-%d-%H%M)
git -C "$HUB" tag -a "$TAG" "$OLD" -m "<integrationBranch> before rebuild"
git -C "$IW" reset --hard origin/<baseBranch>          # step 0 proved IW clean
for b in <keep-list>; do
  git -C "$IW" merge --no-edit "origin/$b" || { git -C "$IW" merge --abort; echo "conflict: $b"; break; }
done
```
On a conflict, stop and report which branch conflicts with what is already merged. The user decides whether to drop it or resolve it. The remote is still untouched, so `git -C "$IW" reset --hard "$OLD"` restores the old branch.

### 4. Verify
Run the build in `IW` (`buildCommand`, else detect as **finish-ticket** step 1 does). A failing build stops here, before anything is pushed.

Then check for dropped database migrations:
```bash
git -C "$IW" diff --name-only "$OLD" HEAD -- ':(glob)**/migrations/**'
```
Listed files mean the local database may hold migrations the code no longer has. Say so, and name the reset command (e.g. `supabase db reset`), which wipes local data. Never run it.

### 5. Push — after approval
Show the old and new tips, the number of commits dropped, and the keep-list. On the user's go-ahead:
```bash
git -C "$IW" push origin "$TAG"
git -C "$IW" push --force-with-lease=<integrationBranch>:"$OLD" origin <integrationBranch>
```
The lease names `$OLD`, so a push made elsewhere since step 1 fails the push instead of being overwritten. If it fails, stop and report; never retry with a plain `--force`.

### 6. Report
Old → new tip, the archive tag, the branches merged, the commits dropped, the build result, and any migration warning. Other checkouts of the integration branch (another machine, another worktree) now need `git fetch && git reset --hard origin/<integrationBranch>`; say so.

## Rules
- Never rebuild `contextBranch`, `baseBranch` or `branchFrom`.
- Never discard uncommitted work, and never force-push without approval in this run.
- Never run a database reset; name it and let the user run it.
