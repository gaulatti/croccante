#!/bin/sh
# Container entrypoint: resolve destinations, start one supervisor each,
# then hand PID 1 to nginx.
set -eu

. /usr/local/bin/relay-lib.sh
LOG_TAG=entrypoint

MAX_SLOTS="${RELAY_MAX_SLOTS:-20}"

# Fresh state on every start. A previous unclean stop must not leave a stale
# "publisher is live" marker behind.
rm -rf "$STATE_DIR"
mkdir -p "$HOOK_DIR" "$CONTROL_DIR/commands" "$METRICS_DIR/hooks"

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

# ── Resolve destinations ─────────────────────────────────────────────────────
# RELAY_DEST_1 … RELAY_DEST_<MAX_SLOTS>. Empty and absent slots are skipped;
# slot numbers need not be contiguous. Supervisors are numbered densely from 1
# regardless of which slots were populated.
DEST_COUNT=0
slot=1
while [ "$slot" -le "$MAX_SLOTS" ]; do
    eval "URL=\${RELAY_DEST_${slot}:-}"
    if [ -n "$URL" ]; then
        DEST_COUNT=$((DEST_COUNT + 1))
        atomic_write "$STATE_DIR/dest-${DEST_COUNT}.url" "$URL"
        log "destination $DEST_COUNT (from RELAY_DEST_${slot}): $(mask_dest "$URL")"
    fi
    slot=$((slot + 1))
done

if [ "$DEST_COUNT" -eq 0 ]; then
    log "ERROR: no destinations configured."
    log "Set at least RELAY_DEST_1 to a full URL, e.g."
    log "  RELAY_DEST_1=rtmp://a.rtmp.youtube.com/live2/<key>"
    log "Slots RELAY_DEST_1..${MAX_SLOTS} are scanned; raise RELAY_MAX_SLOTS for more."
    exit 1
fi

atomic_write "$STATE_DIR/dest.count" "$DEST_COUNT"
log "$DEST_COUNT destination(s) configured"

# ── Build the filler asset once, before any supervisor can need it ──────────
/usr/local/bin/make-filler.sh

# ── Start one supervisor per destination ─────────────────────────────────────
i=1
while [ "$i" -le "$DEST_COUNT" ]; do
    /usr/local/bin/relay-dest.sh "$i" &
    atomic_write "$STATE_DIR/dest-${i}.wrapper.pid" "$!"
    i=$((i + 1))
done

# The lifecycle surface is a separate process so nginx remains the RTMP PID 1.
# It receives the token through a mounted file, never a command-line argument.
/usr/local/bin/control-server.py &
atomic_write "$STATE_DIR/control.pid" "$!"

# ── nginx takes over as the foreground process ───────────────────────────────
log "starting nginx"
exec nginx -g 'daemon off;'
