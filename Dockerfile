FROM alpine:3.21

RUN apk add --no-cache \
    nginx \
    nginx-mod-rtmp \
    stunnel \
    gettext \
    && mkdir -p /var/log/nginx /run/nginx /etc/stunnel

COPY nginx.conf.template /etc/nginx/nginx.conf.template
COPY stunnel.conf         /etc/stunnel/stunnel.conf
COPY entrypoint.sh        /entrypoint.sh

RUN chmod +x /entrypoint.sh

EXPOSE 1935

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD nc -z localhost 1935 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
