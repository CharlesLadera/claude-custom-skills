---
name: finish-ticket
description: Use when wrapping up a ticket — builds, runs code review, opens a PR to the base branch, optionally integrates into a personal test-bed branch, closes out the task file/tracker, and offers worktree cleanup. Works in any repo; resolves conventions from config or by detecting.
---

# Finish Ticket — Wrap Up

## Overview
Takes a completed ticket from "code done" to "PR open + integrated + closed out". Run this **inside the ticket's worktree**. Project-agnostic — resolves conventions the same way as **start-ticket**.

## Steps

### 0. Resolve project conventions
Find and read the config the way **start-ticket** step 0 does (the main checkout's copy first — this worktree may not carry `docs/custom-agents/`): `baseBranch`, `branchFrom`, `integrationBranch`, `contextBranch`, `tracker`, `taskFilesDir`, `project`, `reviewSkills`, plus a build command (`buildCommand`) if present. Detect anything missing. `project` is the folder under `taskFilesDir` holding this ticket's file — resolve it the same way **start-ticket** does. With `contextBranch` set or in local mode, apply start-ticket's **Context home** rule throughout: the task file lives under `$HUB/<taskFilesDir>`; with `contextBranch` its close-out commits to `contextBranch`, in local mode nothing is committed.

### 1. Build
Run the project's build for the affected scope (config `buildCommand`, else detect: `pnpm build` / `npm run build` / `cargo build` / etc.; in a monorepo, filter to the affected package). If the build fails, STOP and fix before continuing. Report failures with output — never claim success without the build passing.

### 2. Code review
Review the branch diff before opening the PR with `/code-review`, or with the project's own review command if it has one.

Then run the stack review skills. For each `reviewSkills` entry (start-ticket step 0), list the changed files it covers, diffing against `<branchFrom>` if set, else `<baseBranch>`:
```bash
git diff --name-only origin/<branchFrom-or-baseBranch>...HEAD -- ':(glob)<path1>' ':(glob)<path2>'
```
If the entry has `imports`, keep only the files that contain one of them (add `--diff-filter=d` so deleted files drop out; with no `paths`, leave off the pathspecs):
```bash
git diff -z --name-only --diff-filter=d origin/<branchFrom-or-baseBranch>...HEAD -- ':(glob)<path1>' \
  | xargs -0 grep -l -F -e '<import1>' -e '<import2>' --
```
Empty output ⇒ skip that skill. Otherwise load it and check only the changed lines in those files against its rules. Report a finding only where a changed line breaks a rule the skill states. A reviewer asked for gaps always finds some, and chasing rules the diff doesn't touch is how over-engineering starts.

Address findings, or consciously defer them with a note.

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
Pick where the merges run (a branch is checked out in one worktree only, so run them there):
- **`contextBranch` set** → the integration worktree, `IW=$HUB/<worktreeDir>/<integrationBranch>`. Never the hub, which stays on `contextBranch`. If `IW` doesn't exist yet, create it from the remote branch, or from `origin/<baseBranch>` when there is none: `git -C "$HUB" worktree add "$IW" <integrationBranch>` (add `-b <integrationBranch> … origin/<baseBranch>` for a new one). If `integrationBranch` equals `contextBranch` (the old dual-role layout), stop and tell the user to split them (start-ticket's **integrationBranch** key): merging tickets into the context branch can't be undone by a rebuild.
- **Otherwise** → the main checkout: `git -C "$HUB" checkout <integrationBranch>`, so `IW=$HUB`.

Then:
```bash
git -C "$IW" merge origin/<baseBranch> --no-edit
```
Then scan all worktree branches and merge any whose tip is NOT yet an ancestor of the integration branch:
```bash
git -C "$HUB" worktree list
git -C "$IW" merge-base --is-ancestor "$(git -C "$HUB" rev-parse <branch>)" HEAD && echo in || echo missing
git -C "$IW" merge <branch> --no-edit   # for each missing one
```
Push (regular, never force, during routine wrap-up): `git -C "$IW" push origin <integrationBranch>`. A rejected push means the remote was rebuilt elsewhere: stop and report, never pull or force here. Print a table: **branch → PR# → in integration branch (✓/✗)**. If no integration branch is configured, skip this entire step. Resetting the integration branch is **rebuild-integration**'s job, never this step's.

### 5. Close out the task file + tracker
`mv <taskFilesDir>/<project>/in-progress/<ticket-id>-*.md <taskFilesDir>/<project>/complete/` (under `$HUB` with `contextBranch` or in local mode — see below), then update the frontmatter and bump `synced_at:`.

Task files carry **two status axes**. Write both — `/start-of-day` reconciles against them:

| Field | Axis | Value to set here |
|---|---|---|
| `status:` | implementation — your work | `complete` (the `in-progress/` → `complete/` move mirrors it) |
| `ticket:` | delivery — the PR's journey to prod | `in-review` while the PR is open; `merged` only once it has actually merged |
| `pr:` | which PR to check later | the number captured in step 3 |

`ticket: in-review` + `pr:` are what let the next `/start-of-day` notice the PR merged, flip the file to `ticket: merged`, and offer the worktree cleanup this skill defers in step 7. Omit either one and that reconciliation silently never fires.

If a tracker is configured, set its status to match — **Done**, or **In Review** if the PR is open and someone else will merge it. In Jira, pick the matching transition by name from the issue's transitions.

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
Summarize: build status, review (which review skills ran, which were skipped for no matching path, findings fixed or deferred), PR# + URL, integration table (if applicable), task file moved (and, with `contextBranch`, the commit on it; in local mode, "local only, not committed"), artifacts fanned out, cleanup state.
