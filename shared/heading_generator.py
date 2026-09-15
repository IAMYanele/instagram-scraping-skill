"""
Generate brief video headings using Claude API.
"""

import logging
import os
import re

import anthropic

logger = logging.getLogger(__name__)

_client = None


def _get_client(api_key: str | None = None) -> anthropic.Anthropic:
    """Get or create Anthropic client."""
    global _client
    if _client is None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("No Anthropic API key found. Set ANTHROPIC_API_KEY env var.")
        _client = anthropic.Anthropic(api_key=key)
    return _client


def _fallback_heading(transcript: str) -> str:
    """Generate a simple heading from the first sentence of the transcript."""
    text = transcript.strip()
    # Take the first sentence
    for sep in (".', '!", "?"):
        idx = text.find(sep)
        if 0 < idx < 80:
            return text[:idx + 1]
    # No sentence break found, take first ~60 chars at a word boundary
    if len(text) <= 60:
        return text
    cut = text[:60].rfind(" ")
    if cut > 20:
        return text[:cut]
    return text[:60]


def generate_heading(transcript: str, api_key: str | None = None) -> str:
    """
    Generate a brief heading/summary for a reel based on its transcript.
    Returns the heading string.
    When SKIP_HEADING_API env var is set, returns a placeholder to be post-processed.
    """
    # Guard near-empty transcripts (e.g. a lone "." from a no-speech reel) — fewer than
    # 5 alphanumeric chars isn't enough to title, and sending it to Claude returns a
    # "I need an actual transcript" error string instead of a heading.
    if not transcript or len(re.sub(r"[^A-Za-z0-9]+", "", transcript)) < 5:
        return "[No transcript available]"

    if os.environ.get("SKIP_HEADING_API"):
        return "[HEADING_TODO] " + transcript[:60].strip() + "..."

    client = _get_client(api_key)

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    "Generate a brief, catchy heading (max 10 words) that summarizes "
                    "the main topic of this Instagram reel transcript. "
                    "Return ONLY the heading, nothing else.\n\n"
                    f"Transcript: {transcript[:2000]}"
                )
            }]
        )
        heading = message.content[0].text.strip()
        logger.info(f"Generated heading: {heading}")
        return heading

    except Exception as e:
        logger.error(f"Heading generation failed: {e}")
        return _fallback_heading(transcript)
