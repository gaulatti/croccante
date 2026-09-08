# croccante

Minimal nginx-rtmp relay container. Receives one RTMP program stream on port
1935 and, only after an authenticated Start command, relays it without
transcoding to any number of destinations.

```
                                   ┌─► rtmp://…   (supervisor 1 ─ ffmpeg -c copy)
Alana ─► [host:1935] ─► nginx-rtmp ├─► rtmps://…  (supervisor 2 ─ ffmpeg -c copy)
                                   └─► rtmp://…   (supervisor 3 ─ ffmpeg -c copy)
```

Each destination gets its own long-lived supervisor process that retries
forever with backoff, so one dead or rejecting destination never affects the
others, and a network blip resolves itself without restarting anything.

## Configuring destinations

Croccante accepts no destination URLs or stream keys in environment variables.
At authenticated Start, Alana supplies an exact configuration version plus one
to twenty opaque IDs and versioned AWS Secrets Manager references:

```json
{
  "version": "destinations-2026-08-25.1",
  "destinations": [
    {"id": "primary", "secretId": "broadcast/example/primary", "versionId": "00000000-0000-0000-0000-000000000000"}
  ]
}
```

Each referenced secret is a strict JSON object containing only `scheme`,
`host`, optional `port`, `application`, and `streamKey`. Croccante resolves
every exact version before opening any output and binds the resulting selection
immutably to that session. A secret rotation takes effect only through an
explicit Stop, validation/reload, and new Start.

## Explicit broadcast lifecycle

One Croccante runtime serves one required `PROGRAM_ID`. Its control API listens
on port 8081 and must stay on the private `broadcast-control` Docker network.
It requires a bearer token mounted at `CONTROL_TOKEN_FILE`; the token is never
accepted on the command line or returned by the API.

An RTMP publisher may connect while Croccante is stopped, but public
destinations remain disconnected until Alana sends an authenticated Start.
Start and Stop require both an `Idempotency-Key` and a monotonically increasing
`X-Command-Sequence`. Stop ends every public output without restarting the
container. Publisher loss during a started session invokes filler and never
implies Stop.

Before Start, Alana prepares an immutable program-scoped filler version through
the same authenticated private API. Croccante downloads the signed source,
verifies its checksum, transcodes it once to the supplied H.264/AAC live profile,
and stores it on the durable filler volume. Start names the prepared version with
`X-Filler-Version`; unprepared or mismatched versions are refused, and an active
session never switches versions.

The machine contract and example requests are documented in
[docs/operations.md](docs/operations.md#lifecycle-control-api).

The versioned, credential-free contract for future as-aired recording manifests
and bounded operator reports is documented in
[docs/recording-contract.md](docs/recording-contract.md). This defines the
fixture-backed boundary only; runtime capture and production wiring remain in
[issue #17](https://github.com/gaulatti/croccante/issues/17).

The same private listener exposes authenticated Prometheus metrics at
`GET /metrics`. It reports container process, publisher, relay, retry,
backoff, filler, and control-request behavior using bounded labels only. The
endpoint never emits program names, destination URLs, stream keys, or
credentials. See [docs/operations.md](docs/operations.md#prometheus-metrics).

RTMPS is handled natively by ffmpeg. Resolved URLs are injected at its I/O
boundary from root-only runtime state, so keys never appear in process
arguments. See [docs/architecture.md](docs/architecture.md).

## Publisher settings

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
docker run --rm -p 1935:1935 \
  -e PROGRAM_ID=example-program \
  -e AWS_REGION=us-east-1 \
  --mount type=bind,src="$PWD/control-token",dst=/run/secrets/croccante-control-token,readonly \
  --mount type=bind,src="$PWD/aws-credentials",dst=/run/secrets/aws-credentials,readonly \
  -e AWS_SHARED_CREDENTIALS_FILE=/run/secrets/aws-credentials \
  croccante:dev
```

## Tests

```bash
python3 test/test_filler_store.py -v
python3 test/test_recording_manifest.py -v
./test/smoke.sh
```

The first command performs real local ffmpeg/ffprobe preparation for image,
silent video, and WebM-with-audio sources plus API/state recovery tests. The
second validates the versioned recording schema, synthetic media fixtures,
lifecycle invariants, checksums, and bounded redacted reports. The
smoke harness brings up local RTMP and RTMPS sinks, runs the relay, and asserts
on actual relayed bytes plus authenticated lifecycle and metrics behavior.
Needs Docker.
Touches no real platform account and no real stream key. Also runs in CI on
every push.

## File map

```
croccante/
├── Dockerfile           # Alpine + nginx-mod-rtmp + ffmpeg
├── nginx.conf           # RTMP ingest + loopback stat endpoint (static, not templated)
├── destination_store.py # Strict selection parsing and exact secret resolution
├── destination_runtime.py # Atomic supervisor ownership for one session
├── destination_url_shim.c # Keeps resolved output URLs out of ffmpeg argv
├── entrypoint.sh        # Initializes stopped state and execs nginx
├── relay-dest.sh        # One supervisor per destination: relay, retry, back off
├── relay-start.sh       # nginx exec_publish hook   — records the live stream name
├── relay-stop.sh        # nginx exec_publish_done   — clears it
├── relay-lib.sh         # Shared helpers (atomic writes, pid checks, counters)
├── healthcheck.sh       # Relay-aware container healthcheck
├── control-server.py    # Authenticated, program-scoped Start/Stop/state API
├── relay_metrics.py     # Bounded Prometheus collector for private scraping
├── recording_manifest.py # Versioned recording manifest validation and reports
├── contracts/           # Closed machine-readable recording contract schemas
├── filler_store.py      # Durable immutable source preparation and validation
├── docs/                # Architecture and operations (destined for the wiki)
├── test/smoke.sh        # End-to-end harness against local sinks
└── .github/workflows/deploy.yml
```
