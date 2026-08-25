#!/bin/sh
# Container entrypoint: initialize private state and hand PID 1 to nginx.
# Destination resolution and supervisor creation happen atomically at Start.
set -eu
umask 077

. /usr/local/bin/relay-lib.sh
LOG_TAG=entrypoint

# Fresh state on every start. A previous unclean stop must not leave a stale
# "publisher is live" marker behind.
rm -rf "$STATE_DIR"
mkdir -p "$HOOK_DIR" "$CONTROL_DIR/commands" "$CONTROL_DIR/destination-commands" "$METRICS_DIR/hooks"

PROGRAM_ID="${PROGRAM_ID:-}"
CONTROL_TOKEN_FILE="${CONTROL_TOKEN_FILE:-/run/secrets/croccante-control-token}"
if [ -z "$PROGRAM_ID" ]; then
    log "ERROR: PROGRAM_ID is required"
    exit 1
fi
if [ ! -s "$CONTROL_TOKEN_FILE" ]; then
    log "ERROR: CONTROL_TOKEN_FILE must name a readable, non-empty secret file"
    exit 1
fi
atomic_write "$REQUESTED_STATE_FILE" stopped
atomic_write "$STATE_DIR/dest.count" 0

# The exec_publish hooks run as the nginx worker user, so they need a directory
# they can write. Everything else stays root-owned. Getting this wrong makes the
# hooks fail silently, because nginx discards their output.
chown nginx:nginx "$HOOK_DIR"
chmod 0775 "$HOOK_DIR"
chown nginx:nginx "$METRICS_DIR/hooks"
chmod 0775 "$METRICS_DIR/hooks"
: > "$HOOK_LOG"
chown nginx:nginx "$HOOK_LOG"

# nginx throws away exec_publish stdout. Surface the hook log on the container's
# stdout so publisher connect/disconnect is visible in `docker logs`.
tail -F "$HOOK_LOG" 2>/dev/null &

mkdir -p "${FILLER_STORE_DIR:-/var/lib/croccante/fillers}"

# The lifecycle surface is a separate process so nginx remains the RTMP PID 1.
# It receives the token through a mounted file, never a command-line argument.
/usr/local/bin/control-server.py &
atomic_write "$STATE_DIR/control.pid" "$!"

# ── nginx takes over as the foreground process ───────────────────────────────
log "starting nginx"
exec nginx -g 'daemon off;'
