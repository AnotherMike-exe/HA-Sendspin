#!/usr/bin/env bash
#
# ha_probe.sh — talk to the live Home Assistant instance over its REST API.
#
# Deliberately no SSH: everything here goes through documented HA endpoints with
# a long-lived access token. See docs/DEPLOYMENT-TESTING.md.
#
# Configuration comes from <repo-root>/.env (gitignored), or from the
# environment if already exported. Copy .env.example to .env to get started.
#
#   HA_BASE_URL   e.g. http://homeassistant.local:8123
#   HA_TOKEN      long-lived access token (NEVER commit this)

set -euo pipefail

RepoRoot="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EnvFile="${RepoRoot}/.env"

# Load .env without executing it — only KEY=VALUE lines, comments skipped.
# Already-exported environment variables win, so a one-off override still works.
if [[ -f "$EnvFile" ]]; then
  while IFS='=' read -r Key Value; do
    [[ "$Key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${Key// }" ]] && continue
    Key="${Key// }"
    Value="${Value%\"}"; Value="${Value#\"}"
    [[ -n "${!Key:-}" ]] || export "$Key=$Value"
  done < "$EnvFile"
fi

DOMAIN="sendspin"
LOGGER_TARGET="custom_components.${DOMAIN}"
UPDATE_ENTITY="${HA_UPDATE_ENTITY:-update.${DOMAIN}_update}"
RESTART_TIMEOUT_SECONDS=180

: "${HA_BASE_URL:?not set — copy .env.example to .env and fill it in}"
: "${HA_TOKEN:?not set — copy .env.example to .env and fill it in}"

Api() {
  # Api <method> <path> [json-body]
  local Method="$1" Path="$2" Body="${3:-}"
  local -a CurlArgs=(
    --silent --show-error --fail-with-body
    --request "$Method"
    --header "Authorization: Bearer ${HA_TOKEN}"
    --header "Content-Type: application/json"
  )
  [[ -n "$Body" ]] && CurlArgs+=(--data "$Body")
  curl "${CurlArgs[@]}" "${HA_BASE_URL}${Path}"
}

FetchLog() {
  # Two log sources, tried in order:
  #
  #   1. /api/hassio/core/logs  — Supervisor journal proxy. Works on HA OS and
  #      Supervised installs. Supports ?lines=N and persists across HA restarts.
  #   2. /api/error_log         — the classic REST endpoint. Returns 404 on this
  #      instance (HA 2026.8.1); kept as a fallback for Container/Core installs
  #      where the Supervisor proxy does not exist.
  # HA writes colour escapes into the journal; strip them so the output is
  # readable in a plain terminal and greppable without matching escape bytes.
  local Lines="${HA_LOG_LINES:-500}"
  local StripAnsi='s/\x1b\[[0-9;]*m//g'
  Api GET "/api/hassio/core/logs?lines=${Lines}" 2>/dev/null | sed "$StripAnsi" && return 0
  Api GET /api/error_log 2>/dev/null | sed "$StripAnsi" && return 0
  echo "Could not retrieve logs from the Supervisor proxy or /api/error_log." >&2
  echo "Check HA_BASE_URL and that the token is still valid." >&2
  return 1
}

CmdLogs() {
  local Pattern="${1:-}"
  if [[ -n "$Pattern" ]]; then
    FetchLog | grep -i --color=never "$Pattern" || {
      echo "No log lines matching '${Pattern}'." >&2
      return 0
    }
  else
    FetchLog
  fi
}

CmdDebug() {
  Api POST /api/services/logger/set_level \
    "{\"${LOGGER_TARGET}\": \"debug\", \"aiosendspin\": \"debug\"}" >/dev/null
  echo "Debug logging enabled for ${LOGGER_TARGET} and aiosendspin."
  echo "Note: this does NOT survive a restart."
}

CmdStates() {
  Api GET /api/states | python3 -c "
import json, sys
States = json.load(sys.stdin)
Matches = [S for S in States if 'sendspin' in json.dumps(S).lower()]
if not Matches:
    print('No sendspin-related entities found.', file=sys.stderr)
for S in Matches:
    print('%-50s %s' % (S['entity_id'], S['state']))
"
}

CmdUpdate() {
  echo "Triggering ${UPDATE_ENTITY} ..."
  Api POST /api/services/update/install \
    "{\"entity_id\": \"${UPDATE_ENTITY}\"}" >/dev/null
  echo "Update requested. HACS performs the same download as the UI button."
}

CmdRestart() {
  echo "Restarting Home Assistant ..."
  # The restart tears down the connection, so a failed call here is expected.
  Api POST /api/services/homeassistant/restart '{}' >/dev/null 2>&1 || true

  echo -n "Waiting for readiness"
  local Elapsed=0
  until Api GET /api/ >/dev/null 2>&1; do
    if (( Elapsed >= RESTART_TIMEOUT_SECONDS )); then
      echo
      echo "Timed out after ${RESTART_TIMEOUT_SECONDS}s waiting for HA." >&2
      return 1
    fi
    sleep 5
    Elapsed=$(( Elapsed + 5 ))
    echo -n "."
  done
  echo " ready (${Elapsed}s)."
}

CmdCycle() {
  CmdUpdate
  sleep 5
  CmdRestart
  CmdDebug
  echo "---- log ----"
  CmdLogs "${1:-sendspin}"
}

Usage() {
  cat <<'EOF'
Usage: ha_probe.sh <command> [args]

  logs [pattern]   Fetch recent logs, optionally filtered
  debug            Enable debug logging for the integration (until restart)
  states           List sendspin-related entities and their states
  update           Trigger the HACS update entity
  restart          Restart HA and wait until it is back
  cycle [pattern]  update -> restart -> enable debug -> show logs

Environment:
  HA_BASE_URL      required
  HA_TOKEN         required
  HA_LOG_LINES     lines to fetch (default 500)
  HA_UPDATE_ENTITY override the HACS update entity id

Config is read from <repo-root>/.env — see docs/DEPLOYMENT-TESTING.md
EOF
}

case "${1:-}" in
  logs)    shift; CmdLogs "${1:-}" ;;
  debug)   CmdDebug ;;
  states)  CmdStates ;;
  update)  CmdUpdate ;;
  restart) CmdRestart ;;
  cycle)   shift; CmdCycle "${1:-}" ;;
  *)       Usage; exit 1 ;;
esac
