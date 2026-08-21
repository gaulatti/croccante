# croccante — operations

## First-time server setup

Fedora 42 with Docker. The existing nginx edge proxy handles external TLS;
croccante only needs 1935, bound to loopback.

```bash
# 1. Deployment directory
sudo mkdir -p /opt/croccante
sudo chown $USER /opt/croccante

# 2. Destination configuration. This file is the ONLY copy of your stream keys.
cp .env.example /opt/croccante/.env
chmod 600 /opt/croccante/.env
$EDITOR /opt/croccante/.env
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

## Ending a broadcast

Filler runs for as long as a publisher is missing and never stops on its own.
Ending a broadcast is therefore a deliberate act:

```bash
docker restart croccante
```

That wipes the state directory, which ends the session. Until the next publish
the container sits idle and pushes nothing.

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
| Filler never engages | No publish has happened since container start, so no session is open. Filler only covers gaps inside a session. |
| One destination in `backoff`, others fine | Bad or revoked key, or that platform is refusing the connection. Check the URL. |
| All destinations `backoff` | Server lost egress, or the publisher is sending something no destination accepts. |
| `nginx reports N publisher(s) but no publisher marker` | The `exec_publish` hook cannot write its state directory. Check ownership of `/run/croccante/hooks`. |
| Relays time out with no data, `nclients` stays 0 | `worker_processes` is not 1. See architecture.md. |
| Healthcheck fine, no bytes at the platform | Publisher connected but the destination silently drops it — verify the key. |

## Verification before shipping a change

```bash
./test/smoke.sh
```

18 checks against local RTMP and RTMPS sinks: fan-out, RTMPS, destination
isolation, backoff, key masking, disconnect/reconnect, restart under load, and
that the healthcheck can both pass and fail. No real platform account involved.

Manual verification against a real YouTube broadcast is still required before
trusting a change in production — the harness proves the relay mechanics, not
that a given platform accepts the stream.
