#!/bin/sh
# Run as root when the agent is done: make sure there is a folder to collect.
mkdir -p /home/agent/work/router
[ -n "$(ls -A /home/agent/work/router 2>/dev/null)" ] || echo "nothing was saved" > /home/agent/work/router/EMPTY
