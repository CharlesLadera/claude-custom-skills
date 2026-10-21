#!/bin/sh
input=$(cat)

# ANSI color codes
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
RESET='\033[0m'

SEP='  |  '

# Build a visual bar for a given percentage (0-100), 6 blocks wide
# Colors: green 0-60%, yellow 61-80%, red 81-100%
make_bar() {
  pct="$1"
  filled=$(( (pct * 6 + 50) / 100 ))
  if [ "$filled" -gt 6 ]; then filled=6; fi
  empty=$(( 6 - filled ))

  if [ "$pct" -le 60 ]; then
    color="$GREEN"
  elif [ "$pct" -le 80 ]; then
    color="$YELLOW"
  else
    color="$RED"
  fi

  bar=""
  i=0
  while [ "$i" -lt "$filled" ]; do
    bar="${bar}●"
    i=$(( i + 1 ))
  done
  i=0
  while [ "$i" -lt "$empty" ]; do
    bar="${bar}○"
    i=$(( i + 1 ))
  done

  printf "${color}%s${RESET}" "$bar"
}

# Format seconds into a human-readable countdown string (e.g. "1h32m", "45m", "2d3h")
format_countdown() {
  secs="$1"
  if [ "$secs" -le 0 ]; then
    echo "now"
    return
  fi
  days=$(( secs / 86400 ))
  hrs=$(( (secs % 86400) / 3600 ))
  mins=$(( (secs % 3600) / 60 ))
  if [ "$days" -gt 0 ]; then
    printf "%dd%dh" "$days" "$hrs"
  elif [ "$hrs" -gt 0 ]; then
    printf "%dh%dm" "$hrs" "$mins"
  else
    printf "%dm" "$mins"
  fi
}

# Format a raw integer with thousands separators (POSIX awk)
format_tokens() {
  echo "$1" | awk '{
    n = int($1)
    s = ""
    neg = 0
    if (n < 0) { neg = 1; n = -n }
    do {
      d = n % 1000
      n = int(n / 1000)
      if (n > 0) s = sprintf(",%03d%s", d, s)
      else       s = sprintf("%d%s",    d, s)
    } while (n > 0)
    if (neg) s = "-" s
    print s
  }'
}

# Format a raw integer as a short count (e.g. "950", "217K", "1.8M", "9.5B")
format_short() {
  echo "$1" | awk '{
    n = $1 + 0
    if      (n >= 1e9) printf "%.1fB", n / 1e9
    else if (n >= 1e6) printf "%.1fM", n / 1e6
    else if (n >= 1e3) printf "%.0fK", n / 1e3
    else               printf "%d", n
  }'
}

now=$(date +%s)

# ── Session duration ─────────────────────────────────────────────────────────
session_id=$(echo "$input" | jq -r '.session_id // empty')
if [ -n "$session_id" ]; then
  SESSION_FILE="${TMPDIR:-/tmp}/claude_session_${session_id}"
else
  SESSION_FILE="${TMPDIR:-/tmp}/claude_session_start"
fi

if [ -f "$SESSION_FILE" ]; then
  session_start=$(cat "$SESSION_FILE" 2>/dev/null)
  # Only digits reach $(( )): arithmetic expansion would run a planted $(...).
  case "$session_start" in *[!0-9]*) session_start="" ;; esac
  if [ -n "$session_start" ]; then
    age=$(( now - session_start ))
  else
    echo "$now" > "$SESSION_FILE"
    session_start="$now"
    age=0
  fi
else
  echo "$now" > "$SESSION_FILE"
  session_start="$now"
  age=0
fi

elapsed_hrs=$(( age / 3600 ))
elapsed_mins=$(( (age % 3600) / 60 ))
if [ "$elapsed_hrs" -gt 0 ]; then
  duration_str=$(printf "%dh%dm" "$elapsed_hrs" "$elapsed_mins")
else
  duration_str=$(printf "%dm" "$elapsed_mins")
fi

# ── Model ────────────────────────────────────────────────────────────────────
model=$(echo "$input" | jq -r '.model.display_name // "Claude"')

# ── Effort level ─────────────────────────────────────────────────────────────
# Prefer effort level from the JSON payload; fall back to settings.json
effort_raw=$(echo "$input" | jq -r '.effort.level // .effort_level // .effort // .settings.effortLevel // empty' 2>/dev/null)
if [ -z "$effort_raw" ]; then
  effort_raw=$(jq -r '.effortLevel // empty' "$HOME/.claude/settings.json" 2>/dev/null)
fi
effort_label=""
case "$effort_raw" in
  low)    effort_label="○ low"   ;;
  medium) effort_label="◐ med"   ;;
  high)   effort_label="● high"  ;;
  xhigh)  effort_label="● xhigh" ;;
  max)    effort_label="◆ max"   ;;
esac

# ── Context window ───────────────────────────────────────────────────────────
used=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
ctx_str=""
used_int=0
if [ -n "$used" ]; then
  used_int=$(printf "%.0f" "$used")
  ctx_bar=$(make_bar "$used_int")

  # Try to get a raw token count; compute from percentage + total if needed
  total_input=$(echo "$input" | jq -r '.context_window.total_input_tokens // empty')
  raw_tokens=""
  if [ -n "$total_input" ] && [ "$total_input" != "0" ]; then
    raw_tokens=$(format_tokens "$total_input")
  else
    ctx_size=$(echo "$input" | jq -r '.context_window.context_window_size // empty')
    if [ -n "$ctx_size" ] && [ -n "$used" ] && [ "$ctx_size" != "0" ]; then
      computed=$(echo "$used $ctx_size" | awk '{printf "%.0f", ($1/100)*$2}')
      if [ -n "$computed" ] && [ "$computed" != "0" ]; then
        raw_tokens=$(format_tokens "$computed")
      fi
    fi
  fi

  if [ -n "$raw_tokens" ]; then
    ctx_str=$(printf "🧠 Context %s %d%% (%s tokens)" "$ctx_bar" "$used_int" "$raw_tokens")
  else
    ctx_str=$(printf "🧠 Context %s %d%%" "$ctx_bar" "$used_int")
  fi
fi

# ── Cache read / write tokens ────────────────────────────────────────────────
# Last request from context_window.current_usage; session totals summed from the
# transcript. Transcripts reach ~50MB, so each refresh scans only the bytes
# appended since the previous one and keeps running totals in a state file.
last_read=$(echo "$input" | jq -r '.context_window.current_usage.cache_read_input_tokens // empty')
last_write=$(echo "$input" | jq -r '.context_window.current_usage.cache_creation_input_tokens // empty')
transcript=$(echo "$input" | jq -r '.transcript_path // empty')

sum_read=""
sum_write=""
if [ -n "$session_id" ] && [ -f "$transcript" ]; then
  TOKENS_FILE="${TMPDIR:-/tmp}/claude_tokens_${session_id}"
  offset=0; sum_read=0; sum_write=0; last_req=""
  if [ -f "$TOKENS_FILE" ]; then
    read -r offset sum_read sum_write last_req < "$TOKENS_FILE"
    # Only digits reach $(( )): arithmetic expansion would run a planted $(...).
    case "${offset}x${sum_read}x${sum_write}" in
      *[!0-9x]*|x*|*xx*|*x) offset=0; sum_read=0; sum_write=0; last_req="" ;;
    esac
  fi
  size=$(wc -c < "$transcript" | tr -d ' ')
  if [ "$size" -lt "$offset" ]; then
    offset=0; sum_read=0; sum_write=0; last_req=""
  fi
  if [ "$size" -gt "$offset" ]; then
    chunk_len=$(( size - offset ))
    # One line per API response chunk; lines of one request share a requestId
    # and the same usage, so each request is counted once. Subagent lines are
    # skipped: the totals describe this conversation's own context.
    scanned=$(tail -c +$(( offset + 1 )) "$transcript" | head -c "$chunk_len" | LC_ALL=C awk -v len="$chunk_len" -v last="$last_req" '
      {
        bytes += length($0) + 1
        lb[NR] = length($0) + 1
        if ($0 ~ /"type":"assistant"/ && $0 !~ /"isSidechain":true/ && match($0, /"cache_read_input_tokens":[0-9]+/)) {
          r[NR] = substr($0, RSTART + 26, RLENGTH - 26)
          w[NR] = 0
          if (match($0, /"cache_creation_input_tokens":[0-9]+/)) w[NR] = substr($0, RSTART + 30, RLENGTH - 30)
          q[NR] = ""
          if (match($0, /"requestId":"[^"]*"/)) q[NR] = substr($0, RSTART + 13, RLENGTH - 14)
        }
      }
      END {
        n = NR; used = bytes
        # A last line with no newline is still being written: leave it for the next refresh.
        if (bytes > len) { n = NR - 1; used = bytes - lb[NR] }
        for (i = 1; i <= n; i++) if (i in r) {
          if (q[i] != "" && q[i] == last) continue
          sr += r[i]; sw += w[i]; last = q[i]
        }
        printf "%d %d %d %s\n", used, sr, sw, last
      }')
    read -r used add_read add_write new_req <<EOF
$scanned
EOF
    case "${used}x${add_read}x${add_write}" in
      *[!0-9x]*|x*|*xx*|*x) ;;
      *)
        offset=$(( offset + used ))
        sum_read=$(( sum_read + add_read ))
        sum_write=$(( sum_write + add_write ))
        last_req="$new_req"
        echo "$offset $sum_read $sum_write $last_req" > "$TOKENS_FILE"
        ;;
    esac
  fi
fi

cache_str=""
if [ -n "$last_read" ] || [ -n "$sum_read" ]; then
  read_part="📖 Read $(format_short "${last_read:-0}")"
  write_part="✍ Write $(format_short "${last_write:-0}")"
  if [ -n "$sum_read" ]; then
    read_part="${read_part} (Σ $(format_short "$sum_read"))"
    write_part="${write_part} (Σ $(format_short "$sum_write"))"
  fi
  cache_str="${read_part}  ${write_part}"
fi

# ── Rate limits ──────────────────────────────────────────────────────────────
five_pct=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
five_reset=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')
week_pct=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty')
week_reset=$(echo "$input" | jq -r '.rate_limits.seven_day.resets_at // empty')

five_int=0
week_int=0
daily_str=""
weekly_str=""

if [ -n "$five_pct" ]; then
  five_int=$(printf "%.0f" "$five_pct")
  five_bar=$(make_bar "$five_int")
  if [ -n "$five_reset" ]; then
    five_secs=$(( five_reset - now ))
    five_cd=$(format_countdown "$five_secs")
    daily_str=$(printf "Daily %s %d%% → %s" "$five_bar" "$five_int" "$five_cd")
  else
    daily_str=$(printf "Daily %s %d%%" "$five_bar" "$five_int")
  fi
fi

if [ -n "$week_pct" ]; then
  week_int=$(printf "%.0f" "$week_pct")
  week_bar=$(make_bar "$week_int")
  if [ -n "$week_reset" ]; then
    week_secs=$(( week_reset - now ))
    week_cd=$(format_countdown "$week_secs")
    weekly_str=$(printf "Weekly %s %d%% → %s" "$week_bar" "$week_int" "$week_cd")
  else
    weekly_str=$(printf "Weekly %s %d%%" "$week_bar" "$week_int")
  fi
fi

# ── Health emoji ─────────────────────────────────────────────────────────────
# Worst pressure across context, daily, and weekly
worst=0
if [ -n "$used" ]; then
  if [ "$used_int" -gt "$worst" ]; then worst="$used_int"; fi
fi
if [ -n "$five_pct" ]; then
  if [ "$five_int" -gt "$worst" ]; then worst="$five_int"; fi
fi
if [ -n "$week_pct" ]; then
  if [ "$week_int" -gt "$worst" ]; then worst="$week_int"; fi
fi

if [ "$worst" -gt 80 ]; then
  health="🔴"
elif [ "$worst" -gt 60 ]; then
  health="🟡"
else
  health="🟢"
fi

# ── Git branch ───────────────────────────────────────────────────────────────
git_branch=$(git branch --show-current 2>/dev/null)
branch_str=""
if [ -n "$git_branch" ]; then
  branch_str="🌿 ${git_branch}"
fi

# ── Cost estimate ────────────────────────────────────────────────────────────
cost_str=""
cost_val=$(echo "$input" | jq -r '
  .cost.total_cost_usd //
  .cost_usd           //
  .usage.cost         //
  .session_cost       //
  .total_cost         //
  empty
')
if [ -n "$cost_val" ] && [ "$cost_val" != "0" ] && [ "$cost_val" != "0.0" ]; then
  formatted_cost=$(printf "%.3f" "$cost_val")
  cost_str=$(printf "💰 \$%s" "$formatted_cost")
fi

# ── Assemble output ──────────────────────────────────────────────────────────
if [ -n "$effort_label" ]; then
  out="${health} ${model} ${effort_label}"
else
  out="${health} ${model}"
fi

if [ -n "$ctx_str" ]; then
  out="${out}${SEP}${ctx_str}"
fi

if [ -n "$cache_str" ]; then
  out="${out}${SEP}${cache_str}"
fi

if [ -n "$daily_str" ]; then
  out="${out}${SEP}${daily_str}"
fi

if [ -n "$weekly_str" ]; then
  out="${out}${SEP}${weekly_str}"
fi

out="${out}${SEP}⏱ ${duration_str}"

if [ -n "$branch_str" ]; then
  out="${out}${SEP}${branch_str}"
fi

if [ -n "$cost_str" ]; then
  out="${out}${SEP}${cost_str}"
fi

printf "%s" "$out"
