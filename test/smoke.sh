#!/usr/bin/env bash
# croccante smoke harness.
#
#   ./test/smoke.sh
#
# Brings up local RTMP and RTMPS sinks, runs croccante against them, and
# asserts on real relayed bytes. Needs Docker. Touches no real platform
# account and no real stream key.
set -uo pipefail

NET=croccante-smoke
IMAGE=${IMAGE:-croccante:dev}
SINK_IMAGE=croccante-sink:dev
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"

PASS=0
FAIL=0
FAILED_NAMES=()

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

ok()   { green "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { red   "  FAIL  $1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; FAIL=$((FAIL+1)); FAILED_NAMES+=("$1"); }

check() { # name, condition-result, detail
    if [ "$2" -eq 0 ]; then ok "$1"; else bad "$1" "${3:-}"; fi
}

cleanup() {
    docker rm -f croccante-under-test publisher sink-a sink-b sink-tls >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

# ── Setup ────────────────────────────────────────────────────────────────────
bold "Building images"
docker build -q -t "$IMAGE" "$ROOT" >/dev/null || { red "croccante build failed"; exit 1; }
docker build -q -t "$SINK_IMAGE" "$HERE/sink" >/dev/null || { red "sink build failed"; exit 1; }

cleanup
docker network create "$NET" >/dev/null

start_sinks() {
    docker run -d --name sink-a --network "$NET" "$SINK_IMAGE" >/dev/null
    docker run -d --name sink-b --network "$NET" "$SINK_IMAGE" >/dev/null
    docker run -d --name sink-tls --network "$NET" -e TLS=1 "$SINK_IMAGE" >/dev/null
    sleep 3
}

# A stream key shaped like a real one, so we can assert it never reaches a log.
FAKE_KEY="abcd-efgh-ijkl-mnop-qrst"

start_croccante() {
    docker run -d --name croccante-under-test --network "$NET" \
        -e RELAY_DEST_1="rtmp://sink-a:1935/live/$FAKE_KEY" \
        -e RELAY_DEST_5="rtmp://sink-b:1935/live/$FAKE_KEY" \
        -e RELAY_DEST_9="rtmps://sink-tls:443/live/$FAKE_KEY" \
        -e RELAY_DEST_12="rtmp://192.0.2.1:1935/live/unreachable" \
        -e RELAY_BACKOFF_MAX=4 \
        "$@" "$IMAGE" >/dev/null
    sleep 2
}

# Publishes a synthetic stream into croccante. Runs from the croccante image
# because that is what has ffmpeg. Fails loudly: a publisher that silently
# fails to start makes every downstream assertion lie.
start_publisher() {
    docker rm -f publisher >/dev/null 2>&1
    if ! docker run -d --name publisher --network "$NET" --entrypoint ffmpeg "$IMAGE" \
        -hide_banner -loglevel error \
        -re -f lavfi -i "testsrc=s=320x240:r=15" \
        -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=44100" \
        -c:v libx264 -preset ultrafast -tune zerolatency -g 30 -b:v 400k \
        -c:a aac -b:a 64k \
        -f flv "rtmp://croccante-under-test:1935/live/test" >/dev/null 2>&1
    then
        red "publisher container failed to start"; exit 1
    fi
    sleep 3
    if [ "$(docker inspect -f '{{.State.Running}}' publisher 2>/dev/null)" != "true" ]; then
        red "publisher exited immediately:"
        docker logs publisher 2>&1 | sed 's/^/    /'
        exit 1
    fi
}

recorded_bytes() { # sink container -> total bytes recorded
    docker exec "$1" sh -c 'cat /rec/* 2>/dev/null | wc -c' 2>/dev/null | tr -d ' ' || echo 0
}

# busybox has no `pgrep -c`, and `pgrep ... || echo 0` silently reports zero on
# an unrecognised option — which made this assertion pass no matter what.
ffmpeg_procs() {
    docker exec croccante-under-test sh -c "ps -o args | grep -c '[f]fmpeg' || true" 2>/dev/null | tr -d ' \n'
}

dest_state() {
    docker exec croccante-under-test cat "/run/croccante/dest-$1.state" 2>/dev/null | tr -d ' \n'
}

stale_pidfiles() {
    docker exec croccante-under-test sh -c 'ls /run/croccante/*.ffmpeg.pid 2>/dev/null | wc -l' 2>/dev/null | tr -d ' \n' || echo 0
}

# Teardown is bounded by nginx drop_idle_publisher (10s) plus the supervisor
# watchdog poll, so poll rather than guessing a sleep.
wait_for_idle() {
    for _ in $(seq 1 "${1:-40}"); do
        [ "$(dest_state 1)" = "idle" ] && return 0
        sleep 1
    done
    return 1
}

echo

# ── Test 1: refuses to start with no destinations ────────────────────────────
bold "Test 1 — refuses to start with no destinations configured"
out=$(docker run --rm --name croccante-nodest "$IMAGE" 2>&1); rc=$?
check "exits non-zero" "$([ $rc -ne 0 ] && echo 0 || echo 1)" "exit code was $rc"
echo "$out" | grep -q "no destinations configured"
check "explains why" $? "output was: $out"
echo

# ── Test 2: fan-out to multiple destinations ─────────────────────────────────
bold "Test 2 — fan-out to multiple simultaneous destinations"
start_sinks
start_croccante
start_publisher
sleep 12

a=$(recorded_bytes sink-a); b=$(recorded_bytes sink-b); t=$(recorded_bytes sink-tls)
check "sink-a received data (${a} bytes)"   "$([ "${a:-0}" -gt 20000 ] && echo 0 || echo 1)" "only ${a} bytes"
check "sink-b received data (${b} bytes)"   "$([ "${b:-0}" -gt 20000 ] && echo 0 || echo 1)" "only ${b} bytes"

# ── Test 3: native RTMPS output (the stunnel-removal evidence) ───────────────
bold "Test 3 — native rtmps:// output works without a stunnel sidecar"
check "rtmps sink received data (${t} bytes)" "$([ "${t:-0}" -gt 20000 ] && echo 0 || echo 1)" "only ${t} bytes"
echo

# ── Test 4: a failing destination does not disturb the others ────────────────
bold "Test 4 — an unreachable destination is isolated"
logs=$(docker logs croccante-under-test 2>&1)
echo "$logs" | grep -qE "dest-4.*(retrying|failed)"
check "unreachable destination reports failure and retries" $?
echo "$logs" | grep -qE "retrying in ([0-9]+)s"
check "retry uses a backoff delay, not a tight loop" $?
check "healthy destinations kept relaying regardless" "$([ "${a:-0}" -gt 20000 ] && [ "${b:-0}" -gt 20000 ] && echo 0 || echo 1)"
echo

# ── Test 5: no key material in logs ──────────────────────────────────────────
bold "Test 5 — stream keys never reach the logs"
echo "$logs" | grep -q "$FAKE_KEY"
check "key absent from container logs" "$([ $? -ne 0 ] && echo 0 || echo 1)" "the fake key appeared in logs"
echo "$logs" | grep -q '/\*\*\*'
check "destinations are logged masked" $?
echo

# ── Test 6: healthcheck reflects relay state ─────────────────────────────────
bold "Test 6 — healthcheck reflects relay liveness"
docker exec croccante-under-test /usr/local/bin/healthcheck.sh >/dev/null 2>&1
check "reports healthy while relaying" $?
echo

# ── Test 7: publisher disconnect and reconnect ───────────────────────────────
bold "Test 7 — publisher disconnect and reconnect"
docker rm -f publisher >/dev/null 2>&1

wait_for_idle 40
check "supervisors returned to idle after disconnect (state=$(dest_state 1))" $? "still $(dest_state 1) after 40s"

orphans=$(ffmpeg_procs)
check "no orphaned ffmpeg processes after disconnect (${orphans})" "$([ "${orphans:-0}" -eq 0 ] && echo 0 || echo 1)" "${orphans} still running"

stale=$(stale_pidfiles)
check "no stale ffmpeg pid files (${stale})" "$([ "${stale:-0}" -eq 0 ] && echo 0 || echo 1)"

before=$(recorded_bytes sink-a)
start_publisher
sleep 12
after=$(recorded_bytes sink-a)
check "relay resumed on reconnect (+$((after-before)) bytes)" "$([ "$after" -gt "$before" ] && echo 0 || echo 1)" "no new bytes after reconnect"
echo

# ── Test 8: container restart with a publisher already connected ─────────────
bold "Test 8 — container restart while a publisher is connected"
docker restart croccante-under-test >/dev/null
sleep 5
docker rm -f publisher >/dev/null 2>&1
start_publisher
sleep 12
final=$(recorded_bytes sink-a)
check "relays recovered after restart (+$((final-after)) bytes)" "$([ "$final" -gt "$after" ] && echo 0 || echo 1)" "no new bytes after restart"
docker exec croccante-under-test /usr/local/bin/healthcheck.sh >/dev/null 2>&1
check "healthy after restart" $?
echo

# ── Test 9: the healthcheck can actually fail ────────────────────────────────
# A healthcheck that only ever returns "healthy" is worthless. Kill a
# supervisor and prove the check notices.
bold "Test 9 — healthcheck detects a dead supervisor"
wpid=$(docker exec croccante-under-test cat /run/croccante/dest-1.wrapper.pid | tr -d ' \n')
docker exec croccante-under-test kill -9 "$wpid" >/dev/null 2>&1
sleep 2
docker exec croccante-under-test /usr/local/bin/healthcheck.sh >/dev/null 2>&1
check "reports unhealthy when a supervisor dies" "$([ $? -ne 0 ] && echo 0 || echo 1)" "healthcheck still reported healthy"
echo

# ── Summary ──────────────────────────────────────────────────────────────────
bold "───────────────────────────────────────"
if [ "$FAIL" -eq 0 ]; then
    green "All $PASS checks passed."
else
    red "$FAIL of $((PASS+FAIL)) checks failed:"
    for n in "${FAILED_NAMES[@]}"; do red "  - $n"; done
fi
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
