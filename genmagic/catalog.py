"""The model dropdowns, read from GenMagic's live catalog.

ComfyUI asks every node for its inputs when the editor loads, so the list is fetched once
(one public, keyless request), kept for ten minutes, and replaced by a short built-in list
when GenMagic cannot be reached, so the editor never waits on the network for long.
A model that is not in the list (a saved workflow naming a model added or retired since)
still runs: the nodes skip ComfyUI's list check and GenMagic answers for that id itself.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .client import GenMagicClient, GenMagicError

AUTO = "auto"
TTL_SECONDS = 600
RETRY_SECONDS = 60

# Used only while the live catalog cannot be read.
FALLBACK: dict[str, list[str]] = {
    "image": ["google/gemini-nano-banana-2.1", "openai/gpt-image-2", "black-forest-labs/flux-3-image", "bytedance-seed/seedream-5-0-flash", "recraft/recraft-v4.1-flash"],
    "video": ["google/veo-3.1", "google/veo-3.1-fast", "kwaivgi/kling-v3.0-pro", "bytedance/seedance-2.0-fast", "minimax/hailuo-3"],
    "speech": ["openai/gpt-audio-mini", "openai/gpt-audio"],
    "music": ["google/lyria-3-clip-preview", "google/lyria-3-pro-preview"],
    "text": ["anthropic/claude-sonnet-5.5", "openai/gpt-6.1-sol", "google/gemini-3.5-flash", "anthropic/claude-haiku-5.5"],
}

_lock = threading.Lock()
_state: dict[str, Any] = {"models": None, "at": 0.0, "failed_at": 0.0}


def _kind(model: dict[str, Any]) -> str | None:
    category = model.get("category")
    if category == "audio":
        return model.get("audio_kind") if model.get("audio_kind") in ("speech", "music") else None
    if category == "video":
        # Only models that make a clip from a prompt (optionally anchored on a frame).
        return "video" if (model.get("capabilities") or {}).get("text_to_video", True) else None
    return category if category in ("image", "text") else None


def _catalog() -> list[dict[str, Any]] | None:
    now = time.monotonic()
    with _lock:
        if _state["models"] is not None and now - _state["at"] < TTL_SECONDS:
            return _state["models"]
        if _state["failed_at"] and now - _state["failed_at"] < RETRY_SECONDS:
            return _state["models"]
        try:
            models = GenMagicClient(api_key="").models_all()
        except GenMagicError as e:
            logging.warning("[GenMagic] model list unavailable (%s); using the built-in list", e.message)
            _state["failed_at"] = now
            return _state["models"]
        _state.update(models=models, at=now, failed_at=0.0)
        return models


def choices(kind: str) -> list[str]:
    """The dropdown for one node: 'auto' (GenMagic's default) then every model, newest first."""
    models = _catalog()
    if not models:
        return [AUTO, *FALLBACK[kind]]
    ids = [m["id"] for m in models if _kind(m) == kind]
    return [AUTO, *ids] if ids else [AUTO, *FALLBACK[kind]]


def entry(model_id: str) -> dict[str, Any] | None:
    """The live catalog entry for one model id, or None when unknown or unreachable."""
    for m in _catalog() or []:
        if m.get("id") == model_id:
            return m
    return None


def chosen(model: str) -> str | None:
    """The id to send, or None to let GenMagic pick its default."""
    model = (model or "").strip()
    return None if not model or model.lower() == AUTO else model
