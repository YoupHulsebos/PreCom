"""Geocoder for PreCom — converts a street address to latitude/longitude via Nominatim."""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

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
# Nominatim ToS: identify the application in the User-Agent header.
_USER_AGENT = "HomeAssistant-PreCom/1.0 (https://github.com/yourusername/hacs-precom)"


class PreComGeocoder:
    """Geocodes addresses using the OpenStreetMap Nominatim API.

    Results are cached in memory for the lifetime of the integration so that the
    same address is never looked up twice in a single HA session.
    Geocoding is best-effort: errors are logged at DEBUG level and None is returned.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        # Maps normalised address string -> (lat, lon, detail) or None when
        # a previous lookup returned no results.
        self._cache: dict[str, dict[str, Any] | None] = {}

    async def geocode_melding(self, melding: str) -> dict[str, Any]:
        """Extract the address from a P2000 alarm text, geocode it, and return both.

        Returns a dict with ``adres`` (str) and ``adres_detail`` (Nominatim result or None).
        """
        adres = extract_adres(melding)
        adres_detail = await self.geocode(adres) if adres else None
        return {"adres": adres, "adres_detail": adres_detail}

    async def geocode(self, address: str) -> dict[str, Any] | None:
        """Return the raw Nominatim result dict for *address*, or None on failure/no result.

        The address string is normalised (stripped + lowercased) before caching so
        that minor whitespace differences don't cause duplicate lookups.
        """
        if not address or not address.strip():
            return None

        cache_key = address.strip().lower()
        if cache_key in self._cache:
            return self._cache[cache_key]

        result = await self._fetch(address.strip())
        self._cache[cache_key] = result
        return result

    async def _fetch(self, address: str) -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "q": address,
            "format": "json",
            "limit": 1,
            "countrycodes": "nl",
        }
        try:
            async with self._session.get(
                _NOMINATIM_URL,
                params=params,
                headers={"User-Agent": _USER_AGENT, "Accept-Language": "nl"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    _LOGGER.debug(
                        "Nominatim returned HTTP %s for address '%s'",
                        response.status,
                        address,
                    )
                    return None
                data = await response.json(content_type=None)
                if not data:
                    _LOGGER.debug("Nominatim: no results for address '%s'", address)
                    return None
                _LOGGER.debug(
                    "Nominatim raw response for '%s': lat=%s, lon=%s, display_name=%s",
                    address,
                    data[0].get("lat", ""),
                    data[0].get("lon", ""),
                    data[0].get("display_name", ""),
                )
                return data[0]
        except (aiohttp.ClientError, KeyError, ValueError, TypeError) as err:
            _LOGGER.debug("Nominatim geocode failed for '%s': %s", address, err)
            return None
