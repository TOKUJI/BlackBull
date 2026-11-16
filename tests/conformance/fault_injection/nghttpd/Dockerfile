# A second reference HTTP/2 server for the differential: nghttp2's nghttpd,
# h2c (prior knowledge) only, answering 200 "ok" for / like the nginx peer.
FROM alpine:3.20
RUN apk add --no-cache nghttp2 && mkdir -p /srv && printf ok > /srv/index.html
