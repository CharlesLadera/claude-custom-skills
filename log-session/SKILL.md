---
name: log-session
description: Use when a supplementary work session (planning, research, a side task) is worth capturing mid-day without running the full end-of-day wrap-up. Appends a timestamped entry to the per-session journal that start-of-day reads.
---

# Log Session

## Overview
Lightweight mid-session capture — auto-gathers git context, asks 2 questions, appends a timestamped entry to the **same per-session journal `/end-of-day` writes and `/start-of-day` reads**: `docs/custom-agents/memory/sessions/<slug>.md`. No PR, no landing on `main`, no status decision — those belong to `/end-of-day`.

## Steps

### 1. Resolve the session slug
Same rules as **end-of-day**, in this order:
1. **Argument** — `/log-session <slug>` (optionally followed by notes) → use `<slug>`, slugified (lowercase kebab-case).
2. **Branch** — else `git branch --show-current`. If it is not `main`/`master` and non-empty, derive the slug from it, dropping a leading `type/` segment: `feat/csv-export` → `csv-export`.
3. **Prompt** — else ask for a session name, suggesting the topic just worked. Don't proceed without a name.

Resolve where the journal lives the way **end-of-day** step 0 does (start-ticket's **Context home** rule): with `contextBranch` set, `CA="$HUB/docs/custom-agents"` and the hub must be on `contextBranch` (**STOP** if not); in local mode (folder gitignored in the hub), `CA="$HUB/docs/custom-agents"` with no branch check; otherwise `CA="$(pwd)/docs/custom-agents"`.
```bash
mkdir -p "$CA/memory/sessions"
echo "$CA/memory/sessions/<slug>.md"
```

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
1. "What was this session about / what did you accomplish?"
2. "Any notes to remember?"

### 5. Append the entry
Use the Read tool on `sessions/<slug>.md`, then:

- **File missing** → create it with end-of-day's frontmatter (`session`, `status: active`, `branch`, `pr: ""`, `related_conversation: ""`, `created`, `last_updated`), followed by today's dated block containing just this entry.
- **Today's `## YYYY-MM-DD (Weekday)` block exists** → append the entry at the end of the last one for today (end-of-day may have added `(cont.)` blocks).
- **File exists but has no block for today** → add a new dated block after the last one. Never overwrite a prior day.

Always refresh `last_updated:`. Leave `status:` alone — only `/end-of-day` decides that.

Entry format:
```markdown
#### + Side session [HH:MMam/pm]
**About:** [answer 1]
**Notes:** [answer 2]
- Branch: [branch]
- Commits: [list]
```

With `contextBranch` set, commit and push just this journal to it right away — it is a direct commit on the context branch, not a PR or a landing on `main`, and the folder is gitignored there, so an uncommitted journal would sit invisible to `git status`:
```bash
J=docs/custom-agents/memory/sessions/<slug>.md
git -C "$HUB" add -f -- "$J" && git -C "$HUB" commit -m "docs(session): <slug> <date> — log-session" -- "$J" && git -C "$HUB" push "$CTX_REMOTE" "$CTX_BRANCH"
```
Without `contextBranch`, don't commit — leave it for `/end-of-day`, as before. In local mode nothing is ever committed, by this skill or by `/end-of-day`.

### 6. Confirm
Tell the user the entry was saved, show the file path (and the `contextBranch` commit, if one was made; in local mode, "local only, not committed"), and note that `/end-of-day` will fold it into the day's wrap-up.

## Legacy
Older entries live in dated `$CA/memory/work-journal-YYYY-MM-DD.md` files. Leave those untouched — `/start-of-day legacy` still reads them.
