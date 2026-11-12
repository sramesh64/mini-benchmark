#!/usr/bin/env python3
"""Which nets fail on the unseen boards, across every collected attempt of `route-unseen-boards`.

Usage, from the repository root (needs KiCad's Python module, so it runs in the KiCad image):

    docker run --rm --network none -v "$PWD":/repo:ro -w /repo kicad-dev \
        python3 dev/kicad/analyse_unseen.py > results/which-nets-fail.json

Reads  tasks/route-unseen-boards/tests/boards/<board>/        the scored boards and their rules
       results/harbor/route-unseen-boards-*/attempt-*/verifier/<board>.json   what the grader found
Prints JSON: for each board, its nets in three groups, with how often a net of that group failed.

The groups, in this order (a net is in the first one it fits):
  many_pads   eight or more pads: the supply and ground nets
  tight_pad   the track has to leave a surface-mount pad that is at most 0.1 mm wider than the
              narrowest track the rules allow on that net
  other       everything else
"""

import collections
import glob
import json
import os
import sys

import pcbnew

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOARDS = os.path.join(ROOT, "tasks", "route-unseen-boards", "tests", "boards")
MANY_PADS = 8
TIGHT_MM = 0.1


def groups_of(board_dir):
    with open(os.path.join(board_dir, "task.json")) as f:
        task = json.load(f)
    board = pcbnew.LoadBoard(glob.glob(os.path.join(board_dir, "*.kicad_pcb"))[0])
    pads = collections.defaultdict(list)
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname():
                size = pad.GetSize(pcbnew.F_Cu) if pad.IsOnLayer(pcbnew.F_Cu) else pad.GetSize(pcbnew.B_Cu)
                pads[pad.GetNetname()].append((pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD, pcbnew.ToMM(min(size.x, size.y))))
    out = {}
    for net in task["routable_nets"]:
        width = max(task["net_min_track_width_mm"].get(net, 0), task["board_min_track_width_mm"])
        narrow = [w for smd, w in pads[net] if smd]
        if len(pads[net]) >= MANY_PADS:
            out[net] = "many_pads"
        elif narrow and min(narrow) - width <= TIGHT_MM + 1e-6:
            out[net] = "tight_pad"
        else:
            out[net] = "other"
    return out


def main():
    result = {}
    for board_dir in sorted(glob.glob(os.path.join(BOARDS, "*"))):
        name = os.path.basename(board_dir)
        if not os.path.exists(os.path.join(board_dir, "task.json")):
            continue
        group = groups_of(board_dir)
        reports = sorted(glob.glob(os.path.join(ROOT, "results", "harbor", "route-unseen-boards-*", "attempt-*", "verifier", name + ".json")))
        failed = collections.Counter()
        for path in reports:
            with open(path) as f:
                detail = json.load(f)["detail"]
            bad = set(detail["open_nets"]) | {net for v in detail["violations"] for net in v["nets"]}
            failed.update(net for net in bad if net in group)
        table = {}
        for kind in ("many_pads", "tight_pad", "other"):
            nets = [net for net, g in group.items() if g == kind]
            table[kind] = {"nets": len(nets), "failures": sum(failed[net] for net in nets), "net_runs": len(nets) * len(reports)}
        result[name] = {"attempts": len(reports), "groups": table,
                        "fail_in_every_attempt": sorted(net for net in group if reports and failed[net] == len(reports))}
    json.dump(result, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
