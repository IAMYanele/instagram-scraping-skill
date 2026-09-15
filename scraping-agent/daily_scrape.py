#!/usr/bin/env python3
"""
Daily Instagram Scraping Agent.
Runs via Task Scheduler at 4 AM daily.
Scrapes competitor profiles, transcribes audio, generates headings.
"""

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.scraper import InstagramScraper, ScraperError
from shared.transcriber import transcribe_audio_url
from shared.heading_generator import generate_heading

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "scraping_agent.log"


# ---------------------------------------------------------------------------
# Config & State
# ---------------------------------------------------------------------------

def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def get_since_timestamp(state: dict, username: str, lookback_days: int) -> int:
    """Get the timestamp to scrape from for a given user."""
    if username in state:
        return state[username]["last_scrape_timestamp"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    return int(cutoff.timestamp())


def update_state(state: dict, username: str, reels_count: int):
    """Update state after successful scrape."""
    now = datetime.now(timezone.utc)
    state[username] = {
        "last_scrape_timestamp": int(now.timestamp()),
        "last_scrape_date": now.isoformat(),
        "reels_scraped": reels_count,
    }


# ---------------------------------------------------------------------------
# Niche discovery
# ---------------------------------------------------------------------------

def discover_niches(pages_dir: Path) -> list[dict]:
    """Scan Pages to scrape/ for niche folders.
    Returns list of {niche_name, creators_file, output_dir}."""
    niches = []
    for folder in sorted(pages_dir.iterdir()):
        if not folder.is_dir():
            continue
        niche_name = folder.name
        creators_file = folder / f"{niche_name} Creators.txt"
        output_dir = folder / f"{niche_name} Video ideas"

        if not creators_file.exists():
            logging.warning(f"No creators file found: {creators_file}")
            continue

        output_dir.mkdir(exist_ok=True)

        niches.append({
            "niche_name": niche_name,
            "creators_file": creators_file,
            "output_dir": output_dir,
        })
    return niches


def read_creators(creators_file: Path) -> list[str]:
    """Read usernames from a creators file, one per line."""
    lines = creators_file.read_text(encoding="utf-8").strip().splitlines()
    usernames = []
    for line in lines:
        username = line.strip().lstrip("@")
        if username and not username.startswith("#"):
            usernames.append(username)
    return usernames


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_reel_output(heading: str, reel: dict, transcript: str,
                       audio_type: str = "talking_head") -> str:
    """Format a single reel's output block.

    audio_type:
        "talking_head" — Whisper transcribed speech successfully
        "caption_only" — No speech detected, using caption/text as fallback
        "no_content"   — No speech and no caption available
    """
    views = f"{reel['combined_views']:,}"
    likes = f"{reel['likes']:,}"
    saves = f"{reel['saves']:,}"
    shares = f"{reel['shares']:,}"

    audio_tag = ""
    if audio_type == "caption_only":
        audio_tag = "[NO SPEECH — caption only] "
    elif audio_type == "no_content":
        audio_tag = "[NO SPEECH — no transcript available] "

    return (
        f"{heading}\n"
        f"{views} Views | {likes} Likes | {saves} Saves | {shares} Shares\n"
        f"{reel['link']}\n"
        f"{audio_tag}{transcript}\n"
    )


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def _process_single_reel(reel: dict, config: dict) -> str:
    """Process a single reel: transcribe audio + generate heading.
    Runs in a thread for parallel execution."""
    logger = logging.getLogger(__name__)
    logger.info(f"  Processing reel: {reel['shortcode']}")

    # Try Whisper transcription. Prefer video_url — the audio_url stem sometimes
    # strips vocals, leaving Whisper nothing to transcribe.
    transcript = None
    audio_type = "talking_head"
    media_url = reel.get("video_url") or reel.get("audio_url")
    if media_url:
        transcript = transcribe_audio_url(
            media_url,
            model_size=config.get("whisper_model", "medium")
        )

    # Fallback to caption/existing transcript
    if not transcript:
        transcript = reel.get("transcript", "")
        if transcript:
            audio_type = "caption_only"
            logger.info(f"  [NO SPEECH] Using caption as fallback for {reel['shortcode']}.")
        else:
            audio_type = "no_content"

    # Generate heading from transcript
    heading = generate_heading(transcript or "[No transcript]")

    return format_reel_output(heading, reel, transcript or "[No transcript]",
                              audio_type=audio_type)


def process_creator(scraper: InstagramScraper, username: str,
                    since_timestamp: int, config: dict,
                    max_workers: int = 4) -> list[str] | None:
    """Scrape a creator's new reels, transcribe + generate headings in parallel.
    Returns a list of formatted output strings ([] = no new reels), or None if the
    scrape itself FAILED — so the caller can leave the creator's state untouched
    and retry next run instead of silently skipping reels."""
    logger = logging.getLogger(__name__)
    logger.info(f"Scraping @{username} (since ts={since_timestamp})...")

    try:
        reels = scraper.get_reels_since(username, since_timestamp)
    except ScraperError as e:
        logger.error(f"Failed to scrape @{username}: {e}")
        return None  # signal failure (vs [] = no new reels) so state is left untouched

    if not reels:
        logger.info(f"No new reels for @{username}.")
        return []

    logger.info(f"Found {len(reels)} new reel(s) for @{username}. Processing in parallel...")

    # Process all reels in parallel (transcription + heading generation)
    outputs = [None] * len(reels)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(_process_single_reel, reel, config): i
            for i, reel in enumerate(reels)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                outputs[idx] = future.result()
            except Exception as e:
                logger.error(f"  Failed to process reel {reels[idx]['shortcode']}: {e}")
                # Still include a basic entry so we don't lose the reel
                reel = reels[idx]
                outputs[idx] = format_reel_output(
                    reel.get("transcript", "")[:50] + "...",
                    reel,
                    reel.get("transcript", "[Processing failed]"),
                    audio_type="caption_only"
                )

    return [o for o in outputs if o]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _write_output(output_path: Path, all_outputs: list[str]):
    """Write (or append to) the output file incrementally."""
    separator = f"\n{'=' * 60}\n\n"
    new_content = separator.join(all_outputs)

    if output_path.exists():
        existing = output_path.read_text(encoding="utf-8").strip()
        if existing:
            new_content = existing + separator + new_content

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(new_content, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Daily Instagram Scraping Agent")
    parser.add_argument("--creators", nargs="*",
                        help="Only process these creators (usernames)")
    parser.add_argument("--niches", nargs="*",
                        help="Only process these niches (folder names, case-insensitive)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ]
    )
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("Daily Instagram Scraping Agent starting...")
    logger.info("=" * 60)

    config = load_config()
    state = load_state()

    scraper = InstagramScraper(
        api_key=config["rapidapi_key"],
        api_host=config["rapidapi_host"],
        max_retries=config.get("max_retries", 4),
        rate_limit_delay=config.get("rate_limit_delay", 0.5),
        fallback_api_key=config.get("rapidapi_key_fallback"),
    )

    pages_dir = BASE_DIR / config.get("pages_dir", "Pages to scrape")
    if not pages_dir.exists():
        logger.error(f"Pages directory not found: {pages_dir}")
        sys.exit(1)

    niches = discover_niches(pages_dir)
    if not niches:
        logger.warning("No niches found. Exiting.")
        return

    only_creators = set(c.lstrip("@") for c in args.creators) if args.creators else None
    only_niches = set(n.lower() for n in args.niches) if args.niches else None
    today = datetime.now().strftime("%Y-%m-%d")

    for niche in niches:
        if only_niches and niche["niche_name"].lower() not in only_niches:
            logger.info(f"Skipping niche: {niche['niche_name']} (not in --niches filter)")
            continue
        logger.info(f"=== Processing niche: {niche['niche_name']} ===")
        creators = read_creators(niche["creators_file"])

        if only_creators:
            creators = [c for c in creators if c in only_creators]

        if not creators:
            logger.warning(f"No creators to process for {niche['niche_name']}")
            continue

        niche_slug = niche["niche_name"].lower().replace(" ", "_")
        filename = f"scripts_{today}_{niche_slug}_creators.txt"
        output_path = niche["output_dir"] / filename

        # Collect all outputs in memory, write once at the end
        all_outputs = []
        for username in creators:
            since_ts = get_since_timestamp(
                state, username, config.get("initial_lookback_days", 30)
            )
            outputs = process_creator(scraper, username, since_ts, config)

            if outputs is None:
                # Scrape FAILED — leave this creator's timestamp untouched so the next
                # run retries from where it left off (advancing it would silently skip reels).
                logger.warning(f"Scrape failed for @{username}; leaving state untouched (will retry next run).")
                continue

            if outputs:
                all_outputs.extend(outputs)
                logger.info(f"Got {len(outputs)} script(s) for @{username}")

            update_state(state, username, len(outputs))
            save_state(state)

        # Single write at the end - no intermediate files
        if all_outputs:
            _write_output(output_path, all_outputs)
            logger.info(f"Total: {len(all_outputs)} script(s) for {niche['niche_name']} written to {output_path}")
        else:
            logger.info(f"No new scripts for {niche['niche_name']} today.")

    logger.info("Daily scrape complete.")


if __name__ == "__main__":
    main()
