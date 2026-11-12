#!/usr/bin/env python3
"""Grid A* router for 2-layer KiCad boards. Usage: route.py in.kicad_pcb out.kicad_pcb"""
import sys, os, re, time, math, heapq, json
from array import array
import numpy as np
import pcbnew

T0 = time.time()
TIME_LIMIT = float(os.environ.get("ROUTE_TIME", "410"))  # seconds of total budget we allow ourselves


def elapsed():
    return time.time() - T0


def log(*a):
    print("[%6.1f]" % elapsed(), *a, file=sys.stderr, flush=True)


MM = 1e6


# ---------------------------------------------------------------- rules
def parse_dru(path):
    widths, skews = {}, {}
    if not os.path.exists(path):
        return widths, skews
    txt = open(path).read()
    # split into rules
    for m in re.finditer(r'\(rule\s+"[^"]*"(.*?)\n\s*\(constraint\s+(\w+)\s+\((min|max)\s+([\d.]+)mm\)\)\)', txt, re.S):
        body, kind, mm_, val = m.group(1), m.group(2), m.group(3), float(m.group(4))
        if kind == "track_width":
            for n in re.findall(r"A\.NetName == '((?:[^'\\]|\\.)*)'", body):
                widths[n] = max(widths.get(n, 0), val)
        elif kind == "skew":
            for n in re.findall(r"inDiffPair\('([^']*)'\)", body):
                skews[n] = val
    return widths, skews


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


# ---------------------------------------------------------------- geometry helpers
def poly_edges(polyset):
    """Return list of (N,2) arrays of outlines + holes in mm."""
    out = []
    for i in range(polyset.OutlineCount()):
        chains = [polyset.Outline(i)] + [polyset.Hole(i, h) for h in range(polyset.HoleCount(i))]
        for ch in chains:
            n = ch.PointCount()
            pts = np.array([[ch.CPoint(k).x / MM, ch.CPoint(k).y / MM] for k in range(n)])
            if len(pts) >= 3:
                out.append(pts)
    return out


def dist_poly(px, py, chains):
    """distance from points to polygon set (0 inside). chains: list of closed point arrays; even-odd."""
    inside = np.zeros(px.shape, bool)
    d2 = np.full(px.shape, np.inf)
    for pts in chains:
        x1 = pts[:, 0]; y1 = pts[:, 1]
        x2 = np.roll(x1, -1); y2 = np.roll(y1, -1)
        for k in range(len(x1)):
            ax, ay, bx, by = x1[k], y1[k], x2[k], y2[k]
            # crossing test
            cond = ((ay > py) != (by > py))
            if by != ay:
                xint = ax + (py - ay) * (bx - ax) / (by - ay)
                inside ^= cond & (px < xint)
            dx, dy = bx - ax, by - ay
            L = dx * dx + dy * dy
            if L > 0:
                t = np.clip(((px - ax) * dx + (py - ay) * dy) / L, 0, 1)
            else:
                t = 0
            qx = ax + t * dx - px; qy = ay + t * dy - py
            d2 = np.minimum(d2, qx * qx + qy * qy)
    d = np.sqrt(d2)
    d[inside] = 0
    return d


def inside_poly(px, py, chains):
    inside = np.zeros(px.shape, bool)
    for pts in chains:
        x1 = pts[:, 0]; y1 = pts[:, 1]
        x2 = np.roll(x1, -1); y2 = np.roll(y1, -1)
        for k in range(len(x1)):
            ax, ay, bx, by = x1[k], y1[k], x2[k], y2[k]
            if by == ay:
                continue
            cond = ((ay > py) != (by > py))
            xint = ax + (py - ay) * (bx - ax) / (by - ay)
            inside ^= cond & (px < xint)
    return inside


def dist_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    if L > 0:
        t = np.clip(((px - ax) * dx + (py - ay) * dy) / L, 0, 1)
    else:
        t = 0
    qx = ax + t * dx - px; qy = ay + t * dy - py
    return np.sqrt(qx * qx + qy * qy)


def _pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


def seg_seg_dist(ax, ay, bx, by, cx, cy, dx, dy):
    def orient(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    o1 = orient(ax, ay, bx, by, cx, cy); o2 = orient(ax, ay, bx, by, dx, dy)
    o3 = orient(cx, cy, dx, dy, ax, ay); o4 = orient(cx, cy, dx, dy, bx, by)
    if ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)) and o1 != 0 and o2 != 0 and o3 != 0 and o4 != 0:
        return 0.0
    return min(_pt_seg(ax, ay, cx, cy, dx, dy), _pt_seg(bx, by, cx, cy, dx, dy),
               _pt_seg(cx, cy, ax, ay, bx, by), _pt_seg(dx, dy, ax, ay, bx, by))


# ---------------------------------------------------------------- the router
class Router:
    def __init__(self, path):
        self.path = path
        self.board = pcbnew.LoadBoard(path)
        b = self.board
        base = os.path.splitext(path)[0]
        self.dru_w, self.skews = parse_dru(base + ".kicad_dru")
        ds = b.GetDesignSettings()
        self.ds = ds
        cu = list(b.GetEnabledLayers().CuStack())
        self.layers = [cu[0], cu[-1]]  # F, B
        self.L = 2
        # clearance
        clr = ds.m_MinClearance / MM
        for name, nc in b.GetAllNetClasses().items():
            try:
                clr = max(clr, nc.GetClearance() / MM)
            except Exception:
                pass
        self.clr = clr + 0.008
        self.hole_clr = max(ds.m_HoleClearance / MM, 0) + 0.02
        self.h2h = max(ds.m_HoleToHoleMin / MM, 0.25) + 0.02
        self.edge_clr = max(ds.m_CopperEdgeClearance / MM, 0.0) + 0.05
        self.min_w = ds.m_TrackMinWidth / MM
        drill = max(ds.m_MinThroughDrill / MM, 0.3)
        ann = ds.m_ViasMinAnnularWidth / MM
        self.via_drill = drill
        self.via_d = max(ds.m_ViasMinSize / MM, drill + 2 * max(ann, 0.1))
        log("clr", self.clr, "via", self.via_d, self.via_drill, "minw", self.min_w)

        # nets / pads
        self.pads = []  # dicts
        nets = {}
        for fp in b.GetFootprints():
            for p in fp.Pads():
                d = {}
                d["net"] = p.GetNetCode()
                d["name"] = p.GetNetname()
                pos = p.GetPosition()
                d["x"], d["y"] = pos.x / MM, pos.y / MM
                d["lay"] = [li for li, l in enumerate(self.layers) if p.IsOnLayer(l)]
                d["chains"] = {}
                for li in d["lay"]:
                    try:
                        ps = p.GetEffectivePolygon(self.layers[li], pcbnew.ERROR_INSIDE)
                        d["chains"][li] = poly_edges(ps)
                    except Exception:
                        d["chains"][li] = []
                hole = 0
                if p.HasHole():
                    hs = p.GetDrillSize()
                    hole = max(hs.x, hs.y) / MM
                d["hole"] = hole
                d["npth"] = p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
                lc = 0
                try:
                    v = p.GetLocalClearance()
                    if v is not None:
                        lc = v / MM
                except Exception:
                    pass
                me = 0
                try:
                    me = max(p.GetSolderMaskExpansion(l) for l in (self.layers[li] for li in d["lay"])) / MM if d["lay"] else 0
                except Exception:
                    me = 0
                d["clr"] = max(self.clr, lc + 0.008, me + 0.01)
                # if copper on layers is empty (npth) chains empty
                self.pads.append(d)
                if d["net"] > 0 and d["lay"]:
                    nets.setdefault(d["net"], []).append(len(self.pads) - 1)
        # copper graphics (board drawings, footprint graphics, text) as obstacles
        gitems = list(b.GetDrawings())
        for fp in b.GetFootprints():
            gitems += list(fp.GraphicalItems())
        for gi in gitems:
            for li, (l, ml) in enumerate(zip(self.layers, (pcbnew.F_Mask, pcbnew.B_Mask))):
                try:
                    if gi.IsOnLayer(l):
                        ul = l
                    elif gi.IsOnLayer(ml):
                        ul = ml
                    else:
                        continue
                    ps = pcbnew.SHAPE_POLY_SET()
                    gi.TransformShapeToPolygon(ps, ul, 0, 5000, pcbnew.ERROR_OUTSIDE)
                    ch = poly_edges(ps)
                    if not ch:
                        continue
                    try:
                        gn = gi.GetNetCode()
                    except Exception:
                        gn = 0
                    self.pads.append({"net": gn if gn > 0 else -1, "name": "", "x": 0, "y": 0, "lay": [li],
                                      "chains": {li: ch}, "hole": 0, "npth": False, "clr": self.clr, "gfx": True})
                except Exception as e:
                    pass
        self.netname = {}
        for code in nets:
            self.netname[code] = b.FindNet(code).GetNetname() if b.FindNet(code) else ""
        self.nets = {c: v for c, v in nets.items() if len(v) >= 2}
        # width per net
        self.width = {}
        for c in self.nets:
            nm = self.netname[c]
            w = max(self.min_w, self.dru_w.get(nm, 0), 0.15)
            self.width[c] = round(w + 0.0005, 4)
        # diff pairs
        self.pairs = []
        byname = {v: k for k, v in self.netname.items()}
        for base_, sk in self.skews.items():
            cand = [(base_ + "+", base_ + "-"), (base_ + "_P", base_ + "_N"), (base_ + "P", base_ + "N")]
            for a, c in cand:
                if a in byname and c in byname and byname[a] in self.nets and byname[c] in self.nets:
                    self.pairs.append((byname[a], byname[c], sk))
                    break
        log("nets", len(self.nets), "pairs", len(self.pairs))

        # grid
        ol = pcbnew.SHAPE_POLY_SET()
        b.GetBoardPolygonOutlines(ol)
        self.outline = poly_edges(ol)
        bb = b.GetBoardEdgesBoundingBox()
        x0, y0 = bb.GetX() / MM, bb.GetY() / MM
        W, H = bb.GetWidth() / MM, bb.GetHeight() / MM
        area = W * H
        minpad = 10
        for d in self.pads:
            for li, ch in d["chains"].items():
                for pts in ch:
                    ext = min(pts[:, 0].max() - pts[:, 0].min(), pts[:, 1].max() - pts[:, 1].min())
                    minpad = min(minpad, ext)
        pitch = max(0.05, math.sqrt(area / 500000.0))
        if self.clr < 0.2:
            pitch = min(pitch, 0.06)
        pitch = min(pitch, 0.127)
        self.p = pitch
        self.x0, self.y0 = x0, y0
        self.nx = int(W / pitch) + 2
        self.ny = int(H / pitch) + 2
        self.N = self.nx * self.ny
        log("grid", self.nx, self.ny, "pitch", pitch)
        xs = x0 + np.arange(self.nx) * pitch
        ys = y0 + np.arange(self.ny) * pitch
        self.xs, self.ys = xs, ys

        # keepout rule areas
        self.keep_tracks = [[] for _ in range(2)]
        self.keep_vias = []
        for z in b.Zones():
            if not z.GetIsRuleArea():
                continue
            ch = poly_edges(z.Outline())
            if z.GetDoNotAllowTracks():
                for li, l in enumerate(self.layers):
                    if z.IsOnLayer(l):
                        self.keep_tracks[li].append(ch)
            if z.GetDoNotAllowVias():
                self.keep_vias.append(ch)

        self.static = {}  # (kind,width) -> list per layer of int32 owner maps
        self.routes = {}  # net -> list of items: ('s', layer, x1,y1,x2,y2,w) / ('v', x,y)
        # edge distance map
        self._edge_dist()

    # ---------------------------------------------------------- static maps
    def _edge_dist(self):
        X, Y = np.meshgrid(self.xs, self.ys)
        ins = inside_poly(X, Y, self.outline)
        # distance to edges, only for cells within 2mm of edge: compute chunked
        d = np.full(X.shape, np.inf)
        for pts in self.outline:
            x1 = pts[:, 0]; y1 = pts[:, 1]
            x2 = np.roll(x1, -1); y2 = np.roll(y1, -1)
            for k in range(len(x1)):
                ax, ay, bx, by = x1[k], y1[k], x2[k], y2[k]
                lo_x, hi_x = min(ax, bx) - 3, max(ax, bx) + 3
                lo_y, hi_y = min(ay, by) - 3, max(ay, by) + 3
                i0, i1 = self._ix(lo_x), self._ix(hi_x) + 1
                j0, j1 = self._iy(lo_y), self._iy(hi_y) + 1
                if i1 <= i0 or j1 <= j0:
                    continue
                sub = dist_seg(X[j0:j1, i0:i1], Y[j0:j1, i0:i1], ax, ay, bx, by)
                d[j0:j1, i0:i1] = np.minimum(d[j0:j1, i0:i1], sub)
        d[~ins] = 0
        self.edge_d = d

    def _ix(self, x):
        return int(min(max(math.floor((x - self.x0) / self.p), 0), self.nx))

    def _iy(self, y):
        return int(min(max(math.floor((y - self.y0) / self.p), 0), self.ny))

    def window(self, x1, y1, x2, y2, r):
        i0, i1 = self._ix(min(x1, x2) - r), self._ix(max(x1, x2) + r) + 2
        j0, j1 = self._iy(min(y1, y2) - r), self._iy(max(y1, y2) + r) + 2
        i1 = min(i1, self.nx); j1 = min(j1, self.ny)
        if i1 <= i0 or j1 <= j0:
            return None
        X, Y = np.meshgrid(self.xs[i0:i1], self.ys[j0:j1])
        return (j0, j1, i0, i1, X, Y)

    @staticmethod
    def mark(owner, sl, mask, net):
        sub = owner[sl]
        cur = sub[mask]
        new = np.where(cur == 0, net, np.where(cur == net, net, -1))
        sub[mask] = new

    def static_map(self, kind, w):
        """kind 't' track of width w, 'v' via of diameter w. Returns per layer int32 owner arrays."""
        key = (kind, w)
        if key in self.static:
            return self.static[key]
        maps = []
        half = w / 2
        for li in range(2):
            own = np.zeros((self.ny, self.nx), np.int32)
            own[self.edge_d < self.edge_clr + half] = -1
            for ch in self.keep_tracks[li] if kind == 't' else self.keep_vias:
                X, Y = np.meshgrid(self.xs, self.ys)
                m = dist_poly(X, Y, ch) < half + 0.05
                own[m] = -1
            for d in self.pads:
                net = d["net"] if d["net"] > 0 else -1
                ch = d["chains"].get(li)
                if ch and not d["npth"]:
                    r = d["clr"] + half
                    allpts = np.vstack(ch)
                    win = self.window(allpts[:, 0].min(), allpts[:, 1].min(), allpts[:, 0].max(), allpts[:, 1].max(), r)
                    if win:
                        j0, j1, i0, i1, X, Y = win
                        m = dist_poly(X, Y, ch) < r
                        self.mark(own, (slice(j0, j1), slice(i0, i1)), m, net)
                if d["hole"] > 0:
                    if kind == 'v':
                        r = max(self.h2h + self.via_drill / 2, self.hole_clr + self.via_d / 2) + d["hole"] / 2
                        nn = -1
                    else:
                        r = (max(self.hole_clr, self.clr) if d["npth"] else self.hole_clr) + d["hole"] / 2 + half
                        nn = -1 if d["npth"] else net
                    win = self.window(d["x"], d["y"], d["x"], d["y"], r)
                    if win:
                        j0, j1, i0, i1, X, Y = win
                        m = np.hypot(X - d["x"], Y - d["y"]) < r
                        self.mark(own, (slice(j0, j1), slice(i0, i1)), m, nn)
            maps.append(own)
        if kind == 'v':
            both = np.where((maps[0] == maps[1]) | (maps[1] == 0), maps[0], np.where(maps[0] == 0, maps[1], -1))
            maps = [both, both]
        self.static[key] = maps
        return maps

    # ---------------------------------------------------------- dynamic
    def dyn_maps(self, net, w, exclude=()):
        """For net with width w: return (track_state per layer, via_state, who per layer, whov)
        states: 0 free,1 hard, 2 soft (other routed copper)."""
        st = self.static_map('t', w)
        sv = self.static_map('v', self.via_d)
        ts = []
        for li in range(2):
            s = np.where((st[li] == 0) | (st[li] == net), 0, 1).astype(np.uint8)
            ts.append(s)
        vs = np.where((sv[0] == 0) | (sv[0] == net), 0, 1).astype(np.uint8)
        who = [np.zeros((self.ny, self.nx), np.int32) for _ in range(2)]
        whov = np.zeros((self.ny, self.nx), np.int32)
        for on, items in self.routes.items():
            if on == net or on in exclude:
                continue
            for it in items:
                if it[0] == 's':
                    _, li, x1, y1, x2, y2, wo = it
                    for tgt, half, ww, wh in ((ts[li], w / 2, None, who[li]), (vs, self.via_d / 2, None, whov)):
                        r = self.clr + wo / 2 + half
                        win = self.window(x1, y1, x2, y2, r)
                        if not win:
                            continue
                        j0, j1, i0, i1, X, Y = win
                        m = dist_seg(X, Y, x1, y1, x2, y2) < r
                        sub = tgt[j0:j1, i0:i1]
                        sub[m & (sub == 0)] = 2
                        wh[j0:j1, i0:i1][m] = on
                else:
                    _, x, y = it
                    for li in range(2):
                        r = self.clr + self.via_d / 2 + w / 2
                        win = self.window(x, y, x, y, r)
                        if win:
                            j0, j1, i0, i1, X, Y = win
                            m = np.hypot(X - x, Y - y) < r
                            sub = ts[li][j0:j1, i0:i1]
                            sub[m & (sub == 0)] = 2
                            who[li][j0:j1, i0:i1][m] = on
                    r = max(self.clr + self.via_d, self.h2h + self.via_drill)
                    win = self.window(x, y, x, y, r)
                    if win:
                        j0, j1, i0, i1, X, Y = win
                        m = np.hypot(X - x, Y - y) < r
                        sub = vs[j0:j1, i0:i1]
                        sub[m & (sub == 0)] = 2
                        whov[j0:j1, i0:i1][m] = on
        # own vias: hole-to-hole between own vias
        for it in self.routes.get(net, []):
            if it[0] == 'v':
                pass
        return ts, vs, who, whov

    # ---------------------------------------------------------- terminals
    def pad_cells(self, pi, li, state):
        d = self.pads[pi]
        ch = d["chains"].get(li)
        if not ch:
            return []
        allpts = np.vstack(ch)
        win = self.window(allpts[:, 0].min(), allpts[:, 1].min(), allpts[:, 0].max(), allpts[:, 1].max(), 0)
        if not win:
            return []
        j0, j1, i0, i1, X, Y = win
        m = dist_poly(X, Y, ch) <= 0
        m &= state[j0:j1, i0:i1] != 1
        jj, ii = np.nonzero(m)
        cells = [(int(j + j0) * self.nx + int(i + i0)) for j, i in zip(jj, ii)]
        if not cells:
            # nearest free cell to center within small radius
            r = 0.5 * self.p + 0.3
            win = self.window(d["x"], d["y"], d["x"], d["y"], r)
            if win:
                j0, j1, i0, i1, X, Y = win
                dd = np.hypot(X - d["x"], Y - d["y"])
                dd[state[j0:j1, i0:i1] == 1] = np.inf
                dd2 = dist_poly(X, Y, ch)
                dd[dd2 > self.p * 0.75] = np.inf
                k = np.argmin(dd)
                if np.isfinite(dd.flat[k]):
                    j, i = divmod(int(k), dd.shape[1])
                    cells = [(j + j0) * self.nx + i + i0]
        return cells

    def stub_cells(self, pi, li, state, w, net):
        d = self.pads[pi]
        R = 1.2
        win = self.window(d["x"], d["y"], d["x"], d["y"], R)
        if not win:
            return []
        j0, j1, i0, i1, X, Y = win
        dd = np.hypot(X - d["x"], Y - d["y"])
        ok = (state[j0:j1, i0:i1] != 1) & (dd < R)
        jj, ii = np.nonzero(ok)
        if len(jj) == 0:
            return []
        order = np.argsort(dd[jj, ii])
        # neighbour obstacles
        obs = []
        for q in self.pads:
            if q is d or (q["net"] == net and net > 0):
                continue
            ch = q["chains"].get(li)
            if not ch or q["npth"]:
                continue
            allpts = np.vstack(ch)
            if allpts[:, 0].min() > d["x"] + R + 1 or allpts[:, 0].max() < d["x"] - R - 1 or \
               allpts[:, 1].min() > d["y"] + R + 1 or allpts[:, 1].max() < d["y"] - R - 1:
                continue
            obs.append((ch, q["clr"] + w / 2))
        res = []
        tried = 0
        for k in order:
            j, i = int(jj[k]), int(ii[k])
            x, y = X[j, i], Y[j, i]
            tried += 1
            if tried > 60:
                break
            L = math.hypot(x - d["x"], y - d["y"])
            n = max(2, int(L / 0.02) + 1)
            t = np.linspace(0, 1, n)
            px = d["x"] + t * (x - d["x"]); py = d["y"] + t * (y - d["y"])
            good = True
            for ch, r in obs:
                if dist_poly(px, py, ch).min() < r:
                    good = False
                    break
            if good and self.edge_d[j + j0, i + i0] >= self.edge_clr + w / 2:
                c = (j + j0) * self.nx + i + i0
                res.append(c)
                self.stubs[li * self.N + c] = (d["x"], d["y"])
                if len(res) >= 4:
                    break
        return res

    # ---------------------------------------------------------- A*
    def astar(self, sources, targets, tstate, vstate, soft_cost, bbox, tx, ty):
        """sources: set of state ids (layer*N+idx). targets: set of state ids. Returns path list."""
        nx, N = self.nx, self.N
        i0, j0, i1, j1 = bbox
        tcode = {}
        INF = 1e18
        g = {}
        came = {}
        pq = []
        # state lists for quick lookup
        t0b = tstate[0]; t1b = tstate[1]; vb = vstate
        ts = (t0b, t1b)
        via_cost = self.via_cost
        hw = self.hw
        txi = (tx - self.x0) / self.p
        tyi = (ty - self.y0) / self.p
        for s in sources:
            g[s] = 0.0
            idx = s % N
            x, y = idx % nx, idx // nx
            dx, dy = abs(x - txi), abs(y - tyi)
            h = (max(dx, dy) + 0.4142 * min(dx, dy)) * hw
            heapq.heappush(pq, (h, 0.0, s))
            came[s] = -1
        dirs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142))
        pen = self.pref_pen
        found = -1
        exp = 0
        maxexp = self.max_exp
        while pq:
            f, gc, s = heapq.heappop(pq)
            if gc > g.get(s, INF):
                continue
            if s in targets:
                found = s
                break
            exp += 1
            if exp > maxexp:
                break
            l = s // N
            idx = s - l * N
            x = idx % nx
            y = idx // nx
            tb = ts[l]
            for dx, dy, c in dirs:
                xx = x + dx; yy = y + dy
                if xx < i0 or xx >= i1 or yy < j0 or yy >= j1:
                    continue
                ni = yy * nx + xx
                v = tb[ni]
                if v == 1:
                    continue
                if dx and dy:
                    # avoid cutting corners through hard blocked
                    if tb[y * nx + xx] == 1 or tb[yy * nx + x] == 1:
                        continue
                cc = c
                if l == 0:
                    if dy and not dx:
                        cc *= pen
                else:
                    if dx and not dy:
                        cc *= pen
                if v == 2:
                    cc += soft_cost
                ng = gc + cc
                ns = l * N + ni
                if ng < g.get(ns, INF):
                    g[ns] = ng
                    came[ns] = s
                    ddx, ddy = abs(xx - txi), abs(yy - tyi)
                    h = (max(ddx, ddy) + 0.4142 * min(ddx, ddy)) * hw
                    heapq.heappush(pq, (ng + h, ng, ns))
            # via
            vv = vb[idx]
            if vv != 1 and ts[0][idx] != 1 and ts[1][idx] != 1:
                cc = via_cost + (soft_cost if vv == 2 else 0)
                ns = (1 - l) * N + idx
                ng = gc + cc
                if ng < g.get(ns, INF):
                    g[ns] = ng
                    came[ns] = s
                    ddx, ddy = abs(x - txi), abs(y - tyi)
                    h = (max(ddx, ddy) + 0.4142 * min(ddx, ddy)) * hw
                    heapq.heappush(pq, (ng + h, ng, ns))
        self.total_exp += exp
        if found < 0:
            return None
        path = []
        s = found
        while s != -1:
            path.append(s)
            s = came[s]
        path.reverse()
        return path

    # ---------------------------------------------------------- route a net
    def route_net(self, net, soft=False, big=False):
        w = self.width[net]
        ts, vs, who, whov = self.dyn_maps(net, w)
        N, nx = self.N, self.nx
        if not soft:
            for a in ts:
                a[a == 2] = 1
            vs[vs == 2] = 1
        tsb = [ts[0].tobytes(), ts[1].tobytes()]
        vsb = vs.tobytes()
        pads = self.nets[net]
        # terminal states per pad
        term = {}
        for pi in pads:
            st = set()
            for li in self.pads[pi]["lay"]:
                for c in self.pad_cells(pi, li, ts[li]):
                    st.add(li * N + c)
            term[pi] = st
        self.stubs = {}
        for pi in pads:
            if len(term[pi]) < 3:
                for li in self.pads[pi]["lay"]:
                    for c in self.stub_cells(pi, li, ts[li], w, net):
                        term[pi].add(li * N + c)
        if any(not v for v in term.values()):
            return None, set()
        # order: start with pad 0, connect nearest
        connected = [pads[0]]
        tree = set(term[pads[0]])
        remaining = list(pads[1:])
        items = []
        hits = set()
        pathcells = []
        while remaining:
            # nearest remaining pad to any connected pad
            best = None
            for pj in remaining:
                pd = self.pads[pj]
                for pk in connected:
                    pc = self.pads[pk]
                    dd = math.hypot(pd["x"] - pc["x"], pd["y"] - pc["y"])
                    if best is None or dd < best[0]:
                        best = (dd, pj)
            pj = best[1]
            pd = self.pads[pj]
            # bbox
            xsrc = [ (s % N) % nx for s in tree]
            ysrc = [ (s % N) // nx for s in tree]
            ti = (pd["x"] - self.x0) / self.p; tj = (pd["y"] - self.y0) / self.p
            path = None
            for margin_mm in ((4, 15, 1e9) if not big else (1e9,)):
                m = int(margin_mm / self.p) if margin_mm < 1e8 else 10 ** 7
                bbox = (max(0, int(min(min(xsrc), ti)) - m), max(0, int(min(min(ysrc), tj)) - m),
                        min(nx, int(max(max(xsrc), ti)) + m + 1), min(self.ny, int(max(max(ysrc), tj)) + m + 1))
                path = self.astar(tree, term[pj], tsb, vsb, self.soft_cost, bbox, pd["x"], pd["y"])
                if path is not None or elapsed() > TIME_LIMIT:
                    break
            if path is None:
                return None, set()
            # record soft hits
            for s in path:
                l = s // N; idx = s - l * N
                j, i = divmod(idx, nx)
                if ts[l][j, i] == 2:
                    hits.add(int(who[l][j, i]))
            for a, b2 in zip(path, path[1:]):
                if a // N != b2 // N:
                    j, i = divmod(a % N, nx)
                    if vs[j, i] == 2:
                        hits.add(int(whov[j, i]))
            pathcells.append(path)
            tree |= set(path)
            tree |= term[pj]
            connected.append(pj)
            remaining.remove(pj)
        hits.discard(0); hits.discard(-1); hits.discard(net)
        items = self.paths_to_items(pathcells, w, net)
        return items, hits

    def cxy(self, idx):
        j, i = divmod(idx, self.nx)
        return self.x0 + i * self.p, self.y0 + j * self.p

    def paths_to_items(self, paths, w, net):
        N = self.N
        items = []
        padpos = {}
        for pi in self.nets[net]:
            d = self.pads[pi]
            padpos[pi] = d
        for path in paths:
            # split into layer runs
            pts = [(s // N, s % N) for s in path]
            k = 0
            runs = []
            cur = [pts[0]]
            for a, b2 in zip(pts, pts[1:]):
                if a[0] != b2[0]:
                    runs.append(cur)
                    items.append(('v',) + self.cxy(a[1]))
                    cur = [b2]
                else:
                    cur.append(b2)
            runs.append(cur)
            for run in runs:
                l = run[0][0]
                cells = [c for _, c in run]
                if len(cells) < 2:
                    continue
                # compress collinear
                verts = [cells[0]]
                prevd = None
                for a, b2 in zip(cells, cells[1:]):
                    ja, ia = divmod(a, self.nx); jb, ib = divmod(b2, self.nx)
                    dd = (ib - ia, jb - ja)
                    if prevd is not None and dd != prevd:
                        verts.append(a)
                    prevd = dd
                verts.append(cells[-1])
                for a, b2 in zip(verts, verts[1:]):
                    x1, y1 = self.cxy(a); x2, y2 = self.cxy(b2)
                    items.append(('s', l, x1, y1, x2, y2, w))
            # endpoints: connect to pad centres if endpoint is inside a pad of the net
            for end in (pts[0], pts[-1]):
                l, c = end
                x, y = self.cxy(c)
                st = self.stubs.get(l * N + c)
                if st is not None:
                    items.append(('s', l, x, y, st[0], st[1], w))
                    continue
                for pi, d in padpos.items():
                    if l in d["chains"] and d["chains"][l]:
                        if dist_poly(np.array([x]), np.array([y]), d["chains"][l])[0] <= 1e-9:
                            if abs(x - d["x"]) > 1e-6 or abs(y - d["y"]) > 1e-6:
                                items.append(('s', l, x, y, d["x"], d["y"], w))
                            break
        # dedupe vias
        seen = set(); out = []
        for it in items:
            key = it if it[0] == 's' else ('v', round(it[1], 4), round(it[2], 4))
            if key in seen:
                continue
            seen.add(key); out.append(it)
        return out

    # ---------------------------------------------------------- main loop
    def run(self, out_path):
        self.via_cost = 1.5 / self.p + 8
        self.pref_pen = 1.25
        self.soft_cost = 2.0 / self.p
        self.total_exp = 0
        self.max_exp = 250000
        self.hw = 1.6
        order = sorted(self.nets, key=lambda n: self.net_span(n))
        failed = []
        for n in order:
            if elapsed() > TIME_LIMIT * 0.5:
                failed.append(n); continue
            items, _ = self.route_net(n)
            if items is None:
                failed.append(n)
            else:
                self.routes[n] = items
        log("pass1 routed", len(self.routes), "failed", len(failed), "exp", self.total_exp)
        self.save(out_path)
        # rip-up and reroute
        import random
        rng = random.Random(1)
        best = dict(self.routes)
        rnd = 0
        while elapsed() < TIME_LIMIT * 0.75 and len(best) < len(self.nets):
          rnd += 1
          self.routes = dict(best)
          queue = [n for n in self.nets if n not in self.routes]
          if rnd > 1:
              rng.shuffle(queue)
              self.soft_cost = (2.0 + rng.random() * 6) / self.p
              self.hw = rng.choice([1.2, 1.3, 1.6, 2.0])
          if rnd % 3 == 2:
              # fresh start with a less greedy search
              self.hw = 1.25
              self.routes = {}
              queue = []
              for n in order:
                  if elapsed() > TIME_LIMIT * 0.75:
                      break
                  items, _ = self.route_net(n)
                  if items is None:
                      queue.append(n)
                  else:
                      self.routes[n] = items
              log("fresh pass", len(self.routes))
              if len(self.routes) > len(best):
                  best = dict(self.routes)
          tries = {}
          it = 0
          while queue and elapsed() < TIME_LIMIT * 0.75:
              n = queue.pop(0)
              tries[n] = tries.get(n, 0) + 1
              if tries[n] > 6:
                  continue
              items, _ = self.route_net(n)
              if items is not None:
                  self.routes[n] = items
                  if len(self.routes) > len(best):
                      best = dict(self.routes)
                  continue
              items, hits = self.route_net(n, soft=True)
              if items is None:
                  continue
              for h in hits:
                  if h in self.routes:
                      del self.routes[h]
                      queue.append(h)
              self.routes[n] = items
              for _ in range(6):
                  bad = self.conflicts(n)
                  if not bad:
                      break
                  for h in bad:
                      if h in self.routes:
                          del self.routes[h]
                          queue.append(h)
              it += 1
              if len(self.routes) > len(best):
                  best = dict(self.routes)
          log("round", rnd, "best", len(best))
        if len(best) > len(self.routes):
            self.routes = dict(best)
            log("restored best", len(best))
        # final attempt for anything left
        for n in self.nets:
            if n not in self.routes and elapsed() < TIME_LIMIT * 0.8:
                items, _ = self.route_net(n)
                if items is not None:
                    self.routes[n] = items
        log("rip-up done routed", len(self.routes), "/", len(self.nets))
        self.save(out_path)
        if self.pairs:
            self.tune_pairs()
            self.save(out_path)
        self.cleanup(out_path)

    def conflicts(self, net):
        mine = self.routes.get(net, [])
        def geo(it):
            if it[0] == 's':
                return it[1], it[2], it[3], it[4], it[5], it[6] / 2, (it[1],)
            return None, it[1], it[2], it[1], it[2], self.via_d / 2, (0, 1)
        mg = [geo(it) for it in mine]
        bad = set()
        for on, items in self.routes.items():
            if on == net:
                continue
            for it in items:
                _, ax, ay, bx, by, ra, la = geo(it)
                for _, cx, cy, dx, dy, rc, lc in mg:
                    if not (set(la) & set(lc)):
                        continue
                    r = self.clr - 0.015 + ra + rc
                    if max(ax, bx) + r < min(cx, dx) or max(cx, dx) + r < min(ax, bx) or \
                       max(ay, by) + r < min(cy, dy) or max(cy, dy) + r < min(ay, by):
                        continue
                    if seg_seg_dist(ax, ay, bx, by, cx, cy, dx, dy) < r:
                        bad.add(on)
                        break
                if on in bad:
                    break
        return bad

    def net_len(self, n):
        vl = self.ds.GetBoardThickness() / MM - 0.055
        return sum(math.hypot(it[4] - it[2], it[5] - it[3]) if it[0] == 's' else vl for it in self.routes.get(n, []))

    def tune_pairs(self):
        for a, c, skew in self.pairs:
            if a not in self.routes or c not in self.routes:
                continue
            for attempt in range(3):
                La, Lc = self.net_len(a), self.net_len(c)
                d = abs(La - Lc)
                if d <= skew * 0.6:
                    break
                sn = a if La < Lc else c
                ok = self.add_length(sn, d)
                log("tune", self.netname[sn], "delta %.2f" % d, "ok" if ok else "FAIL")
                if not ok:
                    break

    def free_pts(self, st, pts):
        p = self.p
        for x, y in pts:
            fi = (x - self.x0) / p; fj = (y - self.y0) / p
            i0, j0 = int(math.floor(fi)), int(math.floor(fj))
            for jj in (j0, j0 + 1):
                for ii in (i0, i0 + 1):
                    if ii < 0 or jj < 0 or ii >= self.nx or jj >= self.ny or st[jj, ii] != 0:
                        return False
        return True

    def add_length(self, net, d):
        w = self.width[net]
        ts, vs, who, whov = self.dyn_maps(net, w)
        items = self.routes[net]
        segs = sorted([k for k, it in enumerate(items) if it[0] == 's'],
                      key=lambda k: -math.hypot(items[k][4] - items[k][2], items[k][5] - items[k][3]))
        remaining = d
        newitems = list(items)
        replace = {}
        gap = w + 0.1
        for k in segs:
            if remaining < 0.02:
                break
            _, l, x1, y1, x2, y2, _w = items[k]
            Ls = math.hypot(x2 - x1, y2 - y1)
            margin = max(0.15, w)
            nbmax = int((Ls - 2 * margin + gap) / (2 * gap))
            if nbmax < 1:
                continue
            ux, uy = (x2 - x1) / Ls, (y2 - y1) / Ls
            nxv, nyv = -uy, ux
            done = False
            for want in (remaining, remaining * 0.5, remaining * 0.25):
                cands = []
                for kb in range(1, nbmax + 1):
                    h = want / (2 * kb)
                    if h < w + 0.05 or h > 2.5:
                        continue
                    cands.append((h, kb))
                cands.sort()
                for h, kb in cands:
                    for side in (1, -1):
                        # build polyline
                        start = margin + (Ls - 2 * margin - (2 * kb - 1) * gap) / 2
                        poly = [(x1, y1)]
                        t = start
                        for b in range(kb):
                            px, py = x1 + ux * t, y1 + uy * t
                            poly.append((px, py))
                            poly.append((px + side * nxv * h, py + side * nyv * h))
                            t += gap
                            px, py = x1 + ux * t, y1 + uy * t
                            poly.append((px + side * nxv * h, py + side * nyv * h))
                            poly.append((px, py))
                            t += gap
                        poly.append((x2, y2))
                        samples = []
                        for (ax, ay), (bx, by) in zip(poly, poly[1:]):
                            L = math.hypot(bx - ax, by - ay)
                            n = max(2, int(L / (self.p * 0.5)) + 2)
                            for q in range(n):
                                tt = q / (n - 1)
                                samples.append((ax + tt * (bx - ax), ay + tt * (by - ay)))
                        if self.free_pts(ts[l], samples):
                            replace[k] = [('s', l, ax, ay, bx, by, _w) for (ax, ay), (bx, by) in zip(poly, poly[1:])]
                            remaining -= 2 * h * kb
                            done = True
                            break
                    if done:
                        break
                if done:
                    break
        if not replace:
            return False
        out = []
        for k, it in enumerate(items):
            if k in replace:
                out.extend(replace[k])
            else:
                out.append(it)
        self.routes[net] = out
        return remaining < 0.05

    def drc_bad(self, out_path):
        """Run KiCad DRC on the saved routing; return list of sets of our nets involved per violation."""
        import tempfile, shutil, subprocess
        base = os.path.splitext(self.path)[0]
        td = tempfile.mkdtemp(prefix="rdrc")
        tgt = os.path.join(td, "b.kicad_pcb")
        b = pcbnew.LoadBoard(out_path)
        pcbnew.ZONE_FILLER(b).Fill(b.Zones())
        pcbnew.SaveBoard(tgt, b)
        for ext in (".kicad_pro", ".kicad_dru"):
            if os.path.exists(base + ext):
                shutil.copy(base + ext, os.path.join(td, "b" + ext))
        rep = os.path.join(td, "r.json")
        subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "--all-track-errors",
                        "--output", rep, tgt], capture_output=True, text=True, timeout=max(30, TIME_LIMIT + 60 - elapsed()))
        data = json.load(open(rep))
        shutil.rmtree(td, ignore_errors=True)
        byname = {}
        for c, nm in self.netname.items():
            byname[nm] = c
            byname[unescape(nm)] = c
        pairnets = set()
        for a, c, _ in self.pairs:
            pairnets |= {a, c}
        out = []
        for v in data.get("violations", []):
            typ = v.get("type")
            dead = typ in ("track_dangling", "via_dangling")
            if not dead and (v.get("severity") != "error" or typ not in COUNTED):
                continue
            nets = set()
            for it in v.get("items", []):
                dsc = it.get("description", "")
                if dsc.split(" ")[0] in ("Track", "Via", "Arc") and "[" in dsc:
                    nm = dsc[dsc.index("[") + 1:dsc.index("]")]
                    if nm in byname:
                        nets.add(byname[nm])
            if dead:
                nets &= pairnets
            if nets:
                out.append((typ, nets))
        return out

    def cleanup(self, out_path, rounds=6):
        for _ in range(rounds):
            if elapsed() > TIME_LIMIT + 20:
                return
            t = time.time()
            viols = self.drc_bad(out_path)
            from collections import Counter
            log("drc took %.1f" % (time.time() - t), "violations", len(viols), dict(Counter(v[0] for v in viols)))
            if os.environ.get("ROUTE_DEBUG"):
                import shutil; shutil.copy(out_path, out_path + ".pre%d" % _)
            if not viols:
                return
            removed = set()
            for typ, nets in viols:
                if nets & removed:
                    continue
                # remove the net with the longest route (usually most to blame) - pick largest span
                victim = max(nets, key=lambda n: (len(self.routes.get(n, [])), n))
                removed.add(victim)
            for n in removed:
                self.routes.pop(n, None)
            log("removed", len(removed))
            skewed = set()
            for typ, nets in viols:
                if typ in ("skew_out_of_range", "length_out_of_range", "track_dangling", "via_dangling"):
                    skewed |= nets
            self.retried = getattr(self, "retried", set())
            for n in sorted(removed):
                if n in skewed or n in self.retried or elapsed() > TIME_LIMIT + 15:
                    continue
                self.retried.add(n)
                items, _ = self.route_net(n)
                if items is not None:
                    self.routes[n] = items
                    log("re-routed", self.netname[n])
            self.save(out_path)

    def net_span(self, n):
        xs = [self.pads[p]["x"] for p in self.nets[n]]
        ys = [self.pads[p]["y"] for p in self.nets[n]]
        return (max(xs) - min(xs) + max(ys) - min(ys)) * (1 + 0.1 * len(xs))

    def save(self, out_path):
        b = pcbnew.LoadBoard(self.path)
        for t in list(b.GetTracks()):
            b.Remove(t)
        for net, items in self.routes.items():
            ni = b.FindNet(net)
            for it in items:
                if it[0] == 's':
                    _, l, x1, y1, x2, y2, w = it
                    if abs(x1 - x2) < 1e-7 and abs(y1 - y2) < 1e-7:
                        continue
                    t = pcbnew.PCB_TRACK(b)
                    t.SetStart(pcbnew.VECTOR2I(int(round(x1 * MM)), int(round(y1 * MM))))
                    t.SetEnd(pcbnew.VECTOR2I(int(round(x2 * MM)), int(round(y2 * MM))))
                    t.SetWidth(int(round(w * MM)))
                    t.SetLayer(self.layers[l])
                    t.SetNet(ni)
                    b.Add(t)
                else:
                    _, x, y = it
                    v = pcbnew.PCB_VIA(b)
                    v.SetPosition(pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM))))
                    v.SetViaType(pcbnew.VIATYPE_THROUGH)
                    v.SetLayerPair(self.layers[0], self.layers[1])
                    v.SetDrill(int(round(self.via_drill * MM)))
                    v.SetWidth(self.layers[0], int(round(self.via_d * MM)))
                    v.SetNet(ni)
                    b.Add(v)
        tmp = out_path + ".tmp.kicad_pcb"
        pcbnew.SaveBoard(tmp, b)
        os.replace(tmp, out_path)
        log("saved", sum(len(v) for v in self.routes.values()), "items")


def main():
    inp, out = sys.argv[1], sys.argv[2]
    r = Router(inp)
    try:
        r.run(out)
    except Exception:
        import traceback
        traceback.print_exc()
        try:
            r.save(out)
            r.cleanup(out)
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    main()
