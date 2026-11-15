"""Config flow for Matrix Notifier."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CHANNELS,
    CHANNEL_MATRIX,
    CONF_CAMERA,
    CONF_CAMERA_FILENAME,
    CONF_CHANNELS,
    CONF_DEVICE_ID,
    CONF_ENABLED,
    CONF_ENTITY_ID,
    CONF_HOMESERVER,
    CONF_MESSAGE,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_ROOMS,
    CONF_TELEGRAM_BOT_TOKEN,
    CONF_TELEGRAM_CHATS,
    CONF_TO_STATE,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_CAMERA_FILENAME,
    DEFAULT_DEVICE_ID,
    DEFAULT_TO_STATE,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    SUBENTRY_RULE,
)
from .notifier import MatrixClient, MatrixLoginError
from .rules import DEFAULT_MESSAGE
from .telegram import TelegramClient, TelegramError, parse_chat_ids

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOMESERVER, default="https://matrix.example.com"): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_DEVICE_ID, default=DEFAULT_DEVICE_ID): str,
        vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
    }
)


class MatrixNotifierConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Matrix Notifier."""

    VERSION = 1

    _client: MatrixClient | None = None
    _creds: dict[str, Any] = {}
    _rooms: list[str] = []

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Return the supported subentry types for this integration."""
        return {SUBENTRY_RULE: NotificationRuleSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for credentials and verify login."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                client = await self.hass.async_add_executor_job(
                    self._try_login, user_input
                )
            except MatrixLoginError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected login error")
                errors["base"] = "unknown"
            else:
                # Unique key = homeserver + username (one entry per account)
                await self.async_set_unique_id(
                    f"{user_input[CONF_HOMESERVER]}|{user_input[CONF_USERNAME]}"
                )
                self._abort_if_unique_id_configured()

                # keep client + credentials on the flow instance (persists
                # across steps, no global state to leak)
                self._client = client
                self._creds = user_input

                return await self.async_step_rooms()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_rooms(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick rooms."""
        errors: dict[str, str] = {}

        client: MatrixClient | None = self._client
        creds: dict[str, Any] = self._creds

        # flow instance was recreated (restart) — go back to credentials
        if client is None:
            return await self.async_step_user()

        if user_input is not None:
            self._rooms = user_input.get(CONF_ROOMS, [])
            return await self.async_step_telegram()

        # fetch rooms in executor
        try:
            rooms = await self.hass.async_add_executor_job(
                client.get_joined_rooms
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to fetch rooms")
            errors["base"] = "cannot_connect"
            rooms = []

        room_options = {
            f"{r['name']} ({r['room_id']})": r["room_id"] for r in rooms
        }

        rooms_schema = vol.Schema(
            {
                vol.Required(CONF_ROOMS, default=[]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=rid, label=label
                            )
                            for label, rid in room_options.items()
                        ],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )

        return self.async_show_form(
            step_id="rooms",
            data_schema=rooms_schema,
            errors=errors,
        )

    async def async_step_telegram(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Optional Telegram setup (bot token + chat ids)."""
        errors: dict[str, str] = {}

        client: MatrixClient | None = self._client
        creds: dict[str, Any] = self._creds

        # flow instance was recreated (restart) — go back to credentials
        if client is None:
            return await self.async_step_user()

        if user_input is not None:
            token = (user_input.get(CONF_TELEGRAM_BOT_TOKEN) or "").strip()
            chats = parse_chat_ids(user_input.get(CONF_TELEGRAM_CHATS))

            if token:
                # validate token via getMe
                try:
                    await self.hass.async_add_executor_job(
                        TelegramClient(token).get_me
                    )
                except TelegramError:
                    errors["base"] = "invalid_telegram_token"
            if not errors:
                data: dict[str, Any] = {
                    CONF_HOMESERVER: creds[CONF_HOMESERVER],
                    CONF_USERNAME: creds[CONF_USERNAME],
                    CONF_PASSWORD: creds[CONF_PASSWORD],
                    CONF_DEVICE_ID: creds.get(
                        CONF_DEVICE_ID, DEFAULT_DEVICE_ID
                    ),
                    CONF_VERIFY_SSL: creds.get(
                        CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL
                    ),
                    CONF_ROOMS: self._rooms,
                    CONF_CHANNELS: [CHANNEL_MATRIX],
                }
                if token:
                    data[CONF_TELEGRAM_BOT_TOKEN] = token
                    data[CONF_TELEGRAM_CHATS] = chats
                return self.async_create_entry(
                    title=f"Matrix Notifier ({client.user_id})", data=data
                )

        return self.async_show_form(
            step_id="telegram",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_TELEGRAM_BOT_TOKEN, default=""
                    ): str,
                    vol.Optional(CONF_TELEGRAM_CHATS, default=""): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    def _try_login(user_input: dict[str, Any]) -> MatrixClient:
        """Blocking login called in executor."""
        client = MatrixClient(
            homeserver=user_input[CONF_HOMESERVER],
            username=user_input[CONF_USERNAME],
            password=user_input[CONF_PASSWORD],
            device_id=user_input.get(CONF_DEVICE_ID, DEFAULT_DEVICE_ID),
            verify_ssl=user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        )
        client.login()
        return client

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Re-authenticate when password changed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                await self.hass.async_add_executor_job(self._try_login, data)
            except MatrixLoginError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates=data
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    def _get_reauth_entry(self) -> config_entries.ConfigEntry:
        return self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlow:
        return MatrixNotifierOptionsFlow(config_entry)


class MatrixNotifierOptionsFlow(config_entries.OptionsFlow):
    """Options flow — edit rooms list."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        data = self._config_entry.data
        client = MatrixClient(
            homeserver=data[CONF_HOMESERVER],
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            device_id=data.get(CONF_DEVICE_ID, DEFAULT_DEVICE_ID),
            verify_ssl=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        )

        try:
            await self.hass.async_add_executor_job(client.login)
            rooms = await self.hass.async_add_executor_job(
                client.get_joined_rooms
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to fetch rooms for options")
            rooms = [
                {"room_id": rid, "name": rid}
                for rid in data.get(CONF_ROOMS, [])
            ]

        room_options = {
            f"{r['name']} ({r['room_id']})": r["room_id"] for r in rooms
        }

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ROOMS, default=data.get(CONF_ROOMS, [])
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=rid, label=label)
                            for label, rid in room_options.items()
                        ],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )


class NotificationRuleSubentryFlow(config_entries.ConfigSubentryFlow):
    """Create / edit a notification rule (subentry)."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create a new rule."""
        return await self._async_step_form(user_input, reconfigure=False)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Edit an existing rule."""
        return await self._async_step_form(user_input, reconfigure=True)

    async def _async_step_form(
        self,
        user_input: dict[str, Any] | None,
        *,
        reconfigure: bool,
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self._get_entry()
        existing: dict[str, Any] = {}
        if reconfigure:
            existing = dict(self._get_reconfigure_subentry().data)

        if user_input is not None:
            # validate rooms against entry
            rooms = user_input.get(CONF_ROOMS) or []
            if not rooms:
                errors[CONF_ROOMS] = "no_rooms"
            if not errors:
                data = {
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_ENTITY_ID: user_input[CONF_ENTITY_ID],
                    CONF_TO_STATE: user_input.get(
                        CONF_TO_STATE, DEFAULT_TO_STATE
                    ),
                    CONF_ROOMS: rooms,
                    CONF_MESSAGE: user_input.get(
                        CONF_MESSAGE, DEFAULT_MESSAGE
                    ),
                    CONF_CAMERA: user_input.get(CONF_CAMERA) or None,
                    CONF_CAMERA_FILENAME: user_input.get(
                        CONF_CAMERA_FILENAME, DEFAULT_CAMERA_FILENAME
                    ),
                    CONF_ENABLED: user_input.get(CONF_ENABLED, True),
                }
                if reconfigure:
                    return self.async_update_reload_and_abort(
                        entry,
                        self._get_reconfigure_subentry(),
                        title=user_input[CONF_NAME],
                        data=data,
                    )
                return self.async_create_entry(
                    title=user_input[CONF_NAME], data=data
                )

        # build schema
        rooms_list = entry.data.get(CONF_ROOMS, [])
        rooms_options = [
            selector.SelectOptionDict(value=rid, label=rid)
            for rid in rooms_list
        ]

        camera_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="camera")
        )
        # prefill camera only when editing an existing rule that has one
        existing_camera = existing.get(CONF_CAMERA)
        camera_field = (
            vol.Optional(CONF_CAMERA, default=existing_camera)
            if existing_camera
            else vol.Optional(CONF_CAMERA)
        )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=existing.get(CONF_NAME, ""),
                ): str,
                vol.Required(
                    CONF_ENTITY_ID,
                    default=existing.get(CONF_ENTITY_ID, ""),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=None)
                ),
                vol.Optional(
                    CONF_TO_STATE,
                    default=existing.get(CONF_TO_STATE, DEFAULT_TO_STATE),
                ): str,
                vol.Required(
                    CONF_ROOMS,
                    default=existing.get(CONF_ROOMS, []),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=rooms_options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(
                    CONF_MESSAGE,
                    default=existing.get(CONF_MESSAGE, DEFAULT_MESSAGE),
                ): selector.TemplateSelector(),
                camera_field: camera_selector,
                vol.Optional(
                    CONF_CAMERA_FILENAME,
                    default=existing.get(
                        CONF_CAMERA_FILENAME, DEFAULT_CAMERA_FILENAME
                    ),
                ): str,
                vol.Required(
                    CONF_ENABLED,
                    default=existing.get(CONF_ENABLED, True),
                ): bool,
            }
        )

        step = "reconfigure" if reconfigure else "user"
        return self.async_show_form(
            step_id=step, data_schema=schema, errors=errors
        )
