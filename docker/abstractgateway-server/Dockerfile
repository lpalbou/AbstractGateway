ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ARG ABSTRACTGATEWAY_VERSION
ARG ABSTRACTGATEWAY_INSTALL_MODE=pypi
ARG ABSTRACTGATEWAY_EXTRAS=server

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    ABSTRACTGATEWAY_DATA_DIR=/data/gateway \
    ABSTRACTGATEWAY_FLOWS_DIR=/data/flows \
    ABSTRACTGATEWAY_WORKFLOW_SOURCE=bundle \
    ABSTRACTGATEWAY_STORE_BACKEND=file \
    ABSTRACTGATEWAY_ALLOWED_ORIGINS=http://localhost:*,http://127.0.0.1:* \
    ABSTRACTGATEWAY_EMBEDDING_PROVIDER=disabled

RUN addgroup --system abstractgateway \
    && adduser --system --ingroup abstractgateway --home /home/abstractgateway abstractgateway \
    && mkdir -p /data/gateway /data/flows /workspace \
    && chown -R abstractgateway:abstractgateway /data /workspace

COPY pyproject.toml README.md CHANGELOG.md /tmp/abstractgateway-src/
COPY src /tmp/abstractgateway-src/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libmagic1 \
    && python -m pip install --upgrade pip \
    && if [ "$ABSTRACTGATEWAY_INSTALL_MODE" = "local" ]; then \
        python -m pip install "/tmp/abstractgateway-src[${ABSTRACTGATEWAY_EXTRAS}]"; \
    else \
        if [ -n "$ABSTRACTGATEWAY_VERSION" ]; then \
            python -m pip install "abstractgateway[${ABSTRACTGATEWAY_EXTRAS}]==${ABSTRACTGATEWAY_VERSION}"; \
        else \
            python -m pip install "abstractgateway[${ABSTRACTGATEWAY_EXTRAS}]"; \
        fi; \
    fi \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/* /tmp/abstractgateway-src

USER abstractgateway
WORKDIR /home/abstractgateway

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8080\")}/api/health', timeout=3).read()" || exit 1

CMD ["sh", "-c", "abstractgateway serve --host \"${HOST:-0.0.0.0}\" --port \"${PORT:-8080}\""]
