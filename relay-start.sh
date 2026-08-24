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
metric_inc "hooks/publisher-connect.total"

atomic_write "$PUBLISHER_FILE" "${1:-}"

# A publisher never starts a session. If an authenticated Start is already in
# force, remember that this session has carried live media so a later gap uses
# filler. A publisher connected while stopped remains withheld.
if session_started; then
    atomic_write "$PUBLISHER_SEEN_FILE" "$(date +%s)"
fi
