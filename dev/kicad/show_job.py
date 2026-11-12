#!/usr/bin/env python3
"""Print one line per attempt of a Harbor job on a routing task.

Usage: show_job.py <jobs/job-name> [more job dirs ...]
"""

import datetime
import json
import pathlib
import sys


def minutes(span):
    if not span or not span.get("started_at") or not span.get("finished_at"):
        return None
    a, b = (datetime.datetime.fromisoformat(span[k].replace("Z", "+00:00")) for k in ("started_at", "finished_at"))
    return (b - a).total_seconds() / 60


def main(job_dirs):
    for job in job_dirs:
        job = pathlib.Path(job)
        for path in sorted(job.glob("*/result.json")):
            d = json.loads(path.read_text())
            r = (d.get("verifier_result") or {}).get("rewards") or {}
            a = d.get("agent_result") or {}
            agent = (d.get("agent_info") or {}).get("model_info") or {}
            took = minutes(d.get("agent_execution"))
            err = (d.get("exception_info") or {}).get("exception_type")
            cost = a.get("cost_usd")
            boards = {k[:-len("_reward")]: v for k, v in r.items() if k.endswith("_reward")}
            if boards:  # the write-a-router task: one score per hidden board
                what = "  ".join(f"{name.replace('_', '-')} {score:.2f}{'*' if r.get(name + '_timed_out') else ''}" for name, score in sorted(boards.items()))
                what += f"  complete {r.get('boards_complete', '?')}/{r.get('boards_scored', len(boards))} of those scored  (* stopped at the time limit)"
            else:
                what = (f"nets {r.get('nets_good', '?')}/{r.get('nets_to_route', '?')}  open {r.get('nets_open', '?')}  "
                        f"with violations {r.get('nets_with_violations', '?')} ({r.get('violations', '?')})")
            print(f"{d.get('task_name', '?'):28} {agent.get('name', '?'):18} reward {r.get('reward', float('nan')):.3f}  {what}  "
                  f"{'%.0f min' % took if took is not None else '? min'}  "
                  f"{'$%.2f' % cost if cost is not None else '$?'}  {a.get('n_output_tokens', '?')} out tokens"
                  f"{'  ' + err if err else ''}")


if __name__ == "__main__":
    main(sys.argv[1:])
