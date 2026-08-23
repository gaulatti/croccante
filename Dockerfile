FROM alpine:3.21

# ca-certificates is what makes ffmpeg's native rtmps:// usable — it replaces
# the stunnel sidecar this image used to carry for Facebook. See
# docs/architecture.md for the evidence behind that removal.
RUN apk add --no-cache \
        nginx \
        nginx-mod-rtmp \
        ffmpeg \
        python3 \
        ca-certificates \
    && update-ca-certificates \
    && mkdir -p /var/log/nginx /run/nginx

COPY nginx.conf        /etc/nginx/nginx.conf
COPY relay-lib.sh      /usr/local/bin/relay-lib.sh
COPY relay-dest.sh     /usr/local/bin/relay-dest.sh
COPY make-filler.sh    /usr/local/bin/make-filler.sh
COPY relay-start.sh    /usr/local/bin/relay-start.sh
COPY relay-stop.sh     /usr/local/bin/relay-stop.sh
COPY healthcheck.sh    /usr/local/bin/healthcheck.sh
COPY control-server.py /usr/local/bin/control-server.py
COPY entrypoint.sh     /entrypoint.sh

RUN chmod +x /entrypoint.sh /usr/local/bin/relay-*.sh /usr/local/bin/make-filler.sh /usr/local/bin/healthcheck.sh /usr/local/bin/control-server.py

EXPOSE 1935
EXPOSE 8081

# Checks relay supervisor liveness, not just the listener. See healthcheck.sh.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD /usr/local/bin/healthcheck.sh || exit 1

ENTRYPOINT ["/entrypoint.sh"]
