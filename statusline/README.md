# Statusline

Custom Claude Code status line showing model, effort level, context window usage, cache read/write tokens, rate limits, session duration, git branch, and cost.

## What it shows

`🟢 Claude Sonnet 4.6 ● high  |  🧠 Context ●●○○○○ 32% (45,123 tokens)  |  📖 Read 44K (Σ 1.8M)  ✍ Write 1K (Σ 108K)  |  Daily ●○○○○○ 12% → 4h23m  |  Weekly ○○○○○○ 5% → 6d2h  |  ⏱ 14m  |  🌿 main  |  💰 $0.042`

- **Health indicator**: 🟢 / 🟡 / 🔴 based on worst pressure across context + rate limits
- **Model + effort**: current model and effort level setting
- **Context bar**: visual fill bar + percentage + token count
- **Cache tokens**: cache read and cache write tokens of the last request, with session totals (Σ) summed from the transcript. Each refresh scans only what was appended since the last one; totals are kept in `$TMPDIR/claude_tokens_<session_id>`. The first refresh of a resumed long session scans the whole transcript once (~3s for 50MB). Subagent requests are not counted
- **Rate limits**: daily (5-hour) and weekly usage with countdown to reset
- **Session duration**: time elapsed since session start
- **Git branch**: current branch name
- **Cost**: session cost in USD (`cost.total_cost_usd`, estimated at list price)

## Setup on a new device

### 1. Copy the script

```sh
cp statusline-command.sh ~/.claude/statusline-command.sh
chmod +x ~/.claude/statusline-command.sh
```

### 2. Add to `~/.claude/settings.json`

Open `~/.claude/settings.json` and add (or update) the `statusLine` key:

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash /Users/YOUR_USERNAME/.claude/statusline-command.sh"
  }
}
```

Replace `YOUR_USERNAME` with your actual macOS username (run `whoami` to check).

### 3. Verify

Restart Claude Code. The status line should appear at the bottom of the interface.
