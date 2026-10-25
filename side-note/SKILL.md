---
name: side-note
description: Use when a side task, piece of research, or decision is worth a quick note in a session journal without the full end-of-day wrap-up — phrases like "note this in the <slug> journal", "jot this down", or /side-note. Appends a timestamped entry to the per-session journal that start-of-day reads, and lands it where end-of-day lands journals (on main via a docs-only PR by default, on contextBranch, or local only).
---

# Side Note

## Overview
A quick note in the middle of a thread — auto-gathers git context, asks 2 questions, appends a timestamped entry to the **same per-session journal `/end-of-day` writes and `/start-of-day` reads**: `docs/custom-agents/memory/sessions/<slug>.md`. It lands the note the way `/end-of-day` lands a journal, so the note survives the next wrap-up. No code commit, no code PR, no status decision — those belong to `/end-of-day`.

## Steps

### 1. Resolve the session slug
Same rules as **end-of-day**, in this order:
1. **Argument** — `/side-note <slug>` (optionally followed by notes) → use `<slug>`, slugified (lowercase kebab-case).
2. **Branch** — else `git branch --show-current`. If it is not `main`/`master` and non-empty, derive the slug from it, dropping a leading `type/` segment: `feat/csv-export` → `csv-export`.
3. **Prompt** — else ask for a session name, suggesting the topic just worked. Don't proceed without a name.

The note may belong to a different thread than the current branch (a decision made here about another session). Then the argument names that thread.

Resolve where the journal lives the way **end-of-day** step 0 does (start-ticket's **Context home** rule): with `contextBranch` set, `CA="$HUB/docs/custom-agents"` and the hub must be on `contextBranch` (**STOP** if not); in local mode (folder gitignored in the hub), `CA="$HUB/docs/custom-agents"` with no branch check; otherwise `CA="$(pwd)/docs/custom-agents"` — **default mode**.

### 2. Gather auto-context
```bash
git branch --show-current
git log --oneline -5 HEAD
git status --short
git diff --name-only HEAD~5 HEAD 2>/dev/null | xargs grep -ln "TODO\|FIXME" 2>/dev/null
date +"%I:%M%p"
```
This is a quick snapshot, deliberately **not** branch-scoped the way end-of-day's is — it's a side note, not the session's full commit range.

### 3. Show auto-context to user
> **Auto-context gathered:**
> - Branch: `[branch]`
> - Recent commits: [list]
> - Changed files: [list]
> - TODOs found: [list or "none"]

### 4. Ask 2 questions
1. "What was this about / what did you decide or find?"
2. "Any notes to remember?"

When the conversation already holds the answers (the usual case), draft both and confirm them in one question instead.

### 5. Append the entry
**Pick the file to write.** In default mode **on the default branch**, the copy that matters is `origin`'s — `/end-of-day` run there appends to `origin`'s copy, so a note left only in the local checkout would be dropped. Write it in a throwaway worktree instead:
```bash
git fetch -q origin
CTX="context/<slug>-$(date +%Y%m%d-%H%M%S)"; WT="$(mktemp -d)"
git worktree add -q -b "$CTX" "$WT" "origin/<default-branch>"
JW="$WT/docs/custom-agents/memory/sessions/<slug>.md"
```
Everywhere else (a feature branch, `contextBranch`, local mode), `JW="$CA/memory/sessions/<slug>.md"`.

Use the Read tool on `$JW`, then:

- **File missing** → create it with end-of-day's frontmatter (`session`, `status: active`, `branch`, `pr: ""`, `related_conversation: ""`, `created`, `last_updated`), followed by today's dated block containing just this entry.
- **Today's `## YYYY-MM-DD (Weekday)` block exists** → append the entry at the end of the last one for today (end-of-day may have added `(cont.)` blocks).
- **File exists but has no block for today** → add a new dated block after the last one. Never overwrite a prior day.

Always refresh `last_updated:`. Leave `status:` alone — only `/end-of-day` decides that.

Entry format (the heading stays `Side session`: `/start-of-day` and older journals use it):
```markdown
#### + Side session [HH:MMam/pm]
**About:** [answer 1]
**Notes:** [answer 2]
- Branch: [branch]
- Commits: [list]
```

A side-note entry has no 🎯 plan or 🗣 lines. If the note changes what comes next, say so in **Notes**, so the next `/end-of-day` on that thread copies it into its plan.

### 6. Land it
The same landing **end-of-day** Step 7 does for its journal; if the two ever differ, end-of-day is the owner.

- **`contextBranch`** — commit and push just this journal to it right away. It is a direct commit on the context branch, not a PR, and the folder is gitignored there, so an uncommitted journal would sit invisible to `git status`:
  ```bash
  J=docs/custom-agents/memory/sessions/<slug>.md
  git -C "$HUB" add -f -- "$J" && git -C "$HUB" commit -m "docs(session): <slug> <date> — side-note" -- "$J" && git -C "$HUB" push "$CTX_REMOTE" "$CTX_BRANCH"
  ```
  A rejected push: stop and report — never pull, rebase or force in the shared checkout.
- **Local mode** — nothing is committed; writing the file was the landing. Never `git add -f` the journal.
- **Default mode** — land just the journal on the default branch through a docs-only PR (end-of-day's 7A). On a feature branch the note is committed on the branch too, so the branch's journal stays identical to `main`'s and the next `/end-of-day` cherry-pick applies cleanly. The feature branch itself is not pushed here — `/end-of-day` does that.
  ```bash
  J=docs/custom-agents/memory/sessions/<slug>.md; MSG="docs(session): <slug> <date> — side-note"
  WT="<step 5's worktree, or empty on a feature branch>"; CTX="<step 5's branch, or empty>"   # shell state doesn't carry between calls
  if [ -z "$WT" ]; then                       # feature branch: commit here, then cherry-pick
    git add -- "$J" && git commit -q -m "$MSG" -- "$J" || exit 1
    SHA=$(git rev-parse HEAD)
    git fetch -q origin
    CTX="context/<slug>-$(git rev-parse --short "$SHA")"; WT="$(mktemp -d)"
    git worktree add -q -b "$CTX" "$WT" "origin/<default-branch>" && git -C "$WT" cherry-pick "$SHA" >/dev/null
  else                                        # default branch: the entry exists only in the worktree
    git -C "$WT" add -- "$J" && git -C "$WT" commit -q -m "$MSG" -- "$J"
  fi && git -C "$WT" push -q -u origin "$CTX"
  OK=$?
  git worktree remove --force "$WT"; git branch -q -D "$CTX" 2>/dev/null
  [ "$OK" = 0 ] || { echo "FAILED: commit/push of $CTX (a cherry-pick conflict means the branch's journal is behind main's — stop and report)"; exit 1; }
  gh pr create --base <default-branch> --head "$CTX" --title "docs(session): <slug> context" --body "Side note for the <slug> session journal." \
    && gh pr merge "$CTX" --merge --delete-branch || echo "FAILED: context PR (if the repo requires checks: gh pr merge $CTX --auto --merge)"
  git fetch -q origin
  [ "$(git branch --show-current)" = "<default-branch>" ] && git merge -q --ff-only "origin/<default-branch>"
  ```

### 7. Confirm
Tell the user the entry was saved, show the file path and where it landed: the context PR on `main`, the `contextBranch` commit, or "local only, not committed". If the note changes the thread's next steps, remind them the next `/end-of-day` on that thread should copy it into its 🎯 plan.

## Legacy
Older entries live in dated `$CA/memory/work-journal-YYYY-MM-DD.md` files. Leave those untouched — `/start-of-day legacy` still reads them.
