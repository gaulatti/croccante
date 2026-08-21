#!/bin/sh
# One long-lived supervisor per destination.
#
# Invoked as: relay-dest.sh <index>
# Reads its destination URL from $STATE_DIR/dest-<index>.url so no key material
# is ever baked into a generated script or passed on the command line.
#
# Lifecycle:
#   idle      — no publisher connected; nothing is pushed
#   relaying  — publisher live; ffmpeg copies the stream to this destination
#   backoff   — the last relay attempt failed; waiting before retrying
#
# The loop never exits. A publisher gap, a dead network, or a rejecting
# destination all resolve themselves once conditions recover, with no restart.

set -u

IDX="$1"
. /usr/local/bin/relay-lib.sh

LOG_TAG="dest-$IDX"
URL_FILE="$STATE_DIR/dest-$IDX.url"
STATE_FILE="$STATE_DIR/dest-$IDX.state"
FFMPEG_PID_FILE="$STATE_DIR/dest-$IDX.ffmpeg.pid"

DST=$(cat "$URL_FILE")
MASKED=$(mask_dest "$DST")

RW_TIMEOUT="${RELAY_RW_TIMEOUT:-10000000}"
BACKOFF_MAX="${RELAY_BACKOFF_MAX:-30}"
RTMP_APP="${RTMP_APP:-live}"

backoff=1

set_state() {
    atomic_write "$STATE_FILE" "$1"
}

# On container stop, take the child with us rather than orphaning it.
cleanup() {
    if [ -f "$FFMPEG_PID_FILE" ]; then
        p=$(cat "$FFMPEG_PID_FILE" 2>/dev/null)
        [ -n "${p:-}" ] && kill "$p" 2>/dev/null
    fi
    rm -f "$FFMPEG_PID_FILE"
    set_state idle
    exit 0
}
trap cleanup TERM INT

log "supervisor up, destination: $MASKED"
set_state idle

while true; do
    if [ ! -f "$PUBLISHER_FILE" ]; then
        set_state idle
        sleep 1
        continue
    fi

    STREAM=$(cat "$PUBLISHER_FILE" 2>/dev/null)
    if [ -z "${STREAM:-}" ]; then
        sleep 1
        continue
    fi

    SRC="rtmp://127.0.0.1:1935/$RTMP_APP/$STREAM"
    log "relaying $SRC -> $MASKED"
    set_state relaying

    started=$(date +%s)

    # -c copy: no transcoding on the fan-out path.
    # -rw_timeout: bounds a stalled socket. (-stimeout is RTSP-only and was
    # removed in ffmpeg 6; using it here made the relay fail to start at all.)
    ffmpeg -hide_banner -loglevel warning \
        -rw_timeout "$RW_TIMEOUT" \
        -i "$SRC" \
        -c copy \
        -rw_timeout "$RW_TIMEOUT" \
        -f flv "$DST" &
    ff=$!
    atomic_write "$FFMPEG_PID_FILE" "$ff"

    # Tear the relay down promptly when the publisher goes away.
    #
    # This cannot be left to relay-stop.sh: the nginx hooks run as the nginx
    # worker user and ffmpeg runs as root, so the hook has no permission to
    # signal it. Nor should it be left to -rw_timeout, which would take the
    # full timeout to notice. The supervisor owns its own child, so it does
    # the teardown itself.
    (
        while kill -0 "$ff" 2>/dev/null; do
            [ -f "$PUBLISHER_FILE" ] || { kill "$ff" 2>/dev/null; break; }
            sleep 1
        done
    ) &
    watchdog=$!

    wait "$ff"
    rc=$?
    kill "$watchdog" 2>/dev/null
    wait "$watchdog" 2>/dev/null
    rm -f "$FFMPEG_PID_FILE"

    elapsed=$(( $(date +%s) - started ))

    if [ ! -f "$PUBLISHER_FILE" ]; then
        # Publisher went away. Expected end of a session, not a failure.
        log "publisher gone after ${elapsed}s; back to idle"
        backoff=1
        set_state idle
        continue
    fi

    if [ "$elapsed" -ge 30 ]; then
        # It ran for a meaningful stretch, so whatever ended it was transient.
        log "relay ended after ${elapsed}s (rc=$rc); reconnecting"
        backoff=1
    else
        log "relay failed after ${elapsed}s (rc=$rc); retrying in ${backoff}s"
        set_state "backoff"
        sleep "$backoff"
        backoff=$(( backoff * 2 ))
        [ "$backoff" -gt "$BACKOFF_MAX" ] && backoff="$BACKOFF_MAX"
    fi
done
