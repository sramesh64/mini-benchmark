#!/usr/bin/env python3
"""Grid maze router for 2-layer KiCad boards.

Usage: python3 route.py <unrouted.kicad_pcb> <routed.kicad_pcb>
"""
import sys, os, re, math, time, heapq, json, random

T0 = time.time()
TIME_LIMIT = float(os.environ.get("ROUTE_SECONDS", "408"))
def left():
    return TIME_LIMIT - (time.time() - T0)

import numpy as np
import pcbnew

MM = 1000000.0  # nm per mm
F_CU = pcbnew.F_Cu
B_CU = pcbnew.B_Cu


# ---------------------------------------------------------------- rules ----

def parse_dru(path):
    """-> (min width per net in nm, skew max per pair name in nm)."""
    widths, skews = {}, {}
    if not os.path.exists(path):
        return widths, skews
    txt = open(path, encoding="utf-8", errors="replace").read()
    # split into rules
    for m in re.finditer(r'\(rule\s', txt):
        start = m.start()
        depth, i = 0, start
        while i < len(txt):
            if txt[i] == '(':
                depth += 1
            elif txt[i] == ')':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = txt[start:i + 1]
        cond = re.search(r'\(condition\s+"(.*?)"\s*\)', body, re.S)
        cond = cond.group(1) if cond else ""
        w = re.search(r'\(constraint\s+track_width\s*\(\s*min\s+([0-9.]+)mm', body)
        if w:
            val = int(round(float(w.group(1)) * MM))
            for n in re.findall(r"A\.NetName\s*==\s*'([^']*)'", cond):
                widths[n] = max(widths.get(n, 0), val)
            for n in re.findall(r'A\.NetName\s*==\s*"([^"]*)"', cond):
                widths[n] = max(widths.get(n, 0), val)
        s = re.search(r'\(constraint\s+skew\s*\(\s*max\s+([0-9.]+)mm', body)
        if s:
            val = int(round(float(s.group(1)) * MM))
            for n in re.findall(r"A\.inDiffPair\s*\(\s*'([^']*)'\s*\)", cond):
                skews[n] = val
            for n in re.findall(r'A\.inDiffPair\s*\(\s*"([^"]*)"\s*\)', cond):
                skews[n] = val
    return widths, skews


def parse_pro(path):
    out = {"clearance": 200000, "track": 250000, "via": 800000, "drill": 400000,
           "min_track": 150000, "min_via": 400000, "min_drill": 300000,
           "hole2hole": 250000, "hole_clear": 0, "edge_clear": 0}
    try:
        pro = json.load(open(path, encoding="utf-8"))
    except Exception:
        return out
    ds = pro.get("board", {}).get("design_settings", {})
    r = ds.get("rules", {})
    def g(k, d):
        v = r.get(k)
        return int(round(float(v) * MM)) if v is not None else d
    out["min_track"] = g("min_track_width", out["min_track"])
    out["min_via"] = g("min_via_diameter", out["min_via"])
    out["min_drill"] = g("min_through_hole_diameter", out["min_drill"])
    out["hole2hole"] = g("min_hole_to_hole", out["hole2hole"])
    out["hole_clear"] = g("min_hole_clearance", 0)
    out["edge_clear"] = g("min_copper_edge_clearance", 0)
    cls = pro.get("net_settings", {}).get("classes", [])
    cl = [c.get("clearance", 0.2) for c in cls if c.get("clearance") is not None]
    tw = [c.get("track_width", 0.25) for c in cls if c.get("track_width") is not None]
    vd = [c.get("via_diameter", 0.8) for c in cls if c.get("via_diameter") is not None]
    vr = [c.get("via_drill", 0.4) for c in cls if c.get("via_drill") is not None]
    if cl:
        out["clearance"] = int(round(max(cl) * MM))
    if tw:
        out["track"] = int(round(min(tw) * MM))
    if vd:
        out["via"] = int(round(min(vd) * MM))
    if vr:
        out["drill"] = int(round(min(vr) * MM))
    out["clearance"] = max(out["clearance"], g("min_clearance", 0))
    return out


# ------------------------------------------------------------ geometry ----

class Grid(object):
    def __init__(self, x0, y0, w, h, pitch):
        self.x0, self.y0, self.W, self.H, self.p = x0, y0, w, h, pitch
        self.N = w * h

    def cx(self, x):
        return int(round((x - self.x0) / float(self.p)))

    def cy(self, y):
        return int(round((y - self.y0) / float(self.p)))

    def wx(self, cx):
        return int(self.x0 + cx * self.p)

    def wy(self, cy):
        return int(self.y0 + cy * self.p)


def win(g, xmin, xmax, ymin, ymax):
    a = max(0, int(math.floor((xmin - g.x0) / g.p)))
    b = min(g.W - 1, int(math.ceil((xmax - g.x0) / g.p)))
    c = max(0, int(math.floor((ymin - g.y0) / g.p)))
    d = min(g.H - 1, int(math.ceil((ymax - g.y0) / g.p)))
    if b < a or d < c:
        return None
    return a, b, c, d


def apply_mask(arr, g, a, b, c, d, mask, val):
    sub = arr[c:d + 1, a:b + 1]
    cur = sub[mask]
    if val < 0:
        sub[mask] = val
    else:
        sub[mask] = np.where((cur == 0) | (cur == val), val, -1)


def mark_disk(arr, g, cx, cy, r, val):
    if r <= 0:
        return
    wd = win(g, cx - r, cx + r, cy - r, cy + r)
    if not wd:
        return
    a, b, c, d = wd
    xs = (g.x0 + np.arange(a, b + 1) * g.p).astype(np.float64) - cx
    ys = (g.y0 + np.arange(c, d + 1) * g.p).astype(np.float64) - cy
    mask = (xs[None, :] ** 2 + ys[:, None] ** 2) <= r * r
    apply_mask(arr, g, a, b, c, d, mask, val)


def mark_capsule(arr, g, x1, y1, x2, y2, r, val):
    if r <= 0:
        return
    wd = win(g, min(x1, x2) - r, max(x1, x2) + r, min(y1, y2) - r, max(y1, y2) + r)
    if not wd:
        return
    a, b, c, d = wd
    xs = (g.x0 + np.arange(a, b + 1) * g.p).astype(np.float64)
    ys = (g.y0 + np.arange(c, d + 1) * g.p).astype(np.float64)
    px = xs[None, :] - x1
    py = ys[:, None] - y1
    dx, dy = float(x2 - x1), float(y2 - y1)
    ln = dx * dx + dy * dy
    if ln <= 0:
        dist2 = px ** 2 + py ** 2
    else:
        t = np.clip((px * dx + py * dy) / ln, 0.0, 1.0)
        dist2 = (px - t * dx) ** 2 + (py - t * dy) ** 2
    mask = dist2 <= r * r
    apply_mask(arr, g, a, b, c, d, mask, val)


def mark_rrect(arr, g, cx, cy, sx, sy, ang, r, val):
    """rotated rectangle sx x sy centred at cx,cy, inflated by r (rounded)."""
    hx, hy = sx / 2.0, sy / 2.0
    rad = math.radians(ang)
    ca, sa = math.cos(rad), math.sin(rad)
    ext = math.hypot(hx, hy) + r
    wd = win(g, cx - ext, cx + ext, cy - ext, cy + ext)
    if not wd:
        return
    a, b, c, d = wd
    xs = (g.x0 + np.arange(a, b + 1) * g.p).astype(np.float64) - cx
    ys = (g.y0 + np.arange(c, d + 1) * g.p).astype(np.float64) - cy
    X = xs[None, :] + 0 * ys[:, None]
    Y = ys[:, None] + 0 * xs[None, :]
    lx = X * ca + Y * sa
    ly = -X * sa + Y * ca
    dx = np.abs(lx) - hx
    dy = np.abs(ly) - hy
    dx = np.maximum(dx, 0.0)
    dy = np.maximum(dy, 0.0)
    mask = (dx * dx + dy * dy) <= r * r + 1e-9 if r > 0 else (dx + dy == 0.0)
    if r <= 0:
        mask = (dx <= 0) & (dy <= 0)
    apply_mask(arr, g, a, b, c, d, mask, val)


def fill_loops(arr, g, loops, val):
    """even-odd fill of closed loops (nm coords), windowed to their bbox."""
    edges = []
    xmin = ymin = 1 << 62
    xmax = ymax = -(1 << 62)
    for pts in loops:
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            if y1 != y2:
                edges.append((x1, y1, x2, y2))
            if x1 < xmin: xmin = x1
            if x1 > xmax: xmax = x1
            if y1 < ymin: ymin = y1
            if y1 > ymax: ymax = y1
    if not edges:
        return
    wd = win(g, xmin, xmax, ymin, ymax)
    if not wd:
        return
    a, b, c, d = wd
    E = np.array(edges, dtype=np.float64)
    x1, y1, x2, y2 = E[:, 0], E[:, 1], E[:, 2], E[:, 3]
    ylo = np.minimum(y1, y2)
    yhi = np.maximum(y1, y2)
    cols = g.x0 + np.arange(a, b + 1) * g.p
    for row in range(c, d + 1):
        yc = g.y0 + row * g.p
        sel = (ylo <= yc) & (yhi > yc)
        if not sel.any():
            continue
        xa, ya, xb, yb = x1[sel], y1[sel], x2[sel], y2[sel]
        xi = np.sort(xa + (yc - ya) * (xb - xa) / (yb - ya))
        cnt = np.searchsorted(xi, cols, side='right')
        mask = (cnt % 2) == 1
        if not mask.any():
            continue
        sub = arr[row, a:b + 1]
        cur = sub[mask]
        if val < 0:
            sub[mask] = val
        else:
            sub[mask] = np.where((cur == 0) | (cur == val), val, -1)


def loop_cells(g, loops):
    """flat indices of cells whose centre is inside the loops (even-odd)."""
    edges = []
    xmin = ymin = 1 << 62
    xmax = ymax = -(1 << 62)
    for pts in loops:
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            if y1 != y2:
                edges.append((x1, y1, x2, y2))
            if x1 < xmin: xmin = x1
            if x1 > xmax: xmax = x1
            if y1 < ymin: ymin = y1
            if y1 > ymax: ymax = y1
    out = []
    if not edges:
        return out
    wd = win(g, xmin, xmax, ymin, ymax)
    if not wd:
        return out
    a, b, c, d = wd
    E = np.array(edges, dtype=np.float64)
    x1, y1, x2, y2 = E[:, 0], E[:, 1], E[:, 2], E[:, 3]
    ylo = np.minimum(y1, y2)
    yhi = np.maximum(y1, y2)
    cols = g.x0 + np.arange(a, b + 1) * g.p
    for row in range(c, d + 1):
        yc = g.y0 + row * g.p
        sel = (ylo <= yc) & (yhi > yc)
        if not sel.any():
            continue
        xa, ya, xb, yb = x1[sel], y1[sel], x2[sel], y2[sel]
        xi = np.sort(xa + (yc - ya) * (xb - xa) / (yb - ya))
        cnt = np.searchsorted(xi, cols, side='right')
        idx = np.nonzero((cnt % 2) == 1)[0]
        base = row * g.W + a
        for k in idx:
            out.append(base + int(k))
    return out


def mark_polyset(arr, g, ps, val):
    for i in range(ps.OutlineCount()):
        loops = []
        ch = ps.Outline(i)
        loops.append([(ch.CPoint(j).x, ch.CPoint(j).y) for j in range(ch.PointCount())])
        for h in range(ps.HoleCount(i)):
            ch2 = ps.Hole(i, h)
            loops.append([(ch2.CPoint(j).x, ch2.CPoint(j).y) for j in range(ch2.PointCount())])
        fill_loops(arr, g, loops, val)


def poly_inside(g, loops):
    """scanline fill: boolean H x W, True inside the (outer) loops."""
    inside = np.zeros((g.H, g.W), dtype=bool)
    edges = []
    for pts in loops:
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            if y1 != y2:
                edges.append((x1, y1, x2, y2))
    if not edges:
        inside[:] = True
        return inside
    E = np.array(edges, dtype=np.float64)
    x1, y1, x2, y2 = E[:, 0], E[:, 1], E[:, 2], E[:, 3]
    ylo = np.minimum(y1, y2)
    yhi = np.maximum(y1, y2)
    for row in range(g.H):
        yc = g.y0 + row * g.p
        sel = (ylo <= yc) & (yhi > yc)
        if not sel.any():
            continue
        xa, ya, xb, yb = x1[sel], y1[sel], x2[sel], y2[sel]
        xi = xa + (yc - ya) * (xb - xa) / (yb - ya)
        xi = np.sort(xi)
        cols = g.x0 + np.arange(g.W) * g.p
        cnt = np.searchsorted(xi, cols, side='right')
        inside[row] = (cnt % 2) == 1
    return inside


# ----------------------------------------------------------------- A* ----

SQ2 = 1.41421356


def astar(grid, freeb, srcs, dsts, dbox, via_cost, maxpop, deadline, nlayers=2):
    """srcs/dsts: sets of node ids (layer*N + idx). returns list of nodes or None."""
    W, H, N, p = grid.W, grid.H, grid.N, grid.p
    bx0, bx1, by0, by1 = dbox
    dxlo, dxhi, dylo, dyhi = None, None, None, None
    for nd in dsts:
        i = nd % N
        x, y = i % W, i // W
        if dxlo is None:
            dxlo = dxhi = x
            dylo = dyhi = y
        else:
            if x < dxlo: dxlo = x
            if x > dxhi: dxhi = x
            if y < dylo: dylo = y
            if y > dyhi: dyhi = y
    wgt = 1.35

    def h(i):
        x, y = i % W, i // W
        ax = dxlo - x if x < dxlo else (x - dxhi if x > dxhi else 0)
        ay = dylo - y if y < dylo else (y - dyhi if y > dyhi else 0)
        if ax > ay:
            ax, ay = ay, ax
        return (ay - ax + ax * SQ2) * p * wgt

    g = {}
    par = {}
    heap = []
    cnt = 0
    for s in srcs:
        g[s] = 0.0
        heapq.heappush(heap, (h(s % N), 0, s))
    pops = 0
    hp = heapq.heappop
    hpush = heapq.heappush
    dcost = (p, p, p, p, p * SQ2, p * SQ2, p * SQ2, p * SQ2)
    found = None
    tick = 0
    while heap:
        f, _, cur = hp(heap)
        if cur in dsts:
            found = cur
            break
        pops += 1
        if pops > maxpop:
            return None
        tick += 1
        if tick >= 4096:
            tick = 0
            if time.time() > deadline:
                return None
        gc = g[cur]
        if f - h(cur % N) > gc + 1e-6:
            continue
        lay = cur // N
        i = cur - lay * N
        x = i % W
        y = i // W
        fb = freeb[lay]
        xg0 = x > bx0
        xg1 = x < bx1
        yg0 = y > by0
        yg1 = y < by1
        for k in range(8):
            if k == 0:
                if not xg1: continue
                ni = i + 1
            elif k == 1:
                if not xg0: continue
                ni = i - 1
            elif k == 2:
                if not yg1: continue
                ni = i + W
            elif k == 3:
                if not yg0: continue
                ni = i - W
            elif k == 4:
                if not (xg1 and yg1): continue
                ni = i + 1 + W
            elif k == 5:
                if not (xg0 and yg1): continue
                ni = i - 1 + W
            elif k == 6:
                if not (xg1 and yg0): continue
                ni = i + 1 - W
            else:
                if not (xg0 and yg0): continue
                ni = i - 1 - W
            if not (fb[ni] & 1):
                continue
            if k >= 4:
                # don't cut corners through blocked cells
                if k == 4:
                    if not (fb[i + 1] & 1) and not (fb[i + W] & 1): continue
                elif k == 5:
                    if not (fb[i - 1] & 1) and not (fb[i + W] & 1): continue
                elif k == 6:
                    if not (fb[i + 1] & 1) and not (fb[i - W] & 1): continue
                else:
                    if not (fb[i - 1] & 1) and not (fb[i - W] & 1): continue
            nn = lay * N + ni
            ng = gc + dcost[k]
            og = g.get(nn)
            if og is None or ng < og - 1e-9:
                g[nn] = ng
                par[nn] = cur
                hpush(heap, (ng + h(ni), cnt, nn))
                cnt += 1
        # via
        if nlayers > 1 and (freeb[0][i] & 2) and (freeb[1][i] & 2):
            nl = 1 - lay
            nn = nl * N + i
            ng = gc + via_cost
            og = g.get(nn)
            if og is None or ng < og - 1e-9:
                g[nn] = ng
                par[nn] = cur
                hpush(heap, (ng + h(i), cnt, nn))
                cnt += 1
    if found is None:
        return None
    path = [found]
    while path[-1] in par:
        path.append(par[path[-1]])
    path.reverse()
    return path


# --------------------------------------------------------------- main ----

class Router(object):
    def __init__(self, src):
        self.src = src
        base = os.path.splitext(src)[0]
        self.widths_rule, self.skews = parse_dru(base + ".kicad_dru")
        self.pro = parse_pro(base + ".kicad_pro")
        self.board = pcbnew.LoadBoard(src)
        self.clear = self.pro["clearance"]
        self.via_d = max(self.pro["via"], self.pro["min_via"])
        self.via_drill = max(self.pro["drill"], self.pro["min_drill"])
        if self.via_d - self.via_drill < 200000:
            self.via_d = self.via_drill + 200000
        self.h2h = self.pro["hole2hole"]
        self.hole_clear = self.pro["hole_clear"]
        self.edge_clear = self.pro["edge_clear"]
        self.collect()

    # ---- board data
    def collect(self):
        b = self.board
        self.layers = [F_CU, B_CU]
        cu = [l for l in b.GetEnabledLayers().CuStack()]
        if len(cu) >= 2:
            self.layers = [cu[0], cu[-1]]
        self.nlayers = 2 if len(cu) >= 2 else 1
        self.pads = []        # (netname, pos, layers set, shape info)
        self.npth = []
        nets = {}
        for fp in b.GetFootprints():
            for pad in fp.Pads():
                if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                    ds = pad.GetDrillSize()
                    self.npth.append((pad.GetPosition(), max(ds[0], ds[1])))
                    continue
                name = pad.GetNetname()
                onf = pad.IsOnLayer(self.layers[0])
                onb = pad.IsOnLayer(self.layers[1]) if self.nlayers > 1 else False
                if not onf and not onb:
                    continue
                p = {"net": name, "pos": pad.GetPosition(), "size": pad.GetSize(),
                     "shape": pad.GetShape(), "ang": pad.GetOrientationDegrees(),
                     "drill": pad.GetDrillSize(), "f": onf, "b": onb,
                     "obj": pad}
                self.pads.append(p)
                if name:
                    nets.setdefault(name, []).append(p)
        self.netpads = nets
        # net widths
        self.netwidth = {}
        minw = self.pro["min_track"]
        for n in nets:
            w = max(minw, self.widths_rule.get(n, 0))
            if w <= 0:
                w = 250000
            self.netwidth[n] = w
        # diff pairs
        self.pairs = {}
        for pname, skew in self.skews.items():
            members = []
            for suf in ("+", "-", "_P", "_N", "P", "N"):
                cand = pname + suf
                if cand in nets:
                    members.append(cand)
            if len(members) == 2:
                self.pairs[pname] = (members, skew)
        self.pairnet = {}
        for pname, (ms, sk) in self.pairs.items():
            for m in ms:
                self.pairnet[m] = pname

    def setup_grid(self):
        b = self.board
        bb = b.GetBoardEdgesBoundingBox()
        if bb.GetWidth() <= 0:
            bb = b.ComputeBoundingBox(False)
        x0, y0 = bb.GetLeft(), bb.GetTop()
        x1, y1 = bb.GetRight(), bb.GetBottom()
        wnm, hnm = x1 - x0, y1 - y0
        area_mm2 = (wnm / MM) * (hnm / MM)
        minfeat = min([self.pro["min_track"]] + list(self.netwidth.values()) + [self.clear])
        pitch = 100000
        if minfeat < 220000 and area_mm2 < 6000:
            pitch = 50000
        while (wnm / pitch + 4) * (hnm / pitch + 4) > 3.2e6:
            pitch += 25000
        m = 2 * pitch
        self.g = Grid(x0 - m, y0 - m, int(wnm / pitch) + 5, int(hnm / pitch) + 5, pitch)
        self.pitch = pitch

    # ---- obstacle maps
    def build_static(self):
        g = self.g
        self.wgroups = sorted(set(self.netwidth.values()))
        self.netid = {}
        for i, n in enumerate(sorted(self.netpads.keys())):
            self.netid[n] = i + 1
        hwmax = max(self.wgroups) / 2.0
        self.hwmax = hwmax
        self.safe = 0.10 * self.pitch
        ds = self.board.GetDesignSettings()
        try:
            self.mask_exp = max(0, int(ds.m_SolderMaskExpansion))
            self.mask_minw = max(0, int(ds.m_SolderMaskMinWidth))
        except Exception:
            self.mask_exp, self.mask_minw = 0, 0
        self.occT = {}
        for w in self.wgroups:
            self.occT[w] = [np.zeros((g.H, g.W), dtype=np.int16) for _ in range(self.nlayers)]
        self.occV = [np.zeros((g.H, g.W), dtype=np.int16) for _ in range(self.nlayers)]
        # board outline
        poly = pcbnew.SHAPE_POLY_SET()
        ok = False
        try:
            ok = self.board.GetBoardPolygonOutlines(poly)
        except Exception:
            ok = False
        loops = []
        holes = []
        if poly.OutlineCount() > 0:
            for i in range(poly.OutlineCount()):
                ch = poly.Outline(i)
                pts = [(ch.CPoint(j).x, ch.CPoint(j).y) for j in range(ch.PointCount())]
                if len(pts) >= 3:
                    loops.append(pts)
                for hh in range(poly.HoleCount(i)):
                    ch2 = poly.Hole(i, hh)
                    p2 = [(ch2.CPoint(j).x, ch2.CPoint(j).y) for j in range(ch2.PointCount())]
                    if len(p2) >= 3:
                        holes.append(p2)
        if loops:
            inside = poly_inside(g, loops)
            for hp in holes:
                hin = poly_inside(g, [hp])
                inside &= ~hin
            outside = ~inside
            for w in self.wgroups:
                for l in range(self.nlayers):
                    self.occT[w][l][outside] = -1
            for l in range(self.nlayers):
                self.occV[l][outside] = -1
            # edge clearance: capsules along outline
            for pts in loops + holes:
                for i in range(len(pts)):
                    x1, y1 = pts[i]
                    x2, y2 = pts[(i + 1) % len(pts)]
                    for w in self.wgroups:
                        r = w / 2.0 + self.edge_clear + self.safe
                        for l in range(self.nlayers):
                            mark_capsule(self.occT[w][l], g, x1, y1, x2, y2, r, -1)
                    rv = self.via_d / 2.0 + self.edge_clear + self.safe
                    for l in range(self.nlayers):
                        mark_capsule(self.occV[l], g, x1, y1, x2, y2, rv, -1)
        # NPTH holes
        for pos, dia in self.npth:
            for w in self.wgroups:
                r = dia / 2.0 + max(self.clear, self.hole_clear) + w / 2.0 + self.safe
                for l in range(self.nlayers):
                    mark_disk(self.occT[w][l], g, pos.x, pos.y, r, -1)
            rv = dia / 2.0 + max(self.h2h, self.clear, self.hole_clear) + self.via_d / 2.0 + self.safe
            for l in range(self.nlayers):
                mark_disk(self.occV[l], g, pos.x, pos.y, rv, -1)
        # solder-mask apertures that belong to no copper pad: a track or via under one of
        # them bridges in the mask, which DRC counts.
        self.mark_mask()
        # pads
        for p in self.pads:
            nid = self.netid.get(p["net"], -1) if p["net"] else -1
            self.mark_pad(p, nid)
        self.base_occT = {w: [a.copy() for a in self.occT[w]] for w in self.wgroups}
        self.base_occV = [a.copy() for a in self.occV]

    def mark_mask(self):
        g = self.g
        maskpair = [(pcbnew.F_Mask, 0), (pcbnew.B_Mask, 1)]
        mr = self.mask_exp + self.mask_minw + self.safe + 1000
        items = list(self.board.GetDrawings())
        for fp in self.board.GetFootprints():
            items += list(fp.GraphicalItems())
            for pad in fp.Pads():
                if pad.IsOnLayer(self.layers[0]) or (self.nlayers > 1 and pad.IsOnLayer(self.layers[-1])):
                    continue
                for ml, li in maskpair:
                    if li >= self.nlayers or not pad.IsOnLayer(ml):
                        continue
                    for w in self.wgroups + [None]:
                        ps = pcbnew.SHAPE_POLY_SET()
                        rr = mr + (self.via_d / 2.0 if w is None else w / 2.0)
                        try:
                            pad.TransformShapeToPolygon(ps, ml, int(rr), 5000, pcbnew.ERROR_OUTSIDE)
                        except Exception:
                            continue
                        mark_polyset(self.occV[li] if w is None else self.occT[w][li], g, ps, -1)
        for it in items:
            try:
                lay = it.GetLayer()
            except Exception:
                continue
            for ml, li in maskpair:
                if lay != ml or li >= self.nlayers:
                    continue
                for w in self.wgroups + [None]:
                    ps = pcbnew.SHAPE_POLY_SET()
                    rr = mr + (self.via_d / 2.0 if w is None else w / 2.0)
                    try:
                        it.TransformShapeToPolygon(ps, ml, int(rr), 5000, pcbnew.ERROR_OUTSIDE)
                    except Exception:
                        continue
                    mark_polyset(self.occV[li] if w is None else self.occT[w][li], g, ps, -1)

    def pad_poly(self, pad, layer, clearance):
        ps = pcbnew.SHAPE_POLY_SET()
        try:
            pad.TransformShapeToPolygon(ps, layer, int(max(0, clearance)), 5000, pcbnew.ERROR_OUTSIDE)
            if ps.OutlineCount() > 0:
                return ps
        except Exception:
            pass
        return None

    def mark_pad(self, p, nid):
        g = self.g
        pad = p["obj"]
        pos = p["pos"]
        sx, sy = p["size"][0], p["size"][1]
        sh = p["shape"]
        ang = p["ang"]
        lays = []
        if p["f"]:
            lays.append(0)
        if p["b"] and self.nlayers > 1:
            lays.append(1)
        for w in self.wgroups:
            r = max(self.clear, self.mask_exp + self.mask_minw) + w / 2.0 + self.safe
            for l in lays:
                arr = self.occT[w][l]
                ps = self.pad_poly(pad, self.layers[l], r)
                if ps is not None:
                    mark_polyset(arr, g, ps, nid)
                elif sh == pcbnew.PAD_SHAPE_CIRCLE:
                    mark_disk(arr, g, pos.x, pos.y, sx / 2.0 + r, nid)
                else:
                    mark_rrect(arr, g, pos.x, pos.y, sx, sy, ang, r, nid)
        dr = max(p["drill"][0], p["drill"][1])
        if dr > 0:
            for w in self.wgroups:
                rr = dr / 2.0 + self.hole_clear + w / 2.0 + self.safe
                for l in range(self.nlayers):
                    mark_disk(self.occT[w][l], g, pos.x, pos.y, rr, nid)
        ext = max(sx, sy) / 2.0
        rv = self.clear + self.via_d / 2.0 + self.safe
        lv = list(range(self.nlayers)) if (dr > 0 or len(lays) == self.nlayers) else lays
        for l in lv:
            ps = self.pad_poly(pad, self.layers[l if l < self.nlayers else 0], rv)
            if ps is not None:
                mark_polyset(self.occV[l], g, ps, nid)
            else:
                mark_disk(self.occV[l], g, pos.x, pos.y, ext + rv, nid)
        if dr > 0:
            rv2 = dr / 2.0 + max(self.h2h, self.hole_clear) + self.via_drill / 2.0 + self.safe
            for l in range(self.nlayers):
                mark_disk(self.occV[l], g, pos.x, pos.y, rv2, nid)

    def mark_track(self, x1, y1, x2, y2, layer, width, nid):
        g = self.g
        for w in self.wgroups:
            r = width / 2.0 + self.clear + w / 2.0 + self.safe
            mark_capsule(self.occT[w][layer], g, x1, y1, x2, y2, r, nid)
        rv = width / 2.0 + self.clear + self.via_d / 2.0 + self.safe
        mark_capsule(self.occV[layer], g, x1, y1, x2, y2, rv, nid)

    def mark_via(self, x, y, nid):
        g = self.g
        for w in self.wgroups:
            r = self.via_d / 2.0 + max(self.clear, self.hole_clear) + w / 2.0 + self.safe
            for l in range(self.nlayers):
                mark_disk(self.occT[w][l], g, x, y, r, nid)
        rv = self.via_d / 2.0 + max(self.clear, self.h2h) + self.via_d / 2.0 + self.safe
        for l in range(self.nlayers):
            mark_disk(self.occV[l], g, x, y, rv, nid)

    # ---- free maps
    def freemap(self, net):
        nid = self.netid[net]
        w = self.netwidth[net]
        out = []
        for l in range(self.nlayers):
            a = self.occT[w][l]
            v = self.occV[l]
            f = ((a == 0) | (a == nid)).astype(np.uint8)
            f |= (((v == 0) | (v == nid)).astype(np.uint8) << 1)
            out.append(f.tobytes())
        return out

    # ---- pad cells
    def pad_cells(self, p):
        """grid cells whose centre lies inside the pad copper."""
        g = self.g
        pad = p["obj"]
        pos = p["pos"]
        lay = self.layers[0] if p["f"] else self.layers[-1]
        cells = []
        ps = self.pad_poly_in(pad, lay)
        if ps is not None:
            for i in range(ps.OutlineCount()):
                loops = []
                ch = ps.Outline(i)
                loops.append([(ch.CPoint(j).x, ch.CPoint(j).y) for j in range(ch.PointCount())])
                for h in range(ps.HoleCount(i)):
                    ch2 = ps.Hole(i, h)
                    loops.append([(ch2.CPoint(j).x, ch2.CPoint(j).y) for j in range(ch2.PointCount())])
                cells += loop_cells(g, loops)
        p["exact"] = bool(cells)
        if not cells:
            cx, cy = g.cx(pos.x), g.cy(pos.y)
            if 0 <= cx < g.W and 0 <= cy < g.H:
                cells.append(cy * g.W + cx)
        return cells

    def pad_poly_in(self, pad, layer):
        ps = pcbnew.SHAPE_POLY_SET()
        try:
            pad.TransformShapeToPolygon(ps, layer, 0, 5000, pcbnew.ERROR_INSIDE)
            if ps.OutlineCount() > 0:
                return ps
        except Exception:
            pass
        return None


def main():
    src, dst = sys.argv[1], sys.argv[2]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        R = Router(src)
        pcbnew.SaveBoard(dst, R.board)
    except Exception:
        import shutil, traceback
        traceback.print_exc()
        try:
            shutil.copy(src, dst)
        except Exception:
            pass
        return
    try:
        R.setup_grid()
        R.build_static()
        import engine
        engine.run(R, dst)
    except Exception:
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
