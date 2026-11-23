# Example vault CLAUDE.md

A starting point for the `CLAUDE.md` at the root of an Obsidian vault, for a PARA + Zettelkasten layout. Copy what fits, rename folders to match the vault, and keep the prose and the rules block in agreement. The `obsidian-vault` skill reads the vault's copy, not this file.

---

```markdown
# CLAUDE.md

This is a personal Obsidian vault built on a PARA + Zettelkasten hybrid. There is nothing to build or test here; work means creating, editing and organizing Markdown notes by the conventions below.

## Vault structure (PARA)

    Vault/
    ├── Home.md            ← dashboard; don't restructure
    ├── 00 - Inbox/        ← raw captures, no structure required
    ├── 01 - Projects/     ← work with a finish line, grouped by client or domain
    ├── 02 - Areas/        ← ongoing responsibilities (career, health, finance)
    ├── 03 - Resources/    ← knowledge base and reference, incl. Map Of Contents/
    ├── 04 - Archive/      ← finished or inactive; never delete, move here instead
    ├── _templates/        ← Templater templates
    └── z_Media/           ← images and attachments

## Method: PARA + Zettelkasten

PARA decides where a note lives, by how actionable it is. Zettelkasten decides how knowledge
notes are written and connected. Folders come from PARA; links come from Zettelkasten.

### PARA: where a note goes

Ask in order; stop at the first yes:

| Test | Folder |
|---|---|
| Is it a raw thought I haven't decided about yet? | 00 - Inbox/ |
| Does it have a finish line? | 01 - Projects/<client-or-domain>/ |
| Is it a standard I keep up indefinitely? | 02 - Areas/ |
| Would I want to look this up again someday? | 03 - Resources/<topic>/ |
| Is it finished, abandoned, or no longer mine? | 04 - Archive/ |

- A finished project moves to the archive as a whole folder. Anything worth keeping beyond it becomes a permanent note first.
- A project done for an area still lives in Projects; the area note links to it.
- Unsure between two folders: leave it in the inbox and say why.

### Zettelkasten: the knowledge layer

Permanent notes live in 03 - Resources/Concepts/ (general) or a concepts/ folder under a topic.

| Note type | What it is | Template |
|---|---|---|
| Capture | a quick thought, link or snippet; lands in the inbox | none |
| Reference | cheat sheets, command lists, API docs; can be long | none |
| Literature | notes on one source; optional | literature.md |
| Permanent | one idea, in my own words | resource.md (type: permanent) |
| MOC | an index of notes on a topic, in Map Of Contents/ | moc.md |

Permanent notes: one idea (if it can't be said in one sentence, suggest a split); own words;
the title states the idea; at least one link to a related note, and listed on the topic's MOC.
Keep a template-stamped `id` unchanged; don't backfill it.

## Naming

Files: lowercase, hyphen-separated (`webhook-retries.md`). Folders: Title Case allowed.
Exempt: Home.md, CLAUDE.md, README.md files, MOCs, the inbox, the archive.

## Frontmatter

Every note has `tags` (lowercase-hyphenated) and `created: YYYY-MM-DD`. Project main notes
also have `status`. Never overwrite an existing value when adding frontmatter.

## Key rules

- Archive instead of delete.
- Moving or renaming a note breaks links to it unless they are rewritten: moves and renames go
  through the obsidian-vault skill, which rewrites them and commits each batch to git.

## Lint rules

    ```json obsidian-vault-rules
    {
      "naming": {"pattern": "^[a-z0-9][a-z0-9.\\-]*$",
                 "exempt": ["Home.md", "CLAUDE.md", "**/README.md", "00 - Inbox/**", "04 - Archive/**",
                            "03 - Resources/Map Of Contents/**"]},
      "frontmatter": {"required": ["tags", "created|date"],
                      "exempt": ["Home.md", "CLAUDE.md", "**/README.md", "00 - Inbox/**"]},
      "orphans": {"exempt": ["Home.md", "CLAUDE.md", "**/README.md", "00 - Inbox/**"]},
      "duplicates": {"exempt": ["**/README.md"]},
      "lintIgnore": ["04 - Archive/**"],
      "inbox": "00 - Inbox",
      "archive": "04 - Archive",
      "templates": "_templates"
    }
    ```
```

The rules block must be a fenced block whose info string is exactly `json obsidian-vault-rules` (shown indented above only so it nests inside this example). `"created|date"` means either key satisfies the requirement. Glob patterns support `*`, `**` and `?`. Add folders other tools write into (generated reports, exports) to `lintIgnore` and to the naming exemptions.
