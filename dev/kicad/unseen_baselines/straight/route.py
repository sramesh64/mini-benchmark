#!/usr/bin/env python3
"""Shortcut "router": joins the pads of every net with straight tracks, ignoring everything in the way."""
import json, math, os, sys, pcbnew
board_path, out_path = sys.argv[1], sys.argv[2]
board = pcbnew.LoadBoard(board_path)
try:
    with open(os.path.splitext(board_path)[0] + ".kicad_pro") as f:
        default = next(c for c in json.load(f)["net_settings"]["classes"] if c.get("name") == "Default")
except Exception:
    default = {}
width = pcbnew.FromMM(default.get("track_width", 0.25))
via_d, drill = pcbnew.FromMM(default.get("via_diameter", 0.8)), pcbnew.FromMM(default.get("via_drill", 0.4))
copper = list(board.GetEnabledLayers().CuStack())
by_net = {}
for fp in board.GetFootprints():
    for pad in fp.Pads():
        layers = [l for l in copper if pad.IsOnLayer(l)]
        if pad.GetNetname() and layers:
            by_net.setdefault(pad.GetNetname(), []).append((pad.GetPosition().x, pad.GetPosition().y, layers))
for net, pads in by_net.items():
    done, todo = [pads[0]], pads[1:]
    while todo:
        d, a, b = min(((math.hypot(p[0] - q[0], p[1] - q[1]), p, q) for p in done for q in todo), key=lambda r: r[0])
        shared = [l for l in a[2] if l in b[2]]
        t = pcbnew.PCB_TRACK(board); t.SetStart(pcbnew.VECTOR2I(a[0], a[1])); t.SetEnd(pcbnew.VECTOR2I(b[0], b[1]))
        t.SetWidth(width); t.SetLayer(shared[0] if shared else a[2][0]); t.SetNet(board.FindNet(net)); board.Add(t)
        if not shared:
            v = pcbnew.PCB_VIA(board); v.SetPosition(pcbnew.VECTOR2I(b[0], b[1])); v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetLayerPair(copper[0], copper[-1]); v.SetDrill(drill); v.SetWidth(pcbnew.F_Cu, via_d); v.SetNet(board.FindNet(net)); board.Add(v)
        done.append(b); todo.remove(b)
pcbnew.SaveBoard(out_path, board)
