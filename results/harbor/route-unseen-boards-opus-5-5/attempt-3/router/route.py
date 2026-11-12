#!/usr/bin/env python3
"""Grid maze router (2 layers, through vias) with rip-up and reroute."""
import sys, os, re, json, math, time, heapq, fnmatch, shutil
import numpy as np

T0 = time.time()
TIME_LIMIT = float(os.environ.get("ROUTE_TIME", "370"))  # seconds of routing work

import pcbnew

MM = 1e6
MARGIN = 0.02


def log(*a):
    print("[%.1f]" % (time.time() - T0), *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- rules
def parse_rules(pro_path, dru_path):
    pro = json.load(open(pro_path)) if os.path.exists(pro_path) else {}
    ns = pro.get("net_settings", {})
    classes = {c["name"]: c for c in ns.get("classes", [])}
    patterns = [(p["pattern"], p["netclass"]) for p in (ns.get("netclass_patterns") or [])]
    assigns = ns.get("netclass_assignments") or {}
    rules = pro.get("board", {}).get("design_settings", {}).get("rules", {})
    minw = {}
    skew = {}
    if os.path.exists(dru_path):
        txt = open(dru_path).read()
        for m in re.finditer(r'\(rule\s+"[^"]*"\s*\(condition\s+"((?:[^"\\]|\\.)*)"\)\s*\(constraint\s+(\w+)\s*\((\w+)\s+([\d.]+)mm\)\)\)', txt):
            cond, kind, mm, val = m.group(1), m.group(2), m.group(3), float(m.group(4))
            if kind == "track_width" and mm == "min":
                for n in re.findall(r"A\.NetName == '((?:[^'\\]|\\.)*)'", cond):
                    minw[n] = max(minw.get(n, 0), val)
            if kind == "skew":
                for n in re.findall(r"inDiffPair\('([^']*)'\)", cond):
                    skew[n] = val
    return classes, patterns, assigns, rules, minw, skew


def netclass_of(name, classes, patterns, assigns):
    if name in assigns:
        v = assigns[name]
        return v[0] if isinstance(v, list) else v
    for pat, cls in patterns:
        try:
            if fnmatch.fnmatchcase(name, pat) or re.fullmatch(pat, name):
                return cls
        except re.error:
            if fnmatch.fnmatchcase(name, pat):
                return cls
    return "Default"


# ---------------------------------------------------------------- geometry helpers
def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return np.hypot(px - ax, py - ay)
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / L2, 0, 1)
    return np.hypot(px - (ax + t * dx), py - (ay + t * dy))


def poly_inside(pts, X, Y):
    inside = np.zeros(X.shape, bool)
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > Y) is None:
            pass
        cond = (y1 > Y) != (y2 > Y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = x1 + (Y - y1) * (x2 - x1) / (y2 - y1 + 1e-30)
        inside ^= cond & (X < xi)
    return inside


def poly_dist(pts, X, Y):
    """signed distance (negative inside) of points to polygon"""
    d = np.full(X.shape, np.inf)
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        d = np.minimum(d, seg_dist(X, Y, x1, y1, x2, y2))
    ins = poly_inside(pts, X, Y)
    return np.where(ins, -d, d)


def sps_outlines(sps):
    out = []
    for i in range(sps.OutlineCount()):
        o = sps.Outline(i)
        pts = [(o.CPoint(j).x / MM, o.CPoint(j).y / MM) for j in range(o.PointCount())]
        holes = []
        for h in range(sps.HoleCount(i)):
            hh = sps.Hole(i, h)
            holes.append([(hh.CPoint(j).x / MM, hh.CPoint(j).y / MM) for j in range(hh.PointCount())])
        out.append((pts, holes))
    return out


# ---------------------------------------------------------------- router
class Router:
    def __init__(self, board, pro, dru):
        self.board = board
        classes, patterns, assigns, rules, minw, skew = parse_rules(pro, dru)
        self.skew = skew
        ds = board.GetDesignSettings()
        self.layers = [pcbnew.F_Cu, pcbnew.B_Cu]
        clr = max([c.get("clearance", 0.2) for c in classes.values()] + [ds.m_MinClearance / MM, rules.get("min_clearance", 0)])
        self.clr = clr
        self.hole_clr = hole_clr = max(ds.m_HoleClearance / MM, rules.get("min_hole_clearance", 0))
        self.h2h = max(ds.m_HoleToHoleMin / MM, rules.get("min_hole_to_hole", 0.25))
        self.edge_clr = max(ds.m_CopperEdgeClearance / MM, rules.get("min_copper_edge_clearance", 0))
        min_tw = max(ds.m_TrackMinWidth / MM, rules.get("min_track_width", 0))
        drill = max(rules.get("min_through_hole_diameter", 0.3), ds.m_MinThroughDrill / MM, 0.3)
        ann = max(rules.get("min_via_annular_width", 0.1), ds.m_ViasMinAnnularWidth / MM, 0.1)
        vdia = max(rules.get("min_via_diameter", 0.5), ds.m_ViasMinSize / MM, drill + 2 * ann)
        self.drill, self.vdia = round(drill + 0.0, 3), round(vdia + 0.0, 3)
        # via hole clearance: hole edge to other copper
        self.via_r = max(self.vdia / 2, self.drill / 2 + max(0, hole_clr - clr))
        log("clr", clr, "via", self.vdia, self.drill, "edge", self.edge_clr, "mintw", min_tw)

        # nets
        self.netnames = {}
        pads_by_net = {}
        self.pads = []
        for fp in board.GetFootprints():
            for p in fp.Pads():
                self.pads.append(p)
                if p.GetNetCode() > 0:
                    pads_by_net.setdefault(p.GetNetCode(), []).append(p)
                    self.netnames[p.GetNetCode()] = p.GetNetname()
        self.width = {}
        for code, name in self.netnames.items():
            w = max(min_tw, minw.get(name, 0))
            self.width[code] = round(w + 0.0, 4)
        self.route_nets = [c for c, ps in pads_by_net.items() if len(ps) >= 2]
        self.pads_by_net = pads_by_net

        # grid
        bb = board.GetBoardEdgesBoundingBox()
        x0, y0, x1, y1 = bb.GetX() / MM, bb.GetY() / MM, bb.GetRight() / MM, bb.GetBottom() / MM
        area = (x1 - x0) * (y1 - y0)
        pitch = 0.127
        for cand in (0.05, 0.0635, 0.08, 0.1, 0.127, 0.15, 0.2):
            if area / (cand * cand) <= 900000:
                pitch = cand
                break
        self.p = pitch
        # align grid with a pad center
        if self.pads:
            ref = self.pads[0].GetPosition()
            rx, ry = ref.x / MM, ref.y / MM
        else:
            rx, ry = x0, y0
        self.ox = rx - math.ceil((rx - x0) / pitch + 2) * pitch
        self.oy = ry - math.ceil((ry - y0) / pitch + 2) * pitch
        self.W = int((x1 - self.ox) / pitch) + 4
        self.H = int((y1 - self.oy) / pitch) + 4
        self.N = self.W * self.H
        log("grid", self.W, self.H, "pitch", pitch, "nets", len(self.route_nets))
        xs = self.ox + np.arange(self.W) * pitch
        ys = self.oy + np.arange(self.H) * pitch
        self.xs, self.ys = xs, ys

        # classes: radius of keep-out around foreign copper for a centreline
        hws = sorted(set(self.width[c] / 2 for c in self.route_nets))
        self.cls_r = [h + clr + MARGIN for h in hws] + [self.via_r + clr + MARGIN]
        self.cls_of_hw = {h: i for i, h in enumerate(hws)}
        self.VIA = len(self.cls_r) - 1
        K = len(self.cls_r)
        shape = (K, 2, self.H, self.W)
        self.st_c = np.zeros(shape, np.int32)
        self.st_s1 = np.zeros(shape, np.int64)
        self.st_s2 = np.zeros(shape, np.int64)
        self.tr_c = np.zeros(shape, np.int32)
        self.tr_s1 = np.zeros(shape, np.int64)
        self.tr_s2 = np.zeros(shape, np.int64)
        self.BLOCK = 100000
        self.build_static()

        self.geom = {}  # net -> list of (kind, layer, x1,y1,x2,y2) kind 't' or 'v'
        self.segs = {}  # net -> list of output items
        self.vias = {}
        self.rips = {}
        self.max_exp = 250000

    # ---- occupancy primitives
    def window(self, x1, y1, x2, y2):
        p = self.p
        i0 = max(0, int((x1 - self.ox) / p) - 1)
        i1 = min(self.W, int((x2 - self.ox) / p) + 2)
        j0 = max(0, int((y1 - self.oy) / p) - 1)
        j1 = min(self.H, int((y2 - self.oy) / p) + 2)
        return i0, i1, j0, j1

    def add_mask(self, static, k, L, i0, j0, mask, net, sign=1):
        if not mask.any():
            return
        h, w = mask.shape
        c, s1, s2 = (self.st_c, self.st_s1, self.st_s2) if static else (self.tr_c, self.tr_s1, self.tr_s2)
        m = mask.astype(np.int64) * sign
        c[k, L, j0:j0 + h, i0:i0 + w] += m.astype(np.int32)
        s1[k, L, j0:j0 + h, i0:i0 + w] += m * net
        s2[k, L, j0:j0 + h, i0:i0 + w] += m * net * net

    def add_shape(self, static, layers, net, distfn, bbox, extra=0.0, sign=1, classes=None):
        """distfn(X,Y)-> distance to copper; mark cells with dist < r_k (+extra)"""
        rmax = max(self.cls_r) + max(extra, 0) + 1.0
        i0, i1, j0, j1 = self.window(bbox[0] - rmax, bbox[1] - rmax, bbox[2] + rmax, bbox[3] + rmax)
        if i1 <= i0 or j1 <= j0:
            return
        X, Y = np.meshgrid(self.xs[i0:i1], self.ys[j0:j1])
        d = distfn(X, Y)
        for k, r in enumerate(self.cls_r):
            if classes is not None and k not in classes:
                continue
            mask = d < r + extra
            for L in layers:
                self.add_mask(static, k, L, i0, j0, mask, net, sign)

    def build_static(self):
        b = self.board
        self.pad_info = []  # (net, layers, distfn, bbox)
        for p in self.pads:
            net = p.GetNetCode()
            if net <= 0 or net not in self.route_nets and net not in self.netnames:
                net = self.BLOCK
            layers = [i for i, L in enumerate(self.layers) if p.IsOnLayer(L)]
            # copper shape
            per_layer = {}
            for L in layers:
                sps = p.GetEffectivePolygon(self.layers[L], pcbnew.ERROR_INSIDE)
                outs = sps_outlines(sps)
                if not outs:
                    continue
                pts = outs[0][0]
                per_layer[L] = pts
            for L, pts in per_layer.items():
                arr = np.array(pts)
                bbox = (arr[:, 0].min(), arr[:, 1].min(), arr[:, 0].max(), arr[:, 1].max())
                fn = (lambda pts: (lambda X, Y: poly_dist(pts, X, Y)))(pts)
                self.add_shape(True, [L], net, fn, bbox)
                self.pad_info.append((p, net, L, fn, bbox))
            # holes
            if p.HasHole() if hasattr(p, "HasHole") else p.GetDrillSize().x > 0:
                ds_ = p.GetDrillSize()
                if ds_.x > 0:
                    c = p.GetPosition()
                    cx, cy, hr = c.x / MM, c.y / MM, max(ds_.x, ds_.y) / MM / 2
                    hn = net if per_layer else self.BLOCK
                    # hole-to-hole with vias, and copper to NPTH
                    fn = lambda X, Y, cx=cx, cy=cy, hr=hr: np.hypot(X - cx, Y - cy) - hr
                    bbox = (cx - hr, cy - hr, cx + hr, cy + hr)
                    hn = net if (net != self.BLOCK and p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH) else self.BLOCK
                    self.add_shape(True, [0, 1], hn, fn, bbox, extra=max(0.0, self.hole_clr - self.clr) + 0.01)
                    # vias must keep hole-to-hole from every hole (even own net)
                    extra = self.h2h + self.drill / 2 - (self.via_r + self.clr + MARGIN) + MARGIN
                    if extra > 0:
                        self.add_shape(True, [0, 1], self.BLOCK, fn, bbox, extra=extra, classes=[self.VIA])
        # copper text and graphics
        items = list(b.GetDrawings())
        for fp in b.GetFootprints():
            items += list(fp.GraphicalItems())
            items += [fp.Reference(), fp.Value()]
        for it in items:
            try:
                for L in range(2):
                    if it.IsOnLayer(self.layers[L]):
                        bb = it.GetBoundingBox()
                        bx = (bb.GetX() / MM, bb.GetY() / MM, bb.GetRight() / MM, bb.GetBottom() / MM)
                        fn = lambda X, Y, bx=bx: np.maximum(np.maximum(bx[0] - X, X - bx[2]), np.maximum(bx[1] - Y, Y - bx[3]))
                        self.add_shape(True, [L], self.BLOCK, fn, bx)
            except Exception as e:
                log("graphic skip", e)
        # board edge
        sps = pcbnew.SHAPE_POLY_SET()
        b.GetBoardPolygonOutlines(sps)
        X, Y = np.meshgrid(self.xs, self.ys)
        inside = np.zeros(X.shape, bool)
        edges = []
        for pts, holes in sps_outlines(sps):
            inside |= poly_inside(pts, X, Y)
            edges.append(pts)
            for h in holes:
                inside &= ~poly_inside(h, X, Y)
                edges.append(h)
        outside = ~inside
        for k, r in enumerate(self.cls_r):
            rr = r - self.clr + self.edge_clr + 0.01
            m = outside.copy()
            # dilate by distance rr: mark cells near edge segments
            for pts in edges:
                n = len(pts)
                for i in range(n):
                    xa, ya = pts[i]
                    xb, yb = pts[(i + 1) % n]
                    i0, i1, j0, j1 = self.window(min(xa, xb) - rr, min(ya, yb) - rr, max(xa, xb) + rr, max(ya, yb) + rr)
                    sub = seg_dist(X[j0:j1, i0:i1], Y[j0:j1, i0:i1], xa, ya, xb, yb) < rr
                    m[j0:j1, i0:i1] |= sub
            m[0, :] = m[-1, :] = True
            m[:, 0] = m[:, -1] = True
            for L in range(2):
                self.add_mask(True, k, L, 0, 0, m, self.BLOCK)
        # keepouts
        for z in b.Zones():
            if not z.GetIsRuleArea():
                continue
            outs = sps_outlines(z.Outline())
            zl = [i for i, L in enumerate(self.layers) if z.GetLayerSet().Contains(L)]
            for pts, holes in outs:
                arr = np.array(pts)
                bbox = (arr[:, 0].min(), arr[:, 1].min(), arr[:, 0].max(), arr[:, 1].max())
                fn = (lambda pts: (lambda X, Y: poly_dist(pts, X, Y)))(pts)
                cl = []
                if z.GetDoNotAllowTracks():
                    cl += list(range(self.VIA))
                if z.GetDoNotAllowVias():
                    cl += [self.VIA]
                if cl:
                    self.add_shape(True, zl, self.BLOCK, fn, bbox, extra=-self.clr + 0.01, classes=cl)

    # ---- free masks
    def free_mask(self, k, net, static_only=False):
        def ok(c, s1, s2):
            return (c == 0) | ((s1 == c * net) & (s2 == c * net * net))
        hard = ok(self.st_c[k], self.st_s1[k], self.st_s2[k])
        if static_only:
            return hard, None
        soft = ok(self.tr_c[k], self.tr_s1[k], self.tr_s2[k])
        return hard, soft

    # ---- terminals
    def pad_cells(self, p):
        cells = []
        net = p.GetNetCode()
        for (pp, n, L, fn, bbox) in self.pad_info:
            if pp is not p:
                continue
            i0, i1, j0, j1 = self.window(*bbox)
            X, Y = np.meshgrid(self.xs[i0:i1], self.ys[j0:j1])
            d = fn(X, Y)
            m = d < -0.02
            if not m.any():
                # take the closest cell
                jj, ii = np.unravel_index(np.argmin(d), d.shape)
                m = np.zeros_like(m)
                m[jj, ii] = True
            jj, ii = np.nonzero(m)
            for j, i in zip(jj, ii):
                cells.append(L * self.N + (j + j0) * self.W + (i + i0))
        return cells

    # ---- A*
    def astar(self, net, sources, targets, tgt_xy, free_t, free_v, soft_t, soft_v, soft_cost, via_cost):
        W, N = self.W, self.N
        tset = set(targets)
        inf = float("inf")
        g = {}
        parent = {}
        h_ = []
        tx, ty = tgt_xy
        p = self.p
        HW = 1.3

        def heur(idx):
            r = idx % N
            x = r % W
            y = r // W
            dx = abs(x * p + self.ox - tx) / p
            dy = abs(y * p + self.oy - ty) / p
            return HW * (max(dx, dy) + 0.4142 * min(dx, dy))
        for s in sources:
            g[s] = 0.0
            parent[s] = -1
            heapq.heappush(h_, (heur(s), 0.0, s))
        nbr = [(1, 1.0), (-1, 1.0), (W, 1.0), (-W, 1.0), (W + 1, 1.4142), (W - 1, 1.4142), (-W + 1, 1.4142), (-W - 1, 1.4142)]
        closed = set()
        expanded = 0
        while h_:
            f, gc, u = heapq.heappop(h_)
            if u in closed:
                continue
            if u in tset:
                path = []
                while u != -1:
                    path.append(u)
                    u = parent[u]
                return path, expanded
            closed.add(u)
            expanded += 1
            if expanded > self.max_exp:
                return None, expanded
            L = u // N
            r = u - L * N
            for d, c in nbr:
                v = u + d
                fv = free_t[v]
                if fv == 0:
                    continue
                if c > 1.1:
                    # no corner cutting through blocked cells
                    pass
                nc = gc + c
                if fv == 2:
                    nc += soft_cost[r + d]
                if nc < g.get(v, inf):
                    g[v] = nc
                    parent[v] = u
                    heapq.heappush(h_, (nc + heur(v), nc, v))
            # via
            fv = free_v[r]
            if fv:
                v = (1 - L) * N + r
                if free_t[v]:
                    nc = gc + via_cost
                    if fv == 2:
                        nc += soft_cost[r] * 2
                    if free_t[v] == 2:
                        nc += soft_cost[r]
                    if nc < g.get(v, inf):
                        g[v] = nc
                        parent[v] = u
                        heapq.heappush(h_, (nc + heur(v), nc, v))
        return None, expanded

    def cell_xy(self, idx):
        L = idx // self.N
        r = idx % self.N
        return L, self.ox + (r % self.W) * self.p, self.oy + (r // self.W) * self.p

    # ---- route one net
    def route_net(self, net, allow_soft, soft_cost):
        k = self.cls_of_hw[self.width[net] / 2]
        hard_t, soft_t = self.free_mask(k, net)
        hard_v, soft_v = self.free_mask(self.VIA, net)

        def comb(hard, soft):
            if allow_soft:
                a = np.where(hard, np.where(soft, 1, 2), 0).astype(np.uint8)
            else:
                a = (hard & soft).astype(np.uint8)
            return a
        ft = comb(hard_t, soft_t).reshape(-1)
        fv = comb(hard_v, soft_v)
        fv = np.minimum(fv[0], fv[1]).reshape(-1)  # through via must fit on both layers
        # own vias hole-to-hole: leave to check after
        ft_b = bytearray(ft.tobytes())
        fv_b = bytearray(fv.tobytes())
        pads = self.pads_by_net[net]
        pad_cells = [self.pad_cells(p) for p in pads]
        for p, cells in zip(pads, pad_cells):
            cells[:] = [c for c in cells if ft_b[c]]
            pc = p.GetPosition()
            for L in range(2):
                if p.IsOnLayer(self.layers[L]):
                    c = L * self.N + int(round((pc.y / MM - self.oy) / self.p)) * self.W + int(round((pc.x / MM - self.ox) / self.p))
                    ft_b[c] = 1
                    if c not in cells:
                        cells.append(c)
        via_cost = 8.0 / self.p * 0.1 + 4
        # tree
        tree_cells = set(pad_cells[0])
        remaining = list(range(1, len(pads)))
        paths = []
        tree_pts = [pads[0].GetPosition()]
        while remaining:
            if time.time() - T0 > TIME_LIMIT:
                return None
            # nearest remaining pad to tree pads
            best = None
            for i in remaining:
                pp = pads[i].GetPosition()
                dmin = min((pp.x - q.x) ** 2 + (pp.y - q.y) ** 2 for q in tree_pts)
                if best is None or dmin < best[0]:
                    best = (dmin, i)
            ti = best[1]
            targets = []
            owner = {}
            for i in remaining:
                for c in pad_cells[i]:
                    owner[c] = i
                    targets.append(c)
            tp = pads[ti].GetPosition()
            path, exp = self.astar(net, list(tree_cells), targets, (tp.x / MM, tp.y / MM), ft_b, fv_b, None, None, soft_cost, via_cost)
            if path is None:
                return None
            reached = owner[path[0]]
            remaining.remove(reached)
            tree_pts.append(pads[reached].GetPosition())
            paths.append(path)
            tree_cells.update(path)
            tree_cells.update(pad_cells[reached])
        return paths

    def paths_to_geom(self, net, paths):
        """convert cell paths to segments and vias"""
        edges = set()
        for path in paths:
            for a, b in zip(path, path[1:]):
                edges.add((min(a, b), max(a, b)))
        adj = {}
        for a, b in edges:
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
        N = self.N
        vias = set()
        tracks = []
        for a, b in edges:
            if a % N == b % N:
                vias.add(a % N)
        # straight chains on each layer
        used = set()

        def dirn(a, b):
            return b - a
        for a, b in edges:
            if a % N == b % N or (a, b) in used:
                continue
            # extend chain in both directions
            d = b - a
            chain = [a, b]
            used.add((a, b))
            # forward
            cur = b
            while True:
                nxt = cur + d
                e = (min(cur, nxt), max(cur, nxt))
                if e in edges and e not in used and len(adj[cur]) == 2:
                    used.add(e)
                    chain.append(nxt)
                    cur = nxt
                else:
                    break
            cur = a
            while True:
                nxt = cur - d
                e = (min(cur, nxt), max(cur, nxt))
                if e in edges and e not in used and len(adj[cur]) == 2:
                    used.add(e)
                    chain.insert(0, nxt)
                    cur = nxt
                else:
                    break
            L, x1, y1 = self.cell_xy(chain[0])
            _, x2, y2 = self.cell_xy(chain[-1])
            tracks.append((L, x1, y1, x2, y2))
        vl = []
        for r in vias:
            _, x, y = self.cell_xy(r)
            vl.append((x, y))
        return tracks, vl

    def commit(self, net, tracks, vias, sign=1):
        hw = self.width[net] / 2
        for (L, x1, y1, x2, y2) in tracks:
            fn = lambda X, Y, a=(x1, y1, x2, y2): seg_dist(X, Y, *a) - hw
            self.add_shape(False, [L], net, fn, (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)), sign=sign)
        vr = self.vdia / 2
        for (x, y) in vias:
            fn = lambda X, Y, x=x, y=y: np.hypot(X - x, Y - y) - vr
            self.add_shape(False, [0, 1], net, fn, (x, y, x, y), sign=sign)
            # hole to hole for other vias, any net
            extra = self.h2h + self.drill - (self.via_r + self.clr + MARGIN) - (vr - self.drill / 2) + MARGIN
            if extra > 0:
                fn2 = lambda X, Y, x=x, y=y: np.hypot(X - x, Y - y) - self.drill / 2
                self.add_shape(False, [0, 1], self.BLOCK + net, fn2, (x, y, x, y), extra=extra - (self.drill / 2 - 0) * 0, sign=sign, classes=[self.VIA])

    def remove(self, net):
        if net in self.geom:
            t, v = self.geom.pop(net)
            self.commit(net, t, v, sign=-1)

    def conflicts(self, net, tracks, vias):
        """nets whose committed copper conflicts with this geometry"""
        hw = self.width[net] / 2
        pts = self.sample(tracks, vias, hw)
        bad = set()
        for m, (t2, v2) in self.geom.items():
            if m == net:
                continue
            q = self.sample(t2, v2, self.width[m] / 2)
            for L in (0, 1):
                a = pts[pts[:, 2] == L]
                b = q[q[:, 2] == L]
                if len(a) == 0 or len(b) == 0:
                    continue
                if a[:, 0].min() - 3 > b[:, 0].max() or b[:, 0].min() - 3 > a[:, 0].max() or a[:, 1].min() - 3 > b[:, 1].max() or b[:, 1].min() - 3 > a[:, 1].max():
                    continue
                d = np.hypot(a[:, None, 0] - b[None, :, 0], a[:, None, 1] - b[None, :, 1]) - a[:, None, 3] - b[None, :, 3]
                if (d < self.clr + MARGIN * 0.5).any():
                    bad.add(m)
                    break
        return bad

    def sample(self, tracks, vias, hw):
        out = []
        st = self.p * 0.7
        for (L, x1, y1, x2, y2) in tracks:
            n = max(1, int(math.hypot(x2 - x1, y2 - y1) / st) + 1)
            t = np.linspace(0, 1, n + 1)
            for tt in t:
                out.append((x1 + (x2 - x1) * tt, y1 + (y2 - y1) * tt, L, hw))
        for (x, y) in vias:
            out.append((x, y, 0, self.vdia / 2))
            out.append((x, y, 1, self.vdia / 2))
        return np.array(out) if out else np.zeros((0, 4))

    def check_own_vias(self, vias):
        for i in range(len(vias)):
            for j in range(i + 1, len(vias)):
                if math.hypot(vias[i][0] - vias[j][0], vias[i][1] - vias[j][1]) < self.drill + self.h2h + 0.01:
                    return False
        return True

    def run(self, save_cb):
        nets = list(self.route_nets)

        def hpwl(n):
            ps = [p.GetPosition() for p in self.pads_by_net[n]]
            xs = [q.x for q in ps]
            ys = [q.y for q in ps]
            return (max(xs) - min(xs) + max(ys) - min(ys)) / MM
        nets.sort(key=hpwl)
        soft_cost = np.full(self.N, 30.0)
        hist = np.zeros(self.N)
        queue = list(nets)
        failed = set()
        it = 0
        last_save = time.time()
        attempts = {}
        while queue and time.time() - T0 < TIME_LIMIT:
            net = queue.pop(0)
            attempts[net] = attempts.get(net, 0) + 1
            it += 1
            paths = self.route_net(net, False, None)
            if paths is not None:
                t, v = self.paths_to_geom(net, paths)
                self.commit(net, t, v)
                self.geom[net] = (t, v)
                failed.discard(net)
                continue
            if attempts[net] > 6:
                failed.add(net)
                continue
            # rip-up mode
            sc = (20.0 + hist * 10.0).tolist()
            paths = self.route_net(net, True, sc)
            if paths is None:
                failed.add(net)
                log("unroutable", self.netnames[net])
                continue
            t, v = self.paths_to_geom(net, paths)
            bad = self.conflicts(net, t, v)
            for m in bad:
                self.remove(m)
                queue.append(m)
                self.rips[m] = self.rips.get(m, 0) + 1
            for path in paths:
                for c in path:
                    hist[c % self.N] += 1
            self.commit(net, t, v)
            self.geom[net] = (t, v)
            failed.discard(net)
            paths2 = True
            log("ripup", self.netnames[net], "ripped", [self.netnames[m] for m in bad], "ok" if paths2 else "fail")
            if time.time() - last_save > 60:
                save_cb()
                last_save = time.time()
        log("done; routed", len(self.geom), "of", len(nets), "failed", [self.netnames[n] for n in nets if n not in self.geom])


def write_board(src, dst, router):
    board = pcbnew.LoadBoard(src)
    for net, (tracks, vias) in router.geom.items():
        ni = board.FindNet(router.netnames[net])
        w = int(round(router.width[net] * MM))
        for (L, x1, y1, x2, y2) in tracks:
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(pcbnew.VECTOR2I(int(round(x1 * MM)), int(round(y1 * MM))))
            t.SetEnd(pcbnew.VECTOR2I(int(round(x2 * MM)), int(round(y2 * MM))))
            t.SetWidth(w)
            t.SetLayer(router.layers[L])
            t.SetNet(ni)
            board.Add(t)
        for (x, y) in vias:
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM))))
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            v.SetDrill(int(round(router.drill * MM)))
            v.SetWidth(pcbnew.F_Cu, int(round(router.vdia * MM)))
            v.SetNet(ni)
            board.Add(v)
    tmp = dst + ".tmp.kicad_pcb"
    pcbnew.SaveBoard(tmp, board)
    os.replace(tmp, dst)
    log("saved", dst)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    try:
        shutil.copy(src, dst)
    except Exception:
        pass
    base = src[:-len(".kicad_pcb")]
    board = pcbnew.LoadBoard(src)
    r = Router(board, base + ".kicad_pro", base + ".kicad_dru")
    log("static built")
    try:
        r.run(lambda: write_board(src, dst, r))
    finally:
        write_board(src, dst, r)


if __name__ == "__main__":
    main()
