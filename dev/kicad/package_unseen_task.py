#!/usr/bin/env python3
"""Package the "write a router, judged on boards you have not seen" task as a Harbor task directory.

Usage: python3 dev/kicad/package_unseen_task.py [config ...]     (default: every entry of CONFIGS)

The boards the agent may see go into its environment with the checker. The boards its program is run
on exist only in the verifier image, which runs the program on each of them and scores what it writes.
A new instance of the task is a new entry in CONFIGS: other practice boards and other hidden boards.
"""

import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent
MADE = ROOT / "jobs/_kicad/made"
MINUTES_PER_BOARD = 8
AGENT_MINUTES = 75
BASE_IMAGE = "debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2"
KICAD_LAYER = ("RUN apt-get update && apt-get install -y --no-install-recommends kicad python3 ca-certificates git unzip \\\n"
               "    && rm -rf /var/lib/apt/lists/*\n")

# "seen" boards are in the agent's environment; "run" boards are the ones the verifier runs the program
# on; "scored" boards are the ones whose result makes the score.
CONFIGS = {
    "unseen-boards": {
        "seen": ["usb-hub", "can-adapter", "interface-card", "pic-programmer"],
        "run": ["bbq20-keyboard", "macro-pad", "keyboard-featherwing", "cpld-test-board"],
        "scored": ["bbq20-keyboard", "macro-pad", "keyboard-featherwing", "cpld-test-board"],
        "description": "Write a program that routes real KiCad boards. It is run on boards the agent has not seen and graded by KiCad's own design-rule check.",
    },
}

TASK_TOML = '''schema_version = "1.4"
# The verifier gets one folder from the agent's container: the routing program.
artifacts = [
  {{ source = "/home/agent/work/router" }},
]

[metadata]
name = "route-{config}"
description = "{description}"
difficulty = "hard"
category = "engineering"
tags = ["electronics", "pcb-layout", "routing", "kicad", "held-out"]

[verifier]
timeout_sec = {verifier_timeout}.0
environment_mode = "separate"
user = "root"
# The agent's program runs in the verifier container: it gets no network there either.
network_mode = "no-network"

# Make sure there is a folder to collect even if the agent saved nothing.
[[verifier.collect]]
command = "/opt/boardcheck/ensure_output.sh"
user = "root"
timeout_sec = 60.0

[verifier.env]

[verifier.environment]
build_timeout_sec = 1800.0
os = "linux"
cpus = 2
memory_mb = 4096

[agent]
timeout_sec = {agent_timeout}.0
user = "agent"
# While the agent works, the container can reach the model API and nothing else.
network_mode = "allowlist"
allowed_hosts = []

[environment]
build_timeout_sec = 1800.0
os = "linux"
cpus = 2
memory_mb = 4096
mcp_servers = []

[environment.env]

[solution.env]
'''

ENV_DOCKERFILE = '''# "Write a router" task (agent environment): KiCad 9 command-line tools and Python module, no GUI use.
FROM {base}
{kicad}# curl, nodejs, npm and procps are what Harbor's agent installers need. numpy is for the agent's own code.
RUN apt-get update && apt-get install -y --no-install-recommends curl nodejs npm procps python3-numpy \\
    && rm -rf /var/lib/apt/lists/*

# The checker with a pristine copy of every practice board. Nothing here is secret; it is read-only for the agent.
COPY boardcheck/ /opt/boardcheck/
RUN chmod -R a+rX /opt/boardcheck && chmod 755 /opt/boardcheck/ensure_output.sh \\
    && printf '#!/bin/sh\\nexec python3 /opt/boardcheck/boardcheck.py "$@"\\n' > /usr/local/bin/boardcheck \\
    && chmod 755 /usr/local/bin/boardcheck \\
    && useradd --create-home --shell /bin/bash agent

COPY boards/ /home/agent/boards/
RUN mkdir -p /home/agent/work/router && chown -R agent:agent /home/agent

WORKDIR /home/agent
'''

TESTS_DOCKERFILE = '''# Verifier image: KiCad 9, numpy, the checker, and the hidden boards. The agent's program runs here as
# an unprivileged user that cannot write the score.
FROM {base}
{kicad}RUN apt-get update && apt-get install -y --no-install-recommends python3-numpy procps \\
    && rm -rf /var/lib/apt/lists/* && useradd --create-home --shell /bin/bash runner
WORKDIR /tests
COPY . /tests/
RUN chmod +x /tests/test.sh && chmod -R go-w /tests
'''

TEST_SH = '''#!/bin/bash
# Run the agent's program on each hidden board, then score what it wrote. Always writes /logs/verifier/reward.json.
set -u
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.json /logs/verifier/reward.txt
python3 /tests/run_hidden.py /home/agent/work/router /logs/verifier > /logs/verifier/report.md 2>/logs/verifier/run_hidden.err \\
  || echo '{"reward": 0.0, "full_pass": 0, "grader_error": 1}' > /logs/verifier/reward.json
[ -s /logs/verifier/reward.json ] || echo '{"reward": 0.0, "full_pass": 0, "grader_error": 1}' > /logs/verifier/reward.json
cat /logs/verifier/reward.json
exit 0
'''

RUN_HIDDEN = r'''#!/usr/bin/env python3
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
'''

ENSURE = '''#!/bin/sh
# Run as root when the agent is done: make sure there is a folder to collect.
mkdir -p /home/agent/work/router
[ -n "$(ls -A /home/agent/work/router 2>/dev/null)" ] || echo "nothing was saved" > /home/agent/work/router/EMPTY
'''

SOLVE = '''#!/bin/bash
# Reference program: a real router (router/route.py). It routes every board by the same steps and
# stores nothing about any board. It was written after the model runs, with the scored boards in
# view and without the 75-minute limit. It shows that the top score can be reached within the time
# each board gets. It does not show that such a program can be written blind in 75 minutes.
set -euo pipefail
mkdir -p /home/agent/work/router
cp -r "$(dirname "$0")/router/." /home/agent/work/router/
'''

COMMON_TOOLS = """- **KiCad 9, without its graphical programs:** `kicad-cli`, and the `pcbnew` Python module
  (`python3 -c "import pcbnew"`). numpy is installed.
- **`boardcheck --board <folder name> <routed file>`:** checks a routed board from `/home/agent/boards/`
  the same way the grader does and prints the result. `boardcheck --help` lists its options. Its
  source is in `/opt/boardcheck/`.
- **No network.** The container can reach the model API and nothing else."""

COMMON_HAND_IN = """## What to hand in

A folder `/home/agent/work/router/` with a file `route.py` in it. The grader runs

    python3 route.py <board.kicad_pcb> <routed.kicad_pcb>

The first argument is an unrouted board; its `.kicad_pro` and `.kicad_dru` are next to it under the
same name. Your program writes the routed board to the second.

## Rules

- The program adds tracks and vias only. Only tracks, arcs and vias are read from the file it writes.
  Parts, pads, nets, zones, the board outline and the design rules are taken from the board as given.
- Vias go through the whole board. Blind, buried and micro vias are not read.
- The program runs by itself, without you and without a network. It may use what is installed here
  (KiCad 9's Python module and command line, numpy, Python's standard library) and the files in its
  own folder.
- Use only the software in this environment."""

COMMON_SCORING = """- Each run gets {minutes} minutes on 2 CPUs with 4 GB of memory. The routed file that exists when the run
  ends is the one that is scored; no file scores 0.
- A board is scored the way `boardcheck` scores it: the nets that count, divided by the nets that
  need routing. A net counts if all its pads are connected and none of its new copper is in a rule
  violation.
- On a net with a length rule, a track or via that leads nowhere counts as a violation: the length
  has to be made up with copper the signal runs through."""

INSTRUCTIONS = {
    "unseen-boards": """# Write a router

Write a program that routes circuit boards. It is judged on boards you have not seen.

## What you have

- **{n_seen} practice boards** in `/home/agent/boards/`, one folder each. Each is a real KiCad board with every
  part placed and the copper tracks missing, with its design rules next to it: the project file
  (`.kicad_pro`) and a rules file (`.kicad_dru`). The rules file sets a minimum track width for some
  nets and, on some boards, a maximum length difference for the two tracks of a differential pair.
{tools}

{hand_in}

## How it is judged

- Your program is run once on each of {n_scored} boards you have not seen. They are real boards of the same
  kind as the practice boards: {layers} copper layers and {nets_low} to {nets_high} nets to route.
{scoring}
- Your score is the average over the {n_scored} boards. It is 1.0 only if every board is complete.

## Budget

{agent_minutes} minutes of wall-clock time.
""",
}


def board_files(slug):
    task = json.loads((MADE / slug / "base" / "task.json").read_text())
    return task, [MADE / slug / "base" / (task["board"] + ext) for ext in (".kicad_pcb", ".kicad_pro", ".kicad_dru")
                  if (MADE / slug / "base" / (task["board"] + ext)).exists()]


def main(config):
    cfg = CONFIGS[config]
    out = ROOT / "tasks" / f"route-{config}"
    router = out / "solution/router/route.py"   # the reference solution, a real router for two-layer boards, is kept in the task
    if not router.exists():
        sys.exit(f"{router} is missing: the reference router lives in the task folder and is not rewritten here")
    for stale in ("environment", "tests"):
        if (out / stale).exists():
            shutil.rmtree(out / stale)
    for sub in ("environment/boards", "environment/boardcheck/boards", "tests/boards"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    for slug in dict.fromkeys(cfg["seen"] + cfg["run"]):
        task, files = board_files(slug)
        where = (["environment/boardcheck/boards", "environment/boards"] if slug in cfg["seen"] else []) + (["tests/boards"] if slug in cfg["run"] else [])
        for folder in where:
            (out / folder / slug).mkdir(parents=True, exist_ok=True)
            for path in files:
                shutil.copy(path, out / folder / slug / path.name)
            if folder != "environment/boards":  # what the checker needs to know about the board
                shutil.copy(MADE / slug / "base" / "task.json", out / folder / slug / "task.json")
    shutil.copy(HERE / "boardcheck.py", out / "environment/boardcheck/boardcheck.py")
    shutil.copy(HERE / "boardcheck.py", out / "tests/boardcheck.py")
    (out / "environment/boardcheck/ensure_output.sh").write_text(ENSURE)
    (out / "tests/hidden.json").write_text(json.dumps({"run": cfg["run"], "scored": cfg["scored"], "minutes_per_board": MINUTES_PER_BOARD}, indent=1))
    (out / "tests/run_hidden.py").write_text(RUN_HIDDEN)
    (out / "tests/test.sh").write_text(TEST_SH)
    (out / "tests/test.sh").chmod(0o755)
    (out / "tests/Dockerfile").write_text(TESTS_DOCKERFILE.format(base=BASE_IMAGE, kicad=KICAD_LAYER))
    (out / "environment/Dockerfile").write_text(ENV_DOCKERFILE.format(base=BASE_IMAGE, kicad=KICAD_LAYER))
    (out / "environment/.dockerignore").write_text("**/__pycache__\n**/*.pyc\n")
    (out / "solution/solve.sh").write_text(SOLVE)
    (out / "solution/solve.sh").chmod(0o755)
    verifier_timeout = len(cfg["run"]) * (MINUTES_PER_BOARD * 60 + 240) + 300
    (out / "task.toml").write_text(TASK_TOML.format(config=config, description=cfg["description"], verifier_timeout=verifier_timeout,
                                                    agent_timeout=AGENT_MINUTES * 60))
    scored = [board_files(slug)[0] for slug in cfg["scored"]]
    first = board_files(cfg["seen"][0])[0]
    words = {2: "two", 4: "four"}
    text = INSTRUCTIONS[config].format(
        tools=COMMON_TOOLS, hand_in=COMMON_HAND_IN, scoring=COMMON_SCORING.format(minutes=MINUTES_PER_BOARD),
        n_seen=len(cfg["seen"]), n_scored=len(cfg["scored"]), agent_minutes=AGENT_MINUTES, seen=cfg["seen"][0],
        nets_low=min(len(t["routable_nets"]) for t in scored), nets_high=max(len(t["routable_nets"]) for t in scored),
        layers=words.get(first["copper_layers"], first["copper_layers"]), parts=first["footprints"], nets=len(first["routable_nets"]),
        size=f"{first['size_mm'][0]:.0f} x {first['size_mm'][1]:.0f}")
    (out / "instruction.md").write_text(text)
    print(f"{out}: seen {cfg['seen']}, run {cfg['run']}, scored {cfg['scored']}, {MINUTES_PER_BOARD} min per board, "
          f"agent {AGENT_MINUTES} min, verifier timeout {verifier_timeout} s")


if __name__ == "__main__":
    for name in sys.argv[1:] or CONFIGS:
        main(name)
