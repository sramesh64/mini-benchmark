# Development image for the real-board tasks: KiCad 9 (kicad-cli and the pcbnew Python module), no GUI libraries.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
RUN apt-get update && apt-get install -y --no-install-recommends kicad python3 ca-certificates git unzip \
    && rm -rf /var/lib/apt/lists/*
