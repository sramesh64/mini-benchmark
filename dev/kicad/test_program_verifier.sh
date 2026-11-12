#!/bin/bash
# Checks of the task's verifier. It runs five stand-in "programs" through the verifier image and
# compares the scores with what they should be:
#
#   the task's reference solution                    1.0, every board complete
#   straight lines between pads                      below 0.1
#   a program that never finishes                    0.0, stopped at the time limit
#   one that writes an unrouted board, then hangs    0.0, and the file it wrote is the one scored
#   one that tries to write the score and edit the grader    0.0, and nothing it tried worked
#
# Usage: dev/kicad/test_program_verifier.sh        (needs Docker; about 8 minutes)
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
fail=0
task=unseen-boards
image="route-$task-tests"
docker build -q -t "$image" "tasks/route-$task/tests" >/dev/null || { echo "could not build $image"; exit 1; }
for prog in reference straight sleeper partial cheat; do
  src="dev/kicad/unseen_baselines/$prog"; limit=20
  [ "$prog" = reference ] && src="tasks/route-$task/solution/router"
  [ "$prog" = reference ] || [ "$prog" = straight ] && limit=480
  out="jobs/_kicad/verifier_tests/$task/$prog"
  mkdir -p "$out"
  docker run --rm --network none --cpus 2 -e ROUTE_LIMIT_SECONDS=$limit -v "$ROOT/$src":/home/agent/work/router:ro \
    -v "$ROOT/$out":/logs/verifier "$image" /tests/test.sh >/dev/null
  python3 - "$out" "$task" "$prog" <<'EOF' || fail=1
import json, pathlib, sys
out, task, prog = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
r = json.loads((out / "reward.json").read_text())
timed_out = [k for k, v in r.items() if k.endswith("_timed_out") and v]
ok = {
    "reference": r["reward"] == 1.0 and r["full_pass"] == 1,
    "straight": r["reward"] < 0.1 and r["full_pass"] == 0,
    "sleeper": r["reward"] == 0.0 and len(timed_out) == sum(1 for k in r if k.endswith("_timed_out")),
    "partial": r["reward"] == 0.0 and bool(timed_out),
    "cheat": r["reward"] == 0.0 and r["full_pass"] == 0,
}[prog]
note = ""
if prog == "cheat":  # what the program reported about its own attempts
    logs = sorted(out.glob("*.program.log"))
    tried = json.loads(logs[0].read_text().strip().splitlines()[0]) if logs else {}
    wrote = [k for k, v in tried.items() if v == "WRITTEN" and not k.startswith("/logs/")]
    ok = ok and not wrote and tried.get("whoami") == "runner" and tried.get("network") == "closed"
    note = f"  ran as {tried.get('whoami')}, network {tried.get('network')}, files it changed outside the results folder: {wrote or 'none'}"
print(("ok   " if ok else "FAIL ") + f"{task:15} {prog:10} score {r['reward']:.3f}, complete {r.get('boards_complete', 0)}{note}")
sys.exit(0 if ok else 1)
EOF
done
[ $fail = 0 ] && echo "all verifier checks passed" || { echo "some verifier checks FAILED"; exit 1; }
