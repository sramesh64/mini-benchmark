#!/usr/bin/env python3
"""Run a routing program on every hidden board and score the boards it writes.

Usage: run_hidden.py <folder with route.py> <folder for the results>
The program runs as the unprivileged user "runner", with no network (the container has none), for at
most MINUTES_PER_BOARD minutes per board. Whatever routed file exists afterwards is scored.
"""

import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "hidden.json")) as f:
    CONFIG = json.load(f)
LIMIT = int(os.environ.get("ROUTE_LIMIT_SECONDS", CONFIG["minutes_per_board"] * 60))  # the variable is for testing this script


def run_board(program_dir, slug, results):
    base = os.path.join(HERE, "boards", slug)
    with open(os.path.join(base, "task.json")) as f:
        name = json.load(f)["board"]
    work = os.path.join("/run/hidden", slug)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        if os.path.exists(os.path.join(base, name + ext)):
            shutil.copy(os.path.join(base, name + ext), os.path.join(work, name + ext))
    home = os.path.join("/run/home", slug)
    os.makedirs(home, exist_ok=True)
    subprocess.run(["chown", "-R", "runner:runner", work, home], check=False)
    routed = os.path.join(work, "routed.kicad_pcb")
    command = (f"cd {work} && HOME={home} exec timeout -k 5 {LIMIT} python3 {program_dir}/route.py "
               f"{work}/{name}.kicad_pcb {routed} > {work}/program.log 2>&1")
    started = time.time()
    try:
        done = subprocess.run(["su", "runner", "-s", "/bin/sh", "-c", command], timeout=LIMIT + 60)
        status = done.returncode
    except subprocess.TimeoutExpired:
        status = 124
    seconds = round(time.time() - started)
    subprocess.run(["pkill", "-9", "-u", "runner"], check=False)  # nothing of the program survives into the next board
    result = {"reward": 0.0, "full_pass": 0}
    if os.path.exists(routed) and os.path.getsize(routed) > 0:
        graded = os.path.join(results, slug + ".json")
        env = dict(os.environ, BOARDCHECK_BASE=base)
        subprocess.run([sys.executable, os.path.join(HERE, "boardcheck.py"), routed, "--json", graded],
                       capture_output=True, text=True, env=env)
        try:
            with open(graded) as f:
                result = json.load(f)
        except (OSError, ValueError):
            pass
    try:  # keep the tail of what the program printed
        with open(os.path.join(work, "program.log"), errors="replace") as f:
            tail = f.read()[-2000:]
        with open(os.path.join(results, slug + ".program.log"), "w") as f:
            f.write(tail)
    except OSError:
        pass
    result.update(seconds=seconds, exit_status=status, timed_out=int(status in (124, 137)))
    return result


def main(program_dir, final):
    # Results are collected in a folder only root can touch and copied out at the end, after the last
    # run's processes are gone, so the program cannot plant or change anything that is read as a result.
    results = "/run/results"
    shutil.rmtree(results, ignore_errors=True)
    os.makedirs(results, mode=0o700)
    out = {}
    if not os.path.exists(os.path.join(program_dir, "route.py")):
        out = {"reward": 0.0, "full_pass": 0, "submitted": 0}
        print("No route.py was handed in.")
    else:
        # The program is copied to a place its own runs cannot change.
        shutil.rmtree("/run/router", ignore_errors=True)
        shutil.copytree(program_dir, "/run/router")
        subprocess.run(["chmod", "-R", "a+rX,go-w", "/run/router"], check=False)
        rewards = []
        for slug in CONFIG["run"]:
            r = run_board("/run/router", slug, results)
            if slug in CONFIG["scored"]:
                rewards.append(r.get("reward", 0.0))
            key = slug.replace("-", "_")
            out[key + "_reward"] = r.get("reward", 0.0)
            out[key + "_complete"] = r.get("full_pass", 0)
            out[key + "_seconds"] = r["seconds"]
            out[key + "_timed_out"] = r["timed_out"]
            print(f"{slug}{'' if slug in CONFIG['scored'] else ' (not in the score)'}: score {r.get('reward', 0.0):.3f}, {r.get('nets_good', 0)} of {r.get('nets_to_route', '?')} nets, "
                  f"{r.get('nets_open', '?')} open, {r.get('violations', '?')} violations, {r['seconds']} s"
                  f"{', stopped at the time limit' if r['timed_out'] else ''}{', error: ' + r['error'] if 'error' in r else ''}")
        out["reward"] = round(sum(rewards) / len(rewards), 4)
        out["full_pass"] = int(all(out[s.replace("-", "_") + "_complete"] for s in CONFIG["scored"]))
        out["boards_complete"] = sum(out[s.replace("-", "_") + "_complete"] for s in CONFIG["scored"])
        out["boards_scored"] = len(rewards)
        out["submitted"] = 1
        print(f"Score: {out['reward']:.3f} (the average of {len(rewards)} boards); boards complete: {out['boards_complete']} of {len(rewards)}")
    with open(os.path.join(results, "reward.json"), "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
    subprocess.run(["pkill", "-9", "-u", "runner"], check=False)
    os.makedirs(final, exist_ok=True)
    for name in os.listdir(results):  # replace whatever is there, including anything the program left
        target = os.path.join(final, name)
        if os.path.islink(target) or os.path.isfile(target):
            os.unlink(target)
        elif os.path.isdir(target):
            shutil.rmtree(target)
        shutil.copy(os.path.join(results, name), target)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
