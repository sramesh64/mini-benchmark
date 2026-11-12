#!/usr/bin/env python3
"""Checks of the routing grader, run inside the KiCad image.

Usage: test_boardcheck.py <single-board task directory, with tests/base/ and solution/>
       test_boardcheck.py <folder made by make_route_task.py, with base/ and original/>
Builds test submissions from the designer's routing and checks what boardcheck says about each.
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

VARIANT = r"""
import json, sys, pcbnew
base_pcb, orig_pcb, dst, mode = sys.argv[1:5]
opts = json.loads(sys.argv[5])
skip, thin, twice = set(opts.get("skip", [])), set(opts.get("thin", [])), set(opts.get("twice", []))
base = pcbnew.LoadBoard(base_pcb)
orig = pcbnew.LoadBoard(orig_pcb)
free = set(opts.get("free", []))  # a place-and-route task: these parts are parked beside the board as given
if free and mode != "unplaced":   # put them where the designer put them
    theirs = {fp.GetReference(): fp for fp in orig.GetFootprints()}
    mine = {fp.GetReference(): fp for fp in base.GetFootprints() if fp.GetReference() in free}
    two_pad = sorted(r for r, fp in mine.items() if len(list(fp.Pads())) == 2)
    def nets(ref):
        return {p.GetNetname() for p in mine[ref].Pads()}
    def kind(ref):
        return (mine[ref].GetFPID().GetUniStringLibItemName(), theirs[ref].GetLayer())
    pair = next(((a, b) for a in two_pad for b in two_pad if a < b and kind(a) == kind(b) and not (nets(a) & nets(b))), None)
    for ref, fp in mine.items():
        src = theirs[ref]
        if mode == "swap" and pair and ref in pair:      # two parts of the same kind in each other's place
            src = theirs[pair[1] if ref == pair[0] else pair[0]]
        if mode == "stack" and pair and ref == pair[0]:  # one part put on top of another
            src = theirs[pair[1]]
        if mode == "parked" and ref == sorted(mine)[0]:  # one part never placed
            continue
        if src.GetLayer() != fp.GetLayer():
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        fp.SetOrientation(src.GetOrientation())
        fp.SetPosition(src.GetPosition())
for item in orig.GetTracks():
    name = item.GetNetname()
    if name in skip or mode == "dots":
        continue
    kind = item.GetClass()
    if (mode == "viasonly" and kind != "PCB_VIA") or (mode == "tracksonly" and kind == "PCB_VIA"):
        continue
    if kind == "PCB_VIA":
        new = pcbnew.PCB_VIA(base); new.SetPosition(item.GetPosition()); new.SetViaType(item.GetViaType())
        new.SetLayerPair(item.TopLayer(), item.BottomLayer()); new.SetDrill(item.GetDrillValue())
        new.SetWidth(pcbnew.F_Cu, item.GetWidth(pcbnew.F_Cu))
        if mode == "blind":  # every via declared blind or buried
            new.SetViaType(pcbnew.VIATYPE_BLIND_BURIED)
    else:
        new = pcbnew.PCB_ARC(base) if kind == "PCB_ARC" else pcbnew.PCB_TRACK(base)
        new.SetStart(item.GetStart()); new.SetEnd(item.GetEnd())
        if kind == "PCB_ARC":
            new.SetMid(item.GetMid())
        new.SetWidth(item.GetWidth()); new.SetLayer(item.GetLayer())
        if mode == "silk":  # every track on a layer that is not copper
            new.SetLayer(pcbnew.F_SilkS)
        if name in thin:  # this net's tracks at the board-wide minimum width
            new.SetWidth(pcbnew.FromMM(opts["floor_mm"]))
    new.SetNet(base.FindNet(name)); base.Add(new)
    if name in twice and kind != "PCB_VIA":  # the same track laid a second time on top: the net's copper is now twice as long
        again = pcbnew.PCB_ARC(base) if kind == "PCB_ARC" else pcbnew.PCB_TRACK(base)
        again.SetStart(item.GetStart()); again.SetEnd(item.GetEnd())
        if kind == "PCB_ARC":
            again.SetMid(item.GetMid())
        again.SetWidth(item.GetWidth()); again.SetLayer(item.GetLayer()); again.SetNet(base.FindNet(name)); base.Add(again)
pads = [p for f in base.GetFootprints() for p in f.Pads() if p.GetNetname()]
if mode == "stub":  # a dead-end track on one net: from one of its pads into the clearest space nearby
    import math
    copper = list(base.GetEnabledLayers().CuStack())
    segments = [(t.GetStart().x, t.GetStart().y, t.GetEnd().x, t.GetEnd().y, t.GetWidth() / 2, None if t.GetClass() == "PCB_VIA" else t.GetLayer())
                for t in base.GetTracks()]
    def far_from_copper(x, y, layer, own):
        best = 1e18
        for ax, ay, bx, by, half, lay in segments:
            if lay is not None and lay != layer:
                continue
            dx, dy = bx - ax, by - ay
            k = 0 if dx == dy == 0 else max(0, min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
            best = min(best, math.hypot(x - ax - k * dx, y - ay - k * dy) - half)
        for p in pads:
            if p is not own and p.IsOnLayer(layer):
                box = p.GetBoundingBox()
                best = min(best, math.hypot(x - p.GetPosition().x, y - p.GetPosition().y) - max(box.GetWidth(), box.GetHeight()) / 2)
        return best
    choice = None
    for start in [p for p in pads if p.GetNetname() == opts["stub"]][:8]:
        layer = next(l for l in copper if start.IsOnLayer(l))
        for k in range(24):
            for length in (0.8, 1.2, 1.6):
                ux, uy = math.cos(k * math.pi / 12), math.sin(k * math.pi / 12)
                points = [(start.GetPosition().x + pcbnew.FromMM(length) * f * ux, start.GetPosition().y + pcbnew.FromMM(length) * f * uy) for f in (0.5, 0.75, 1.0)]
                room = min(far_from_copper(x, y, layer, start) for x, y in points)
                if choice is None or room > choice[0]:
                    choice = (room, start, layer, points[-1])
    room, start, layer, tip = choice
    t = pcbnew.PCB_TRACK(base); t.SetStart(start.GetPosition()); t.SetEnd(pcbnew.VECTOR2I(int(tip[0]), int(tip[1])))
    t.SetWidth(pcbnew.FromMM(opts["floor_mm"])); t.SetLayer(layer); t.SetNet(start.GetNet()); base.Add(t)
    print("stub room mm:", round(pcbnew.ToMM(int(room)), 3))
if mode == "short":  # one track laid straight across the board between pads of two different nets
    a = pads[0]
    b = next(p for p in pads[len(pads) // 2:] if p.GetNetname() != a.GetNetname())
    t = pcbnew.PCB_TRACK(base); t.SetStart(a.GetPosition()); t.SetEnd(b.GetPosition()); t.SetWidth(pcbnew.FromMM(0.3))
    t.SetLayer(pcbnew.F_Cu); t.SetNet(a.GetNet()); base.Add(t)
if mode == "dots":  # a speck of copper in the middle of every pad, connecting nothing
    copper = list(base.GetEnabledLayers().CuStack())
    for p in pads:
        layer = next((l for l in copper if p.IsOnLayer(l)), None)
        if layer is None:
            continue
        t = pcbnew.PCB_TRACK(base); t.SetStart(p.GetPosition()); t.SetEnd(pcbnew.VECTOR2I(p.GetPosition().x + pcbnew.FromMM(0.01), p.GetPosition().y))
        t.SetWidth(pcbnew.FromMM(opts["floor_mm"])); t.SetLayer(layer); t.SetNet(p.GetNet()); base.Add(t)
if mode == "moved":  # the routing is the original's, but the file also moves a part
    # Far off the board: KiCad gives a via the net of a pad that is saved on top of it, so a part
    # moved onto the routing would change the routing itself, which is not what this checks.
    fp = next(f for f in base.GetFootprints() if f.GetReference() not in free)
    fp.SetPosition(pcbnew.VECTOR2I(fp.GetPosition().x + pcbnew.FromMM(600), fp.GetPosition().y))
pcbnew.SaveBoard(dst, base)
"""


def run_check(base_dir, path):
    out = os.path.join(tempfile.mkdtemp(prefix="result-"), "result.json")
    # A fresh HOME each time: KiCad behaves differently the first time it runs for a user (it writes a
    # default project file beside a board it saves), and the verifier's container is always fresh.
    env = dict(os.environ, BOARDCHECK_BASE=base_dir, HOME=tempfile.mkdtemp(prefix="home-"))
    done = subprocess.run([sys.executable, os.path.join(HERE, "boardcheck.py"), path, "--json", out], capture_output=True, text=True, env=env)
    with open(out) as f:
        LAST.update(result=json.load(f), stderr=done.stderr[-300:])
    return LAST["result"]


LAST = {}  # the most recent result, shown when a check fails


def main(where):
    # Either a packaged task (the unrouted board is in tests/base, the designer's routing in solution/)
    # or the folder make_route_task.py writes (base/ and original/).
    packaged = os.path.isdir(os.path.join(where, "tests", "base"))
    made = os.path.join(where, "tests", "base") if packaged else os.path.join(where, "base")
    with open(os.path.join(made, "task.json")) as f:
        task = json.load(f)
    name, nets = task["board"], task["routable_nets"]
    base_pcb = os.path.join(made, name + ".kicad_pcb")
    orig_pcb = os.path.join(where, "solution" if packaged else "original", name + ".kicad_pcb")
    work = tempfile.mkdtemp()

    free = task.get("free_footprints", [])

    def variant(mode, **opts):
        opts["free"] = free
        dst = os.path.join(work, mode + ".kicad_pcb")
        subprocess.run([sys.executable, "-c", VARIANT, base_pcb, orig_pcb, dst, mode, json.dumps(opts)], capture_output=True, text=True)
        assert os.path.exists(dst), mode
        return run_check(made, dst)

    checks = []

    def expect(label, ok, shown):
        checks.append(ok)
        print(("ok   " if ok else "FAIL ") + label + ": " + shown, flush=True)
        if not ok:  # what the last check actually returned
            r = LAST.get("result", {})
            d = r.get("detail", {})
            print("     last result:", {k: v for k, v in r.items() if k != "detail"})
            print("     open:", d.get("open_nets", [])[:8], "| violations:", [(v["type"], v["nets"][:2]) for v in d.get("violations", [])[:5]],
                  "| notes:", d.get("problems", [])[:3], "| stderr:", LAST.get("stderr", "")[-200:])

    r = run_check(made, base_pcb)
    expect("the unrouted board scores 0", r["reward"] == 0.0 and r["nets_open"] == len(nets), f"reward {r['reward']}, open {r['nets_open']}/{len(nets)}")
    r = run_check(made, orig_pcb)
    expect("the original routing is complete and clean", r["reward"] == 1.0 and r["full_pass"] == 1 and r["violations"] == 0,
           f"reward {r['reward']}, violations {r['violations']}")
    skip = nets[:10]
    r = variant("partial", skip=skip)
    expect("ten nets left out", r["nets_open"] == 10 and abs(r["reward"] - (len(nets) - 10) / len(nets)) < 1e-3 and r["full_pass"] == 0,
           f"reward {r['reward']}, open {r['nets_open']}")
    r = variant("short")
    expect("a track across other nets is caught", r["violations"] > 0 and r["full_pass"] == 0 and r["reward"] < 1.0,
           f"reward {r['reward']}, violations {r['violations']}, nets with violations {r['nets_with_violations']}")
    r = variant("moved")
    expect("moving a part that is not the agent's to place changes nothing" if free else "moving a part in the file changes nothing",
           r["reward"] == 1.0 and r["full_pass"] == 1, f"reward {r['reward']}")
    if free:  # place-and-route: the parts are parked beside the board as given, and their placing is graded
        def kinds(result):
            return sorted({v["type"] for v in result["detail"]["violations"]})
        r = variant("unplaced")  # nets that run only between fixed parts still count; every net of a parked part must not
        expect("the designer's tracks with every part left parked", r["reward"] < 1.0 and r["placement_violations"] >= len(free) and r["full_pass"] == 0,
               f"reward {r['reward']}, placement violations {r['placement_violations']} for {len(free)} parts")
        r = variant("parked")
        expect("one part left parked", r["reward"] < 1.0 and r["full_pass"] == 0 and "part_off_board" in kinds(r),
               f"reward {r['reward']}, kinds {kinds(r)}")
        r = variant("swap")
        expect("two parts of the same kind in each other's place", r["reward"] < 1.0 and r["full_pass"] == 0,
               f"reward {r['reward']}, open {r['nets_open']}, kinds {kinds(r)}")
        r = variant("stack")
        expect("one part put on top of another", r["reward"] < 1.0 and r["full_pass"] == 0 and (r["placement_violations"] > 0 or r["violations"] > 0),
               f"reward {r['reward']}, placement violations {r['placement_violations']}, kinds {kinds(r)}")
    r = variant("silk")
    v = variant("viasonly")  # on a board with copper planes, vias alone connect a few nets
    expect("tracks on a non-copper layer are not read: same as handing in the vias alone",
           r["reward"] == v["reward"] and r["tracks_and_vias"] == v["tracks_and_vias"] <= task["original_vias"] and r["full_pass"] == 0,
           f"reward {r['reward']} (vias alone {v['reward']}), items read {r['tracks_and_vias']}")
    widths = task.get("net_min_track_width_mm", {})
    if widths:  # the net that must be widest, routed at the board-wide minimum instead
        net = max(sorted(widths), key=lambda n: widths[n])
        r = variant("thin", thin=[net], floor_mm=max(task["board_min_track_width_mm"], 0.1))
        types = {v["type"] for v in r["detail"]["violations"]}
        expect(f"a {widths[net]:g} mm net routed at the board minimum is caught", "track_width" in types and r["full_pass"] == 0 and r["reward"] < 1.0,
               f"reward {r['reward']}, violation types {sorted(types)}")
        star = [n for n in widths if "*" in n or "?" in n or "{" in n]
        if star:  # the same for a net whose name has characters KiCad's rule language treats specially
            r = variant("thin", thin=star[:1], floor_mm=max(task["board_min_track_width_mm"], 0.1))
            hit = {n for v in r["detail"]["violations"] if v["type"] == "track_width" for n in v["nets"]}
            expect("the same for a net with an unusual name", hit == set(star[:1]), f"nets flagged {sorted(hit)} for {star[:1]}")
    for pair, (plus, minus) in sorted(task.get("pair_nets", {}).items())[:1]:
        if pair in task.get("pair_max_skew_mm", {}):  # one track of a differential pair made much longer than the other
            r = variant("twice", twice=[plus])
            hit = {n for v in r["detail"]["violations"] if v["type"] == "skew_out_of_range" for n in v["nets"]}
            expect(f"a differential pair with one track far longer than the other is caught (limit {task['pair_max_skew_mm'][pair]} mm)",
                   bool(hit & {plus, minus}) and r["full_pass"] == 0, f"reward {r['reward']}, nets flagged for skew {sorted(hit)}")
    for net in task.get("no_dead_end_nets", [])[:1]:  # length made up with a stub that leads nowhere
        r = variant("stub", stub=net, floor_mm=max(task["board_min_track_width_mm"], 0.1))
        flagged = {n for v in r["detail"]["violations"] if v["type"] == "track_dangling" for n in v["nets"]}
        caught = {n for v in r["detail"]["violations"] for n in v["nets"]}
        expect("a dead-end track on a length-matched net is caught", net in caught and r["full_pass"] == 0,
               f"reward {r['reward']}, flagged as a dead end {sorted(flagged)}, in a violation of any kind {sorted(caught)[:3]}")
    if task["original_vias"]:
        r = variant("blind")
        v = variant("tracksonly")
        expect("blind or buried vias are not read: same as handing in the tracks alone",
               r["reward"] == v["reward"] and r["tracks_and_vias"] == v["tracks_and_vias"] <= task["original_tracks"] and r["full_pass"] == 0,
               f"reward {r['reward']} (tracks alone {v['reward']}), items read {r['tracks_and_vias']}")
    r = variant("dots", floor_mm=max(task["board_min_track_width_mm"], 0.1))
    # With the parts placed as the designer placed them, a copper pour can join a net or two by itself.
    expect("a speck of copper on every pad connects nothing", (r["reward"] < 0.1 if free else r["reward"] == 0.0 and r["nets_good"] == 0)
           and r["tracks_and_vias"] > len(nets), f"reward {r['reward']}, good {r['nets_good']}, open {r['nets_open']}, items read {r['tracks_and_vias']}")
    # Straight lines between pads: hundreds of violations, so KiCad's list is cut short. Whatever still
    # counts must count on its own: hand in the copper of those nets alone and get the same nets, clean.
    import route_policies
    with open(os.path.join(made, name + ".kicad_pro")) as f:
        default = next((c for c in json.load(f).get("net_settings", {}).get("classes", []) if c.get("name") == "Default"), {})
    sizes = [str(default.get(k, d)) for k, d in (("track_width", 0.25), ("via_diameter", 0.8), ("via_drill", 0.4))]

    def straight(tag, which):
        dst = os.path.join(work, tag + ".kicad_pcb")
        subprocess.run([sys.executable, "-c", route_policies.BUILD, base_pcb, dst, "straight", json.dumps(sorted(which))] + sizes, capture_output=True, text=True)
        return run_check(made, dst)

    r = straight("straight", nets)
    kept = r["detail"]["good_nets"]
    alone = straight("straight-kept", kept) if kept else {"detail": {"good_nets": []}, "violations": 0}
    expect("straight lines between pads: the nets that count also count on their own",
           r["full_pass"] == 0 and r["reward"] < 0.5 and alone["detail"]["good_nets"] == kept and alone["violations"] == 0,
           f"reward {r['reward']} after {r['rounds']} rounds, {len(kept)} nets count; alone: {len(alone['detail']['good_nets'])} count, {alone['violations']} violations")
    junk = os.path.join(work, "junk.kicad_pcb")
    with open(junk, "w") as f:
        f.write("this is not a board\n")
    r = run_check(made, junk)
    expect("a file KiCad cannot load scores 0", r["reward"] == 0.0 and "error" in r, r.get("error", "")[:80])
    print(f"{sum(checks)} of {len(checks)} checks passed")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
