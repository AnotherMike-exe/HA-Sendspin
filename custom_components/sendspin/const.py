"""Constants for the Sendspin integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "sendspin"

# --- Discovery -------------------------------------------------------------
# Sendspin uses TWO service types, in opposite directions:
#
#   _sendspin-server._tcp  advertised by SERVERS, port 8927. Clients dial these.
#   _sendspin._tcp         advertised by PLAYERS, port 8928. Servers dial these.
#
# Both are duplicated in manifest.json ("zeroconf") and the two MUST stay in
# sync. Verified against aiosendspin (server/server.py, client/listener.py) and
# Plum-Audio's backend/scripts/mesh/avahi.py.
ZEROCONF_SERVICE_TYPE_SERVER: Final = "_sendspin-server._tcp.local."
ZEROCONF_SERVICE_TYPE_PLAYER: Final = "_sendspin._tcp.local."

DEFAULT_SERVER_PORT: Final = 8927
DEFAULT_PLAYER_PORT: Final = 8928

# The only two TXT keys Sendspin publishes. `path` defaults when absent;
# `name` is frequently absent on third-party devices, which is why the mDNS
# instance name is needed as a fallback. See docs/OPEN-QUESTIONS.md §2.
TXT_KEY_PATH: Final = "path"
TXT_KEY_NAME: Final = "name"
DEFAULT_WEBSOCKET_PATH: Final = "/sendspin"

# Sendspin control traffic is cleartext ws:// — there is no wss anywhere in the
# protocol, aiosendspin, or Plum-Audio. Do not "upgrade" this.
WEBSOCKET_SCHEME: Final = "ws"

# --- Endpoint identity -----------------------------------------------------
# Entities are keyed on the Sendspin listener URL as FIRST SEEN, frozen for the
# life of the entity — a client's id is not stable across the two identity
# views, and the live dial URL moves with DHCP. The mDNS instance name is the
# secondary matcher that lets a moved device update its dial URL instead of
# orphaning the entity. See docs/OPEN-QUESTIONS.md §2.
CONF_LISTENER_URL: Final = "listener_url"
CONF_DIAL_URL: Final = "dial_url"
CONF_MDNS_INSTANCE_NAME: Final = "mdns_instance_name"
CONF_SERVER_NAME: Final = "server_name"

# --- Services --------------------------------------------------------------
# Routing is NOT a service: it is media_player.select_source on the endpoint
# entity. These three are the adoption lifecycle only. No service accepts a
# player_id — services target HA entities/devices, and the only accepted raw
# identifier is the listener URL.
SERVICE_ADOPT_PLAYER: Final = "adopt_player"
SERVICE_RELEASE_PLAYER: Final = "release_player"
SERVICE_RECLAIM_PLAYER: Final = "reclaim_player"

ATTR_LISTENER_URL: Final = "listener_url"
ATTR_TIMEOUT: Final = "timeout"

# --- Source selection ------------------------------------------------------
# The source_list entry meaning "attached to nothing".
SOURCE_NONE: Final = "None"

# --- Tuning ----------------------------------------------------------------
# Membership in a Sendspin stream is fixed at start_stream(): a client added to
# a LIVE group joins silent. The fix is a membership window — stop_stream(),
# mutate membership, settle, start_stream(). The settle exists because Plum
# measured ~110ms as permanently wedging sendspin-cpp 0.7.0 clients.
# Hardware-tuned in M4; see docs/OPEN-QUESTIONS.md §3.
MEMBERSHIP_WINDOW_SETTLE_S: Final = 0.5
