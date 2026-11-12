#!/bin/bash
# Run the agent's program on each hidden board, then score what it wrote. Always writes /logs/verifier/reward.json.
set -u
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.json /logs/verifier/reward.txt
python3 /tests/run_hidden.py /home/agent/work/router /logs/verifier > /logs/verifier/report.md 2>/logs/verifier/run_hidden.err \
  || echo '{"reward": 0.0, "full_pass": 0, "grader_error": 1}' > /logs/verifier/reward.json
[ -s /logs/verifier/reward.json ] || echo '{"reward": 0.0, "full_pass": 0, "grader_error": 1}' > /logs/verifier/reward.json
cat /logs/verifier/reward.json
exit 0
