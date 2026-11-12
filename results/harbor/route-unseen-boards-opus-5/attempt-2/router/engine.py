#!/usr/bin/env python3
"""Routing engine: ratsnest -> maze routing -> DRC feedback -> rip-up and retry."""
import os, sys, json, math, time, shutil, subprocess, tempfile, heapq, re

import numpy as np
import pcbnew

from route import astar, mark_capsule, mark_disk, T0, TIME_LIMIT, MM

COUNTED = {
    "clearance", "shorting_items", "tracks_crossing", "track_width", "via_diameter", "annular_width",
    "drill_out_of_range", "hole_clearance", "hole_to_hole", "holes_co_located", "copper_edge_clearance",
    "items_not_allowed", "microvia_drill_out_of_range", "padstack", "track_angle", "track_segment_length",
    "via_dangling", "creepage", "connection_width", "copper_sliver", "solder_mask_bridge",
    "skew_out_of_range", "length_out_of_range",
}
ESCAPES = {"dblquote": '"', "quote": "'", "lt": "<", "gt": ">", "backslash": "\\", "slash": "/", "bar": "|",
           "comma": ",", "colon": ":", "space": " ", "dollar": "$", "tab": "\t", "return": "\n", "brace": "{"}


def unescape(name):
    return re.sub(r"\{([a-z]+)\}", lambda m: ESCAPES.get(m.group(1), m.group(0)), name)


def left():
    return TIME_LIMIT - (time.time() - T0)


def run_drc(pcb_path, workdir, extra=()):
    out = os.path.join(workdir, "drc.json")
    if os.path.exists(out):
        os.unlink(out)
    try:
        subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all",
                        "--all-track-errors", "--output", out, pcb_path],
                       capture_output=True, text=True, timeout=max(20, min(180, left())))
    except Exception:
        return None
    try:
        return json.load(open(out))
    except Exception:
        return None


class Engine(object):
    def __init__(self, R, dst):
        self.R = R
        self.dst = dst
        self.work = tempfile.mkdtemp(prefix="router-")
        self.netitems = {}
        self.plain = {}
        for n in R.netpads:
            self.plain[unescape(n)] = n

    # -------------------------------------------------- output
    def save(self, path=None):
        R = self.R
        b = R.board
        self.myuuids = {}
        for t in list(b.GetTracks()):
            b.Remove(t)
        for net, items in self.netitems.items():
            nobj = b.FindNet(net)
            if nobj is None:
                continue
            for it in items:
                if it[0] == 'T':
                    _, lay, x1, y1, x2, y2, w = it
                    t = pcbnew.PCB_TRACK(b)
                    t.SetStart(pcbnew.VECTOR2I(int(x1), int(y1)))
                    t.SetEnd(pcbnew.VECTOR2I(int(x2), int(y2)))
                    t.SetWidth(int(w))
                    t.SetLayer(R.layers[lay])
                    t.SetNet(nobj)
                    b.Add(t)
                    self.myuuids[t.m_Uuid.AsString()] = net
                else:
                    _, x, y = it
                    v = pcbnew.PCB_VIA(b)
                    v.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
                    v.SetViaType(pcbnew.VIATYPE_THROUGH)
                    v.SetLayerPair(R.layers[0], R.layers[-1])
                    v.SetDrill(int(R.via_drill))
                    try:
                        v.SetWidth(R.layers[0], int(R.via_d))
                        v.SetWidth(R.layers[-1], int(R.via_d))
                    except Exception:
                        pass
                    try:
                        v.SetFrontWidth(int(R.via_d))
                    except Exception:
                        pass
                    v.SetNet(nobj)
                    b.Add(v)
                    self.myuuids[v.m_Uuid.AsString()] = net
        pcbnew.SaveBoard(path or self.dst, b)

    # -------------------------------------------------- ratsnest
    def ratsnest(self, pcb_path):
        rep = run_drc(pcb_path, self.work)
        if rep is None:
            return None
        return self.ratsnest_from(rep)

    def ratsnest_from(self, rep):
        edges = []
        for e in rep.get("unconnected_items", []):
            items = e.get("items", [])
            if len(items) != 2:
                continue
            a, b = items[0], items[1]
            net = None
            d = a.get("description", "")
            if "[" in d and "]" in d:
                net = d[d.index("[") + 1:d.rindex("]")]
                net = self.plain.get(net, net)
            if net is None or net not in self.R.netpads:
                continue
            edges.append((net, a.get("uuid"), b.get("uuid"),
                          (int(round(a["pos"]["x"] * MM)), int(round(a["pos"]["y"] * MM))),
                          (int(round(b["pos"]["x"] * MM)), int(round(b["pos"]["y"] * MM)))))
        return edges

    # -------------------------------------------------- occupancy
    def rebuild_occ(self, skip=()):
        R = self.R
        for w in R.wgroups:
            for l in range(R.nlayers):
                np.copyto(R.occT[w][l], R.base_occT[w][l])
        for l in range(R.nlayers):
            np.copyto(R.occV[l], R.base_occV[l])
        for net, items in self.netitems.items():
            if net in skip:
                continue
            nid = R.netid[net]
            for it in items:
                if it[0] == 'T':
                    _, lay, x1, y1, x2, y2, w = it
                    R.mark_track(x1, y1, x2, y2, lay, w, nid)
                else:
                    R.mark_via(it[1], it[2], nid)

    def add_items(self, net, items):
        R = self.R
        nid = R.netid[net]
        self.netitems.setdefault(net, []).extend(items)
        for it in items:
            if it[0] == 'T':
                _, lay, x1, y1, x2, y2, w = it
                R.mark_track(x1, y1, x2, y2, lay, w, nid)
            else:
                R.mark_via(it[1], it[2], nid)

    # -------------------------------------------------- routing
    def node_sets(self, net, uuid, pos, freeb):
        """nodes for one endpoint of a ratsnest edge."""
        R = self.R
        g = R.g
        N = g.N
        pad = self.paduuid.get(uuid)
        nodes = set()
        anchors = {}
        if pad is not None:
            cells = R.pad_cells(pad)
            lays = []
            if pad["f"]:
                lays.append(0)
            if pad["b"] and R.nlayers > 1:
                lays.append(1)
            anch = None if pad.get("exact") else (pad["pos"].x, pad["pos"].y)
            for l in lays:
                fb = freeb[l]
                for c in cells:
                    if fb[c] & 1:
                        nodes.add(l * N + c)
                        anchors[l * N + c] = anch
        if not nodes:
            # fall back: nearest free cells around pos
            cx, cy = g.cx(pos[0]), g.cy(pos[1])
            for rad in range(0, 8):
                for dy in range(-rad, rad + 1):
                    for dx in range(-rad, rad + 1):
                        if max(abs(dx), abs(dy)) != rad:
                            continue
                        x, y = cx + dx, cy + dy
                        if 0 <= x < g.W and 0 <= y < g.H:
                            c = y * g.W + x
                            for l in range(R.nlayers):
                                if freeb[l][c] & 1:
                                    nodes.add(l * N + c)
                                    anchors[l * N + c] = None
                if nodes:
                    break
        return nodes, anchors

    def tree_nodes(self, net):
        """grid nodes covered by copper already placed for this net."""
        R = self.R
        g = R.g
        N = g.N
        out = set()
        for it in self.netitems.get(net, []):
            if it[0] == 'T':
                _, lay, x1, y1, x2, y2, w = it
                n = max(1, int(math.hypot(x2 - x1, y2 - y1) / g.p))
                for k in range(n + 1):
                    x = x1 + (x2 - x1) * k / float(n)
                    y = y1 + (y2 - y1) * k / float(n)
                    cx, cy = g.cx(x), g.cy(y)
                    if 0 <= cx < g.W and 0 <= cy < g.H:
                        out.add(lay * N + cy * g.W + cx)
            else:
                cx, cy = g.cx(it[1]), g.cy(it[2])
                if 0 <= cx < g.W and 0 <= cy < g.H:
                    for l in range(R.nlayers):
                        out.add(l * N + cy * g.W + cx)
        return out

    def path_items(self, net, path, anch_s, anch_d):
        """turn a node path into track/via items."""
        R = self.R
        g = R.g
        N = g.N
        w = R.netwidth[net]
        items = []
        pts = []
        for nd in path:
            lay = nd // N
            i = nd - lay * N
            pts.append((lay, g.wx(i % g.W), g.wy(i // g.W)))
        # anchors
        if anch_s:
            l0, x0, y0 = pts[0]
            if (x0, y0) != anch_s:
                pts.insert(0, (l0, anch_s[0], anch_s[1]))
        if anch_d:
            l1, x1, y1 = pts[-1]
            if (x1, y1) != anch_d:
                pts.append((l1, anch_d[0], anch_d[1]))
        # compress
        k = 0
        while k < len(pts) - 1:
            lay, x, y = pts[k]
            lay2, x2, y2 = pts[k + 1]
            if lay2 != lay:
                items.append(('V', x, y))
                k += 1
                continue
            # extend while same direction
            dx, dy = x2 - x, y2 - y
            j = k + 1
            while j + 1 < len(pts):
                l3, x3, y3 = pts[j + 1]
                if l3 != lay:
                    break
                ndx, ndy = x3 - pts[j][1], y3 - pts[j][2]
                if ndx * dy != ndy * dx:
                    break
                j += 1
            ex, ey = pts[j][1], pts[j][2]
            if (ex, ey) != (x, y):
                items.append(('T', lay, x, y, ex, ey, w))
            k = j
        return items

    def route_edge(self, net, e, effort=1.0, via_cost_mul=1.0):
        R = self.R
        g = R.g
        freeb = R.freemap(net)
        _, ua, ub, pa, pb = e
        sa, anchA = self.node_sets(net, ua, pa, freeb)
        sb, anchB = self.node_sets(net, ub, pb, freeb)
        if not sa or not sb:
            return None
        tn = self.tree_nodes(net)
        srcs = sa | (tn & set())
        # add existing copper of this net as extra sources (helps multi-pad nets)
        extra = self.tree_nodes(net)
        srcs = set(sa)
        anchors = dict(anchA)
        for nd in extra:
            if nd not in srcs:
                srcs.add(nd)
                anchors[nd] = None
        dsts = sb
        # bounding box
        xs = [pa[0], pb[0]]
        ys = [pa[1], pb[1]]
        span = max(abs(pa[0] - pb[0]), abs(pa[1] - pb[1]))
        marg = int(max(6 * g.p, span * 0.45 * effort) + 8 * g.p * effort)
        bx0 = max(0, g.cx(min(xs) - marg))
        bx1 = min(g.W - 1, g.cx(max(xs) + marg))
        by0 = max(0, g.cy(min(ys) - marg))
        by1 = min(g.H - 1, g.cy(max(ys) + marg))
        if effort >= 2.0:
            bx0, by0, bx1, by1 = 0, 0, g.W - 1, g.H - 1
        vc = R.via_cost * via_cost_mul
        maxpop = int(min(250000 * effort, (25000 + 1.5 * (bx1 - bx0 + 1) * (by1 - by0 + 1)) * effort))
        dl = min(time.time() + max(2.0, left() * 0.25), T0 + TIME_LIMIT)
        path = astar(g, freeb, srcs, dsts, (bx0, bx1, by0, by1), vc, maxpop, dl, R.nlayers)
        if path is None:
            return None
        aS = anchors.get(path[0])
        aD = anchB.get(path[-1])
        return self.path_items(net, path, aS, aD)


def run(R, dst):
    eng = Engine(R, dst)
    g = R.g
    R.via_cost = 2.0 * MM
    eng.paduuid = {}
    for p in R.pads:
        try:
            eng.paduuid[p["obj"].m_Uuid.AsString()] = p
        except Exception:
            pass
    edges = eng.ratsnest(R.src)
    if not edges:
        # fall back: MST over pads of each net
        edges = []
        for net, pads in R.netpads.items():
            if len(pads) < 2:
                continue
            pts = [(p["pos"].x, p["pos"].y, p) for p in pads]
            used = [0]
            rest = list(range(1, len(pts)))
            while rest:
                best = None
                for i in used:
                    for j in rest:
                        d = (pts[i][0] - pts[j][0]) ** 2 + (pts[i][1] - pts[j][1]) ** 2
                        if best is None or d < best[0]:
                            best = (d, i, j)
                _, i, j = best
                used.append(j)
                rest.remove(j)
                edges.append((net, pts[i][2]["obj"].m_Uuid.AsString(), pts[j][2]["obj"].m_Uuid.AsString(),
                              (pts[i][0], pts[i][1]), (pts[j][0], pts[j][1])))
    # order: group by net, nets by total length
    bynet = {}
    for e in edges:
        bynet.setdefault(e[0], []).append(e)
    def elen(e):
        return math.hypot(e[3][0] - e[4][0], e[3][1] - e[4][1])
    order = sorted(bynet.keys(), key=lambda n: sum(elen(e) for e in bynet[n]))
    for n in order:
        bynet[n].sort(key=elen)
    eng.bynet = bynet

    failed = []
    done = 0
    for net in order:
        if left() < 60:
            break
        for e in bynet[net]:
            items = eng.route_edge(net, e, 1.0)
            if items is None and left() > 150:
                items = eng.route_edge(net, e, 2.0)
            if items is None:
                failed.append(e)
            else:
                eng.add_items(net, items)
                done += 1
    eng.save()
    sys.stderr.write("pass1 routed %d/%d failed %d t=%.1f\n" % (done, len(edges), len(failed), time.time() - T0))
    sys.stderr.flush()
    improve(eng, edges, failed)


def analyze(eng, rep):
    bad = set()
    for v in rep.get("violations", []):
        tp = v.get("type")
        if v.get("severity") != "error" or tp not in COUNTED:
            continue
        paironly = False
        mine = set()
        for it in v.get("items", []):
            u = it.get("uuid")
            if u in eng.myuuids:
                mine.add(eng.myuuids[u])
        if paironly:
            mine &= set(eng.R.pairnet.keys())
        bad |= mine
    openedges = eng.ratsnest_from(rep)
    return bad, openedges


def improve(eng, edges, failed):
    import copy
    R = eng.R
    best = None
    rounds = 0
    while left() > 95 and rounds < 20:
        rounds += 1
        rep = run_drc(eng.dst, eng.work)
        if rep is None:
            return
        bad, openedges = analyze(eng, rep)
        openn = set(e[0] for e in openedges)
        cost = len(bad | openn)
        sys.stderr.write("round %d: bad=%d open=%d cost=%d t=%.1f\n"
                         % (rounds, len(bad), len(openn), cost, time.time() - T0))
        sys.stderr.flush()
        if best is None or cost < best[0]:
            best = (cost, copy.deepcopy(eng.netitems))
        if cost == 0:
            return
        # rip up the nets that break rules, keep the rest, and route the open bits again
        ripped = set(sorted(bad)[:8])
        if rounds > 1:
            # also rip up a couple of the nets blocking the still-open ones
            for e in openedges[:3]:
                ripped.add(e[0])
        saved = {}
        for n in ripped:
            saved[n] = eng.netitems.pop(n, [])
        eng.rebuild_occ()
        todo = []
        for n in ripped:
            for e in eng.bynet.get(n, []):
                todo.append(e)
        for e in openedges:
            if e[0] not in ripped:
                todo.append(e)
        todo.sort(key=lambda e: math.hypot(e[3][0] - e[4][0], e[3][1] - e[4][1]))
        for e in todo:
            if left() < 85:
                break
            items = eng.route_edge(e[0], e, 2.0, via_cost_mul=1.0 + 0.25 * rounds)
            if items is not None:
                eng.add_items(e[0], items)
        eng.save()
    # hand in the best state seen
    if best is not None:
        rep = run_drc(eng.dst, eng.work) if left() > 25 else None
        cost = None
        if rep is not None:
            bad, openedges = analyze(eng, rep)
            cost = len(bad | set(e[0] for e in openedges))
        if cost is None or cost > best[0]:
            eng.netitems = best[1]
            eng.save()
            sys.stderr.write("restored best cost=%d\n" % best[0])
