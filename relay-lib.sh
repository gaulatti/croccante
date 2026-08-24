#!/bin/sh
# Shared helpers for the relay scripts. Sourced, not executed.

# Root-owned. Destination URLs, pid files and supervisor state live here so a
# compromised nginx worker cannot rewrite where the stream is being sent.
STATE_DIR="${STATE_DIR:-/run/croccante}"

# Writable by the nginx worker user, because the exec_publish hooks run as that
# user. Only the publisher marker and the hook log live here.
HOOK_DIR="$STATE_DIR/hooks"
PUBLISHER_FILE="$HOOK_DIR/publisher"

# nginx discards the stdout of exec_publish children, so hook scripts log to a
# file instead. entrypoint.sh tails it onto the container's stdout.
HOOK_LOG="$HOOK_DIR/hooks.log"

# Root-owned explicit lifecycle state. Publisher hooks may read this directory
# but only the authenticated control server writes it.
CONTROL_DIR="$STATE_DIR/control"
REQUESTED_STATE_FILE="$CONTROL_DIR/requested.state"
SESSION_FILE="$CONTROL_DIR/session.id"
# This is an observation from nginx, not an authority command, so it belongs in
# the nginx-writable hook directory alongside the live publisher marker.
PUBLISHER_SEEN_FILE="$HOOK_DIR/publisher.seen"

# Durable immutable versions prepared by the authenticated control service.
FILLER_STORE_DIR="${FILLER_STORE_DIR:-/var/lib/croccante/fillers}"
ACTIVE_FILLER_VERSION_FILE="$CONTROL_DIR/active-filler.version"

active_filler_file() {
    _version=$(cat "$ACTIVE_FILLER_VERSION_FILE" 2>/dev/null) || return 1
    _program_hash=$(printf '%s' "${PROGRAM_ID:?}" | sha256sum | cut -d ' ' -f1)
    _file="$FILLER_STORE_DIR/$_program_hash/$_version/filler.flv"
    [ -s "$_file" ] || return 1
    printf '%s\n' "$_file"
}

# Redact the key-bearing tail of a destination URL before it reaches a log.
# rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl  ->  rtmp://a.rtmp.youtube.com/live2/***
mask_dest() {
    printf '%s\n' "$1" | sed 's#/[^/]*$#/***#'
}

log() {
    if [ "${LOG_TO_HOOK_FILE:-0}" = "1" ]; then
        printf '[%s] %s\n' "${LOG_TAG:-croccante}" "$*" >> "$HOOK_LOG" 2>/dev/null
    else
        printf '[%s] %s\n' "${LOG_TAG:-croccante}" "$*"
    fi
}

# Is a pid recorded in $1 still running?
pid_alive() {
    _pf="$1"
    [ -f "$_pf" ] || return 1
    _p=$(cat "$_pf" 2>/dev/null) || return 1
    [ -n "$_p" ] || return 1
    kill -0 "$_p" 2>/dev/null
}

# Write a file atomically so no reader ever sees a half-written value.
atomic_write() {
    _dest="$1"
    _val="$2"
    printf '%s\n' "$_val" > "$_dest.tmp" && mv -f "$_dest.tmp" "$_dest"
}

session_started() {
    [ -f "$SESSION_FILE" ] && [ "$(cat "$REQUESTED_STATE_FILE" 2>/dev/null)" = "started" ]
}

# Number of publishers nginx itself believes are connected. This is the
# authority on whether a stream is arriving; our own state files only reflect
# whether the hooks managed to tell us about it.
nginx_publisher_count() {
    wget -q -O - "http://127.0.0.1:8080/stat" 2>/dev/null \
        | tr '>' '>\n' | grep -c '<publishing' || true
}
