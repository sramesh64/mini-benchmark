# "Write a router" task (agent environment): KiCad 9 command-line tools and Python module, no GUI use.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
RUN apt-get update && apt-get install -y --no-install-recommends kicad python3 ca-certificates git unzip \
    && rm -rf /var/lib/apt/lists/*
# curl, nodejs, npm and procps are what Harbor's agent installers need. numpy is for the agent's own code.
RUN apt-get update && apt-get install -y --no-install-recommends curl nodejs npm procps python3-numpy \
    && rm -rf /var/lib/apt/lists/*

# The checker with a pristine copy of every practice board. Nothing here is secret; it is read-only for the agent.
COPY boardcheck/ /opt/boardcheck/
RUN chmod -R a+rX /opt/boardcheck && chmod 755 /opt/boardcheck/ensure_output.sh \
    && printf '#!/bin/sh\nexec python3 /opt/boardcheck/boardcheck.py "$@"\n' > /usr/local/bin/boardcheck \
    && chmod 755 /usr/local/bin/boardcheck \
    && useradd --create-home --shell /bin/bash agent

COPY boards/ /home/agent/boards/
RUN mkdir -p /home/agent/work/router && chown -R agent:agent /home/agent

WORKDIR /home/agent
