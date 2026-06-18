"""PreCom sensor platform — sensor.precom_last_alarm."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback, async_get_current_platform
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ALARM_ID,
    ATTR_BENODIGD,
    ATTR_COORDINATES,
    ATTR_FUNCTIONS,
    ATTR_FUNCTIONS_FORMATTED,
    ATTR_GROUP_ID,
    ATTR_GROUP_LABEL,
    ATTR_GROUPS,
    ATTR_LAST_UPDATED,
    ATTR_LOCATION,
    ATTR_RESPONSE_DATA,
    ATTR_TEXT,
    ATTR_TIMESTAMP,
    ATTR_VOORGESTELDE_FUNCTIES,
    DOMAIN,
    SERVICE_UPDATE_ALARM,
    STATE_NO_ALARM,
)
from .coordinator import PreComCoordinator

if TYPE_CHECKING:
    from . import PreComConfigEntry

_LOGGER = logging.getLogger(__name__)

# Coordinator centralises all data updates; no per-entity polling needed.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PreComConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PreCom sensor from a config entry."""
    coordinator = entry.runtime_data
    
    # Always create the global sensors (backwards compatibility)
    sensors: list[SensorEntity] = [
        PreComLastAlarmSensor(coordinator, entry),
        PreComGroupsSensor(coordinator, entry),
    ]
    
    # Create a sensor for each user group
    if coordinator.data and coordinator.data.user_groups:
        for group in coordinator.data.user_groups:
            group_id = str(group.get("GroupID", ""))
            if group_id:
                sensors.append(PreComGroupAlarmSensor(coordinator, entry, group_id))
            else:
                _LOGGER.warning("Skipping group without GroupID: %s", group)
    
    async_add_entities(sensors)

    platform = async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_UPDATE_ALARM,
        {},
        "async_update_alarm",
    )


class PreComLastAlarmSensor(CoordinatorEntity[PreComCoordinator], SensorEntity):
    """Represents the most recent PreCom alarm.

    State:    alarm ID (str) when an alarm is active, "none" when idle.
    Attributes:
        alarm_id     — same as state, for template convenience
        functions    — list of {label: str, users: list[str]}
        last_updated — ISO timestamp of the last successful poll
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_alarm"

    def __init__(
        self,
        coordinator: PreComCoordinator,
        entry: PreComConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_alarm"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="PreCom",
            manufacturer="PreCom",
            model="Cloud Alerting Service",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://portal.pre-com.nl",
        )

    @property
    def native_value(self) -> str:
        """Return the alarm text, or 'none' when no alarm is active."""
        if self.coordinator.data is None:
            return STATE_NO_ALARM
        return self.coordinator.data.text or STATE_NO_ALARM

    @staticmethod
    def _format_functions(functions: list[dict]) -> str:
        """Return a human-readable string listing each function and its users."""
        groups: list[str] = []
        for func in functions:
            users: list[str] = func.get("users", [])
            block = [f"{func.get('label', '')} ({len(users)}):"]
            block.extend(f"- {user}" for user in users)
            groups.append("\n".join(block))
        return "\n\n".join(groups)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return alarm details as entity attributes."""
        if self.coordinator.data is None:
            return {}
        return {
            ATTR_ALARM_ID: self.coordinator.data.alarm_id,
            ATTR_TEXT: self.coordinator.data.text,
            ATTR_LOCATION: self.coordinator.data.location,
            ATTR_COORDINATES: self.coordinator.data.coordinates,
            ATTR_TIMESTAMP: self.coordinator.data.timestamp,
            ATTR_FUNCTIONS: self.coordinator.data.functions,
            ATTR_FUNCTIONS_FORMATTED: self._format_functions(
                self.coordinator.data.functions
            ),
            ATTR_RESPONSE_DATA: self.coordinator.data.response_data,
            ATTR_BENODIGD: self.coordinator.data.benodigd,
            ATTR_VOORGESTELDE_FUNCTIES: self.coordinator.data.voorgestelde_functies,
            ATTR_LAST_UPDATED: datetime.now(timezone.utc).isoformat(),
        }

    async def async_update_alarm(self) -> None:
        """Force an immediate refresh of alarm data."""
        await self.coordinator.async_request_refresh()


class PreComGroupsSensor(CoordinatorEntity[PreComCoordinator], SensorEntity):
    """Represents the groups the PreCom user belongs to.

    State:    number of groups (int).
    Attributes:
        groups       — list of group dicts as returned by the API
        last_updated — ISO timestamp of the last successful poll
    """

    _attr_has_entity_name = True
    _attr_translation_key = "groups"

    def __init__(
        self,
        coordinator: PreComCoordinator,
        entry: PreComConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_groups"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="PreCom",
            manufacturer="PreCom",
            model="Cloud Alerting Service",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://portal.pre-com.nl",
        )

    @property
    def native_value(self) -> int:
        """Return the number of groups."""
        if self.coordinator.data is None:
            return 0
        return len(self.coordinator.data.groups)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return group details as entity attributes."""
        if self.coordinator.data is None:
            return {}
        return {
            ATTR_GROUPS: self.coordinator.data.groups,
            ATTR_LAST_UPDATED: datetime.now(timezone.utc).isoformat(),
        }


class PreComGroupAlarmSensor(CoordinatorEntity[PreComCoordinator], SensorEntity):
    """Represents the most recent alarm for a specific group.

    State:    alarm text (str) when an alarm is active, "none" when idle.
    Attributes:
        alarm_id             — alarm ID string
        text                 — alarm message text
        timestamp            — ISO timestamp of the alarm
        functions            — list of {label: str, users: list[str]}
        response_data        — who responded to the alarm
        benodigd             — staffing summary
        voorgestelde_functies — proposed functions
        group_id             — numeric group ID
        group_label          — human-readable group name
        last_updated         — ISO timestamp of the last successful poll
    """

    _attr_has_entity_name = True
    _attr_translation_key = "group_alarm"

    def __init__(
        self,
        coordinator: PreComCoordinator,
        entry: PreComConfigEntry,
        group_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._group_id = group_id
        # Use group_id for unique_id (numeric, stable)
        self._attr_unique_id = f"{entry.entry_id}_group_alarm_{group_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="PreCom",
            manufacturer="PreCom",
            model="Cloud Alerting Service",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://portal.pre-com.nl",
        )

    @property
    def available(self) -> bool:
        """Return True if the sensor data is valid and scraping succeeded.
        
        Returns False if:
        - Coordinator has no data (startup or total failure)
        - Group not found in coordinator data
        - Portal scraping failed for this group
        """
        if not self.coordinator.last_update_success:
            return False
        if self.coordinator.data is None:
            return False
        group_alarm = self.coordinator.data.group_alarms.get(self._group_id)
        if group_alarm is None:
            return False
        # Mark unavailable if portal scraping failed
        return not group_alarm.scraping_failed

    @property
    def name(self) -> str:
        """Return the name of the sensor based on the group label."""
        if self.coordinator.data is None:
            return f"Group {self._group_id} Alarm"
        group_alarm = self.coordinator.data.group_alarms.get(self._group_id)
        if group_alarm:
            return f"{group_alarm.group_label} Alarm"
        return f"Group {self._group_id} Alarm"

    @property
    def native_value(self) -> str:
        """Return the alarm text, or 'none' when no alarm is active."""
        if self.coordinator.data is None:
            return STATE_NO_ALARM
        group_alarm = self.coordinator.data.group_alarms.get(self._group_id)
        if group_alarm is None:
            return STATE_NO_ALARM
        return group_alarm.text or STATE_NO_ALARM

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return alarm details as entity attributes."""
        if self.coordinator.data is None:
            return {}
        group_alarm = self.coordinator.data.group_alarms.get(self._group_id)
        if group_alarm is None:
            return {}
        return {
            ATTR_ALARM_ID: group_alarm.alarm_id,
            ATTR_TEXT: group_alarm.text,
            ATTR_LOCATION: group_alarm.location,
            ATTR_COORDINATES: group_alarm.coordinates,
            ATTR_TIMESTAMP: group_alarm.timestamp,
            ATTR_FUNCTIONS: group_alarm.functions,
            ATTR_RESPONSE_DATA: group_alarm.response_data,
            ATTR_BENODIGD: group_alarm.benodigd,
            ATTR_VOORGESTELDE_FUNCTIES: group_alarm.voorgestelde_functies,
            ATTR_GROUP_ID: group_alarm.group_id,
            ATTR_GROUP_LABEL: group_alarm.group_label,
            ATTR_LAST_UPDATED: datetime.now(timezone.utc).isoformat(),
        }
