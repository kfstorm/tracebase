FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ARG OPENCODE_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
RUN case "$(uname -m)" in x86_64) arch=x64;; aarch64) arch=arm64;; *) exit 1;; esac \
    && curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${arch}.tar.gz" \
    | tar -xz -C /usr/local/bin
ENV HOME=/isolated/home XDG_CONFIG_HOME=/isolated/config \
    XDG_DATA_HOME=/isolated/data XDG_CACHE_HOME=/isolated/cache \
    XDG_STATE_HOME=/isolated/state OPENCODE_DISABLE_DEFAULT_PLUGINS=true \
    OPENCODE_DISABLE_MODELS_FETCH=true
WORKDIR /workspace
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --group dev
ENV PATH="/workspace/.venv/bin:$PATH"
COPY tests/integration/opencode /integration
ENTRYPOINT ["bash", "/integration/entrypoint.sh"]
