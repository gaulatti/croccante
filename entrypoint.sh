#!/bin/sh
set -eu

# ── Validate required environment variables ───────────────────────────────────
MISSING=""
for VAR in YOUTUBE_STREAM_KEY TWITCH_STREAM_KEY FACEBOOK_STREAM_KEY; do
    eval "VAL=\${${VAR}:-}"
    if [ -z "$VAL" ]; then
        MISSING="${MISSING} ${VAR}"
    fi
done

if [ -n "$MISSING" ]; then
    echo "ERROR: The following required environment variables are not set:${MISSING}" >&2
    exit 1
fi

# ── Render nginx config from template ────────────────────────────────────────
envsubst '${YOUTUBE_STREAM_KEY} ${TWITCH_STREAM_KEY} ${FACEBOOK_STREAM_KEY}' \
    < /etc/nginx/nginx.conf.template \
    > /etc/nginx/nginx.conf

# ── Start stunnel in background ───────────────────────────────────────────────
# stunnel.conf uses foreground=yes but we background the whole process here
# so that nginx can be PID 1 for proper signal handling.
stunnel /etc/stunnel/stunnel.conf &
STUNNEL_PID=$!

echo "stunnel started (pid ${STUNNEL_PID})"

# ── Start nginx as PID 1 ─────────────────────────────────────────────────────
exec nginx -g 'daemon off;'
