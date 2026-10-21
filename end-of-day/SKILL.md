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

## Four requests, no more
`/end-of-day` runs when the conversation is at its largest, and **every request re-reads the whole conversation** — a tool call, a confirmation round, a re-read to verify. So run it in exactly four:

| # | Request | Covers |
|---|---|---|
| 1 | **Gather** — one Bash call: the Step 2 snippet | Steps 0–2 |
| 2 | **Confirm** — one batched question | Steps 3, 4's confirms, 5's draft, 6's decisions |
| 3 | **Land** — one Bash call: the Step 7 chain, journal included | Steps 4, 6, 7 |
| 4 | **Report** — text only, no tools | Step 8 |

- **Every "confirm" below is a question in request 2**, never its own round trip. Ask with the AskUserQuestion tool (up to 4 questions in one call; drop the ones with nothing to decide), or, without it, one message listing every decision.
- **No extra calls.** No separate `git log`/`status`, no Read of the journal (the gather prints it), no Write tool (the land chain writes the journal), no re-read to check what a command already printed.
- **Exceptions cost one request each — take one only when it happens:** no session name derivable (ask it in request 2, then the land chain creates or appends as the file dictates), a `STOP` from Step 0, a failed command in request 3 (fix it and rerun only from the failed step), auto-merge unavailable (Step 7B's fallback needs a look at the checks), or merging into a block this same conversation already wrote today (Read + Edit).
- **Run it when you stop, not after a break.** The prompt cache keeps the conversation warm for about an hour. After that the first request rewrites the whole conversation into cache — in past runs, more than half the cost of `/end-of-day` came from that one rewrite.

## Steps

### 0. Resolve the context home
Apply **start-ticket**'s step 0 config discovery and **Context home** rule — the Step 2 snippet does it. It reads `contextBranch` / `contextRemote` from the config (main checkout's copy first). If `contextBranch` is set: `CA="$HUB/docs/custom-agents"`, and the hub must be on `contextBranch` — the snippet prints **STOP** if not; stop there and tell the user (never switch it). Else, if the folder is gitignored **in the hub** (start-ticket's test, run against `$HUB`, not this worktree), it's **local mode**: `CA="$HUB/docs/custom-agents"`, no branch check. Otherwise `CA="$(pwd)/docs/custom-agents"` and nothing below changes from the default flow.

### 1. Resolve the session slug (hybrid: branch default, name override)
Priority order:
1. **Argument** — if invoked as `/end-of-day <slug>` (optionally followed by notes), use `<slug>`: set it as `SLUG=` on the snippet's first line.
2. **Branch** — else, when the branch is **not** `main`/`master` and non-empty, the snippet derives it, dropping a leading `type/` segment: `feat/csv-export` → `csv-export`.
3. **Ask** — else (on `main`/`master`, or detached HEAD) the snippet prints `slug=ASK`: ask for a session name in request 2, suggesting a default from the most recent conversation folder or the topic just worked. Don't land without a name.

The user may always override the derived slug. The journal is `$CA/memory/sessions/<slug>.md`.

### 2. Gather branch-scoped auto-context (request 1)
One Bash call. It prints everything requests 2 and 3 need, including the existing journal, so nothing is read again:
```bash
SLUG=""   # the /end-of-day <slug> argument, if one was given

# Step 0 — context home (start-ticket's Context home rule)
HUB=$(git worktree list --porcelain | sed -n 's/^worktree //p' | head -1)   # main checkout is always first
CFG="$HUB/docs/custom-agents/ticket-workflow.config.json"
[ -f "$CFG" ] || CFG="$(git rev-parse --show-toplevel)/docs/custom-agents/ticket-workflow.config.json"
CTX_BRANCH=""; CTX_REMOTE=origin
if [ -f "$CFG" ]; then
  CTX_BRANCH=$(sed -n 's/.*"contextBranch" *: *"\([^"]*\)".*/\1/p' "$CFG" | head -1)
  R=$(sed -n 's/.*"contextRemote" *: *"\([^"]*\)".*/\1/p' "$CFG" | head -1); [ -n "$R" ] && CTX_REMOTE="$R"
fi
if [ -n "$CTX_BRANCH" ]; then
  MODE=contextBranch; CA="$HUB/docs/custom-agents"
  HUB_ON=$(git -C "$HUB" branch --show-current)
  [ "$HUB_ON" = "$CTX_BRANCH" ] || echo "STOP: main checkout is on '$HUB_ON', not '$CTX_BRANCH'"
elif git -C "$HUB" check-ignore -q --no-index docs/custom-agents/; then
  MODE=local; CA="$HUB/docs/custom-agents"
else
  MODE=default; CA="$(pwd)/docs/custom-agents"
fi

# Step 1 — slug: argument, else the branch minus a leading type/ segment
BRANCH=$(git branch --show-current)
case "$BRANCH" in main|master|"") ;; *) [ -n "$SLUG" ] || SLUG=${BRANCH#*/} ;; esac
SLUG=$(printf '%s' "$SLUG" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]\{1,\}/-/g; s/^-//; s/-$//')
J="$CA/memory/sessions/$SLUG.md"

# Default branch NAME (main or master) — reused by every land/PR command
if   git show-ref --verify --quiet refs/heads/main;            then BASE_BRANCH=main
elif git show-ref --verify --quiet refs/heads/master;          then BASE_BRANCH=master
elif git show-ref --verify --quiet refs/remotes/origin/main;   then BASE_BRANCH=main
elif git show-ref --verify --quiet refs/remotes/origin/master; then BASE_BRANCH=master
else BASE_BRANCH=main; fi
# Base = where this branch diverged — isolates THIS session's commits (not date-scoped:
# that's what pulled in parallel work)
BASE=$(git merge-base "$BASE_BRANCH" HEAD 2>/dev/null || git merge-base "origin/$BASE_BRANCH" HEAD 2>/dev/null)
[ "$BASE" = "$(git rev-parse HEAD)" ] && BASE=""   # ON the default branch ⇒ no branch range

echo "== mode=$MODE hub=$HUB ca=$CA context=${CTX_BRANCH:-none}@$CTX_REMOTE"
echo "== branch=${BRANCH:-DETACHED} default=$BASE_BRANCH slug=${SLUG:-ASK}"
echo "== this session's commits"
if [ -n "$BASE" ]; then git log --oneline "$BASE"..HEAD
else                                                # no range: since last_updated, or 7 days
  SINCE="7 days ago"; [ -n "$SLUG" ] && [ -f "$J" ] && SINCE=$(sed -n 's/^last_updated: *//p' "$J" | head -1)
  git log --oneline --since="$SINCE" HEAD
fi
echo "== files those commits touched"; [ -n "$BASE" ] && git log --name-only --format= "$BASE"..HEAD | sort -u
echo "== uncommitted (a shared checkout may include a PARALLEL session's files)"; git status --short
echo "== TODO/FIXME in this session's files"
[ -n "$BASE" ] && git diff --name-only "$BASE"..HEAD | xargs grep -ln "TODO\|FIXME" 2>/dev/null
if [ "$MODE" != default ] && [ -n "$BASE" ]; then
  echo "== context files on the code branch (must be none)"; git diff --name-only "$BASE"..HEAD -- docs/custom-agents
fi
echo "== upstream"
if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then echo "unpushed: $(git rev-list --count '@{u}..HEAD')"; else echo "no upstream"; fi
echo "== PR"; [ -n "$BRANCH" ] && gh pr list --head "$BRANCH" --base "$BASE_BRANCH" --state all --json number,state,url --jq '.[] | "#\(.number) \(.state) \(.url)"'
echo "== in $BASE_BRANCH already?"; git merge-base --is-ancestor HEAD "$BASE_BRANCH" 2>/dev/null && echo merged || echo open
echo "== journal $J"
if [ -n "$SLUG" ] && [ -f "$J" ]; then
  awk 'NR == 1, (NR > 1 && /^---$/) { print; next }
       /^## / { heads = heads $0 "\n"; last = "" }
       { last = last $0 "\n" }
       END { printf "-- entries:\n%s-- last entry:\n%s", heads, last }' "$J"
else echo "(new journal)"; fi
```
Everywhere below, `<default-branch>` is `$BASE_BRANCH` from this output.

### 3. Show auto-context to user
Put it in request 2's question text, not in a message of its own:
> **Auto-context (session `<slug>`):** branch `[branch]` · this session's commits: [list or "none"] · uncommitted: [list or "none"] · TODOs: [list or "none"]

### 4. Commit this session's work, push, open a PR — **this session only**
Decide all of it in request 2; run it in request 3. Open the PR but **don't merge** — Step 7 handles landing. **Never sweep in another session's changes.**

1. **Split the uncommitted changes** into **"this session"** vs **"looks unrelated"**. This session's files are the ones under paths it has been editing — cross-check against the gather's "files those commits touched" and the topic just worked. In a shared checkout, the rest likely belongs to a parallel session. **Present the split and confirm it with the user**, together with the commit message.
2. **Commit this session's changes:** stage only those paths and commit them. **Do not `git add -A`** when unrelated changes are present — name explicit paths.
3. **Unrelated changes:** do **not** commit or stash them here — that could disrupt the other session. Say in request 2:
   > These look unrelated to this session: `[list]`. Leave them for their own session to commit on a **separate branch** — I've left them untouched.
   Only move them (`git stash push -- <paths>` → `git switch -c <other-branch>` → `git stash pop`) if the user explicitly asks.
4. **On `main`/`master` with commits to land:** there's no branch to PR or merge. Recommend creating one for the session — `git switch -c <slug>` as the chain's first line — and ask in request 2.
5. **Push** if the branch has unpushed commits or no upstream (confirm). **With `contextBranch` set or in local mode**, context files are never part of the code commit; if the gather's "context files on the code branch" lists any, warn in request 2 before pushing (they belong on `contextBranch`; a PR carrying them would put the folder back on `<default-branch>`).
6. **Open (don't merge) the PR** when the gather shows none for this branch (confirm title and body). Its number goes into the journal — the chain fills the `__PR__` placeholder once `gh pr create` returns it. If the branch is already merged into `<default-branch>`, note it — the code's already on `main`, so Step 7 skips the code merge but still lands the journal.

The journal's Auto-Context lists the gather's commits plus the one this run is about to make, written as `__CODE_COMMIT__` — the chain replaces it with that commit's sha and subject.

### 5. Gather the four answers — draft them from the session first
The four answers:
1. What did you accomplish this session?
2. What's still in progress or needs to be continued next session?
3. What do you want to tackle first next session?
4. Any important context, decisions, blockers, or links to remember?

**When the conversation already holds the answers** (the usual case: the user ran `/end-of-day` in the session that did the work, or said "append the context so far"), **draft all four from the session yourself**: commits, PRs, deploys, decisions the user made, numbers measured, what was left open. Show the draft's 🎯 plan and 🗣 lines in request 2 so the user can correct them there. Don't make them retype what the session already shows.

Ask the questions only when the session can't answer them: a fresh session with no work in it, or when the user asks to be interviewed. That costs requests — batch them into request 2 as well.

**Answer 3 is the most important one.** The daily-report session reads it to say what happens next. Make it an **ordered list of concrete next actions**: the specific check, ticket/item ID, or decision, each one sentence. If something waits on the user (a click, an approval, a re-auth), say so as its own step. Never "continue work" or "keep going".

### 6. Decide how to land, then write the journal
Decide two things in request 2 — they drive Step 7 and the journal's `status`:

- **Land decision** — the journal **always** lands (on `main` in Step 7A, on `contextBranch` in Step 7, or — in local mode — nowhere but this machine, done by writing it), CI-independent, so this is only about the **code**: ask **"Is this session's work complete — also merge the whole branch to `main` (auto-merge when checks pass), or keep the code on the branch for now?"** Merging the branch is outward-facing, so **merging = required confirm**. Choose **merge branch** when the work is done; **code stays** when mid-work — unfinished code stays off `main` while the journal still lands.
- **Status** — hint from the gather's "in `<default-branch>` already?". Propose `done` when you're merging the branch **and** the work is complete (or the branch is already merged/gone); else `active`, or `paused` if setting it down for a while. **Confirm** before setting `done`. A `done` session drops off the default `/start-of-day` list (still visible via `/start-of-day all`).

Then the journal, written by the land chain (request 3) from the gather's printout:
- **New file** → frontmatter + today's dated entry.
- **Existing file** → **append** today's dated block after the last entry (never overwrite prior days); the chain refreshes `status`, `last_updated` and `pr`. If the gather shows a block for today **written by another session or an earlier run** (parallel sessions can share one journal), leave it untouched and append `## YYYY-MM-DD (Weekday, cont.)` (then `cont. 2`, …) with a short title naming this run's focus. Don't repeat facts it already records; reference it instead. If the block was written **by this same conversation**, merge into it instead of duplicating the date — the one case that needs Read + Edit.

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

### 7. Land (request 3)
One Bash call runs the whole chain: code commit → push → PR → journal written → journal committed and landed → code merge. Fill the values from the gather and the answers; leave a step's value empty to skip it. On failure the chain stops and names the step — fix that and rerun from there, never from the top (the earlier steps already happened).

Where the journal lands depends on the mode:
- **default** — **7A always lands the journal on `main`**, CI-independent: the journal is committed on the branch, then that one commit is cherry-picked onto a throwaway branch off `origin/<default-branch>` and merged as a docs-only PR — no code, no waiting on the branch's CI. **Worktree-safe:** it never checks out the default branch in the current tree.
- **`contextBranch`** — the journal is committed in the main checkout straight onto `contextBranch` and pushed; that *is* the landing, so 7A is skipped and no context PR is opened. A rejected push stops the chain: report it — never pull, rebase or force in the shared checkout.
- **local** — nothing is committed; writing the file was the landing. 7A is skipped, no context PR. Never `git add -f` the journal.

**7B — also merge the code branch**, only if Step 6 chose **merge branch** and the branch isn't already merged: auto-merge, so the code lands the moment checks pass and you can sign off first.

```bash
# ── Values from request 1 (gather) and request 2 (answers) ──
MODE=<default|contextBranch|local>; HUB=<hub>; CA=<ca>; CTX_BRANCH=<contextBranch or "">; CTX_REMOTE=<remote>
BRANCH=<branch>; BASE_BRANCH=<main|master>; SLUG=<slug>; TODAY=$(date +%F)
J="$CA/memory/sessions/$SLUG.md"
CODE_PATHS=(<this session's paths>)          # () when there is nothing to commit
CODE_MSG='<confirmed commit message>'         # '' = no code commit
PUSH=<yes|no>
PR=<existing PR number or "">
PR_TITLE='<confirmed title>'                  # '' = don't open one
PR_BODY='<confirmed body>'
MERGE=<yes|no>                                # Step 6's land decision
STATUS=<active|paused|done>
fail() { echo "FAILED at: $1 — fix it, then rerun from that step"; exit 1; }

# 4 — code commit, push, PR
if [ -n "$CODE_MSG" ]; then
  git add -- "${CODE_PATHS[@]}" && git commit -q -m "$CODE_MSG" -- "${CODE_PATHS[@]}" || fail "code commit"
  CODE_COMMIT=$(git log -1 --format='%h %s')
fi
if [ "$PUSH" = yes ]; then git push -q -u origin "$BRANCH" || fail "push"; fi
if [ -z "$PR" ] && [ -n "$PR_TITLE" ]; then
  URL=$(gh pr create --base "$BASE_BRANCH" --head "$BRANCH" --title "$PR_TITLE" --body "$PR_BODY") || fail "gh pr create"
  PR=${URL##*/}; echo "PR: $URL"
fi

# 6 — journal, appended (or created). New file: frontmatter + entry. Existing: the entry only.
mkdir -p "$CA/memory/sessions"
cat >> "$J" <<'JOURNAL'
<the entry, in Step 6's format — `__PR__` and `__CODE_COMMIT__` where the PR number and new commit go>
JOURNAL
STATUS="$STATUS" TODAY="$TODAY" PR="$PR" CODE_COMMIT="${CODE_COMMIT:-}" perl -pi -e '
  $fm++ if /^---\s*$/;
  if ($fm == 1) {                       # frontmatter only
    s/^status:.*/status: $ENV{STATUS}/;
    s/^last_updated:.*/last_updated: $ENV{TODAY}/;
    s/^pr:.*/pr: $ENV{PR}/ if length $ENV{PR};
  }
  s/__PR__/$ENV{PR}/g; s/__CODE_COMMIT__/$ENV{CODE_COMMIT}/g;' "$J" || fail "journal refresh"

# 6/7 — land the journal
case "$MODE" in
  contextBranch)
    JR="docs/custom-agents/memory/sessions/$SLUG.md"   # relative to $HUB
    git -C "$HUB" add -f -- "$JR" \
      && git -C "$HUB" commit -q -m "docs(session): $SLUG $TODAY — end-of-day" -- "$JR" \
      && git -C "$HUB" push -q "$CTX_REMOTE" "$CTX_BRANCH" || fail "journal to $CTX_BRANCH (rejected push: stop, never pull/rebase/force)"
    echo "journal: $(git -C "$HUB" rev-parse --short HEAD) on $CTX_BRANCH" ;;
  local)
    echo "journal: local only, not committed" ;;
  default)
    git add -- "$J" && git commit -q -m "docs(session): $SLUG $TODAY — end-of-day" -- "$J" || fail "journal commit"
    JOURNAL_SHA=$(git rev-parse HEAD)
    [ "$PUSH" = yes ] && { git push -q || fail "journal push"; }
    # 7A — land just the journal commit on the default branch
    git fetch -q origin || fail "7A fetch"
    CTX="context/$SLUG-$(git rev-parse --short "$JOURNAL_SHA")"   # per-journal name ⇒ never collides across days
    WT="$(mktemp -d)"
    git worktree add -q -b "$CTX" "$WT" "origin/$BASE_BRANCH" \
      && git -C "$WT" cherry-pick "$JOURNAL_SHA" >/dev/null \
      && git -C "$WT" push -q -u origin "$CTX"
    OK=$?
    git worktree remove --force "$WT" 2>/dev/null; git branch -q -D "$CTX" 2>/dev/null   # removal keeps the branch — drop the local copy
    [ "$OK" = 0 ] || fail "7A cherry-pick/push of $CTX (a conflict means an earlier context PR for this journal hasn't merged yet — merge it, then rerun 7A)"
    gh pr create --base "$BASE_BRANCH" --head "$CTX" \
      --title "docs(session): $SLUG context" --body "Session journal — code (if any) rides on $BRANCH." \
      && gh pr merge "$CTX" --merge --delete-branch || fail "7A context PR (if the repo requires checks: gh pr merge $CTX --auto --merge)"
    echo "journal: landed on $BASE_BRANCH via $CTX" ;;
esac

# 7B — also merge the code branch (only when Step 6 chose "merge branch")
if [ "$MERGE" = yes ] && [ -n "$PR" ]; then
  gh pr merge "$PR" --auto --merge || { gh pr checks "$PR"; echo "AUTO-MERGE UNAVAILABLE — see 7B"; }
fi

# Advance the local default branch (best-effort), so you see the context now
git fetch -q origin; git fetch -q origin "$BASE_BRANCH:$BASE_BRANCH" 2>/dev/null || true
```
- **Auto-merge unavailable** (repo setting off / no branch protection) → the chain prints the PR's checks. Merge now **only if** they show green or "no checks found" **and** the PR isn't conflicting → `gh pr merge <PR> --merge`. That one extra request is allowed.
- **Checks red or PR conflicts** → don't force it; tell the user. The journal already landed (7A, `contextBranch`, or written locally), so context is safe — the code merges later from its still-open PR.

7A already put the journal on `main`, so the branch carries an identical journal commit; whenever the branch merges (now or a later session) git reconciles it cleanly — no duplication. (With `contextBranch` or in local mode the branch carries no journal commit at all.) If the local default branch is checked out elsewhere, the last line does nothing — the next `/start-of-day` fetch reconciles it, and an auto-merge that completes after you've signed off lands the same way.

### 8. Report (request 4 — text only)
From the chain's output, no further calls. Report: session file path, resolved `status`, the branch's push state and PR URL, where the journal landed — the **context PR** on `main`, the commit sha pushed to `contextBranch`, or "local only, not committed" — and whether the **code** is merging (whole-branch auto-merge) or staying on the branch for later. If any changes were left untouched as unrelated, list them with the reminder to branch them separately. If `status: done`, note it drops off the default `/start-of-day` list. End with the entry's **🎯 Next Session Plan** and **🗣 For the daily report** lines quoted back, so the user can confirm what the daily-report session will pick up.
