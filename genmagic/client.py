"""The one HTTP client every GenMagic node uses.

It speaks GenMagic's public REST API (https://genmagic.co/openapi.json): bearer
authentication with the user's GenMagic API key, the metering headers every generation
returns (X-Cost-Cents, X-Credits-Remaining), and the OpenAI-style error body
({"error": {"message", "code"}}). Each request names this pack in the X-GenMagic-Client
header, so the key owner's usage through ComfyUI can be told apart from their other API use.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import requests

VERSION = "1.0.0"
BASE_URL = os.environ.get("GENMAGIC_BASE_URL", "https://genmagic.co").rstrip("/")
CLIENT_HEADER = f"comfyui/{VERSION}"
USER_AGENT = f"comfyui-genmagic/{VERSION}"

UTM = "utm_source=comfyui&utm_medium=integration&utm_campaign=comfy-registry"
KEY_URL = f"https://genmagic.co/get-api-key?{UTM}"
PRICING_URL = f"https://genmagic.co/pricing?{UTM}"

# Hosts GenMagic serves generated media from. Media is downloaded from these only.
MEDIA_HOSTS = ("genmagic.co", "tlwdqzfibvgektzgdfqa.supabase.co")
MAX_MEDIA_BYTES = 200 * 1024 * 1024

SETTING_ID = "GenMagic.APIKey"


class GenMagicError(Exception):
    """A GenMagic API error with the HTTP status and the API's own error code."""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(self.user_message())

    def user_message(self) -> str:
        if self.code == "missing_api_key":
            return (
                "GenMagic needs an API key. Get one at "
                f"{KEY_URL}, then paste it in Settings > GenMagic > API key "
                "(or set the GENMAGIC_API_KEY environment variable) and run again."
            )
        if self.status == 401:
            return f"GenMagic rejected the API key ({self.message}). Check it in Settings > GenMagic, or get a new one at {KEY_URL}."
        if self.code == "email_verification_required":
            return "Verify the email address of your GenMagic account first, then run again: https://genmagic.co/verify-email"
        if self.status == 402:
            return f"Your GenMagic balance is too low for this generation ({self.message}). Add credits at {PRICING_URL}."
        if self.status == 429:
            return f"GenMagic rate limit reached ({self.message}). Wait a moment and run again."
        return f"GenMagic error {self.status} ({self.code}): {self.message}"


@dataclass
class Metering:
    """What a call cost and what is left, from GenMagic's response headers (cents)."""

    cost_cents: float | None = None
    balance_cents: float | None = None

    @classmethod
    def from_headers(cls, headers: Any) -> "Metering":
        def number(name: str) -> float | None:
            raw = headers.get(name)
            try:
                return float(raw) if raw is not None else None
            except ValueError:
                return None

        return cls(cost_cents=number("X-Cost-Cents"), balance_cents=number("X-Credits-Remaining"))

    def describe(self, model: str | None) -> str:
        parts = [f"model: {model or 'auto'}"]
        if self.cost_cents is not None:
            parts.append(f"cost: ${self.cost_cents / 100:.4f}")
        if self.balance_cents is not None:
            parts.append(f"balance: ${self.balance_cents / 100:.2f}")
        return ", ".join(parts)


def settings_key() -> str:
    """The key saved in ComfyUI's own settings (Settings > GenMagic > API key)."""
    try:
        import folder_paths  # ComfyUI's module; absent outside ComfyUI

        user_dir = folder_paths.get_user_directory()
    except Exception:
        return ""
    users = sorted(os.listdir(user_dir)) if os.path.isdir(user_dir) else []
    # Single-user ComfyUI keeps its settings under "default"; read that one first.
    for user in ["default", *(u for u in users if u != "default")]:
        path = os.path.join(user_dir, user, "comfy.settings.json")
        try:
            with open(path, encoding="utf-8") as f:
                value = json.load(f).get(SETTING_ID)
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def resolve_api_key() -> str:
    """The environment variable wins, so a server can be configured without the UI."""
    return (os.environ.get("GENMAGIC_API_KEY") or "").strip() or settings_key()


class GenMagicClient:
    def __init__(self, api_key: str | None = None, base_url: str = BASE_URL):
        self.api_key = (api_key if api_key is not None else resolve_api_key()).strip()
        self.base_url = base_url.rstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        accept: str = "application/json",
        timeout: float = 180,
        auth: bool = True,
    ) -> requests.Response:
        headers = {"Accept": accept, "X-GenMagic-Client": CLIENT_HEADER, "User-Agent": USER_AGENT}
        if auth:
            if not self.api_key:
                raise GenMagicError(401, "missing_api_key", "No API key is configured")
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            res = requests.request(
                method, f"{self.base_url}{path}", headers=headers, json=body, params=params, timeout=(10, timeout)
            )
        except requests.Timeout as e:
            raise GenMagicError(504, "timeout", "GenMagic did not answer in time") from e
        except requests.RequestException as e:
            raise GenMagicError(503, "network_error", f"Could not reach GenMagic ({e.__class__.__name__})") from e
        if res.status_code >= 400:
            raise error_from(res)
        return res

    # The model list is public catalog data: no key, no credits. Newest first.
    def models_all(self, timeout: float = 6) -> list[dict[str, Any]]:
        try:
            data = self._request("GET", "/api/v1/models", timeout=timeout, auth=False).json()
        except ValueError as e:
            raise GenMagicError(502, "bad_catalog", "GenMagic returned an unreadable model list") from e
        return [m for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]

    def text(self, body: dict[str, Any]) -> tuple[dict[str, Any], Metering]:
        res = self._request("POST", "/api/v1/text", body=body, timeout=240)
        return res.json(), Metering.from_headers(res.headers)

    def image(self, body: dict[str, Any]) -> tuple[dict[str, Any], Metering]:
        res = self._request("POST", "/api/v1/images/generations", body=body, timeout=240)
        return res.json(), Metering.from_headers(res.headers)

    def speech(self, body: dict[str, Any]) -> tuple[bytes, Metering]:
        res = self._request("POST", "/api/v1/audio/speech", body=body, accept="audio/*", timeout=240)
        return res.content, Metering.from_headers(res.headers)

    def music(self, body: dict[str, Any]) -> tuple[bytes, Metering]:
        res = self._request("POST", "/api/v1/audio/music", body=body, accept="audio/*", timeout=300)
        return res.content, Metering.from_headers(res.headers)

    def create_video(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/videos", body=body, timeout=90).json()

    def get_video(self, job_id: str) -> tuple[dict[str, Any], Metering]:
        res = self._request("GET", f"/api/v1/videos/{quote(job_id, safe='')}", timeout=90)
        return res.json(), Metering.from_headers(res.headers)

    def fetch_media(self, url: str) -> bytes:
        """The bytes of a generated file: a data URL decoded in place, or an https URL on
        GenMagic's own media hosts (never an arbitrary address)."""
        if url.startswith("data:"):
            header, _, payload = url.partition(",")
            if ";base64" not in header:
                raise GenMagicError(502, "bad_media", "GenMagic returned media in an unexpected encoding")
            try:
                return base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError) as e:
                raise GenMagicError(502, "bad_media", "GenMagic returned unreadable media") from e
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in MEDIA_HOSTS:
            raise GenMagicError(502, "bad_media", f"Refusing to download media from {parsed.hostname or 'an unknown host'}")
        for attempt in range(3):
            try:
                with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(10, 180), stream=True) as res:
                    if res.status_code != 200:
                        raise GenMagicError(res.status_code, "media_unavailable", "The generated file could not be downloaded")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in res.iter_content(chunk_size=512 * 1024):
                        size += len(chunk)
                        if size > MAX_MEDIA_BYTES:
                            raise GenMagicError(502, "media_too_large", "The generated file is larger than this node accepts")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except requests.RequestException as e:
                if attempt == 2:
                    raise GenMagicError(503, "network_error", f"Could not download the generated file ({e.__class__.__name__})") from e
                time.sleep(2 * (attempt + 1))
        raise GenMagicError(503, "network_error", "Could not download the generated file")


def error_from(res: requests.Response) -> GenMagicError:
    code, message = "http_error", f"HTTP {res.status_code}"
    try:
        err = res.json().get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or code)
            message = str(err.get("message") or message)
        elif isinstance(err, str):
            message = err
    except ValueError:
        pass
    return GenMagicError(res.status_code, code, message)
