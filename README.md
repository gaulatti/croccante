# croccante

Minimal nginx-rtmp relay container. Receives one RTMP stream from OBS on port
1935 and simultaneously relays it, without transcoding, to any number of
destinations.

```
                                   ┌─► rtmp://…   (supervisor 1 ─ ffmpeg -c copy)
OBS ──► [host:1935] ──► nginx-rtmp ├─► rtmps://…  (supervisor 2 ─ ffmpeg -c copy)
                                   └─► rtmp://…   (supervisor 3 ─ ffmpeg -c copy)
```

Each destination gets its own long-lived supervisor process that retries
forever with backoff, so one dead or rejecting destination never affects the
others, and a network blip resolves itself without restarting anything.

## Configuring destinations

Destinations are a flat list of **full URLs** — `RELAY_DEST_1` … `RELAY_DEST_20`.
There is no per-platform configuration: anything ffmpeg can write FLV to works.

```bash
RELAY_DEST_1=rtmp://a.rtmp.youtube.com/live2/<key>
RELAY_DEST_2=rtmp://10.0.0.5:1935/live/mystream
RELAY_DEST_3=rtmps://live-api-s.facebook.com:443/rtmp/<key>
```

Empty and absent slots are skipped, and slot numbers need not be contiguous.
The container refuses to start if no destination is configured.

RTMPS is handled natively by ffmpeg — there is no stunnel sidecar. See
[docs/architecture.md](docs/architecture.md) for why.

See [.env.example](.env.example) for the full set of tuning variables.

## OBS settings

| Field      | Value                          |
|------------|--------------------------------|
| Service    | Custom                         |
| Server     | `rtmp://<server-ip>:1935/live` |
| Stream key | anything (e.g. `stream`)       |

## Running it

Server setup, deployment, and key rotation live in
[docs/operations.md](docs/operations.md).

Locally:

```bash
docker build -t croccante:dev .
docker run --rm -p 1935:1935 --env-file .env croccante:dev
```

## Tests

```bash
./test/smoke.sh
```

Brings up local RTMP and RTMPS sinks, runs the relay against them, and asserts
on actual relayed bytes. Needs Docker. Touches no real platform account and no
real stream key. Also runs in CI on every push.

## What this does not do

Relaying stops when the publisher disconnects. If OBS drops mid-broadcast — a
WiFi gap, a captive portal — the outbound legs drop with it and the platform
may end the broadcast. Surviving that gap is
[G-178](https://linear.app/gaulatti/issue/G-178), not this.

## File map

```
croccante/
├── Dockerfile           # Alpine + nginx-mod-rtmp + ffmpeg
├── nginx.conf           # RTMP ingest + loopback stat endpoint (static, not templated)
├── entrypoint.sh        # Resolves destinations, starts supervisors, execs nginx
├── relay-dest.sh        # One supervisor per destination: relay, retry, back off
├── relay-start.sh       # nginx exec_publish hook   — records the live stream name
├── relay-stop.sh        # nginx exec_publish_done   — clears it
├── relay-lib.sh         # Shared helpers (masking, atomic writes, pid checks)
├── healthcheck.sh       # Relay-aware container healthcheck
├── .env.example         # Destination template — copy to .env, never commit
├── docs/                # Architecture and operations (destined for the wiki)
├── test/smoke.sh        # End-to-end harness against local sinks
└── .github/workflows/deploy.yml
```
