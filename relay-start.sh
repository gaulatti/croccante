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

# A publisher never starts a session. Remember the observation here without
# granting the nginx worker read access to root-owned lifecycle state. The
# authenticated controller clears this marker on every Start and Stop boundary,
# then restores it during Start only when a publisher is currently connected.
# A publisher connected while stopped therefore remains withheld.
atomic_write "$PUBLISHER_SEEN_FILE" "$(date +%s)"
