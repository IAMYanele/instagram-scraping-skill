# Instagram Scraping Agent

Scrape Instagram reels, transcribe them, and save them as script entries — either daily
from a list of creators, or on demand from reel links posted in Slack.
Full usage for Claude Code is in [`SKILL.md`](SKILL.md).

## Setup

**1. Install** (Python 3.10+; `ffmpeg` recommended for local transcription)
```bash
pip install -r requirements.txt
```

**2. Get your own keys**
- **RapidAPI** — subscribe to *Instagram Scraper 2025* (host
  `instagram-scraper-20251.p.rapidapi.com`) and copy your key.
- **Anthropic** — for headings (optional; set `SKIP_HEADING_API=1` to skip).
- **OpenAI** — for fast Whisper transcription (optional; falls back to local faster-whisper).

**3. Configure**
```bash
cd scraping-agent
cp config.example.json config.json   # then paste your RapidAPI key into config.json
```
Set the other keys as environment variables:
```bash
export ANTHROPIC_API_KEY=...
export OPENAI_API_KEY=...
```
(PowerShell: `$env:ANTHROPIC_API_KEY="..."`)

`config.json` is git-ignored — keep it that way. The RapidAPI key must live in `config.json`
(the scripts read it from there).

**4. Add creators** — edit `scraping-agent/Pages to scrape/Example Niche/Example Niche Creators.txt`
or add your own niche folder (see `SKILL.md`).

## Run

```bash
cd scraping-agent
python daily_scrape.py                              # daily mode
python slack_scrape.py <reel_url> --niche "Example Niche"   # on-demand mode
```

## Use as a Claude Code skill

Copy this folder into your project (or `~/.claude/skills/instagram-scraping-agent/`) and
ask Claude to "run the daily scrape" or "scrape the reels in Slack". The Slack mode needs a
Slack connector/MCP so Claude can read the channel.
