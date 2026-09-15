FROM node:22-bookworm-slim

ARG OPENCODE_VERSION

RUN test -n "$OPENCODE_VERSION" \
    && apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl \
    && curl --fail --silent --show-error --location https://astral.sh/uv/install.sh \
        | UV_INSTALL_DIR=/usr/local/bin sh \
    && uv python install 3.14 --install-dir /opt/tracebase-python \
    && python_bin="$(find /opt/tracebase-python -type f -name python3.14 -print -quit)" \
    && test -n "$python_bin" \
    && ln -sf "$python_bin" /usr/local/bin/python3 \
    && npm install --global "opencode-ai@${OPENCODE_VERSION}" \
    && mkdir -p /home/eval/.local/share/opencode \
    && chmod 0777 /home/eval/.local/share/opencode \
    && rm -rf /var/lib/apt/lists/*

ENV HOME=/home/eval
ENV XDG_DATA_HOME=/home/eval/.local/share

ENTRYPOINT ["opencode"]
