#!/bin/sh
# Healthy means: nginx and the lifecycle controller are accepting work, every
# destination supervisor is alive, and its state agrees with the explicit
# requested session state.
#
# A supervisor legitimately sits in "idle" (no publisher) or "backoff" (the
# destination is rejecting us). Neither is unhealthy on its own. What is
# unhealthy: a supervisor process that died, or one still reporting "idle"
# while a publisher is connected, or one claiming to relay with no ffmpeg.

set -u
. /usr/local/bin/relay-lib.sh

nc -z 127.0.0.1 1935 || { echo "nginx not accepting on 1935"; exit 1; }
pid_alive "$STATE_DIR/control.pid" || { echo "lifecycle control server is dead"; exit 1; }

COUNT=$(cat "$STATE_DIR/dest.count" 2>/dev/null) || { echo "no destination count"; exit 1; }
case "$COUNT" in ''|*[!0-9]*) echo "invalid destination count"; exit 1 ;; esac

PUBLISHER_LIVE=0
PUBLISHER_AGE=0
if [ -f "$PUBLISHER_FILE" ]; then
    PUBLISHER_LIVE=1
    _since=$(stat -c %Y "$PUBLISHER_FILE" 2>/dev/null || echo 0)
    PUBLISHER_AGE=$(( $(date +%s) - _since ))
fi

REQUESTED_STATE=$(cat "$REQUESTED_STATE_FILE" 2>/dev/null) || { echo "no requested session state"; exit 1; }
case "$REQUESTED_STATE" in
    started|stopped) ;;
    *) echo "invalid requested session state"; exit 1 ;;
esac
if [ "$REQUESTED_STATE" = "started" ] && ! active_filler_file >/dev/null; then
    echo "active session has no valid prepared filler"
    exit 1
fi
if [ "$REQUESTED_STATE" = "started" ] && [ "$COUNT" -eq 0 ]; then
    echo "active session has no destination supervisors"
    exit 1
fi

# nginx is the authority on whether a stream is actually arriving. If it sees a
# publisher and we do not, the exec_publish hook is broken — a failure mode that
# is otherwise completely silent, since nginx discards hook output.
NGINX_PUBLISHERS=$(nginx_publisher_count)
if [ "${NGINX_PUBLISHERS:-0}" -gt 0 ] && [ "$PUBLISHER_LIVE" -eq 0 ] && [ "$PUBLISHER_AGE" -eq 0 ]; then
    echo "nginx reports $NGINX_PUBLISHERS publisher(s) but no publisher marker exists; exec_publish hook is failing"
    exit 1
fi

i=1
while [ "$i" -le "$COUNT" ]; do
    pid_alive "$STATE_DIR/dest-$i.wrapper.pid" || { echo "supervisor $i is dead"; exit 1; }

    state=$(cat "$STATE_DIR/dest-$i.state" 2>/dev/null || echo unknown)

    if [ "$REQUESTED_STATE" = "stopped" ]; then
        if [ "$state" != "idle" ] || pid_alive "$STATE_DIR/dest-$i.ffmpeg.pid"; then
            echo "supervisor $i is not idle while the session is stopped"
            exit 1
        fi
        i=$((i + 1))
        continue
    fi

    # Started before the first publisher is a healthy waiting state and must
    # not connect a destination. Once media has appeared, idle/waiting would
    # starve the explicitly active session.
    if [ ! -f "$PUBLISHER_SEEN_FILE" ] && [ "$state" = "waiting" ]; then
        i=$((i + 1))
        continue
    fi
    if [ "$state" = "idle" ] || [ "$state" = "waiting" ]; then
        echo "supervisor $i is $state during an active media session"
        exit 1
    fi

    if [ "$state" = "relaying" ] && ! pid_alive "$STATE_DIR/dest-$i.ffmpeg.pid"; then
        echo "supervisor $i claims to be relaying but has no ffmpeg"
        exit 1
    fi

    # Filler is healthy, but only if it is actually pushing. A filler-mode
    # supervisor with no ffmpeg is a starved outbound leg wearing a green badge.
    if [ "$state" = "filler" ] && ! pid_alive "$STATE_DIR/dest-$i.ffmpeg.pid"; then
        echo "supervisor $i claims to be filling but has no ffmpeg"
        exit 1
    fi

    i=$((i + 1))
done

echo "ok: requested_state=$REQUESTED_STATE, nginx up, control up, $COUNT supervisor(s) healthy, publisher_live=$PUBLISHER_LIVE, nginx_publishers=${NGINX_PUBLISHERS:-0}"
