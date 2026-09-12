FROM node:22-bookworm-slim

ARG OPENCODE_VERSION=1.18.29

RUN npm install --global "opencode-ai@${OPENCODE_VERSION}" \
    && mkdir -p /home/eval/.local/share/opencode \
    && chmod 0777 /home/eval/.local/share/opencode

ENV HOME=/home/eval
ENV XDG_DATA_HOME=/home/eval/.local/share

ENTRYPOINT ["opencode"]
