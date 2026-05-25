"""Portal scraper for enriching the latest PreCom alarm with report details."""
from __future__ import annotations

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import aiohttp
from yarl import URL

from .const import (
    PORTAL_BASE_URL,
    PORTAL_HOME_URL,
    PORTAL_LOGIN_URL,
    PORTAL_MESSAGE_DETAILS_URL,
    PORTAL_MODULE_LOAD_URL,
    PORTAL_NAVIGATION_MODULES_URL,
    PORTAL_NAVIGATION_NODES_URL,
    PORTAL_OVERVIEW_URL,
    PORTAL_POST_LOGIN_URL,
    PORTAL_SEARCH_RESPONSE_URL,
)

_PORTAL_TIMEOUT = aiohttp.ClientTimeout(total=20)
_SEARCH_URL_RE = re.compile(r'/PreCom/ReportMessage/SearchMessage\?[^"]+')
_OVERVIEW_DATASOURCE_RE = re.compile(r'"dataSource":(\[[\s\S]*?\]),"value"')


class PreComPortalError(Exception):
    """Raised when portal.pre-com.nl report lookups fail."""


def _normalize_portal_text(value: str) -> str:
    """Collapse whitespace so HTML text blocks compare reliably."""
    return " ".join(value.split())


class _PortalMessageDetailsParser(HTMLParser):
    """Parse staffing summary and proposed functions from MessageDetails HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.benodigd: list[dict[str, Any]] = []
        self.voorgestelde_functies: list[dict[str, Any]] = []
        self._depth = 0
        self._form_wrapper_index = 0
        self._details_wrapper_depth: int | None = None
        self._proposal_depth: int | None = None
        self._seen_benodigd_header = False
        self._row_depth: int | None = None
        self._row_context: str | None = None
        self._row_columns: list[str] = []
        self._column_depth: int | None = None
        self._column_chunks: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Track div rows/columns for the relevant sections."""
        if tag != "div":
            return

        self._depth += 1
        class_attr = dict(attrs).get("class") or ""
        classes = set(class_attr.split())

        if "formPrecomWrapper" in classes:
            self._form_wrapper_index += 1
            if self._form_wrapper_index == 1:
                self._details_wrapper_depth = self._depth
                self._seen_benodigd_header = False

        if "RProposalServiceFunction" in classes:
            self._proposal_depth = self._depth

        if "row" in classes and (
            self._details_wrapper_depth is not None or self._proposal_depth is not None
        ):
            self._row_depth = self._depth
            self._row_context = (
                "proposal" if self._proposal_depth is not None else "details"
            )
            self._row_columns = []

        if self._row_depth is not None and any(
            current.startswith("col-") or current.startswith("col-md-")
            for current in classes
        ):
            self._column_depth = self._depth
            self._column_chunks = []

    def handle_data(self, data: str) -> None:
        """Collect text inside the active column."""
        if self._column_depth is not None:
            self._column_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Close tracked rows/columns and emit structured output."""
        if tag != "div":
            return

        if self._column_depth == self._depth:
            self._row_columns.append(
                _normalize_portal_text("".join(self._column_chunks))
            )
            self._column_depth = None
            self._column_chunks = []

        if self._row_depth == self._depth:
            self._finalize_row()
            self._row_depth = None
            self._row_context = None
            self._row_columns = []

        if self._proposal_depth == self._depth:
            self._proposal_depth = None

        if self._details_wrapper_depth == self._depth:
            self._details_wrapper_depth = None
            self._seen_benodigd_header = False

        self._depth -= 1

    def _finalize_row(self) -> None:
        """Route a completed HTML row to the matching section parser."""
        if self._row_context == "details":
            self._finalize_details_row()
        elif self._row_context == "proposal":
            self._finalize_proposal_row()

    def _finalize_details_row(self) -> None:
        """Capture Benodigd rows once the section header has appeared."""
        if not self._row_columns:
            return

        first = self._row_columns[0]
        if first == "Benodigd":
            self._seen_benodigd_header = True
            return

        if not self._seen_benodigd_header or len(self._row_columns) < 3:
            return

        self.benodigd.append(
            {
                "Naam": first,
                "Aantal": self._row_columns[1],
                "Percentage": self._row_columns[2],
            }
        )

    def _finalize_proposal_row(self) -> None:
        """Capture the proposed function rows."""
        if len(self._row_columns) < 2:
            return

        functie_naam = self._row_columns[0]
        full_name = self._row_columns[1]
        if not functie_naam or not full_name:
            return

        self.voorgestelde_functies.append(
            {"FunctieNaam": functie_naam, "FullName": full_name}
        )


class PreComHtmlScraper:
    """Handles portal.pre-com.nl scraping for the latest alarm report."""

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._username = username
        self._password = password
        self._session = session

    def _portal_base_headers(self) -> dict[str, str]:
        """Return the common headers used for portal requests."""
        return {
            "Accept-Language": "nl,en;q=0.9",
            "DNT": "1",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
            ),
        }

    def _portal_document_headers(self) -> dict[str, str]:
        """Headers for full HTML document requests."""
        return {
            **self._portal_base_headers(),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Upgrade-Insecure-Requests": "1",
        }

    def _portal_ajax_html_headers(self) -> dict[str, str]:
        """Headers for same-origin HTML/XHR responses."""
        return {
            **self._portal_base_headers(),
            "Accept": "text/html, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }

    def _portal_ajax_json_headers(self) -> dict[str, str]:
        """Headers for same-origin JSON/XHR responses."""
        return {
            **self._portal_base_headers(),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }

    def _portal_form_headers(self, referer: str | None = None) -> dict[str, str]:
        """Headers for form posts inside the portal."""
        headers = {
            **self._portal_base_headers(),
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://portal.pre-com.nl",
            "X-Requested-With": "XMLHttpRequest",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    async def _portal_request_text(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        expected_statuses: tuple[int, ...] = (200,),
    ) -> str:
        """Perform a portal request and return the response body as text."""
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                params=params,
                data=data,
                timeout=_PORTAL_TIMEOUT,
            ) as response:
                body = await response.text()
                if response.status not in expected_statuses:
                    raise PreComPortalError(
                        f"Portal request to {url} returned HTTP {response.status}"
                    )
                return body
        except aiohttp.ClientError as err:
            raise PreComPortalError(
                f"Network error during portal request to {url}: {err}"
            ) from err

    async def _portal_request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        expected_type: type[list] | type[dict],
    ) -> list[Any] | dict[str, Any]:
        """Perform a portal request and decode the JSON body."""
        body = await self._portal_request_text(
            method,
            url,
            headers=headers,
            params=params,
            data=data,
        )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as err:
            raise PreComPortalError(f"Portal endpoint {url} returned invalid JSON") from err

        if not isinstance(payload, expected_type):
            raise PreComPortalError(
                f"Portal endpoint {url} returned {type(payload).__name__}, "
                f"expected {expected_type.__name__}"
            )
        return payload

    async def _portal_login(self) -> None:
        """Log into portal.pre-com.nl using the configured credentials."""
        await self._portal_request_text(
            "GET",
            PORTAL_LOGIN_URL,
            headers=self._portal_document_headers(),
        )
        raw_response = await self._portal_request_text(
            "POST",
            PORTAL_POST_LOGIN_URL,
            headers=self._portal_form_headers(PORTAL_LOGIN_URL),
            data={"UserName": self._username, "Password": self._password},
        )

        login_succeeded = False
        try:
            payload = json.loads(raw_response)
            if isinstance(payload, dict):
                login_succeeded = bool(payload.get("Succesfully"))
        except json.JSONDecodeError:
            login_succeeded = '"Succesfully":true' in raw_response

        if not login_succeeded:
            raise PreComPortalError("Portal authentication failed")

        self._session.cookie_jar.update_cookies(
            {".AspNetCore.Culture": "c%3Dnl-NL%7Cuic%3Dnl-NL"},
            response_url=URL(PORTAL_HOME_URL),
        )

    async def _portal_prepare_report_context(self) -> None:
        """Load the report module context before opening the overview page."""
        await self._portal_request_text(
            "GET",
            PORTAL_HOME_URL,
            headers=self._portal_document_headers(),
        )

        nodes = await self._portal_request_json(
            "GET",
            PORTAL_NAVIGATION_NODES_URL,
            headers=self._portal_ajax_json_headers(),
            expected_type=list,
        )
        if not nodes:
            raise PreComPortalError("Portal returned no navigation nodes")
        active_node = nodes[0]

        top_modules = await self._portal_request_json(
            "GET",
            PORTAL_NAVIGATION_MODULES_URL,
            headers=self._portal_ajax_json_headers(),
            expected_type=list,
        )
        report_module = next(
            (module for module in top_modules if module.get("name") == "Rapportage"),
            None,
        )
        if report_module is None:
            raise PreComPortalError("Portal module 'Rapportage' was not found")

        child_modules = await self._portal_request_json(
            "GET",
            PORTAL_NAVIGATION_MODULES_URL,
            headers=self._portal_ajax_json_headers(),
            params={"id": report_module.get("id")},
            expected_type=list,
        )
        report_message_module = next(
            (
                module
                for module in child_modules
                if module.get("name") == "Rapportage per bericht"
            ),
            None,
        )
        if report_message_module is None:
            raise PreComPortalError(
                "Portal module 'Rapportage per bericht' was not found"
            )

        await self._portal_request_text(
            "GET",
            PORTAL_MODULE_LOAD_URL,
            headers=self._portal_ajax_html_headers(),
            params={
                "nodeId": active_node.get("id"),
                "menuId": report_message_module.get("id"),
            },
        )

    @staticmethod
    def _extract_input_value(html: str, field_id: str) -> str:
        """Extract a hidden/input value from overview HTML."""
        match = re.search(
            rf'id="{re.escape(field_id)}"[^>]*value="([^"]*)"',
            html,
        )
        if not match:
            raise PreComPortalError(f"Could not find {field_id} in overview HTML")
        return match.group(1)

    def _get_group_candidates_from_overview_html(
        self, overview_html: str
    ) -> list[dict[str, str]]:
        """Return the available report groups shown in the overview page."""
        match = _OVERVIEW_DATASOURCE_RE.search(overview_html)
        if match:
            try:
                items = json.loads(match.group(1))
            except json.JSONDecodeError as err:
                raise PreComPortalError(
                    "Could not decode report groups from overview HTML"
                ) from err

            result = [
                {"GroupId": str(item.get("Value", "")), "Name": str(item.get("Text", ""))}
                for item in items
                if item.get("Value") not in (None, "")
            ]
            if result:
                return result

        return [
            {
                "GroupId": self._extract_input_value(overview_html, "GroupId"),
                "Name": "Default",
            }
        ]

    def _resolve_search_url_from_overview_html(
        self, overview_html: str, group_id: str
    ) -> str:
        """Build the report search URL, looking back 30 days from now.
        
        The portal's default From/To values are ignored to ensure we always
        search the last 30 days, regardless of the portal's current settings.
        """
        from datetime import timedelta
        working_day = self._extract_input_value(overview_html, "PeriodPartSelection")

        # Always search the last 30 days to ensure we find recent alarms
        now = datetime.now()
        from_dt = now - timedelta(days=30)
        to_dt = now

        return str(
            URL(f"{PORTAL_BASE_URL}/ReportMessage/Search").with_query(
                {
                    "GroupId": group_id,
                    "From": from_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "To": to_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "WorkingDay": "" if working_day in ("", "0") else working_day,
                }
            )
        )

    def _resolve_search_message_url_from_html(self, search_html: str) -> str:
        """Extract the SearchMessage URL from the search response HTML."""
        match = _SEARCH_URL_RE.search(search_html)
        if not match:
            raise PreComPortalError("Could not find SearchMessage URL in search HTML")
        relative_url = match.group(0).replace("\\u0026", "&")
        return urljoin(PORTAL_HOME_URL, relative_url)

    @staticmethod
    def _select_matching_message(
        rows: list[dict[str, Any]], alarm_text: str
    ) -> dict[str, Any] | None:
        """Return the first portal row whose Text exactly matches the alarm text."""
        target = _normalize_portal_text(alarm_text)
        for row in rows:
            if _normalize_portal_text(str(row.get("Text", ""))) == target:
                return row
        return None

    @staticmethod
    def _require_message_field(message: dict[str, Any], field: str) -> str:
        """Return a required message field as a non-empty string."""
        value = str(message.get(field, "")).strip()
        if not value:
            raise PreComPortalError(f"Selected portal message is missing {field}")
        return value

    def _build_message_details_url(self, message: dict[str, Any]) -> str:
        """Build the MessageDetails URL for a selected portal message."""
        query: dict[str, str] = {
            "messageInLogId": self._require_message_field(message, "MsgInLogID")
        }
        incident_log_id = str(message.get("IncidentLogID", "")).strip()
        if incident_log_id:
            query["incidentLogID"] = incident_log_id
        return str(URL(PORTAL_MESSAGE_DETAILS_URL).with_query(query))

    def _build_search_response_url(self, message: dict[str, Any]) -> str:
        """Build the SearchResponse URL for a selected portal message."""
        return str(
            URL(PORTAL_SEARCH_RESPONSE_URL).with_query(
                {"msgInLogId": self._require_message_field(message, "MsgInLogID")}
            )
        )

    @staticmethod
    def _parse_response_data(response_payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Keep only the requested response fields for HA attributes."""
        rows = response_payload.get("Data", [])
        if not isinstance(rows, list):
            raise PreComPortalError("SearchResponse payload did not contain a Data list")

        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            result.append(
                {
                    "FullName": _normalize_portal_text(str(row.get("FullName") or "")),
                    "ResponseTime": row.get("ResponseTime"),
                    "Available": row.get("Available"),
                    "Response": row.get("Response"),
                }
            )
        return result

    @staticmethod
    def _parse_message_details_html(
        message_details_html: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Parse Benodigd and VoorgesteldeFuncties from MessageDetails HTML."""
        parser = _PortalMessageDetailsParser()
        parser.feed(message_details_html)
        return parser.benodigd, parser.voorgestelde_functies

    async def get_alarm_portal_details(
        self, alarm_text: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch response and staffing details for the latest alarm from the portal."""
        normalized_alarm_text = _normalize_portal_text(alarm_text)
        if not normalized_alarm_text:
            return {
                "response_data": [],
                "benodigd": [],
                "voorgestelde_functies": [],
            }

        await self._portal_login()
        await self._portal_prepare_report_context()

        overview_html = await self._portal_request_text(
            "GET",
            PORTAL_OVERVIEW_URL,
            headers=self._portal_ajax_html_headers(),
        )

        selected_message: dict[str, Any] | None = None
        for group in self._get_group_candidates_from_overview_html(overview_html):
            search_url = self._resolve_search_url_from_overview_html(
                overview_html, group["GroupId"]
            )
            search_html = await self._portal_request_text(
                "GET",
                search_url,
                headers=self._portal_ajax_html_headers(),
            )
            search_message_url = self._resolve_search_message_url_from_html(search_html)
            search_message_payload = await self._portal_request_json(
                "GET",
                search_message_url,
                headers=self._portal_ajax_json_headers(),
                expected_type=dict,
            )
            rows = search_message_payload.get("Data", [])
            if not isinstance(rows, list):
                raise PreComPortalError("SearchMessage payload did not contain a Data list")
            selected_message = self._select_matching_message(rows, normalized_alarm_text)
            if selected_message is not None:
                break

        if selected_message is None:
            raise PreComPortalError(
                "Could not find a portal report row matching the latest alarm text"
            )

        message_details_html = await self._portal_request_text(
            "GET",
            self._build_message_details_url(selected_message),
            headers=self._portal_ajax_html_headers(),
        )
        response_payload = await self._portal_request_json(
            "POST",
            self._build_search_response_url(selected_message),
            headers=self._portal_form_headers(PORTAL_OVERVIEW_URL),
            data={"sort": "", "group": "", "filter": ""},
            expected_type=dict,
        )

        benodigd, voorgestelde_functies = self._parse_message_details_html(
            message_details_html
        )
        response_data = self._parse_response_data(response_payload)

        return {
            "response_data": response_data,
            "benodigd": benodigd,
            "voorgestelde_functies": voorgestelde_functies,
        }

    async def get_latest_alarm_for_group(
        self, group_id: str, group_label: str
    ) -> dict[str, Any] | None:
        """Fetch the latest alarm and its details for a specific group from the portal.
        
        Args:
            group_id: The numeric group ID to search for.
            group_label: The group label (for logging).
            
        Returns:
            Dict with alarm data if found, None if no alarms for this group.
            Dict contains: alarm_id, text, timestamp, response_data, benodigd, voorgestelde_functies
        """
        await self._portal_login()
        await self._portal_prepare_report_context()

        overview_html = await self._portal_request_text(
            "GET",
            PORTAL_OVERVIEW_URL,
            headers=self._portal_ajax_html_headers(),
        )

        # Search specifically for this group
        search_url = self._resolve_search_url_from_overview_html(overview_html, group_id)
        search_html = await self._portal_request_text(
            "GET",
            search_url,
            headers=self._portal_ajax_html_headers(),
        )
        search_message_url = self._resolve_search_message_url_from_html(search_html)
        search_message_payload = await self._portal_request_json(
            "GET",
            search_message_url,
            headers=self._portal_ajax_json_headers(),
            expected_type=dict,
        )
        
        rows = search_message_payload.get("Data", [])
        if not isinstance(rows, list) or not rows:
            # No messages found for this group
            return None
        
        # Take the first message (newest)
        latest_message = rows[0]
        
        # Extract basic alarm info
        alarm_id = str(latest_message.get("MsgInLogID", ""))
        text = str(latest_message.get("Text", ""))
        timestamp = latest_message.get("Timestamp", "")
        
        # Fetch portal details
        message_details_html = await self._portal_request_text(
            "GET",
            self._build_message_details_url(latest_message),
            headers=self._portal_ajax_html_headers(),
        )
        response_payload = await self._portal_request_json(
            "POST",
            self._build_search_response_url(latest_message),
            headers=self._portal_form_headers(PORTAL_OVERVIEW_URL),
            data={"sort": "", "group": "", "filter": ""},
            expected_type=dict,
        )

        benodigd, voorgestelde_functies = self._parse_message_details_html(
            message_details_html
        )
        response_data = self._parse_response_data(response_payload)

        return {
            "alarm_id": alarm_id,
            "text": text,
            "timestamp": timestamp,
            "response_data": response_data,
            "benodigd": benodigd,
            "voorgestelde_functies": voorgestelde_functies,
        }
