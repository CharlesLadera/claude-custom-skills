---
name: save-conversation
description: Use when the user wants the current conversation captured for later — phrases like "save this conversation", "write this up", "archive this chat", or "/save-conversation" — or wants to update a conversation folder saved earlier. Writes a structured folder under docs/custom-agents/conversations/ with a TL;DR + detailed breakdown, metadata, references, and artifacts.
---

# Save Conversation

You are saving the current conversation to a structured folder for future reference. Follow these steps exactly.

## Base Path

All conversation folders are saved under a `docs/custom-agents/conversations/` directory. Where that directory lives depends on the repo's config — apply **start-ticket**'s config discovery and **Context home** rule:

```bash
pwd
HUB=$(git worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p' | head -1)   # main checkout is always first
CFG="$HUB/docs/custom-agents/ticket-workflow.config.json"
[ -f "$CFG" ] || CFG="$(git rev-parse --show-toplevel 2>/dev/null)/docs/custom-agents/ticket-workflow.config.json"
```

- **`$CFG` sets `contextBranch`** → the base path is `$HUB/docs/custom-agents/conversations/`, never the current worktree's. The hub must be on `contextBranch`; if it isn't, **STOP** and tell the user (never switch it). Step 8 also commits and pushes the folder to `contextBranch`.
- **Local mode** (no `contextBranch`, folder gitignored — same rule) → the base path is `$HUB/docs/custom-agents/conversations/`, no branch check, and nothing is committed.
- **Otherwise** → the base path is `<current working directory>/docs/custom-agents/conversations/`, as always, and nothing is committed.

If the `conversations/` directory does not exist yet, create it in Step 8 along with the conversation folder.

## Argument Parsing

Check if the skill was invoked with arguments (e.g., `/save-conversation update` or `/save-conversation update <folder-name>`).

### Update mode: `/save-conversation update`

If invoked with `update` and **no folder name**:

1. List existing conversation folders:
```bash
ls <base-path>/
```
2. Present the list to the user and ask which one to update:
> Here are your saved conversations:
> - `<folder-1>`
> - `<folder-2>`
> ...
>
> Which conversation would you like to update?

3. Wait for the user to confirm a folder name. Then go to **Update Flow** below.

### Update mode: `/save-conversation update <folder-name>`

If invoked with `update` and a **folder name**:

1. Verify the folder exists:
```bash
ls <base-path>/<folder-name>/ 2>/dev/null
```
2. If the folder does **not** exist, tell the user:
> No conversation folder named `<folder-name>` was found. Would you like to save it as a new conversation instead?

   Wait for their response and proceed accordingly.
3. If the folder exists, go to **Update Flow** below.

### Update Flow

Skip Steps 1 and 2. Read the existing files first:
- `<base-path>/<folder-name>/chat.md`
- `<base-path>/<folder-name>/metadata.md`
- `<base-path>/<folder-name>/references.md` (if it exists)

Then apply these update rules throughout Steps 4–9:
- **TL;DR:** Replace entirely with the current final state
- **Conversation sections:** Append new numbered sections, continuing from the last number
- **Key Decisions table:** Append new rows; do not duplicate existing rows
- **Open Items:** Replace entirely with the current state
- **Artifacts:** Preserve existing artifacts; add new ones; overwrite if same filename with newer version
- **references.md:** Append new references; do not duplicate existing rows. Create if it doesn't exist yet but new references exist
- **metadata.md:** Update `date_updated`, refresh tags and related_conversations, keep `date_created` unchanged

Jump to **Step 1** (scan only — no name suggestion needed) then continue from **Step 4**.

### No arguments

If no arguments were provided, proceed normally through Steps 1–9.

## Step 1: Scan the Conversation

Review the full current conversation. Identify:
- The main topic(s) discussed
- Key decisions made
- Any pivots where the user changed direction (watch for: "nvm", "scrap that", "forget that", "actually let's", "scratch that", "never mind", "disregard")
- Any code files, configs, or artifacts that were written or discussed with concrete code
- Unresolved questions or open items
- **References used:** Any local folders/files that were searched or read, and any MCP servers or connectors that were invoked to retrieve external data

## Step 2: Suggest a Name

> **Skip this step if in update mode** (argument parsing resolved the folder name already).

Propose a short, descriptive folder name for this conversation. Use lowercase kebab-case. For example: "building-a-login-page", "debugging-auth-middleware", "planning-api-redesign".

Present it to the user:

> **Suggested conversation name:** `<your-suggestion>`
> Want to use this name, or would you like a different one?

Wait for the user to confirm or provide an alternative. Do not proceed until you have a confirmed name.

## Step 3: Check for Existing Folder

> **Skip this step if in update mode** (the folder was already verified in argument parsing).

Check if a folder with this name already exists:

```bash
ls <base-path>/<confirmed-name>/ 2>/dev/null
```

If it exists, ask the user:

> A conversation folder named `<name>` already exists. Would you like to:
> - **A) Update it** — append new sections to chat.md and refresh metadata
> - **B) Create a new one** with a different name

If updating, read the existing `chat.md`, `metadata.md`, and `references.md` (if it exists) first, then apply the update rules from the **Update Flow** in Argument Parsing above.

If creating new, go back to Step 2 for a new name.

## Step 4: Generate chat.md

Write `chat.md` following this exact format:

```markdown
# <Conversation Title>

## TL;DR
- Bullet points summarizing final decisions, outcomes, and what was built
- Only the final state — scrapped ideas do NOT appear here
- Be specific: include tech choices, file names, key conclusions

## Conversation

### 1. <Topic/Phase Name>
**Context:** What prompted this part of the conversation
**Discussion:** What was explored and the key back-and-forth points
**Outcome:** What was decided or produced

### 2. <Topic/Phase Name>
**Context:** ...
**Discussion:** ...
**Outcome:** ...

(Continue for each distinct phase/topic of the conversation)

## Key Decisions
| Decision | Reasoning | Alternatives Considered |
|----------|-----------|------------------------|
| <decision> | <why this was chosen> | <what else was considered, including scrapped directions> |

## Open Items
- Any unresolved questions or next steps from the conversation
- If none, write "None — conversation fully resolved."
```

### Rules for chat.md:
- **Pivot handling:** When the user scrapped a direction, do NOT include it in the TL;DR or Conversation sections. Collapse it into a brief note in the "Alternatives Considered" column of Key Decisions.
- **Context preservation:** Someone reading this file should be able to fully resume the work without the original conversation. Include enough detail: specific file paths, function names, config values, reasoning behind choices.
- **No fluff:** Be concise but complete. Every sentence should carry information.
- **Code snippets:** Include short, critical code snippets inline if they are essential to understanding a decision. Do not reproduce entire files — those go in artifacts/.

## Step 5: Identify and Write Artifacts

Scan the conversation for any files that were written, created, or discussed with concrete code blocks. For each artifact:
- Recreate it from the conversation context (the version that reflects the final state, not intermediate drafts)
- Place it in the `artifacts/` subdirectory, preserving the original filename

Only create the `artifacts/` directory if there are artifacts to save. If none, skip this step entirely.

## Step 6: Generate references.md

Scan the conversation for any local paths that were searched, read, or explored, and any MCP servers or connectors that were used to retrieve external data. If there are no references of either kind, skip this step entirely.

Write `references.md` following this exact format:

```markdown
# References

## Local Paths
| Path | Type | Purpose |
|------|------|---------|
| `/path/to/folder/or/file` | `searched` / `read` / `explored` | Brief description of why this path was accessed |

## External Sources
| Source | Type | Purpose |
|--------|------|---------|
| `mcp-server-name` | MCP Server | What data was retrieved and why |
| `connector-name` | Connector | What it was used for |
```

### Rules for references.md:
- **Local Paths:** Include any directory or file that was accessed via Glob, Grep, Read, Bash (`ls`, `find`, etc.), or any file-reading tool. Group by purpose when multiple files in the same directory were accessed — use the directory path instead of listing every file.
- **External Sources:** Include any MCP server (e.g., `mcp__slack__search`, `mcp__github__search`, `mcp__jira__get_issue`) or external connector/API that was invoked during the conversation. Use the MCP server's base name (e.g., `slack`, `github`, `jira`) and describe what was retrieved.
- **Type column (Local Paths):** Use `searched` if the path was used for Glob/Grep/find, `read` if file contents were read, `explored` if the directory was listed or broadly navigated.
- Only create this file if there is at least one reference to record.

## Step 7: Generate metadata.md

Auto-suggest tags and related conversations, then present them to the user for confirmation.

First, check for existing conversations to find related ones:

```bash
ls <base-path>/
```

Then write `metadata.md` with this exact format:

```markdown
---
topic: <Human-Readable Conversation Title>
date_created: <YYYY-MM-DD>
date_updated: <YYYY-MM-DD>
status: <in-progress|completed|paused|abandoned>
tags: [<auto-suggested tags based on conversation content>]
related_conversations: [<folder names of related conversations, or empty []>]
model: <model used in this conversation>
project: <name of current project directory>
---
```

Present the suggested tags, status, and related conversations to the user:

> **Suggested metadata:**
> - **Status:** `<suggestion>`
> - **Tags:** `[<suggestions>]`
> - **Related:** `[<suggestions or none>]`
>
> Look good, or would you like to change anything?

With `contextBranch` set, add one line to that prompt — `> Saving to <contextBranch> in the main checkout — it will be committed and pushed there.` — so this one confirmation covers the write, the commit and the push.

Wait for confirmation before writing.

## Step 8: Write Everything

Create the folder and write all files:

1. Create the conversation directory and a `docs/` subfolder for manual reference files:
```bash
mkdir -p <base-path>/<name>/docs/
```

2. Write `chat.md` using the Write tool
3. Write `references.md` using the Write tool (if references exist)
4. Write `metadata.md` using the Write tool
5. If artifacts exist, create `artifacts/` and write each file using the Write tool

The `docs/` folder is reserved for manually-created reference files (specs, notes, designs, context docs). Do not auto-populate it — its contents are managed by the user.

6. **Only with `contextBranch` set:** commit the folder straight onto `contextBranch` in the main checkout and push, per start-ticket's **Context home** rule. Stage the one folder by explicit path and commit only it, so a parallel session's staged work is never swept in:
```bash
F=docs/custom-agents/conversations/<name>             # relative to $HUB
git -C "$HUB" add -f -A -- "$F"                       # the folder is gitignored off contextBranch
git -C "$HUB" commit -m "docs(conversation): <name>" -- "$F"   # on update: "docs(conversation): update <name>"
git -C "$HUB" push <contextRemote, default origin> <contextBranch>
```
If the push is rejected, stop and report — never pull, rebase or force in the shared checkout. Never open a PR for it.

## Step 9: Confirm

Check if the `docs/` folder has any manually-added files:

```bash
ls <base-path>/<name>/docs/ 2>/dev/null
```

Print a summary:

> **Conversation saved!**
> - **Location:** `<base-path>/<name>/`[ — committed to `<contextBranch>` as `<sha>`, pushed][ — local only, not committed]
> - **Files:** chat.md, metadata.md[, references.md][, artifacts/ (N files)]
> - **Docs folder:** `docs/` — place any manual reference files here (specs, notes, designs)[ — currently contains: <list files if any>]
> - **References:** N local paths, N external sources (or "none")
> - **Status:** `<status>`
> - **Tags:** `[<tags>]`
