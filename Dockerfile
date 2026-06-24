FROM python:3.13-slim

ARG TARGETARCH=amd64
ARG RTLAMR_VERSION=v0.9.5

RUN apt-get update \
    && apt-get install -y --no-install-recommends rtl-sdr curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN case "${TARGETARCH}" in \
        amd64) RTLAMR_ARCH=amd64 ;; \
        arm64) RTLAMR_ARCH=arm64 ;; \
        arm)   RTLAMR_ARCH=armv6 ;; \
        *) echo "unsupported arch: ${TARGETARCH}" && exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/bemasher/rtlamr/releases/download/${RTLAMR_VERSION}/rtlamr_linux_${RTLAMR_ARCH}.tar.gz" \
       | tar -xz -C /usr/local/bin rtlamr \
    && chmod +x /usr/local/bin/rtlamr

RUN pip install --no-cache-dir "paho-mqtt>=2,<3"

COPY bridge.py /app/bridge.py

# Unhealthy when no meter message has been published recently (HEALTHCHECK_MAX_AGE,
# default 600s) -- e.g. a re-enumerated dongle the in-process watchdog couldn't recover.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "/app/bridge.py", "--healthcheck"]

ENTRYPOINT ["python", "-u", "/app/bridge.py"]
