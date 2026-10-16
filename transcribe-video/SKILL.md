---
name: transcribe-video
description: Use when the user wants to transcribe or caption a video or audio file with OpenAI Whisper locally — phrases like "transcribe this video", "get a transcript", "make subtitles/captions/SRT/VTT", "run whisper on <path>", or when they paste a path to a media file and want text out. Verifies whisper + ffmpeg are installed, asks for Whisper settings interactively (model, language, task, output formats), then runs the official `whisper` CLI and writes the outputs next to the source file. Do NOT use for the hosted OpenAI transcription API or for cloud transcription services — this drives the locally installed CLI only.
---

# Transcribe Video (local Whisper)

## Overview
Drives the **official `whisper` CLI** (the `openai-whisper` pip package) to turn a local
video or audio file into transcripts/subtitles. Whisper calls `ffmpeg` itself to decode
the media, so any container `ffmpeg` can read works directly — no separate audio
extraction step is needed.

The flow is: take a file path → preflight the tools and the file → ask the user how they
want Whisper configured → run it → report where the outputs landed (always **next to the
source file**).

## Steps

### 1. Get the source path
The user usually pastes a path (or passes it as the skill argument). If none was given,
ask for the absolute path to the video/audio file before doing anything else.

Normalise it: strip surrounding quotes and a `file://` prefix, and expand `~`. Keep the
absolute path — you'll need the file's parent directory for the output location.

### 2. Preflight — tools and file
Run these checks and **stop with a clear message if any fail** (don't push forward and let
Whisper error out cryptically):

- **`whisper` on PATH** — `command -v whisper`. If missing, tell the user to install it:
  `pip install -U openai-whisper` (and that it needs Python). Stop.
- **`ffmpeg` on PATH** — `command -v ffmpeg`. If missing, on macOS suggest
  `brew install ffmpeg`. Whisper cannot decode media without it. Stop.
- **File exists and is non-empty** — confirm the path resolves to a real file. If not,
  surface the bad path rather than guessing at a correction.

Optionally probe the media so you can set time expectations (Whisper on CPU is roughly
real-time-ish on the small models and much slower on `large`):
`ffprobe -v error -show_entries format=duration -of default=nk=1:noprint_wrappers=1 "<path>"`.
If `ffprobe` isn't present, skip this — it's a nicety, not a requirement.

### 3. Ask for Whisper settings (interactive)
Collect the settings with the **AskUserQuestion tool** — one call, grouped questions, with
the sensible default pre-selected as the first option in each group. This is the heart of
the skill: the user wants to choose how Whisper runs without memorising flags. Present
these groups:

**Model** — accuracy vs. speed/size tradeoff. Default `turbo`.
| Model | Notes |
|-------|-------|
| `turbo` | Default. Pruned large-v3; near-large quality, much faster. Multilingual. Good first choice. |
| `tiny` / `base` / `small` / `medium` | Increasing accuracy and cost. `small`/`medium` are solid CPU compromises. |
| `large` | Best accuracy, slowest, ~10 GB download on first use. |

If the user is sure the audio is English, mention the English-only variants
(`tiny.en`, `base.en`, `small.en`, `medium.en`) are faster/better for English — offer it,
don't force it. (There is no `turbo.en` or `large.en`.)

**Task** — `transcribe` (same language as the audio, default) or `translate` (translate
the speech into English).

**Language** — default **auto-detect** (pass nothing). Offer auto-detect plus a few likely
choices, and let the user type any supported language name/code (e.g. `English`, `en`,
`Tagalog`, `tl`). Specifying the language is faster and more accurate than detection when
the user already knows it. The CLI accepts either the full name or the ISO code.

**Output formats** — default `all`. Options map to `--output_format`:
`txt` (plain text), `srt` (subtitles), `vtt` (web captions), `tsv`, `json` (full segments
with timestamps), or `all`. This is a multi-select intent — if the user picks several
distinct formats, run Whisper once per format (the CLI's `-f` takes one value or `all`),
or just use `all` and point them at the ones they wanted. Prefer `all` when they pick 3+.

You may also surface, as a lighter touch, two useful extras — only ask if it fits the
context, otherwise leave at defaults:
- **Word-level timestamps** (`--word_timestamps True`) — needed for word-highlighted
  captions; default off.
- **Initial prompt** (`--initial_prompt "..."`) — seed jargon, names, or spelling to bias
  recognition; default none.

### 4. Build and run the command
Construct the command. Key flags:

- `--model <model>`
- `--task <transcribe|translate>`
- `--language <name>` — **omit entirely** for auto-detect.
- `--output_format <fmt>` — omit or use `all` for everything.
- `--output_dir "<parent dir of the source file>"` — this is what puts outputs **next to
  the video**. Always set it explicitly; the CLI's default is the current working
  directory, which is usually wrong here.
- `--device` — leave it **unset** and let Whisper pick, unless a CUDA GPU is present and you
  want to force it. On a Mac there is no usable CUDA device, so it runs on CPU either way
  (Whisper's MPS support is not wired up in the CLI). On CPU it warns `FP16 is not supported,
  using FP32`; that warning is harmless — don't treat it as an error, and don't pass
  `--fp16 False`, the fallback is automatic.

Show the user the exact command before running it (transparency + a chance to tweak), then
run it. Transcription can take a while on larger models — run it in the foreground and let
the user see Whisper's progress output, or background it for very long files and poll.

Example:
```bash
whisper "/Users/me/Movies/talk.mp4" \
  --model turbo \
  --task transcribe \
  --output_format all \
  --output_dir "/Users/me/Movies"
```

### 5. Report results
List the files Whisper wrote (it names them after the source stem, e.g. `talk.srt`,
`talk.txt`, in the output dir). Give the user the full paths. If a `.txt` was produced,
optionally show the first few lines so they can sanity-check the result. If Whisper exited
non-zero, surface its actual stderr — common real failures are an unreadable/corrupt file
or running out of memory on `large`.

## Notes
- **Outputs always land next to the source file** by design (per this skill's setup). If
  the user wants them elsewhere, they can say so and you adjust `--output_dir`.
- First use of any model downloads its weights to `~/.cache/whisper` (one-time, can be
  large for `large`). Mention this if a download is clearly happening on a slow link.
- Batch: the CLI accepts multiple input files. If the user points at several videos, pass
  them all in one invocation with a shared `--output_dir`, or loop per-file when each
  needs different settings.
