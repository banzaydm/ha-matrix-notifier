"""Matrix Notifier client — login, token refresh, send messages."""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .const import DEFAULT_DEVICE_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Reuse one session per client
_SESSION = requests.Session()


class MatrixNotifierError(Exception):
    """Base error."""


class MatrixLoginError(MatrixNotifierError):
    """Login failed."""


class MatrixSendError(MatrixNotifierError):
    """Send failed."""


class MatrixClient:
    """Thin Matrix Client-Server API wrapper (requests-based)."""

    def __init__(
        self,
        homeserver: str,
        username: str,
        password: str,
        device_id: str = DEFAULT_DEVICE_ID,
        verify_ssl: bool = True,
    ) -> None:
        self._homeserver = homeserver.rstrip("/")
        self._username = username
        self._password = password
        self._device_id = device_id
        self._verify_ssl = verify_ssl
        self._access_token: str | None = None
        self._user_id: str | None = None

    # ------------------------------------------------------------------ #
    # auth
    # ------------------------------------------------------------------ #
    @property
    def access_token(self) -> str | None:
        return self._access_token

    @property
    def user_id(self) -> str | None:
        return self._user_id

    @property
    def logged_in(self) -> bool:
        return self._access_token is not None

    def login(self) -> str:
        """Login (or refresh) and return the access token."""
        url = f"{self._homeserver}/_matrix/client/v3/login"
        payload = {
            "identifier": {"type": "m.id.user", "user": self._username},
            "password": self._password,
            "type": "m.login.password",
            "initial_device_display_name": "Home Assistant Notifier",
            "device_id": self._device_id,
        }
        try:
            resp = _SESSION.post(
                url, json=payload, timeout=30, verify=self._verify_ssl
            )
        except requests.RequestException as err:
            raise MatrixLoginError(f"Connection error: {err}") from err

        if resp.status_code != 200:
            raise MatrixLoginError(
                f"Login failed ({resp.status_code}): {resp.text[:300]}"
            )

        data = resp.json()
        self._access_token = data["access_token"]
        self._user_id = data.get("user_id")
        _LOGGER.debug("Logged in as %s", self._user_id)
        return self._access_token

    def ensure_token(self) -> str:
        """Return a valid token, re-login if missing."""
        if not self._access_token:
            self.login()
        assert self._access_token is not None
        return self._access_token

    # ------------------------------------------------------------------ #
    # rooms
    # ------------------------------------------------------------------ #
    def get_joined_rooms(self) -> list[dict[str, str]]:
        """Return list of joined rooms: [{room_id, name}]."""
        token = self.ensure_token()
        url = f"{self._homeserver}/_matrix/client/v3/joined_rooms"
        try:
            resp = _SESSION.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                verify=self._verify_ssl,
            )
        except requests.RequestException as err:
            raise MatrixSendError(f"Connection error: {err}") from err

        if resp.status_code == 401:
            # token expired — re-login once
            token = self.login()
            resp = _SESSION.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                verify=self._verify_ssl,
            )
        if resp.status_code != 200:
            raise MatrixSendError(
                f"joined_rooms failed ({resp.status_code}): {resp.text[:300]}"
            )

        room_ids: list[str] = resp.json().get("joined_rooms", [])
        rooms: list[dict[str, str]] = []
        for rid in room_ids:
            rooms.append({"room_id": rid, "name": self._room_name(rid, token)})
        return rooms

    def get_room_members(self, room_id: str) -> list[dict[str, str]]:
        """Return joined members of a room: [{user_id, display_name, avatar_url}]."""
        token = self.ensure_token()
        url = (
            f"{self._homeserver}/_matrix/client/v3/rooms/"
            f"{room_id}/joined_members"
        )
        try:
            resp = _SESSION.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                verify=self._verify_ssl,
            )
        except requests.RequestException as err:
            raise MatrixSendError(f"Connection error: {err}") from err

        if resp.status_code == 401:
            token = self.login()
            resp = _SESSION.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                verify=self._verify_ssl,
            )
        if resp.status_code != 200:
            raise MatrixSendError(
                f"joined_members failed ({resp.status_code}): {resp.text[:300]}"
            )

        joined = resp.json().get("joined", {})
        members: list[dict[str, str]] = []
        for user_id, info in joined.items():
            members.append(
                {
                    "user_id": user_id,
                    "display_name": info.get("display_name") or user_id,
                    "avatar_url": info.get("avatar_url") or "",
                }
            )
        return members

    def _room_name(self, room_id: str, token: str) -> str:
        """Best-effort room name (falls back to room_id)."""
        try:
            url = (
                f"{self._homeserver}/_matrix/client/v3/rooms/"
                f"{room_id}/state/m.room.name"
            )
            resp = _SESSION.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
                verify=self._verify_ssl,
            )
            if resp.status_code == 200:
                return resp.json().get("name", room_id)
        except Exception:  # noqa: BLE001
            pass
        return room_id

    def create_room(self, name: str, invite: list[str] | None = None) -> str:
        """Create a new Matrix room. Returns room_id."""
        token = self.ensure_token()
        url = f"{self._homeserver}/_matrix/client/v3/createRoom"
        payload: dict = {"name": name, "visibility": "private"}
        if invite:
            payload["invite"] = invite
        try:
            resp = _SESSION.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                verify=self._verify_ssl,
            )
        except requests.RequestException as err:
            raise MatrixSendError(f"Connection error: {err}") from err

        if resp.status_code == 401:
            token = self.login()
            resp = _SESSION.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                verify=self._verify_ssl,
            )
        if resp.status_code not in (200, 201):
            raise MatrixSendError(
                f"createRoom failed ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json().get("room_id", "")

    def invite_user(self, room_id: str, user_id: str) -> bool:
        """Invite a user to a room."""
        token = self.ensure_token()
        url = (
            f"{self._homeserver}/_matrix/client/v3/rooms/"
            f"{room_id}/invite"
        )
        payload = {"user_id": user_id}
        try:
            resp = _SESSION.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
                verify=self._verify_ssl,
            )
        except requests.RequestException as err:
            raise MatrixSendError(f"Connection error: {err}") from err

        if resp.status_code == 401:
            token = self.login()
            resp = _SESSION.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
                verify=self._verify_ssl,
            )
        return resp.status_code in (200, 201)

    def kick_user(self, room_id: str, user_id: str) -> bool:
        """Kick a user from a room."""
        token = self.ensure_token()
        url = (
            f"{self._homeserver}/_matrix/client/v3/rooms/"
            f"{room_id}/kick"
        )
        payload = {"user_id": user_id}
        try:
            resp = _SESSION.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
                verify=self._verify_ssl,
            )
        except requests.RequestException as err:
            raise MatrixSendError(f"Connection error: {err}") from err

        if resp.status_code == 401:
            token = self.login()
            resp = _SESSION.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
                verify=self._verify_ssl,
            )
        return resp.status_code in (200, 201)

    # ------------------------------------------------------------------ #
    # send
    # ------------------------------------------------------------------ #
    def send_message(
        self, room_id: str, body: str, msgtype: str = "m.text"
    ) -> str:
        """Send a plain text message. Returns event_id."""
        token = self.ensure_token()
        url = (
            f"{self._homeserver}/_matrix/client/v3/rooms/"
            f"{room_id}/send/m.room.message"
        )
        payload = {"msgtype": msgtype, "body": body}
        try:
            resp = _SESSION.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                verify=self._verify_ssl,
            )
        except requests.RequestException as err:
            raise MatrixSendError(f"Connection error: {err}") from err

        # 401 → token expired, re-login and retry once
        if resp.status_code == 401:
            token = self.login()
            resp = _SESSION.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
                verify=self._verify_ssl,
            )

        if resp.status_code not in (200, 201):
            raise MatrixSendError(
                f"send failed ({resp.status_code}): {resp.text[:300]}"
            )
        event_id = resp.json().get("event_id", "")
        _LOGGER.debug("Sent to %s → %s", room_id, event_id)
        return event_id

    # ------------------------------------------------------------------ #
    # media
    # ------------------------------------------------------------------ #
    def upload_image(
        self, content: bytes, content_type: str, filename: str
    ) -> str:
        """Upload binary content to the media repo. Returns mxc:// URI."""
        token = self.ensure_token()
        url = f"{self._homeserver}/_matrix/media/v3/upload"
        try:
            resp = _SESSION.post(
                url,
                params={"filename": filename},
                data=content,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": content_type,
                },
                timeout=120,
                verify=self._verify_ssl,
            )
        except requests.RequestException as err:
            raise MatrixSendError(f"Upload connection error: {err}") from err

        if resp.status_code == 401:
            token = self.login()
            resp = _SESSION.post(
                url,
                params={"filename": filename},
                data=content,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": content_type,
                },
                timeout=120,
                verify=self._verify_ssl,
            )

        if resp.status_code not in (200, 201):
            raise MatrixSendError(
                f"upload failed ({resp.status_code}): {resp.text[:300]}"
            )
        uri = resp.json().get("content_uri", "")
        _LOGGER.debug("Uploaded %s → %s", filename, uri)
        return uri

    def send_image(
        self,
        room_id: str,
        body: str,
        content: bytes,
        content_type: str = "image/jpeg",
        filename: str = "image.jpg",
        width: int | None = None,
        height: int | None = None,
    ) -> str:
        """Upload an image and send it as an m.image message."""
        mxc = self.upload_image(content, content_type, filename)
        info: dict[str, Any] = {"mimetype": content_type, "size": len(content)}
        if width and height:
            info["w"] = width
            info["h"] = height

        payload = {
            "msgtype": "m.image",
            "body": body or filename,
            "url": mxc,
            "info": info,
        }
        token = self.ensure_token()
        url = (
            f"{self._homeserver}/_matrix/client/v3/rooms/"
            f"{room_id}/send/m.room.message"
        )
        try:
            resp = _SESSION.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=60,
                verify=self._verify_ssl,
            )
        except requests.RequestException as err:
            raise MatrixSendError(f"Connection error: {err}") from err

        if resp.status_code == 401:
            token = self.login()
            resp = _SESSION.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=60,
                verify=self._verify_ssl,
            )

        if resp.status_code not in (200, 201):
            raise MatrixSendError(
                f"image send failed ({resp.status_code}): {resp.text[:300]}"
            )
        event_id = resp.json().get("event_id", "")
        _LOGGER.debug("Sent image to %s → %s", room_id, event_id)
        return event_id

    def send_video(
        self,
        room_id: str,
        body: str,
        content: bytes,
        content_type: str = "video/mp4",
        filename: str = "video.mp4",
        duration: int = 0,
        width: int | None = None,
        height: int | None = None,
    ) -> str:
        """Upload a video and send it as an m.video message."""
        mxc = self.upload_image(content, content_type, filename)
        info: dict[str, Any] = {"mimetype": content_type, "size": len(content)}
        if duration:
            info["duration"] = duration
        if width and height:
            info["w"] = width
            info["h"] = height

        payload = {
            "msgtype": "m.video",
            "body": body or filename,
            "url": mxc,
            "info": info,
        }
        token = self.ensure_token()
        url = (
            f"{self._homeserver}/_matrix/client/v3/rooms/"
            f"{room_id}/send/m.room.message"
        )
        try:
            resp = _SESSION.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=120,
                verify=self._verify_ssl,
            )
        except requests.RequestException as err:
            raise MatrixSendError(f"Connection error: {err}") from err

        if resp.status_code == 401:
            token = self.login()
            resp = _SESSION.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=120,
                verify=self._verify_ssl,
            )

        if resp.status_code not in (200, 201):
            raise MatrixSendError(
                f"video send failed ({resp.status_code}): {resp.text[:300]}"
            )
        event_id = resp.json().get("event_id", "")
        _LOGGER.debug("Sent video to %s → %s", room_id, event_id)
        return event_id

