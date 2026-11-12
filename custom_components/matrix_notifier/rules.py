"""Notification rule engine for Matrix Notifier.

Each rule is a config-entry subentry. The engine watches the trigger
entity, and when it enters the configured state it renders the message
template, optionally grabs a camera snapshot and sends everything to
the configured rooms.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.components.camera import async_get_image
from homeassistant.core import (
    Event,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntry, async_get as dr_get
from homeassistant.helpers.entity_registry import (
    RegistryEntry,
    async_get as er_get,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.template import Template
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import slugify

from .const import (
    CONF_AUTOMATION,
    CONF_CAMERA,
    CONF_CAMERA_FILENAME,
    CONF_ENABLED,
    CONF_ENTITY_ID,
    CONF_MEDIA_MODE,
    CONF_MESSAGE,
    CONF_NAME,
    CONF_ROOMS,
    CONF_TO_STATE,
    CONF_VIDEO,
    CONF_VIDEO_DURATION,
    CONF_VIDEO_QUALITY,
    DEFAULT_CAMERA_FILENAME,
    DEFAULT_TO_STATE,
    DOMAIN,
    SUBENTRY_RULE,
)
from .notifier import MatrixClient, MatrixSendError

_LOGGER = logging.getLogger(__name__)

DEFAULT_MESSAGE = (
    "🚨 {{ name }}\n"
    "Датчик: {{ entity_id }}\n"
    "Состояние: {{ state }}\n"
    "Время: {{ now().strftime('%H:%M %d.%m.%Y') }}"
)

# Minimum seconds between fires of the same rule (dedup for rapid
# state_changed events, e.g. attribute updates or flapping sensors).
COOLDOWN_SECONDS = 30


@dataclass
class Rule:
    """A single notification rule loaded from a subentry."""

    subentry_id: str
    title: str
    entity_id: str
    to_state: str
    message: str
    rooms: list[str]
    automation: str | None = None
    camera: str | None = None
    camera_filename: str = DEFAULT_CAMERA_FILENAME
    media_mode: str = "none"  # none | snapshot | video | both
    video_duration: int = 15
    video_quality: str = "original"
    enabled: bool = True
    template: Template | None = field(default=None, repr=False)

    @property
    def slug(self) -> str:
        return slugify(self.title) or self.subentry_id


def rule_from_subentry(subentry: Any) -> Rule | None:
    """Build a Rule from a ConfigSubentry, or None if invalid."""
    if subentry.subentry_type != SUBENTRY_RULE:
        return None
    data: ConfigType = dict(subentry.data)
    entity_id = data.get(CONF_ENTITY_ID)
    automation = data.get(CONF_AUTOMATION)
    rooms = data.get(CONF_ROOMS) or []
    if not rooms or (not entity_id and not automation):
        _LOGGER.warning(
            "Rule '%s' ignored: no entity/automation or rooms",
            subentry.title,
        )
        return None
    # use automation as trigger entity if no entity_id
    trigger_entity = entity_id or automation

    # media_mode: new field, fallback to legacy video bool
    media_mode = data.get(CONF_MEDIA_MODE)
    if media_mode is None:
        media_mode = "video" if data.get(CONF_VIDEO, False) else "snapshot"

    return Rule(
        subentry_id=subentry.subentry_id,
        title=subentry.title or data.get(CONF_NAME, "Уведомление"),
        entity_id=trigger_entity,
        to_state=str(data.get(CONF_TO_STATE) or DEFAULT_TO_STATE),
        message=data.get(CONF_MESSAGE) or DEFAULT_MESSAGE,
        rooms=list(rooms),
        automation=automation,
        camera=data.get(CONF_CAMERA) or None,
        camera_filename=data.get(CONF_CAMERA_FILENAME)
        or DEFAULT_CAMERA_FILENAME,
        media_mode=media_mode,
        video_duration=int(data.get(CONF_VIDEO_DURATION) or 15),
        video_quality=str(data.get(CONF_VIDEO_QUALITY) or "original"),
        enabled=bool(data.get(CONF_ENABLED, True)),
    )


class RuleEngine:
    """Watches entities and dispatches Matrix notifications for rules."""

    def __init__(self, hass: HomeAssistant, client: MatrixClient) -> None:
        self._hass = hass
        self._client = client
        self._rules: dict[str, Rule] = {}
        self._unsubs: list[callable] = []
        self._last_sent: dict[str, datetime] = {}

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def load_rules(self, rules: list[Rule]) -> None:
        """(Re)load rules and re-arm the state listeners."""
        self._teardown()
        self._rules = {r.slug: r for r in rules}
        if not rules:
            _LOGGER.warning("Matrix Notifier: no active rules")
            return

        # entity-based rules: watch state changes
        entity_ids = sorted(
            {r.entity_id for r in rules if r.enabled and not r.automation}
        )
        if entity_ids:
            self._unsubs.append(
                async_track_state_change_event(
                    self._hass, entity_ids, self._handle_state_change
                )
            )

        # automation-based rules: watch automation_triggered events
        automation_ids = sorted(
            {r.automation for r in rules if r.enabled and r.automation}
        )
        if automation_ids:
            self._unsubs.append(
                self._hass.bus.async_listen(
                    "automation_triggered",
                    self._handle_automation_triggered,
                )
            )
            _LOGGER.warning(
                "Matrix Notifier: watching automations %s",
                ", ".join(automation_ids),
            )

        watched = entity_ids + automation_ids
        if not watched:
            _LOGGER.warning("Matrix Notifier: nothing to watch")
            return

        _LOGGER.warning(
            "Matrix Notifier: watching %s (%d rules)",
            ", ".join(watched),
            len(rules),
        )

    def unload(self) -> None:
        """Drop all listeners."""
        self._teardown()
        self._rules.clear()

    def _teardown(self) -> None:
        while self._unsubs:
            unsub = self._unsubs.pop()
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # devices (one per rule → shows up in the UI as a card)
    # ------------------------------------------------------------------ #
    @callback
    def sync_devices(self, entry: Any) -> None:
        """Create/refresh a device per rule so each is manageable in UI."""
        dev_reg = dr_get(self._hass)
        # find the hub device for via_device_id
        hub = dev_reg.async_get_device_by_identifier(
            identifier=(DOMAIN, entry.entry_id),
            config_entry_id=entry.entry_id,
        )
        via_device_id = hub.id if hub else None
        for rule in self._rules.values():
            dev_reg.async_get_or_create(
                config_entry_id=entry.entry_id,
                config_subentry_id=rule.subentry_id,
                identifiers={(DOMAIN, f"{entry.entry_id}_{rule.slug}")},
                name=rule.title,
                manufacturer="Matrix",
                model="Notification rule",
                via_device_id=via_device_id,
            )

    # ------------------------------------------------------------------ #
    # triggering
    # ------------------------------------------------------------------ #
    @callback
    def _handle_state_change(self, event: Event) -> None:
        """State changed — evaluate rules and schedule sends."""
        entity_id: str = event.data.get("entity_id", "")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        new_state_val = new_state.state

        # attribute-only update (state string unchanged) — skip entirely
        if old_state is not None and old_state.state == new_state_val:
            return

        now = datetime.now()
        for rule in self._rules.values():
            if not rule.enabled or rule.entity_id != entity_id:
                continue
            if rule.to_state and new_state_val != rule.to_state:
                continue
            # cooldown — ignore rapid re-fires of the same rule
            last = self._last_sent.get(rule.slug)
            if last and (now - last).total_seconds() < COOLDOWN_SECONDS:
                _LOGGER.debug(
                    "Rule '%s' in cooldown (%.0fs left), skipping",
                    rule.title,
                    COOLDOWN_SECONDS - (now - last).total_seconds(),
                )
                continue
            self._last_sent[rule.slug] = now
            _LOGGER.warning(
                "Rule '%s' matched: %s → %s",
                rule.title,
                old_state.state if old_state else "?",
                new_state_val,
            )
            self._hass.async_create_background_task(
                self._async_fire_rule(rule, new_state_val, new_state.name),
                f"matrix_notifier rule {rule.slug}",
            )

    @callback
    def _handle_automation_triggered(self, event: Event) -> None:
        """Automation fired — extract trigger info and evaluate rules.

        The automation_triggered event does NOT contain a 'trigger' dict.
        It carries: entity_id (automation), name (automation title),
        source (e.g. "state of binary_sensor.leak").
        """
        entity_id: str = event.data.get("entity_id", "")
        source: str = event.data.get("source", "")

        # extract sensor entity_id from source string
        # examples: "state of binary_sensor.leak", "state of switch.light"
        trigger_entity = ""
        m = re.search(r"\b([a-z_][a-z0-9_]*\.[a-z0-9_]+)\b", source)
        if m:
            trigger_entity = m.group(1)

        # look up trigger sensor state
        trigger_name = ""
        trigger_from = ""
        trigger_to = ""
        if trigger_entity:
            state_obj = self._hass.states.get(trigger_entity)
            if state_obj:
                trigger_name = state_obj.name
                trigger_to = state_obj.state

        # build human-readable description
        if trigger_entity and trigger_name:
            desc = f"{trigger_name}: {trigger_to}"
        elif trigger_entity:
            desc = f"{trigger_entity}: {trigger_to}"
        elif source:
            desc = source
        else:
            desc = "automation triggered"

        now = datetime.now()
        for rule in self._rules.values():
            if not rule.enabled or rule.automation != entity_id:
                continue

            # cooldown
            last = self._last_sent.get(rule.slug)
            if last and (now - last).total_seconds() < COOLDOWN_SECONDS:
                continue
            self._last_sent[rule.slug] = now

            _LOGGER.warning(
                "Rule '%s' matched automation %s (trigger: %s)",
                rule.title,
                entity_id,
                desc,
            )
            self._hass.async_create_background_task(
                self._async_fire_rule(
                    rule,
                    state=trigger_to or "on",
                    friendly_name=rule.title,
                    trigger_info={
                        "entity_id": trigger_entity,
                        "name": trigger_name,
                        "from_state": trigger_from,
                        "to_state": trigger_to,
                        "platform": "state",
                        "description": desc,
                    },
                ),
                f"matrix_notifier rule {rule.slug}",
            )

    async def async_fire_rule(
        self, rule: Rule, state: str, friendly_name: str,
        trigger_info: dict | None = None,
    ) -> None:
        """Public wrapper — fire a rule now (used by the test endpoint)."""
        await self._async_fire_rule(rule, state, friendly_name, trigger_info)

    async def _async_fire_rule(
        self,
        rule: Rule,
        state: str,
        friendly_name: str,
        trigger_info: dict | None = None,
    ) -> None:
        """Render template, snapshot camera / record video, send rooms."""
        try:
            message = self._render(rule, state, friendly_name, trigger_info)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Rule '%s': template error: %s", rule.title, err)
            return

        image: tuple[bytes, str, str, int | None, int | None] | None = None
        video: tuple[bytes, int, int | None, int | None] | None = None

        want_video = rule.media_mode in ("video", "both")
        want_snapshot = rule.media_mode in ("snapshot", "both")

        if want_video and rule.camera:
            # record a video clip
            try:
                from .video import async_record_video

                video = await async_record_video(
                    self._hass,
                    rule.camera,
                    duration=rule.video_duration,
                    fps=15,
                    quality=rule.video_quality,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Rule '%s': video record failed (%s)",
                    rule.title,
                    err,
                )
                video = None

        # snapshot: explicit request, or fallback when video failed
        if rule.camera and (want_snapshot or (want_video and video is None)):
            image = await self._async_snapshot(rule.camera, rule.camera_filename)

        for room in rule.rooms:
            try:
                await self._async_send_to_room(room, message, image, video)
                _LOGGER.warning(
                    "Rule '%s' sent to %s", rule.title, room
                )
            except MatrixSendError:
                try:
                    await self._hass.async_add_executor_job(self._client.login)
                    await self._async_send_to_room(
                        room, message, image, video
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "Rule '%s' → %s failed: %s", rule.title, room, err
                    )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "Rule '%s' → %s unexpected error: %s",
                    rule.title,
                    room,
                    err,
                )

    async def _async_send_to_room(
        self,
        room: str,
        message: str,
        image: tuple[bytes, str, str, int | None, int | None] | None,
        video: tuple[bytes, int, int | None, int | None] | None,
    ) -> None:
        """Send text message first, then video/image if present."""
        # 1) always send the text notification
        await self._hass.async_add_executor_job(
            self._client.send_message, room, message
        )
        # 2) attach media after the text
        if video is not None:
            content, duration_ms, w, h = video
            await self._hass.async_add_executor_job(
                lambda: self._client.send_video(
                    room, "", content, "video/mp4",
                    "recording.mp4", duration_ms, w, h,
                )
            )
        if image is not None:
            content, ctype, fname, w, h = image
            await self._hass.async_add_executor_job(
                lambda: self._client.send_image(
                    room, "", content, ctype, fname, w, h
                )
            )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _render(
        self,
        rule: Rule,
        state: str,
        friendly_name: str,
        trigger_info: dict | None = None,
    ) -> str:
        """Render the rule message template."""
        if rule.template is None:
            rule.template = Template(rule.message, self._hass)

        # base context
        ctx: dict[str, Any] = {
            "entity_id": rule.entity_id,
            "state": state,
            "name": friendly_name or rule.title,
            "rule": rule.title,
            "rooms": ", ".join(rule.rooms),
            "trigger": {"entity_id": rule.entity_id, "to_state": state},
        }

        # enrich with automation trigger info if available
        if trigger_info:
            ti = trigger_info
            ctx["trigger_entity"] = ti.get("entity_id", "")
            ctx["trigger_name"] = ti.get("name", "")
            ctx["trigger_from"] = ti.get("from_state", "")
            ctx["trigger_to"] = ti.get("to_state", "")
            ctx["trigger_description"] = ti.get("description", "")
            ctx["trigger"] = {
                "entity_id": ti.get("entity_id", rule.entity_id),
                "from_state": ti.get("from_state", ""),
                "to_state": ti.get("to_state", state),
                "platform": ti.get("platform", ""),
                "description": ti.get("description", ""),
            }
            # override name/entity for convenience:
            # if template uses {{ name }} / {{ entity_id }} it shows the
            # actual trigger sensor, not the automation entity
            if ti.get("entity_id"):
                ctx["entity_id"] = ti["entity_id"]
            if ti.get("name"):
                ctx["name"] = ti["name"]

        return str(
            rule.template.async_render(ctx, parse_result=False)
        ).strip()

    async def _async_snapshot(
        self, camera_entity: str, filename: str
    ) -> tuple[bytes, str, str, int | None, int | None] | None:
        """Grab a JPEG snapshot from a camera entity."""
        return await _async_snapshot(self._hass, camera_entity, filename)


async def _async_snapshot(
    hass: HomeAssistant, camera_entity: str, filename: str
) -> tuple[bytes, str, str, int | None, int | None] | None:
    """Grab a JPEG snapshot from a camera entity (module-level helper)."""
    try:
        img = await async_get_image(hass, camera_entity, timeout=15)
    except (HomeAssistantError, TimeoutError, Exception) as err:  # noqa: BLE001
        _LOGGER.warning("Camera snapshot %s failed: %s", camera_entity, err)
        return None
    if not img.content:
        return None

    width = height = None
    try:
        from io import BytesIO

        from PIL import Image as PILImage

        with PILImage.open(BytesIO(img.content)) as im:
            width, height = im.size
    except Exception:  # noqa: BLE001
        pass

    ctype = img.content_type or "image/jpeg"
    return (img.content, ctype, filename, width, height)


def build_rules(hass: HomeAssistant, entry: Any) -> list[Rule]:
    """Extract all valid rules from a config entry's subentries."""
    rules: list[Rule] = []
    for subentry in entry.subentries.values():
        rule = rule_from_subentry(subentry)
        if rule is not None:
            rules.append(rule)
    # dedupe by slug keeping first
    seen: set[str] = set()
    unique: list[Rule] = []
    for r in rules:
        if r.slug in seen:
            continue
        seen.add(r.slug)
        unique.append(r)
    return unique
