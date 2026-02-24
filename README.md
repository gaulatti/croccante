# croccante

Minimal nginx-rtmp relay container. Receives a single RTMP stream from OBS on port 1935 and simultaneously pushes to YouTube, Twitch, and Facebook (RTMPS via stunnel). No transcoding.

## Architecture

```
OBS → [host:1935] → nginx-rtmp ──► rtmp://a.rtmp.youtube.com/live2/<key>
                                ──► rtmp://live.twitch.tv/app/<key>
                                ──► rtmp://127.0.0.1:19350/rtmp/<key>
                                         │
                                    stunnel (TLS)
                                         │
                                         ▼
                              live-api-s.facebook.com:443 (RTMPS)
```

## Server prerequisites

- Fedora 42, Docker + Compose plugin installed
- Existing nginx edge proxy handles TLS — croccante only needs port 1935
- The deploy directory lives at `/opt/croccante`

## First-time server setup

```bash
# 1. Create the deployment directory
sudo mkdir -p /opt/croccante
sudo chown $USER /opt/croccante

# 2. Copy docker-compose.yml onto the server
scp docker-compose.yml user@server:/opt/croccante/

# 3. Create the .env file with real stream keys (never committed to git)
cp .env.example /opt/croccante/.env
$EDITOR /opt/croccante/.env

# 4. Authenticate Docker to GHCR (one-time; use a PAT with read:packages)
echo "<ghcr-pat>" | docker login ghcr.io -u <github-username> --password-stdin

# 5. Pull and start
cd /opt/croccante
docker compose up -d
```

## OBS settings

| Field        | Value                          |
|--------------|--------------------------------|
| Service      | Custom                         |
| Server       | `rtmp://<server-ip>:1935/live` |
| Stream key   | anything (e.g. `stream`)       |

## CI/CD (GitHub Actions)

Push to `main` → build image → push to GHCR → SSH into server → pull → hot-switch.

### Required GitHub secrets

| Secret                   | Description                                     |
|--------------------------|-------------------------------------------------|
| `SERVER_HOST`            | Server IP or hostname                           |
| `SERVER_USER`            | SSH user                                        |
| `SERVER_SSH_KEY`         | Private SSH key (no passphrase)                 |
| `SERVER_SSH_FINGERPRINT` | Server host key fingerprint (`ssh-keyscan`)     |
| `GHCR_PAT`               | GitHub PAT with `read:packages` scope           |

```bash
# Get the fingerprint to paste into SERVER_SSH_FINGERPRINT
ssh-keyscan -t ed25519 <server-ip>
```

### Deployment directory on server

The workflow `cd /opt/croccante` and runs `docker compose up`. Keep `docker-compose.yml` and `.env` there. The workflow does **not** push these files — manage them manually or via a separate secrets manager.

## Local dev / smoke test

```bash
# Build locally
docker build -t croccante:dev .

# Run with dummy keys (stunnel will fail to connect — that's fine for local)
docker run --rm -p 1935:1935 \
  -e YOUTUBE_STREAM_KEY=test \
  -e TWITCH_STREAM_KEY=test \
  -e FACEBOOK_STREAM_KEY=test \
  croccante:dev

# Verify RTMP port is up
nc -z localhost 1935 && echo "OK"
```

## File map

```
croccante/
├── Dockerfile              # Alpine + nginx-mod-rtmp + stunnel
├── nginx.conf.template     # RTMP relay config; vars substituted at startup
├── stunnel.conf            # RTMP→RTMPS proxy for Facebook
├── entrypoint.sh           # Renders config, starts stunnel, execs nginx
├── docker-compose.yml      # Production compose file
├── .env.example            # Stream key template (copy → .env on server)
└── .github/
    └── workflows/
        └── deploy.yml      # Build → GHCR → SSH hot-switch
```

## Updating stream keys

Stream keys live only in `/opt/croccante/.env` on the server. To change them:

```bash
ssh user@server
$EDITOR /opt/croccante/.env
cd /opt/croccante
docker compose up -d --force-recreate croccante
```

No image rebuild needed — keys are injected at container start via `env_file`.

## Facebook RTMPS note

Facebook requires RTMPS (RTMP over TLS). Inside the container, stunnel listens on `127.0.0.1:19350` and wraps outbound traffic in TLS before forwarding to `live-api-s.facebook.com:443`. nginx pushes plain RTMP to stunnel's local port. No certificates to manage — stunnel uses the system CA bundle to verify Facebook's server certificate.
