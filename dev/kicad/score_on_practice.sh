#!/bin/bash
# Run every handed-in program of `route-unseen-boards` on the four PRACTICE boards, with the task's own
# verifier image, the same 8 minutes per board and the same 2 CPUs. The agents report their own practice
# scores in their hand-in notes; this measures them.
#
#   dev/kicad/score_on_practice.sh [programs at once, default 4]
#
# Reads   results/harbor/route-unseen-boards-*/attempt-N/router/
# Writes  results/harbor/route-unseen-boards-*/attempt-N/practice/   reward.json, one JSON and one log per board
#
# The practice boards are the ones in the agent's container. The checker's own copy of each board
# (environment/boardcheck/boards/) holds what the grader needs.
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
at_once=${1:-4}
image=route-unseen-boards-tests
boards="usb-hub can-adapter interface-card pic-programmer"
docker build -q -t "$image" tasks/route-unseen-boards/tests >/dev/null || { echo "could not build $image"; exit 1; }
config="$ROOT/jobs/_kicad/practice.json"
mkdir -p "$(dirname "$config")"
printf '{"run": ["usb-hub", "can-adapter", "interface-card", "pic-programmer"], "scored": ["usb-hub", "can-adapter", "interface-card", "pic-programmer"], "minutes_per_board": 8}\n' > "$config"
mounts=""
for b in $boards; do
  base="tasks/route-unseen-boards/environment/boardcheck/boards/$b"
  for f in tasks/route-unseen-boards/environment/boards/$b/*; do
    cmp -s "$f" "$base/$(basename "$f")" || { echo "$b: the agent's copy of the practice board differs from the checker's"; exit 1; }
  done
  mounts="$mounts -v $ROOT/$base:/tests/boards/$b:ro"
done
running=0
for router in results/harbor/route-unseen-boards-*/attempt-*/router; do
  out="$(dirname "$router")/practice"
  [ -s "$out/reward.json" ] && { echo "have $out"; continue; }
  mkdir -p "$out"
  (
    # shellcheck disable=SC2086
    docker run --rm --network none --cpus 2 --memory 4g $mounts -v "$config":/tests/hidden.json:ro \
      -v "$ROOT/$router":/home/agent/work/router:ro -v "$ROOT/$out":/logs/verifier "$image" /tests/test.sh >/dev/null 2>&1
    rm -f "$out/run_hidden.err"
    echo "$(date +%H:%M:%S) $out: $(python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(r.get("reward"), "complete", r.get("boards_complete"))' "$out/reward.json" 2>/dev/null)"
  ) &
  running=$((running + 1))
  if [ "$running" -ge "$at_once" ]; then wait; running=0; fi
done
wait
echo "done"
