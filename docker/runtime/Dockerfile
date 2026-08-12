# syntax=docker/dockerfile:1.24.0@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
FROM node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3

ARG RUNTIME_CONTRACT_SHA256=unknown
ARG SILICON_CLI_SPEC=silicon-cli==1.0.29
ARG SILICON_BROWSER_SPEC=silicon-browser==1.1.1
ARG SILICON_EXTEND_SPEC=silicon-extend==0.1.4
ARG SILICON_INTERFACE_CLI_URL=https://github.com/teamofsilicons/silicon-interface-web/releases/download/interface-cli-v2.0.8/teamofsilicons-silicon-interface-cli-2.0.8.tgz
ARG SILICON_INTERFACE_CLI_SHA256=9dc92a55a10e6ccaed0986cae8e5053fddff53118ff89047d501c9fd05c66bc7
ARG CLAUDE_CODE_SPEC=@anthropic-ai/claude-code@2.1.220
ARG CODEX_SPEC=@openai/codex@0.146.0
ARG PIP_SPEC=pip==26.2
ARG SETUPTOOLS_SPEC=setuptools==83.0.0
ARG WHEEL_SPEC=wheel==0.47.0

LABEL org.opencontainers.image.title="Silicon Runtime" \
      io.teamofsilicons.runtime-contract-sha256="${RUNTIME_CONTRACT_SHA256}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SILICON_EXTEND_REQUIRED_VERSION="0.1.4" \
    PATH="/opt/silicon-runtime/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        libffi-dev \
        libssl-dev \
        openssh-client \
        pkg-config \
        procps \
        python3 \
        python3-dev \
        python3-venv \
        tini \
        unzip \
    && python3 -m venv /opt/silicon-runtime \
    && /opt/silicon-runtime/bin/pip install --upgrade \
        --no-cache-dir \
        "${PIP_SPEC}" \
        "${SETUPTOOLS_SPEC}" \
        "${WHEEL_SPEC}" \
    && /opt/silicon-runtime/bin/pip install \
        --no-cache-dir \
        "${SILICON_CLI_SPEC}" \
        "${SILICON_BROWSER_SPEC}" \
        "${SILICON_EXTEND_SPEC}" \
    && /opt/silicon-runtime/bin/pip check \
    && apt-get purge -y --auto-remove \
        build-essential \
        libffi-dev \
        libssl-dev \
        pkg-config \
        python3-dev \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

RUN curl --fail --location --silent --show-error \
        --output /tmp/silicon-interface-cli.tgz \
        "${SILICON_INTERFACE_CLI_URL}" \
    && echo "${SILICON_INTERFACE_CLI_SHA256}  /tmp/silicon-interface-cli.tgz" \
        | sha256sum --check --strict \
    && npm install -g --no-audit --no-fund \
        /tmp/silicon-interface-cli.tgz \
    && rm -f /tmp/silicon-interface-cli.tgz \
    && npm install -g --no-audit --no-fund \
    "${CLAUDE_CODE_SPEC}" \
    "${CODEX_SPEC}" \
    && npm cache clean --force

COPY docker/runtime/activate-interface-cli.py /usr/local/libexec/silicon-activate-interface-cli.py
COPY docker/runtime/runtime-healthcheck.py /usr/local/libexec/silicon-runtime-healthcheck.py
COPY docker/runtime/runtime-entrypoint.sh /usr/local/bin/silicon-runtime-entrypoint
RUN chmod 0555 \
        /usr/local/libexec/silicon-activate-interface-cli.py \
        /usr/local/libexec/silicon-runtime-healthcheck.py \
        /usr/local/bin/silicon-runtime-entrypoint

WORKDIR /silicon
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python3", "/usr/local/libexec/silicon-runtime-healthcheck.py"]
ENTRYPOINT ["tini", "--", "/usr/local/bin/silicon-runtime-entrypoint"]
