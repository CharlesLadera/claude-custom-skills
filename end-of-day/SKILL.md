---
name: end-of-day
description: Use when wrapping up a work session — captures a per-session journal keyed to the git branch (under docs/custom-agents/memory/sessions/) and lands it where the repo keeps context — on main via a tiny PR by default, or committed straight to the configured contextBranch (e.g. dev-yourname), or kept local and uncommitted when the folder is gitignored — so it's visible from any branch or worktree. Handles parallel sessions and worktrees without collisions. Each entry carries an explicit next-steps plan and a plain-words "For the daily report" block, because one later session aggregates every journal into the daily report and standup.
---

# End of Day Wrap-Up

## Overview
Ends a **session** cleanly. (1) **Lands the session journal — always, and independent of CI:** by default on `main`, via its own tiny docs-only PR (or directly on `contextBranch` when the repo sets one, or nowhere but this machine in local mode — see below), so your context lands even if the branch's checks are red or you've already signed off. `/start-of-day` then finds it from any branch or worktree. When the session's work is *complete*, it **also** merges the whole branch (auto-merge when checks pass) so the code lands too; when it's mid-work, the code stays on the branch and only the journal goes up. (2) **Captures** the session's progress into its own file `docs/custom-agents/memory/sessions/<slug>.md`. A session is a thread of work — usually one git branch — so parallel sessions each keep their own journal instead of colliding in a single dated file. Git is the source of truth, context is gathered **branch-scoped** so a parallel session's commits can't leak in, and journal landing is **worktree-safe** — it never checks out the default branch where another worktree already has it.

**The journal feeds the daily report.** The user runs `/end-of-day` in many sessions on the same day, then one separate session (yours, or a custom skill) reads **every** journal and writes the daily report and daily standup from them. So each entry must stand on its own for a reader who never saw this conversation: it must say plainly **what comes next** (🎯 Next Session Plan) and carry a ready-to-lift **🗣 For the daily report** block (Step 6). An entry without those two is incomplete.

**When the repo sets `contextBranch`** (start-ticket's config, **Context home** rule), the journal never goes near `main` or the session's branch: it is written in the main checkout and committed + pushed straight to `contextBranch` — no context PR, no cherry-pick. Everything about the *code* (commit, PR, optional merge) is unchanged, except that the code branch must never carry `docs/custom-agents/`.

**In local mode** (no `contextBranch`, folder gitignored — same rule), the journal is written in the main checkout and never committed: no journal commit, no context PR, no cherry-pick. The code flow is unchanged, except that nothing under `docs/custom-agents/` may enter the code commit.

Legacy `work-journal-YYYY-MM-DD.md` files are left untouched — `/start-of-day legacy` still reads them.

## Steps

### 0. Resolve the context home
Apply **start-ticket**'s step 0 config discovery and **Context home** rule:
```bash
HUB=$(git worktree list --porcelain | sed -n 's/^worktree //p' | head -1)   # main checkout is always first
CFG="$HUB/docs/custom-agents/ticket-workflow.config.json"
[ -f "$CFG" ] || CFG="$(git rev-parse --show-toplevel)/docs/custom-agents/ticket-workflow.config.json"
```
Read `$CFG` if it exists. If it sets `contextBranch`: `CA="$HUB/docs/custom-agents"`, `CTX_BRANCH`/`CTX_REMOTE` from the config, and verify the hub is on `CTX_BRANCH` — **STOP** if not (never switch it). Else, if the folder is gitignored **in the hub** (start-ticket's test, run against `$HUB`, not this worktree), it's **local mode**: `CA="$HUB/docs/custom-agents"`, no branch check. Otherwise `CA="$(pwd)/docs/custom-agents"` and nothing below changes from the default flow.

### 1. Resolve the session slug (hybrid: branch default, name override)
Priority order:
1. **Argument** — if invoked as `/end-of-day <slug>` (optionally followed by notes), use `<slug>`, slugified (lowercase kebab-case).
2. **Branch** — else read `git branch --show-current`. If it is **not** `main`/`master` and non-empty, derive the slug from it, dropping a leading `type/` segment: `feat/csv-export` → `csv-export`.
3. **Prompt** — else (on `main`/`master`, or detached HEAD), ask the user for a session name, suggesting a default from the most recent conversation folder or the topic just worked. Don't proceed without a name.

The user may always override the derived slug. Then resolve the path (`$CA` from step 0):
```bash
mkdir -p "$CA/memory/sessions"
echo "$CA/memory/sessions/<slug>.md"
```

### 2. Gather branch-scoped auto-context
```bash
git branch --show-current

# Resolve the repo's default branch NAME once (main or master) — reused by every
# land/PR command below so nothing hardcodes "main".
if   git show-ref --verify --quiet refs/heads/main;            then BASE_BRANCH=main
elif git show-ref --verify --quiet refs/heads/master;          then BASE_BRANCH=master
elif git show-ref --verify --quiet refs/remotes/origin/main;   then BASE_BRANCH=main
elif git show-ref --verify --quiet refs/remotes/origin/master; then BASE_BRANCH=master
else BASE_BRANCH=main; fi

# Base = where this branch diverged from the default branch — isolates THIS session's
# commits (works whether that branch is local or only a remote-tracking ref).
BASE=$(git merge-base "$BASE_BRANCH" HEAD 2>/dev/null || git merge-base "origin/$BASE_BRANCH" HEAD 2>/dev/null)

# This session's commits only (NOT date-scoped — that's what pulled in parallel work).
# Empty BASE, or BASE == HEAD (you're ON the default branch) ⇒ no branch range: fall
# back to commits since the session file's last_updated, or the last 7 days if new:
#   git log --oneline --since="<last_updated | 7 days ago>" HEAD
[ -n "$BASE" ] && git log --oneline "$BASE"..HEAD 2>/dev/null

# Working tree — in a SHARED checkout this can include files touched by a PARALLEL
# session. Attribute to this session only what you know belongs to it.
git status --short

# TODOs/FIXMEs in this session's changed files
[ -n "$BASE" ] && git diff --name-only "$BASE"..HEAD 2>/dev/null | xargs grep -ln "TODO\|FIXME" 2>/dev/null
```

Everywhere below, `<default-branch>` is this resolved default-branch name (`main` or `master`).

### 3. Show auto-context to user
> **Auto-context (session `<slug>`):**
> - Branch: `[branch]`
> - This session's commits: [list from `BASE..HEAD`, or "none"]
> - Uncommitted changes: [list from `git status --short`, or "none"]
> - TODOs found: [list or "none"]

### 4. Commit this session's work, push, open a PR — **this session only**
Open the PR now but **don't merge** — Step 7 handles landing. The journal is written next (Step 6) so it's ready for Step 7 to land on `main` (or, with `contextBranch`, Step 6 lands it itself; in local mode it is never landed or committed). **Never sweep in another session's changes.**

1. **Split the uncommitted changes** into **"this session"** vs **"looks unrelated"**. This session's files are the ones under paths it has been editing — cross-check against `git log --name-only "$BASE"..HEAD` and the topic just worked. In a shared checkout, the rest likely belongs to a parallel session. **Present the split and confirm it with the user.**
2. **Commit this session's changes:** stage only those paths and commit (confirm the message). **Do not `git add -A`** when unrelated changes are present — stage explicit paths.
3. **Unrelated changes:** do **not** commit or stash them here — that could disrupt the other session. Surface them and advise:
   > These look unrelated to this session: `[list]`. Leave them for their own session to commit on a **separate branch** — I've left them untouched.
   Only move them (`git stash push -- <paths>` → `git switch -c <other-branch>` → `git stash pop`) if the user explicitly asks.
4. **On `main`/`master` with commits to land:** there's no branch to PR or merge. Recommend creating one for the session — `git switch -c <slug>` — then continue (confirm before switching).
5. **Push:** if the branch has unpushed commits or no upstream → `git push -u origin <branch>` (confirm). **With `contextBranch` set or in local mode**, first check `git diff --name-only "$BASE"..HEAD -- docs/custom-agents` and the split from item 1: context files are never part of the code commit, and if the branch already carries some, warn the user before pushing (they belong on `contextBranch`; a PR carrying them would put the folder back on `<default-branch>`).
6. **Open (don't merge) the PR:** `gh pr list --head "<branch>" --base <default-branch>`. If none → offer to open one: `gh pr create --base <default-branch>` (confirm title/body). **Capture the PR number/URL** — the journal (Step 6) records it and the code merge (Step 7B) needs it. If the branch is already merged into `<default-branch>`, note it — the code's already on `main`, so Step 7 skips the code merge but still lands the journal.

Then re-read `git log --oneline "$BASE"..HEAD` so the journal's Auto-Context reflects the final commit list.

### 5. Gather the four answers — draft them from the session first
The four answers:
1. What did you accomplish this session?
2. What's still in progress or needs to be continued next session?
3. What do you want to tackle first next session?
4. Any important context, decisions, blockers, or links to remember?

**When the conversation already holds the answers** (the usual case: the user ran `/end-of-day` in the session that did the work, or said "append the context so far"), **draft all four from the session yourself**: commits, PRs, deploys, decisions the user made, numbers measured, what was left open. Write the entry, then show the user a short summary of it and let them correct it. Don't make them retype what the session already shows.

Ask the questions one at a time only when the session can't answer them: a fresh session with no work in it, or when the user asks to be interviewed.

**Answer 3 is the most important one.** The daily-report session reads it to say what happens next. Make it an **ordered list of concrete next actions**: the specific check, ticket/item ID, or decision, each one sentence. If something waits on the user (a click, an approval, a re-auth), say so as its own step. Never "continue work" or "keep going".

### 6. Decide how to land, then write the journal (commit & push it)
Decide two things first — they drive Step 7 and the journal's `status`:

- **Land decision** — the journal **always** lands (on `main` in Step 7, on `contextBranch` right here in Step 6, or — in local mode — nowhere but this machine, already done by writing it), CI-independent, so this is only about the **code**: ask **"Is this session's work complete — also merge the whole branch to `main` (auto-merge when checks pass), or keep the code on the branch for now?"** Merging the branch is outward-facing, so **merging = required confirm**. Choose **merge branch** when the work is done; **code stays** when mid-work — unfinished code stays off `main` while the journal still lands.
- **Status** — auto-hint with `git merge-base --is-ancestor HEAD <default-branch> 2>/dev/null && echo merged || echo open`. Propose `done` when you're merging the branch **and** the work is complete (or the branch is already merged/gone); else `active`, or `paused` if setting it down for a while. **Confirm** before setting `done`. A `done` session drops off the default `/start-of-day` list (still visible via `/start-of-day all`).

Then write the journal. Use the Read tool on `sessions/<slug>.md`.
- **New file** → create with frontmatter + today's dated entry.
- **Existing file** → **append** today's dated block after the last entry (never overwrite prior days), and refresh `status` + `last_updated`. If a block for today already exists **from this same conversation**, merge into it instead of duplicating the date. If it was written **by another session or an earlier run** (parallel sessions can share one journal), leave it untouched and append `## YYYY-MM-DD (Weekday, cont.)` (then `cont. 2`, …) with a short title naming this run's focus. Read the earlier block first and don't repeat facts it already records; reference it instead.

```markdown
---
session: <slug>
status: active            # active | paused | done
branch: <branch>
pr: <number or "">
related_conversation: <conversation folder slug or "">
created: YYYY-MM-DD
last_updated: YYYY-MM-DD
---

## YYYY-MM-DD (Weekday)

### Auto-Context
- Branch: [branch]
- Commits this session: [list from BASE..HEAD]
- PR: [#number / merged / none]
- TODOs: [list]

### ✅ Accomplished
- [bullets from answer 1]

### 🔄 In Progress / Pending
- [bullets from answer 2]

### 🎯 Next Session Plan
1. [ordered, concrete next actions from answer 3; steps waiting on the user marked as such]

### 🗣 For the daily report
- **Did:** [1–3 plain sentences: what got done that matters to the team]
- **Next:** [1 sentence: what this thread does next]
- **Blockers/asks:** [who is needed for what, or "none"]

### 📝 Notes & Context
- [bullets from answer 4]
```

**🗣 For the daily report** is what the aggregating session lifts into the daily report and standup, so write it for someone outside the work:
- **Plain words, readable aloud in ~20 seconds.** No IDs, version hashes, node names or PR numbers. Say "fixed the export skipping its last page", not "T12 / #345 / `a1b2c3d4`". Those belong in Auto-Context and Accomplished.
- **Today only:** what this run did, not the thread's whole history.
- **Numbers that matter**, rounded: "~3,500 cases (~9%)".
- **Blockers name the person and the ask** ("Alex: IP allow-list"). Say "none" when there are none; don't omit the line.
- **Consistent with the 🎯 plan:** "Next" is the plan's first one or two items in plain words.

**With `contextBranch` set** — commit **just the journal** in the main checkout, straight onto `contextBranch`, and push. This *is* the landing; Step 7A is skipped:
```bash
J=docs/custom-agents/memory/sessions/<slug>.md          # relative to $HUB
git -C "$HUB" add -f -- "$J"                            # the folder is gitignored off contextBranch
git -C "$HUB" commit -m "docs(session): <slug> <date> — end-of-day" -- "$J"   # only this path
git -C "$HUB" push "$CTX_REMOTE" "$CTX_BRANCH"          # rejected ⇒ stop and report; never pull/rebase/force here
```

**In local mode** — commit nothing. Writing the file was the landing; Step 7A is skipped. Never `git add -f` the journal.

**Otherwise** — commit **just the journal** and push — this is the commit Step 7 lands on `main` (and it rides along if you also merge the branch):
```bash
git add docs/custom-agents/memory/sessions/<slug>.md
git commit -m "docs(session): <slug> <date> — end-of-day"
git push
JOURNAL_SHA=$(git rev-parse HEAD)   # the journal commit; Step 7A lands it on main
```

### 7. Land on `main`
Two independent actions: **7A always lands the journal** on `main` (CI-independent), and **7B also merges the code branch** — but only if Step 6 chose **merge branch**. Landing the journal unconditionally is what guarantees your context reaches `main` even when the branch's checks fail or you've walked away. With `contextBranch` set, the journal already landed on it in Step 6 — only 7B can apply. Same in local mode, where it never leaves the machine.

**A — Land the journal on `main` (always — unless `contextBranch` is set, in which case Step 6 already landed it there, or local mode is on, in which case it stays local: skip 7A entirely, open no context PR).** Cherry-pick just the `JOURNAL_SHA` commit onto a throwaway branch off `origin/<default-branch>` and merge it — no code, no waiting on the branch's CI. **Worktree-safe:** it never checks out the default branch in the current tree. (If Step 6's journal commit was empty — nothing changed — there's nothing to land; skip 7A.)
```bash
git fetch origin
DEFAULT_BRANCH=<default-branch>                              # main or master, resolved in Step 2
CTX="context/<slug>-$(git rev-parse --short "$JOURNAL_SHA")" # per-journal name ⇒ never collides across days
WT="$(mktemp -d)"
git worktree add -b "$CTX" "$WT" "origin/$DEFAULT_BRANCH"
git -C "$WT" cherry-pick "$JOURNAL_SHA"
git -C "$WT" push -u origin "$CTX"
git worktree remove "$WT"
git branch -D "$CTX"                                         # worktree removal keeps the branch — drop the local copy
gh pr create --base "$DEFAULT_BRANCH" --head "$CTX" \
  --title "docs(session): <slug> context" --body "Session journal — code (if any) rides on <branch>."
gh pr merge "$CTX" --merge --delete-branch                  # docs-only: merge now. If the repo requires checks, use --auto --merge.
```
**B — Also merge the code branch (only if Step 6 chose "merge branch").** Skip if the branch is already merged (noted in Step 4, item 6). Enable auto-merge so the code lands the moment checks pass; you can sign off first.
```bash
gh pr merge <PR> --auto --merge
```
- **Auto-merge unavailable** (repo setting off / no branch protection) → gh errors. Fall back to an immediate merge **only if** the PR is clean: `gh pr checks <PR>` shows green or "no checks found" **and** the PR isn't conflicting → `gh pr merge <PR> --merge`.
- **Checks red or PR conflicts** → don't force it; tell the user. The journal already landed (7A, Step 6 with `contextBranch`, or written locally in local mode), so context is safe — the code merges later from its still-open PR.

7A already put the journal on `main`, so the branch carries an identical journal commit; whenever the branch merges (now or a later session) git reconciles it cleanly — no duplication. (With `contextBranch` or in local mode the branch carries no journal commit at all.)

**Advance local default branch (best-effort), so you see the context now:**
```bash
git fetch origin
git fetch origin <default-branch>:<default-branch> 2>/dev/null || true   # no-op when that branch is checked out in a worktree
```
If it's checked out elsewhere this does nothing — the next `/start-of-day` fetch reconciles it. An auto-merge that completes **after** you've signed off lands the same way: tomorrow's fetch has it.

### 8. Report
Report: session file path, resolved `status`, the branch's push state and PR URL, where the journal landed — the **context PR** on `main`, the commit sha pushed to `contextBranch`, or "local only, not committed" — and whether the **code** is merging (whole-branch auto-merge) or staying on the branch for later. If any changes were left untouched as unrelated, list them with the reminder to branch them separately. If `status: done`, note it drops off the default `/start-of-day` list. End with the entry's **🎯 Next Session Plan** and **🗣 For the daily report** lines quoted back, so the user can confirm what the daily-report session will pick up.
