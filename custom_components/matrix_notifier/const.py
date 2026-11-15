"""Constants for Matrix Notifier."""
DOMAIN = "matrix_notifier"

CONF_HOMESERVER = "homeserver"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DEVICE_ID = "device_id"
CONF_ROOMS = "rooms"
CONF_ROOM_ID = "room_id"
CONF_VERIFY_SSL = "verify_ssl"

SUBENTRY_RULE = "notification_rule"

CONF_NAME = "name"
CONF_ENTITY_ID = "entity_id"
CONF_AUTOMATION = "automation"
CONF_TO_STATE = "to_state"
CONF_MESSAGE = "message"
CONF_CAMERA = "camera"
CONF_CAMERA_FILENAME = "camera_filename"
CONF_ENABLED = "enabled"
CONF_VIDEO = "video"
CONF_VIDEO_DURATION = "video_duration"
CONF_VIDEO_QUALITY = "video_quality"
CONF_MEDIA_MODE = "media_mode"

CONF_CHANNELS = "channels"
CONF_TELEGRAM_BOT_TOKEN = "telegram_bot_token"
CONF_TELEGRAM_CHATS = "telegram_chats"

CHANNEL_MATRIX = "matrix"
CHANNEL_TELEGRAM = "telegram"
CHANNELS = [CHANNEL_MATRIX, CHANNEL_TELEGRAM]

MEDIA_MODE_NONE = "none"
MEDIA_MODE_SNAPSHOT = "snapshot"
MEDIA_MODE_VIDEO = "video"
MEDIA_MODE_BOTH = "both"
MEDIA_MODES = [
    MEDIA_MODE_NONE,
    MEDIA_MODE_SNAPSHOT,
    MEDIA_MODE_VIDEO,
    MEDIA_MODE_BOTH,
]

VIDEO_QUALITY_ORIGINAL = "original"
VIDEO_QUALITY_720P = "720p"
VIDEO_QUALITY_480P = "480p"
VIDEO_QUALITIES = [
    VIDEO_QUALITY_ORIGINAL,
    VIDEO_QUALITY_720P,
    VIDEO_QUALITY_480P,
]

DEFAULT_DEVICE_ID = "HANOTIFIER"
DEFAULT_VERIFY_SSL = True
DEFAULT_TO_STATE = "on"
DEFAULT_CAMERA_FILENAME = "snapshot.jpg"

ATTR_ROOM_ID = "room_id"
ATTR_MESSAGE = "message"
ATTR_MESSAGE_TYPE = "message_type"
ATTR_TITLE = "title"
ATTR_IMAGE = "image"

SERVICE_SEND = "send"
