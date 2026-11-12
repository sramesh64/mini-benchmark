#!/usr/bin/env python3
"""Turn a routed KiCad board into a routing task: strip every track and via, keep everything else.

Usage (inside the KiCad image): make_route_task.py <project dir> <board name> <out dir>
Writes   <out>/base/<board>.kicad_pcb    the unrouted board (zones refilled)
         <out>/base/<board>.kicad_pro    the project file with the design rules
         <out>/base/<board>.kicad_dru    KiCad custom rules: the designer's own, if any, plus a minimum track
                                         width for each net (the narrowest track the designer used on it)
         <out>/base/task.json            which nets have to be routed
         <out>/original/<board>.kicad_pcb  the board as its designer routed it (the reference solution)
"""

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

TRACK_NODES = ("segment", "via", "arc")  # the board file's top-level track items


def strip_tracks(text):
    """Remove the top-level (segment ...), (via ...) and (arc ...) nodes from a .kicad_pcb file's text."""
    out, depth, i, n = [], 0, 0, len(text)
    counts = {name: 0 for name in TRACK_NODES}
    while i < n:
        ch = text[i]
        if ch == '"':  # a quoted string: copy it whole
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif ch == "(":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            name = text[i + 1:j]
            if depth == 1 and name in TRACK_NODES:  # skip this whole node
                d, k = 0, i
                while k < n:
                    c = text[k]
                    if c == '"':
                        k += 1
                        while k < n and text[k] != '"':
                            k += 2 if text[k] == "\\" else 1
                    elif c == "(":
                        d += 1
                    elif c == ")":
                        d -= 1
                        if d == 0:
                            break
                    k += 1
                counts[name] += 1
                i = k + 1
                while i < n and text[i] in " \t":  # and the rest of its line
                    i += 1
                if i < n and text[i] == "\n":
                    i += 1
                while out and out[-1] in (" ", "\t"):
                    out.pop()
                continue
            depth += 1
            out.append(ch)
            i += 1
        elif ch == ")":
            depth -= 1
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out), counts


REFILL = """
import sys, pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
pcbnew.ZONE_FILLER(board).Fill(board.Zones())
pcbnew.SaveBoard(sys.argv[1], board)
pads = sum(len(list(f.Pads())) for f in board.GetFootprints())
bb = board.GetBoardEdgesBoundingBox()
import json
json.dump({"copper_layers": board.GetCopperLayerCount(), "footprints": len(board.GetFootprints()), "pads": pads,
           "zones": board.GetAreaCount(), "size_mm": [round(pcbnew.ToMM(bb.GetWidth()), 1), round(pcbnew.ToMM(bb.GetHeight()), 1)]},
          open(sys.argv[2], "w"))
"""


WIDTHS = """
import json, sys, pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
narrowest = {}
for t in board.GetTracks():
    if t.GetClass() != "PCB_VIA":
        narrowest[t.GetNetname()] = min(narrowest.get(t.GetNetname(), 10**12), t.GetWidth())
names = [board.FindNet(code).GetNetname() for code in range(board.GetNetCount()) if board.FindNet(code) is not None]
json.dump({"narrowest_nm": narrowest, "names": names}, open(sys.argv[2], "w"))
"""


def width_rules(narrowest_nm, nets, floor_mm, all_names):
    """KiCad custom rules saying that no track on a net may be narrower than the narrowest track the
    board's designer used on that net. KiCad's DRC only checks the one board-wide minimum otherwise, so
    a hairline track on a motor or supply net would pass. Returns (rules text, {net: mm}, nets left out).

    A rule names its nets as the board file spells them. KiCad compares names without regard to case and
    treats * and ? as wildcards, so a net whose name would also match another net is left out."""
    tiers, skipped = {}, []
    for net in sorted(nets):
        width = narrowest_nm.get(net)
        if width is None or width <= round(floor_mm * 1e6):
            continue  # no tighter than the board-wide minimum: nothing to add
        pattern = re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in net) + r"\Z", re.IGNORECASE | re.DOTALL)
        if any(c in net for c in "'\"\\") or sum(1 for other in all_names if pattern.match(other)) != 1:
            skipped.append(net)
            continue
        tiers.setdefault(width, []).append(net)
    text, widths = "", {}
    for width in sorted(tiers):
        mm = f"{width / 1e6:.6f}".rstrip("0").rstrip(".")
        condition = " || ".join(f"A.NetName == '{net}'" for net in tiers[width])
        text += (f'(rule "tracks at least {mm} mm wide ({len(tiers[width])} net{"" if len(tiers[width]) == 1 else "s"})"\n'
                 f'  (condition "{condition}")\n  (constraint track_width (min {mm}mm)))\n')
        widths.update({net: width / 1e6 for net in tiers[width]})
    return text, widths, skipped


def diff_pairs(nets):
    """Differential pairs among these nets, by name as KiCad pairs them: X+ with X-, X_P with X_N.
    Returns [(base name, positive net, negative net)]."""
    pairs = []
    for net in sorted(nets):
        for plus, minus in (("+", "-"), ("_P", "_N")):
            if net.endswith(plus) and net[:-len(plus)] + minus in nets:
                pairs.append((net[:-len(plus)], net, net[:-len(plus)] + minus))
    return pairs


def skew_rule(base, limit_mm):
    mm = f"{limit_mm:.2f}".rstrip("0").rstrip(".")
    return (f'(rule "pair {base}: the two tracks within {mm} mm of each other in length"\n'
            f"  (condition \"A.inDiffPair('{base}')\")\n  (constraint skew (max {mm}mm)))\n")


SKEW_FLOOR_MM = 0.5   # no pair has to be matched more closely than this
SKEW_SLACK = 1.25     # ... or more closely than the designer matched it, with this much slack


def pair_rules(src_dir, name, pairs, work):
    """KiCad custom rules saying that the two tracks of a differential pair must match in length about as
    closely as the designer matched them. The designer's own mismatch is read off KiCad's check of the
    original board against an impossibly tight limit. Returns (rules text, {base: mm}, pairs left out)."""
    usable, left_out = [], []
    for base, plus, minus in pairs:
        (left_out if any(c in base for c in "'\"\\*?") else usable).append(base)
    if not usable:
        return "", {}, left_out
    probe = os.path.join(work, "pairs")
    os.makedirs(probe)
    shutil.copy(os.path.join(src_dir, name + ".kicad_pcb"), os.path.join(probe, name + ".kicad_pcb"))
    shutil.copy(os.path.join(src_dir, name + ".kicad_pro"), os.path.join(probe, name + ".kicad_pro"))
    with open(os.path.join(probe, name + ".kicad_dru"), "w") as f:
        f.write("(version 1)\n" + "".join(
            f"(rule \"probe{k}\"\n  (condition \"A.inDiffPair('{base}')\")\n  (constraint skew (max 0.0001mm)))\n" for k, base in enumerate(usable)))
    import boardcheck
    measured = {}
    for v in boardcheck.drc(os.path.join(probe, name + ".kicad_pcb")).get("violations", []):
        found = re.search(r"rule 'probe(\d+)'.*?actual (-?[\d.]+) mm", v.get("description", ""))
        if v.get("type") == "skew_out_of_range" and found:
            k = int(found.group(1))
            measured[usable[k]] = max(measured.get(usable[k], 0.0), abs(float(found.group(2))))
    limits = {base: max(SKEW_FLOOR_MM, math.ceil(measured.get(base, 0.0) * SKEW_SLACK * 100) / 100) for base in usable}
    return "".join(skew_rule(base, limits[base]) for base in usable), limits, left_out


def main(src_dir, name, out):
    base, orig = os.path.join(out, "base"), os.path.join(out, "original")
    os.makedirs(base, exist_ok=True)
    os.makedirs(orig, exist_ok=True)
    src = os.path.join(src_dir, name + ".kicad_pcb")
    shutil.copy(src, os.path.join(orig, name + ".kicad_pcb"))
    with open(src) as f:
        stripped_text, counts = strip_tracks(f.read())
    stripped = os.path.join(base, name + ".kicad_pcb")
    with open(stripped, "w") as f:
        f.write(stripped_text)
    def project_files():
        for ext in (".kicad_pro", ".kicad_dru"):
            if os.path.exists(os.path.join(src_dir, name + ext)):
                shutil.copy(os.path.join(src_dir, name + ext), os.path.join(base, name + ext))
                shutil.copy(os.path.join(src_dir, name + ext), os.path.join(orig, name + ext))

    project_files()  # the refill needs the board's rules beside it
    info = stripped + ".info.json"
    for attempt in range(3):  # KiCad's Python module now and then dies before it has finished; try again
        subprocess.run([sys.executable, "-c", REFILL, stripped, info], capture_output=True, text=True)  # may also crash on exit; the files tell
        if os.path.exists(info):
            break
    with open(info) as f:
        facts = json.load(f)
    os.unlink(info)
    project_files()  # KiCad rewrites the project file when it saves a board: put the designer's own back
    for extra in (".kicad_prl",):
        if os.path.exists(os.path.join(base, name + extra)):
            os.unlink(os.path.join(base, name + extra))
    # The nets to route: those the designer put copper on, and that are not already connected once that
    # copper is gone (a ground pour can still connect some).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import boardcheck
    work = tempfile.mkdtemp()
    with_copper = set(boardcheck.merge(src, stripped, os.path.join(work, "a.kicad_pcb"))["owners"].values())
    os.makedirs(os.path.join(work, "b"))
    bare = boardcheck.merge(stripped, stripped, os.path.join(work, "b", name + ".kicad_pcb"))
    rundir = os.path.join(work, "drc")
    os.makedirs(rundir)
    shutil.copy(os.path.join(work, "b", name + ".kicad_pcb"), os.path.join(rundir, name + ".kicad_pcb"))
    for ext in (".kicad_pro", ".kicad_dru"):
        if os.path.exists(os.path.join(src_dir, name + ext)):
            shutil.copy(os.path.join(src_dir, name + ext), os.path.join(rundir, name + ext))
    report = boardcheck.drc(os.path.join(rundir, name + ".kicad_pcb"))
    capped = len(report.get("unconnected_items", [])) >= boardcheck.UNCONNECTED_LIMIT
    still_open = boardcheck.open_nets_of(report, bare["plain"])
    if capped:  # KiCad's list stopped early: test the nets it did not get to one by one
        still_open |= boardcheck.probe_open(os.path.join(rundir, name + ".kicad_pcb"), with_copper - still_open)
    nets = with_copper & still_open
    # Minimum track widths, net by net, as KiCad custom rules beside the board.
    for attempt in range(3):
        subprocess.run([sys.executable, "-c", WIDTHS, src, os.path.join(work, "widths.json")], capture_output=True, text=True)
        if os.path.exists(os.path.join(work, "widths.json")):
            break
    with open(os.path.join(work, "widths.json")) as f:
        measured = json.load(f)
    with open(os.path.join(src_dir, name + ".kicad_pro")) as f:
        floor_mm = json.load(f).get("board", {}).get("design_settings", {}).get("rules", {}).get("min_track_width", 0.0)
    rules, widths, left_out = width_rules(measured["narrowest_nm"], nets, floor_mm, measured["names"])
    pairs = diff_pairs(nets)
    matched, skews, pairs_left_out = pair_rules(src_dir, name, pairs, work)
    own = os.path.join(src_dir, name + ".kicad_dru")
    text = open(own).read().rstrip("\n") + "\n" if os.path.exists(own) else "(version 1)\n"
    if rules:
        text += ("# Minimum track widths, net by net: no track may be narrower than the narrowest track the\n"
                 "# board's designer used on that net. Net names are spelled as in the board file.\n" + rules)
    if matched:
        text += ("# Differential pairs: the two tracks of a pair must match in length about as closely as the\n"
                 f"# board's designer matched them (the designer's mismatch plus {round((SKEW_SLACK - 1) * 100)}%, and never tighter than {SKEW_FLOOR_MM} mm).\n" + matched)
    if rules or matched:
        for folder in (base, orig):
            with open(os.path.join(folder, name + ".kicad_dru"), "w") as f:
                f.write(text)
    # A dead-end track on a length-matched net would pad its length, so it counts as a violation there,
    # except on a net where the designer's own board has one.
    matched_nets = {net for base_name, plus, minus in pairs if base_name in skews for net in (plus, minus)}
    if matched_nets:
        look = os.path.join(work, "dead-ends")
        os.makedirs(look)
        shutil.copy(src, os.path.join(look, name + ".kicad_pcb"))
        shutil.copy(os.path.join(src_dir, name + ".kicad_pro"), os.path.join(look, name + ".kicad_pro"))
        seen = boardcheck.drc(os.path.join(look, name + ".kicad_pcb"))
        for v in seen.get("violations", []):
            if v.get("type") in boardcheck.DEAD_ENDS:
                for item in v.get("items", []):
                    d = item.get("description", "")
                    if "[" in d and "]" in d:
                        shown = d[d.index("[") + 1:d.rindex("]")]
                        matched_nets.discard(bare["plain"].get(shown, shown))
    shutil.rmtree(work, ignore_errors=True)
    task = dict({"board": name, "routable_nets": sorted(nets)}, **facts)
    task.update(original_tracks=counts["segment"] + counts["arc"], original_vias=counts["via"],
                connections_missing=bare["missing"], report_capped=capped,
                board_min_track_width_mm=floor_mm, net_min_track_width_mm=widths, width_rule_left_out=left_out,
                pair_max_skew_mm=skews, pair_nets={base: [plus, minus] for base, plus, minus in pairs}, pair_rule_left_out=pairs_left_out,
                no_dead_end_nets=sorted(matched_nets))
    with open(os.path.join(base, "task.json"), "w") as f:
        json.dump(task, f, indent=1)
    print(json.dumps({k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in task.items()}))


if __name__ == "__main__":
    main(*sys.argv[1:4])
