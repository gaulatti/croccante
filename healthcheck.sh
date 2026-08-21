#!/bin/sh
# Healthy means: nginx is accepting RTMP, every destination supervisor is
# alive, and no supervisor is asleep at the wheel while a publisher is live.
#
# A supervisor legitimately sits in "idle" (no publisher) or "backoff" (the
# destination is rejecting us). Neither is unhealthy on its own. What is
# unhealthy: a supervisor process that died, or one still reporting "idle"
# while a publisher is connected, or one claiming to relay with no ffmpeg.

set -u
. /usr/local/bin/relay-lib.sh

nc -z 127.0.0.1 1935 || { echo "nginx not accepting on 1935"; exit 1; }

COUNT=$(cat "$STATE_DIR/dest.count" 2>/dev/null) || { echo "no destination count"; exit 1; }

PUBLISHER_LIVE=0
PUBLISHER_AGE=0
if [ -f "$PUBLISHER_FILE" ]; then
    PUBLISHER_LIVE=1
    _since=$(stat -c %Y "$PUBLISHER_FILE" 2>/dev/null || echo 0)
    PUBLISHER_AGE=$(( $(date +%s) - _since ))
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

    # Supervisors poll once a second, so allow a short grace period after a
    # publisher connects before "still idle" counts against them.
    if [ "$PUBLISHER_LIVE" -eq 1 ] && [ "$state" = "idle" ] && [ "$PUBLISHER_AGE" -gt 5 ]; then
        echo "supervisor $i idle while a publisher is live"
        exit 1
    fi

    if [ "$state" = "relaying" ] && ! pid_alive "$STATE_DIR/dest-$i.ffmpeg.pid"; then
        echo "supervisor $i claims to be relaying but has no ffmpeg"
        exit 1
    fi

    i=$((i + 1))
done

echo "ok: nginx up, $COUNT supervisor(s) healthy, publisher_live=$PUBLISHER_LIVE, nginx_publishers=${NGINX_PUBLISHERS:-0}"
