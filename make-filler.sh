#!/bin/sh
# Produce the filler asset exactly once, at container start, before any
# destination supervisor runs.
#
# Filler must be byte-compatible with the live stream as far as each
# destination is concerned: identical codec, resolution, framerate, audio
# sample rate and channel layout. A mid-broadcast parameter change is one of
# the things platforms drop a stream for, so the profile is configuration and
# the asset is generated from it rather than hardcoded.
#
# Encoded once here, then fanned out to every destination with -c copy. No
# encoder ever runs per destination.
set -eu

. /usr/local/bin/relay-lib.sh
LOG_TAG=filler

WIDTH="${BROADCAST_WIDTH:-1280}"
HEIGHT="${BROADCAST_HEIGHT:-720}"
FPS="${BROADCAST_FPS:-30}"
VIDEO_BITRATE="${BROADCAST_VIDEO_BITRATE:-2500k}"
AUDIO_RATE="${BROADCAST_AUDIO_RATE:-44100}"
AUDIO_CHANNELS="${BROADCAST_AUDIO_CHANNELS:-2}"
AUDIO_BITRATE="${BROADCAST_AUDIO_BITRATE:-128k}"
LOOP_SECONDS="${FILLER_LOOP_SECONDS:-10}"

# A keyframe every second bounds how long a destination waits for a decodable
# picture when the loop wraps or a relay reconnects.
GOP=$FPS

if [ -n "${FILLER_ASSET:-}" ]; then
    # An operator-supplied asset replaces the generated one wholesale. It is
    # their responsibility to match the broadcast profile.
    if [ ! -f "$FILLER_ASSET" ]; then
        log "ERROR: FILLER_ASSET is set but '$FILLER_ASSET' does not exist."
        exit 1
    fi
    log "using operator-supplied filler asset: $FILLER_ASSET"
    cp "$FILLER_ASSET" "$FILLER_FILE"
    exit 0
fi

log "generating ${LOOP_SECONDS}s filler at ${WIDTH}x${HEIGHT}@${FPS} ${VIDEO_BITRATE}, ${AUDIO_RATE}Hz x${AUDIO_CHANNELS}"

ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "color=c=black:s=${WIDTH}x${HEIGHT}:r=${FPS}" \
    -f lavfi -i "anullsrc=sample_rate=${AUDIO_RATE}:channel_layout=stereo" \
    -t "$LOOP_SECONDS" \
    -c:v libx264 -preset veryfast -profile:v main -pix_fmt yuv420p \
    -b:v "$VIDEO_BITRATE" -minrate "$VIDEO_BITRATE" -maxrate "$VIDEO_BITRATE" \
    -bufsize "$VIDEO_BITRATE" -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 \
    -c:a aac -b:a "$AUDIO_BITRATE" -ar "$AUDIO_RATE" -ac "$AUDIO_CHANNELS" \
    -f flv "$FILLER_FILE"

log "filler asset ready: $(du -h "$FILLER_FILE" | cut -f1)"
