FROM node:22-bookworm-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

ARG OPENCODE_VERSION

RUN test -n "$OPENCODE_VERSION" \
    && apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl \
    && uv python install 3.14 --install-dir /opt/tracebase-python \
    && python_bin="$(find /opt/tracebase-python -type f -name python3.14 -print -quit)" \
    && test -n "$python_bin" \
    && ln -sf "$python_bin" /usr/local/bin/python3 \
    && case "$(uname -m)" in x86_64) arch=x64;; aarch64) arch=arm64;; *) exit 1;; esac \
    && curl --fail --silent --show-error --location \
        "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${arch}.tar.gz" \
        | tar -xz -C /usr/local/bin \
    && mkdir -p /home/eval/.local/share/opencode \
    && chmod 0777 /home/eval/.local/share/opencode \
    && rm -rf /var/lib/apt/lists/*

ENV HOME=/home/eval
ENV XDG_DATA_HOME=/home/eval/.local/share

ENTRYPOINT ["opencode"]

FROM runtime AS summary

# Only this target needs the repository as its build context. The packaged
# Summary target above builds using just the distributed container directory.
FROM runtime AS integration
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
ENV HOME=/isolated/home XDG_CONFIG_HOME=/isolated/config \
    XDG_DATA_HOME=/isolated/data XDG_CACHE_HOME=/isolated/cache \
    XDG_STATE_HOME=/isolated/state OPENCODE_DISABLE_DEFAULT_PLUGINS=true \
    OPENCODE_DISABLE_MODELS_FETCH=true
WORKDIR /workspace
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --group dev --python /usr/local/bin/python3
ENV PATH="/workspace/.venv/bin:$PATH"
COPY tests/integration/opencode /integration
ENTRYPOINT ["bash", "/integration/entrypoint.sh"]
