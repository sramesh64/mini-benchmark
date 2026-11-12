#!/usr/bin/env python3
"""Grid A* router for 2-layer KiCad boards with rip-up and reroute."""
import sys, os, re, json, math, time, heapq
import numpy as np

T0 = time.time()
DEADLINE = float(os.environ.get("ROUTE_DEADLINE", "370"))
HARD = float(os.environ.get("ROUTE_HARD", "430"))
import pcbnew

def log(*a):
    print("[%.1f]" % (time.time() - T0), *a, file=sys.stderr, flush=True)

def elapsed():
    return time.time() - T0

MM = 1e6

# ---------------------------------------------------------------- rules
def load_rules(pcb_path, board):
    base = os.path.splitext(pcb_path)[0]
    pro, dru = {}, ""
    try:
        pro = json.load(open(base + ".kicad_pro"))
    except Exception:
        pass
    try:
        dru = open(base + ".kicad_dru").read()
    except Exception:
        pass
    ds = board.GetDesignSettings()
    rules = pro.get("board", {}).get("design_settings", {}).get("rules", {})
    classes = pro.get("net_settings", {}).get("classes", [])
    clr = max([c.get("clearance", 0.2) or 0.0 for c in classes] + [rules.get("min_clearance", 0.0) or 0.0, 0.1])
    r = dict(
        clearance=clr,
        min_width=max(rules.get("min_track_width", 0.15) or 0.15, 0.1),
        hole_clr=rules.get("min_hole_clearance", 0.25) or 0.0,
        h2h=max(rules.get("min_hole_to_hole", 0.25) or 0.0, 0.25),
        edge=max(rules.get("min_copper_edge_clearance", 0.0) or 0.0, 0.0),
        via_min_d=rules.get("min_via_diameter", 0.5) or 0.5,
        via_min_drill=rules.get("min_through_hole_diameter", 0.3) or 0.3,
        via_ann=max(rules.get("min_via_annular_width", 0.1) or 0.1, rules.get("min_via_annulus", 0) or 0),
    )
    # edge clearance from board settings too
    try:
        r["edge"] = max(r["edge"], ds.m_CopperEdgeClearance / MM)
    except Exception:
        pass
    netw = {}
    skew = {}
    for m in re.finditer(r'\(rule\s+"[^"]*"\s*\(condition\s+"((?:[^"\\]|\\.)*)"\)\s*\(constraint\s+(\w+)\s*\((\w+)\s+([\d.]+)mm\)', dru):
        cond, kind, mm, val = m.group(1), m.group(2), m.group(3), float(m.group(4))
        if kind == "track_width" and mm == "min":
            for n in re.findall(r"A\.NetName == '((?:[^'\\]|\\.)*)'", cond):
                netw[n] = max(netw.get(n, 0), val)
        if kind == "skew":
            for p in re.findall(r"A\.inDiffPair\('((?:[^'\\]|\\.)*)'\)", cond):
                skew[p] = val
    r["netw"] = netw
    r["skew"] = skew
    return r

# ---------------------------------------------------------------- geometry helpers
def poly_from_shapeset(ps):
    """list of numpy arrays (outlines) in mm"""
    out = []
    for i in range(ps.OutlineCount()):
        ol = ps.Outline(i)
        pts = np.array([[ol.CPoint(k).x / MM, ol.CPoint(k).y / MM] for k in range(ol.PointCount())])
        if len(pts) >= 3:
            out.append(pts)
        for h in range(ps.HoleCount(i)):
            hl = ps.Hole(i, h)
            pts = np.array([[hl.CPoint(k).x / MM, hl.CPoint(k).y / MM] for k in range(hl.PointCount())])
            if len(pts) >= 3:
                out.append(pts)
    return out

def points_in_polys(px, py, polys):
    """even-odd fill over all rings"""
    inside = np.zeros(px.shape, bool)
    for pts in polys:
        x1 = pts[:, 0]; y1 = pts[:, 1]
        x2 = np.roll(x1, -1); y2 = np.roll(y1, -1)
        for a, b, c, d in zip(x1, y1, x2, y2):
            if b == d:
                continue
            cond = (py >= min(b, d)) & (py < max(b, d))
            if not cond.any():
                continue
            xint = a + (py - b) * (c - a) / (d - b)
            inside ^= cond & (px < xint)
    return inside


class Grid:
    def __init__(self, x0, y0, W, H, res):
        self.x0, self.y0, self.W, self.H, self.res = x0, y0, W, H, res

    def cell(self, x, y):
        return int(round((y - self.y0) / self.res)), int(round((x - self.x0) / self.res))

    def xy(self, r, c):
        return self.x0 + c * self.res, self.y0 + r * self.res

    def bbox_cells(self, xmin, ymin, xmax, ymax, pad=0):
        c0 = max(0, int(math.floor((xmin - self.x0) / self.res)) - pad)
        c1 = min(self.W, int(math.ceil((xmax - self.x0) / self.res)) + 1 + pad)
        r0 = max(0, int(math.floor((ymin - self.y0) / self.res)) - pad)
        r1 = min(self.H, int(math.ceil((ymax - self.y0) / self.res)) + 1 + pad)
        return r0, r1, c0, c1

    def raster_polys(self, polys, target, value, mode="set"):
        if not polys:
            return None
        allp = np.vstack(polys)
        r0, r1, c0, c1 = self.bbox_cells(allp[:, 0].min(), allp[:, 1].min(), allp[:, 0].max(), allp[:, 1].max())
        if r0 >= r1 or c0 >= c1:
            return None
        cy, cx = np.mgrid[r0:r1, c0:c1]
        px = self.x0 + cx * self.res; py = self.y0 + cy * self.res
        m = points_in_polys(px, py, polys)
        if target is not None:
            sub = target[r0:r1, c0:c1]
            sub[m] = value
        return (r0, r1, c0, c1, m)

    def capsule(self, xa, ya, xb, yb, rad):
        r0, r1, c0, c1 = self.bbox_cells(min(xa, xb) - rad, min(ya, yb) - rad, max(xa, xb) + rad, max(ya, yb) + rad)
        if r0 >= r1 or c0 >= c1:
            return r0, c0, np.zeros((0, 0), bool)
        cy, cx = np.mgrid[r0:r1, c0:c1]
        px = self.x0 + cx * self.res; py = self.y0 + cy * self.res
        dx, dy = xb - xa, yb - ya
        L2 = dx * dx + dy * dy
        if L2 < 1e-12:
            t = np.zeros_like(px)
        else:
            t = np.clip(((px - xa) * dx + (py - ya) * dy) / L2, 0, 1)
        qx = xa + t * dx; qy = ya + t * dy
        d2 = (px - qx) ** 2 + (py - qy) ** 2
        return r0, c0, d2 <= rad * rad


_hcache = {}
def dilate(mask, R):
    """binary dilation by Euclidean disk radius R (cells)"""
    if R <= 0:
        return mask.copy()
    Ri = int(math.floor(R))
    H, W = mask.shape
    out = np.zeros_like(mask)
    hd = {}
    cs = np.zeros((H, W + 1), np.int32)
    np.cumsum(mask, axis=1, out=cs[:, 1:])
    for dy in range(-Ri, Ri + 1):
        hx = int(math.floor(math.sqrt(max(R * R - dy * dy, 0))))
        if hx not in hd:
            lo = np.clip(np.arange(W) - hx, 0, W)
            hi = np.clip(np.arange(W) + hx + 1, 0, W)
            hd[hx] = (cs[:, hi] - cs[:, lo]) > 0
        h = hd[hx]
        if dy >= 0:
            if dy < H:
                out[dy:, :] |= h[:H - dy, :]
        else:
            if -dy < H:
                out[:H + dy, :] |= h[-dy:, :]
    return out


# ---------------------------------------------------------------- router
class Router:
    def __init__(self, pcb_path):
        self.board = pcbnew.LoadBoard(pcb_path)
        b = self.board
        self.rules = load_rules(pcb_path, b)
        R = self.rules
        self.C = R["clearance"]
        cu = list(b.GetEnabledLayers().CuStack())
        self.layers = [cu[0], cu[-1]] if len(cu) >= 2 else cu
        self.lidx = {l: i for i, l in enumerate(self.layers)}
        bb = b.GetBoardEdgesBoundingBox()
        if bb.GetWidth() <= 0:
            bb = b.GetBoundingBox()
        x0, y0 = bb.GetX() / MM, bb.GetY() / MM
        w, h = bb.GetWidth() / MM, bb.GetHeight() / MM
        area = w * h
        res = max(0.05, math.sqrt(area / 1.4e6))
        # finer grid for fine boards
        res = round(res, 4)
        self.res = res
        W = int(math.ceil(w / res)) + 3
        H = int(math.ceil(h / res)) + 3
        self.g = Grid(x0 - res, y0 - res, W, H, res)
        log("grid", W, H, "res", res, "clr", self.C)
        NL = len(self.layers)
        self.pad_own = np.zeros((NL, H, W), np.int32)    # inflated pad copper owner (-1 = everyone)
        self.trk_own = np.zeros((NL, H, W), np.int32)
        self.holes = np.zeros((H, W), bool)
        self.edge = np.ones((H, W), bool)                 # outside board
        self.keep_trk = np.zeros((NL, H, W), bool)
        self.keep_via = np.zeros((H, W), bool)
        e = 0.71 * res
        # board outline
        ps = pcbnew.SHAPE_POLY_SET()
        if b.GetBoardPolygonOutlines(ps) and ps.OutlineCount():
            m = self.g.raster_polys(poly_from_shapeset(ps), None, 0)
            if m:
                r0, r1, c0, c1, mm_ = m
                self.edge[r0:r1, c0:c1] = ~mm_
        else:
            self.edge[1:-1, 1:-1] = False
        # nets
        self.netcode = {}
        self.netname = {}
        for name, net in b.GetNetsByName().items():
            name = str(name)
            if name:
                self.netcode[name] = net.GetNetCode()
                self.netname[net.GetNetCode()] = name
        # pads
        self.pads = {}   # code -> list of pad dicts
        self.padlist = []
        self.drawlist = []
        self.masklist = []
        maxerr = pcbnew.FromMM(0.005)
        for fp in b.GetFootprints():
            for p in fp.Pads():
                code = p.GetNetCode()
                lays = [self.lidx[l] for l in self.layers if p.IsOnLayer(l)]
                pclr = self.C
                try:
                    pclr = max(self.C, p.GetOwnClearance(self.layers[lays[0]]) / MM) if lays else self.C
                except Exception:
                    pass
                info = dict(code=code, layers=lays, cells={}, pos=(p.GetPosition().x / MM, p.GetPosition().y / MM), pad=p)
                self.padlist.append((p, code, lays, pclr))
                for li in lays:
                    L = self.layers[li]
                    sp2 = pcbnew.SHAPE_POLY_SET()
                    p.TransformShapeToPolygon(sp2, L, 0, maxerr, pcbnew.ERROR_INSIDE)
                    m = self.g.raster_polys(poly_from_shapeset(sp2), None, 0)
                    cells = []
                    if m:
                        r0, r1, c0, c1, mm_ = m
                        ys, xs = np.nonzero(mm_)
                        cells = list(zip((ys + r0).tolist(), (xs + c0).tolist()))
                    if not cells:
                        cells = [self.g.cell(*info["pos"])]
                    info["cells"][li] = cells
                if code > 0 and lays:
                    self.pads.setdefault(code, []).append(info)
        # copper drawings / texts / footprint graphics
        items = list(b.GetDrawings())
        for fp in b.GetFootprints():
            items += list(fp.GraphicalItems())
            for t in (fp.Reference(), fp.Value()):
                items.append(t)
        for d in items:
            for L in self.layers:
                try:
                    mk = pcbnew.F_Mask if L == pcbnew.F_Cu else pcbnew.B_Mask
                    if d.GetClass() == "PCB_SHAPE" and d.IsOnLayer(mk) and not d.IsOnLayer(L):
                        self.masklist.append((d, mk, L))
                        continue
                    if not d.IsOnLayer(L) or (d.GetClass() == "PCB_FIELD" and not d.IsVisible()):
                        continue
                    code = d.GetNetCode() if hasattr(d, "GetNetCode") else 0
                    self.drawlist.append((d, L, code))
                except Exception:
                    pass
        # rule areas
        for z in b.Zones():
            if not z.GetIsRuleArea():
                continue
            polys = poly_from_shapeset(z.Outline())
            for L in self.layers:
                if z.IsOnLayer(L):
                    if z.GetDoNotAllowTracks():
                        self.g.raster_polys(polys, self.keep_trk[self.lidx[L]], True)
                    if z.GetDoNotAllowVias():
                        self.g.raster_polys(polys, self.keep_via, True)
        # via geometry
        drill = max(R["via_min_drill"], 0.3)
        vd = max(R["via_min_d"], drill + 2 * max(R["via_ann"], 0.1))
        self.via_drill, self.via_d = drill, vd
        log("via", vd, drill)
        # nets to route
        self.width = {}
        for code, pl in self.pads.items():
            name = self.netname[code]
            self.width[code] = math.ceil(max(self.rules["min_width"], self.rules["netw"].get(name, 0)) * 1000 - 1e-6) / 1000
        self.to_route = [c for c, pl in self.pads.items() if len(pl) >= 2]
        self.routes = {}  # code -> dict(segs=[(li,x1,y1,x2,y2)], vias=[(x,y)])
        try:
            self.via_len = self.board.GetDesignSettings().GetBoardThickness() / MM * 0.9655
        except Exception:
            self.via_len = 1.545
        self.setup_classes()
        log("classes ready", self.radii)

    # -------------------------------------------------------------- masks
    def setup_classes(self):
        NL = len(self.layers); H, W = self.g.H, self.g.W
        M = 0.01
        self.radii = sorted(set([self.width[c] / 2 for c in self.to_route] + [self.via_d / 2]))
        self.cls = {}
        maxerr = pcbnew.FromMM(0.003)
        for rk in self.radii:
            own = np.zeros((NL, H, W), np.int32); multi = np.zeros((NL, H, W), bool)
            self.cls[rk] = dict(pown=own, pmulti=multi, town=np.zeros((NL, H, W), np.int32), tmulti=np.zeros((NL, H, W), bool),
                                edge=dilate(self.edge, (rk + self.rules["edge"]) / self.res + 0.8))
        for (p, code, lays, pclr) in self.padlist:
            for li in lays:
                L = self.layers[li]
                infl = pclr
                try:
                    mk = pcbnew.F_Mask if L == pcbnew.F_Cu else pcbnew.B_Mask
                    if p.IsOnLayer(mk):
                        infl = max(infl, p.GetSolderMaskExpansion(mk) / MM + 0.005)
                except Exception:
                    pass
                for rk in self.radii:
                    sp = pcbnew.SHAPE_POLY_SET()
                    p.TransformShapeToPolygon(sp, L, pcbnew.FromMM(infl + rk + M), maxerr, pcbnew.ERROR_OUTSIDE)
                    m = self.g.raster_polys(poly_from_shapeset(sp), None, 0)
                    if m:
                        self.stamp(self.cls[rk]["pown"][li], self.cls[rk]["pmulti"][li], m[0], m[2], m[4], code if code > 0 else -1)
            if p.HasHole() and (not lays or code <= 0):
                for rk in self.radii:
                    sp = pcbnew.SHAPE_POLY_SET()
                    p.TransformHoleToPolygon(sp, pcbnew.FromMM(max(self.rules["hole_clr"], self.C) + rk + M), maxerr, pcbnew.ERROR_OUTSIDE)
                    m = self.g.raster_polys(poly_from_shapeset(sp), None, 0)
                    if m:
                        for li in range(NL):
                            self.stamp(self.cls[rk]["pown"][li], self.cls[rk]["pmulti"][li], m[0], m[2], m[4], -1)
        for (d, L, code) in self.drawlist:
            for rk in self.radii:
                sp = pcbnew.SHAPE_POLY_SET()
                d.TransformShapeToPolygon(sp, L, pcbnew.FromMM(self.C + rk + M), maxerr, pcbnew.ERROR_OUTSIDE)
                m = self.g.raster_polys(poly_from_shapeset(sp), None, 0)
                if m:
                    li = self.lidx[L]
                    self.stamp(self.cls[rk]["pown"][li], self.cls[rk]["pmulti"][li], m[0], m[2], m[4], code if code > 0 else -1)
        for (d, mk, L) in self.masklist:
            for rk in self.radii:
                sp = pcbnew.SHAPE_POLY_SET()
                d.TransformShapeToPolygon(sp, mk, pcbnew.FromMM(rk + M + 0.005), maxerr, pcbnew.ERROR_OUTSIDE)
                m = self.g.raster_polys(poly_from_shapeset(sp), None, 0)
                if m:
                    li = self.lidx[L]
                    self.stamp(self.cls[rk]["pown"][li], self.cls[rk]["pmulti"][li], m[0], m[2], m[4], -1)
        # via hole-to-hole map from pad holes
        self.hole_base = np.zeros((H, W), bool)
        for (p, code, lays, pclr) in self.padlist:
            if p.HasHole():
                sp = pcbnew.SHAPE_POLY_SET()
                p.TransformHoleToPolygon(sp, pcbnew.FromMM(self.via_drill / 2 + self.rules["h2h"] + M), maxerr, pcbnew.ERROR_OUTSIDE)
                self.g.raster_polys(poly_from_shapeset(sp), self.hole_base, True)
        self.hole_map = self.hole_base.copy()

    @staticmethod
    def stamp(own, multi, r0, c0, m, code):
        sub = own[r0:r0 + m.shape[0], c0:c0 + m.shape[1]]
        msub = multi[r0:r0 + m.shape[0], c0:c0 + m.shape[1]]
        msub |= m & (sub != 0) & (sub != code)
        sub[m & (sub == 0)] = code

    def stamp_route(self, code, segs, vias):
        g = self.g; M = 0.01
        w2 = self.width[code] / 2
        for rk in self.radii:
            cl = self.cls[rk]
            for li, a, b in segs:
                xa, ya = g.xy(*a); xb, yb = g.xy(*b)
                r0, c0, m = g.capsule(xa, ya, xb, yb, w2 + rk + self.C + M)
                self.stamp(cl["town"][li], cl["tmulti"][li], r0, c0, m, code)
            for (r, c) in vias:
                x, y = g.xy(r, c)
                r0, c0, m = g.capsule(x, y, x, y, self.via_d / 2 + rk + self.C + M)
                for li in range(len(self.layers)):
                    self.stamp(cl["town"][li], cl["tmulti"][li], r0, c0, m, code)
        for (r, c) in vias:
            x, y = g.xy(r, c)
            r0, c0, m = g.capsule(x, y, x, y, self.via_drill + self.rules["h2h"] + M)
            self.hole_map[r0:r0 + m.shape[0], c0:c0 + m.shape[1]] |= m

    def rebuild_tracks(self):
        for rk in self.radii:
            self.cls[rk]["town"][:] = 0
            self.cls[rk]["tmulti"][:] = False
        self.hole_map = self.hole_base.copy()
        for code, rt in self.routes.items():
            self.stamp_route(code, rt["segs"], rt["vias"])

    def window(self, code, margin_mm):
        pts = np.array([p["pos"] for p in self.pads[code]])
        return self.g.bbox_cells(pts[:, 0].min() - margin_mm, pts[:, 1].min() - margin_mm,
                                 pts[:, 0].max() + margin_mm, pts[:, 1].max() + margin_mm)

    def masks(self, code, win, soft=False):
        r0, r1, c0, c1 = win
        w2 = self.width[code] / 2
        ct = self.cls[w2]; cv = self.cls[self.via_d / 2]
        def bl(cl, part):
            o = cl[part + "own"][:, r0:r1, c0:c1]
            return ((o != 0) & (o != code)) | cl[part + "multi"][:, r0:r1, c0:c1]
        hard = bl(ct, "p") | ct["edge"][None, r0:r1, c0:c1] | self.keep_trk[:, r0:r1, c0:c1]
        tb = bl(ct, "t")
        vh = bl(cv, "p") | cv["edge"][None, r0:r1, c0:c1]
        vt = bl(cv, "t")
        vbase = self.hole_map[r0:r1, c0:c1] | self.keep_via[r0:r1, c0:c1]
        if soft:
            blk = hard
            soft_m = tb & ~hard
            vblk = vh.any(0) | vbase
        else:
            blk = hard | tb
            soft_m = None
            vblk = (vh | vt).any(0) | vbase
        sblk = hard | tb
        return blk, sblk, vblk, soft_m

    # -------------------------------------------------------------- search
    def astar(self, win, blk, vblk, sources, target_cells, tgt_xy, soft_m=None, soft_cost=0.0, max_exp=3000000):
        r0, r1, c0, c1 = win
        Hh, Ww = r1 - r0, c1 - c0
        NL = blk.shape[0]
        plane = Hh * Ww
        free = bytes((~blk).astype(np.uint8).ravel())
        vfree = bytes((~vblk).astype(np.uint8).ravel())
        softb = bytes(soft_m.astype(np.uint8).ravel()) if soft_m is not None else None
        tset = set()
        for (li, r, c) in target_cells:
            if r0 <= r < r1 and c0 <= c < c1:
                tset.add(li * plane + (r - r0) * Ww + (c - c0))
        if not tset:
            return None
        ty, tx = (tgt_xy[1] - self.g.y0) / self.res - r0, (tgt_xy[0] - self.g.x0) / self.res - c0
        viac = max(8.0, 1.2 / self.res)
        SQ2 = 1.41421356
        g = {}
        parent = {}
        heap = []
        HW = 1.15
        for (li, r, c) in sources:
            if not (r0 <= r < r1 and c0 <= c < c1):
                continue
            i = li * plane + (r - r0) * Ww + (c - c0)
            if i in g:
                continue
            g[i] = 0.0
            parent[i] = -1
            y, x = r - r0, c - c0
            dx, dy = abs(x - tx), abs(y - ty)
            h = (max(dx, dy) + (SQ2 - 1) * min(dx, dy)) * HW
            heapq.heappush(heap, (h, 0.0, i))
        nbr = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0), (-1, -1, SQ2), (-1, 1, SQ2), (1, -1, SQ2), (1, 1, SQ2)]
        nexp = 0
        found = -1
        while heap:
            f, gc, i = heapq.heappop(heap)
            if gc > g.get(i, 1e18):
                continue
            if i in tset:
                found = i
                break
            nexp += 1
            if nexp > max_exp:
                break
            if not (nexp & 16383) and elapsed() > HARD:
                break
            li, rem = divmod(i, plane)
            y, x = divmod(rem, Ww)
            for dy, dx, cst in nbr:
                ny, nx = y + dy, x + dx
                if ny < 0 or nx < 0 or ny >= Hh or nx >= Ww:
                    continue
                j = i + dy * Ww + dx
                if not free[j]:
                    continue
                if dy and dx:
                    # avoid cutting corners between two blocked cells
                    if not free[i + dy * Ww] and not free[i + dx]:
                        continue
                ng = gc + cst
                if softb is not None and softb[j]:
                    ng += soft_cost
                if ng < g.get(j, 1e18):
                    g[j] = ng
                    parent[j] = i
                    ddx, ddy = abs(nx - tx), abs(ny - ty)
                    heapq.heappush(heap, (ng + (max(ddx, ddy) + (SQ2 - 1) * min(ddx, ddy)) * HW, ng, j))
            if vfree[rem]:
                for nl in range(NL):
                    if nl == li:
                        continue
                    j = nl * plane + rem
                    if not free[j]:
                        continue
                    ng = gc + viac
                    if ng < g.get(j, 1e18):
                        g[j] = ng
                        parent[j] = i
                        heapq.heappush(heap, (ng + (max(abs(x - tx), abs(y - ty)) + (SQ2 - 1) * min(abs(x - tx), abs(y - ty))) * HW, ng, j))
        if found < 0:
            return None
        path = []
        i = found
        while i != -1:
            li, rem = divmod(i, plane)
            y, x = divmod(rem, Ww)
            path.append((li, y + r0, x + c0))
            i = parent[i]
        path.reverse()
        return path

    # -------------------------------------------------------------- geometry from path
    def seg_free(self, sblk_l, win, a, b):
        r0, r1, c0, c1 = win
        (ya, xa), (yb, xb) = a, b
        n = int(max(abs(ya - yb), abs(xa - xb)) * 3) + 1
        fy = np.linspace(ya, yb, n + 1) - r0
        fx = np.linspace(xa, xb, n + 1) - c0
        y0 = np.floor(fy).astype(int); x0 = np.floor(fx).astype(int)
        y1 = np.ceil(fy).astype(int); x1 = np.ceil(fx).astype(int)
        if y0.min() < 0 or x0.min() < 0 or y1.max() >= r1 - r0 or x1.max() >= c1 - c0:
            return False
        return not (sblk_l[y0, x0].any() or sblk_l[y0, x1].any() or sblk_l[y1, x0].any() or sblk_l[y1, x1].any())

    def path_to_geom(self, path, win, sblk):
        """split into layer runs; simplify each run; return segs(li,(r,c),(r,c)) and vias (r,c)"""
        runs = []
        cur = [path[0]]
        vias = []
        for p in path[1:]:
            if p[0] != cur[-1][0]:
                vias.append((p[1], p[2]))
                runs.append(cur)
                cur = [p]
            else:
                cur.append(p)
        runs.append(cur)
        segs = []
        for run in runs:
            li = run[0][0]
            pts = [(p[1], p[2]) for p in run]
            if len(pts) == 1:
                continue
            # collapse collinear first
            out = [pts[0]]
            i = 0
            n = len(pts)
            while i < n - 1:
                # farthest j visible
                j = n - 1
                best = i + 1
                # binary-ish: step back from far end with exponential search
                lo = i + 1
                step = 1
                cand = i + 1
                while cand < n and self.seg_free(sblk[li], win, pts[i], pts[cand]):
                    best = cand
                    step *= 2
                    cand = i + step
                # refine between best and min(cand, n-1)
                hi = min(cand, n - 1)
                while hi > best:
                    if self.seg_free(sblk[li], win, pts[i], pts[hi]):
                        best = hi
                        break
                    hi -= max(1, (hi - best) // 2) if hi - best > 4 else 1
                if best == i + 1:
                    # extend along straight grid direction
                    d = (pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                    k = i + 1
                    while k + 1 < n and (pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1]) == d:
                        k += 1
                    best = k
                out.append(pts[best])
                i = best
            for a, b in zip(out[:-1], out[1:]):
                segs.append((li, a, b))
        return segs, vias

    def commit(self, code, segs, vias):
        rt = self.routes.setdefault(code, dict(segs=[], vias=[]))
        rt["segs"] += segs
        rt["vias"] += vias
        self.stamp_route(code, segs, vias)

    def tree_cells(self, code, segs, vias):
        g = self.g
        w2 = self.width[code] / 2
        cells = []
        rr = max(w2 * 0.9, 1e-3)
        for li, a, b in segs:
            xa, ya = g.xy(*a); xb, yb = g.xy(*b)
            r0, c0, m = g.capsule(xa, ya, xb, yb, rr)
            ys, xs = np.nonzero(m)
            cells += [(li, int(y + r0), int(x + c0)) for y, x in zip(ys, xs)]
            cells += [(li, a[0], a[1]), (li, b[0], b[1])]
        for (r, c) in vias:
            for li in range(len(self.layers)):
                cells.append((li, r, c))
        return cells

    def rip(self, code):
        self.routes.pop(code, None)

    # -------------------------------------------------------------- route a net
    def route_net(self, code, soft=False, margin=4.0):
        pads = self.pads[code]
        win = self.window(code, margin)
        blk, sblk, vblk, soft_m = self.masks(code, win, soft)
        r0, r1, c0, c1 = win
        def okcells(p):
            out = []
            for li, cells in p["cells"].items():
                for (r, c) in cells:
                    if r0 <= r < r1 and c0 <= c < c1 and not blk[li, r - r0, c - c0]:
                        out.append((li, r, c))
            if not out:
                # fall back to centre cells even if "blocked"; unblock them
                for li, cells in p["cells"].items():
                    r, c = self.g.cell(*p["pos"])
                    if r0 <= r < r1 and c0 <= c < c1:
                        out.append((li, r, c))
                        blk[li, r - r0, c - c0] = False
            return out
        pcells = [okcells(p) for p in pads]
        connected = [0]
        tree = list(pcells[0])
        remaining = list(range(1, len(pads)))
        all_segs, all_vias = [], []
        hits = set()
        while remaining:
            # nearest remaining pad to tree pads
            cx = np.array([pads[k]["pos"] for k in connected])
            best, bd = None, 1e18
            for k in remaining:
                d = np.min(np.hypot(cx[:, 0] - pads[k]["pos"][0], cx[:, 1] - pads[k]["pos"][1]))
                if d < bd:
                    bd, best = d, k
            # if target pad touches tree already (same cell) skip
            tset = set(pcells[best])
            if tset & set(tree):
                connected.append(best); remaining.remove(best); tree += pcells[best]
                continue
            path = self.astar(win, blk, vblk, tree, pcells[best], pads[best]["pos"], soft_m,
                              soft_cost=(4.0 if soft else 0))
            if path is None:
                return None, hits
            segs, vias = self.path_to_geom(path, win, sblk)
            if soft:
                for (li, r, c) in path:
                    if soft_m[li, r - r0, c - c0]:
                        hits.add((li, r, c))
            all_segs += segs; all_vias += vias
            tree += self.tree_cells(code, segs, vias) + pcells[best]
            # vias also block own-net? own copper is fine
            connected.append(best); remaining.remove(best)
        return (all_segs, all_vias), hits

    def nets_hit(self, code, hits):
        """which other nets' tracks are near the hit cells"""
        res = set()
        w2 = self.width[code] / 2
        own = self.cls[w2]["town"]
        g = self.g
        for (li, r, c) in hits:
            x, y = g.xy(r, c)
            for oc, rt in self.routes.items():
                if oc in res:
                    continue
                for (sl, a, b) in rt["segs"]:
                    if sl != li:
                        continue
                    xa, ya = g.xy(*a); xb, yb = g.xy(*b)
                    dx, dy = xb - xa, yb - ya
                    L2 = dx * dx + dy * dy
                    t = 0 if L2 == 0 else min(1, max(0, ((x - xa) * dx + (y - ya) * dy) / L2))
                    d = math.hypot(x - xa - t * dx, y - ya - t * dy)
                    if d < w2 + self.width[oc] / 2 + self.C + 0.02:
                        res.add(oc); break
                else:
                    for (vr, vc) in rt["vias"]:
                        vx, vy = g.xy(vr, vc)
                        if math.hypot(x - vx, y - vy) < w2 + self.via_d / 2 + self.C + 0.02:
                            res.add(oc); break
        res.discard(code)
        return res

    def run(self, out_path):
        order = sorted(self.to_route, key=lambda c: self.net_size(c))
        failed = []
        for code in order:
            if elapsed() > DEADLINE:
                failed.append(code); continue
            r, _ = self.route_net(code)
            if r is None and elapsed() < DEADLINE:
                r, _ = self.route_net(code, margin=1e4)
            if r is None:
                failed.append(code)
            else:
                self.commit(code, *r)
        log("first pass done; failed", len(failed), "of", len(order))
        self.save(out_path)
        best = (len(self.routes), {k: dict(segs=list(v["segs"]), vias=list(v["vias"])) for k, v in self.routes.items()})
        # rip-up and reroute
        queue = list(failed)
        ripcount = {}
        it = 0
        while queue and elapsed() < DEADLINE:
            it += 1
            code = queue.pop(0)
            if code in self.routes:
                continue
            r, _ = self.route_net(code, margin=1e4)
            if r is not None:
                self.commit(code, *r)
                continue
            r, hits = self.route_net(code, soft=True, margin=1e4)
            if r is None:
                log("net unroutable even soft", self.netname[code])
                continue
            victims = self.nets_hit(code, hits)
            ripcount[code] = ripcount.get(code, 0) + 1
            if ripcount[code] > 6:
                continue
            for v in victims:
                self.rip(v)
            self.rebuild_tracks()
            r, _ = self.route_net(code, margin=1e4)
            if r is None:
                log("still failed after rip", self.netname[code], len(victims))
                queue.append(code)
            else:
                self.commit(code, *r)
            for v in victims:
                if elapsed() > DEADLINE:
                    queue.append(v); continue
                r, _ = self.route_net(v)
                if r is None:
                    r, _ = self.route_net(v, margin=1e4)
                if r is None:
                    queue.append(v)
                else:
                    self.commit(v, *r)
            if len(self.routes) > best[0]:
                best = (len(self.routes), {k: dict(segs=list(v["segs"]), vias=list(v["vias"])) for k, v in self.routes.items()})
                self.save(out_path)
            log("iter", it, "routed", len(self.routes), "/", len(order), "queue", len(queue))
        if len(self.routes) >= best[0]:
            best = (len(self.routes), self.routes)
        self.routes = best[1]
        self.rebuild_tracks()
        self.fix_skew()
        log("final routed", len(self.routes), "/", len(order))
        self.save(out_path)

    def net_len(self, code):
        g = self.g
        L = 0.0
        for li, a, b in self.routes.get(code, {}).get("segs", []):
            xa, ya = g.xy(*a); xb, yb = g.xy(*b)
            L += math.hypot(xb - xa, yb - ya)
        L += len(self.routes.get(code, {}).get("vias", [])) * self.via_len
        return L

    def pairs(self):
        out = []
        for base, mx in self.rules["skew"].items():
            nets = []
            for suf in ("+", "-", "P", "N", "_P", "_N"):
                if base + suf in self.netcode:
                    nets.append(self.netcode[base + suf])
            if len(nets) == 2:
                out.append((nets[0], nets[1], mx))
        return out

    def fix_skew(self):
        for a, b, mx in self.pairs():
            if a not in self.routes or b not in self.routes:
                continue
            for attempt in range(12):
                la, lb = self.net_len(a), self.net_len(b)
                diff = abs(la - lb)
                log("pair", self.netname[a], round(la, 3), self.netname[b], round(lb, 3), "max", mx)
                if diff <= mx * 0.5:
                    break
                short = a if la < lb else b
                if not self.add_bump(short, diff) or elapsed() > DEADLINE + 40:
                    break

    def add_bump(self, code, need):
        g = self.g
        res = self.res
        rt = self.routes[code]
        win = (0, g.H, 0, g.W)
        blk, sblk, vblk, _ = self.masks(code, win)
        w = self.width[code]
        wb = max(2.5 * w, 0.35) / res
        segs = rt["segs"]
        order = sorted(range(len(segs)), key=lambda k: -math.hypot(segs[k][2][0] - segs[k][1][0], segs[k][2][1] - segs[k][1][1]))
        hmax = min(need / 2, 2.0)
        heights = [hmax] + [h for h in (1.5, 1.0, 0.7, 0.5, 0.35, 0.25) if h < hmax]
        for h_mm in heights:
            h = h_mm / res
            for k in order:
                li, A, B = segs[k]
                dy, dx = B[0] - A[0], B[1] - A[1]
                Ls = math.hypot(dx, dy)
                margin = (w + self.C) / res + 1
                if Ls < wb + 2 * margin:
                    break
                uy, ux = dy / Ls, dx / Ls
                for side in (1, -1):
                    ny, nx = -ux * side, uy * side
                    s = margin
                    while s + wb + margin <= Ls:
                        P1 = (A[0] + uy * s, A[1] + ux * s)
                        P2 = (P1[0] + ny * h, P1[1] + nx * h)
                        P4 = (A[0] + uy * (s + wb), A[1] + ux * (s + wb))
                        P3 = (P4[0] + ny * h, P4[1] + nx * h)
                        if (self.seg_free(sblk[li], win, P1, P2) and self.seg_free(sblk[li], win, P2, P3)
                                and self.seg_free(sblk[li], win, P3, P4)):
                            segs[k:k + 1] = [(li, A, P1), (li, P1, P2), (li, P2, P3), (li, P3, P4), (li, P4, B)]
                            self.rebuild_tracks()
                            return True
                        s += max(1.0, wb / 2)
        return False

    def net_size(self, code):
        pts = np.array([p["pos"] for p in self.pads[code]])
        return (pts[:, 0].max() - pts[:, 0].min()) + (pts[:, 1].max() - pts[:, 1].min())

    def save(self, out_path):
        b = self.board
        for t in list(b.GetTracks()):
            b.Remove(t)
        g = self.g
        for code, rt in self.routes.items():
            net = b.FindNet(code)
            wid = pcbnew.FromMM(self.width[code])
            for li, a, bb in rt["segs"]:
                xa, ya = g.xy(*a); xb, yb = g.xy(*bb)
                t = pcbnew.PCB_TRACK(b)
                t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(xa), pcbnew.FromMM(ya)))
                t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(xb), pcbnew.FromMM(yb)))
                t.SetWidth(wid)
                t.SetLayer(self.layers[li])
                t.SetNet(net)
                b.Add(t)
            for (r, c) in rt["vias"]:
                x, y = g.xy(r, c)
                v = pcbnew.PCB_VIA(b)
                v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
                v.SetViaType(pcbnew.VIATYPE_THROUGH)
                v.SetLayerPair(self.layers[0], self.layers[-1])
                v.SetDrill(pcbnew.FromMM(self.via_drill))
                v.SetWidth(self.layers[0], pcbnew.FromMM(self.via_d))
                v.SetNet(net)
                b.Add(v)
        tmp = out_path + ".tmp.kicad_pcb"
        pcbnew.SaveBoard(tmp, b)
        os.replace(tmp, out_path)
        log("saved", len(self.routes), "nets")


def main():
    src, dst = sys.argv[1], sys.argv[2]
    import traceback, shutil
    r = None
    try:
        r = Router(src)
        log("loaded; nets to route", len(r.to_route))
        r.run(dst)
    except Exception:
        traceback.print_exc()
        try:
            if r is not None and r.routes:
                r.save(dst)
            elif not os.path.exists(dst):
                shutil.copy(src, dst)
        except Exception:
            traceback.print_exc()

if __name__ == "__main__":
    main()
