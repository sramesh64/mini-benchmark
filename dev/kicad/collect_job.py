#!/usr/bin/env python3
"""Copy what is worth keeping from a Harbor job on a board-layout task into results/harbor/.

Usage: python3 dev/kicad/collect_job.py jobs/<job> [jobs/<job> ...]

Writes results/harbor/<job>/attempt-N/ for each attempt, numbered in the order of the trial folders'
names (the README uses the same order):

  run.json          task, model, harness, minutes, tokens, and how the run ended
  reward.json       what the verifier wrote
  report.md         the verifier's summary
  verifier/         per-board results and the program's own output
  trajectory.json   Harbor's trajectory of the run
  transcript.md     the same run as readable text
  router/           the program that was handed in

Nothing is copied from agent/sessions/, which holds the signed-in account's settings. Every file is
checked for keys, tokens and personal details before it is written, and the job stops if one is found.
The personal details are read from this machine when the script runs (home folder, git name and
e-mail, and the lines of .private-words, which git ignores); none is written in this file.
A path under /Users/ is let through only if the task's own published files already contain it (the
designers' boards carry the paths of their 3D models).
"""

import datetime
import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
KEYS = rb"sk-ant-[A-Za-z0-9_-]{8,}|sk-proj-[A-Za-z0-9_-]{8,}|dtn_[a-f0-9]{16,}|/Users/"
MAX_FILE_MB = 25


def secrets():
    """Every secret in .env.oauth and .env (the sign-in token and the API keys), to check that none is in
    anything that gets copied. Never printed."""
    found = []
    for name in (".env.oauth", ".env"):
        f = ROOT / name
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            line = line.strip().removeprefix("export ")
            if "=" in line and not line.startswith("#"):
                value = line.split("=", 1)[1].strip().strip("\"'")
                if len(value) >= 16:
                    found.append(value.encode())
    return found


def personal_words():
    """Words that must not reach the repository, gathered on this machine and never written down here:
    the home folder, the git name and e-mail (and their parts), and any line of .private-words,
    a file git ignores."""
    words = {str(pathlib.Path.home()), pathlib.Path.home().name}
    for key in ("user.name", "user.email"):
        for scope in ([], ["--global"]):
            value = subprocess.run(["git", "-C", str(ROOT), "config", *scope, "--get", key], capture_output=True, text=True).stdout.strip()
            if value:
                words.add(value)
                words.update(part for part in re.split(r"[@\s]", value) if len(part) >= 5)
    extra = ROOT / ".private-words"
    if extra.exists():
        words.update(line.strip() for line in extra.read_text().splitlines() if line.strip() and not line.startswith("#"))
    return [w for w in words if len(w) >= 5]


PRIVATE = re.compile(KEYS + b"".join(b"|(?i:" + re.escape(w.encode()) + b")" for w in sorted(personal_words())))


HOME_PATH = re.compile(rb"/Users/[^\s\"')\\]+")


def published_paths(task_dir):
    """Paths under /Users/ that are already in the task's own files, so not ours and not new."""
    found = set()
    own = str(pathlib.Path.home()).encode()
    for f in task_dir.rglob("*"):
        if f.is_file() and f.suffix in (".kicad_pcb", ".kicad_pro", ".kicad_sch", ".kicad_dru", ".json", ".md", ".txt"):
            found.update(m for m in HOME_PATH.findall(f.read_bytes()) if not m.startswith(own))
    return found


def private_text(data, secret, published):
    """The first private-looking thing in `data`, or None."""
    if any(value in data for value in secret):
        return "a sign-in token or an API key"
    for m in PRIVATE.finditer(data):
        if m.group(0) == b"/Users/":
            path = HOME_PATH.match(data, m.start())
            if path and path.group(0) in published:
                continue
        return "a key" if m.group(0)[:3] in (b"sk-", b"dtn") else ("a path under /Users/" if m.group(0) == b"/Users/" else "a personal word")
    return None


def safe_copy(src, dst, secret, published=frozenset()):
    data = src.read_bytes()
    found = private_text(data, secret, published)
    if found:
        sys.exit(f"private-looking text ({found}) in {src}; stopped without copying it")
    if len(data) > MAX_FILE_MB * 1e6:
        print(f"  left out {src.name}: {len(data) / 1e6:.0f} MB")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)


def minutes(span):
    if not span or not span.get("started_at") or not span.get("finished_at"):
        return None
    a, b = (datetime.datetime.fromisoformat(span[k].replace("Z", "+00:00")) for k in ("started_at", "finished_at"))
    return round((b - a).total_seconds() / 60, 1)


def run_facts(trial):
    """The facts about one attempt that the README needs, from Harbor's result and Claude Code's log."""
    d = json.loads((trial / "result.json").read_text())
    agent, used = d.get("agent_info") or {}, d.get("agent_result") or {}
    facts = {
        "task": d.get("task_name"), "trial": d.get("trial_name"),
        "model": (agent.get("model_info") or {}).get("name"), "harness": agent.get("name"), "harness_version": agent.get("version"),
        "agent_minutes": minutes(d.get("agent_execution")), "verifier_minutes": minutes(d.get("verifier")),
        "started_at": (d.get("agent_execution") or {}).get("started_at"),
        "input_tokens": used.get("n_input_tokens"), "cached_tokens": used.get("n_cache_tokens"), "output_tokens": used.get("n_output_tokens"),
        "api_price_estimate_usd": used.get("cost_usd"),
        "harbor_error": (d.get("exception_info") or {}).get("exception_type"),
    }
    log = trial / "agent" / "claude-code.txt"
    tool_calls = clock_reads = 0
    if log.exists():
        for line in log.read_text(errors="replace").splitlines():
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "system" and e.get("subtype") == "init":
                facts["harness_version"] = facts["harness_version"] or e.get("claude_code_version")
                facts["signed_in_with"] = "API key" if e.get("apiKeySource") not in (None, "none") else "OAuth token"
            elif e.get("type") == "assistant":
                for block in (e.get("message") or {}).get("content", []):
                    if block.get("type") == "tool_use":
                        tool_calls += 1
                        command = (block.get("input") or {}).get("command", "")
                        if block.get("name") == "Bash" and re.search(r"(^|[\s;&|(])date\b", command):
                            clock_reads += 1
            elif e.get("type") == "result":
                # A log has a second result when a leftover background command woke the agent after it had finished.
                facts["ended"] = e.get("terminal_reason") or e.get("subtype")
                facts["turns"] = facts.get("turns", 0) + (e.get("num_turns") or 0)
    facts["tool_calls"], facts["clock_reads"] = tool_calls, clock_reads
    return facts


def collect(job):
    job = pathlib.Path(job)
    dest = ROOT / "results" / "harbor" / job.name
    secret = secrets()
    published = None
    trials = sorted(p for p in job.iterdir() if p.is_dir() and (p / "result.json").exists())
    for n, trial in enumerate(trials, 1):
        out = dest / f"attempt-{n}"
        kept = None
        if out.exists():
            if (out / "practice").is_dir():  # written by score_on_practice.sh, not part of the Harbor job
                kept = dest / f".practice-{n}"
                shutil.move(str(out / "practice"), str(kept))
            shutil.rmtree(out)
        out.mkdir(parents=True)
        if kept:
            shutil.move(str(kept), str(out / "practice"))
        facts = run_facts(trial)
        if published is None:
            published = published_paths(ROOT / "tasks" / str(facts["task"]).split("/")[-1])
        (out / "run.json").write_text(json.dumps(facts, indent=1) + "\n")
        for name in ("reward.json", "report.md"):
            if (trial / "verifier" / name).exists():
                safe_copy(trial / "verifier" / name, out / name, secret, published)
        for f in sorted((trial / "verifier").glob("*")):
            if f.is_file() and f.name not in ("reward.json", "report.md", "test-stdout.txt") and f.stat().st_size:
                safe_copy(f, out / "verifier" / f.name, secret, published)
        if (trial / "agent" / "trajectory.json").exists():
            safe_copy(trial / "agent" / "trajectory.json", out / "trajectory.json", secret, published)
        work = trial / "artifacts" / "home" / "agent" / "work"
        if (work / "router").is_dir():
            for f in sorted((work / "router").rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts:
                    safe_copy(f, out / "router" / f.relative_to(work / "router"), secret, published)
        if (work / "routed.kicad_pcb").exists():
            safe_copy(work / "routed.kicad_pcb", out / "routed.kicad_pcb", secret, published)
        done = subprocess.run([sys.executable, str(ROOT / "dev" / "kicad" / "render_transcript.py"), str(trial), "--out", str(out / "transcript.md")],
                              capture_output=True, text=True)
        if done.returncode != 0:
            print("  transcript failed:", done.stderr[-300:])
        else:
            found = private_text((out / "transcript.md").read_bytes(), secret, published)
            if found:
                (out / "transcript.md").unlink()
                sys.exit(f"private-looking text ({found}) in the transcript of {trial}; removed it and stopped")
        reward = json.loads((out / "reward.json").read_text()) if (out / "reward.json").exists() else {}
        print(f"  attempt-{n}  {trial.name}  {facts['model']}  score {reward.get('reward')}  {facts['agent_minutes']} min  "
              f"ended: {facts.get('ended')}  clock reads: {clock_reads_text(facts)}")
    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) / 1e6 if dest.exists() else 0
    print(f"{dest.relative_to(ROOT)}: {len(trials)} attempts, {size:.1f} MB")


def clock_reads_text(facts):
    return facts["clock_reads"] if facts["clock_reads"] else "none"


if __name__ == "__main__":
    for job in sys.argv[1:]:
        collect(job)
