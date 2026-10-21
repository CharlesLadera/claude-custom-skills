# claude-custom-skills

Personal Claude Code skill library. Each top-level directory containing a `SKILL.md` is one skill.

## This is the public copy, not the live one

The skills that actually run come from a separate private repo: `~/.claude/skills/<name>` links there, not here. Editing this repo changes no session's behavior, and the branch checked out here doesn't matter to anything live. The private repo also holds work-specific skills and journals that must never land here.

The two repos have separate histories. Changes cross as patches (`git diff` → `git apply`), never as commits, merges or cherry-picks:

- **Private → public:** scan the patch for names, ticket IDs, company or client names, locations and machine paths before pushing. The private copies of shared skills keep machine details, such as `disk-cleanup`'s measured volume roots, that the public copies replace with generic wording. Keep those differences.
- **Public → private:** port the same way, then run that repo's test suites.

Fix a shared skill in both repos, or say which one you left out.

To try a change here live, load it for one run with `claude --plugin-dir <skill-dir>` instead of relinking `~/.claude/skills`.

## Most of these are behavior specs, not code

Most skills are prose: no build, no lint, no tests. "Verified" means re-read for contract consistency with sibling skills; it does not mean a command exited 0. Don't claim a skill works because the file was written.

Three ship real code, and those are judged the ordinary way:

- `context-audit/scripts/audit.py` — a script, no test suite.
- `disk-cleanup/` — `scripts/scan.py`, `scripts/classify.py`, and 287 tests run by `disk-cleanup/tests/run.sh`. Run them before and after changing anything there.
- `gmail-organize/` — `scripts/scan.py` (read-only), `apply/apply.py` (the only writer), `lib/gws.py`, and the tests run by `gmail-organize/tests/run.sh`. Run them before and after changing anything there.

`disk-cleanup/tests/test_safety.py` is load-bearing, not housekeeping: it fails the suite if any deletion or move primitive appears in a file under `disk-cleanup/scripts/`, which is what makes the scanner structurally incapable of deleting. Deletion lives in `SKILL.md`, after approval, by design. Never weaken that test to make a change pass — if a change seems to need it, the change is wrong.

`gmail-organize/tests/test_safety.py` plays the same role: it fails the suite if a write method appears in `scripts/` or `lib/`, if `apply.py`'s write allowlist changes, or if anything other than `scan.gws_read` and `apply.gws_call` reaches `gws`. Same rule — never weaken it to make a change pass.

Where a skill embeds a shell snippet, run it against a scratch directory before shipping it — those are the parts that can actually be wrong in a checkable way. Even that is not enough on its own: `disk-cleanup`'s snippets passed in a scratch tree and still broke on the real machine twice, once on a GNU `du` flag BSD accepts and silently ignores, and once on a macOS privacy denial that made an approved removal fail without the skill noticing. Run it against the real thing before believing it.

## Families share a format through one owner

Where several skills read and write the same file, exactly one owns the format and the rest reference it by name:

- `end-of-day` owns the session journal (`docs/custom-agents/memory/sessions/<slug>.md`) — its frontmatter and slug rules. `start-of-day` and `log-session` follow it. Its `🗣 For the daily report` and `🎯 Next Session Plan` sections exist for a later session that aggregates every journal into one report.
- `start-ticket` owns the ticket-workflow config keys and the task-file layout. `resume-ticket`, `finish-ticket`, and `ticket-status` follow it.
- `start-ticket` also owns the **Context home** rule (`contextBranch` and local mode: config discovery from the main checkout first, where `docs/custom-agents/` lives, how it is committed or why it never is). Every skill that touches that folder — the ticket family, the journaling trio, `save-conversation`, `daily-report` — applies it by reference.

Change an owner and check its siblings in the same pass. A sibling that restates a format instead of pointing at its owner is how the two drift apart — that drift was the cause of every broken cross-skill contract found in the 2026-09-17 audit.

## Layout

Directories stay flat: `~/.claude/skills/` is a flat namespace, so a directory name is exactly the name it installs under. Don't nest skills into bundle folders — it breaks existing symlinks for no gain.

`.claude-plugin/marketplace.json` publishes the same directories as plugins without moving them. A skill that runs its own scripts must reach them through `${CLAUDE_SKILL_DIR}`, never `~/.claude/skills/<name>/` — a plugin install doesn't live there. Adding a skill means adding it to the marketplace too. Check with `claude plugin validate .`, then a real install: the validator never opens a plugin-root `SKILL.md`.

Every marketplace entry pins `"version"`, all set to the current release. Plugin users stay on their cached copy until that string changes, so merging to `main` ships nothing to them. To release: set every entry's `version` to the new number in a PR, merge it, then tag `v<number>` on that merge commit. Forget the bump and plugin users never get the release. Keep `version` out of any `plugin.json`, because a manifest value silently overrides the entry.

`statusline/` is a config artifact, not a skill. `docs/` is gitignored and lives only on this machine — this repo runs the Context home rule's local mode, so journaling skills write there and commit nothing. Never `git add -f` anything under it. `docs/custom-agents/` holds this repo's own session journals. `docs/superpowers/` holds the design spec, implementation plan, and decision record behind `disk-cleanup` — the decision record lists every ruling made while building it, with what each costs if wrong.

## Git

Push requires the `CharlesLadera` GitHub account. The wrong account fails as "Repository not found", not as an auth error.

`main` is protected, admins included: every change lands through a PR, the `GitGuardian Security Checks` status must pass, and force-push and deletion are blocked. CodeRabbit reviews but is deliberately not a required check — its free tier allows one review per hour, and a required check that can't run blocks every merge.
