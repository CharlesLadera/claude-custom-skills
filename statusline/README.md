# Statusline

Custom Claude Code status line showing model, effort level, context window usage, rate limits, session duration, git branch, and cost.

## What it shows

`🟢 Claude Sonnet 4.6 ● high  |  🧠 Context ●●○○○○ 32% (45,123 tokens)  |  Daily ●○○○○○ 12% → 4h23m  |  Weekly ○○○○○○ 5% → 6d2h  |  ⏱ 14m  |  🌿 main  |  💰 $0.042`

- **Health indicator**: 🟢 / 🟡 / 🔴 based on worst pressure across context + rate limits
- **Model + effort**: current model and effort level setting
- **Context bar**: visual fill bar + percentage + token count
- **Rate limits**: daily (5-hour) and weekly usage with countdown to reset
- **Session duration**: time elapsed since session start
- **Git branch**: current branch name
- **Cost**: session cost in USD (when available)

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
