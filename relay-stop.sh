#!/bin/sh
# nginx exec_publish_done hook. $1 = RTMP stream name.
#
# Clears the live stream name, which is the signal each destination supervisor
# watches to tear down its relay.

set -u
. /usr/local/bin/relay-lib.sh
LOG_TAG=relay-stop
LOG_TO_HOOK_FILE=1

log "publisher disconnected: stream='${1:-}'"

# Clearing the marker is the whole job. Each destination supervisor watches it
# and tears down its own ffmpeg within about a second. Doing it here would not
# work anyway: this hook runs as the nginx worker user and the relay processes
# belong to root.
rm -f "$PUBLISHER_FILE"
