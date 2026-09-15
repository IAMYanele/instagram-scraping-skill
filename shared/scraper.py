"""
Instagram Reels Scraper - Core API logic.
Refactored from scripting agent/get_videos.py.
Uses RapidAPI "Instagram Scraper 2025" by DavidGelling.
"""

import logging
import re
import time

import requests

logger = logging.getLogger(__name__)


class ScraperError(Exception):
    """Raised when API calls fail after all retries."""


def _deep_get(obj: dict, *paths):
    """Try multiple dot-separated paths and return the first match."""
    for path in paths:
        current = obj
        for key in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current is not None:
            return current
    return None


def _extract_items(data: dict) -> list:
    """Try multiple common response structures to find the reels list."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # data.data.items
        inner = data.get("data")
        if isinstance(inner, dict):
            items = inner.get("items")
            if isinstance(items, list):
                return items
        # data.items
        items = data.get("items")
        if isinstance(items, list):
            return items
        # data.data as list
        if isinstance(inner, list):
            return inner
    return []


def _extract_pagination(data: dict) -> str | None:
    """Try to find a pagination cursor in the response."""
    if not isinstance(data, dict):
        return None
    # Check for boolean more_available/has_next_page flags
    for flag in ("more_available", "has_next_page"):
        val = data.get(flag)
        if val is False:
            return None
    # Direct pagination_token
    for key in ("pagination_token", "next_max_id", "end_cursor", "next_cursor"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    # Nested in data
    inner = data.get("data")
    if isinstance(inner, dict):
        return _extract_pagination(inner)
    return None


def _to_int(val) -> int:
    """Safely convert a value to int."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def parse_reel(item: dict) -> dict:
    """Extract the fields we care about from a reel item."""
    # Some responses nest under "node"
    if "node" in item and isinstance(item.get("node"), dict):
        item = item["node"]

    taken_at = _to_int(_deep_get(item, "taken_at", "taken_at_timestamp", "timestamp", "created_time"))
    shortcode = _deep_get(item, "code", "shortcode", "short_code") or ""
    media_id = _deep_get(item, "id", "pk", "media_id") or ""
    link = f"https://www.instagram.com/reel/{shortcode}/" if shortcode else ""
    # Creator handle. Needed to pivot from /postdetail/ (Instagram-only plays, no saves)
    # back to /userreels/, which carries the collective play_count and save_count.
    username = str(_deep_get(item, "user.username", "owner.username", "username") or "")

    # NOTE: top-level `play_count` / `total_play_count` is ALREADY Instagram + Facebook
    # COMBINED (verified: play_count 263,427 == ig 67,388 + fb 196,039). ig_play_count /
    # fb_play_count are per-platform splits kept for reference only and must NOT be summed
    # on top of the combined total. `metrics.play_count` (from /postdetail/) is IG-ONLY, so
    # it belongs in the IG lookup, never the combined lookup. Only fall back to ig+fb when
    # no combined total exists.
    ig_views = _to_int(_deep_get(item, "ig_play_count", "video_play_count", "video_view_count",
                                  "view_count", "metrics.ig_play_count", "metrics.play_count"))
    fb_views = _to_int(_deep_get(item, "fb_play_count", "fb_view_count",
                                  "metrics.fb_play_count"))
    combined_views = _to_int(_deep_get(item, "play_count", "total_play_count"))
    if not combined_views:
        combined_views = ig_views + fb_views

    likes = _to_int(_deep_get(item, "like_count", "likes", "edge_media_preview_like.count",
                               "metrics.like_count"))
    comments = _to_int(_deep_get(item, "comment_count", "comments", "edge_media_to_comment.count",
                                  "metrics.comment_count"))
    saves = _to_int(_deep_get(item, "save_count", "saved_count", "saves",
                               "metrics.save_count"))
    shares = _to_int(_deep_get(item, "share_count", "reshare_count", "shares", "reshares_count",
                                "metrics.share_count"))

    caption_raw = _deep_get(item, "caption.text", "caption",
                            "edge_media_to_caption.edges.0.node.text", "text")
    if isinstance(caption_raw, dict):
        caption_raw = caption_raw.get("text", "")
    transcript = str(caption_raw or "")

    # Also check for actual transcript in clips_metadata
    cm_transcript = _deep_get(item, "transcript", "clips_metadata.transcript",
                               "accessibility_caption")
    if cm_transcript and len(str(cm_transcript)) > len(transcript):
        transcript = str(cm_transcript)

    return {
        "shortcode": shortcode,
        "taken_at": taken_at,
        "link": link,
        "username": username,
        "ig_views": ig_views,
        "fb_views": fb_views,
        "combined_views": combined_views,
        "likes": likes,
        "comments": comments,
        "saves": saves,
        "shares": shares,
        "transcript": transcript,
    }


def extract_audio_url(detail: dict) -> str | None:
    """Extract the progressive download audio URL from post detail response."""
    inner = detail.get("data") if isinstance(detail.get("data"), dict) else detail
    url = _deep_get(inner,
                    "clips_metadata.original_sound_info.progressive_download_url",
                    "clips_metadata.music_info.music_asset_info.progressive_download_url",
                    "video_versions.0.url")
    return url


def extract_video_url(detail: dict) -> str | None:
    """Extract the video URL (has combined voice + music audio track).
    Use as fallback when audio_url returns music-only and Whisper gets no speech."""
    inner = detail.get("data") if isinstance(detail.get("data"), dict) else detail
    url = _deep_get(inner, "video_url", "video_versions.0.url")
    return url


def _merge_detail(base: dict, detail: dict):
    """Merge detail metrics into base reel, preferring higher values."""
    for key in ("ig_views", "fb_views", "likes", "comments", "saves", "shares"):
        if detail.get(key, 0) > base.get(key, 0):
            base[key] = detail[key]
    # Both base and detail already carry a correct (collective) combined_views; take the
    # larger rather than re-deriving ig+fb, which would double-count Facebook.
    base["combined_views"] = max(base.get("combined_views", 0), detail.get("combined_views", 0))
    if detail.get("transcript") and len(detail.get("transcript", "")) > len(base.get("transcript", "")):
        base["transcript"] = detail["transcript"]


def _resolve_username_from_shortcode(shortcode: str) -> str | None:
    """Try to resolve the Instagram username from a reel's shortcode
    by fetching the Instagram page and extracting the username from meta tags."""
    try:
        url = f"https://www.instagram.com/reel/{shortcode}/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=10)
        text = resp.text
        match = re.search(r'"username"\s*:\s*"([^"]+)"', text)
        if match and len(match.group(1)) > 0:
            return match.group(1)
    except Exception as e:
        logger.warning(f"Failed to resolve username from Instagram page: {e}")
    return None


class InstagramScraper:
    api_key: str
    api_host: str
    max_retries: int
    rate_limit_delay: float

    def __init__(self, api_key: str, api_host: str, max_retries: int = 4,
                 rate_limit_delay: float = 0.5, fallback_api_key: str = None):
        self.api_keys = [api_key]
        if fallback_api_key:
            self.api_keys.append(fallback_api_key)
        self.current_key_idx = 0
        self.api_host = api_host
        self.base_url = f"https://{api_host}"
        self.headers = {
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": api_host,
        }
        self.max_retries = max_retries
        self.rate_limit_delay = rate_limit_delay

    def _try_switch_api_key(self) -> bool:
        """Switch to the next available API key. Returns True if switched."""
        if self.current_key_idx + 1 < len(self.api_keys):
            self.current_key_idx += 1
            self.headers["x-rapidapi-key"] = self.api_keys[self.current_key_idx]
            logger.info(f"Switched to fallback API key (key #{self.current_key_idx + 1})")
            return True
        return False

    def api_get(self, endpoint: str, params: dict) -> dict:
        """Make a GET request to the API with retry logic and key fallback."""
        url = self.base_url + endpoint
        for attempt in range(self.max_retries):
            try:
                r = requests.get(url, headers=self.headers, params=params, timeout=25)
                if r.status_code == 429:
                    if self._try_switch_api_key():
                        logger.info("Rate limited — switching to fallback key and retrying immediately...")
                        continue
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"Rate limited. Waiting {wait}s before retry...")
                    time.sleep(wait)
                    continue
                if r.status_code == 200:
                    body = r.json()
                    quota_msg = body.get("message", "") if isinstance(body, dict) else ""
                    if "exceeded" in quota_msg.lower() and "quota" in quota_msg.lower():
                        logger.warning("API quota exceeded on current key.")
                        if self._try_switch_api_key():
                            continue
                    return body
                if r.status_code >= 400:
                    wait = 2 ** attempt
                    logger.warning(f"Request failed ({r.status_code}). Retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.exceptions.RequestException as e:
                if attempt < self.max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"Request failed ({e}). Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    raise ScraperError(f"API request failed after {self.max_retries} attempts: {e}")
        raise ScraperError(f"API request failed after {self.max_retries} attempts")

    def fetch_user_reels(self, username: str, since_timestamp: int = 0,
                         max_pages: int = 20) -> list[dict]:
        """Fetch reels pages for a user, following pagination.
        If since_timestamp > 0, stop paginating once we hit older reels.
        max_pages caps total pages to prevent infinite pagination loops."""
        all_items = []
        seen_codes = set()
        pagination_token = None

        for page in range(1, max_pages + 1):
            logger.info(f"  Fetching reels page {page} for @{username}...")
            params = {"username_or_id": username}
            if pagination_token:
                params["pagination_token"] = pagination_token

            data = self.api_get("/userreels/", params)
            items = _extract_items(data)

            if not items:
                if page == 1:
                    logger.warning(f"No reels found for @{username}.")
                break

            # Dedup by shortcode within this page
            new_items = []
            page_all_dupes = True
            for item in items:
                node = item.get("node", item) if isinstance(item, dict) else item
                code = _deep_get(node, "code", "shortcode") or ""
                if code and code not in seen_codes:
                    seen_codes.add(code)
                    new_items.append(item)
                    page_all_dupes = False

            if page_all_dupes and page > 1:
                logger.info(f"  Page {page} returned only duplicates, stopping pagination.")
                break

            all_items.extend(new_items)

            # Check if we've gone past the cutoff (but DON'T stop early -
            # items are NOT in strict chronological order)
            if since_timestamp > 0:
                old_count = sum(1 for it in new_items
                                if _to_int(_deep_get(it.get("node", it) if isinstance(it, dict) else it,
                                                     "taken_at", "taken_at_timestamp", "timestamp"))
                                < since_timestamp)
                if old_count == len(new_items):
                    logger.info("  Reached reels older than cutoff, stopping pagination.")
                    break

            pagination_token = _extract_pagination(data)
            if not pagination_token:
                break

            if page >= max_pages:
                logger.info(f"  Hit max pages limit ({max_pages}) for @{username}")
                break

            time.sleep(self.rate_limit_delay)

        return all_items

    def fetch_post_detail(self, shortcode: str) -> dict:
        """Fetch detailed metrics for a single post/reel."""
        data = self.api_get("/postdetail/", {"code_or_url": shortcode})
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            return data["data"]
        return data if isinstance(data, dict) else {}

    def get_reels_since(self, username: str, since_timestamp: int) -> list[dict]:
        """Fetch reels for a user, filtered to those newer than since_timestamp.
        Returns list of parsed reel dicts, each augmented with audio_url + video_url."""
        raw_items = self.fetch_user_reels(username, since_timestamp)
        results = []
        for item in raw_items:
            reel = parse_reel(item)
            if reel["taken_at"] >= since_timestamp:
                reel["audio_url"] = extract_audio_url(item)
                reel["video_url"] = extract_video_url(item)
                results.append(reel)
        # Sort by views descending
        results.sort(key=lambda r: r["combined_views"], reverse=True)
        return results

    def find_reel_in_user_reels(self, username: str, target_shortcodes: set,
                                 max_pages: int = 15) -> dict:
        """Search through a user's reels page by page, returning matches.
        Stops pagination as soon as all targets are found.
        Returns {shortcode: parsed_reel_dict} for matches found."""
        found = {}
        seen_codes = set()
        pagination_token = None

        for page in range(1, max_pages + 1):
            logger.info(f"  Searching page {page} of @{username}'s reels...")
            params = {"username_or_id": username}
            if pagination_token:
                params["pagination_token"] = pagination_token

            data = self.api_get("/userreels/", params)
            items = _extract_items(data)
            if not items:
                break

            page_all_dupes = True
            for item in items:
                reel = parse_reel(item)
                code = reel["shortcode"]
                if code and code not in seen_codes:
                    seen_codes.add(code)
                    page_all_dupes = False
                if code in target_shortcodes and code not in found:
                    reel["audio_url"] = extract_audio_url(item)
                    found[code] = reel
                    logger.info(f"  Found: {code} ({reel['combined_views']:,} views)")

            if len(found) == len(target_shortcodes):
                break

            if page_all_dupes and page > 1:
                logger.info(f"  Page {page} returned only duplicates, stopping.")
                break

            pagination_token = _extract_pagination(data)
            if not pagination_token:
                break

            time.sleep(self.rate_limit_delay)

        return found

    def get_reel_by_shortcode_direct(self, shortcode: str) -> dict:
        """Try to fetch a reel via /postdetail/ only. May return empty data."""
        detail = self.fetch_post_detail(shortcode)
        reel = parse_reel(detail)
        reel["audio_url"] = extract_audio_url(detail)
        reel["video_url"] = extract_video_url(detail)
        return reel

    def get_reel_by_shortcode(self, shortcode: str) -> dict:
        """Fetch a single reel by shortcode, with audio URL.
        Tries /postdetail/ first, falls back to resolving username from Instagram
        page and searching /userreels/ for the matching reel."""
        # Try direct fetch first
        try:
            detail = self.fetch_post_detail(shortcode)
            reel = parse_reel(detail)
            reel["audio_url"] = extract_audio_url(detail)
            if reel.get("combined_views") or reel.get("likes") or reel.get("audio_url"):
                return reel
            logger.info(f"/postdetail/ returned empty data for {shortcode}, trying fallback...")
        except ScraperError:
            logger.info(f"/postdetail/ failed for {shortcode}")

        # Fallback: resolve username and search their reels
        username = _resolve_username_from_shortcode(shortcode)
        if not username:
            raise ScraperError(f"Could not resolve username for shortcode {shortcode}")

        logger.info(f"Resolved username @{username} for shortcode {shortcode}")
        raw_items = self.fetch_user_reels(username)
        for item in raw_items:
            reel = parse_reel(item)
            if reel["shortcode"] == shortcode:
                reel["audio_url"] = extract_audio_url(item)
                return reel

        raise ScraperError(f"Reel {shortcode} not found in @{username}'s reels")
