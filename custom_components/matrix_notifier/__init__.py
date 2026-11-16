"""The Matrix Notifier integration."""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .api import async_register_api
from .const import (
    ATTR_IMAGE,
    ATTR_MESSAGE,
    ATTR_MESSAGE_TYPE,
    ATTR_ROOM_ID,
    ATTR_TITLE,
    CONF_CAMERA_FILENAME,
    CONF_DEVICE_ID,
    CONF_ENABLED,
    CONF_ENTITY_ID,
    CONF_HOMESERVER,
    CONF_MESSAGE,
    CONF_PASSWORD,
    CONF_ROOMS,
    CONF_TELEGRAM_BOT_TOKEN,
    CONF_TELEGRAM_CHATS,
    CONF_TO_STATE,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_CAMERA_FILENAME,
    DEFAULT_DEVICE_ID,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    SERVICE_SEND,
)
from .notifier import MatrixClient, MatrixLoginError, MatrixSendError
from .rules import RuleEngine, build_rules
from .telegram import TelegramClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.NOTIFY]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SEND_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_ROOM_ID): cv.string,
        vol.Optional(ATTR_TITLE): cv.string,
        vol.Optional(ATTR_MESSAGE_TYPE, default="m.text"): vol.In(
            ["m.text", "m.notice", "m.emote"]
        ),
        vol.Optional(ATTR_IMAGE): cv.string,
    }
)

_PANEL_REGISTERED = False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Matrix Notifier from a config entry."""
    global _PANEL_REGISTERED
    data = entry.data

    client = MatrixClient(
        homeserver=data[CONF_HOMESERVER],
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        device_id=data.get(CONF_DEVICE_ID, DEFAULT_DEVICE_ID),
        verify_ssl=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
    )

    # verify login at startup
    try:
        await hass.async_add_executor_job(client.login)
    except MatrixLoginError as err:
        raise ConfigEntryNotReady(str(err)) from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "rooms": data.get(CONF_ROOMS, []),
        "telegram": None,
        "telegram_chats": [],
    }

    # Optional Telegram channel
    tg_token = data.get(CONF_TELEGRAM_BOT_TOKEN)
    if tg_token:
        tg = TelegramClient(
            tg_token, verify_ssl=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
        )
        try:
            await hass.async_add_executor_job(tg.get_me)
        except Exception as err:  # noqa: BLE001
            # don't fail setup — Telegram just stays unavailable
            _LOGGER.warning("Matrix Notifier: Telegram token invalid: %s", err)
            tg = None
        if tg:
            hass.data[DOMAIN][entry.entry_id]["telegram"] = tg
            hass.data[DOMAIN][entry.entry_id]["telegram_chats"] = (
                data.get(CONF_TELEGRAM_CHATS) or []
            )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _register_services(hass, entry)

    # Rule engine — loads notification rules (subentries) and listens
    ctx = hass.data[DOMAIN][entry.entry_id]
    engine = RuleEngine(
        hass,
        client,
        telegram=ctx.get("telegram"),
        telegram_chats=ctx.get("telegram_chats") or [],
    )
    engine.load_rules(build_rules(hass, entry))
    engine.sync_devices(entry)
    hass.data[DOMAIN][entry.entry_id]["engine"] = engine

    # Reload the entry when any subentry (rule) is added/updated/removed
    # so new/changed rules take effect immediately.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Register REST API + sidebar panel (once)
    if not _PANEL_REGISTERED:
        _LOGGER.warning("Matrix Notifier: registering API and panel")
        async_register_api(hass)
        await hass.async_add_executor_job(_copy_panel_files, hass)
        _register_panel(hass)
        _PANEL_REGISTERED = True
        _LOGGER.warning("Matrix Notifier: API and panel registered")

    return True


def _copy_panel_files(hass: HomeAssistant) -> None:
    """Copy the web panel files (HTML, logo, favicon) into HA's www/."""
    src_dir = Path(__file__).parent / "www"
    dst_dir = Path(hass.config.path("www")) / "matrix_notifier"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src in src_dir.iterdir():
        if src.is_file():
            shutil.copy2(src, dst_dir / src.name)


def _register_panel(hass: HomeAssistant) -> None:
    """Register an iframe panel in the HA sidebar."""
    from homeassistant.components import frontend

    # cache-bust: use file mtime so browser always fetches latest HTML
    src = Path(__file__).parent / "www" / "index.html"
    try:
        mtime = int(src.stat().st_mtime)
    except OSError:
        mtime = 0

    frontend.async_register_built_in_panel(
        hass,
        component_name="iframe",
        sidebar_title="Matrix Notifier",
        sidebar_icon="mdi:message-alert",
        config={"url": f"/local/matrix_notifier/index.html?v={mtime}"},
        frontend_url_path="matrix-notifier",
    )


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when a subentry changes."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        ctx = hass.data[DOMAIN].pop(entry.entry_id, None)
        if ctx and "engine" in ctx:
            ctx["engine"].unload()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SEND)
    return unload_ok


def _register_services(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register matrix_notifier.send (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND):
        return

    async def async_send(call: ServiceCall) -> dict[str, Any]:
        """Send a message via Matrix."""
        message: str = call.data[ATTR_MESSAGE]
        room_id: str | None = call.data.get(ATTR_ROOM_ID)
        title: str | None = call.data.get(ATTR_TITLE)
        message_type: str = call.data.get(ATTR_MESSAGE_TYPE, "m.text")
        image: str | None = call.data.get(ATTR_IMAGE)

        results: dict[str, str] = {}
        errors: list[str] = []

        # pick all config entries (usually one)
        for eid, ctx in hass.data[DOMAIN].items():
            if not isinstance(ctx, dict) or "client" not in ctx:
                continue
            client: MatrixClient = ctx["client"]
            target_rooms = [room_id] if room_id else ctx.get("rooms", [])

            # load image if requested (camera entity id or file path)
            img_bytes = None
            img_ctype = "image/jpeg"
            if image:
                img_bytes, img_ctype = await _async_load_image(hass, image)
                if img_bytes is None:
                    errors.append(f"image: cannot load {image}")
                    continue

            body = f"{title}\n{message}" if title else message

            for rid in target_rooms:
                try:
                    if img_bytes is not None:
                        event_id = await hass.async_add_executor_job(
                            lambda r=rid: client.send_image(
                                r, body, img_bytes, img_ctype
                            )
                        )
                    else:
                        event_id = await hass.async_add_executor_job(
                            client.send_message, rid, body, message_type
                        )
                    results[rid] = event_id
                except MatrixSendError as err:
                    # retry once after re-login
                    try:
                        await hass.async_add_executor_job(client.login)
                        if img_bytes is not None:
                            event_id = await hass.async_add_executor_job(
                                lambda r=rid: client.send_image(
                                    r, body, img_bytes, img_ctype
                                )
                            )
                        else:
                            event_id = await hass.async_add_executor_job(
                                client.send_message, rid, body, message_type
                            )
                        results[rid] = event_id
                    except Exception as retry_err:  # noqa: BLE001
                        _LOGGER.error("Send to %s failed: %s", rid, retry_err)
                        errors.append(f"{rid}: {retry_err}")
                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception("Unexpected send error")
                    errors.append(f"{rid}: {err}")

        if errors and not results:
            raise MatrixSendError("; ".join(errors))

        return {"sent": results, "errors": errors}

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND,
        async_send,
        schema=SEND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def _async_load_image(
    hass: HomeAssistant, reference: str
) -> tuple[bytes | None, str]:
    """Load image bytes from a camera entity id or a file path."""
    from .rules import _async_snapshot  # local import to avoid cycle

    if reference.startswith(("camera.", "image.")):
        snap = await _async_snapshot(hass, reference, DEFAULT_CAMERA_FILENAME)
        if snap:
            return snap[0], snap[1]
        return None, "image/jpeg"

    # treat as file path
    import os

    try:
        path = reference
        if not os.path.isabs(path):
            path = hass.config.path(path)
        with open(path, "rb") as f:
            content = f.read()
        ctype = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        return content, ctype
    except OSError:
        _LOGGER.exception("Cannot read image file: %s", reference)
        return None, "image/jpeg"
