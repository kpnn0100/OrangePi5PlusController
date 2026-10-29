# The Arstro device manager (relay). Build from the REPOSITORY ROOT (the image also carries the
# standalone client files from server/):
#
#   docker build -f relay/Dockerfile -t arstro-devices .
#   docker run -d --name arstro-devices -p 8090:8090 -v arstro-devices:/data arstro-devices
#
# python:3.12-slim is multi-arch: the same file builds on amd64 and arm64 (Oracle Ampere).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RELAY_HOST=0.0.0.0 \
    RELAY_PORT=8090 \
    RELAY_DATA=/data

RUN useradd --system --uid 10001 --user-group --home-dir /app --shell /usr/sbin/nologin relay \
 && mkdir -p /data \
 && chown relay:relay /data \
 && chmod 700 /data

WORKDIR /app
COPY relay/arstro_relay /app/arstro_relay
# Served at B/client/: arstro_relay_client.py (= client.py), e2e.py, ws.py
COPY server/arstro_remote/relay/client.py server/arstro_remote/relay/e2e.py server/arstro_remote/web/ws.py \
     /app/arstro_relay/client/
RUN chmod -R a+rX,go-w /app

USER relay
VOLUME /data
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s%s/api/health' % (os.environ.get('RELAY_PORT') or '8090', os.environ.get('RELAY_BASE', '/arstro/internal/devices').rstrip('/')), timeout=4)" || exit 1
CMD ["python", "-m", "arstro_relay"]
