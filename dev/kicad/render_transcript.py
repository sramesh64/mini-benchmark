"""Turn a Harbor trial into a readable markdown transcript. dev/kicad/collect_job.py calls it.

Usage: python3 dev/kicad/render_transcript.py <trial-dir> [--out file.md] [--max-output 4000]

Reads agent/claude-code.txt (Claude Code's stream-json log) and the verifier's reward.json.
"""

import argparse
import json
import pathlib


def clip(text, limit):
    text = text if isinstance(text, str) else json.dumps(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters]"


def tool_result_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def render(trial, max_output):
    trial = pathlib.Path(trial)
    out = [f"# Transcript: {trial.name}", ""]
    reward_path = trial / "verifier" / "reward.json"
    if reward_path.exists():
        r = json.loads(reward_path.read_text())
        if "board_checks_passed" not in r:  # a task other than bringup-sensor-node: show its own score fields
            fields = ", ".join(f"{k} {v}" for k, v in sorted(r.items()) if k not in ("reward", "full_pass"))
            out += [f"**Score {r.get('reward')}** (full pass: {'yes' if r.get('full_pass') else 'no'}). {fields}.", ""]
            r = None
    if reward_path.exists() and r is not None:
        out += [
            f"**Score {r.get('reward')}** (full pass: {'yes' if r.get('full_pass') else 'no'}). "
            f"Board {r.get('board_checks_passed')}/5, design {r.get('design_checks_passed')}/5, "
            f"problems reported {r.get('problems_reported_correctly')}/5, parts destroyed {r.get('parts_destroyed')}, "
            f"bench time {r.get('minutes_used')} of 300 min in {r.get('commands')} commands.",
            "",
        ]
    log = trial / "agent" / "claude-code.txt"
    turn = 0
    for line in log.read_text().splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            out += [f"Model `{obj.get('model')}`, Claude Code {obj.get('claude_code_version')}.", ""]
        elif kind == "assistant":
            for block in obj["message"].get("content", []):
                btype = block.get("type")
                if btype == "thinking" and block.get("thinking"):
                    out += ["**Thinking**", "", "> " + clip(block["thinking"], max_output).replace("\n", "\n> "), ""]
                elif btype == "text" and block.get("text", "").strip():
                    out += ["**Agent**", "", block["text"].strip(), ""]
                elif btype == "tool_use":
                    turn += 1
                    data = block.get("input", {})
                    body = data.get("command") or data.get("file_path") or json.dumps(data)
                    extra = ""
                    if block.get("name") == "Write" and "content" in data:
                        extra = "\n" + clip(data["content"], max_output)
                    out += [f"**Tool call {turn}: {block.get('name')}**", "", "```", clip(body, max_output) + extra, "```", ""]
        elif kind == "user":
            content = obj["message"].get("content")
            if isinstance(content, str):
                continue
            for block in content:
                if block.get("type") == "tool_result":
                    text = tool_result_text(block.get("content"))
                    out += ["Output:", "", "```", clip(text, max_output), "```", ""]
        elif kind == "result":
            usage = obj.get("usage", {})
            out += [
                "---",
                "",
                f"Finished: {obj.get('num_turns')} turns, {obj.get('duration_ms', 0) / 1000:.0f} s, "
                f"{usage.get('output_tokens')} output tokens, cost ${obj.get('total_cost_usd', 0):.2f}.",
                "",
            ]
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("trial")
    parser.add_argument("--out")
    parser.add_argument("--max-output", type=int, default=4000)
    args = parser.parse_args()
    text = render(args.trial, args.max_output)
    if args.out:
        pathlib.Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(text)} characters)")
    else:
        print(text)


if __name__ == "__main__":
    main()
