"""Constants for the PreCom integration."""

DOMAIN = "precom"

# Config entry keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 60

# API endpoints
API_BASE_URL = "https://app.pre-com.nl"
API_TOKEN_URL = f"{API_BASE_URL}/Token"
API_ALARMS_URL = f"{API_BASE_URL}/api/v2/Message/GetAlarmMessages"
API_SET_OUTSIDE_REGION_URL = f"{API_BASE_URL}/api/v2/Available/SetOutsideRegion"
API_USER_INFO_URL = f"{API_BASE_URL}/api/v2/User/GetUserInfo"
API_GROUPS_URL = f"{API_BASE_URL}/api/v2/Group/GetAllGroups"
API_USER_GROUPS_URL = f"{API_BASE_URL}/api/v2/Group/GetAllUserGroups"
API_GROUP_FUNCTIONS_URL = f"{API_BASE_URL}/api/v2/Group/GetAllFunctions"

# Portal endpoints
PORTAL_BASE_URL = "https://portal.pre-com.nl/PreCom"
PORTAL_LOGIN_URL = f"{PORTAL_BASE_URL}/Account/Login"
PORTAL_POST_LOGIN_URL = f"{PORTAL_BASE_URL}/Account/PostLogin"
PORTAL_HOME_URL = PORTAL_BASE_URL
PORTAL_NAVIGATION_NODES_URL = f"{PORTAL_BASE_URL}/Navigation/GetNodes"
PORTAL_NAVIGATION_MODULES_URL = f"{PORTAL_BASE_URL}/Navigation/GetModules"
PORTAL_MODULE_LOAD_URL = f"{PORTAL_BASE_URL}/Module/Load"
PORTAL_OVERVIEW_URL = f"{PORTAL_BASE_URL}/ReportMessage/Overview"
PORTAL_MESSAGE_DETAILS_URL = f"{PORTAL_BASE_URL}/ReportMessage/MessageDetails"
PORTAL_SEARCH_RESPONSE_URL = f"{PORTAL_BASE_URL}/ReportUser/SearchResponse"

# Service names
SERVICE_SET_UNAVAILABLE = "set_unavailable"
SERVICE_SET_AVAILABLE = "set_available"
SERVICE_UPDATE_ALARM = "update_alarm"
SERVICE_GET_ALARM_PORTAL_DETAILS = "get_alarm_portal_details"
SERVICE_GEOCODE_MELDING = "geocode_melding"

# Service field names
ATTR_HOURS = "hours"
ATTR_MELDING = "melding"

# Sensor state when no alarm is active
STATE_NO_ALARM = "none"

# Sensor extra attribute keys
ATTR_ALARM_ID = "alarm_id"
ATTR_LOCATION = "location"
ATTR_COORDINATES = "coordinates"
ATTR_FUNCTIONS = "functions"
ATTR_FUNCTIONS_FORMATTED = "functions_formatted"
ATTR_LAST_UPDATED = "last_updated"
ATTR_TEXT = "text"
ATTR_TIMESTAMP = "timestamp"
ATTR_RESPONSE_DATA = "ResponseData"
ATTR_BENODIGD = "Benodigd"
ATTR_VOORGESTELDE_FUNCTIES = "VoorgesteldeFuncties"

# Availability sensor attribute keys
ATTR_NOT_AVAILABLE_TIMESTAMP = "not_available_timestamp"
ATTR_NOT_AVAILABLE_SCHEDULED = "not_available_scheduled"

# Groups sensor attribute keys
ATTR_GROUPS = "groups"
ATTR_GROUP_ID = "group_id"

# Staffing sensor attribute keys
ATTR_GROUP_LABEL = "group_label"
ATTR_FUNCTION_LABEL = "function_label"
ATTR_NUMBER_NEEDED = "number_needed"
ATTR_CURRENT_COUNT = "current_count"
ATTR_CURRENT_AVAILABLE = "current_available"
ATTR_CURRENT_UNAVAILABLE = "current_unavailable"
ATTR_SHORTAGE = "shortage"
