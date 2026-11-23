---
name: obsidian-vault
description: Use when working in an Obsidian vault — finding notes, filing a new note or capture, linking notes or updating a MOC, triaging the inbox, moving, renaming or archiving notes, importing notes from another folder, or cleaning up broken links, orphans, empty notes, frontmatter and naming. Also for "/obsidian-vault", "my vault", "second brain", "file this in Obsidian".
---

# Obsidian vault

Search, file, link, organize and clean up the vault, keeping its conventions and never breaking a link.

The vault's own `CLAUDE.md` owns the conventions: folder structure, file naming, frontmatter, templates, and the `obsidian-vault-rules` JSON block the scanner reads. A vault without one starts from `references/vault-claude-md.example.md` (a PARA + Zettelkasten layout), adapted to the vault as it is. Read it at the start of every session. This skill owns how changes are made safely, not what the conventions are. When the two disagree, fix the vault `CLAUDE.md` (prose and rules block together) with the user's yes.

Two scripts do the mechanical work. Both print JSON. Run them with `python3`, from this skill's folder or by absolute path:

| Script | Does | Writes? |
|---|---|---|
| `scripts/scan.py` | `lint`, `backlinks`, `find`, `unlinked`, `resolve`, `inbox`, `rules` | Never |
| `apply/apply.py` | `preview`, `run`, `checkpoint`, `import` | The only writer |

The vault path comes from `config.json` (`vaultPath`). If it is missing, stop and tell the user to copy `config.example.json` to `config.json`. Every command also takes `--vault <path>`, before or after the subcommand.

## Hard rules

1. **Moves and renames go through `apply.py`, never `mv`, Write-then-delete, or Finder.** A move on disk breaks every link to the file, and Obsidian's "update links" setting only works for renames inside the app. `apply.py` rewrites wikilinks, embeds, markdown links and canvas nodes, and keeps the old visible text as an alias when a name changes.
2. **Nothing is deleted.** Empty junk (blank `Untitled` notes, 0-byte stubs) moves to `<archive>/_trash/`, the vault's bin. Inactive notes that have content move to `<archive>/<area>/`. (`<inbox>`, `<archive>` and `<templates>` are the folders the rules block names.) `apply.py` has no delete operation; don't use `rm` or the trash in its place. That includes text: fixing a broken link means retargeting it, never deleting the link or the bullet it sits in.
3. **Every change is a git commit in the vault, local only.** `apply.py run` commits its own batch, so `git revert <sha>` undoes it. Never push the vault repo.
4. **Approval before any batch.** Show the `preview` output (moves, every link it will rewrite, frontmatter it will add), get a yes, then `run` the same plan file. A single new note the user asked for needs no extra approval. "I'm busy, just do it" doesn't remove this step. It changes its shape: put every batch in one message, each with its preview summary, and ask for one yes covering all of them. A guess (a retarget with more than one candidate, a folder chosen by elimination) is never applied silently: list it in its own "needs your call" group.
5. **Leave these alone unless the user names them:** the structure of the vault's home or dashboard note; folders that other tools write into (the vault `CLAUDE.md` lists them); `.obsidian/`; and `<templates>/`.
6. **Add frontmatter, never overwrite it.** `apply.py` skips keys that already have a value. `date:` satisfies `created:`.

### Dirty vault and the first checkpoint

`run` and `import` refuse to start while the vault has uncommitted changes, unless given `--checkpoint`, which first commits them as `obsidian-vault: checkpoint before …`.

- If `git -C <vault> log --oneline --grep '^obsidian-vault:'` prints nothing, this is the first run. Show the user the `uncommitted_changes` count from `preview` and the top folders affected (`git -C <vault> status --short`), and ask before passing `--checkpoint`. That commit becomes the baseline. The count leaves out `.DS_Store` and `.obsidian/workspace.json`, which the OS and the app rewrite on their own; they ride along in the next real commit and never count as dirt.
- After that, pass `--checkpoint` without asking. It only commits the user's own edits from Obsidian.

## Session start

1. Read `<vault>/CLAUDE.md`, including its method section if it has one (for example how PARA picks a folder and how Zettelkasten notes are written and linked).
2. Run `scan.py rules`. If `_source` says defaults, the rules block is missing: tell the user, and offer to add one that matches the prose.

## Search

| Need | Command |
|---|---|
| By tag (nested tags match) | `scan.py find --tag finance` |
| By property | `scan.py find --prop status="🔵 In Progress"` |
| Text, name or folder | `scan.py find --text 'regex' --name 'regex' --folder '01 - Projects'` |
| Who links here | `scan.py backlinks "<note name or path>"` |
| Where a link points | `scan.py resolve "<link text>" --from "<source note>"` |

For a broad full-text question, Grep over the vault works too. Answer with vault-relative paths, and quote the matching line.

## Capture: file a new note

1. **Folder and note type.** Apply the filing rules in the vault `CLAUDE.md` (with the example's PARA method: its tests in order, and a note type from its Zettelkasten table). Look at the sibling notes in the folder you pick before deciding. When two folders fit, leave the note in the inbox and say why.
2. **Name.** Follow the naming rule in the vault `CLAUDE.md` (lowercase-hyphenated, `.md`). Check that the name is unique: `scan.py resolve <name>` must return null, or wikilinks to it become ambiguous.
3. **Template.** Use the template the vault `CLAUDE.md` lists for the note type. Templater won't run on a file written to disk, so replace every `<% … %>` yourself:

   | Expression | Write |
   |---|---|
   | `tp.date.now("YYYY-MM-DD")`, `tp.file.creation_date()` | today, `YYYY-MM-DD` |
   | `tp.date.now("dddd")` / `("MMM Do")` | weekday / `Nov 23rd` |
   | `tp.date.now("YYYYMMDDHHmm")` | now, digits only |
   | `tp.file.title` | the note's title in words |
   | anything else | the value it would produce; delete the expression if none applies |

   No `<%` may remain. `scan.py lint` reports leftovers as `templater_leftovers`.
4. **Frontmatter.** At least `tags` (lowercase-hyphenated, reuse tags the siblings use) and `created`. A project's main note (the one made from `project.md`) also gets `status`; other notes inside a project folder don't.
5. **Link it in.** Add a link from the note's natural parent (the project's overview note, the area note, or the topic's MOC), or the note is an orphan from day one.
   For a **permanent note**, also: state its idea in one sentence. If you can't, propose a split and let the user decide. Run `scan.py find --folder "<resources folder>" --text '<key term>'` for related notes and link at least one in its Connections section.
6. Commit: `apply.py checkpoint -m "add <note>"`.

If the `obsidian` plugin ([obsidianmd/obsidian-skills](https://github.com/obsidianmd/obsidian-skills)) is installed, use it for Obsidian syntax and file formats: `obsidian:obsidian-markdown` for callouts, embeds and properties, `obsidian:defuddle` for a web page as a note, `obsidian:obsidian-bases` for `.base` files, `obsidian:json-canvas` for `.canvas`.

## Link

- **Unlinked mentions:** `scan.py unlinked "<note>"` lists notes that mention its title or aliases in plain text. Propose links as a table (source, line, the new link). On approval, edit each source, linking only the first mention: `[[target|text as written]]`.
- **MOC upkeep:** compare a MOC's links with `scan.py find --folder <topic folder>`. Propose the missing notes under the right MOC headings.
- A single edit can go through the Edit tool (read the note first), then `apply.py checkpoint -m "<what>"`. Several edits go in a plan's `edits` list instead, so they get a preview and one commit.

## Organize: inbox, moves, archive

**One note** ("move X", "rename X", "archive X"): `scan.py backlinks X` to see what will be rewritten, pick the destination as in Capture steps 1-2, then steps 3-5 below with a one-move plan. No inbox scan needed.

**The inbox:**

1. `scan.py inbox` lists inbox notes with their age, size and first line. Read each one.
2. Propose a table: note, destination path (folder and new name), tags, and the parent note that will link to it. Include archiving as an option. Leave a note in the inbox when its home is unclear, and say why.
3. Write the plan file in the scratchpad:

   ```json
   {
     "description": "triage inbox: 3 notes",
     "moves": [{"from": "00 - Inbox/quick note.md", "to": "03 - Resources/Tools/quick-note.md"}],
     "frontmatter": [{"path": "03 - Resources/Tools/quick-note.md",
                      "set": {"tags": ["tools"], "created": "2026-10-26"}}],
     "aliasOnRename": true
   }
   ```

   `frontmatter` paths are the paths after the moves. For `created`, use `created_guess` from `lint`, `created` from `scan.py inbox`, or the file's birth date. Never use today's date for an old note. A filed capture keeps its text as written, half-finished lines included; it gets frontmatter, not a template. A rename needs no `aliases` entry for the old name: Obsidian doesn't resolve links through aliases, and `apply.py` already rewrote every existing link.
4. `apply.py preview <plan>`, show the result, get a yes, then `apply.py run <plan> [--checkpoint]`. Report the commit and its `undo` line. The preview's `emptied_folders` lists folders the plan leaves empty: say so in the approval message, especially for a whole project folder. The skill never removes folders; the user can remove them in Finder. `"checkpoint": null` in the result means there was nothing to commit first.
5. Then add the parent-note links from step 2, and commit them with a checkpoint.

**Import** from a folder outside the vault: `apply.py import <file>... --to "<vault folder>"` copies the files under their original names and commits them. The source is never touched. Then rename them with a normal plan so their links get rewritten. Deleting the source folder is a separate step: show what is in it and ask first.

## Cleanup

1. `scan.py lint` (add `--limit 20` for a first look). Show the `summary` as a table, then work through the kinds in this order. A batch is one plan of 25 files or fewer, and it may mix kinds. A whole-vault cleanup doesn't have to finish in one session: stop after the batches the user approved and report what is left.

   | Kind | Fix |
   |---|---|
   | `broken_links` | Find the intended target (`find --name`, `find --text`), confirm it with `resolve`, and retarget through the plan's `edits` (`{"path", "find", "replace"}`; `find` must occur exactly once in the file, so include enough of the link). A link to a note that was never written stays as it is unless the user says otherwise; it is a placeholder, not an error. |
   | `templater_leftovers` | Render the expressions, as in Capture step 3. |
   | `missing_frontmatter`, `missing_keys` | A plan with `frontmatter` only. Propose tags from the folder and siblings, and `created_guess` for the date. |
   | `unportable_names` | Rename first. Windows can't store `<>:"|?*\` or a trailing dot or space, so syncing the vault to a Windows device fails on them. |
   | `naming` | A plan with renames to `suggest`. Where the item has `suggest_taken`, pick a more specific name; where it has `name_from_content` (Untitled notes), name it from what the note says. The preview shows every link it rewrites. |
   | `empty` | Empty **and** orphaned: move to `<archive>/_trash/` (rule 2) as part of the batch. Empty but linked from somewhere: ask whether to fill it in or archive it, and leave it until the user answers. |
   | `orphans` | Link from a parent note or MOC, or archive. Some are fine on their own; say which. |
   | `duplicate_names` | Rename one so the names differ; links stay correct because `apply.py` writes path-form links where a name is ambiguous. |

2. After each `run`, rerun `scan.py lint` and report the change in the summary counts. That is the check that proves the batch worked.

## Common mistakes

| Mistake | Instead |
|---|---|
| `mv` a note, then grep for links to fix | `apply.py` plan: one step, every link form, committed |
| Grepping `[[Old Name]]` to find links | `scan.py backlinks`: it resolves headings, table pipes, `%20`, case and relative paths the way Obsidian does |
| Deleting an empty or duplicate note | Move it to `<archive>/` |
| Leaving `<% tp... %>` in a new note | Render every expression yourself |
| Using today's date as `created` for an old note | `created_guess`, or the file's birth date |
| Filing a new note without linking it anywhere | Link it from its parent note or MOC |
| Renaming notes in a folder another tool writes | Leave those folders to the tool that owns them; exempt them in the rules block |
| Writing your own link checker | `scan.py lint`: hand-rolled ones miss table pipes (`\|`) and report dozens of false broken links |
| A tarball or file copies as the backup | `apply.py` commits each batch; `git revert` undoes it |
| "User is busy" so batches skip approval | One message, all batches, one yes; guesses listed apart |

## Tests

`tests/run.sh` runs the suite. `tests/test_safety.py` is load-bearing: it fails if `lib/` or `scripts/` can write or spawn a process, if `apply.py` gains a delete primitive or a move that can overwrite, or if its git verbs grow beyond `rev-parse status add commit log`. Never weaken it to make a change pass.
