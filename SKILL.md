---
name: instagram-scraping-agent
description: >
  Scrape Instagram reels, transcribe them with Whisper, and save them as formatted script
  entries (heading, views/likes/saves/shares, link, full transcript). Two modes: a DAILY
  scrape of competitor creators listed per niche, and an ON-DEMAND scrape of reel links
  posted in a Slack channel. Use when the user says "run the daily scrape", "scrape these
  reels", "scrape the Slack channel", or pastes instagram.com/reel/ links to transcribe.
---

# Instagram Scraping Agent

Scrapes Instagram reels via RapidAPI ("Instagram Scraper 2025"), transcribes the audio,
generates a short heading with Claude, and writes each reel as a script entry:

```
[Heading - brief summary]
[Views] Views | [Likes] Likes | [Saves] Saves | [Shares] Shares
https://www.instagram.com/reel/[shortcode]/
[Full transcript]
```

Setup (keys, install) is in `README.md`. Never print or commit `scraping-agent/config.json`.

## Mode 1 — Daily scrape

Scrapes every creator listed under `scraping-agent/Pages to scrape/`, only reels newer than
the last run (tracked per creator in `state.json`).

```bash
cd scraping-agent
python daily_scrape.py                          # all niches
python daily_scrape.py --niches "Example Niche" # one niche
python daily_scrape.py --creators someuser      # specific creators
```

Output: `Pages to scrape/<Niche>/<Niche> Video ideas/scripts_YYYY-MM-DD_<niche>_creators.txt`

A failed creator scrape leaves that creator's state untouched, so the next run retries
instead of skipping reels. To run it unattended, schedule it (Windows Task Scheduler / cron).

### Adding a niche
```
Pages to scrape/<Niche Name>/
└── <Niche Name> Creators.txt   # one username per line, no @, # = comment
```
The `<Niche Name> Video ideas/` output folder is created automatically.

## Mode 2 — On-demand scrape from Slack

1. Read the Slack channel set in `config.json` → `slack_channel_name` (via the Slack MCP /
   connector) and collect the `instagram.com/reel/...` or `/p/...` URLs from recent messages.
2. Group the links by creator if known, then run:

```bash
cd scraping-agent
python slack_scrape.py <url1> <url2> --niche "Example Niche"
python slack_scrape.py --file links.txt --username <creator> --niche "Example Niche"
```

- `--username` helps enrich metrics when the creator can't be resolved automatically.
- `--output path.txt` overrides the output location.
- Output goes to the niche's `Video ideas` folder if it exists, otherwise `scraping-agent/`.

## How metrics work (why two endpoints)

- `/postdetail/` gives media URLs + caption, but its views are **Instagram-only** and it
  never returns saves.
- `/userreels/` returns the **combined Instagram + Facebook** play count and real saves.
- `slack_scrape.py` fetches `/postdetail/` first, then does one `/userreels/` lookup per
  creator to upgrade views and fill saves. If a reel isn't found there, it keeps the
  Instagram-only number and logs a warning.

## Transcription

- Uses the `video_url` first (the audio-only stem sometimes strips vocals).
- Primary: OpenAI `whisper-1` if `OPENAI_API_KEY` is set. Fallback: local `faster-whisper`
  (model size from `whisper_model`). Output is English (`task=translate`).
- No speech → falls back to the caption, tagged `[NO SPEECH — caption only]`. Treat those as
  caption-only reels, not talking-head transcripts.

## Headings

Generated with Claude (`ANTHROPIC_API_KEY`). Set `SKIP_HEADING_API=1` to use a local
fallback heading instead (no API calls).

## Troubleshooting

- Logs: `scraping-agent/scraping_agent.log` (daily mode).
- 429 / quota errors: add a second key as `rapidapi_key_fallback`; the scraper switches
  automatically.
- Empty transcripts: check `OPENAI_API_KEY` quota, or that `faster-whisper` installed.
