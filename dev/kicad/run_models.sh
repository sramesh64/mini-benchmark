#!/bin/sh
# Run Claude Code on routing tasks through Harbor, on local Docker, signed in with an OAuth token.
#
#   dev/kicad/run_models.sh <model> <attempts> <concurrent> <slug> [slug ...]
#   e.g. dev/kicad/run_models.sh claude-opus-5 4 4 unseen-boards
#
# Needs .env.oauth in the repo root (git-ignored), holding
#   CLAUDE_CODE_OAUTH_TOKEN=...
#   CLAUDE_FORCE_OAUTH=1
# It never reads .env, so the API key kept there is not used.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
model=$1; attempts=$2; concurrent=$3; shift 3
[ -s .env.oauth ] || { echo "missing .env.oauth (see the top of this script)"; exit 2; }
mkdir -p jobs   # Harbor's job folders and this script's logs go there; a fresh clone has no such folder
short=$(echo "$model" | sed 's/^claude-//')
for slug in "$@"; do
  job="route-$slug-$short${JOB_SUFFIX:-}"   # JOB_SUFFIX names a separate job, e.g. for a second batch
  # Harbor downloads Claude Code into each container, about 340 MB, hence the longer setup timeout.
  env -u ANTHROPIC_API_KEY harbor run -p "tasks/route-$slug" -a claude-code -m "$model" -k "$attempts" -n "$concurrent" -y -q \
    --env-file .env.oauth --agent-setup-timeout-multiplier 6 --job-name "$job" > "jobs/$job.log" 2>&1 || echo "harbor exited with an error for $slug"
  python3 dev/kicad/show_job.py "jobs/$job" || true
done
