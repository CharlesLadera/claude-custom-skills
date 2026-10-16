---
name: disk-cleanup
description: Use when the user wants to free up disk space or find what is eating storage — phrases like "clean up my disk", "running out of space", "what is using all my storage", "clear caches", "find big files". Detects the host OS, scans every readable volume, and presents one table ranked biggest-to-smallest covering directories, large individual files, and applications. Removes nothing without explicit per-item approval. Do NOT use for cleaning up code (use a code-cleanup tool) or for freeing context/token budget (see context-audit).
---

# Disk Cleanup

## Overview

Scans every readable volume, ranks every consumer of storage in one table, and reclaims
space only from vetted targets the user approves item by item.

The scan is wide; the proposal set is narrow. `scan.py` measures everything readable —
including paths that must never be deleted — so the user sees the full picture. Only two
kinds of row are ever proposed: the 18 entries in the macOS manifest, and a project
artifact (`node_modules`, `target`, `.venv`, `venv`) that passes the ownership test in
Notes below. Everything else renders `review only`.

## The gate

This is a contract, not a guideline:

- **Nothing is deleted without the user's explicit approval in this session, naming the
  items being removed.** A grouped approval covers only the list just shown — it is not
  blanket permission and never carries into another run.
- `scripts/scan.py` and `scripts/classify.py` contain no deletion code, by construction.
  `tests/test_safety.py` scans every file under `scripts/` for delete/move/rename
  primitives and fails the suite if one appears. Deletion happens only in step 5 below,
  only for items just approved.
- Removal defaults to `/usr/bin/trash`. A manifest entry's own `command` runs instead of
  trash where trashing would relocate bytes rather than free them (`cheap`-tier package
  caches: `npm cache clean --force`, `pnpm store prune`, `brew cleanup`) or where there is
  no path to trash at all (`docker-images`, whose manifest `path` is the placeholder
  `__command_only__docker` — `classify.build_rows` gives it a readable row and a real size
  instead, from `scan.py`'s `docker_pass()`). A `cheap`-tier entry with no `command` is
  reclaimed with permanent `rm` instead of trash, for the same reason — trashing a
  regenerable package-manager cache only relocates the bytes, it does not free them; step
  5 says so before the user approves it. Every manifest command is itself gated by
  `test_safety.py`: plain argv, a fixed tool allowlist, no destructive words.
- **Never escalate with sudo.** Report a privileged region as unmeasured
  (`unreadable_count`) and hand the user the command to run themselves — never run it for
  them.

## Steps

### 1. Scan

```bash
python3 ~/.claude/skills/disk-cleanup/scripts/scan.py --json > "${TMPDIR:-/tmp}/disk-scan.json"
```

Roots come from `choose_roots()` filtering `df -h`, not a hardcoded list — on a
typical Mac that resolves to `/System/Volumes/Data` (the volume `~` and `/Applications`
firmlink onto) plus any mounted external volume. Default depth is 6: from a volume root rather
than `$HOME`, `~/Library/Caches/*` sits 5 levels down and `~/.cache/*` 4 — depth 3 cannot
reach either.

A cold scan can take several minutes (one measured run: ~8.5 minutes at depth 6 across two
roots). Tell the user before starting. Results cache for 24h, and a cached payload
missing a field the current schema expects is treated as stale automatically. `--refresh`
forces a fresh walk.

### 2. Classify and present

```bash
python3 ~/.claude/skills/disk-cleanup/scripts/classify.py "${TMPDIR:-/tmp}/disk-scan.json"
```

One command produces the whole report — not a manual join. It opens with a `scanned:`
header naming when the data is from (the oldest root, if the two were cached at different
times) and a plain `STALE - rerun with --refresh` line when that is over 24h old — a
days-old JSON must never render as though it were current.

Below that is the ranked directories+files table (paths canonicalised back to the
firmlink form, `/Users/...`, so the table shows what the user recognises, never the
`/System/Volumes/Data/...` mount form). Every `proposed`/`ask` row is in it, always,
plus the biggest 25 `review only` rows — there can be tens of thousands of those, kept
only for orientation, never for decision, so the table ends with a line naming how many
were omitted and their total size instead of drowning the actionable rows in them.
`%ROOT` is each row's share of **its own** scan root, not a sum across both roots — a
root's own self-row correctly reads 100%. A `METHOD` column names exactly how step 5
would reclaim that row's bytes (`trash`, `rm`, a manifest command, or `empty Trash
yourself`); step 5 reads this column and nothing else.

The report continues with an `APPLICATIONS` section sorted by size with `LAST USED` in
place of `AGE` (a null `last_used` renders `unknown`, never "unused" — `mdls` cannot tell
never-launched from not-yet-indexed; always `review only`, since removing an app is
uninstalling software, not clearing junk); the regeneration cost for every `ask` row,
biggest first (step 4 reads this, not the manifest — this is also where a qualifying
project artifact's own regeneration note lives); and a footer with:

- `reclaimable` and `review only` totals — labelled as overlapping, e.g. `review only:
  281G (includes the reclaimable bytes above, which sit inside larger review-only
  directories)`, because every reclaimable row sits inside some larger review-only
  ancestor and the two numbers answer different questions, not additive ones.
- `unreadable (need sudo)` — bytes **and** entry count behind a permission wall, derived
  per root as `df`'s used total minus what `du`'s own unprivileged walk could see, floored
  at zero. This is the real sudo gap; it is reported, never folded into `reclaimable`.
- `paths that vanished mid-scan` — gone before the walk reached them, a different
  blind spot than the sudo gap and never conflated with it.
- a `no Full Disk Access` note, only when `scan.py`'s `full_disk_access` probe came back
  ungranted **and** at least one `proposed`/`ask` row's path sits under `~/Library` — it
  names how many rows are affected and where to grant it. Silent otherwise, including
  when access is granted: this is a heads-up before approval, not a prediction of which
  row will fail — TCC overrides `os.access`, so nothing here can tell you that in advance.

### 3. Stop

Present the table and stop. Do not pre-select anything.

### 4. Collect approval

Use AskUserQuestion:

- `free` and `cheap` rows (action `proposed`) may be offered as one grouped question,
  itemizing the list the user just read.
- `costly` and `project` rows (action `ask`) are asked **individually**, never grouped —
  for each one's regeneration cost, read the block `classify.py` already printed in step
  2, not the manifest JSON.
- `review only` and `protected` rows are never offered.
- **When a row's `METHOD` is `rm`, say so plainly before the user answers.** That row will
  be permanently deleted, not moved to Trash — the approval question must state that in
  words (e.g. "this permanently deletes ~/.cache/convex, it will not go to Trash"), not
  leave the user to assume trash-by-default applies. A `~/.Trash` row is never offered at
  all — see step 5.

### 5. Execute approved items only

Use the `METHOD` column `classify.py` already printed next to each row in step 2 —
never re-derive the method from the manifest, the tier, or anywhere else. Reopening the
manifest and prefix-matching paths by eye is exactly how a `trash`-worded approval could
end up running a native command instead; `METHOD` exists so that never happens.

| `METHOD` column says | Do this |
|---|---|
| a command (e.g. `npm cache clean --force`, `docker system prune`) | run that exact command |
| `rm` | permanent `rm` on the path(s) — `cheap`-tier regenerable caches with no native command only; trashing them would relocate the bytes, not reclaim them. The user was already told this in step 4; do not soften it here. |
| `trash` | `/usr/bin/trash <path>` — one item at a time, never batched. See "Run per item, verify every outcome" below. |
| `empty Trash yourself` | run nothing. This is `~/.Trash` — report its size and tell the user to empty it themselves. Trashing the Trash reclaims 0 bytes while still counting toward what the user was asked about, and emptying it is inherently permanent, so it is the user's own action, never this skill's. |

**Run per item, verify every outcome — never report a success you did not check.** A
command returning is not the same as an item being reclaimed:

- **One invocation per item for `trash`/`rm`.** Run it once per approved path, even when
  several `trash` rows were approved together in one grouped question in step 4. A manifest
  `command` runs once per its natural unit instead (`brew cleanup` cleans everything
  Homebrew has cached in a single call — there is no per-item form of that, and none is
  needed since it either runs or it doesn't as one thing).
- **Why per item, not batched:** `/usr/bin/trash a b c` exits non-zero if *any* of `a`,
  `b`, `c` failed to move — the exit code alone cannot say which one. A batch invocation
  can half-succeed silently. Invoking `trash` once per path is what makes each outcome
  attributable to the item that produced it.
- **Capture the exit code of every invocation**, then confirm the path is actually gone
  (`test -e`, or equivalent) before calling it reclaimed — for a manifest command, confirm
  from its own output that it reclaimed something. A non-zero exit **or** a path that still
  exists afterward is a FAILURE for that item, full stop. Do not infer success merely
  because the command was issued.
- **On failure, report the tool's actual error text**, not a paraphrase. `"Logs" couldn't
  be moved to the trash because you don't have permission to access it` tells the user
  something real; "trash failed" does not.
- **Recognise the TCC signature specifically.** When the error contains
  `NSCocoaErrorDomain Code=513`, `afpAccessDenied`, `Operation not permitted`, or `don't
  have permission to access it`, say plainly that macOS privacy protection (TCC) blocked
  the operation — not a bug in the skill, not a bad path — and that the fix is granting
  the terminal Full Disk Access in System Settings → Privacy & Security → Full Disk
  Access. The skill will not do this for the user; say that plainly too, then move on to
  the next item.
- **One failure never stops the run.** Continue through the remaining approved items after
  one fails — a single TCC-protected path failing must not abort items that would have
  succeeded. Carry forward, per item, whether it succeeded or failed and why: step 6
  reports every one of them, not only the aggregate.

`docker system prune` is gated exactly like every other `command` entry, no differently —
run it only if the `docker-images` row appeared in step 2's table (meaning `docker_pass()`
in `scan.py` could reach the daemon and found reclaimable bytes) and only after that row
was individually approved in step 4, same as any other `costly` entry. When Docker is
unavailable — not on PATH, or its daemon isn't running, which is common since Docker
Desktop is frequently closed — no row is offered; report that Docker was unavailable and
move on, the same as for any other unreachable tool. Never run `docker system prune` from
memory of a prior session's row: a grouped or individual approval never carries into
another run, and this entry is no exception.

Show the exact command before running it. Run a dry run first when the tool has one
(`brew cleanup -n`, `docker system prune --dry-run`). Paths handed to `trash` (or `rm`)
are always absolute, so a leading `-` in a filename is never ambiguous to either — do not
add a `--` separator defensively: this build's `trash` has no such flag and instead treats
a literal `--` as a file to move, exiting nonzero even when the real path was trashed
successfully (verified in `tests/test_execution.sh`).

### 6. Verify and report

Report per item first, aggregate second. A `df` delta alone cannot distinguish "trashed,
awaiting an empty Trash" from "blocked by TCC" from "freed outright" — those mean
different things to the user, and a shortfall in the total cannot answer which items it
came from:

- **List every approved item** with the outcome step 5 already determined for it:
  **reclaimed** (the command exited zero and the path was confirmed gone — or, for a
  manifest command, confirmed by its own output) or **failed**, with the reason. For a
  failure, give the tool's real error text, and name Full Disk Access as the cause when
  that's what step 5 recognised. An item moved to Trash counts as reclaimed here even
  though its bytes are not yet freed from disk — say so; see the Trash note below. Never
  upgrade a "ran without a visible error" into "reclaimed" here — only report what step 5
  actually verified.
- **Then re-run `df -h`** and report bytes actually reclaimed against predicted, in
  aggregate. Explain any shortfall honestly — the usual causes are one or more failed
  items from the per-item list above, Trash not yet emptied (bytes moved, not freed), a
  sparse file reporting differently than its allocated size, or a daemon that was down. If
  items went to Trash, say so and note that emptying it is a separate, user-initiated
  step; items reclaimed with `rm` have no such gap — they are already fully freed.

## Tiers

| Tier | Meaning | Approval |
|---|---|---|
| `free` | regenerates itself at no cost | grouped |
| `cheap` | re-downloads on demand, incremental | grouped |
| `costly` | regenerable but expensive in time or bandwidth | individual |
| `project` | rebuildable, but the user's own work product | individual |
| `review only` | not in the manifest | never proposed |

## Notes

- `~/.cache/whisper` holds the model weights this repo's `transcribe-video` skill needs.
  Removing them forces a multi-GB re-download. Always surface that when offering it.
- A `node_modules`-shaped hit only qualifies as a `project` candidate when it has a
  sibling manifest (`package.json`, `Cargo.toml`, ...), is 30+ days stale, and its
  project's git worktree is clean — an unknown git state disqualifies it, it does not
  default to allowed. A bare name match returns far more hits than that on a real
  machine, mostly editor extensions and MCP servers under `~/.vscode/extensions`,
  `~/Library/Application Support/*`, and package-manager tool caches; `classify.is_protected`
  denylists those regardless of age or git state. This test runs on every `node_modules`,
  `target`, `.venv`, and `venv` directory `scan.py`'s `project_pass()` finds on every
  scanned root, not only under `$HOME` — a qualifying hit is itemized as its own `ask` row
  (tier `project`, `METHOD` `trash`) with a regeneration note in step 2's table, same as
  any other `ask` row; it is never grouped into `free`/`cheap`'s batch approval. A
  non-qualifying hit is classified exactly like any other path — most land `review only`,
  tool-directory hits land `protected` — and never appears twice: if the directory also
  has a row from the ordinary size scan, that row is what gets upgraded, not duplicated.
- Linux and Windows manifests exist (auto-selected via `classify.detect_os`) but are
  small, conservative starter sets, not vetted like macOS's 18 entries.
