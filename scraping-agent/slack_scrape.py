#!/usr/bin/env python3
"""
On-demand Instagram Reel Scraper.
Accepts reel links from command line or file.
Designed to be invoked by Claude Code after reading Slack.

Usage:
    python slack_scrape.py --username <creator> https://instagram.com/reel/ABC123/
    python slack_scrape.py --username <creator> --file links.txt
    python slack_scrape.py --username <creator> --file links.txt --niche "Example Niche"
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.scraper import InstagramScraper, ScraperError, extract_audio_url
from shared.transcriber import transcribe_audio_url as transcribe
from shared.heading_generator import generate_heading

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"


def extract_shortcode(url_or_shortcode: str) -> str | None:
    """Extract Instagram shortcode from a URL or return as-is if already a shortcode."""
    match = re.search(r'instagram\.com/(?:reel|p)/([A-Za-z0-9_-]+)', url_or_shortcode)
    if match:
        return match.group(1)

    stripped = url_or_shortcode.strip().strip("/")
    if re.match(r'^[A-Za-z0-9_-]+$', stripped) and len(stripped) > 5:
        return stripped

    return None


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def load_known_creators() -> list[str]:
    """Load all known creator usernames from the Pages to scrape directory."""
    creators = []
    pages_dir = BASE_DIR / "Pages to scrape"
    if not pages_dir.exists():
        return creators

    for folder in pages_dir.iterdir():
        if not folder.is_dir():
            continue
        creators_file = folder / f"{folder.name} Creators.txt"
        if creators_file.exists():
            lines = creators_file.read_text(encoding="utf-8").strip().splitlines()
            for line in lines:
                username = line.strip().lstrip("@")
                if username and not username.startswith("#"):
                    creators.append(username)
    return creators


def main():
    parser = argparse.ArgumentParser(
        description="Scrape specific Instagram reels by URL/shortcode."
    )
    parser.add_argument("urls", nargs="*", help="Instagram reel URLs or shortcodes")
    parser.add_argument("--file", "-f", help="File containing URLs, one per line")
    parser.add_argument("--output", "-o", help="Output file path")
    parser.add_argument("--niche", "-n", default="general",
                        help="Niche name for output filename (default: general)")
    parser.add_argument("--username", "-u",
                        help="Instagram username to search reels from (required if /postdetail/ API is down)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger(__name__)

    # Collect all URLs/shortcodes
    inputs = list(args.urls) if args.urls else []
    if args.file:
        file_path = Path(args.file)
        if file_path.exists():
            lines = file_path.read_text(encoding="utf-8").strip().splitlines()
            inputs.extend(line.strip() for line in lines if line.strip())
        else:
            logger.error(f"File not found: {args.file}")
            sys.exit(1)

    if not inputs:
        print("No reel URLs or shortcodes provided.", file=sys.stderr)
        print("Usage: python slack_scrape.py --username <creator> <url1> <url2> ...")
        print("   or: python slack_scrape.py --username <creator> --file links.txt")
        sys.exit(1)

    # Extract shortcodes, dedup
    seen = set()
    shortcodes = []
    for inp in inputs:
        sc = extract_shortcode(inp)
        if sc and sc not in seen:
            shortcodes.append(sc)
            seen.add(sc)
        elif not sc:
            logger.warning(f"Could not extract shortcode from: {inp}")

    if not shortcodes:
        print("No valid shortcodes found in input.", file=sys.stderr)
        sys.exit(1)

    logger.info(f"Processing {len(shortcodes)} reel(s)...")

    config = load_config()
    scraper = InstagramScraper(
        api_key=config["rapidapi_key"],
        api_host=config["rapidapi_host"],
        max_retries=config.get("max_retries", 4),
        rate_limit_delay=config.get("rate_limit_delay", 0.5),
        fallback_api_key=config.get("rapidapi_key_fallback"),
    )

    # Strategy: /postdetail/ gives us the media URLs and caption, but it reports
    # Instagram-ONLY plays and never returns saves. So fetch there first, then enrich
    # views + saves from /userreels/, whose top-level play_count is already IG+FB
    # collectively. Metrics are settled before the slow transcription pass.
    outputs = []
    failed_shortcodes = set()
    reels = []

    # Fetch each reel via /postdetail/ (1 API call each)
    for i, sc in enumerate(shortcodes, 1):
        logger.info(f"[{i}/{len(shortcodes)}] Fetching /postdetail/ for: {sc}")
        try:
            reel = scraper.get_reel_by_shortcode_direct(sc)
            if reel and reel.get("shortcode"):
                reels.append(reel)
            else:
                logger.warning(f"  No data returned for {sc}")
                failed_shortcodes.add(sc)
        except (ScraperError, Exception) as e:
            logger.warning(f"  /postdetail/ failed for {sc}: {e}")
            failed_shortcodes.add(sc)

    if failed_shortcodes:
        logger.warning(f"Could not fetch {len(failed_shortcodes)} reel(s): {failed_shortcodes}")

    # Upgrade Instagram-only plays -> collective play_count, and fill in saves.
    _enrich_from_user_reels(scraper, reels, fallback_username=args.username)

    # Transcribe + generate headings (slow — do it only after metrics are settled)
    for reel in reels:
        outputs.append(_process_reel(reel, config))

    if not outputs:
        print("No reels were successfully processed.", file=sys.stderr)
        sys.exit(1)

    # Join with separator
    separator = f"\n{'=' * 60}\n\n"
    content = separator.join(outputs)

    # Write to file
    today = datetime.now().strftime("%Y-%m-%d")
    niche_slug = args.niche.lower().replace(" ", "_")
    filename = f"scripts_{today}_{niche_slug}_creators.txt"

    if args.output:
        output_path = Path(args.output)
    else:
        niche_dir = BASE_DIR / "Pages to scrape" / args.niche / f"{args.niche} Video ideas"
        if niche_dir.exists():
            output_path = niche_dir / filename
        else:
            output_path = BASE_DIR / filename

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    logger.info(f"Output written to: {output_path}")

    print(f"\n{'=' * 60}")
    print(f"  {len(outputs)} reel(s) processed. Output: {output_path}")
    print(f"{'=' * 60}\n")
    print(content)


def _enrich_from_user_reels(scraper, reels: list, fallback_username: str | None = None,
                            max_pages: int = 5) -> None:
    """Upgrade the metrics that /postdetail/ cannot provide.

    /postdetail/ reports `metrics.play_count`, which is Instagram-ONLY — that endpoint
    carries no Facebook data at all — and it never returns `save_count`. /userreels/
    returns the top-level `play_count`, which is already Instagram + Facebook collectively,
    plus real saves. So we group the reels by creator, do ONE /userreels/ lookup per
    creator, and overwrite views + saves.

    Mutates `reels` in place. On any failure the reel keeps its Instagram-only figure and
    we log a warning, rather than silently reporting a number that is too low.
    """
    logger = logging.getLogger(__name__)
    if not reels:
        return

    by_user: dict[str, set] = {}
    for r in reels:
        user = r.get("username") or fallback_username
        if not user:
            logger.warning(f"  {r['shortcode']}: no creator resolved; "
                           f"views stay Instagram-only ({r['combined_views']:,}).")
            continue
        by_user.setdefault(user, set()).add(r["shortcode"])

    by_code = {r["shortcode"]: r for r in reels}

    for user, codes in by_user.items():
        logger.info(f"Enriching {len(codes)} reel(s) from @{user} via /userreels/ "
                    f"(collective IG+FB views + saves)...")
        try:
            found = scraper.find_reel_in_user_reels(user, codes, max_pages=max_pages)
        except Exception as e:
            logger.warning(f"  /userreels/ lookup failed for @{user}: {e}. "
                           f"Views stay Instagram-only.")
            continue

        for code in codes:
            reel = by_code[code]
            match = found.get(code)
            if not match:
                logger.warning(f"  {code}: not found in @{user}'s first {max_pages} reel pages; "
                               f"views stay Instagram-only ({reel['combined_views']:,}), "
                               f"saves unavailable.")
                continue
            before = reel["combined_views"]
            reel["combined_views"] = match["combined_views"]
            reel["ig_views"] = match["ig_views"]
            reel["fb_views"] = match["fb_views"]
            for key in ("likes", "comments", "saves", "shares"):
                if match.get(key, 0) > reel.get(key, 0):
                    reel[key] = match[key]
            if match["combined_views"] != before:
                logger.info(f"  {code}: views {before:,} -> {reel['combined_views']:,} "
                            f"(ig={reel['ig_views']:,} + fb={reel['fb_views']:,}), "
                            f"saves={reel['saves']:,}")


def _process_reel(reel: dict, config: dict) -> str:
    """Transcribe + generate heading + format output for a single reel."""
    # Transcribe — try audio_url first, fall back to video_url if Whisper returns empty
    # (audio_url can be music-only track; video_url has combined voice + music)
    transcript = None
    whisper_model = config.get("whisper_model", "medium")
    if reel.get("audio_url"):
        transcript = transcribe(reel["audio_url"], model_size=whisper_model)
    if not transcript and reel.get("video_url"):
        logging.getLogger(__name__).info("  Audio track returned empty, trying video URL...")
        transcript = transcribe(reel["video_url"], model_size=whisper_model)
    if not transcript:
        transcript = reel.get("transcript", "")

    # Generate heading
    try:
        heading = generate_heading(transcript)
    except Exception as e:
        logging.getLogger(__name__).warning(
            f"Heading generation failed ({e}). Using placeholder heading."
        )
        heading = "[Heading TBD]"

    # Format output
    views = f"{reel['combined_views']:,}"
    likes = f"{reel['likes']:,}"
    saves = f"{reel['saves']:,}"
    shares = f"{reel['shares']:,}"

    return (
        f"{heading}\n"
        f"{views} Views | {likes} Likes | {saves} Saves | {shares} Shares\n"
        f"{reel['link']}\n"
        f"{transcript or '[No transcript]'}\n"
    )


if __name__ == "__main__":
    main()
