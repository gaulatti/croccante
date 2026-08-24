# croccante — operations

## First-time server setup

Fedora 42 with Docker. The existing nginx edge proxy handles external TLS;
croccante only needs 1935, bound to loopback.

```bash
# 1. Deployment directory
sudo mkdir -p /opt/croccante
sudo chown $USER /opt/croccante
mkdir -p /opt/croccante/fillers

# 2. Destination configuration. This file is the ONLY copy of your stream keys.
cp .env.example /opt/croccante/.env
chmod 600 /opt/croccante/.env
$EDITOR /opt/croccante/.env

# Independent machine credential used only by Alana. Transfer it through the
# approved secret channel; do not paste it into shell history or .env.
install -m 600 /secure/input/croccante-control-token /opt/croccante/control-token
```

No manual `docker login` is needed on the server. The deploy workflow logs in
with its own short-lived token and logs out again when the step ends.

### GitHub secrets

| Secret                   | Description                                 |
|--------------------------|---------------------------------------------|
| `SERVER_HOST`            | Server IP or hostname                       |
| `SERVER_USER`            | SSH user                                    |
| `SERVER_SSH_KEY`         | Private SSH key (no passphrase)             |
| `SERVER_SSH_FINGERPRINT` | Server host key fingerprint                 |

```bash
ssh-keyscan -t ed25519 <server-ip>
```

There is deliberately **no `GHCR_PAT`**. The deploy authenticates to GHCR using
the workflow run's own `GITHUB_TOKEN`, so no long-lived registry credential is
ever written to the server.

### Production environment gate

The deploy job declares `environment: production`. Configure that environment in
the repository settings with a required reviewer if you want deploys to pause
for approval. Deploying interrupts a live broadcast for the duration of the
container swap.

## Deploying

Push to `main`, or run the workflow manually. The pipeline is:

```
smoke test → build → push to GHCR → SSH → pull → swap → verify healthy
```

The image is pulled **before** the running container is stopped, so the download
does not count against the interruption. The only downtime is the
stop/rm/run swap, which the workflow measures and reports as a GitHub notice:

```
Stream interruption window: NNN ms
```

The deploy fails if the new container does not reach `healthy`.

> **Open item:** the interruption window has not yet been measured against the
> real server, because croccante has never been deployed. Record the figure from
> the first real deploy here.

## Updating stream keys

Keys live only in `/opt/croccante/.env`. No image rebuild is needed.

```bash
ssh user@server
$EDITOR /opt/croccante/.env
docker restart croccante
```

A restart interrupts a live broadcast. Rotate between streams.

Moving keys off this file and into a secrets manager is
[G-180](https://linear.app/gaulatti/issue/G-180).

## Lifecycle control API

The API is reachable as `http://croccante:8081` only from containers attached
to the private `broadcast-control` network. Do not publish this port publicly.
Alana supplies the mounted bearer token and the exact configured program ID.

| Method and path | Purpose |
|-----------------|---------|
| `GET /v1/programs/{programId}/session` | Authoritative requested/actual state, session timestamps, publisher presence, mode, destination health, and last command result. |
| `POST /v1/programs/{programId}/session/start` | Open or idempotently retain the session. |
| `POST /v1/programs/{programId}/session/stop` | End all public destination sessions and return to idle. |
| `PUT /v1/programs/{programId}/fillers/{version}` | Idempotently download, checksum, transcode, validate, and atomically prepare an immutable version. |
| `GET /v1/programs/{programId}/fillers/{version}` | Revalidate and report one prepared version after restart or retry. |
| `GET /metrics` | Authenticated private preparation counters and version inventory. |

Both POST routes require `Idempotency-Key` and a positive,
monotonically increasing `X-Command-Sequence`. Reusing a key returns its stored
result without applying the command again. A sequence older than or equal to a
different accepted command returns `409`, so a delayed Start cannot undo a
newer Stop.

Start additionally requires `X-Filler-Version`; the named version must already
be ready for this program. Preparation requires an `Idempotency-Key` equal to
the payload's bounded `commandId`:

```json
{
  "commandId": "alana-config-123",
  "source": {
    "id": "asset-456",
    "sha256": "64-lowercase-hex-characters",
    "downloadUrl": "https://signed-download.example/object"
  },
  "profile": {
    "width": 1920, "height": 1080, "fps": 30,
    "videoBitrate": "6000k", "audioRate": 48000,
    "audioChannels": 2, "audioBitrate": "160k",
    "gop": 60, "loopSeconds": 10
  }
}
```

Responses expose only bounded readiness/failure, source identity/checksum,
artifact checksum, and profile. They never echo or persist the signed URL.
Repeating the same version/request is safe; different content for an existing
version returns `409`. Failed work leaves ready and active versions untouched.
The authenticated `/metrics` surface uses only the bounded `outcome` label and
reports preparation counts, ready-version inventory, and whether a session has
a bound version. Version, program, source, command, URL, and credential values
never become labels.

Example from an authorized container on the private network (read the token
into the request without printing it):

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $(< /run/secrets/croccante-control-token)" \
  -H 'Idempotency-Key: alana-command-123' \
  -H 'X-Command-Sequence: 123' \
  -H 'X-Filler-Version: filler-v7' \
  -X POST \
  http://croccante:8081/v1/programs/example-program/session/start
```

Filler continues for a missing publisher until explicit Stop. If Alana or the
control route becomes unavailable, Croccante keeps the last accepted state.
Manual recovery uses the same API with a new idempotency key and sequence; a
container restart is an emergency reset that returns the runtime to stopped.

## Diagnosing

```bash
docker logs -f croccante          # supervisor + hook activity
docker inspect -f '{{.State.Health.Status}}' croccante
docker exec croccante /usr/local/bin/healthcheck.sh    # explains WHY it is unhealthy
```

Supervisor state per destination:

```bash
docker exec croccante sh -c 'for f in /run/croccante/dest-*.state; do echo "$f: $(cat $f)"; done'
```

nginx's own view of connected clients:

```bash
docker exec croccante wget -qO- http://127.0.0.1:8080/stat
```

Destination URLs are masked in logs (`rtmp://host/live2/***`), so logs are safe
to paste.

| Symptom | Likely cause |
|---------|--------------|
| Stuck showing black on the platform | The publisher is gone and filler is covering it. Check `docker logs` for `publisher gone; filling`. |
| Filler never engages | No publish has happened since the current explicit Start. Filler only covers gaps after live media has appeared in that session. |
| Publisher connected but destinations remain idle | The session is explicitly stopped. Inspect the lifecycle API and have Alana issue Start. |
| API returns `401` | Alana's mounted token does not match Croccante's control-token file. Rotate both sides through the approved secret path. |
| API returns `409` | The command sequence is stale or reordered. Read state and retry only the intended newer command with a higher sequence. |
| One destination in `backoff`, others fine | Bad or revoked key, or that platform is refusing the connection. Check the URL. |
| All destinations `backoff` | Server lost egress, or the publisher is sending something no destination accepts. |
| `nginx reports N publisher(s) but no publisher marker` | The `exec_publish` hook cannot write its state directory. Check ownership of `/run/croccante/hooks`. |
| Relays time out with no data, `nclients` stays 0 | `worker_processes` is not 1. See architecture.md. |
| Healthcheck fine, no bytes at the platform | Publisher connected but the destination silently drops it — verify the key. |

## Verification before shipping a change

```bash
python3 test/test_filler_store.py -v
./test/smoke.sh
```

More than 50 checks against local RTMP and RTMPS sinks: private authentication, explicit
Start/Stop, idempotency and ordering, fan-out, RTMPS, destination isolation,
filler recovery, restart, control-plane loss, key masking, and health failure
detection. No real platform account is involved.

Manual verification against a real YouTube broadcast is still required before
trusting a change in production — the harness proves the relay mechanics, not
that a given platform accepts the stream.
