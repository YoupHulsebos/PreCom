# PreCom for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Home Assistant integration for the [PreCom](https://portal.pre-com.nl) fire department alerting service. Monitors incoming P2000 alarms, shows staffing levels per function at your fire station, and lets you report your availability (inside or outside your response region) directly from Home Assistant.

## Requirements

- Home Assistant 2024.1.0 or newer
- A PreCom account (username + password)
- [HACS](https://hacs.xyz) installed

## Installation

### HACS (recommended)

1. Open HACS → **Integrations**.
2. Click the three-dot menu → **Custom repositories**.
3. Add this repository URL and select category **Integration**.
4. Search for **PreCom** and click **Download**.
5. Restart Home Assistant.

### Manual

1. Copy the `custom_components/precom` folder into your `<config>/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **PreCom**.
3. Enter your PreCom **username** and **password**.
4. Optionally set a **scan interval** (range: 10–3600 s). Leave blank to disable automatic polling — use `precom.update_alarm` to refresh on demand instead.

Credentials are validated against the PreCom API before the entry is saved.

## Entities

| Entity | Type | State |
|--------|------|-------|
| `sensor.precom_last_alarm` | Sensor | Alarm message text when active, `none` when idle |
| `sensor.precom_<group>_alarm` | Sensor (per group) | Latest alarm text for this group, `none` when no recent alarm |
| `sensor.precom_groups` | Sensor | Number of groups the user belongs to |
| `binary_sensor.precom_availability` | Binary sensor | `on` = available, `off` = unavailable |
| `binary_sensor.precom_staffing_<function>` | Binary sensor (per function) | `on` = staffing in next 24 h, `off` = sufficient |

### Last alarm sensor attributes

| Attribute | Description |
|-----------|-------------|
| `alarm_id` | Internal alarm ID |
| `text` | Alarm message text (same as entity state) |
| `timestamp` | Date/time of the alarm as returned by the API |
| `functions` | List of `{label, users}` objects for the alarm |
| `functions_formatted` | Human-readable text listing each function and its users |
| `ResponseData` | List of `{FullName, ResponseTime, Available, Response}` rows from the portal report |
| `Benodigd` | List of `{Naam, Aantal, Percentage}` rows from the portal staffing summary, including `Totaal` |
| `VoorgesteldeFuncties` | List of `{FunctieNaam, FullName}` rows from the portal proposed-function section |
| `last_updated` | ISO timestamp of the last successful poll |

### Groups sensor attributes

| Attribute | Description |
|-----------|-------------|
| `groups` | List of group objects as returned by the API |
| `last_updated` | ISO timestamp of the last successful poll |

### Availability sensor attributes

| Attribute | Description |
|-----------|-------------|
| `not_available_timestamp` | ISO timestamp when unavailability was set |
| `not_available_scheduled` | `true` when the absence is scheduler-driven |

### Group alarm sensor attributes

One sensor is created per group (e.g. `sensor.precom_velp_frb_en_vk_alarm`). The sensor shows the latest alarm for that specific group from the past 30 days. The state is the alarm message text or `none` when no recent alarm is found.

| Attribute | Description |
|-----------|-------------|
| `group_id` | Internal group ID |
| `group_label` | Group name (e.g. "Velp FRB en VK") |
| `alarm_id` | Internal alarm ID (MsgInLogID from portal) |
| `text` | Alarm message text (same as entity state) |
| `timestamp` | Date/time of the alarm from the portal |
| `response_data` | List of `{FullName, ResponseTime, Available, Response}` rows from the portal report |
| `benodigd` | List of `{Naam, Aantal, Percentage}` rows from the portal staffing summary |
| `voorgestelde_functies` | List of `{FunctieNaam, FullName}` rows from the portal proposed-function section |

**Note:** Group alarm sensors use portal scraping with a 30-day lookback window. This allows viewing historical alarms beyond the API's retention period. Data is updated every scan interval.

### Staffing sensor attributes

One binary sensor is created per function (e.g. `binary_sensor.precom_staffing_chauffeur_ts`). The sensor is `on` (problem) when any 15-minute slot in the next 24 hours has fewer available people than required.

| Attribute | Description |
|-----------|-------------|
| `number_needed` | Minimum staffing level required |
| `current_available` | Number of available people in the current 15-minute slot (from DayTotals) |
| `current_unavailable` | Number of people in the function who are currently marked unavailable |
| `shortage` | `max(0, number_needed − current_available)` |

## Services

### `precom.set_unavailable`

Mark yourself as **unavailable** (outside region) for a number of hours. Targets the `binary_sensor.precom_availability` entity.

| Field | Required | Description |
|-------|----------|-------------|
| `hours` | Yes | Duration in hours (1–72) |

### `precom.set_available`

Cancel the unavailable status and mark yourself as **available** again. Targets the `binary_sensor.precom_availability` entity. No fields required.

### `precom.update_alarm`

Force an immediate refresh of the latest alarm data from PreCom, without waiting for the next scheduled poll interval. Targets the `sensor.precom_last_alarm` entity. No fields required.

### `precom.get_alarm_portal_details`

Look up portal report details for a specific alarm message text and return portal response data, staffing requirements, and proposed functions.

| Field | Required | Description |
|-------|----------|-------------|
| `melding` | Yes | Exact alarm message text to search for in the portal report |

## Automation example

```yaml
automation:
  - alias: "Notify on new PreCom alarm"
    trigger:
      - platform: state
        entity_id: sensor.precom_last_alarm
    condition:
      - condition: template
        value_template: "{{ trigger.to_state.state != 'none' }}"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "PreCom Alarm"
          message: "{{ states('sensor.precom_last_alarm') }}"
```

## Troubleshooting

- **Invalid credentials on setup** — verify your username and password at [portal.pre-com.nl](https://portal.pre-com.nl).
- **Sensor stuck on `none`** — check that your account has access to alarm messages and review the Home Assistant logs for API errors.
- **No staffing sensors appearing** — the sensors are created dynamically after the first successful poll. Reload the integration if they do not appear after a minute.
- **Rate limiting / connectivity** — increase the scan interval in the integration options.
- **"Authentication failed" after working correctly** — Home Assistant will automatically prompt you to re-enter your password via a re-authentication notification. Go to **Settings → Integrations → PreCom** and follow the notification.
- **Reconfiguring after an email/password change** — click the three-dot menu on the integration card → **Reconfigure**, enter new credentials.

## How data is updated

The integration uses a polling model (`iot_class: cloud_polling`). Every `scan_interval` seconds (default 60 s, configurable 10–3600 s) the coordinator fetches alarm data, user info, and staffing data from the PreCom API. For staffing, both today's and tomorrow's data are fetched per group so that the next 24 hours can be evaluated at 15-minute granularity. The JWT token is cached in memory; if it is rejected (HTTP 401) the coordinator re-authenticates once and retries before marking the sensor unavailable.

No webhooks or push mechanisms are used.

## Use cases

- **Alarm notifications** — trigger a mobile notification, flash a light, or start a siren whenever `sensor.precom_last_alarm` changes to a non-`none` value.
- **Group-specific alarms** — monitor individual groups with `sensor.precom_<group>_alarm` to track alarms per station or unit, with 30-day history.
- **Automatic availability reporting** — use a device tracker or input boolean to automatically call `precom.set_unavailable` when you leave home.
- **Dashboard card** — display the latest alarm text and staffing levels on a Lovelace dashboard.
- **Understaffing alerts** — use the staffing binary sensors to send a notification when your fire station is short on a specific function in the coming hours.
- **Response tracking** — combined with `precom.set_available` / `precom.set_unavailable`, build automations that keep PreCom in sync with your physical location.

## Supported functionality

| Platform | Entity | State | Notes |
|----------|--------|-------|-------|
| Sensor | `sensor.precom_last_alarm` | Alarm text or `none` | Attributes: `alarm_id`, `text`, `timestamp`, `functions`, `functions_formatted`, `ResponseData`, `Benodigd`, `VoorgesteldeFuncties`, `last_updated` |
| Sensor | `sensor.precom_<group>_alarm` | Alarm text or `none` | Per group sensor with 30-day portal history. Attributes: `group_id`, `group_label`, `alarm_id`, `text`, `timestamp`, `response_data`, `benodigd`, `voorgestelde_functies` |
| Sensor | `sensor.precom_groups` | Number of groups | Attributes: `groups`, `last_updated` |
| Binary sensor | `binary_sensor.precom_availability` | `on` = available | Attributes: `not_available_timestamp`, `not_available_scheduled` |
| Binary sensor | `binary_sensor.precom_staffing_<function>` | `on` = staffing | Attributes: `number_needed`, `current_available`, `current_unavailable`, `shortage` |

| Service | Target entity | Description |
|---------|---------------|-------------|
| `precom.set_unavailable` | `binary_sensor.precom_availability` | Mark user unavailable (1–72 h) |
| `precom.set_available` | `binary_sensor.precom_availability` | Cancel unavailable status |
| `precom.update_alarm` | `sensor.precom_last_alarm` | Force immediate alarm data refresh |
| `precom.get_alarm_portal_details` | None | Return `response_data`, `benodigd`, and `voorgestelde_functies` for a supplied alarm text |

## Known limitations

- **Polling only** — no push support; minimum latency equals the configured scan interval.
- **No P2000 raw messages** — alarm data is what PreCom exposes via its own API; raw P2000 data is not available.
- **Single device per entry** — each config entry covers one PreCom account. Multiple accounts require multiple entries.
- **Limited alarm history** — `sensor.precom_last_alarm` shows only the current active alarm. Group sensors (`sensor.precom_<group>_alarm`) show the latest alarm from the past 30 days per group via portal scraping.
- **Dynamic sensors** — group sensors and staffing sensors are created dynamically after the first successful poll. Removing a group or function from PreCom does not automatically remove the corresponding entity from Home Assistant.
- **Portal scraping overhead** — group alarm sensors scrape the portal per group per update cycle. For accounts with many groups, consider increasing the scan interval to reduce load.

## Removing the integration

1. Go to **Settings → Devices & Services**.
2. Find the **PreCom** integration and click the three-dot menu → **Delete**.
3. Confirm deletion. Home Assistant will unload the integration and remove all associated entities and devices.
4. No files are left behind after removal.

## Contributing

Issues and pull requests are welcome. Please open an issue before submitting large changes.
