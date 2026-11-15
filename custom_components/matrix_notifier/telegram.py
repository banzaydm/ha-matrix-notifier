"""Telegram Bot API client for Matrix Notifier.

Minimal client for the four methods we need: getMe, sendMessage,
sendPhoto, sendVideo. The Telegram Bot API is backward-compatible by
policy (methods/fields are only added, never changed), so this stays
stable across API releases.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

_LOGGER = logging.getLogger(__name__)

# Telegram hard limits
_TEXT_LIMIT = 4096          # sendMessage text length
_CAPTION_LIMIT = 1024       # photo/video caption length
_TIMEOUT = 30               # seconds per request


class TelegramError(Exception):
    """Telegram Bot API error."""


class TelegramClient:
    """Blocking Telegram Bot API client (run in executor)."""

    def __init__(self, bot_token: str, verify_ssl: bool = True) -> None:
        self._token = bot_token.strip()
        self._verify = verify_ssl
        self._base = f"https://api.telegram.org/bot{self._token}"
        self._bot_username: str | None = None

    @property
    def bot_username(self) -> str | None:
        return self._bot_username

    # ------------------------------------------------------------------ #
    # low-level
    # ------------------------------------------------------------------ #
    def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """POST to api.telegram.org, return result or raise TelegramError."""
        try:
            resp = requests.post(
                f"{self._base}/{method}",
                timeout=_TIMEOUT,
                verify=self._verify,
                **kwargs,
            )
        except requests.RequestException as err:
            raise TelegramError(f"network error: {err}") from err

        try:
            data = resp.json()
        except ValueError as err:
            raise TelegramError(
                f"non-JSON response (HTTP {resp.status_code})"
            ) from err

        if not data.get("ok"):
            desc = data.get("description", resp.text[:200])
            error_code = data.get("error_code")
            raise TelegramError(f"HTTP {error_code}: {desc}")
        return data["result"]

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def get_me(self) -> str:
        """Validate the token; return bot username (e.g. 'my_bot')."""
        result = self._call("getMe")
        self._bot_username = result.get("username")
        return self._bot_username or ""

    def send_text(self, chat_id: str, text: str) -> int:
        """Send a text message (split at 4096 chars). Returns message id."""
        last_id = 0
        chunks = [
            text[i : i + _TEXT_LIMIT] for i in range(0, len(text), _TEXT_LIMIT)
        ] or [text]
        for chunk in chunks:
            result = self._call(
                "sendMessage",
                data={"chat_id": chat_id, "text": chunk},
            )
            last_id = result.get("message_id", last_id)
        return last_id

    def send_photo(
        self, chat_id: str, photo: bytes, caption: str = ""
    ) -> int:
        """Send a photo (JPEG bytes). Returns message id."""
        result = self._call(
            "sendPhoto",
            data={"chat_id": chat_id},
            files={"photo": ("snapshot.jpg", photo, "image/jpeg")},
            **({"caption": caption[:_CAPTION_LIMIT]} if caption else {}),
        )
        return result.get("message_id", 0)

    def send_video(
        self,
        chat_id: str,
        video: bytes,
        caption: str = "",
        duration: int = 0,
        width: int | None = None,
        height: int | None = None,
    ) -> int:
        """Send an mp4 video (≤50 MB). Returns message id."""
        data: dict[str, Any] = {"chat_id": chat_id, "supports_streaming": "true"}
        if duration:
            data["duration"] = str(int(duration))
        if width:
            data["width"] = str(int(width))
        if height:
            data["height"] = str(int(height))
        if caption:
            data["caption"] = caption[:_CAPTION_LIMIT]
        result = self._call(
            "sendVideo",
            data=data,
            files={"video": ("recording.mp4", video, "video/mp4")},
        )
        return result.get("message_id", 0)


def parse_chat_ids(raw: Any) -> list[str]:
    """Normalize chat ids from list/comma-string into a clean list.

    Accepts: ["-1001", " 2 "], "-1001, 2", "@channel",123.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = str(raw).split(",")
    out: list[str] = []
    for item in items:
        value = str(item).strip()
        if value and value not in out:
            out.append(value)
    return out
