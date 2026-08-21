#!/bin/sh
# Test-only RTMP sink. With TLS=1 it also fronts itself with stunnel on 443,
# giving the harness a real rtmps:// endpoint to prove ffmpeg's native RTMPS
# output works without a stunnel sidecar in croccante itself.
set -eu
# nginx workers run as the nginx user and must be able to write recordings.
mkdir -p /rec
chown -R nginx:nginx /rec

if [ "${TLS:-0}" = "1" ]; then
    openssl req -new -x509 -days 365 -nodes \
        -subj "/CN=rtmps-sink" \
        -out /etc/stunnel/stunnel.pem \
        -keyout /etc/stunnel/stunnel.key >/dev/null 2>&1
    cat /etc/stunnel/stunnel.key >> /etc/stunnel/stunnel.pem
    cat > /etc/stunnel/stunnel.conf << 'CONF'
foreground = yes
pid =
[rtmps]
accept = 443
connect = 127.0.0.1:1935
cert = /etc/stunnel/stunnel.pem
CONF
    stunnel /etc/stunnel/stunnel.conf &
    echo "[sink] stunnel listening on 443 (TLS -> 1935)"
fi

exec nginx -g 'daemon off;'
