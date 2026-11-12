#!/usr/bin/env python3
"""Shortcut submissions for a routing task, scored with the grader. Run inside the KiCad image.

Usage: route_policies.py <made dir with base/ and original/> [policy ...]

Policies:
  nothing    hand in the board as given
  straight   join the pads of every net with straight tracks (a minimum spanning tree), ignoring
             everything in the way; a via where two pads are on different sides
  stubs      a short track on every pad, connecting nothing
  original   the designer's routing (the reference solution)
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

BUILD = r"""
import json, math, sys, pcbnew
base_pcb, dst, mode, nets_json, width_mm, via_mm, drill_mm = sys.argv[1:8]
nets = set(json.loads(nets_json))
board = pcbnew.LoadBoard(base_pcb)
width, via_d, drill = (pcbnew.FromMM(float(v)) for v in (width_mm, via_mm, drill_mm))
copper = list(board.GetEnabledLayers().CuStack())
by_net = {}
for fp in board.GetFootprints():
    for pad in fp.Pads():
        if pad.GetNetname() in nets:
            layers = [l for l in copper if pad.IsOnLayer(l)]
            if layers:
                by_net.setdefault(pad.GetNetname(), []).append((pad.GetPosition().x, pad.GetPosition().y, layers))
def track(net, a, b, layer):
    t = pcbnew.PCB_TRACK(board); t.SetStart(pcbnew.VECTOR2I(*a)); t.SetEnd(pcbnew.VECTOR2I(*b))
    t.SetWidth(width); t.SetLayer(layer); t.SetNet(board.FindNet(net)); board.Add(t)
def via(net, at):
    v = pcbnew.PCB_VIA(board); v.SetPosition(pcbnew.VECTOR2I(*at)); v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetLayerPair(copper[0], copper[-1]); v.SetDrill(drill); v.SetWidth(pcbnew.F_Cu, via_d)
    v.SetNet(board.FindNet(net)); board.Add(v)
for net, pads in by_net.items():
    if mode == "stubs":
        for x, y, layers in pads:
            track(net, (x, y), (x + pcbnew.FromMM(0.3), y), layers[0])
        continue
    # Prim's algorithm: grow a tree from the first pad, always adding the nearest pad not yet in it
    done, todo = [pads[0]], pads[1:]
    while todo:
        d, a, b = min(((math.hypot(p[0] - q[0], p[1] - q[1]), p, q) for p in done for q in todo), key=lambda r: r[0])
        shared = [l for l in a[2] if l in b[2]]
        if shared:
            track(net, a[:2], b[:2], shared[0])
        else:  # pads on different sides: run on the first pad's side and drop a via on the second pad
            track(net, a[:2], b[:2], a[2][0]); via(net, b[:2])
        done.append(b); todo.remove(b)
pcbnew.SaveBoard(dst, board)
"""


def main(made, policies):
    with open(os.path.join(made, "base", "task.json")) as f:
        task = json.load(f)
    name = task["board"]
    base_pcb = os.path.join(made, "base", name + ".kicad_pcb")
    with open(os.path.join(made, "base", name + ".kicad_pro")) as f:
        pro = json.load(f)
    default = next((c for c in pro.get("net_settings", {}).get("classes", []) if c.get("name") == "Default"), {})
    width, via_d, drill = default.get("track_width", 0.25), default.get("via_diameter", 0.8), default.get("via_drill", 0.4)
    work = tempfile.mkdtemp()
    out = {}
    for policy in policies or ["nothing", "stubs", "straight", "original"]:
        if policy == "nothing":
            path = base_pcb
        elif policy == "original":
            path = os.path.join(made, "original", name + ".kicad_pcb")
        else:
            path = os.path.join(work, policy + ".kicad_pcb")
            subprocess.run([sys.executable, "-c", BUILD, base_pcb, path, policy, json.dumps(task["routable_nets"]), str(width), str(via_d), str(drill)],
                           capture_output=True, text=True)
        result = os.path.join(work, policy + ".result.json")
        env = dict(os.environ, BOARDCHECK_BASE=os.path.join(made, "base"), HOME=tempfile.mkdtemp(prefix="home-"))
        subprocess.run([sys.executable, os.path.join(HERE, "boardcheck.py"), path, "--json", result], capture_output=True, text=True, env=env)
        with open(result) as f:
            r = json.load(f)
        out[policy] = {k: r.get(k) for k in ("reward", "full_pass", "nets_good", "nets_open", "nets_with_violations", "other_nets_broken", "violations")}
    print(json.dumps({"board": os.path.basename(os.path.normpath(made)), "nets": len(task["routable_nets"]), "policies": out}))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
