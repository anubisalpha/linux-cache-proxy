FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    ca-certificates \
    curl \
    openssl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -r -m -s /usr/sbin/nologin cacheproxy

WORKDIR /opt/cache-proxy
COPY proxy/ /opt/cache-proxy/

# Two venvs: mitmproxy and the fastapi/uvicorn stack pin conflicting
# typing_extensions ranges, so they can't share one environment.
RUN python3 -m venv /opt/cache-proxy/venv-proxy \
    && /opt/cache-proxy/venv-proxy/bin/pip install --no-cache-dir -r requirements-proxy.txt \
    && python3 -m venv /opt/cache-proxy/venv-webui \
    && /opt/cache-proxy/venv-webui/bin/pip install --no-cache-dir -r requirements-webui.txt

RUN mkdir -p /var/lib/cache-proxy/files /var/lib/cache-proxy/mitmproxy-ca \
    && chown -R cacheproxy:cacheproxy /var/lib/cache-proxy /opt/cache-proxy

# Throwaway self-signed cert for the webui in this dev/test image only --
# the real .deb package's postinst generates one per-install instead (see
# packaging/build-deb.sh), so this never ships to production. Lives under
# /opt (image filesystem), NOT /var/lib/cache-proxy -- that path gets a
# named volume mounted over it at container start, which would shadow
# anything baked in there.
RUN mkdir -p /opt/cache-proxy/webui-tls \
    && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
       -keyout /opt/cache-proxy/webui-tls/key.pem \
       -out /opt/cache-proxy/webui-tls/cert.pem \
       -subj "/CN=cache-proxy-dev" \
    && chown -R cacheproxy:cacheproxy /opt/cache-proxy/webui-tls

USER cacheproxy

ENV CACHE_PROXY_DIR=/var/lib/cache-proxy/files
ENV CACHE_PROXY_DB=/var/lib/cache-proxy/index.db
ENV CACHE_PROXY_CONFDIR=/var/lib/cache-proxy/mitmproxy-ca
ENV CACHE_PROXY_WEBUI_TLS_CERT=/opt/cache-proxy/webui-tls/cert.pem
ENV CACHE_PROXY_WEBUI_TLS_KEY=/opt/cache-proxy/webui-tls/key.pem

# Overridden per-service by compose.yml. run_webui.sh calls bare
# python3/uvicorn, so PATH needs the right venv first for this fallback.
ENV PATH="/opt/cache-proxy/venv-webui/bin:${PATH}"
CMD ["/bin/bash", "run_webui.sh"]
