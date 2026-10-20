# Sakoo probe station: both VPNs + the probe in one container. Same image on the laptop and on the Iran Linux host.
FROM python:3.12-alpine
RUN apk add --no-cache openvpn
WORKDIR /app
COPY entrypoint.sh probe-station.json ./
COPY probe.py ./
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
