"""REST API for Matrix Notifier web panel."""
from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_AUTOMATION,
    CONF_HOMESERVER,
    CONF_PASSWORD,
    CONF_ROOMS,
    CONF_TELEGRAM_BOT_TOKEN,
    CONF_TELEGRAM_CHATS,
    CONF_USERNAME,
    DOMAIN,
    SUBENTRY_RULE,
)
from .notifier import MatrixClient, MatrixLoginError
from .telegram import TelegramClient, TelegramError, parse_chat_ids

_LOGGER = logging.getLogger(__name__)


class StatusView(HomeAssistantView):
    """GET /api/matrix_notifier/status — integration status."""

    url = "/api/matrix_notifier/status"
    name = "api:matrix_notifier:status"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json({"configured": False})

        entry = entries[0]
        rules = [
            {
                "subentry_id": sid,
                "title": s.title,
                **dict(s.data),
            }
            for sid, s in entry.subentries.items()
            if s.subentry_type == SUBENTRY_RULE
        ]
        ctx = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
        tg_client: TelegramClient | None = ctx.get("telegram")
        return self.json(
            {
                "configured": True,
                "state": entry.state.value,
                "title": entry.title,
                "rooms": entry.data.get("rooms", []),
                "telegram": {
                    "configured": bool(
                        entry.data.get(CONF_TELEGRAM_BOT_TOKEN)
                    ),
                    "chats": entry.data.get(CONF_TELEGRAM_CHATS) or [],
                    "bot": tg_client.bot_username if tg_client else None,
                },
                "rules": rules,
            }
        )


class RulesView(HomeAssistantView):
    """GET/POST /api/matrix_notifier/rules."""

    url = "/api/matrix_notifier/rules"
    name = "api:matrix_notifier:rules"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json([])

        entry = entries[0]
        rules = [
            {
                "subentry_id": sid,
                "title": s.title,
                **dict(s.data),
            }
            for sid, s in entry.subentries.items()
            if s.subentry_type == SUBENTRY_RULE
        ]
        return self.json(rules)

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        data: dict[str, Any] = await request.json()
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("No config entry", status_code=404)

        entry = entries[0]
        name = data.pop("name", "New Rule")
        data.pop("subentry_id", None)

        subentry = ConfigSubentry(
            data=MappingProxyType(data),
            subentry_type=SUBENTRY_RULE,
            title=name,
            unique_id=None,
        )
        ok = hass.config_entries.async_add_subentry(entry, subentry)
        if ok:
            return self.json({"subentry_id": subentry.subentry_id})
        return self.json_message("Failed to create", status_code=500)


class RuleView(HomeAssistantView):
    """PUT/DELETE /api/matrix_notifier/rules/{subentry_id}."""

    url = "/api/matrix_notifier/rules/{subentry_id}"
    name = "api:matrix_notifier:rule"
    requires_auth = True

    async def put(
        self, request: web.Request, subentry_id: str
    ) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        data: dict[str, Any] = await request.json()

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("No config entry", status_code=404)

        entry = entries[0]
        subentry = entry.subentries.get(subentry_id)
        if subentry is None:
            return self.json_message("Rule not found", status_code=404)

        name = data.pop("name", subentry.title)
        data.pop("subentry_id", None)
        data.pop("title", None)

        # no actual change — async_update_subentry would return False (500)
        old_data = dict(subentry.data)
        if name == subentry.title and old_data == data:
            return self.json({"success": True, "unchanged": True})

        ok = hass.config_entries.async_update_subentry(
            entry,
            subentry,
            title=name,
            data=data,
        )
        if ok:
            return self.json({"success": True})
        return self.json_message("Failed to update", status_code=500)

    async def delete(
        self, request: web.Request, subentry_id: str
    ) -> web.Response:
        hass: HomeAssistant = request.app["hass"]

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("No config entry", status_code=404)

        entry = entries[0]
        if subentry_id not in entry.subentries:
            return self.json_message("Rule not found", status_code=404)

        ok = hass.config_entries.async_remove_subentry(entry, subentry_id)
        if ok:
            return self.json({"success": True})
        return self.json_message("Failed to delete", status_code=500)


class RuleTestView(HomeAssistantView):
    """POST /api/matrix_notifier/rules/{subentry_id}/test — fire rule now."""

    url = "/api/matrix_notifier/rules/{subentry_id}/test"
    name = "api:matrix_notifier:rule:test"
    requires_auth = True

    async def post(
        self, request: web.Request, subentry_id: str
    ) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("No config entry", status_code=404)

        entry = entries[0]
        subentry = entry.subentries.get(subentry_id)
        if subentry is None:
            return self.json_message("Rule not found", status_code=404)

        from .rules import rule_from_subentry

        rule = rule_from_subentry(subentry)
        if rule is None:
            return self.json_message(
                "Rule is invalid (no entity/rooms)", status_code=400
            )

        ctx = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not ctx or "engine" not in ctx:
            return self.json_message("Integration not loaded", status_code=503)

        engine = ctx["engine"]
        # run in background so HTTP doesn't block on video recording
        hass.async_create_background_task(
            engine.async_fire_rule(rule, rule.to_state, rule.title),
            f"matrix_notifier test {rule.slug}",
        )
        return self.json({"status": "fired"})


class SendView(HomeAssistantView):
    """POST /api/matrix_notifier/send — send a test message."""

    url = "/api/matrix_notifier/send"
    name = "api:matrix_notifier:send"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        data: dict[str, Any] = await request.json()
        message: str = data.get("message", "")
        room_id: str | None = data.get("room_id")

        if not message:
            return self.json_message("Empty message", status_code=400)

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("No config entry", status_code=404)

        entry = entries[0]
        ctx = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not ctx:
            return self.json_message("Integration not loaded", status_code=503)

        client = ctx["client"]
        rooms = [room_id] if room_id else ctx.get("rooms", [])
        results: dict[str, str] = {}
        errors: list[str] = []

        for rid in rooms:
            try:
                eid = await hass.async_add_executor_job(
                    client.send_message, rid, message
                )
                results[rid] = eid
            except Exception as err:  # noqa: BLE001
                errors.append(f"{rid}: {err}")

        return self.json({"sent": results, "errors": errors})


class UsersView(HomeAssistantView):
    """GET /api/matrix_notifier/users — Matrix users from joined rooms."""

    url = "/api/matrix_notifier/users"
    name = "api:matrix_notifier:users"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json([])

        entry = entries[0]
        ctx = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not ctx:
            return self.json_message("Integration not loaded", status_code=503)

        client: MatrixClient = ctx["client"]
        rooms: list[str] = ctx.get("rooms", [])

        # collect members across all rooms, dedupe by user_id
        users: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for rid in rooms:
            try:
                members = await hass.async_add_executor_job(
                    client.get_room_members, rid
                )
            except Exception as err:  # noqa: BLE001
                errors.append(f"{rid}: {err}")
                continue
            for m in members:
                uid = m["user_id"]
                if uid not in users:
                    users[uid] = {
                        "user_id": uid,
                        "display_name": m.get("display_name") or uid,
                        "avatar_url": m.get("avatar_url") or "",
                        "rooms": [],
                    }
                users[uid]["rooms"].append(rid)

        return self.json(
            {"users": list(users.values()), "errors": errors}
        )


class SetupView(HomeAssistantView):
    """POST /api/matrix_notifier/setup — create config entry from web panel."""

    url = "/api/matrix_notifier/setup"
    name = "api:matrix_notifier:setup"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        data: dict[str, Any] = await request.json()

        homeserver = (data.get(CONF_HOMESERVER) or "").strip()
        username = (data.get(CONF_USERNAME) or "").strip()
        password = data.get(CONF_PASSWORD) or ""
        rooms = data.get(CONF_ROOMS) or []

        if not homeserver or not username or not password:
            return self.json_message(
                "homeserver, username, password required", status_code=400
            )

        entries = hass.config_entries.async_entries(DOMAIN)
        if entries:
            return self.json_message(
                "Already configured — remove existing entry first",
                status_code=409,
            )

        # verify login
        client = MatrixClient(
            homeserver=homeserver,
            username=username,
            password=password,
        )
        try:
            await hass.async_add_executor_job(client.login)
        except MatrixLoginError as err:
            return self.json_message(f"Login failed: {err}", status_code=401)
        except Exception as err:  # noqa: BLE001
            return self.json_message(f"Login error: {err}", status_code=500)

        # if no rooms given, return available rooms for selection
        if not rooms:
            try:
                joined = await hass.async_add_executor_job(
                    client.get_joined_rooms
                )
            except Exception as err:  # noqa: BLE001
                return self.json_message(
                    f"Cannot fetch rooms: {err}", status_code=500
                )
            return self.json(
                {
                    "status": "rooms_required",
                    "rooms": joined,
                    "user_id": client.user_id,
                }
            )

        # create config entry directly
        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title=f"Matrix Notifier ({client.user_id})",
            data={
                CONF_HOMESERVER: homeserver,
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
                CONF_ROOMS: rooms,
            },
            source="user",
            options={},
            unique_id=f"{homeserver}|{username}",
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        await hass.config_entries.async_add(entry)
        return self.json({"status": "ok", "entry_id": entry.entry_id})


class RoomsView(HomeAssistantView):
    """GET /api/matrix_notifier/rooms — rooms with friendly names."""

    url = "/api/matrix_notifier/rooms"
    name = "api:matrix_notifier:rooms"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json([])

        entry = entries[0]
        ctx = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not ctx:
            return self.json_message("Integration not loaded", status_code=503)

        client: MatrixClient = ctx["client"]

        # fetch LIVE joined rooms from Matrix (not the stale setup-time cache)
        try:
            rooms = await hass.async_add_executor_job(client.get_joined_rooms)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("get_joined_rooms failed: %s", err)
            # fallback to cached list
            room_ids: list[str] = ctx.get("rooms", [])
            rooms = [{"room_id": rid, "name": rid} for rid in room_ids]

        # keep cache in sync so other consumers see new rooms too
        ctx["rooms"] = [r["room_id"] for r in rooms]

        return self.json(rooms)


class CreateRoomView(HomeAssistantView):
    """POST /api/matrix_notifier/rooms/create — create a new room."""

    url = "/api/matrix_notifier/rooms/create"
    name = "api:matrix_notifier:rooms:create"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        data: dict[str, Any] = await request.json()
        name = (data.get("name") or "").strip()
        invite = data.get("invite") or []

        if not name:
            return self.json_message("Room name required", status_code=400)

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("No config entry", status_code=404)

        entry = entries[0]
        ctx = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not ctx:
            return self.json_message("Integration not loaded", status_code=503)

        client: MatrixClient = ctx["client"]
        try:
            room_id = await hass.async_add_executor_job(
                client.create_room, name, invite
            )
        except Exception as err:  # noqa: BLE001
            return self.json_message(f"Create room failed: {err}", status_code=500)

        # persist: add to ctx cache + entry.data so all consumers see it
        current = list(ctx.get("rooms", []))
        if room_id not in current:
            current.append(room_id)
            ctx["rooms"] = current
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_ROOMS: current}
            )

        return self.json({"room_id": room_id, "name": name})


class InviteUserView(HomeAssistantView):
    """POST /api/matrix_notifier/rooms/{room_id}/invite — invite user."""

    url = "/api/matrix_notifier/rooms/{room_id}/invite"
    name = "api:matrix_notifier:rooms:invite"
    requires_auth = True

    async def post(
        self, request: web.Request, room_id: str
    ) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        data: dict[str, Any] = await request.json()
        user_id = (data.get("user_id") or "").strip()

        if not user_id:
            return self.json_message("user_id required", status_code=400)

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("No config entry", status_code=404)

        entry = entries[0]
        ctx = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not ctx:
            return self.json_message("Integration not loaded", status_code=503)

        client: MatrixClient = ctx["client"]
        try:
            ok = await hass.async_add_executor_job(
                client.invite_user, room_id, user_id
            )
        except Exception as err:  # noqa: BLE001
            return self.json_message(f"Invite failed: {err}", status_code=500)

        if ok:
            return self.json({"success": True})
        return self.json_message("Invite rejected by server", status_code=500)


class KickUserView(HomeAssistantView):
    """POST /api/matrix_notifier/rooms/{room_id}/kick — kick user."""

    url = "/api/matrix_notifier/rooms/{room_id}/kick"
    name = "api:matrix_notifier:rooms:kick"
    requires_auth = True

    async def post(
        self, request: web.Request, room_id: str
    ) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        data: dict[str, Any] = await request.json()
        user_id = (data.get("user_id") or "").strip()

        if not user_id:
            return self.json_message("user_id required", status_code=400)

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("No config entry", status_code=404)

        entry = entries[0]
        ctx = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not ctx:
            return self.json_message("Integration not loaded", status_code=503)

        client: MatrixClient = ctx["client"]
        try:
            ok = await hass.async_add_executor_job(
                client.kick_user, room_id, user_id
            )
        except Exception as err:  # noqa: BLE001
            return self.json_message(f"Kick failed: {err}", status_code=500)

        if ok:
            return self.json({"success": True})
        return self.json_message("Kick rejected by server", status_code=500)


class RoomMembersView(HomeAssistantView):
    """GET /api/matrix_notifier/rooms/{room_id}/members — list members."""

    url = "/api/matrix_notifier/rooms/{room_id}/members"
    name = "api:matrix_notifier:rooms:members"
    requires_auth = True

    async def get(
        self, request: web.Request, room_id: str
    ) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("No config entry", status_code=404)

        entry = entries[0]
        ctx = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not ctx:
            return self.json_message("Integration not loaded", status_code=503)

        client: MatrixClient = ctx["client"]
        try:
            members = await hass.async_add_executor_job(
                client.get_room_members, room_id
            )
        except Exception as err:  # noqa: BLE001
            return self.json_message(f"Failed: {err}", status_code=500)

        return self.json({"members": members})


class VideoView(HomeAssistantView):
    """POST /api/matrix_notifier/video — record 15s clip from camera and send."""

    url = "/api/matrix_notifier/video"
    name = "api:matrix_notifier:video"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        data: dict[str, Any] = await request.json()
        camera: str = (data.get("camera") or "").strip()
        room_id: str = (data.get("room_id") or "").strip()
        duration: int = int(data.get("duration") or 15)
        fps: int = int(data.get("fps") or 15)
        quality: str = (data.get("quality") or "original").strip()

        if not camera or not room_id:
            return self.json_message(
                "camera and room_id required", status_code=400
            )

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("No config entry", status_code=404)

        entry = entries[0]
        ctx = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not ctx:
            return self.json_message("Integration not loaded", status_code=503)

        client: MatrixClient = ctx["client"]
        from .video import async_record_video

        try:
            video_bytes, duration_ms, width, height = (
                await async_record_video(
                    hass, camera, duration=duration, fps=fps,
                    quality=quality,
                )
            )
        except Exception as err:  # noqa: BLE001
            return self.json_message(f"Video failed: {err}", status_code=500)

        try:
            eid = await hass.async_add_executor_job(
                lambda: client.send_video(
                    room_id,
                    f"Видео с {camera} ({duration}s)",
                    video_bytes,
                    "video/mp4",
                    "recording.mp4",
                    duration_ms,
                    width,
                    height,
                )
            )
        except Exception as err:  # noqa: BLE001
            return self.json_message(f"Send failed: {err}", status_code=500)

        return self.json(
            {
                "event_id": eid,
                "duration_ms": duration_ms,
                "size": len(video_bytes),
            }
        )


class TelegramView(HomeAssistantView):
    """POST /api/matrix_notifier/telegram — save/clear Telegram config."""

    url = "/api/matrix_notifier/telegram"
    name = "api:matrix_notifier:telegram"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        body: dict[str, Any] = await request.json()

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("No config entry", status_code=404)
        entry = entries[0]
        data = dict(entry.data)

        # explicit clear
        if body.get("clear"):
            data.pop(CONF_TELEGRAM_BOT_TOKEN, None)
            data.pop(CONF_TELEGRAM_CHATS, None)
            hass.config_entries.async_update_entry(entry, data=data)
            return self.json({"success": True, "configured": False})

        new_token = (body.get(CONF_TELEGRAM_BOT_TOKEN) or "").strip()
        token = new_token or data.get(CONF_TELEGRAM_BOT_TOKEN)

        if not token:
            return self.json_message(
                "bot_token required (or send clear:true)",
                status_code=400,
            )

        # validate a newly supplied token
        if new_token:
            try:
                bot_username = await hass.async_add_executor_job(
                    TelegramClient(new_token).get_me
                )
            except TelegramError as err:
                return self.json_message(
                    f"Invalid Telegram token: {err}", status_code=400
                )
        else:
            ctx = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
            tg: TelegramClient | None = ctx.get("telegram")
            bot_username = tg.bot_username if tg else None

        data[CONF_TELEGRAM_BOT_TOKEN] = token
        if "chats" in body or CONF_TELEGRAM_CHATS in body:
            data[CONF_TELEGRAM_CHATS] = parse_chat_ids(
                body.get("chats", body.get(CONF_TELEGRAM_CHATS))
            )
        else:
            data.setdefault(CONF_TELEGRAM_CHATS, [])

        hass.config_entries.async_update_entry(entry, data=data)
        return self.json(
            {
                "success": True,
                "configured": True,
                "chats": data.get(CONF_TELEGRAM_CHATS) or [],
                "bot": bot_username,
            }
        )


class TelegramTestView(HomeAssistantView):
    """POST /api/matrix_notifier/telegram/test — send a test message."""

    url = "/api/matrix_notifier/telegram/test"
    name = "api:matrix_notifier:telegram:test"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("No config entry", status_code=404)
        entry = entries[0]

        ctx = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
        tg: TelegramClient | None = ctx.get("telegram")
        chats: list[str] = entry.data.get(CONF_TELEGRAM_CHATS) or []

        if tg is None:
            return self.json_message(
                "Telegram is not configured", status_code=400
            )
        if not chats:
            return self.json_message(
                "No Telegram chats configured", status_code=400
            )

        text = (
            "✅ Matrix Notifier\n"
            "Тест Telegram-канала — уведомления работают."
        )
        sent: list[str] = []
        errors: list[str] = []
        for chat in chats:
            try:
                await hass.async_add_executor_job(tg.send_text, chat, text)
                sent.append(chat)
            except Exception as err:  # noqa: BLE001
                errors.append(f"{chat}: {err}")
        return self.json({"sent": sent, "errors": errors})


def async_register_api(hass: HomeAssistant) -> None:
    """Register all REST API views."""
    _LOGGER.warning("Matrix Notifier: registering REST API views")
    hass.http.register_view(StatusView)
    hass.http.register_view(RulesView)
    hass.http.register_view(RuleView)
    hass.http.register_view(RuleTestView)
    hass.http.register_view(SendView)
    hass.http.register_view(UsersView)
    hass.http.register_view(SetupView)
    hass.http.register_view(RoomsView)
    hass.http.register_view(CreateRoomView)
    hass.http.register_view(InviteUserView)
    hass.http.register_view(KickUserView)
    hass.http.register_view(RoomMembersView)
    hass.http.register_view(VideoView)
    hass.http.register_view(TelegramView)
    hass.http.register_view(TelegramTestView)
    _LOGGER.warning("Matrix Notifier: REST API views registered")
