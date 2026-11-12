#!/bin/bash
# Reference program: a real router (router/route.py). It routes every board by the same steps and
# stores nothing about any board. It was written after the model runs, with the scored boards in
# view and without the 75-minute limit. It shows that the top score can be reached within the time
# each board gets. It does not show that such a program can be written blind in 75 minutes.
set -euo pipefail
mkdir -p /home/agent/work/router
cp -r "$(dirname "$0")/router/." /home/agent/work/router/
