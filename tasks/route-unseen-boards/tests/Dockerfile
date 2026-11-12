# Verifier image: KiCad 9, numpy, the checker, and the hidden boards. The agent's program runs here as
# an unprivileged user that cannot write the score.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
RUN apt-get update && apt-get install -y --no-install-recommends kicad python3 ca-certificates git unzip \
    && rm -rf /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y --no-install-recommends python3-numpy procps \
    && rm -rf /var/lib/apt/lists/* && useradd --create-home --shell /bin/bash runner
WORKDIR /tests
COPY . /tests/
RUN chmod +x /tests/test.sh && chmod -R go-w /tests
