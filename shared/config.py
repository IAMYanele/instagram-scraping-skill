"""Centralized configuration — loads API keys from scraping-agent/config.json and env vars."""

import json
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT / "scraping-agent" / "config.json"

_raw = {}
if _CONFIG_PATH.exists():
    _raw = json.loads(_CONFIG_PATH.read_text())

# RapidAPI
RAPIDAPI_KEY: str = os.getenv("RAPIDAPI_KEY", _raw.get("rapidapi_key", ""))
RAPIDAPI_KEY_FALLBACK: str = os.getenv("RAPIDAPI_KEY_FALLBACK", _raw.get("rapidapi_key_fallback", ""))
RAPIDAPI_HOST: str = os.getenv("RAPIDAPI_HOST", _raw.get("rapidapi_host", "instagram-scraper-20251.p.rapidapi.com"))

# Whisper
WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", _raw.get("whisper_model", "small"))

# Claude / OpenAI
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

# Scraper settings
RATE_LIMIT_DELAY: float = float(_raw.get("rate_limit_delay", 0.5))
MAX_RETRIES: int = int(_raw.get("max_retries", 4))
INITIAL_LOOKBACK_DAYS: int = int(_raw.get("initial_lookback_days", 30))


def get_scraper_kwargs() -> dict:
    """Return kwargs ready to pass to InstagramScraper(...)."""
    return {
        "api_key": RAPIDAPI_KEY,
        "api_host": RAPIDAPI_HOST,
        "max_retries": MAX_RETRIES,
        "rate_limit_delay": RATE_LIMIT_DELAY,
        "fallback_api_key": RAPIDAPI_KEY_FALLBACK or None,
    }
