FROM alpine:3.21
RUN apk add --no-cache nginx nginx-mod-rtmp stunnel openssl \
    && mkdir -p /run/nginx /etc/stunnel /rec
COPY nginx.conf /etc/nginx/nginx.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 1935 443
ENTRYPOINT ["/entrypoint.sh"]
