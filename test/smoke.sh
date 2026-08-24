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
PROGRAM_ID=croccante-smoke-program
TEST_CONTROL_TOKEN=croccante-smoke-control-token
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
CONTROL_SECRET_FILE=$(mktemp)
printf '%s\n' "$TEST_CONTROL_TOKEN" > "$CONTROL_SECRET_FILE"

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
    docker rm -f croccante-under-test publisher sink-a sink-b sink-tls filler-source >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
finish() {
    cleanup
    rm -f "$CONTROL_SECRET_FILE"
}
trap finish EXIT

# ── Setup ────────────────────────────────────────────────────────────────────
bold "Building images"
PYTHONPATH="$ROOT" python3 -m unittest discover -s "$HERE" -p 'test_*.py' || {
    red "metrics unit tests failed"; exit 1;
}
docker build -q -t "$IMAGE" "$ROOT" >/dev/null || { red "croccante build failed"; exit 1; }
docker run --rm --entrypoint python3 -v "$ROOT:/workspace:ro" -w /workspace "$IMAGE" \
    test/test_filler_store.py -v || { red "filler preparation runtime tests failed"; exit 1; }
docker build -q -t "$SINK_IMAGE" "$HERE/sink" >/dev/null || { red "sink build failed"; exit 1; }

cleanup
docker network create "$NET" >/dev/null

start_sinks() {
    docker rm -f filler-source >/dev/null 2>&1
    docker run -d --name sink-a --network "$NET" "$SINK_IMAGE" >/dev/null
    docker run -d --name sink-b --network "$NET" "$SINK_IMAGE" >/dev/null
    docker run -d --name sink-tls --network "$NET" -e TLS=1 "$SINK_IMAGE" >/dev/null
    docker run -d --name filler-source --network "$NET" --entrypoint sh "$IMAGE" -c '
        mkdir -p /source
        ffmpeg -hide_banner -loglevel error -y -f lavfi -i color=c=blue:s=320x240 -frames:v 1 /source/filler.bmp
        cd /source && exec python3 -m http.server 8090
    ' >/dev/null
    sleep 3
    SOURCE_SHA=$(docker exec filler-source sha256sum /source/filler.bmp | cut -d ' ' -f1)
}

# A stream key shaped like a real one, so we can assert it never reaches a log.
FAKE_KEY="abcd-efgh-ijkl-mnop-qrst"

start_croccante() {
    docker run -d --name croccante-under-test --network "$NET" \
        -e PROGRAM_ID="$PROGRAM_ID" \
        -e CONTROL_TOKEN_FILE=/run/secrets/croccante-control-token \
        -v "$CONTROL_SECRET_FILE:/run/secrets/croccante-control-token:ro" \
        -e RELAY_DEST_1="rtmp://sink-a:1935/live/$FAKE_KEY" \
        -e RELAY_DEST_5="rtmp://sink-b:1935/live/$FAKE_KEY" \
        -e RELAY_DEST_9="rtmps://sink-tls:443/live/$FAKE_KEY" \
        -e RELAY_DEST_12="rtmp://192.0.2.1:1935/live/unreachable" \
        -e RELAY_BACKOFF_MAX=4 \
        "$@" "$IMAGE" >/dev/null
    sleep 2
    prepare_filler test-default test-prepare >/dev/null
}

control_request() { # method, action-or-state, token, key, sequence, program
    docker exec croccante-under-test python3 -c '
import sys, urllib.error, urllib.request
method, action, token, key, sequence, program = sys.argv[1:]
path = f"http://127.0.0.1:8081/v1/programs/{program}/session"
if action != "state": path += f"/{action}"
headers = {"Authorization": f"Bearer {token}"}
if key: headers["Idempotency-Key"] = key
if sequence: headers["X-Command-Sequence"] = sequence
request = urllib.request.Request(path, data=b"" if method == "POST" else None, headers=headers, method=method)
try:
    response = urllib.request.urlopen(request)
except urllib.error.HTTPError as error:
    print(error.code)
    print(error.read().decode())
else:
    print(response.status)
    print(response.read().decode())
' "$1" "$2" "${3:-$TEST_CONTROL_TOKEN}" "${4:-}" "${5:-}" "${6:-$PROGRAM_ID}" 2>/dev/null
}

control_command() { # start|stop, sequence, idempotency key
    if [ "$1" = start ]; then
        docker exec croccante-under-test python3 -c '
import sys, urllib.error, urllib.request
action, token, key, sequence, program = sys.argv[1:]
request = urllib.request.Request(f"http://127.0.0.1:8081/v1/programs/{program}/session/{action}", data=b"", headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key, "X-Command-Sequence": sequence, "X-Filler-Version": "test-default"}, method="POST")
try: response = urllib.request.urlopen(request)
except urllib.error.HTTPError as error: print(error.code); print(error.read().decode())
else: print(response.status); print(response.read().decode())
' "$1" "$TEST_CONTROL_TOKEN" "$3" "$2" "$PROGRAM_ID" 2>/dev/null
    else
        control_request POST "$1" "$TEST_CONTROL_TOKEN" "$3" "$2"
    fi
}

prepare_filler() { # version, idempotency key
    docker exec croccante-under-test python3 -c '
import json, sys, urllib.error, urllib.request
version, key, token, program, checksum = sys.argv[1:]
payload = {"commandId": key, "source": {"id": "smoke-image", "sha256": checksum, "downloadUrl": "http://filler-source:8090/filler.bmp?signature=redacted-test"}, "profile": {"width": 320, "height": 240, "fps": 15, "videoBitrate": "400k", "audioRate": 44100, "audioChannels": 2, "audioBitrate": "64k", "gop": 30, "loopSeconds": 6}}
body = json.dumps(payload).encode()
request = urllib.request.Request(f"http://127.0.0.1:8081/v1/programs/{program}/fillers/{version}", data=body, headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key, "Content-Type": "application/json"}, method="PUT")
try: response = urllib.request.urlopen(request)
except urllib.error.HTTPError as error: print(error.code); print(error.read().decode())
else: print(response.status); print(response.read().decode())
' "$1" "$2" "$TEST_CONTROL_TOKEN" "$PROGRAM_ID" "$SOURCE_SHA" 2>/dev/null
}

control_status() {
    control_request GET state "$TEST_CONTROL_TOKEN" "" ""
}

metrics_request() { # token
    docker exec croccante-under-test python3 -c '
import sys, urllib.error, urllib.request
token = sys.argv[1]
headers = {"Authorization": f"Bearer {token}"} if token else {}
request = urllib.request.Request("http://127.0.0.1:8081/metrics", headers=headers)
try:
    response = urllib.request.urlopen(request)
except urllib.error.HTTPError as error:
    print(error.code)
    print(error.read().decode())
else:
    print(response.status)
    print(response.read().decode())
' "${1:-}" 2>/dev/null
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

# Longest outbound disconnect a sink observed, in seconds. This is the number
# that decides whether a filler transition is survivable at a real destination.
max_outbound_gap() {
    docker exec "$1" sh -c 'cat /rec/events.log 2>/dev/null' 2>/dev/null | awk '
        $2 == "disconnect" { d = $1 }
        $2 == "connect" && d { g = $1 - d; if (g > m) m = g; d = 0 }
        END { printf "%.2f", m + 0 }'
}

wait_for_state() { # container-state, timeout
    for _ in $(seq 1 "${2:-40}"); do
        [ "$(dest_state 1)" = "$1" ] && return 0
        sleep 1
    done
    return 1
}

wait_for_control_mode() { # mode, timeout
    for _ in $(seq 1 "${2:-40}"); do
        control_status | tail -n 1 | grep -q "\"mode\":\"$1\"" && return 0
        sleep 1
    done
    return 1
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
out=$(docker run --rm --name croccante-nodest \
    -e PROGRAM_ID="$PROGRAM_ID" \
    -e CONTROL_TOKEN_FILE=/run/secrets/croccante-control-token \
    -v "$CONTROL_SECRET_FILE:/run/secrets/croccante-control-token:ro" \
    "$IMAGE" 2>&1); rc=$?
check "exits non-zero" "$([ $rc -ne 0 ] && echo 0 || echo 1)" "exit code was $rc"
echo "$out" | grep -q "no destinations configured"
check "explains why" $? "output was: $out"
echo

# ── Test 2: private explicit start ───────────────────────────────────────────
bold "Test 2 — publisher is withheld until authenticated explicit Start"
start_sinks
start_croccante

unauthorized=$(control_request GET state wrong-token "" "")
check "rejects an unauthenticated state request" "$(printf '%s\n' "$unauthorized" | head -n 1 | grep -q 401 && echo 0 || echo 1)"
wrong_program=$(control_request GET state "$TEST_CONTROL_TOKEN" "" "" wrong-program)
check "rejects a request scoped to another program" "$(printf '%s\n' "$wrong_program" | head -n 1 | grep -q 404 && echo 0 || echo 1)"

unauthorized_metrics=$(metrics_request wrong-token)
check "rejects an unauthenticated metrics scrape" "$(printf '%s\n' "$unauthorized_metrics" | head -n 1 | grep -q 401 && echo 0 || echo 1)"
authorized_metrics=$(metrics_request "$TEST_CONTROL_TOKEN")
check "serves authenticated Prometheus metrics" "$(printf '%s\n' "$authorized_metrics" | head -n 1 | grep -q 200 && echo 0 || echo 1)"
printf '%s\n' "$authorized_metrics" | tail -n +2 | PYTHONPATH="$ROOT" python3 -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("metrics_test_parser", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
samples = module.parse_exposition(sys.stdin.read())
required = {"croccante_build_info", "croccante_relay_slot_state", "croccante_process_resident_memory_bytes"}
assert required <= {name for name, _, _ in samples}
' "$HERE/test_metrics.py"
check "parses real container collector output" $?
check "metrics omit program, token, stream key, and destination URL" \
    "$(printf '%s' "$authorized_metrics" | grep -qE "$PROGRAM_ID|$TEST_CONTROL_TOKEN|$FAKE_KEY|rtmps?://" && echo 1 || echo 0)"

start_publisher
sleep 4
check "connected publisher remains idle while stopped" "$([ "$(dest_state 1)" = "idle" ] && echo 0 || echo 1)" "state was $(dest_state 1)"
before_start=$(recorded_bytes sink-a)
check "no destination bytes before Start (${before_start})" "$([ "${before_start:-0}" -eq 0 ] && echo 0 || echo 1)"

started=$(control_command start 1 initial-start)
check "authenticated Start is accepted" "$(printf '%s\n' "$started" | head -n 1 | grep -q 200 && echo 0 || echo 1)"
wait_for_state relaying 30
check "Start reaches live relay state" $? "state was $(dest_state 1)"
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
failure_metrics=$(metrics_request "$TEST_CONTROL_TOKEN")
printf '%s\n' "$failure_metrics" | tail -n +2 | PYTHONPATH="$ROOT" python3 -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("metrics_test_parser", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
samples = module.parse_exposition(sys.stdin.read())
values = {(name, tuple(sorted(labels.items()))): value for name, labels, value in samples}
assert values[("croccante_relay_retries_total", (("slot", "4"),))] >= 1
assert values[("croccante_relay_results_total", (("result", "failure"), ("slot", "4")))] >= 1
' "$HERE/test_metrics.py"
check "collector reports the isolated failure and retry" $?
echo

# ── Test 5: no key material in logs ──────────────────────────────────────────
bold "Test 5 — stream keys never reach the logs"
if echo "$logs" | grep -q "$FAKE_KEY"; then
    key_absent=1
else
    key_absent=0
fi
check "key absent from container logs" "$key_absent" "the fake key appeared in logs"
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

# Since filler landed, a publisher gap is covered rather than idled through:
# every destination keeps exactly one ffmpeg alive pushing the filler asset.
# Asserting zero here would be asserting the bug filler exists to fix.
#
# Note the ordering: capture the outcome BEFORE any command substitution runs,
# because `check "...$(dest_state 1)" $?` would clobber $? with the
# substitution's status and could never fail.
wait_for_state filler 40
transitioned=$?
state_now=$(dest_state 1)
check "supervisors switch to filler after disconnect (state=${state_now})" "$transitioned" "still ${state_now} after 40s"

filler_metrics=$(metrics_request "$TEST_CONTROL_TOKEN")
printf '%s\n' "$filler_metrics" | tail -n +2 | PYTHONPATH="$ROOT" python3 -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("metrics_test_parser", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
samples = module.parse_exposition(sys.stdin.read())
values = {(name, tuple(sorted(labels.items()))): value for name, labels, value in samples}
assert values[("croccante_filler_activations_total", (("slot", "1"),))] >= 1
assert values[("croccante_ingest_publisher_events_total", (("event", "connect"),))] >= 1
assert values[("croccante_ingest_publisher_events_total", (("event", "disconnect"),))] >= 1
' "$HERE/test_metrics.py"
check "collector reports publisher lifecycle and filler activation" $?

# Three destinations are reachable; the fourth points at TEST-NET-1 on purpose
# and sits in backoff with no process, so the healthy range is 3..4 depending on
# whether that one happens to be mid-attempt. Fewer than 3 means a reachable
# destination is starved; more than 4 means a leak.
REACHABLE=3
DEST_COUNT=4
procs=$(ffmpeg_procs)
check "every reachable destination keeps filling during the gap (${procs} procs)" \
    "$([ "${procs:-0}" -ge "$REACHABLE" ] && [ "${procs:-0}" -le "$DEST_COUNT" ] && echo 0 || echo 1)" \
    "expected ${REACHABLE}-${DEST_COUNT}, saw ${procs} — a starved destination or a leak"

states=$(docker exec croccante-under-test sh -c 'for f in /run/croccante/dest-*.state; do printf "%s " "$(cat "$f")"; done' 2>/dev/null)
check "no reachable destination fell back to idle (${states})" \
    "$(printf '%s' "$states" | grep -q idle && echo 1 || echo 0)" \
    "a supervisor idled during an open session"

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
restarted=$(control_command start 1 restart-start)
check "restart returns to stopped until a new Start" "$(printf '%s\n' "$restarted" | head -n 1 | grep -q 200 && echo 0 || echo 1)"
wait_for_state relaying 30
sleep 12
final=$(recorded_bytes sink-a)
check "relays recovered after restart (+$((final-after)) bytes)" "$([ "$final" -gt "$after" ] && echo 0 || echo 1)" "no new bytes after restart"
docker exec croccante-under-test /usr/local/bin/healthcheck.sh >/dev/null 2>&1
check "healthy after restart" $?
echo

# ── Test 9: filler covers a publisher gap ────────────────────────────────────
bold "Test 9 — filler covers a publisher gap without starving destinations"

# Fresh containers so the event log starts clean.
docker rm -f croccante-under-test publisher sink-a sink-b sink-tls >/dev/null 2>&1
start_sinks
start_croccante

check "no filler before the first publish (state=$(dest_state 1))" "$([ "$(dest_state 1)" = "idle" ] && echo 0 || echo 1)" "filler must not run before a session opens"

waiting=$(control_command start 1 filler-start)
check "Start without a publisher is accepted" "$(printf '%s\n' "$waiting" | head -n 1 | grep -q 200 && echo 0 || echo 1)"
wait_for_state waiting 10
check "Start without a publisher waits without opening destinations" $? "state was $(dest_state 1)"
wait_for_control_mode waiting-for-publisher 10
check "control state reports waiting-for-publisher" $?

start_publisher
wait_for_state relaying 30
sleep 8
check "relaying once the publisher connects" "$([ "$(dest_state 1)" = "relaying" ] && echo 0 || echo 1)" "state was $(dest_state 1)"

# Publisher vanishes.
docker rm -f publisher >/dev/null 2>&1
wait_for_state filler 40
check "switches to filler when the publisher drops" $? "state was $(dest_state 1) after 40s"

before_fill=$(recorded_bytes sink-a)
sleep 12
after_fill=$(recorded_bytes sink-a)
check "destination keeps receiving during the gap (+$((after_fill-before_fill)) bytes)" \
    "$([ "$after_fill" -gt "$before_fill" ] && echo 0 || echo 1)" "destination starved while in filler"

docker exec croccante-under-test /usr/local/bin/healthcheck.sh >/dev/null 2>&1
check "healthy while in filler" $?

# Publisher returns.
start_publisher
wait_for_state relaying 30
check "returns to live relay when the publisher comes back" $? "state was $(dest_state 1) after 30s"
echo

# ── Test 10: transitions are cheap and repeatable ───────────────────────────
bold "Test 10 — repeated transitions stay clean"
for _ in 1 2; do
    docker rm -f publisher >/dev/null 2>&1
    wait_for_state filler 40 || true
    start_publisher
    wait_for_state relaying 30 || true
done

check "still relaying after repeated transitions (state=$(dest_state 1))" "$([ "$(dest_state 1)" = "relaying" ] && echo 0 || echo 1)"

stale=$(stale_pidfiles)
procs=$(ffmpeg_procs)
check "no stale pid files after repeated transitions (${stale})" "$([ "${stale:-0}" -le 4 ] && echo 0 || echo 1)"
check "ffmpeg process count is bounded (${procs})" "$([ "${procs:-0}" -le 4 ] && echo 0 || echo 1)" "${procs} processes suggests a leak"

# The measurement that justifies the simple design over a FIFO re-architecture.
gap=$(max_outbound_gap sink-a)
bold "      longest outbound disconnect observed: ${gap}s"
check "outbound gap stays under 5s (${gap}s)" \
    "$(awk -v g="$gap" 'BEGIN { exit !(g < 5) }' && echo 0 || echo 1)" \
    "a gap this long risks the platform ending the broadcast"
echo

# ── Test 11: explicit stop, ordering, and fresh restart ──────────────────────
bold "Test 11 — explicit Stop is idempotent, ordered, and reversible"
state_before_stop=$(control_status | tail -n 1)
session_before_stop=$(printf '%s' "$state_before_stop" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sessionId"])')
check "state omits destination URLs and stream keys" \
    "$(printf '%s' "$state_before_stop" | grep -qE 'rtmps?://|abcd-efgh' && echo 1 || echo 0)"

stopped=$(control_command stop 2 explicit-stop)
check "authenticated Stop is accepted" "$(printf '%s\n' "$stopped" | head -n 1 | grep -q 200 && echo 0 || echo 1)"
wait_for_idle 10
check "Stop reaches idle without a container restart" $? "state was $(dest_state 1)"
procs=$(ffmpeg_procs)
check "Stop terminates every destination publisher (${procs} procs)" "$([ "${procs:-0}" -eq 0 ] && echo 0 || echo 1)"
wait_for_control_mode idle 10
check "control state reports deliberate idle" $?

duplicate=$(control_request POST stop "$TEST_CONTROL_TOKEN" explicit-stop 2)
check "duplicate Stop returns the recorded idempotent result" \
    "$(printf '%s' "$duplicate" | tail -n 1 | grep -q '"duplicate":true' && echo 0 || echo 1)"

reordered=$(control_request POST start "$TEST_CONTROL_TOKEN" reordered-start 1)
check "reordered Start is rejected" "$(printf '%s\n' "$reordered" | head -n 1 | grep -q 409 && echo 0 || echo 1)"
check "reordered command cannot reopen destinations" "$([ "$(dest_state 1)" = "idle" ] && echo 0 || echo 1)"

started_again=$(control_command start 3 fresh-start)
check "newer Start is accepted" "$(printf '%s\n' "$started_again" | head -n 1 | grep -q 200 && echo 0 || echo 1)"
wait_for_state relaying 30
check "Stop followed by Start returns to live" $? "state was $(dest_state 1)"
state_after_start=$(control_status | tail -n 1)
session_after_start=$(printf '%s' "$state_after_start" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sessionId"])')
check "fresh Start creates a new session identifier" "$([ "$session_before_stop" != "$session_after_start" ] && echo 0 || echo 1)"
duplicate_start=$(control_command start 3 fresh-start)
check "duplicate Start returns the recorded idempotent result" \
    "$(printf '%s' "$duplicate_start" | tail -n 1 | grep -q '"duplicate":true' && echo 0 || echo 1)"
procs=$(ffmpeg_procs)
check "duplicate Start creates no parallel publishers (${procs} procs)" "$([ "${procs:-0}" -le 4 ] && echo 0 || echo 1)"
echo

# ── Test 12: the healthcheck can actually fail ───────────────────────────────
# A healthcheck that only ever returns "healthy" is worthless. Kill a
# supervisor and prove the check notices.
bold "Test 12 — healthcheck detects a dead supervisor"
wpid=$(docker exec croccante-under-test cat /run/croccante/dest-1.wrapper.pid | tr -d ' \n')
docker exec croccante-under-test kill -9 "$wpid" >/dev/null 2>&1
sleep 2
if docker exec croccante-under-test /usr/local/bin/healthcheck.sh >/dev/null 2>&1; then
    dead_supervisor_detected=1
else
    dead_supervisor_detected=0
fi
check "reports unhealthy when a supervisor dies" "$dead_supervisor_detected" "healthcheck still reported healthy"
echo

# ── Test 13: control-plane loss does not infer Stop ──────────────────────────
bold "Test 13 — control-plane loss leaves an active broadcast running"
docker restart croccante-under-test >/dev/null
sleep 5
docker rm -f publisher >/dev/null 2>&1
start_publisher
control_command start 1 outage-start >/dev/null
wait_for_state relaying 30
before_outage=$(recorded_bytes sink-a)
control_pid=$(docker exec croccante-under-test cat /run/croccante/control.pid | tr -d ' \n')
docker exec croccante-under-test kill -9 "$control_pid" >/dev/null 2>&1
sleep 6
after_outage=$(recorded_bytes sink-a)
check "broadcast advances after the controller dies (+$((after_outage-before_outage)) bytes)" \
    "$([ "$after_outage" -gt "$before_outage" ] && echo 0 || echo 1)"
if docker exec croccante-under-test /usr/local/bin/healthcheck.sh >/dev/null 2>&1; then
    dead_controller_detected=1
else
    dead_controller_detected=0
fi
check "health reports the unavailable controller" "$dead_controller_detected"
echo

# ── Summary ──────────────────────────────────────────────────────────────────
bold "───────────────────────────────────────"
if [ "$FAIL" -eq 0 ]; then
    green "All $PASS checks passed."
    exit 0
else
    red "$FAIL of $((PASS+FAIL)) checks failed:"
    for n in "${FAILED_NAMES[@]}"; do red "  - $n"; done
    exit 1
fi
