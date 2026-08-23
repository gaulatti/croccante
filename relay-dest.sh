#!/bin/sh
# One long-lived supervisor per destination.
#
# Invoked as: relay-dest.sh <index>
# Reads its destination URL from $STATE_DIR/dest-<index>.url so no key material
# is ever baked into a generated script or passed on the command line.
#
# Lifecycle:
#   idle      — explicitly stopped; nothing is pushed
#   waiting   — started but no publisher has arrived in this session
#   relaying  — publisher live; ffmpeg copies the stream to this destination
#   filler    — session open but publisher gone; the pre-encoded filler asset
#               is looped to this destination so the outbound leg never starves
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
    STREAM=""
    if [ -f "$PUBLISHER_FILE" ]; then
        STREAM=$(cat "$PUBLISHER_FILE" 2>/dev/null)
    fi

    if ! session_started; then
        set_state idle
        sleep 1
        continue
    fi

    if [ -z "${STREAM:-}" ] && [ ! -f "$PUBLISHER_SEEN_FILE" ]; then
        set_state waiting
        sleep 1
        continue
    fi

    if [ -n "${STREAM:-}" ]; then
        MODE=relaying
        SRC="rtmp://127.0.0.1:1935/$RTMP_APP/$STREAM"
        log "relaying $SRC -> $MASKED"
    else
        MODE=filler
        SRC="$FILLER_FILE"
        log "publisher gone; filling -> $MASKED"
    fi
    set_state "$MODE"

    started=$(date +%s)

    # -c copy: no transcoding on the fan-out path.
    # -rw_timeout: bounds a stalled socket. (-stimeout is RTSP-only and was
    # removed in ffmpeg 6; using it here made the relay fail to start at all.)
    # Both modes are -c copy. Filler was encoded once by make-filler.sh at the
    # configured broadcast profile, so no encoder runs per destination and a
    # destination sees identical parameters across live/filler transitions.
    if [ "$MODE" = filler ]; then
        # -re paces the file at realtime; without it ffmpeg would blast the
        # whole loop at the destination as fast as the socket accepts it.
        # +genpts keeps timestamps monotonic across loop wraps.
        ffmpeg -hide_banner -loglevel warning \
            -re -stream_loop -1 -fflags +genpts \
            -i "$SRC" \
            -c copy \
            -rw_timeout "$RW_TIMEOUT" \
            -f flv "$DST" &
    else
        ffmpeg -hide_banner -loglevel warning \
            -rw_timeout "$RW_TIMEOUT" \
            -i "$SRC" \
            -c copy \
            -rw_timeout "$RW_TIMEOUT" \
            -f flv "$DST" &
    fi
    ff=$!
    atomic_write "$FFMPEG_PID_FILE" "$ff"

    # Tear the relay down promptly when the publisher goes away.
    #
    # This cannot be left to relay-stop.sh: the nginx hooks run as the nginx
    # worker user and ffmpeg runs as root, so the hook has no permission to
    # signal it. Nor should it be left to -rw_timeout, which would take the
    # full timeout to notice. The supervisor owns its own child, so it does
    # the teardown itself.
    # Tear the current mode down as soon as the world changes under it: in
    # relaying that means the publisher vanished, in filler it means the
    # publisher came back and we must stop pushing black frames over them.
    (
        while kill -0 "$ff" 2>/dev/null; do
            if ! session_started; then
                kill "$ff" 2>/dev/null
                break
            elif [ "$MODE" = filler ]; then
                [ -f "$PUBLISHER_FILE" ] && { kill "$ff" 2>/dev/null; break; }
            else
                [ -f "$PUBLISHER_FILE" ] || { kill "$ff" 2>/dev/null; break; }
            fi
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

    if ! session_started; then
        log "session stopped; destination idle"
        set_state idle
        backoff=1
        continue
    fi

    if [ "$MODE" = relaying ] && [ ! -f "$PUBLISHER_FILE" ]; then
        # Expected: the publisher dropped. The next iteration picks up filler.
        log "live relay ended after ${elapsed}s; switching to filler"
        backoff=1
        continue
    fi

    if [ "$MODE" = filler ] && [ -f "$PUBLISHER_FILE" ]; then
        # Expected: the publisher returned.
        log "filler ended after ${elapsed}s; switching to live"
        backoff=1
        continue
    fi

    if [ "$elapsed" -ge 30 ]; then
        # It ran for a meaningful stretch, so whatever ended it was transient.
        log "relay ended after ${elapsed}s (rc=$rc); reconnecting"
        backoff=1
    else
        log "relay failed after ${elapsed}s (rc=$rc); retrying in ${backoff}s"
        set_state "backoff"
        slept=0
        while [ "$slept" -lt "$backoff" ] && session_started; do
            sleep 1
            slept=$((slept + 1))
        done
        backoff=$(( backoff * 2 ))
        [ "$backoff" -gt "$BACKOFF_MAX" ] && backoff="$BACKOFF_MAX"
    fi
done
