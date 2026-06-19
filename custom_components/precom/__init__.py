"""The PreCom integration."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PreComApiClient
from .const import (
    ATTR_MELDING,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_GET_ALARM_PORTAL_DETAILS,
    SERVICE_GEOCODE_MELDING,
)
from .coordinator import PreComCoordinator, extract_adres as _extract_adres
from .geocoder import PreComGeocoder
from .htmlscraper import PreComHtmlScraper, PreComPortalError

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]
_LOGGER = logging.getLogger(__name__)

type PreComConfigEntry = ConfigEntry[PreComCoordinator]

GET_ALARM_PORTAL_DETAILS_SCHEMA = vol.Schema(
    {vol.Required(ATTR_MELDING): vol.All(str, vol.Length(min=1))}
)


async def async_setup_entry(hass: HomeAssistant, entry: PreComConfigEntry) -> bool:
    """Set up PreCom from a config entry."""
    username: str = entry.data[CONF_USERNAME]
    password: str = entry.data[CONF_PASSWORD]
    scan_interval: int = (
        entry.options.get(CONF_SCAN_INTERVAL)
        or entry.data.get(CONF_SCAN_INTERVAL)
        or DEFAULT_SCAN_INTERVAL
    )

    session = async_get_clientsession(hass)
    client = PreComApiClient(username=username, password=password, session=session)
    htmlscraper = PreComHtmlScraper(
        username=username, password=password, session=session
    )
    geocoder = PreComGeocoder(session=session)
    coordinator = PreComCoordinator(
        hass, entry, client, htmlscraper, geocoder, scan_interval
    )

    # Raises ConfigEntryNotReady on failure; HA will retry with backoff.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    if not hass.services.has_service(DOMAIN, SERVICE_GET_ALARM_PORTAL_DETAILS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_ALARM_PORTAL_DETAILS,
            _make_get_alarm_portal_details_handler(hass),
            schema=GET_ALARM_PORTAL_DETAILS_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_GEOCODE_MELDING):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GEOCODE_MELDING,
            _make_geocode_melding_handler(hass),
            schema=GET_ALARM_PORTAL_DETAILS_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry when options (e.g. scan_interval) are changed.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: PreComConfigEntry
) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PreComConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and len(hass.config_entries.async_entries(DOMAIN)) <= 1:
        hass.services.async_remove(DOMAIN, SERVICE_GET_ALARM_PORTAL_DETAILS)
        hass.services.async_remove(DOMAIN, SERVICE_GEOCODE_MELDING)
    return unload_ok


def _make_get_alarm_portal_details_handler(hass: HomeAssistant):
    """Create the domain service handler for portal detail lookups."""

    async def _handle(call: ServiceCall) -> ServiceResponse:
        melding = str(call.data[ATTR_MELDING]).strip()
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_entry_loaded",
            )

        coordinator = entries[0].runtime_data
        if len(entries) > 1:
            _LOGGER.debug(
                "Multiple PreCom entries loaded; using the first entry for %s",
                SERVICE_GET_ALARM_PORTAL_DETAILS,
            )

        try:
            details = await coordinator.htmlscraper.get_alarm_portal_details(melding)
        except PreComPortalError as err:
            raise HomeAssistantError(
                f"Could not fetch PreCom portal details for '{melding}': {err}"
            ) from err

        # Also run geocoding so the caller gets a complete picture in one call.
        adres = _extract_adres(melding)
        adres_detail = await coordinator.geocoder.geocode(adres) if adres else None

        location = None
        coordinates = None
        is_latest_alarm = False
        if coordinator.data and coordinator.data.text.strip() == melding:
            location = coordinator.data.location or None
            coordinates = coordinator.data.coordinates
            is_latest_alarm = True

        return {
            "response_data": details.get("response_data", []),
            "benodigd": details.get("benodigd", []),
            "voorgestelde_functies": details.get("voorgestelde_functies", []),
            "adres": adres,
            "adres_detail": adres_detail,
            "location": location,
            "coordinates": coordinates,
            "is_latest_alarm": is_latest_alarm,
        }

    return _handle


def _make_geocode_melding_handler(hass: HomeAssistant):
    """Create the domain service handler for geocoding an alarm message text."""

    async def _handle(call: ServiceCall) -> ServiceResponse:
        melding = str(call.data[ATTR_MELDING]).strip()
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_entry_loaded",
            )

        coordinator = entries[0].runtime_data
        adres = _extract_adres(melding)
        
        # Build base response with Nominatim geocoding
        response: dict[str, Any] = {
            "adres": adres,
            "adres_detail": None,
            "location": None,
            "coordinates": None,
            "is_latest_alarm": False,
        }
        
        if not adres:
            return response

        detail = await coordinator.geocoder.geocode(adres)
        response["adres_detail"] = detail

        # Check if the given melding matches the latest alarm from PreCom
        if coordinator.data and coordinator.data.text.strip() == melding:
            # Same alarm - include PreCom location and coordinates
            response["location"] = coordinator.data.location or None
            response["coordinates"] = coordinator.data.coordinates
            response["is_latest_alarm"] = True
            _LOGGER.debug(
                "Melding matches latest alarm (ID=%s), including PreCom location/coordinates",
                coordinator.data.alarm_id,
            )

        return response

    return _handle
