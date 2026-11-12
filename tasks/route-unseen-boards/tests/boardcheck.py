#!/usr/bin/env python3
"""Check a routed board against the unrouted board it started from, with KiCad's own design-rule check.

Usage: boardcheck <routed.kicad_pcb> [--json out.json] [--keep graded.kicad_pcb]

Only tracks, arcs and through vias are taken from the routed file. They are copied onto a pristine copy
of the unrouted board, so nothing else in the routed file (footprints, pads, nets, zones, outline, rules)
can affect the result. Zones are refilled, KiCad's DRC is run with the board's own rules (the project
file and the rules file beside the pristine board), and each net is counted as good if it is fully
connected and none of its new copper is part of a rule violation.

KiCad stops listing one kind of violation after 199 entries (499 for clearance) and missing connections
after 499. A net is never counted good just because the list stopped:
  - violations cut short: the nets found bad so far are taken off the board and the rest is checked
    again, until the list is complete. What counts is then exactly what would count if only the
    surviving nets' copper had been handed in.
  - missing connections cut short: every net that has new copper and is not on the list is tested on
    its own with KiCad's connectivity engine.

The same script runs in the agent's environment (so the agent can check its own work) and in the
verifier.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.environ.get("BOARDCHECK_BASE", os.path.join(HERE, "base"))  # pristine unrouted board + project file
MAX_ITEMS = 60000

# Violations that count. Each is an "error" in KiCad's default severities and is about copper that a
# router places, including the length-matching rules in a board's rules file. Rules about silkscreen,
# courtyards and zone thermals do not count. Nor does the gap between the tracks of a differential pair,
# which KiCad takes from a net-class default that the designers' own boards do not always keep.
COUNTED = {
    "clearance", "shorting_items", "tracks_crossing", "track_width", "via_diameter", "annular_width",
    "drill_out_of_range", "hole_clearance", "hole_to_hole", "holes_co_located", "copper_edge_clearance",
    "items_not_allowed", "microvia_drill_out_of_range", "padstack", "track_angle", "track_segment_length",
    "via_dangling", "creepage", "connection_width", "copper_sliver", "solder_mask_bridge",
    "skew_out_of_range", "length_out_of_range",
}


MERGE = r"""
import json, re, sys, pcbnew
ESCAPES = {"dblquote": '"', "quote": "'", "lt": "<", "gt": ">", "backslash": "\\", "slash": "/", "bar": "|",
           "comma": ",", "colon": ":", "space": " ", "dollar": "$", "tab": "\t", "return": "\n", "brace": "{"}
def unescape(name):  # KiCad stores e.g. a '/' inside a label as {slash}; its reports print the plain character
    return re.sub(r"\{([a-z]+)\}", lambda m: ESCAPES.get(m.group(1), m.group(0)), name)
routed_path, base_pcb, out_path, owners_path, max_items, leave_off_path = sys.argv[1:7]
options = json.load(open(leave_off_path))
leave_off = set(options["leave_off"])  # nets whose copper is left off the board this round
free = set(options["free"])            # parts whose place is read from the routed file (place-and-route tasks)
base = pcbnew.LoadBoard(base_pcb)
routed = pcbnew.LoadBoard(routed_path)
problems, owners = [], {}
parts, off_board = {}, {}
if free:
    # Position, rotation and side of each free part come from the routed file, by reference. The part
    # itself, with its pads and nets, is the one on the pristine board.
    theirs = {}
    for fp in routed.GetFootprints():
        theirs.setdefault(fp.GetReference(), []).append(fp)
    outline = pcbnew.SHAPE_POLY_SET()
    base.GetBoardPolygonOutlines(outline)
    for fp in base.GetFootprints():
        ref = fp.GetReference()
        if ref not in free:
            continue
        found = theirs.get(ref, [])
        if len(found) != 1:
            problems.append(f"part {ref} is {'not in' if not found else 'more than once in'} the file; left where it was parked")
        else:
            if found[0].GetLayer() != fp.GetLayer():
                fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
            fp.SetOrientation(found[0].GetOrientation())
            fp.SetPosition(found[0].GetPosition())
        nets = sorted({p.GetNetname() for p in fp.Pads() if p.GetNetname()})
        parts[fp.m_Uuid.AsString()] = nets
        for pad in fp.Pads():
            parts[pad.m_Uuid.AsString()] = [pad.GetNetname()] if pad.GetNetname() else nets
        if outline.OutlineCount() and not all(outline.Contains(pad.GetPosition()) for pad in fp.Pads()):
            off_board[ref] = nets
copper = {base.GetLayerName(layer) for layer in base.GetEnabledLayers().CuStack()}
items = list(routed.GetTracks())
if len(items) > int(max_items):
    json.dump({"error": f"too many tracks and vias ({len(items)}; the limit is {max_items})"}, open(owners_path, "w"))
    sys.exit(0)
for item in items:
    name = item.GetNetname()
    net = base.FindNet(name) if name else None
    if net is None:
        problems.append(f"an item is on net {name!r}, which the board does not have; skipped")
        continue
    if name in leave_off:
        continue
    kind = item.GetClass()
    if kind == "PCB_VIA":
        if item.GetViaType() != pcbnew.VIATYPE_THROUGH:
            problems.append("a via is blind, buried or micro; only vias through the whole board are read; skipped")
            continue
        new = pcbnew.PCB_VIA(base)
        new.SetPosition(item.GetPosition())
        new.SetViaType(pcbnew.VIATYPE_THROUGH)
        new.SetLayerPair(item.TopLayer(), item.BottomLayer())
        new.SetDrill(item.GetDrillValue())
        new.SetWidth(pcbnew.F_Cu, item.GetWidth(pcbnew.F_Cu))
    elif kind in ("PCB_TRACK", "PCB_ARC"):
        layer_name = routed.GetLayerName(item.GetLayer())
        if layer_name not in copper:
            problems.append(f"a track is on layer {layer_name!r}, which is not a copper layer of this board; skipped")
            continue
        new = pcbnew.PCB_ARC(base) if kind == "PCB_ARC" else pcbnew.PCB_TRACK(base)
        new.SetStart(item.GetStart())
        new.SetEnd(item.GetEnd())
        if kind == "PCB_ARC":
            new.SetMid(item.GetMid())
        new.SetWidth(item.GetWidth())
        new.SetLayer(base.GetLayerID(layer_name))
    else:
        continue
    new.SetNet(net)
    base.Add(new)
    owners[new.m_Uuid.AsString()] = name
pcbnew.ZONE_FILLER(base).Fill(base.Zones())
pcbnew.SaveBoard(out_path, base)
base.BuildConnectivity()
missing = base.GetConnectivity().GetUnconnectedCount(False)  # connections still missing, counted by KiCad itself
plain = {}  # net name as KiCad's reports print it -> net name as the board file spells it
for code in range(base.GetNetCount()):
    net = base.FindNet(code)
    if net is not None and net.GetNetname():
        plain[unescape(net.GetNetname())] = net.GetNetname()
json.dump({"owners": owners, "problems": problems, "missing": missing, "plain": plain, "parts": parts, "off_board": off_board},
          open(owners_path, "w"))
"""


def merge(routed_path, base_pcb, out_path, leave_off=(), free=()):
    """Copy tracks, arcs and vias from routed_path onto the pristine board, except those of the nets in
    leave_off; in a place-and-route task, also put each part named in free where routed_path has it;
    refill zones; save. Returns a dict: "owners" {uuid: net name} for the copied items, "problems",
    "missing" (how many connections KiCad still counts as missing), "plain" (net names as reports
    print them -> as the board spells them), "parts" {uuid of a free part or one of its pads: its nets}
    and "off_board" {reference of a free part that is not wholly on the board: its nets}. Runs KiCad's
    Python module in a child process: it prints debug lines and can crash when it exits, after its work
    is done."""
    owners_path = out_path + ".owners.json"
    leave_off_path = out_path + ".options.json"
    with open(leave_off_path, "w") as f:
        json.dump({"leave_off": sorted(leave_off), "free": sorted(free)}, f)
    for attempt in range(3):  # KiCad's Python module now and then dies before it has finished; try again
        done = subprocess.run([sys.executable, "-c", MERGE, routed_path, base_pcb, out_path, owners_path, str(MAX_ITEMS), leave_off_path],
                              capture_output=True, text=True, timeout=1800)
        if os.path.exists(owners_path):
            break
    if not os.path.exists(owners_path):
        tail = (done.stderr or "").strip().splitlines()[-1:] or ["KiCad could not load or process the file"]
        raise ValueError(tail[0][:200])
    with open(owners_path) as f:
        data = json.load(f)
    os.unlink(owners_path)
    if "error" in data:
        raise ValueError(data["error"])
    return data


def drc(pcb_path):
    out = pcb_path + ".drc.json"
    for attempt in range(3):  # the same for kicad-cli: try again if it left no readable report
        subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "--all-track-errors", "--output", out, pcb_path],
                       capture_output=True, text=True, timeout=1800)
        try:
            with open(out) as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    raise ValueError("KiCad's design-rule check did not produce a report")


UNCONNECTED_LIMIT = 499                      # KiCad lists at most this many missing connections in one report
VIOLATION_LIMITS = {"clearance": 499}        # ... and at most this many violations of one kind
OTHER_VIOLATION_LIMIT = 199
MAX_ROUNDS = 12


def open_nets_of(report, plain):
    """Nets with an unconnected item in a DRC report, spelled as the board file spells them. KiCad
    describes an item as e.g. 'Pad 3 [/SDA] of U2 on F.Cu' or 'Track [GND] on B.Cu, length ...'."""
    nets = set()
    for entry in report.get("unconnected_items", []):
        for item in entry.get("items", []):
            d = item.get("description", "")
            if "[" in d and "]" in d:
                shown = d[d.index("[") + 1:d.rindex("]")]
                nets.add(plain.get(shown, shown))
    return nets


PROBE = r"""
import json, sys, pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
nets = json.load(open(sys.argv[2]))
pads = [p for fp in board.GetFootprints() for p in fp.Pads()]
zones = list(board.Zones())
tracks = list(board.GetTracks())
codes = {kind: [i.GetNetCode() for i in items] for kind, items in (("pads", pads), ("zones", zones), ("tracks", tracks))}
far = pcbnew.FromMM(1500)
parked = [False] * len(tracks)
def missing(group):
    # Connections KiCad counts as missing among these nets alone. Everything else is taken out of the
    # picture: pads and zones lose their net, tracks and vias lose their net and are parked far off the
    # board (KiCad gives a track that touches a pad the pad's net, so they must not touch anything).
    keep = {board.FindNet(n).GetNetCode() for n in group if board.FindNet(n) is not None}
    for item, code in zip(pads, codes["pads"]):
        item.SetNetCode(code if code in keep else 0)
    for item, code in zip(zones, codes["zones"]):
        item.SetNetCode(code if code in keep else 0)
    for k, (item, code) in enumerate(zip(tracks, codes["tracks"])):
        if (code in keep) == parked[k]:
            item.Move(pcbnew.VECTOR2I(-far if parked[k] else far, 0))
            parked[k] = not parked[k]
        item.SetNetCode(code if code in keep else 0)
    board.BuildConnectivity()
    return board.GetConnectivity().GetUnconnectedCount(False)
def open_among(group):  # split in halves until the open nets are singled out
    if not group or missing(group) == 0:
        return []
    if len(group) == 1:
        return list(group)
    half = len(group) // 2
    return open_among(group[:half]) + open_among(group[half:])
json.dump(open_among(nets), open(sys.argv[3], "w"))
"""


def probe_open(board_path, nets):
    """Which of these nets still have a missing connection on the board, by KiCad's connectivity engine."""
    if not nets:
        return set()
    ask, answer = board_path + ".probe.json", board_path + ".probed.json"
    with open(ask, "w") as f:
        json.dump(sorted(nets), f)
    subprocess.run([sys.executable, "-c", PROBE, board_path, ask, answer], capture_output=True, text=True, timeout=1800)
    if not os.path.exists(answer):
        return set(nets)  # the probe did not finish: count none of them
    with open(answer) as f:
        return set(json.load(f))


DEAD_ENDS = {"track_dangling", "via_dangling"}  # KiCad only warns about these


def read_violations(report, owners, matched=()):
    """The violations that count and that involve copper from the routed file, and the nets of that copper.
    On a net whose length is matched against another (the nets in matched), a track or via that leads
    nowhere counts too: otherwise a dead-end stub would make up the length without carrying the signal."""
    violations, bad_nets = [], set()
    for v in report.get("violations", []):
        nets = {owners[i["uuid"]] for i in v.get("items", []) if i.get("uuid") in owners}
        if v.get("type") in DEAD_ENDS and v.get("severity") != "error":
            nets &= set(matched)
        elif v.get("severity") != "error" or v.get("type") not in COUNTED:
            continue
        if not nets:
            continue  # not about copper from the routed file
        bad_nets |= nets
        violations.append({"type": v["type"], "description": v.get("description", ""), "nets": sorted(nets),
                           "at": [(round(i["pos"]["x"], 3), round(i["pos"]["y"], 3)) for i in v.get("items", []) if "pos" in i][:2]})
    return violations, bad_nets


# In a place-and-route task, violations between the parts themselves count too.
PLACEMENT = {
    "clearance", "shorting_items", "hole_clearance", "hole_to_hole", "copper_edge_clearance", "courtyards_overlap",
    "items_not_allowed", "pth_inside_courtyard", "npth_inside_courtyard", "solder_mask_bridge",
}


def read_placement(report, owners, parts):
    """Violations that are about where a free part sits and involve no copper from the routed file
    (those are already counted against the copper), and the nets of the parts involved."""
    violations, bad_nets = [], set()
    for v in report.get("violations", []):
        if v.get("severity") != "error" or v.get("type") not in PLACEMENT:
            continue
        ids = [i.get("uuid") for i in v.get("items", [])]
        if any(i in owners for i in ids) or not any(i in parts for i in ids):
            continue
        nets = {n for i in ids for n in parts.get(i, [])}
        bad_nets |= nets
        violations.append({"type": v["type"], "description": v.get("description", ""), "nets": sorted(nets),
                           "at": [(round(i["pos"]["x"], 3), round(i["pos"]["y"], 3)) for i in v.get("items", []) if "pos" in i][:2]})
    return violations, bad_nets


def cut_short(report, kinds=COUNTED):
    """Kinds of violation, among those given, whose list KiCad stopped before the end."""
    seen = {}
    for v in report.get("violations", []):
        seen[v.get("type")] = seen.get(v.get("type"), 0) + 1
    return sorted(t for t, n in seen.items() if t in kinds and n >= VIOLATION_LIMITS.get(t, OTHER_VIOLATION_LIMIT))


def check(routed_path, keep=None, base_dir=None):
    base_dir = base_dir or BASE_DIR
    with open(os.path.join(base_dir, "task.json")) as f:
        task = json.load(f)
    work = tempfile.mkdtemp(prefix="boardcheck-")
    name = task["board"]
    routable = set(task["routable_nets"])
    free = task.get("free_footprints", [])  # a place-and-route task names the parts the agent has to place
    taken_off, violations, rounds, unfinished = set(), [], 0, []
    try:
        while True:
            rounds += 1
            merged = os.path.join(work, f"merged{rounds}", name + ".kicad_pcb")
            os.makedirs(os.path.dirname(merged))
            merged_info = merge(routed_path, os.path.join(base_dir, name + ".kicad_pcb"), merged, taken_off, free)
            owners = merged_info["owners"]
            if rounds == 1:
                items_read = len(owners)
            # KiCad's Python module writes a project file next to a board it saves, and on a fresh machine
            # that file holds KiCad's default rules, not the board's. So the check runs in a clean folder:
            # the merged board next to the pristine project file and the pristine rules file.
            rundir = os.path.join(work, f"drc{rounds}")
            os.makedirs(rundir)
            graded = os.path.join(rundir, name + ".kicad_pcb")
            shutil.copy(merged, graded)
            for ext in (".kicad_pro", ".kicad_dru"):
                src = os.path.join(base_dir, name + ext)
                if os.path.exists(src):
                    shutil.copy(src, os.path.join(rundir, name + ext))
            report = drc(graded)
            found, bad_nets = read_violations(report, owners, task.get("no_dead_end_nets", []))
            violations += found
            unfinished = cut_short(report)
            if not unfinished or not bad_nets or rounds >= MAX_ROUNDS:
                break
            taken_off |= bad_nets  # the list stopped early: take these nets off the board and look again
        open_nets = open_nets_of(report, merged_info["plain"])
        if len(report.get("unconnected_items", [])) >= UNCONNECTED_LIMIT:
            # The list of missing connections stopped early. A net with no new copper is certainly still
            # open. Every other net that is not on the list is tested on its own.
            with_copper = set(owners.values())
            open_nets |= routable - with_copper
            open_nets |= probe_open(graded, (with_copper | set(merged_info["plain"].values())) - open_nets)
        if keep:
            shutil.copy(graded, keep)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    problems = merged_info["problems"]
    still_cut_short = bool(unfinished and bad_nets)  # after MAX_ROUNDS
    placement = []
    if free:
        placement, misplaced = read_placement(report, owners, merged_info["parts"])
        for ref, nets in sorted(merged_info["off_board"].items()):
            placement.append({"type": "part_off_board", "description": f"Part {ref} is not wholly on the board", "nets": nets, "at": []})
            misplaced |= set(nets)
        if cut_short(report, PLACEMENT):  # the list of placement problems stopped early: count no placed part
            misplaced |= {n for nets in merged_info["parts"].values() for n in nets}
            problems = problems + ["KiCad's list of violations between parts was cut short; no net of a placed part is counted"]
        bad_nets = bad_nets | misplaced
        violations += placement
    if still_cut_short:  # nothing left on the board is counted
        bad_nets = bad_nets | set(owners.values())
        problems = problems + [f"KiCad's list of violations was still cut short after {MAX_ROUNDS} rounds; no net with copper on the board is counted"]
    bad_nets = bad_nets | taken_off
    failed = open_nets | bad_nets
    good = routable - failed
    collateral = failed - routable  # nets that needed no routing and are now broken, e.g. a cut ground pour
    reward = max(0.0, (len(good) - len(collateral)) / len(routable)) if routable else 0.0
    return {
        "reward": round(reward, 4),
        "full_pass": int(not failed and not violations),
        "nets_to_route": len(routable),
        "nets_good": len(good),
        "nets_open": len(open_nets & routable),
        "nets_with_violations": len(bad_nets),
        "other_nets_broken": len(collateral),
        "violations": len(violations),
        "tracks_and_vias": items_read,
        "connections_missing": merged_info["missing"],
        "rounds": rounds,
        "parts_to_place": len(free),
        "placement_violations": len(placement),
        "detail": {"good_nets": sorted(good), "open_nets": sorted(open_nets), "violations": violations[:200], "problems": problems[:50]},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("routed")
    parser.add_argument("--board", help="which board the file is a routing of, where there are several: a folder name under "
                                        + os.path.join(HERE, "boards"))
    parser.add_argument("--json")
    parser.add_argument("--keep", help="save the board that was actually checked (pristine board + your copper, zones refilled)")
    args = parser.parse_args()
    base_dir = os.path.join(HERE, "boards", args.board) if args.board else BASE_DIR
    if not os.path.exists(os.path.join(base_dir, "task.json")):
        boards = sorted(os.listdir(os.path.join(HERE, "boards"))) if os.path.isdir(os.path.join(HERE, "boards")) else []
        print("boardcheck needs to know which board this is: --board <name>" + (", one of " + ", ".join(boards) if boards else ""))
        return 2
    try:
        result = check(args.routed, args.keep, base_dir)
    except Exception as exc:  # a file KiCad cannot load, and the like
        result = {"reward": 0.0, "full_pass": 0, "error": f"{type(exc).__name__}: {exc}"}
    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=1)
    if "error" in result:
        print("boardcheck could not check this file:", result["error"])
        return 1
    print(f"Nets to route: {result['nets_to_route']}")
    print(f"  fully connected and free of violations: {result['nets_good']}")
    print(f"  still open: {result['nets_open']}")
    print(f"  with design-rule violations: {result['nets_with_violations']} ({result['violations']} violations)")
    if result["parts_to_place"]:
        print(f"  parts to place: {result['parts_to_place']}; violations between parts or parts off the board: {result['placement_violations']}")
    if result["other_nets_broken"]:
        print(f"  other nets broken by the new copper: {result['other_nets_broken']}")
    if result["rounds"] > 1:
        print(f"  KiCad's list of violations was cut short, so the nets found bad were taken off the board and the "
              f"rest was checked again ({result['rounds']} rounds)")
    print(f"Score: {result['reward']:.3f}   Complete and clean: {'yes' if result['full_pass'] else 'no'}")
    detail = result["detail"]
    for p in detail["problems"][:10]:
        print("  note:", p)
    if detail["open_nets"]:
        print("Open nets:", ", ".join(detail["open_nets"][:60]) + (" ..." if len(detail["open_nets"]) > 60 else ""))
    for v in detail["violations"][:40]:
        print(f"  {v['type']}: {v['description']} nets {v['nets']} near {v['at']}")
    if len(detail["violations"]) > 40:
        print(f"  ... and {len(detail['violations']) - 40} more (use --json for all)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
