"""
Audio transcription with automatic local fallback.

Primary path: OpenAI Whisper API (`whisper-1`) — fastest when the key has quota.
Fallback path: local `faster-whisper` (CTranslate2, CPU int8) — used automatically
when the API has no key, hits a quota/rate error, or otherwise fails. This keeps the
daily scrape producing real transcripts even when OpenAI billing is exhausted
(429 insufficient_quota), instead of silently degrading to caption-only.

The audio/video file is downloaded ONCE and reused for whichever engine runs.
"""

import logging
import os
import tempfile
import threading
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# --- local faster-whisper singleton (lazy, thread-safe) ---------------------
_LOCAL_MODEL = None
_LOCAL_MODEL_SIZE = None
_MODEL_LOCK = threading.Lock()           # guards model load AND transcription
# Once the OpenAI API reports no quota, stop calling it for the rest of the run
# (avoids a 429-retry storm on every subsequent reel).
_OPENAI_QUOTA_EXHAUSTED = False


def _download(url: str) -> str | None:
    """Download audio/video URL to a temp file. Returns path or None."""
    logger.info(f"Downloading media from {url[:80]}...")
    try:
        resp = requests.get(url, timeout=90, stream=True)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Media download failed: {e}")
        return None

    ext = ".mp4"
    ctype = resp.headers.get("content-type", "")
    if "audio/mpeg" in ctype:
        ext = ".mp3"
    elif "audio/mp4" in ctype or "audio/m4a" in ctype:
        ext = ".m4a"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        for chunk in resp.iter_content(chunk_size=8192):
            tmp.write(chunk)
        return tmp.name


def _transcribe_openai(path: str, task: str = "translate") -> str | None:
    """Try the OpenAI Whisper API. Returns text, or None on failure.
    task='translate' uses the translations endpoint (always English output);
    'transcribe' keeps the source language.
    Sets the module quota flag on insufficient_quota so we stop retrying the API."""
    global _OPENAI_QUOTA_EXHAUSTED
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, max_retries=0)  # fail fast; we have a fallback
        logger.info(f"Transcribing with OpenAI Whisper API (task={task})...")
        with open(path, "rb") as f:
            if task == "translate":
                result = client.audio.translations.create(model="whisper-1", file=f)
            else:
                result = client.audio.transcriptions.create(model="whisper-1", file=f)
        text = (result.text or "").strip()
        return text or None
    except Exception as e:
        msg = str(e)
        if "insufficient_quota" in msg or "exceeded your current quota" in msg:
            _OPENAI_QUOTA_EXHAUSTED = True
            logger.warning("OpenAI Whisper quota exhausted — switching to local "
                           "faster-whisper for the rest of this run.")
        else:
            logger.warning(f"OpenAI Whisper failed ({msg[:120]}); trying local fallback.")
        return None


def _get_local_model(size: str):
    """Lazily load (and cache) the faster-whisper model. Returns model or None."""
    global _LOCAL_MODEL, _LOCAL_MODEL_SIZE
    with _MODEL_LOCK:
        if _LOCAL_MODEL is not None and _LOCAL_MODEL_SIZE == size:
            return _LOCAL_MODEL
        try:
            from faster_whisper import WhisperModel
        except Exception as e:
            logger.error(f"faster-whisper not available for local fallback: {e}")
            return None
        try:
            logger.info(f"Loading local faster-whisper '{size}' (cpu/int8)...")
            _LOCAL_MODEL = WhisperModel(size, device="cpu", compute_type="int8")
            _LOCAL_MODEL_SIZE = size
            logger.info("Local faster-whisper model ready.")
            return _LOCAL_MODEL
        except Exception as e:
            logger.error(f"Failed to load local faster-whisper model: {e}")
            return None


def _transcribe_local(path: str, size: str, task: str = "transcribe") -> str | None:
    """Transcribe locally with faster-whisper. task='translate' -> English output.
    Serialized via _MODEL_LOCK because faster-whisper isn't safe for concurrent calls."""
    model = _get_local_model(size)
    if model is None:
        return None
    try:
        with _MODEL_LOCK:
            logger.info(f"Transcribing locally with faster-whisper ('{size}', task={task})...")
            segments, info = model.transcribe(path, beam_size=5, vad_filter=True, task=task)
            text = " ".join(s.text.strip() for s in segments).strip()
        if text:
            logger.info(f"Local transcription complete ({len(text)} chars, lang={info.language}).")
            return text
        logger.warning("Local faster-whisper returned empty transcript.")
        return None
    except Exception as e:
        logger.error(f"Local transcription failed: {e}")
        return None


def transcribe_audio_url(audio_url: str, model_size: str = "small",
                         task: str = "translate") -> str | None:
    """
    Download audio/video from URL and transcribe it.

    Tries the OpenAI Whisper API first (fast), then automatically falls back to a
    local faster-whisper model if the API is unavailable / out of quota / errors.
    `model_size` selects the local model size (the API ignores it). `task` defaults
    to 'translate' (always English output — the scripting pipeline is English-only,
    so non-English source reels come back usable); pass 'transcribe' to keep the
    source language. Both engines honor `task`. Returns text, or None if both fail.
    """
    if not audio_url:
        return None

    tmp_path = None
    try:
        tmp_path = _download(audio_url)
        if not tmp_path:
            return None

        # Primary: OpenAI API (skipped once we know the quota is gone).
        if not _OPENAI_QUOTA_EXHAUSTED:
            text = _transcribe_openai(tmp_path, task=task)
            if text:
                logger.info(f"Transcription complete via OpenAI ({len(text)} chars).")
                return text

        # Fallback: local faster-whisper on the already-downloaded file.
        return _transcribe_local(tmp_path, model_size, task=task)

    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
