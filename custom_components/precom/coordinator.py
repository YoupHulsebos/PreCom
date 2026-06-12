"""DataUpdateCoordinator for PreCom - handles polling and token refresh."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PreComApiClient, PreComAuthError, PreComApiError
from .const import DOMAIN, STATE_NO_ALARM
from .geocoder import PreComGeocoder
from .htmlscraper import PreComHtmlScraper, PreComPortalError

_LOGGER = logging.getLogger(__name__)

# Regex to extract the address from a P2000-style alarm message text.
# Named group 'adres' captures the address portion between the incident type and the postcode.
_ADRES_RE = re.compile(
    r"^P\s+\d+(?:\s+[A-Z]{3}-\d+)?\s+(?:\([^)]*\)\s+)*"
    r"(?:Reanimatie|Ass\.\s+Ambu|BR\s+(?:berm\/bosschage|buiten\s+industrie|afval|woning|bos|bijgebouw|wegvervoer|gebouw|container|gezondheidszorg|industrie)"
    r"|Nacontrole|Ongeval\s+(?:wegvervoer|gev\.\s+stof)|Liftopsluiting|PAC\s+brandmelding"
    r"|OMS\s+(?:brandmelding|handmelder)|CO-melder|Persoon\s+te\s+water|Voertuig\s+te\s+water"
    r"|Dier\s+(?:in\s+problemen|in\s+put\/kelder|in\/op\s+ijs|op\s+hoogte|te\s+water)"
    r"|Buitensluiting|Stank\/hind\.\s+lucht|Brandgerucht|Wateroverlast|Vervuild\s+wegdek"
    r"|Stormschade|Ongeval\s+VVE|Dienstverlening)"
    r"(?:\s+\([^)]*\))*\s+(?P<adres>.+?)(?:\s+\d{6})*\s*$"
)


def extract_adres(text: str) -> str:
    """Return the address extracted from a P2000 alarm text, or empty string."""
    if not text:
        return ""
    m = _ADRES_RE.match(text)
    if m:
        return m.group("adres")
    return ""


# Internal alias used within this module.
_extract_adres = extract_adres


class GroupAlarmData:
    """Container for alarm data specific to a single group."""

    def __init__(
        self,
        group_id: str,
        group_label: str,
        alarm_id: str,
        text: str,
        timestamp: str,
        functions: list[dict[str, Any]],
        response_data: list[dict[str, Any]],
        benodigd: list[dict[str, Any]],
        voorgestelde_functies: list[dict[str, Any]],
        adres: str = "",
        adres_detail: dict | None = None,
        scraping_failed: bool = False,
    ) -> None:
        self.group_id = group_id
        self.group_label = group_label
        self.alarm_id = alarm_id      # alarm ID string, or STATE_NO_ALARM
        self.text = text              # alarm message text
        self.timestamp = timestamp    # alarm date/time string from API
        self.functions = functions    # list of {label: str, users: list[str]}
        self.response_data = response_data
        self.benodigd = benodigd
        self.voorgestelde_functies = voorgestelde_functies
        self.adres = adres            # address extracted from alarm text
        self.adres_detail = adres_detail  # full Nominatim result, or None
        self.scraping_failed = scraping_failed  # True if portal scraping failed

    def __eq__(self, other: object) -> bool:
        """Compare alarm data for change detection.

        Compares the core alarm state (group_id, alarm_id, text, timestamp, adres).
        Skips functions (not from portal), response_data/benodigd/voorgestelde_functies
        (order may vary, enriched asynchronously), adres_detail (geocoding can vary),
        and scraping_failed (status flag, not alarm content).
        """
        if not isinstance(other, GroupAlarmData):
            return False
        return (
            self.group_id == other.group_id
            and self.alarm_id == other.alarm_id
            and self.text == other.text
            and self.timestamp == other.timestamp
            and self.adres == other.adres
        )


class PreComCoordinatorData:
    """Typed container for the data fetched on each polling cycle."""

    def __init__(
        self,
        alarm_id: str,
        functions: list[dict[str, Any]],
        text: str,
        timestamp: str,
        response_data: list[dict[str, Any]],
        benodigd: list[dict[str, Any]],
        voorgestelde_functies: list[dict[str, Any]],
        is_available: bool,
        not_available_timestamp: str,
        not_available_scheduled: bool,
        groups: list[dict[str, Any]],
        user_groups: list[dict[str, Any]],
        group_alarms: dict[str, GroupAlarmData],
        adres: str = "",
        adres_detail: dict | None = None,
    ) -> None:
        self.alarm_id = alarm_id      # alarm ID string, or STATE_NO_ALARM
        self.functions = functions    # list of {label: str, users: list[str]}
        self.text = text              # alarm message text
        self.timestamp = timestamp    # alarm date/time string from API
        self.response_data = response_data
        self.benodigd = benodigd
        self.voorgestelde_functies = voorgestelde_functies
        self.adres = adres            # address extracted from alarm text
        self.adres_detail = adres_detail  # full Nominatim result, or None
        self.is_available = is_available              # True when user is available
        self.not_available_timestamp = not_available_timestamp  # ISO ts of unavailability
        self.not_available_scheduled = not_available_scheduled  # scheduled absence
        self.groups = groups          # list of group dicts from GetAllGroups
        self.user_groups = user_groups  # list of group dicts from GetAllUserGroups (today)
        self.group_alarms = group_alarms  # dict of GroupID -> GroupAlarmData

    def __eq__(self, other: object) -> bool:
        """Compare coordinator data for change detection.

        Compares the core state (alarm data, availability, group alarms).
        Skips functions, response_data, groups, user_groups (supplementary data).
        """
        if not isinstance(other, PreComCoordinatorData):
            return False
        return (
            self.alarm_id == other.alarm_id
            and self.text == other.text
            and self.timestamp == other.timestamp
            and self.is_available == other.is_available
            and self.not_available_timestamp == other.not_available_timestamp
            and self.not_available_scheduled == other.not_available_scheduled
            and self.adres == other.adres
            and self.group_alarms == other.group_alarms
        )


class PreComCoordinator(DataUpdateCoordinator[PreComCoordinatorData]):
    """Polls PreCom every scan_interval seconds.

    On PreComAuthError the coordinator re-authenticates once and retries before
    raising UpdateFailed. This mirrors the original YAML pattern of always
    fetching a fresh token before each alarm check, but avoids a full
    authenticate() call on every successful poll cycle.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: PreComApiClient,
        htmlscraper: PreComHtmlScraper,
        geocoder: PreComGeocoder,
        scan_interval: int | None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval) if scan_interval else None,
            always_update=False,
        )
        self._entry = entry
        self.client = client
        self.htmlscraper = htmlscraper
        self.geocoder = geocoder
        self._unavailable = False
        self._previous_data: PreComCoordinatorData | None = None

    async def _fetch_alarms(self) -> list[dict]:
        """Fetch alarms, re-authenticating once on token rejection."""
        _LOGGER.debug("API call: GetAlarmMessages")
        try:
            alarms = await self.client.get_alarm_messages()
            _LOGGER.debug("API call: GetAlarmMessages completed - %d alarms received", len(alarms))
            return alarms
        except PreComAuthError:
            pass

        # Token was rejected — re-authenticate and retry once.
        _LOGGER.debug("PreCom token rejected, re-authenticating")
        await self.client.authenticate()
        alarms = await self.client.get_alarm_messages()
        _LOGGER.debug("API call: GetAlarmMessages retry completed - %d alarms received", len(alarms))
        return alarms

    async def _fetch_user_info(self) -> dict:
        """Fetch user info, re-authenticating once on token rejection."""
        _LOGGER.debug("API call: GetUserInfo")
        try:
            user_info = await self.client.get_user_info()
            _LOGGER.debug("API call: GetUserInfo completed")
            return user_info
        except PreComAuthError:
            pass

        _LOGGER.debug("API call: GetUserInfo - retrying after re-authentication")
        await self.client.authenticate()
        user_info = await self.client.get_user_info()
        _LOGGER.debug("API call: GetUserInfo retry completed")
        return user_info

    async def _fetch_groups(self) -> list[dict]:
        """Fetch all groups, re-authenticating once on token rejection."""
        _LOGGER.debug("API call: GetAllGroups")
        try:
            groups = await self.client.get_all_groups()
            _LOGGER.debug("API call: GetAllGroups completed - %d groups received", len(groups))
            return groups
        except PreComAuthError:
            pass

        _LOGGER.debug("API call: GetAllGroups - retrying after re-authentication")
        await self.client.authenticate()
        groups = await self.client.get_all_groups()
        _LOGGER.debug("API call: GetAllGroups retry completed - %d groups received", len(groups))
        return groups

    async def _fetch_user_groups(self) -> list[dict]:
        """Fetch user's groups for today and tomorrow with populated ServiceFuntions.

        GetAllUserGroups returns groups with empty ServiceFuntions arrays.
        GetAllFunctions is called per group for today and tomorrow so that
        the next 24 hours can be evaluated at 15-minute granularity.
        DayTotals from tomorrow are merged into today's response.
        """
        _LOGGER.debug("API call: GetAllUserGroups")
        try:
            groups = await self.client.get_all_user_groups()
        except PreComAuthError:
            _LOGGER.debug("API call: GetAllUserGroups - retrying after re-authentication")
            await self.client.authenticate()
            groups = await self.client.get_all_user_groups()
        
        _LOGGER.debug("API call: GetAllUserGroups completed - %d groups received", len(groups))
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%dT%H:%M:%S")
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        enriched: list[dict] = []
        for group in groups:
            group_id = group.get("GroupID")
            group_label = group.get("Label", f"Group {group_id}")
            if group_id is None:
                enriched.append(group)
                continue
            _LOGGER.debug("API call: GetAllFunctions for group '%s' (ID=%s)", group_label, group_id)
            try:
                full_today = await self.client.get_group_functions(group_id, today)
                # Fetch tomorrow to cover the full next-24h window.
                try:
                    full_tomorrow = await self.client.get_group_functions(group_id, tomorrow)
                    tomorrow_funcs = {
                        f.get("ServiceFunctionID"): f
                        for f in full_tomorrow.get("ServiceFuntions", [])
                    }
                    for func in full_today.get("ServiceFuntions", []):
                        func_id = func.get("ServiceFunctionID")
                        t_func = tomorrow_funcs.get(func_id)
                        if t_func:
                            func.setdefault("DayTotals", {}).update(
                                t_func.get("DayTotals", {})
                            )
                except (PreComAuthError, PreComApiError) as err:
                    _LOGGER.debug(
                        "Could not fetch tomorrow's functions for group %s (%s): %s",
                        group.get("Label"),
                        group_id,
                        err,
                    )
                enriched.append(full_today)
                _LOGGER.debug("API call: GetAllFunctions completed for group '%s'", group_label)
            except (PreComAuthError, PreComApiError) as err:
                _LOGGER.warning(
                    "Could not fetch functions for group %s (%s): %s",
                    group_label,
                    group_id,
                    err,
                )
                enriched.append(group)
        
        _LOGGER.debug("Enriched %d user groups with function data", len(enriched))
        return enriched

    def _mark_unavailable(self, reason: str) -> None:
        """Log a warning the first time the service becomes unavailable."""
        if not self._unavailable:
            _LOGGER.warning("PreCom unavailable: %s", reason)
            self._unavailable = True

    def _mark_available(self) -> None:
        """Log recovery once when the service becomes reachable again."""
        if self._unavailable:
            _LOGGER.info("PreCom API connection restored")
            self._unavailable = False

    async def _build_group_alarms(
        self,
        user_groups: list[dict],
    ) -> dict[str, GroupAlarmData]:
        """Build a mapping of GroupID -> GroupAlarmData for each user group.

        For each group in user_groups, scrapes the portal to find the newest alarm.
        Fetches full alarm details (response_data, benodigd, voorgestelde_functies)
        via HTML scraping.

        Groups without active alarms get an entry with alarm_id = STATE_NO_ALARM.
        
        Note: This method uses ONLY portal scraping, not the API, to find alarms.
        This allows finding historical alarms beyond the API's retention period.
        """
        _LOGGER.debug(
            "Building group alarms: %d user groups",
            len(user_groups),
        )
        
        group_alarms: dict[str, GroupAlarmData] = {}

        # Process each user group by scraping the portal
        for group in user_groups:
            group_id = str(group.get("GroupID", ""))
            group_label = str(group.get("Label", f"Group {group_id}"))

            if not group_id:
                _LOGGER.debug("Skipping group without GroupID: %s", group)
                continue

            _LOGGER.debug(
                "Portal scraping: Searching for latest alarm in group '%s' (ID=%s)",
                group_label,
                group_id,
            )

            # Scrape portal for this group's latest alarm
            try:
                alarm_data = await self.htmlscraper.get_latest_alarm_for_group(
                    group_id, group_label
                )
            except PreComPortalError as err:
                _LOGGER.warning(
                    "Portal scraping failed for group '%s' (ID=%s): %s",
                    group_label,
                    group_id,
                    err,
                )
                # Try to use previous data if available
                if (
                    self._previous_data
                    and group_id in self._previous_data.group_alarms
                ):
                    previous_alarm = self._previous_data.group_alarms[group_id]
                    _LOGGER.debug(
                        "Using previous alarm data for group '%s' after scraping failure",
                        group_label,
                    )
                    # Keep previous data but mark as failed
                    group_alarms[group_id] = GroupAlarmData(
                        group_id=previous_alarm.group_id,
                        group_label=previous_alarm.group_label,
                        alarm_id=previous_alarm.alarm_id,
                        text=previous_alarm.text,
                        timestamp=previous_alarm.timestamp,
                        functions=previous_alarm.functions,
                        response_data=previous_alarm.response_data,
                        benodigd=previous_alarm.benodigd,
                        voorgestelde_functies=previous_alarm.voorgestelde_functies,
                        adres=previous_alarm.adres,
                        adres_detail=previous_alarm.adres_detail,
                        scraping_failed=True,
                    )
                else:
                    # No previous data - create empty entry
                    _LOGGER.debug(
                        "No previous data available for group '%s', creating empty entry",
                        group_label,
                    )
                    group_alarms[group_id] = GroupAlarmData(
                        group_id=group_id,
                        group_label=group_label,
                        alarm_id=STATE_NO_ALARM,
                        text="",
                        timestamp="",
                        functions=[],
                        response_data=[],
                        benodigd=[],
                        voorgestelde_functies=[],
                        adres="",
                        scraping_failed=True,
                    )
                continue

            if alarm_data is None:
                # No alarm found for this group
                _LOGGER.debug(
                    "Group '%s' (ID=%s): No alarms found in portal (searched last 30 days)",
                    group_label,
                    group_id,
                )
                group_alarms[group_id] = GroupAlarmData(
                    group_id=group_id,
                    group_label=group_label,
                    alarm_id=STATE_NO_ALARM,
                    text="",
                    timestamp="",
                    functions=[],
                    response_data=[],
                    benodigd=[],
                    voorgestelde_functies=[],
                    adres="",
                    scraping_failed=False,
                )
                continue

            # Alarm found - extract data
            alarm_id = alarm_data.get("alarm_id", STATE_NO_ALARM)
            text = alarm_data.get("text", "")
            raw_ts = alarm_data.get("timestamp", "")
            
            # Parse timestamp
            timestamp = ""
            if raw_ts:
                try:
                    # Portal returns ISO format or datetime string
                    if isinstance(raw_ts, str):
                        dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    else:
                        dt = raw_ts
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    timestamp = dt.isoformat()
                except (ValueError, AttributeError) as err:
                    _LOGGER.debug("Could not parse timestamp '%s': %s", raw_ts, err)
                    timestamp = str(raw_ts)

            _LOGGER.debug(
                "Group '%s' (ID=%s): Found alarm MsgInLogID=%s, Text='%s', Timestamp=%s",
                group_label,
                group_id,
                alarm_id,
                text[:100] if text else "",
                timestamp,
            )
            
            response_data = alarm_data.get("response_data", [])
            benodigd = alarm_data.get("benodigd", [])
            voorgestelde_functies = alarm_data.get("voorgestelde_functies", [])
            
            _LOGGER.debug(
                "Group '%s': Portal details - %d responses, %d benodigd, %d voorgestelde functies",
                group_label,
                len(response_data),
                len(benodigd),
                len(voorgestelde_functies),
            )

            group_adres = _extract_adres(text)
            group_coords = await self.geocoder.geocode(group_adres) if group_adres else None
            group_alarms[group_id] = GroupAlarmData(
                group_id=group_id,
                group_label=group_label,
                alarm_id=alarm_id,
                text=text,
                timestamp=timestamp,
                functions=[],  # Not available from portal scraping
                response_data=response_data,
                benodigd=benodigd,
                voorgestelde_functies=voorgestelde_functies,
                adres=group_adres,
                adres_detail=group_coords,
                scraping_failed=False,
            )

        _LOGGER.debug(
            "Built %d group alarms (%d with active alarms, %d without)",
            len(group_alarms),
            sum(1 for ga in group_alarms.values() if ga.alarm_id != STATE_NO_ALARM),
            sum(1 for ga in group_alarms.values() if ga.alarm_id == STATE_NO_ALARM),
        )
        return group_alarms

    async def _async_update_data(self) -> PreComCoordinatorData:
        """Fetch latest alarm data and user info. Called automatically by HA on each interval."""
        _LOGGER.debug("=== PreCom coordinator update started ===")
        
        try:
            alarms = await self._fetch_alarms()
            user_info = await self._fetch_user_info()
            groups = await self._fetch_groups()
            user_groups = await self._fetch_user_groups()
            _LOGGER.debug("All API calls completed successfully")
        except PreComAuthError as err:
            _LOGGER.error("Update failed: authentication error - %s", err)
            self._mark_unavailable(f"authentication failed after token refresh: {err}")
            self._entry.async_start_reauth(self.hass)
            raise UpdateFailed(f"PreCom auth failed: {err}") from err
        except PreComApiError as err:
            _LOGGER.error("Update failed: API error - %s", err)
            self._mark_unavailable(f"API error: {err}")
            raise UpdateFailed(f"PreCom API error: {err}") from err

        self._mark_available()

        # Parse user availability info
        # NOTE: The API uses "NotAvailalbeScheduled" (missing 'i') — intentional typo.
        not_available_manual = bool(user_info.get("NotAvailable", False))
        not_available_scheduled = bool(user_info.get("NotAvailalbeScheduled", False))
        is_available = not (not_available_manual or not_available_scheduled)
        not_available_timestamp = ""
        raw_na_ts = user_info.get("NotAvailableTimestamp", "")
        if raw_na_ts:
            try:
                dt = datetime.fromisoformat(str(raw_na_ts))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                not_available_timestamp = dt.isoformat()
            except ValueError:
                not_available_timestamp = str(raw_na_ts)

        # Build group-specific alarm data
        _LOGGER.debug("Building group-specific alarm data via portal scraping")
        group_alarms = await self._build_group_alarms(user_groups)
        _LOGGER.debug("Group alarm data built successfully")

        if not alarms:
            _LOGGER.debug("No alarms present, returning empty coordinator data")
            return PreComCoordinatorData(
                alarm_id=STATE_NO_ALARM,
                functions=[],
                text="",
                timestamp="",
                response_data=[],
                benodigd=[],
                voorgestelde_functies=[],
                adres="",
                is_available=is_available,
                not_available_timestamp=not_available_timestamp,
                not_available_scheduled=not_available_scheduled,
                groups=groups,
                user_groups=user_groups,
                group_alarms=group_alarms,
            )

        latest = alarms[0]
        alarm_id = str(latest.get("MsgInID", STATE_NO_ALARM))
        text = str(latest.get("Text", ""))
        _LOGGER.debug("Processing latest alarm: MsgInID=%s, Text='%s'", alarm_id, text[:100] if text else "")

        # The API returns Timestamp as an ISO 8601 date-time string.
        # Parse it and attach UTC if no timezone is present.
        raw_ts = latest.get("Timestamp", "")
        timestamp = ""
        if raw_ts:
            try:
                dt = datetime.fromisoformat(str(raw_ts))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                timestamp = dt.isoformat()
            except ValueError:
                timestamp = str(raw_ts)

        # NOTE: The API uses "ServiceFuntions" (missing 'c') — this is an
        # intentional typo in the PreCom API response. Do not correct it.
        raw_functions = latest.get("Group", {}).get("ServiceFuntions", [])
        functions = [
            {
                "label": func.get("Label", ""),
                "users": [u.get("FullName", "") for u in func.get("Users", [])],
            }
            for func in raw_functions
        ]

        response_data: list[dict[str, Any]] = []
        benodigd: list[dict[str, Any]] = []
        voorgestelde_functies: list[dict[str, Any]] = []
        if text:
            try:
                _LOGGER.debug("Fetching portal details for latest alarm")
                portal_details = await self.htmlscraper.get_alarm_portal_details(text)
                response_data = portal_details.get("response_data", [])
                benodigd = portal_details.get("benodigd", [])
                voorgestelde_functies = portal_details.get(
                    "voorgestelde_functies", []
                )
                _LOGGER.debug(
                    "Portal details for latest alarm: %d responses, %d benodigd, %d voorgestelde functies",
                    len(response_data),
                    len(benodigd),
                    len(voorgestelde_functies),
                )
            except PreComPortalError as err:
                _LOGGER.warning(
                    "Could not enrich latest alarm '%s' with portal details: %s",
                    text,
                    err,
                )

        _LOGGER.debug("=== PreCom coordinator update completed successfully ===")
        adres = _extract_adres(text)
        coords = await self.geocoder.geocode(adres) if adres else None
        new_data = PreComCoordinatorData(
            alarm_id=alarm_id,
            functions=functions,
            text=text,
            timestamp=timestamp,
            response_data=response_data,
            benodigd=benodigd,
            voorgestelde_functies=voorgestelde_functies,
            adres=adres,
            adres_detail=coords,
            is_available=is_available,
            not_available_timestamp=not_available_timestamp,
            not_available_scheduled=not_available_scheduled,
            groups=groups,
            user_groups=user_groups,
            group_alarms=group_alarms,
        )
        # Store previous data for fallback on next failure
        self._previous_data = new_data
        return new_data

    async def async_set_unavailable(self, hours: int) -> None:
        """Call set_unavailable on the API client with token-refresh retry."""
        try:
            await self.client.set_unavailable(hours)
        except PreComAuthError:
            await self.client.authenticate()
            await self.client.set_unavailable(hours)

    async def async_set_available(self) -> None:
        """Call set_available on the API client with token-refresh retry."""
        try:
            await self.client.set_available()
        except PreComAuthError:
            await self.client.authenticate()
            await self.client.set_available()
