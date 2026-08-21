#!/bin/sh
# nginx exec_publish hook. $1 = RTMP stream name.
#
# Records the live stream name. Each destination supervisor is already running
# and polling for this; it starts relaying on its own. Nothing is spawned here.

set -u
. /usr/local/bin/relay-lib.sh
LOG_TAG=relay-start
LOG_TO_HOOK_FILE=1

log "publisher connected: stream='${1:-}'"
atomic_write "$PUBLISHER_FILE" "${1:-}"
