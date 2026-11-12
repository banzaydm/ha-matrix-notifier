"""Notify entity platform for Matrix Notifier (HA 2026 NotifyEntity API)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.notify import NotifyEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .notifier import MatrixClient, MatrixSendError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Matrix Notifier notify entity from a config entry."""
    ctx = hass.data[DOMAIN].get(entry.entry_id)
    if ctx is None:
        _LOGGER.error("No data for entry %s", entry.entry_id)
        return
    async_add_entities([MatrixNotifyEntity(hass, entry, ctx)])


class MatrixNotifyEntity(NotifyEntity):
    """Send notifications to configured Matrix rooms."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        ctx: dict[str, Any],
    ) -> None:
        self._hass = hass
        self._client: MatrixClient = ctx["client"]
        self._rooms: list[str] = ctx.get("rooms", [])
        self._attr_unique_id = f"{entry.entry_id}_notify"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Matrix",
        }

    async def async_send_message(
        self, message: str, title: str | None = None, **kwargs: Any
    ) -> None:
        """Send a message to all configured rooms."""
        targets: list[str] = list(self._rooms)
        override = kwargs.get("target") or kwargs.get("room_id")
        if override:
            targets = [override] if isinstance(override, str) else list(override)

        if not targets:
            _LOGGER.warning("Matrix Notifier: no rooms configured")
            return

        text = f"{title}\n{message}" if title else message

        for rid in targets:
            try:
                await self._hass.async_add_executor_job(
                    self._client.send_message, rid, text
                )
            except MatrixSendError:
                try:
                    await self._hass.async_add_executor_job(
                        self._client.login
                    )
                    await self._hass.async_add_executor_job(
                        self._client.send_message, rid, text
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("Matrix notify to %s failed: %s", rid, err)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "Unexpected error sending to %s: %s", rid, err
                )
