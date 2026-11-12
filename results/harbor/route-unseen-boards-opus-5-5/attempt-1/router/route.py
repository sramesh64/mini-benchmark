#!/usr/bin/env python3
"""Grid-based two-layer maze router for KiCad boards (tracks + through vias)."""
import sys, os, re, time, math, heapq, json, random
import numpy as np
import pcbnew

T0 = time.time()
DEADLINE = T0 + float(os.environ.get("ROUTE_TIME", "370"))
MARGIN = 0.02   # extra clearance (mm) on top of the rules
EPS = 1e-9


def mm(v):
    return v / 1e6


def log(*a):
    print("[%.1f]" % (time.time() - T0), *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- rules
def parse_dru(path):
    widths, pairs = {}, {}
    if not os.path.exists(path):
        return widths, pairs
    txt = open(path).read()
    for m in re.finditer(r'\(rule\s+"[^"]*"\s*\(condition\s+"((?:[^"\\]|\\.)*)"\s*\)\s*\(constraint\s+(\w+)\s*\((\w+)\s+([\d.]+)mm\)', txt):
        cond, kind, mx, val = m.group(1), m.group(2), m.group(3), float(m.group(4))
        if kind == "track_width":
            for n in re.findall(r"A\.NetName\s*==\s*'([^']*)'", cond):
                widths[n] = max(widths.get(n, 0), val)
        elif kind == "skew":
            mm_ = re.search(r"inDiffPair\('([^']*)'\)", cond)
            if mm_:
                pairs[mm_.group(1)] = val
    return widths, pairs


class Router:
    def __init__(self, path):
        self.path = path
        self.board = pcbnew.LoadBoard(path)
        b = self.board
        base = os.path.splitext(path)[0]
        self.rule_w, self.pair_skew = parse_dru(base + ".kicad_dru")
        ds = b.GetDesignSettings()
        self.ds = ds
        nets = b.GetNetsByNetcode()
        self.netname = {}
        for code, ni in nets.items():
            if code > 0:
                self.netname[int(code)] = ni.GetNetname()
        self.netcode = {v: k for k, v in self.netname.items()}
        ns = ds.m_NetSettings
        dnc = ns.GetDefaultNetclass()
        clr = mm(dnc.GetClearance())
        vdia, vdrill = mm(dnc.GetViaDiameter()), mm(dnc.GetViaDrill())
        for k, v in ns.GetNetclasses().items():
            clr = max(clr, mm(v.GetClearance()))
        clr = max(clr, mm(ds.m_MinClearance))
        self.clr = clr
        self.minw = max(mm(ds.m_TrackMinWidth), 0.1)
        vdrill = max(vdrill, mm(ds.m_MinThroughDrill))
        vdia = max(vdia, mm(ds.m_ViasMinSize), vdrill + 2 * max(mm(ds.m_ViasMinAnnularWidth), 0.1))
        self.vdia, self.vdrill = vdia, vdrill
        self.h2h = max(mm(ds.m_HoleToHoleMin), 0.25)
        self.holeclr = mm(ds.m_HoleClearance)
        self.edgeclr = max(mm(ds.m_CopperEdgeClearance), 0.05)
        log("clr", clr, "via", vdia, vdrill, "minw", self.minw)
        # pads
        self.pads = []
        for fp in b.GetFootprints():
            for p in fp.Pads():
                self.pads.append(p)
        # nets to route: nets with >=2 pads
        cnt = {}
        for p in self.pads:
            c = p.GetNetCode()
            if c > 0:
                cnt[c] = cnt.get(c, 0) + 1
        self.route_nets = [c for c, k in cnt.items() if k >= 2]
        self.width = {}
        for c in self.route_nets:
            n = self.netname[c]
            w = max(self.rule_w.get(n, 0), self.minw)
            self.width[c] = round(w + 0.0005, 4)
        # pairs
        self.pairs = []
        for pn, sk in self.pair_skew.items():
            p_ = [c for c in self.route_nets if self._pair_match(self.netname[c], pn)]
            if len(p_) == 2:
                self.pairs.append((p_[0], p_[1], sk))
        log("pairs", [(self.netname[a], self.netname[b_], s) for a, b_, s in self.pairs])
        self.setup_grid()

    @staticmethod
    def _pair_match(name, base):
        for sp, sn in (("_P", "_N"), ("+", "-"), ("P", "N")):
            if name == base + sp or name == base + sn:
                return True
        return False

    # ------------------------------------------------------------ grid
    def setup_grid(self):
        b = self.board
        bb = b.GetBoardEdgesBoundingBox()
        x0, y0 = mm(bb.GetX()), mm(bb.GetY())
        w, h = mm(bb.GetWidth()), mm(bb.GetHeight())
        area = w * h
        g = math.sqrt(area / 900000.0)
        cands = [0.05, 0.0635, 0.08, 0.1, 0.127, 0.15, 0.2]
        g = min([c for c in cands if c >= g] or [0.2])
        g = max(g, 0.05)
        self.g = g
        # choose origin phase aligning most pad centres
        best, bestn = (0.0, 0.0), -1
        pts = np.array([(mm(p.GetPosition().x), mm(p.GetPosition().y)) for p in self.pads]) if self.pads else np.zeros((0, 2))
        phx = [0.0] + list(np.mod(pts[:200, 0], g))
        phy = [0.0] + list(np.mod(pts[:200, 1], g))
        def score(ph, arr):
            d = np.mod(arr - ph + g / 2, g) - g / 2
            return int(np.sum(np.abs(d) < 0.003))
        bx = max(phx, key=lambda ph: score(ph, pts[:, 0]))
        by = max(phy, key=lambda ph: score(ph, pts[:, 1]))
        ox = math.floor((x0 - 1) / g) * g + bx
        oy = math.floor((y0 - 1) / g) * g + by
        self.ox, self.oy = ox, oy
        self.nx = int(math.ceil((x0 + w + 1 - ox) / g)) + 1
        self.ny = int(math.ceil((y0 + h + 1 - oy) / g)) + 1
        self.N = self.nx * self.ny
        log("grid", g, self.nx, self.ny)
        # classes of radii
        rads = set()
        for c in self.route_nets:
            rads.add(round(self.width[c] / 2 + self.clr + MARGIN, 4))
        self.rads = sorted(rads)
        self.rv = round(self.vdia / 2 + self.clr + MARGIN, 4)
        self.rad_index = {r: i for i, r in enumerate(self.rads)}
        # static obstacles list: (layer, net, kind, geom, extra)
        self.static = []
        self.outline_mask()
        for p in self.pads:
            net = p.GetNetCode()
            if net <= 0 or net not in self.width:
                net_id = -1 if net <= 0 else net
            else:
                net_id = net
            if net > 0 and net not in self.width:
                net_id = net  # single pad nets; keep as own net (no one else may touch)
            own = 0.0
            try:
                own = max(0.0, mm(p.GetOwnClearance(pcbnew.F_Cu)) - self.clr)
            except Exception:
                pass
            for L, lay in enumerate((pcbnew.F_Cu, pcbnew.B_Cu)):
                if p.IsOnLayer(lay) and p.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH:
                    polys = self.pad_polys(p, lay)
                    for poly in polys:
                        self.static.append((L, net_id, "poly", poly, own))
                if p.GetDrillSizeX() > 0:
                    # hole: clearance to holes for tracks of other nets
                    r = mm(max(p.GetDrillSizeX(), p.GetDrillSizeY())) / 2
                    if p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                        hc = max(self.holeclr, self.clr) - self.clr
                        self.static.append((L, -1, "circ", (mm(p.GetPosition().x), mm(p.GetPosition().y), r), hc))
        for z in b.Zones():
            if z.GetIsRuleArea() and (z.GetDoNotAllowTracks() or z.GetDoNotAllowVias()):
                ps = z.Outline()
                for L, lay in enumerate((pcbnew.F_Cu, pcbnew.B_Cu)):
                    if z.IsOnLayer(lay):
                        for i in range(ps.OutlineCount()):
                            ol = ps.Outline(i)
                            poly = np.array([(mm(ol.CPoint(j).x), mm(ol.CPoint(j).y)) for j in range(ol.PointCount())])
                            self.static.append((L, -1, "poly", poly, -self.clr))
        # copper / mask graphics
        items = list(b.GetDrawings())
        for fp in b.GetFootprints():
            items += list(fp.GraphicalItems())
            try:
                items += [fp.Reference(), fp.Value()]
            except Exception:
                pass
        lays = ((pcbnew.F_Cu, 0, 0.0), (pcbnew.B_Cu, 1, 0.0), (pcbnew.F_Mask, 0, -self.clr), (pcbnew.B_Mask, 1, -self.clr))
        for it in items:
            for lay, L, extra in lays:
                try:
                    if not it.IsOnLayer(lay):
                        continue
                    if it.GetClass() in ("PCB_TEXT", "PCB_FIELD", "PCB_TEXTBOX"):
                        if hasattr(it, "IsVisible") and not it.IsVisible():
                            continue
                        bb = it.GetBoundingBox()
                        x0, y0, x1, y1 = mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())
                        self.static.append((L, -1, "poly", np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]), extra))
                    else:
                        ps = pcbnew.SHAPE_POLY_SET()
                        it.TransformShapeToPolygon(ps, lay, 0, 5000, pcbnew.ERROR_OUTSIDE)
                        for i in range(ps.OutlineCount()):
                            ol = ps.Outline(i)
                            poly = np.array([(mm(ol.CPoint(j).x), mm(ol.CPoint(j).y)) for j in range(ol.PointCount())])
                            if len(poly) >= 3:
                                self.static.append((L, -1, "poly", poly, extra))
                except Exception as e:
                    log("graphic err", e)
        # pad mask apertures
        for p in self.pads:
            for lay, L in ((pcbnew.F_Mask, 0), (pcbnew.B_Mask, 1)):
                try:
                    if p.IsOnLayer(lay) and not p.IsOnLayer(pcbnew.F_Cu if L == 0 else pcbnew.B_Cu):
                        for poly in self.pad_polys(p, lay):
                            self.static.append((L, -1, "poly", poly, -self.clr + MARGIN))
                        continue
                    if p.IsOnLayer(lay):
                        ex = max(mm(p.GetSolderMaskExpansion(lay)), mm(p.GetSolderMaskExpansion(pcbnew.F_Cu if L == 0 else pcbnew.B_Cu)), mm(self.ds.m_SolderMaskExpansion))
                        if ex > self.clr - MARGIN:
                            net = p.GetNetCode() if p.GetNetCode() > 0 else -1
                            for poly in self.pad_polys(p, pcbnew.F_Cu if L == 0 else pcbnew.B_Cu):
                                self.static.append((L, net, "poly", poly, ex - self.clr + MARGIN))
                except Exception as e:
                    log("mask err", e)
        # existing tracks/vias
        for t in b.GetTracks():
            if t.GetClass() == "PCB_VIA":
                pos = t.GetPosition()
                for L in (0, 1):
                    self.static.append((L, t.GetNetCode() or -1, "circ", (mm(pos.x), mm(pos.y), mm(t.GetWidth(pcbnew.F_Cu)) / 2), 0))
            else:
                L = 0 if t.GetLayer() == pcbnew.F_Cu else 1
                s, e = t.GetStart(), t.GetEnd()
                self.static.append((L, t.GetNetCode() or -1, "seg", (mm(s.x), mm(s.y), mm(e.x), mm(e.y), mm(t.GetWidth()) / 2), 0))
        self.pad_info = {}
        for i, p in enumerate(self.pads):
            pass

    def pad_polys(self, p, lay):
        ps = p.GetEffectivePolygon(lay, pcbnew.ERROR_OUTSIDE)
        out = []
        for i in range(ps.OutlineCount()):
            ol = ps.Outline(i)
            out.append(np.array([(mm(ol.CPoint(j).x), mm(ol.CPoint(j).y)) for j in range(ol.PointCount())]))
        return out

    def outline_mask(self):
        b = self.board
        ps = pcbnew.SHAPE_POLY_SET()
        ok = b.GetBoardPolygonOutlines(ps)
        inside = np.zeros((self.ny, self.nx), bool)
        edges = []
        ys = self.oy + np.arange(self.ny) * self.g
        xs = self.ox + np.arange(self.nx) * self.g
        polys = []
        for i in range(ps.OutlineCount()):
            polys.append(ps.Outline(i))
            for hI in range(ps.HoleCount(i)):
                polys.append(ps.Hole(i, hI))
        for ol in polys:
            pts = np.array([(mm(ol.CPoint(j).x), mm(ol.CPoint(j).y)) for j in range(ol.PointCount())])
            n = len(pts)
            for j in range(n):
                edges.append((pts[j][0], pts[j][1], pts[(j + 1) % n][0], pts[(j + 1) % n][1]))
        E = np.array(edges) if edges else np.zeros((0, 4))
        if len(E) == 0:
            inside[:] = True
        else:
            X = xs[None, :]
            for iy, y in enumerate(ys):
                a = E[:, 1]; c = E[:, 3]
                cross = ((a <= y) & (c > y)) | ((c <= y) & (a > y))
                if not cross.any():
                    continue
                e = E[cross]
                xi = e[:, 0] + (y - e[:, 1]) * (e[:, 2] - e[:, 0]) / (e[:, 3] - e[:, 1])
                cntx = (xi[None, :] < xs[:, None]).sum(1)
                inside[iy] = (cntx % 2) == 1
        self.inside = inside
        self.edges = E

    # ------------------------------------------------------------ maps
    def window(self, xmin, ymin, xmax, ymax):
        g = self.g
        i0 = max(0, int(math.floor((xmin - self.ox) / g)))
        i1 = min(self.nx, int(math.ceil((xmax - self.ox) / g)) + 1)
        j0 = max(0, int(math.floor((ymin - self.oy) / g)))
        j1 = min(self.ny, int(math.ceil((ymax - self.oy) / g)) + 1)
        return i0, i1, j0, j1

    def dist_geom(self, kind, geom, rmax):
        g = self.g
        if kind == "poly":
            P = geom
            xmin, ymin = P.min(0) - rmax
            xmax, ymax = P.max(0) + rmax
        elif kind == "circ":
            x, y, r = geom
            xmin, ymin, xmax, ymax = x - r - rmax, y - r - rmax, x + r + rmax, y + r + rmax
        else:
            x1, y1, x2, y2, hw = geom
            xmin, xmax = min(x1, x2) - hw - rmax, max(x1, x2) + hw + rmax
            ymin, ymax = min(y1, y2) - hw - rmax, max(y1, y2) + hw + rmax
        i0, i1, j0, j1 = self.window(xmin, ymin, xmax, ymax)
        if i0 >= i1 or j0 >= j1:
            return None
        X = (self.ox + np.arange(i0, i1) * g)[None, :]
        Y = (self.oy + np.arange(j0, j1) * g)[:, None]
        if kind == "poly":
            d = np.full((j1 - j0, i1 - i0), 1e9)
            ins = np.zeros((j1 - j0, i1 - i0), bool)
            n = len(P)
            for k in range(n):
                ax, ay = P[k]; bx, by = P[(k + 1) % n]
                d = np.minimum(d, seg_dist(X, Y, ax, ay, bx, by))
                if (ay > Y[0, 0] - 1e9):
                    cond = ((ay <= Y) & (by > Y)) | ((by <= Y) & (ay > Y))
                    if by != ay:
                        xi = ax + (Y - ay) * (bx - ax) / (by - ay)
                        ins ^= cond & (X < xi)
            d[ins] = 0
        elif kind == "circ":
            x, y, r = geom
            d = np.maximum(np.sqrt((X - x) ** 2 + (Y - y) ** 2) - r, 0)
        else:
            x1, y1, x2, y2, hw = geom
            d = np.maximum(seg_dist(X, Y, x1, y1, x2, y2) - hw, 0)
        return (j0, j1, i0, i1), d

    def paint(self, maps, L, net, kind, geom, extra, rads):
        rmax = max(rads) + extra
        r = self.dist_geom(kind, geom, rmax)
        if r is None:
            return
        (j0, j1, i0, i1), d = r
        for M, rk in zip(maps, rads):
            m = d < rk + extra
            if not m.any():
                continue
            sub = M[L][j0:j1, i0:i1]
            if net == -1:
                sub[m] = -1
            else:
                cur = sub[m]
                sub[m] = np.where((cur == 0) | (cur == net), net, -1)

    def build_static(self):
        nr = len(self.rads)
        self.maps0 = [[np.zeros((self.ny, self.nx), np.int32) for _ in range(2)] for _ in range(nr)]
        self.vmap0 = [np.zeros((self.ny, self.nx), np.int32) for _ in range(2)]
        allm = self.maps0 + [self.vmap0]
        allr = self.rads + [self.rv]
        # board edge
        for k, M in enumerate(allm):
            r = allr[k] - self.clr - MARGIN + self.edgeclr + MARGIN
            for L in (0, 1):
                M[L][~self.inside] = -1
                M[L][0, :] = -1; M[L][-1, :] = -1; M[L][:, 0] = -1; M[L][:, -1] = -1
            for e in self.edges:
                res = self.dist_geom("seg", (e[0], e[1], e[2], e[3], 0.0), r)
                if res is None:
                    continue
                (j0, j1, i0, i1), d = res
                m = d < r
                for L in (0, 1):
                    M[L][j0:j1, i0:i1][m] = -1
        for (L, net, kind, geom, extra) in self.static:
            self.paint(allm, L, net, kind, geom, extra, allr)

    def reset_maps(self):
        self.maps = [[a.copy() for a in M] for M in self.maps0]
        self.vmap = [a.copy() for a in self.vmap0]

    def paint_route(self, net, segs, vias):
        allm = self.maps + [self.vmap]
        allr = self.rads + [self.rv]
        for (L, x1, y1, x2, y2, w) in segs:
            self.paint(allm, L, net, "seg", (x1, y1, x2, y2, w / 2), 0, allr)
        for (x, y) in vias:
            for L in (0, 1):
                self.paint(allm, L, net, "circ", (x, y, self.vdia / 2), 0, allr)
            # hole-to-hole: no other via within drill+h2h
            res = self.dist_geom("circ", (x, y, 0), self.vdrill + self.h2h + 0.05)
            if res:
                (j0, j1, i0, i1), d = res
                m = d < self.vdrill + self.h2h + 0.05
                for L in (0, 1):
                    self.vmap[L][j0:j1, i0:i1][m] = -1

    # ------------------------------------------------------------ pads
    def pad_cells(self, p, net, ok):
        """cells (flat L*N+idx) inside the pad usable for routing; plus stub cells"""
        res = []
        N = self.N
        for L, lay in enumerate((pcbnew.F_Cu, pcbnew.B_Cu)):
            if not p.IsOnLayer(lay):
                continue
            for poly in self.pad_polys(p, lay):
                r = self.dist_geom("poly", poly, 0)
                if r is None:
                    continue
                (j0, j1, i0, i1), d = r
                jj, ii = np.nonzero(d <= 0)
                for j, i in zip(jj + j0, ii + i0):
                    idx = L * N + j * self.nx + i
                    if ok[idx]:
                        res.append(idx)
        return res

    # ------------------------------------------------------------ A*
    def astar(self, sources, targets, tgt_xy, net, ok, okv, max_exp=400000):
        nx, N = self.nx, self.N
        g = self.g
        viac = max(8.0, 1.2 / g) * 1.0
        offs = [(1, 1.0, 0), (-1, 1.0, 0), (nx, 1.0, 1), (-nx, 1.0, 1),
                (nx + 1, 1.4142, 2), (nx - 1, 1.4142, 2), (-nx + 1, 1.4142, 2), (-nx - 1, 1.4142, 2)]
        cxs = [((t % N) % nx) for t in tgt_xy]; cys = [((t % N) // nx) for t in tgt_xy]
        ax_ = (min(cxs) + max(cxs)) / 2.0; ay_ = (min(cys) + max(cys)) / 2.0
        rx_ = (max(cxs) - min(cxs)) / 2.0; ry_ = (max(cys) - min(cys)) / 2.0
        HW = 1.15
        def hfun(idx):
            c = idx % N
            dx = abs(c % nx - ax_) - rx_; dy = abs(c // nx - ay_) - ry_
            if dx < 0: dx = 0
            if dy < 0: dy = 0
            if dx < dy:
                return HW * (dy + 0.4142 * dx)
            return HW * (dx + 0.4142 * dy)
        gs = {}
        prev = {}
        heap = []
        for s, c0 in sources:
            if gs.get(s, 1e18) > c0:
                gs[s] = c0
                prev[s] = -1
                heapq.heappush(heap, (c0 + hfun(s), c0, s))
        tset = targets
        exp = 0
        cong = self.cong
        while heap:
            f, gc, idx = heapq.heappop(heap)
            if gc > gs.get(idx, 1e18):
                continue
            if idx in tset:
                path = [idx]
                while prev[path[-1]] != -1:
                    path.append(prev[path[-1]])
                return path[::-1]
            exp += 1
            if exp > max_exp:
                return None
            L = idx // N
            c = idx - L * N
            # preferred direction: L0 horizontal, L1 vertical
            for off, cost, kind in offs:
                ni = idx + off
                if not ok[ni]:
                    continue
                if kind != 2 and kind != L:
                    cost = cost * 1.6
                elif kind == 2:
                    cost = cost * 1.15
                ng = gc + cost + cong[ni]
                if ng < gs.get(ni, 1e18):
                    gs[ni] = ng
                    prev[ni] = idx
                    heapq.heappush(heap, (ng + hfun(ni), ng, ni))
            if okv[c]:
                ni = (1 - L) * N + c
                if ok[ni]:
                    ng = gc + viac
                    if ng < gs.get(ni, 1e18):
                        gs[ni] = ng
                        prev[ni] = idx
                        heapq.heappush(heap, (ng + hfun(ni), ng, ni))
        return None

    def xy(self, c):
        return (self.ox + (c % self.nx) * self.g, self.oy + (c // self.nx) * self.g)

    def path_to_geom(self, path, w):
        N = self.N
        segs, vias = [], []
        pts = []  # runs per layer
        cur = []
        curL = path[0] // N
        for idx in path:
            L = idx // N
            c = idx % N
            if L != curL:
                if len(cur) >= 1:
                    pts.append((curL, cur))
                vias.append(self.xy(c))
                cur = [c]
                curL = L
            else:
                cur.append(c)
        pts.append((curL, cur))
        for L, cells in pts:
            if len(cells) < 2:
                continue
            # compress collinear
            nodes = [cells[0]]
            for k in range(1, len(cells) - 1):
                a, b, c = nodes[-1] if False else cells[k - 1], cells[k], cells[k + 1]
                if b - a != c - b:
                    nodes.append(b)
            nodes.append(cells[-1])
            for a, b in zip(nodes, nodes[1:]):
                x1, y1 = self.xy(a); x2, y2 = self.xy(b)
                segs.append((L, x1, y1, x2, y2, w))
        return segs, vias

    # ------------------------------------------------------------ net routing
    def route_net(self, net, max_exp):
        w = self.width[net]
        rk = self.rad_index[round(w / 2 + self.clr + MARGIN, 4)]
        M = self.maps[rk]
        N = self.N
        okL = [((M[L] == 0) | (M[L] == net)) for L in (0, 1)]
        okv_a = ((self.vmap[0] == 0) | (self.vmap[0] == net)) & ((self.vmap[1] == 0) | (self.vmap[1] == net))
        ok = np.concatenate([okL[0].ravel(), okL[1].ravel()]).astype(np.uint8).tobytes()
        okv = okv_a.ravel().astype(np.uint8).tobytes()
        pads = [p for p in self.pads if p.GetNetCode() == net]
        cells = [self.pad_cells(p, net, ok) for p in pads]
        # pads with no usable cells: try to make stubs
        stubs = {}
        for k, p in enumerate(pads):
            if not cells[k]:
                cs, st = self.stub_cells(p, net, ok, w)
                cells[k] = cs
                stubs[k] = st
        allsegs, allvias = [], []
        if any(not c for c in cells):
            failed = True
        connected = [0]
        tree = set(cells[0])
        remaining = list(range(1, len(pads)))
        failed = False
        srcc = {c: stubs.get(0, {}).get(c, 0.0) for c in cells[0]}
        used_stub = {}
        while remaining:
            if time.time() > DEADLINE:
                failed = True
                break
            # nearest remaining pad to any connected pad
            def dist(k):
                pk = pads[k].GetPosition()
                return min((pk.x - pads[j].GetPosition().x) ** 2 + (pk.y - pads[j].GetPosition().y) ** 2 for j in connected)
            remaining.sort(key=dist)
            k = remaining[0]
            if not cells[k] or not tree:
                failed = True
                remaining.pop(0)
                continue
            tset = set(cells[k])
            if tset & tree:
                path = [next(iter(tset & tree))]
            else:
                srcs = [(c, srcc.get(c, 0.0)) for c in tree]
                dmin = math.sqrt(dist(k)) / 1e6
                cap = int(min(max_exp, (30000 + 600 * dmin / self.g) * self.capmul))
                path = self.astar(srcs, tset, cells[k], net, ok, okv, cap)
            remaining.pop(0)
            if path is None:
                failed = True
                continue
            segs, vias = self.path_to_geom(path, w)
            # stubs
            for kk in (k,):
                if kk in stubs and path[-1] in stubs[kk]:
                    pos = pads[kk].GetPosition()
                    x, y = self.xy(path[-1] % N)
                    segs.append((path[-1] // N, x, y, mm(pos.x), mm(pos.y), w))
            if 0 in stubs and path[0] in stubs[0] and 0 not in used_stub:
                used_stub[0] = True
                pos = pads[0].GetPosition()
                x, y = self.xy(path[0] % N)
                segs.append((path[0] // N, x, y, mm(pos.x), mm(pos.y), w))
            allsegs += segs
            allvias += vias
            connected.append(k)
            tree |= set(path)
            for c in cells[k]:
                tree.add(c)
            if k in stubs:
                # only the used stub cell belongs to the tree as pad copper
                tree -= set(cells[k])
                tree |= set(path)
        return (not failed), allsegs, allvias

    def stub_cells(self, p, net, ok, w):
        """cells near the pad reachable by a straight stub from pad centre that clears other copper"""
        pos = p.GetPosition()
        px, py = mm(pos.x), mm(pos.y)
        N, nx, g = self.N, self.nx, self.g
        res, cost = [], {}
        R = 1.2
        i0, i1, j0, j1 = self.window(px - R, py - R, px + R, py + R)
        for L, lay in enumerate((pcbnew.F_Cu, pcbnew.B_Cu)):
            if not p.IsOnLayer(lay):
                continue
            cand = []
            for j in range(j0, j1):
                for i in range(i0, i1):
                    idx = L * N + j * nx + i
                    if ok[idx]:
                        x, y = self.ox + i * g, self.oy + j * g
                        d = math.hypot(x - px, y - py)
                        if d <= R:
                            cand.append((d, idx, x, y))
            cand.sort()
            for d, idx, x, y in cand[:60]:
                if self.stub_clear(L, net, px, py, x, y, w):
                    res.append(idx)
                    cost[idx] = d / g
                    if len(res) >= 6:
                        break
        return res, cost

    def stub_clear(self, L, net, x1, y1, x2, y2, w):
        need = w / 2 + self.clr + MARGIN
        for (sL, snet, kind, geom, extra) in self.static_near(L, x1, y1):
            if snet == net:
                continue
            if kind == "poly":
                if poly_seg_dist(geom, x1, y1, x2, y2) < need + extra:
                    return False
            elif kind == "circ":
                x, y, r = geom
                if float(seg_dist(np.array(x), np.array(y), x1, y1, x2, y2)) - r < need + extra:
                    return False
        for (rnet, segs, vias) in self.routed_geom:
            if rnet == net:
                continue
            for (sL, a, b, c, d, sw) in segs:
                if sL == L and abs(a - x1) < 5 and abs(b - y1) < 5 and segseg_dist(a, b, c, d, x1, y1, x2, y2) < need + sw / 2:
                    return False
            for (vx, vy) in vias:
                if abs(vx - x1) < 3 and abs(vy - y1) < 3 and float(seg_dist(np.array(vx), np.array(vy), x1, y1, x2, y2)) < need + self.vdia / 2:
                    return False
        return True

    def static_near(self, L, x, y):
        if not hasattr(self, "_sidx"):
            self._sidx = {}
            for item in self.static:
                sL, snet, kind, geom, extra = item
                if kind == "poly":
                    cx, cy = geom.mean(0)
                    rr = np.max(np.hypot(geom[:, 0] - cx, geom[:, 1] - cy))
                elif kind == "circ":
                    cx, cy, rr = geom
                else:
                    cx, cy = (geom[0] + geom[2]) / 2, (geom[1] + geom[3]) / 2
                    rr = math.hypot(geom[2] - geom[0], geom[3] - geom[1]) / 2 + geom[4]
                self._sidx.setdefault(sL, []).append((cx, cy, rr, item))
        out = []
        for cx, cy, rr, item in self._sidx.get(L, []):
            if math.hypot(cx - x, cy - y) < rr + 2.0:
                out.append(item)
        return out

    # ------------------------------------------------------------ main loop
    def route_all(self, order, max_exp, capmul=1):
        self.capmul = capmul
        self.reset_maps()
        self.routed_geom = []
        self.cong = bytes(2 * self.N)
        ok_nets = []
        result = {}
        for net in order:
            if time.time() > DEADLINE:
                break
            okk, segs, vias = self.route_net(net, max_exp)
            result[net] = (okk, segs, vias)
            self.routed_geom.append((net, segs, vias))
            self.paint_route(net, segs, vias)
            for (a, b_, sk) in self.pairs:
                if net in (a, b_) and a in result and b_ in result and result[a][0] and result[b_][0]:
                    try:
                        self.tune(result, a, b_, sk)
                    except Exception as e:
                        log("tune err", e)
            if okk:
                ok_nets.append(net)
        return result

    def net_ok_bytes(self, net):
        w = self.width[net]
        rk = self.rad_index[round(w / 2 + self.clr + MARGIN, 4)]
        M = self.maps[rk]
        return np.concatenate([((M[L] == 0) | (M[L] == net)).ravel() for L in (0, 1)]).astype(np.uint8).tobytes()

    def seg_free(self, ok, L, x1, y1, x2, y2):
        g = self.g
        n = int(math.hypot(x2 - x1, y2 - y1) / (g / 3)) + 1
        N, nx = self.N, self.nx
        for k in range(n + 1):
            t = k / n
            fx = (x1 + (x2 - x1) * t - self.ox) / g
            fy = (y1 + (y2 - y1) * t - self.oy) / g
            for i in {int(math.floor(fx)), int(math.ceil(fx))}:
                for j in {int(math.floor(fy)), int(math.ceil(fy))}:
                    if i < 0 or j < 0 or i >= nx or j >= self.ny or not ok[L * N + j * nx + i]:
                        return False
        return True

    @staticmethod
    def seglen(segs):
        return sum(math.hypot(s[3] - s[1], s[4] - s[2]) for s in segs)

    def tune(self, result, a, b, sk):
        la, lb = self.seglen(result[a][1]), self.seglen(result[b][1])
        if abs(la - lb) <= sk * 0.4:
            return
        short = a if la < lb else b
        deficit = abs(la - lb)
        okk, segs, vias = result[short]
        w = self.width[short]
        ok = self.net_ok_bytes(short)
        order = sorted(range(len(segs)), key=lambda i: -math.hypot(segs[i][3] - segs[i][1], segs[i][4] - segs[i][2]))
        segs = list(segs)
        newsegs_all = []
        for i in order:
            if deficit <= sk * 0.2:
                break
            L, x1, y1, x2, y2, _ = segs[i]
            ln = math.hypot(x2 - x1, y2 - y1)
            sp = max(w + self.clr + 0.1, 3 * self.g)  # bump leg spacing
            if ln < 3 * sp:
                continue
            ux, uy = (x2 - x1) / ln, (y2 - y1) / ln
            done = False
            for side in (1, -1):
                if done:
                    break
                vx, vy = -uy * side, ux * side
                nb_max = int((ln - sp) // (2 * sp))
                for h in (deficit / 2, 1.5, 1.0, 0.7, 0.5, 0.35, 0.25):
                    if h > deficit / 2 + 1e-9 or h < w:
                        continue
                    nb = int(min(nb_max, math.ceil(deficit / (2 * h) - 1e-9)))
                    if nb < 1:
                        continue
                    hh = min(h, deficit / (2 * nb))
                    if hh < w * 0.75:
                        continue
                    pts = [(x1, y1)]
                    start = (ln - (2 * nb - 1) * sp) / 2
                    for k in range(nb):
                        t0 = start + 2 * k * sp
                        t1 = t0 + sp
                        p0 = (x1 + ux * t0, y1 + uy * t0)
                        p1 = (x1 + ux * t1, y1 + uy * t1)
                        pts += [p0, (p0[0] + vx * hh, p0[1] + vy * hh), (p1[0] + vx * hh, p1[1] + vy * hh), p1]
                    pts.append((x2, y2))
                    good = True
                    for (p, q) in zip(pts, pts[1:]):
                        if not self.seg_free(ok, L, p[0], p[1], q[0], q[1]):
                            good = False
                            break
                    if good:
                        ns = [(L, p[0], p[1], q[0], q[1], w) for p, q in zip(pts, pts[1:])]
                        segs[i] = None
                        newsegs_all += ns
                        deficit -= 2 * hh * nb
                        self.paint_route(short, ns, [])
                        ok = self.net_ok_bytes(short)
                        done = True
                        break
        segs = [s_ for s_ in segs if s_ is not None] + newsegs_all
        result[short] = (okk, segs, vias)
        if deficit > sk * 0.45:
            log("tune failed", self.netname[short], deficit)
            result[short] = (False, segs, vias)

    def save(self, result, out):
        b = pcbnew.LoadBoard(self.path)
        for net, (okk, segs, vias) in result.items():
            ni = b.FindNet(self.netname[net])
            for (L, x1, y1, x2, y2, w) in segs:
                if abs(x1 - x2) < 1e-6 and abs(y1 - y2) < 1e-6:
                    continue
                t = pcbnew.PCB_TRACK(b)
                t.SetStart(pcbnew.VECTOR2I(int(round(x1 * 1e6)), int(round(y1 * 1e6))))
                t.SetEnd(pcbnew.VECTOR2I(int(round(x2 * 1e6)), int(round(y2 * 1e6))))
                t.SetWidth(int(round(w * 1e6)))
                t.SetLayer(pcbnew.F_Cu if L == 0 else pcbnew.B_Cu)
                t.SetNet(ni)
                b.Add(t)
            for (x, y) in vias:
                v = pcbnew.PCB_VIA(b)
                v.SetPosition(pcbnew.VECTOR2I(int(round(x * 1e6)), int(round(y * 1e6))))
                v.SetViaType(pcbnew.VIATYPE_THROUGH)
                v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                v.SetDrill(int(round(self.vdrill * 1e6)))
                v.SetWidth(pcbnew.F_Cu, int(round(self.vdia * 1e6)))
                v.SetNet(ni)
                b.Add(v)
        tmp = out + ".tmp.kicad_pcb"
        pcbnew.SaveBoard(tmp, b)
        os.replace(tmp, out)


def seg_dist(X, Y, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return np.sqrt((X - ax) ** 2 + (Y - ay) ** 2)
    t = np.clip(((X - ax) * dx + (Y - ay) * dy) / L2, 0, 1)
    return np.sqrt((X - ax - t * dx) ** 2 + (Y - ay - t * dy) ** 2)


def segseg_dist(ax, ay, bx, by, cx, cy, dx, dy):
    def ccw(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1 = ccw(ax, ay, bx, by, cx, cy); d2 = ccw(ax, ay, bx, by, dx, dy)
    d3 = ccw(cx, cy, dx, dy, ax, ay); d4 = ccw(cx, cy, dx, dy, bx, by)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return float(min(seg_dist(np.array(ax), np.array(ay), cx, cy, dx, dy),
                     seg_dist(np.array(bx), np.array(by), cx, cy, dx, dy),
                     seg_dist(np.array(cx), np.array(cy), ax, ay, bx, by),
                     seg_dist(np.array(dx), np.array(dy), ax, ay, bx, by)))


def point_in_poly(P, x, y):
    n = len(P); ins = False
    for k in range(n):
        ax, ay = P[k]; bx, by = P[(k + 1) % n]
        if (ay <= y) != (by <= y):
            xi = ax + (y - ay) * (bx - ax) / (by - ay)
            if x < xi:
                ins = not ins
    return ins


def poly_seg_dist(P, x1, y1, x2, y2):
    if point_in_poly(P, x1, y1) or point_in_poly(P, x2, y2):
        return 0.0
    n = len(P)
    best = 1e9
    for k in range(n):
        ax, ay = P[k]; bx, by = P[(k + 1) % n]
        best = min(best, segseg_dist(ax, ay, bx, by, x1, y1, x2, y2))
    return best


def main():
    inp, out = sys.argv[1], sys.argv[2]
    try:
        import shutil
        shutil.copy(inp, out)
    except Exception:
        pass
    R = Router(inp)
    R.build_static()
    log("static built")
    nets = list(R.route_nets)
    def hp(n):
        ps = [p.GetPosition() for p in R.pads if p.GetNetCode() == n]
        xs = [mm(p.x) for p in ps]; ys = [mm(p.y) for p in ps]
        return (max(xs) - min(xs)) + (max(ys) - min(ys))
    hpw = {n: hp(n) for n in nets}
    order = sorted(nets, key=lambda n: (len([p for p in R.pads if p.GetNetCode() == n]) > 12, hpw[n]))
    best = None
    prio = {n: i for i, n in enumerate(order)}
    random.seed(1)
    bestscore = -1
    tried = 0
    while time.time() < DEADLINE:
        tried += 1
        res = R.route_all(order, 300000 * (3 if tried == 2 else 1), capmul=(3 if tried == 2 else 1))
        good = [n for n in order if n in res and res[n][0]]
        bad = [n for n in order if n not in res or not res[n][0]]
        log("pass", tried, "good", len(good), "bad", len(bad), [R.netname[n] for n in bad][:20])
        if len(good) > bestscore:
            bestscore = len(good)
            best = res
            R.save(res, out)
            log("saved")
        if not bad:
            break
        if tried >= 2:
            for n in bad:
                prio[n] -= len(nets) * random.uniform(0.2, 0.8)
            order = sorted(nets, key=lambda n: prio[n])
    log("done", bestscore, "/", len(nets))


if __name__ == "__main__":
    main()
