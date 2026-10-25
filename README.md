# claude-custom-skills

A personal library of custom [Claude Code](https://docs.anthropic.com/en/docs/claude-code) skills and configuration, kept in one repo so any machine can be set up by cloning it and linking the skills into place. MIT licensed — take one skill or all of them.

## Highlights

If you only look at three, look at these. Each ships real code with tests, and each is built so the tool cannot change anything you didn't approve.

| Skill | What it does | Why it's different |
|---|---|---|
| [`context-audit`](#environment-upkeep) | Ranks every enabled plugin and MCP server by always-on token cost against how much you actually use it, then applies the cut list | Took one machine's per-request baseline from 51,590 to 6,495 tokens. Tells "unused here but busy in another repo" apart from "dead everywhere", so a cut never breaks another project |
| [`disk-cleanup`](#disk-cleanup) | Scans every readable volume and ranks directories, large files and apps biggest-first in one table | The scanner is structurally unable to delete: a safety test fails the 287-test suite if a delete or move call ever appears in its scripts. Removal happens only after you approve each item by name |
| [`gmail-organize`](#email) | Migrates a mailbox to one label convention, sorts the backlog and installs filters | One file can write, its write allowlist is pinned by tests (112 in the suite), every batch is validated before its first write, and `--undo-last` reverses it. Nothing is ever permanently deleted |

Everything else here is a `SKILL.md` of instructions with no code and no test suite. They are checked by re-reading them against their sibling skills, not by running anything — treat them as well-specified workflows, not verified software.

## What's inside

| Area | Contents |
|---|---|
| [Session journaling](#session-journaling) | Skills that capture and restore work context across sessions |
| [Conversation capture](#conversation-capture) | Archive a whole conversation to a structured folder |
| [Ticket workflow](#ticket-workflow-worktree-per-ticket) | A worktree-per-ticket development lifecycle, from kickoff to PR |
| [Environment upkeep](#environment-upkeep) | Keep the Claude Code prompt lean — rank plugins by always-on token cost against real usage |
| [Disk cleanup](#disk-cleanup) | Scan the whole machine for reclaimable storage and remove only what you approve, item by item |
| [Email](#email) | Organize a Gmail mailbox into one label convention through the gws CLI, changing only what you approve |
| [Media](#media) | Local media tooling — transcribe video/audio with Whisper |
| [Job search](#job-search) | Scrape Philippine government sources for open, degree-relevant vacancies |
| [Statusline](#statusline) | A custom status line showing model, context usage, rate limits, and cost |

Each skill lives in its own directory as a `SKILL.md` file — the standard Claude Code skill format with frontmatter (`name`, `description`) followed by the instructions Claude follows when the skill is invoked. Directories stay flat here because `~/.claude/skills/` is itself a flat namespace, so a skill's directory name is exactly the name it installs under.

**Families share a format through one owner.** Where several skills read and write the same file, exactly one of them defines that format and the rest reference it by name instead of restating it — `end-of-day` owns the session journal's frontmatter and slug rules, `start-ticket` owns the ticket-workflow config keys and the task-file layout. Change an owner and check its siblings in the same pass. A sibling that restates a format rather than pointing at its owner is how the two silently drift apart.

## Skills

### Session journaling

Skills for keeping a continuous work journal so context survives between sessions. They all write to a shared path convention inside whatever project you're working in (see [Path conventions](#path-conventions)).

| Skill | Description |
|---|---|
| [`start-of-day`](./start-of-day/SKILL.md) | Restore context from previous sessions at the start of a work session |
| [`end-of-day`](./end-of-day/SKILL.md) | Wrap up a session — captures accomplishments, pending items, and next plans |
| [`side-note`](./side-note/SKILL.md) | Quick side note (research, a decision, a side task) appended to a session journal and landed on `main` the same way `end-of-day` lands its entry |
| [`daily-report`](./daily-report/SKILL.md) | Prepare for a daily standup: summarize recent ticket and email activity into a standup document |

A typical day: `start-of-day` to pick up where you left off → work → `side-note` for any notable side tasks → `end-of-day` to close out.

`start-of-day`, `side-note`, and `end-of-day` all read and write the same per-session journal, `docs/custom-agents/memory/sessions/<slug>.md`, keyed to a slug derived from the git branch. Parallel branches get parallel journals instead of colliding in one dated file. `end-of-day` owns that format; the other two follow it.

### Conversation capture

| Skill | Description |
|---|---|
| [`save-conversation`](./save-conversation/SKILL.md) | Save any conversation to a structured folder with a TL;DR + detailed breakdown, metadata, references, and artifacts |

Deliberately **not** part of the session journal above. It writes to `docs/custom-agents/conversations/<name>/`, captures a whole conversation rather than a session's progress, and shares no format with the journaling trio — so don't edit it to "match" them.

### Ticket workflow (worktree-per-ticket)

A lifecycle for working multiple tickets in parallel, each in its own git worktree. Project-agnostic — each skill resolves project conventions from an optional [config file](#ticket-workflow-config-optional) or by detecting/asking.

| Skill | Run from | Description |
|---|---|---|
| [`start-ticket`](./start-ticket/SKILL.md) | main checkout | Provision a worktree on a fresh branch, scaffold the ticket task file, set the tracker to In Progress |
| [`resume-ticket`](./resume-ticket/SKILL.md) | the worktree | Re-read the ticket file, recap done-vs-pending, launch the dev server |
| [`finish-ticket`](./finish-ticket/SKILL.md) | the worktree | Build, review, open a PR to the base branch, optionally integrate into a test-bed branch, close out |
| [`ticket-status`](./ticket-status/SKILL.md) | anywhere | Dashboard of all worktrees — ticket, branch, PR, integration status, dev server |

The flow: `start-ticket` → work in the worktree (with `resume-ticket` to re-enter after a break) → `finish-ticket` → repeat. `ticket-status` gives an at-a-glance view of everything in flight.

### Environment upkeep

Keeping the Claude Code environment itself lean, so context goes to your work instead of tooling you forgot you installed.

| Skill | Description |
|---|---|
| [`context-audit`](./context-audit/SKILL.md) | Rank enabled plugins by always-on token cost against how much you actually use them, propose a cut list, and write a full local tooling inventory |

Every enabled plugin's skill descriptions and every MCP server's tool names ride in the system prompt on **every request**. `context-audit` joins per-plugin cost (`claude plugin details`) with real usage — both machine-wide counts and per-repo counts derived from session transcripts — and separates four verdicts: `keep`, `CUT` (dead everywhere), `SCOPE` (unused here but busy elsewhere, so a global disable would break another project), and `SHARED` (enabled in a committed `settings.json`, so cutting it would change teammates' setups).

On the machine it was built for, it took the baseline from 51,590 to 6,495 tokens.

### Disk cleanup

Finding what is eating a disk, and reclaiming it without the tool deciding anything on its own.

| Skill | Description |
|---|---|
| [`disk-cleanup`](./disk-cleanup/SKILL.md) | Scan every readable volume, rank all storage biggest-first in one table, and reclaim only what you approve by name |

The scan is wide and the proposal set is narrow, deliberately. It walks both volumes, ranks directories, large individual files and applications together, and shows the whole picture — including plenty it will never offer to touch. Only two kinds of row can be proposed: an entry in the vetted 18-entry macOS manifest, and a project artifact (`node_modules`, `target`, `.venv`) that has a sibling manifest, is 30+ days stale, and sits in a clean git worktree. Everything else renders `review only`.

`scripts/scan.py` and `scripts/classify.py` contain no deletion code at all, and `tests/test_safety.py` fails the suite if a deletion or move primitive ever appears in either. Removal happens only in `SKILL.md`, after approval, and each row's `METHOD` column states exactly how it would be reclaimed — `trash`, permanent `rm` for regenerable caches where trashing would only relocate the bytes, or a native command like `npm cache clean --force`. `costly` and `project` rows are approved one at a time with their regeneration cost shown; `free` and `cheap` can be approved as a group, but only over a list just displayed.

The footer is written to be trusted rather than impressive: `reclaimable` and `review only` are labelled as overlapping rather than summed, the sudo-gated blind spot is reported in bytes and never counted as reclaimable, and paths that vanished mid-scan are tracked separately from paths permission refused.

On the machine it was built for: 27G reclaimable across 22 rows, out of 281G its unprivileged walk could see — plus a further 20G across 404 entries it reports as unreadable rather than guessing at.

### Email

Organizing a Gmail mailbox without handing the decisions to the tool.

| Skill | Description |
|---|---|
| [`gmail-organize`](./gmail-organize/SKILL.md) | Migrate existing labels into one Action + Topic convention, sort the backlog, install Gmail filters, and keep it sorted — one approved batch at a time |

It talks to Gmail through the `gws` CLI rather than a Claude connector, so it works on whichever Google account `gws` is signed in as, whatever Claude account runs it. The label convention lives in one file, `references/convention.md`: three `@` action labels you apply by hand, and fixed topic roots (`Clients/`, `Team/`, `Projects/`, `Vendors/`, `Finance/`, `Notify/`, `News/`, `Personal/`).

`scripts/scan.py` only reads — headers, never bodies — and `tests/test_safety.py` fails the suite if a write method ever appears in it. Every change goes through `apply/apply.py`, which validates a whole approved batch before its first write, verifies each op afterwards, and logs every write so `--undo-last` can reverse it. Nothing is permanently deleted; trash is its own table, and the applier re-checks each message (starred, important, client/team/finance labels, age, threads you replied to) before trashing it. Per-mailbox rules and the undo log live in `~/.config/gmail-organize/`, outside this repo.

### Media

Local media tooling that wraps CLIs you already have installed.

| Skill | Description |
|---|---|
| [`transcribe-video`](./transcribe-video/SKILL.md) | Transcribe/caption a video or audio file with the local OpenAI Whisper CLI — preflights `whisper` + `ffmpeg`, asks for settings (model, language, task, output formats) interactively, then writes outputs next to the source file |

Requires the `openai-whisper` pip package (`whisper` on PATH) and `ffmpeg`. Point it at a path — "transcribe this video: /path/to/clip.mp4" — and it walks you through the settings.

### Job search

On-demand scraping of Philippine government job sources, filtered to the roles worth applying to.

| Skill | Description |
|---|---|
| [`gov-job-finder`](./gov-job-finder/SKILL.md) | Find open PH government vacancies in a region you name. Fans out parallel subagents across CSC, agency regional offices, state universities, and LGUs; keeps tech roles + roles open to any bachelor's degree; drops board-locked and field-specific ones; ranks tech first; prints a table and saves a dated markdown file |

Runs on demand — "find gov jobs" or `/gov-job-finder`. Takes `region=` (asked if missing); tech + any-degree, open postings only; tune with `radius=`, `city=`, `breadth=`, `save=` arguments.

## Statusline

A custom Claude Code status line that surfaces model, effort level, context-window usage, daily/weekly rate limits with reset countdowns, session duration, git branch, and cost:

`🟢 Claude Sonnet 4.6 ● high  |  🧠 Context ▓▓░░░░ 32% (45,123 tokens)  |  Daily ▓░░░░░ 12% → 4h23m  |  ...`

See [`statusline/README.md`](./statusline/README.md) for setup instructions.

## Installation

Two ways in. Use the plugin marketplace to take a few skills and update them through Claude Code. Use symlinks to hack on the skills themselves.

### As plugins

```bash
claude plugin marketplace add CharlesLadera/claude-custom-skills
claude plugin install disk-cleanup@claude-custom-skills
```

Or in one step inside a session (Claude Code 2.1.275+):

```
/plugin install dev-workflow --marketplace CharlesLadera/claude-custom-skills
```

| Plugin | Skills |
|---|---|
| `context-audit` | `context-audit` |
| `disk-cleanup` | `disk-cleanup` |
| `gmail-organize` | `gmail-organize` |
| `dev-workflow` | `start-of-day`, `end-of-day`, `side-note`, `save-conversation`, `daily-report`, `start-ticket`, `resume-ticket`, `finish-ticket`, `ticket-status` |
| `transcribe-video` | `transcribe-video` |
| `gov-job-finder` | `gov-job-finder` |

`dev-workflow` is one plugin because its skills share the `docs/custom-agents/` folder and the **Context home** rule in `start-ticket`. Plugin skills are namespaced (`/disk-cleanup:disk-cleanup`). The bare name works too, unless something else already uses it. Don't install a skill both as a plugin and as a symlink — you'd get two copies under the same bare name. The statusline isn't a plugin; see [Statusline](#statusline).

Plugins follow tagged releases, not every commit to `main`. Each plugin carries the release's version number, so `claude plugin update <name>@claude-custom-skills` changes nothing until the next release. Auto-update is off by default for marketplaces outside Anthropic's own. Turn it on under `/plugin` → Marketplaces → `claude-custom-skills` → **Enable auto-update**.

### As symlinks

On a new machine, clone this repo and **symlink** each skill into `~/.claude/skills/`:

```bash
git clone https://github.com/CharlesLadera/claude-custom-skills.git ~/claude-custom-skills

# Link every skill in the repo (any directory containing a SKILL.md)
for skill_md in ~/claude-custom-skills/*/SKILL.md; do
  src=$(dirname "$skill_md")
  dest=~/.claude/skills/"$(basename "$src")"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    echo "skip $dest — a real directory is already there; diff it before replacing"
    continue
  fi
  ln -sfn "$src" "$dest"
done
```

To take just one skill, link only that directory:

```bash
ln -sfn ~/claude-custom-skills/disk-cleanup ~/.claude/skills/disk-cleanup
```

The skills in `dev-workflow` — the journaling trio, the ticket family, `save-conversation` and `daily-report` — read files the others write and follow `start-ticket`'s **Context home** rule, so link them together. `context-audit`, `disk-cleanup`, `gmail-organize`, `transcribe-video` and `gov-job-finder` stand alone.

### Requirements

| Skill | Needs on `PATH` |
|---|---|
| `context-audit` | `python3`, the `claude` CLI |
| `disk-cleanup` | `python3`. macOS is the vetted target; Linux and Windows get small, conservative starter manifests |
| `gmail-organize` | `python3`, `gws` (`npm install -g @googleworkspace/cli`, then `gws auth login`) |
| `end-of-day`, `start-of-day`, ticket family | `git`, [`gh`](https://cli.github.com/) for PR status |
| `transcribe-video` | `whisper` (`pip install openai-whisper`), `ffmpeg` |
| `daily-report` | an MCP for your PM tool (Jira, Linear, Monday.com, …), optionally Gmail |

Links, not copies, for two reasons. A skill directory can hold more than `SKILL.md` — `context-audit/` ships `scripts/audit.py`, which its own instructions invoke by path, so a `SKILL.md`-only copy installs a broken skill. And copies drift silently in *both* directions: the repo goes stale on one side while a hand-edit on the other exists nowhere else. Linking makes the repo the single source of truth and the drift structurally impossible. The loop refuses to touch a path that is already a real directory — diff that copy against the repo before replacing it, in both directions.

Skills in `~/.claude/skills/` hot-reload; no restart is needed. Type `/start-of-day`, or just describe the task and Claude picks the matching skill.

**One consequence of linking:** a live skill follows whichever branch this repo has checked out. Editing a skill on a feature branch changes its behaviour everywhere until that branch merges, and switching back to `main` silently reverts it. Merge before relying on a change.

The same goes for updates: with links, every `git pull` changes what your skills do, scripts included. If you would rather review changes first, check out a tagged release instead of `main`.

### Updating

```bash
cd ~/claude-custom-skills && git pull
```

That is the entire update — the links already point here. Re-run the install loop only after adding a brand-new skill directory.

## Path conventions

The skills use a consistent layout under `docs/custom-agents/` inside whatever project you're working in:

- `docs/custom-agents/memory/` — work journals (`start-of-day`, `end-of-day`, `side-note`)
- `docs/custom-agents/conversations/` — saved conversations (`save-conversation`)
- `docs/custom-agents/tasks/` — ticket task files (the ticket-workflow skills)
- `docs/custom-agents/standups/` — standup documents (`daily-report`)

By default that folder lives in whichever checkout you're in and travels with your branches. Setting **`contextBranch`** in the config (below) moves all of it to a single branch in the main checkout instead — see **Context home** in [`start-ticket`](./start-ticket/SKILL.md), which owns the rule.

### Ticket-workflow config (optional)

The ticket-workflow skills work in any repo. They detect sensible defaults and ask when unsure, but you can pin per-project choices in `docs/custom-agents/ticket-workflow.config.json` so they run zero-friction:

```json
{
  "worktreeDir": ".worktrees",
  "baseBranch": "main",
  "integrationBranch": null,
  "contextBranch": null,
  "contextRemote": "origin",
  "tracker": "linear",
  "branchPrefix": "",
  "taskFilesDir": "docs/custom-agents/tasks",
  "project": "web",
  "devCommand": "pnpm dev",
  "buildCommand": "pnpm build"
}
```

Every skill looks for this file in the **main checkout** first, then the current checkout — so a worktree that doesn't carry `docs/custom-agents/` still finds it.

- **worktreeDir** — where worktrees are created (must be gitignored).
- **baseBranch** — the PR target (e.g. `main`, `staging`, `develop`).
- **integrationBranch** — optional personal test-bed branch that all in-flight work is merged into for combined testing; leave `null` to disable that step.
- **contextBranch** / **contextRemote** — optional. When set (e.g. `"dev-yourname"`), all of `docs/custom-agents/` — journals, conversations, task files, standups, this config — lives only on that branch, in the main checkout. Every skill writes there and commits + pushes straight to it (explicit paths, `add -f`, no PR); nothing under `docs/custom-agents/` goes to the base branch or a feature branch. The main checkout must stay on this branch; the skills stop rather than switch it. Leave `null` for the default per-checkout behavior.
  - **Local mode** — leave `contextBranch` `null` and gitignore `docs/custom-agents/` (or a parent like `docs/`). The folder then lives only in the main checkout and is never committed, pushed or PR'd. Use it for a public repo whose journals must stay private. There is no backup, so back the folder up yourself.
- **tracker** — `linear`, `github`, or `none`.
- **branchPrefix** — optional namespace for branch names (e.g. `yourname/`).
- **project** — the app/sub-project a ticket belongs to. It is a folder under `taskFilesDir`, so task files live at `<taskFilesDir>/<project>/<in-progress|complete>/<ticket-id>-<slug>.md`. Every ticket skill reads this key; leave it unset and each one guesses a different folder.
- **devCommand** / **buildCommand** — for a monorepo these may be filter templates like `pnpm --filter <pkg> dev`.

## Repo layout

```
claude-custom-skills/
├── README.md
├── LICENSE               # MIT
├── .claude-plugin/       # marketplace.json: the skills as installable plugins
├── start-of-day/         # session journaling skills
├── end-of-day/
├── side-note/
├── save-conversation/
├── start-ticket/         # ticket-workflow skills
├── resume-ticket/
├── finish-ticket/
├── ticket-status/
├── daily-report/         # standup prep from tickets + email
├── disk-cleanup/         # reclaimable-storage scan (+ scripts/, tests/)
├── gmail-organize/       # Gmail label convention via gws (+ scripts/, apply/, tests/)
├── context-audit/        # plugin/MCP token-cost audit (+ scripts/audit.py)
├── transcribe-video/     # local Whisper transcription
├── gov-job-finder/       # PH government job scraper (any region)
├── statusline/           # status line script + setup guide
└── docs/                 # local only, gitignored: session journals and design notes
```
