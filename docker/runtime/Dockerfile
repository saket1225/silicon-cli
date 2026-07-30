# syntax=docker/dockerfile:1.24.0@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
FROM node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3

ARG SILICON_CLI_SPEC=silicon-cli==1.0.22
ARG SILICON_BROWSER_SPEC=silicon-browser==1.1.1
ARG SILICON_EXTEND_SPEC=silicon-extend==0.1.1
ARG SILICON_INTERFACE_CLI_URL=https://github.com/teamofsilicons/silicon-interface-web/releases/download/interface-cli-v2.0.2/teamofsilicons-silicon-interface-cli-2.0.2.tgz
ARG SILICON_INTERFACE_CLI_SHA256=5f594958e8165dfaf87e19a71781a628012b5debe0482dcdc24f28b308e710b2
ARG CLAUDE_CODE_SPEC=@anthropic-ai/claude-code@2.1.220
ARG CODEX_SPEC=@openai/codex@0.145.0
ARG PIP_SPEC=pip==26.1.2
ARG SETUPTOOLS_SPEC=setuptools==83.0.0
ARG WHEEL_SPEC=wheel==0.47.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SILICON_EXTEND_REQUIRED_VERSION="0.1.1" \
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
        python3-pip \
        python3-venv \
        tini \
        unzip \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/silicon-runtime \
    && /opt/silicon-runtime/bin/pip install --upgrade \
        "${PIP_SPEC}" \
        "${SETUPTOOLS_SPEC}" \
        "${WHEEL_SPEC}" \
    && /opt/silicon-runtime/bin/pip install \
        "${SILICON_CLI_SPEC}" \
        "${SILICON_BROWSER_SPEC}" \
        "${SILICON_EXTEND_SPEC}" \
    && /opt/silicon-runtime/bin/pip check

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
    "${CODEX_SPEC}"

COPY docker/runtime/activate-interface-cli.py /usr/local/libexec/silicon-activate-interface-cli.py
COPY docker/runtime/runtime-entrypoint.sh /usr/local/bin/silicon-runtime-entrypoint
RUN chmod 0555 \
        /usr/local/libexec/silicon-activate-interface-cli.py \
        /usr/local/bin/silicon-runtime-entrypoint

WORKDIR /silicon
ENTRYPOINT ["tini", "--", "/usr/local/bin/silicon-runtime-entrypoint"]
