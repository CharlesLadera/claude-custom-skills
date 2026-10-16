---
name: finish-ticket
description: Use when wrapping up a ticket — builds, runs code review, opens a PR to the base branch, optionally integrates into a personal test-bed branch, closes out the task file/tracker, and offers worktree cleanup. Works in any repo; resolves conventions from config or by detecting.
---

# Finish Ticket — Wrap Up

## Overview
Takes a completed ticket from "code done" to "PR open + integrated + closed out". Run this **inside the ticket's worktree**. Project-agnostic — resolves conventions the same way as **start-ticket**.

## Steps

### 0. Resolve project conventions
Find and read the config the way **start-ticket** step 0 does (the main checkout's copy first — this worktree may not carry `docs/custom-agents/`): `baseBranch`, `branchFrom`, `integrationBranch`, `contextBranch`, `tracker`, `taskFilesDir`, `project`, plus a build command (`buildCommand`) if present. Detect anything missing. `project` is the folder under `taskFilesDir` holding this ticket's file — resolve it the same way **start-ticket** does. With `contextBranch` set or in local mode, apply start-ticket's **Context home** rule throughout: the task file lives under `$HUB/<taskFilesDir>`; with `contextBranch` its close-out commits to `contextBranch`, in local mode nothing is committed.

### 1. Build
Run the project's build for the affected scope (config `buildCommand`, else detect: `pnpm build` / `npm run build` / `cargo build` / etc.; in a monorepo, filter to the affected package). If the build fails, STOP and fix before continuing. Report failures with output — never claim success without the build passing.

### 2. Code review
Review the branch diff before opening the PR — use a code-review skill if one is available (e.g. `superpowers:requesting-code-review`) or run the project's review command. Address findings, or consciously defer them with a note.

### 3. Open the PR to the base branch
Commit any remaining work first (only if the user asked to commit). With `contextBranch` set or in local mode, first make sure the branch carries no context files — `git diff --name-only origin/<baseBranch>...HEAD -- docs/custom-agents` must print nothing; if it lists files, stop and tell the user (they belong on `contextBranch`, and merging this PR would put the folder back on `<baseBranch>`). With `branchFrom` set (and different from `baseBranch`), also check that `git log --oneline origin/<branchFrom>..HEAD` lists only this ticket's commits. If it shows commits from `<baseBranch>`, someone merged the base in: stop and tell the user. The PR may show conflicts against `<baseBranch>`; never merge the base in to clear them. Then:
```bash
git push -u origin <branch-name>
BODY="$(mktemp)"   # write <summary + acceptance criteria + screenshots> into it with the Write tool
gh pr create --base <baseBranch> --head <branch-name> --title '<type(scope): summary> (<ticket-id>)' --body-file "$BODY"
```
Tracker text often holds backticks or `$(…)`, which a double-quoted shell string would run. So the body goes through `--body-file`, never inline. The title goes in single quotes; if it contains a `'`, write it as `'\''`.
Capture the PR number.

### 4. Integrate into the personal test-bed branch (only if `integrationBranch` is set)
From the main checkout (a branch lives in one worktree, so switch there):
```bash
git checkout <integrationBranch>
git merge origin/<baseBranch> --no-edit
```
Then scan all worktree branches and merge any whose tip is NOT yet an ancestor of the integration branch:
```bash
git worktree list
git merge-base --is-ancestor "$(git rev-parse <branch>)" <integrationBranch> && echo in || echo missing
git merge <branch> --no-edit   # for each missing one
```
Push (regular, never force, during routine wrap-up): `git push origin <integrationBranch>`. Print a table: **branch → PR# → in integration branch (✓/✗)**. If no integration branch is configured, skip this entire step.

With `contextBranch` set, the main checkout must stay on `contextBranch`: skip the `git checkout` when `integrationBranch` equals `contextBranch` (run the merges with `git -C "$HUB" …`), and skip this step with a note when it names a different branch. Never switch the hub.

### 5. Close out the task file + tracker
`mv <taskFilesDir>/<project>/in-progress/<ticket-id>-*.md <taskFilesDir>/<project>/complete/` (under `$HUB` with `contextBranch` or in local mode — see below), then update the frontmatter and bump `synced_at:`.

Task files carry **two status axes**. Write both — `/start-of-day` reconciles against them:

| Field | Axis | Value to set here |
|---|---|---|
| `status:` | implementation — your work | `complete` (the `in-progress/` → `complete/` move mirrors it) |
| `ticket:` | delivery — the PR's journey to prod | `in-review` while the PR is open; `merged` only once it has actually merged |
| `pr:` | which PR to check later | the number captured in step 3 |

`ticket: in-review` + `pr:` are what let the next `/start-of-day` notice the PR merged, flip the file to `ticket: merged`, and offer the worktree cleanup this skill defers in step 7. Omit either one and that reconciliation silently never fires.

If a tracker is configured, set its status to match — **Done**, or **In Review** if the PR is open and someone else will merge it.

**With `contextBranch` set**, do the move and frontmatter edits in `$HUB/<taskFilesDir>` (not this worktree), then commit both paths to `contextBranch` per start-ticket's **Context home** rule and push:
```bash
OLD=<taskFilesDir>/<project>/in-progress/<ticket-id>-<slug>.md
NEW=<taskFilesDir>/<project>/complete/<ticket-id>-<slug>.md
git -C "$HUB" add -f -A -- "$OLD" "$NEW"
git -C "$HUB" commit -m "docs(task): <ticket-id> complete — PR #<n>" -- "$OLD" "$NEW"
git -C "$HUB" push "$CTX_REMOTE" "$CTX_BRANCH"
```
The file only ever lived in the hub, so the sync below does not apply — skip it.

**In local mode** (no `contextBranch`, folder gitignored — same rule), do the same move and edits in `$HUB/<taskFilesDir>` and commit nothing. The file only ever lived in the hub here too, so skip the sync below.

**Otherwise — sync the closed-out file to the main checkout (hub).** When `<taskFilesDir>` is **gitignored** but `docs/custom-agents/` itself is not, so local mode didn't apply (e.g. only `tasks/` is ignored; confirm with `git -C "$HUB" check-ignore <taskFilesDir>`), finish-ticket runs inside the worktree, but that copy vanishes when the worktree is removed (step 7), leaving the hub's copy stale — so a later `/ticket-status` or `/start-ticket` run from the hub reads the wrong `status:` and folder. Mirror the closed-out file into the main checkout and drop the stale hub copy. The guard skips the block unless the completed file is actually in the worktree (i.e. the `mv` above ran here, not against a stub that only ever lived in the hub):
```bash
WT=$(git rev-parse --show-toplevel)
HUB=$(git worktree list --porcelain | sed -n 's/^worktree //p' | head -1)   # main checkout is always first
if [ "$HUB" != "$WT" ] && ls "$WT/<taskFilesDir>/<project>/complete/<ticket-id>-"*.md >/dev/null 2>&1; then
  mkdir -p "$HUB/<taskFilesDir>/<project>/complete"
  cp "$WT/<taskFilesDir>/<project>/complete/<ticket-id>-"*.md "$HUB/<taskFilesDir>/<project>/complete/"
  rm -f "$HUB/<taskFilesDir>/<project>/in-progress/<ticket-id>-"*.md   # remove the now-stale hub copy
fi
```
If task files are **tracked** by git instead, skip this — the PR/merge already carries the move.

### 6. Artifacts fan-out (optional)
If QA captures (screenshots, recordings) were produced for this ticket, place them in a per-ticket folder under the app's docs and commit them on THIS feature branch — so the PR shows its own proof — rather than lumping them into an integration-branch-only commit.

### 7. Offer worktree cleanup
Worktree removal should happen only AFTER the PR merges, and never with uncommitted/unpushed work. Offer:
```bash
git worktree remove <worktreeDir>/<slug>
```
If the PR isn't merged yet, just note it for later cleanup.

### 8. Report
Summarize: build status, PR# + URL, integration table (if applicable), task file moved (and, with `contextBranch`, the commit on it; in local mode, "local only, not committed"), artifacts fanned out, cleanup state.
