#!/usr/bin/env python3
"""Grid maze router for 2-layer KiCad boards.

Usage: python3 route.py <board.kicad_pcb> <routed.kicad_pcb>
"""
import os
import sys
import re
import json
import math
import time
import heapq

import numpy as np
import pcbnew

T0 = time.time()
BUDGET = float(os.environ.get("ROUTER_BUDGET", "420"))


def elapsed():
    return time.time() - T0


def log(*a):
    if os.environ.get("ROUTER_VERBOSE"):
        print("[%6.1fs]" % elapsed(), *a, file=sys.stderr, flush=True)


MM = 1000000.0  # nm per mm


# ---------------------------------------------------------------- rules ----

def load_project(path):
    pro = os.path.splitext(path)[0] + ".kicad_pro"
    try:
        with open(pro) as f:
            return json.load(f)
    except Exception:
        return {}


def parse_dru(path):
    """-> (min width per net in nm, max skew per diff-pair prefix in nm)."""
    dru = os.path.splitext(path)[0] + ".kicad_dru"
    widths, skews = {}, {}
    try:
        with open(dru) as f:
            text = f.read()
    except Exception:
        return widths, skews
    # split into rules
    for m in re.finditer(r'\(rule\s', text):
        start = m.start()
        depth, i = 0, start
        while i < len(text):
            if text[i] == '(':
                depth += 1
            elif text[i] == ')':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = text[start:i + 1]
        cond = re.search(r'\(condition\s+"(.*?)"\s*\)', body, re.S)
        if not cond:
            continue
        c = cond.group(1)
        wm = re.search(r'\(constraint\s+track_width\s*\(min\s+([0-9.]+)mm\s*\)', body)
        if wm:
            w = int(round(float(wm.group(1)) * MM))
            for nm in re.findall(r"A\.NetName\s*==\s*'([^']*)'", c):
                widths[nm] = max(widths.get(nm, 0), w)
        sm = re.search(r'\(constraint\s+skew\s*\(max\s+([0-9.]+)mm\s*\)', body)
        if sm:
            s = int(round(float(sm.group(1)) * MM))
            for nm in re.findall(r"A\.inDiffPair\('([^']*)'\)", c):
                skews[nm] = s
    return widths, skews


def netclass_of(name, pro):
    ns = pro.get("net_settings", {}) or {}
    classes = {c.get("name"): c for c in ns.get("classes", []) or []}
    assign = ns.get("netclass_assignments") or {}
    cn = assign.get(name)
    if isinstance(cn, list):
        cn = cn[0] if cn else None
    if cn and cn in classes:
        return classes[cn]
    import fnmatch
    for pat in ns.get("netclass_patterns") or []:
        p = pat.get("pattern", "")
        try:
            if fnmatch.fnmatchcase(name, p) or re.fullmatch(p, name):
                c = classes.get(pat.get("netclass"))
                if c:
                    return c
        except Exception:
            pass
    return classes.get("Default", {})


# --------------------------------------------------------------- raster ----

def dilate_disk(mask, r):
    """Binary dilation by a Euclidean disk of radius r cells."""
    if r <= 0:
        return mask
    h, w = mask.shape
    # horizontal dilations by 0..r
    hd = [mask]
    cur = mask
    for j in range(1, r + 1):
        nxt = cur.copy()
        nxt[:, 1:] |= cur[:, :-1]
        nxt[:, :-1] |= cur[:, 1:]
        hd.append(nxt)
        cur = nxt
    out = np.zeros_like(mask)
    r2 = r * r
    for dy in range(-r, r + 1):
        dx = int(math.isqrt(max(0, r2 - dy * dy)))
        src = hd[dx]
        if dy == 0:
            out |= src
        elif dy > 0:
            out[dy:, :] |= src[:-dy, :]
        else:
            out[:dy, :] |= src[-dy:, :]
    return out


class Grid:
    def __init__(self, x0, y0, w, h, pitch):
        self.x0, self.y0, self.pitch = x0, y0, pitch
        self.W, self.H = w, h

    def to_cell(self, x, y):
        return (int((x - self.x0) // self.pitch), int((y - self.y0) // self.pitch))

    def to_nm(self, ix, iy):
        return (int(self.x0 + (ix + 0.5) * self.pitch), int(self.y0 + (iy + 0.5) * self.pitch))


def poly_mask(grid, pts, pad_cells=0):
    """Rasterize polygon (list of (x,y) nm). Returns (x0,y0,mask) window indices."""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    ix0 = max(0, int((min(xs) - grid.x0) // grid.pitch) - 1 - pad_cells)
    ix1 = min(grid.W - 1, int((max(xs) - grid.x0) // grid.pitch) + 1 + pad_cells)
    iy0 = max(0, int((min(ys) - grid.y0) // grid.pitch) - 1 - pad_cells)
    iy1 = min(grid.H - 1, int((max(ys) - grid.y0) // grid.pitch) + 1 + pad_cells)
    if ix1 < ix0 or iy1 < iy0:
        return None
    X = grid.x0 + (np.arange(ix0, ix1 + 1) + 0.5) * grid.pitch
    Y = grid.y0 + (np.arange(iy0, iy1 + 1) + 0.5) * grid.pitch
    XX = X[None, :]
    YY = Y[:, None]
    inside = np.zeros((len(Y), len(X)), dtype=bool)
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if y1 == y2:
            continue
        cond = ((y1 > YY) != (y2 > YY))
        with np.errstate(invalid='ignore', divide='ignore'):
            xint = (x2 - x1) * (YY - y1) / (y2 - y1) + x1
        inside ^= (cond & (XX < xint))
    return ix0, iy0, inside


def win_bounds(grid, xmin, ymin, xmax, ymax):
    ix0 = max(0, int((xmin - grid.x0) // grid.pitch) - 1)
    ix1 = min(grid.W - 1, int((xmax - grid.x0) // grid.pitch) + 1)
    iy0 = max(0, int((ymin - grid.y0) // grid.pitch) - 1)
    iy1 = min(grid.H - 1, int((ymax - grid.y0) // grid.pitch) + 1)
    if ix1 < ix0 or iy1 < iy0:
        return None
    return ix0, iy0, ix1, iy1


def poly_dist_mask(grid, pts, R):
    """Cells whose centre is within R of the polygon (inside counts as distance 0)."""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    wb = win_bounds(grid, min(xs) - R, min(ys) - R, max(xs) + R, max(ys) + R)
    if wb is None:
        return None
    ix0, iy0, ix1, iy1 = wb
    X = grid.x0 + (np.arange(ix0, ix1 + 1) + 0.5) * grid.pitch
    Y = grid.y0 + (np.arange(iy0, iy1 + 1) + 0.5) * grid.pitch
    XX = X[None, :]
    YY = Y[:, None]
    inside = np.zeros((len(Y), len(X)), dtype=bool)
    best = np.full((len(Y), len(X)), 1e18)
    n = len(pts)
    R2 = float(R) ** 2
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if y1 != y2:
            cond = ((y1 > YY) != (y2 > YY))
            with np.errstate(invalid='ignore', divide='ignore'):
                xint = (x2 - x1) * (YY - y1) / (y2 - y1) + x1
            inside ^= (cond & (XX < xint))
        px = XX - x1
        py = YY - y1
        vx, vy = float(x2 - x1), float(y2 - y1)
        L2 = vx * vx + vy * vy
        if L2 <= 0:
            d2 = px ** 2 + py ** 2
        else:
            t = np.clip((px * vx + py * vy) / L2, 0.0, 1.0)
            d2 = (px - t * vx) ** 2 + (py - t * vy) ** 2
        np.minimum(best, d2, out=best)
    return ix0, iy0, (inside | (best <= R2)), inside, best


def disk_mask(grid, cx, cy, r, pad_cells=0):
    ix0 = max(0, int((cx - r - grid.x0) // grid.pitch) - 1 - pad_cells)
    ix1 = min(grid.W - 1, int((cx + r - grid.x0) // grid.pitch) + 1 + pad_cells)
    iy0 = max(0, int((cy - r - grid.y0) // grid.pitch) - 1 - pad_cells)
    iy1 = min(grid.H - 1, int((cy + r - grid.y0) // grid.pitch) + 1 + pad_cells)
    if ix1 < ix0 or iy1 < iy0:
        return None
    X = grid.x0 + (np.arange(ix0, ix1 + 1) + 0.5) * grid.pitch - cx
    Y = grid.y0 + (np.arange(iy0, iy1 + 1) + 0.5) * grid.pitch - cy
    m = (X[None, :] ** 2 + Y[:, None] ** 2) <= float(r) ** 2
    return ix0, iy0, m


def seg_mask(grid, p, q, r, pad_cells=0):
    (x1, y1), (x2, y2) = p, q
    ix0 = max(0, int((min(x1, x2) - r - grid.x0) // grid.pitch) - 1 - pad_cells)
    ix1 = min(grid.W - 1, int((max(x1, x2) + r - grid.x0) // grid.pitch) + 1 + pad_cells)
    iy0 = max(0, int((min(y1, y2) - r - grid.y0) // grid.pitch) - 1 - pad_cells)
    iy1 = min(grid.H - 1, int((max(y1, y2) + r - grid.y0) // grid.pitch) + 1 + pad_cells)
    if ix1 < ix0 or iy1 < iy0:
        return None
    X = grid.x0 + (np.arange(ix0, ix1 + 1) + 0.5) * grid.pitch
    Y = grid.y0 + (np.arange(iy0, iy1 + 1) + 0.5) * grid.pitch
    px = X[None, :] - x1
    py = Y[:, None] - y1
    vx, vy = float(x2 - x1), float(y2 - y1)
    L2 = vx * vx + vy * vy
    if L2 <= 0:
        d2 = px ** 2 + py ** 2
    else:
        t = np.clip((px * vx + py * vy) / L2, 0.0, 1.0)
        d2 = (px - t * vx) ** 2 + (py - t * vy) ** 2
    return ix0, iy0, d2 <= float(r) ** 2


# --------------------------------------------------------------- router ----

FAKE_NET = 30000


class Router:
    def __init__(self, board, path):
        self.board = board
        self.pro = load_project(path)
        self.dru_w, self.dru_skew = parse_dru(path)
        rules = (self.pro.get("board", {}).get("design_settings", {}) or {}).get("rules", {}) or {}
        self.min_clearance = int(round(rules.get("min_clearance", 0.0) * MM))
        self.min_track = int(round(rules.get("min_track_width", 0.2) * MM))
        self.min_via_dia = int(round(rules.get("min_via_diameter", 0.4) * MM))
        self.min_via_ann = int(round(rules.get("min_via_annular_width", 0.05) * MM))
        self.min_drill = int(round(rules.get("min_through_hole_diameter", 0.3) * MM))
        self.edge_cl = int(round(rules.get("min_copper_edge_clearance", 0.0) * MM))
        self.hole_cl = int(round(rules.get("min_hole_clearance", 0.0) * MM))
        self.h2h = int(round(rules.get("min_hole_to_hole", 0.25) * MM))
        ns = self.pro.get("net_settings", {}) or {}
        cls = ns.get("classes", []) or [{}]
        self.clearance = max([int(round((c.get("clearance", 0.2) or 0.2) * MM)) for c in cls] + [self.min_clearance])
        self.layers = list(board.GetEnabledLayers().CuStack())
        if len(self.layers) > 2:
            self.layers = [pcbnew.F_Cu, pcbnew.B_Cu]
        self.nlay = len(self.layers)
        self.lay_index = {l: i for i, l in enumerate(self.layers)}

        # via geometry: smallest legal
        drill = max(self.min_drill, int(0.3 * MM))
        dia = max(self.min_via_dia, drill + 2 * self.min_via_ann + 20000)
        self.via_drill = drill + 2000
        self.via_dia = dia + 2000
        self.safety = 5000

    # ---- net info
    def setup_nets(self):
        b = self.board
        self.nets = {}        # name -> id
        self.net_names = {}   # id -> name
        self.net_width = {}
        nid = 0
        pads_by_net = {}
        for fp in b.GetFootprints():
            for pad in fp.Pads():
                name = pad.GetNetname()
                if not name:
                    continue
                pads_by_net.setdefault(name, []).append(pad)
        for name in sorted(pads_by_net):
            if len(pads_by_net[name]) < 1:
                continue
            nid += 1
            self.nets[name] = nid
            self.net_names[nid] = name
            w = max(self.min_track, self.dru_w.get(name, 0))
            self.net_width[nid] = w + 1000
        self.pads_by_net = pads_by_net

    def setup_grid(self):
        b = self.board
        poly = pcbnew.SHAPE_POLY_SET()
        try:
            ok = b.GetBoardPolygonOutlines(poly)
        except Exception:
            ok = False
        bb = b.GetBoardEdgesBoundingBox()
        x0, y0 = bb.GetX(), bb.GetY()
        wnm, hnm = bb.GetWidth(), bb.GetHeight()
        if wnm <= 0 or hnm <= 0:
            bb = b.GetBoundingBox()
            x0, y0, wnm, hnm = bb.GetX(), bb.GetY(), bb.GetWidth(), bb.GetHeight()
        margin = int(0.6 * MM)
        x0 -= margin
        y0 -= margin
        wnm += 2 * margin
        hnm += 2 * margin
        area_mm2 = (wnm / MM) * (hnm / MM)
        pitch = max(0.05, math.sqrt(area_mm2 / 1.7e6))
        pitch = math.ceil(pitch * 200) / 200.0  # round to 0.005mm
        self.pitch_nm = pitch * MM
        W = int(wnm / self.pitch_nm) + 1
        H = int(hnm / self.pitch_nm) + 1
        self.grid = Grid(x0, y0, W, H, self.pitch_nm)
        self.W, self.H = W, H
        self.HW = W * H
        log("grid %dx%d pitch %.3fmm cells %d" % (W, H, pitch, W * H))
        # inside-outline mask
        inside = np.zeros((H, W), dtype=bool)
        if ok and poly.OutlineCount() > 0:
            for oi in range(poly.OutlineCount()):
                o = poly.Outline(oi)
                pts = [(o.CPoint(i).x, o.CPoint(i).y) for i in range(o.PointCount())]
                res = poly_mask(self.grid, pts)
                if res:
                    ax, ay, m = res
                    inside[ay:ay + m.shape[0], ax:ax + m.shape[1]] |= m
            for oi in range(poly.OutlineCount()):
                o = poly.Outline(oi)
                for hi in range(poly.HoleCount(oi)):
                    hpoly = poly.Hole(oi, hi)
                    pts = [(hpoly.CPoint(i).x, hpoly.CPoint(i).y) for i in range(hpoly.PointCount())]
                    res = poly_mask(self.grid, pts)
                    if res:
                        ax, ay, m = res
                        inside[ay:ay + m.shape[0], ax:ax + m.shape[1]] &= ~m
        if inside.sum() < 0.05 * H * W:
            inside[:, :] = True
            inside[0, :] = False
            inside[-1, :] = False
            inside[:, 0] = False
            inside[:, -1] = False
        self.inside = inside
        self.safety = int(0.15 * self.pitch_nm) + 3000
        self.mask_extra = int(float(os.environ.get("ROUTER_MASK_EXTRA", "0")) * MM)
        self.holeblock = np.zeros((H, W), dtype=bool)

    # ---- obstacle buckets
    def bucket_for(self, halfw, is_via=False):
        key = (int(halfw), bool(is_via))
        if key not in self.buckets:
            n1 = [np.zeros((self.H, self.W), dtype=np.int16) for _ in range(self.nlay)]
            n2 = [np.zeros((self.H, self.W), dtype=np.int16) for _ in range(self.nlay)]
            ov = [np.zeros((self.H, self.W), dtype=bool) for _ in range(self.nlay)]
            re_ = int(math.ceil((self.edge_cl + halfw) / self.pitch_nm + 0.75))
            outside = dilate_disk(~self.inside, re_)
            if is_via:
                hextra = max(0, self.h2h + self.via_drill // 2 - halfw - self.clearance)
            else:
                hextra = max(0, self.hole_cl - self.clearance)
            self.buckets[key] = dict(keep=self.clearance + halfw + self.safety, hextra=hextra,
                                     n1=n1, n2=n2, ov=ov, outside=outside)
            self.bucket_order.append(key)
        return self.buckets[key]

    def add_obstacle(self, layer_ids, shape, netid):
        """shape: ('poly', pts) | ('disk', cx, cy, rad) | ('hole', cx, cy, rad) |
                  ('seg', p, q, rad)"""
        for key in self.bucket_order:
            bk = self.buckets[key]
            R = bk['keep']
            if shape[0] == 'hole':
                win = disk_mask(self.grid, shape[1], shape[2], R + bk['hextra'] + shape[3])
            elif shape[0] == 'disk':
                win = disk_mask(self.grid, shape[1], shape[2], R + shape[3])
            elif shape[0] == 'hseg':
                win = seg_mask(self.grid, shape[1], shape[2], R + bk['hextra'] + shape[3])
            elif shape[0] == 'seg':
                win = seg_mask(self.grid, shape[1], shape[2], R + shape[3])
            else:
                if shape[0] == 'padpoly':
                    R += self.mask_extra
                res = poly_dist_mask(self.grid, shape[1], R)
                win = None if res is None else (res[0], res[1], res[2])
            if win is None:
                continue
            ax, ay, d = win
            if not d.any():
                continue
            h, w = d.shape
            for li in layer_ids:
                s1 = bk['n1'][li][ay:ay + h, ax:ax + w]
                s2 = bk['n2'][li][ay:ay + h, ax:ax + w]
                so = bk['ov'][li][ay:ay + h, ax:ax + w]
                a = d & (s1 == 0)
                s1[a] = netid
                nd = d & (s1 != netid)
                a2 = nd & (s2 == 0)
                s2[a2] = netid
                so |= nd & (s2 != netid)

    def hole_shape(self, pad):
        dx = pad.GetDrillSizeX()
        dy = pad.GetDrillSizeY()
        pp = pad.GetPosition()
        r = min(dx, dy) / 2.0
        L = abs(dx - dy)
        if L <= self.pitch_nm * 0.2 or r <= 0:
            return ('hole', pp.x, pp.y, max(dx, dy) / 2.0)
        try:
            ang = pad.GetOrientation().AsRadians()
        except Exception:
            ang = math.radians(pad.GetOrientationDegrees())
        if dx > dy:
            ux, uy = math.cos(ang), -math.sin(ang)
        else:
            ux, uy = math.sin(ang), math.cos(ang)
        p1 = (pp.x - ux * L / 2.0, pp.y - uy * L / 2.0)
        p2 = (pp.x + ux * L / 2.0, pp.y + uy * L / 2.0)
        return ('hseg', p1, p2, r)

    def block_hole(self, shape, extra=0.0):
        """Net-agnostic keepout for our via drills (hole-to-hole)."""
        R = self.h2h + self.via_drill / 2.0 + self.safety + extra
        if shape[0] == 'hole':
            win = disk_mask(self.grid, shape[1], shape[2], R + shape[3])
        else:
            win = seg_mask(self.grid, shape[1], shape[2], R + shape[3])
        if win:
            ax, ay, m = win
            self.holeblock[ay:ay + m.shape[0], ax:ax + m.shape[1]] |= m

    def rasterize_zones(self):
        for z in self.board.Zones():
            try:
                is_rule = z.GetIsRuleArea()
            except Exception:
                is_rule = False
            name = z.GetNetname()
            if name and not is_rule:
                continue
            if is_rule:
                try:
                    if not (z.GetDoNotAllowTracks() or z.GetDoNotAllowVias() or z.GetDoNotAllowCopperPour()):
                        continue
                except Exception:
                    pass
            lset = set(z.GetLayerSet().CuStack())
            lids = [self.lay_index[l] for l in self.layers if l in lset]
            if not lids:
                continue
            try:
                poly = z.Outline()
            except Exception:
                continue
            for oi in range(poly.OutlineCount()):
                o = poly.Outline(oi)
                pts = [(o.CPoint(i).x, o.CPoint(i).y) for i in range(o.PointCount())]
                if len(pts) < 3:
                    continue
                res = poly_dist_mask(self.grid, pts, 0.0)
                if res is not None:
                    ax, ay, m, inside, best = res
                    for li in lids:
                        sub = self.occ[li][ay:ay + m.shape[0], ax:ax + m.shape[1]]
                        sub[inside & (sub == 0)] = FAKE_NET
                self.add_obstacle(lids, ('poly', pts), FAKE_NET)

    def rasterize_pads(self):
        b = self.board
        self.occ = [np.zeros((self.H, self.W), dtype=np.int16) for _ in range(self.nlay)]
        self.pad_cells = {}
        items = []
        for fp in b.GetFootprints():
            for pad in fp.Pads():
                name = pad.GetNetname()
                nid = self.nets.get(name, FAKE_NET)
                lset = set(pad.GetLayerSet().CuStack())
                lids = [self.lay_index[l] for l in self.layers if l in lset]
                drill = max(pad.GetDrillSizeX(), pad.GetDrillSizeY())
                if drill > 0:
                    lids = list(range(self.nlay))
                if not lids:
                    continue
                items.append((pad, nid, lids, drill))
        for pad, nid, lids, drill in items:
            polys = []
            try:
                lay = self.layers[lids[0]]
                ep = pad.GetEffectivePolygon(lay)
                for oi in range(ep.OutlineCount()):
                    o = ep.Outline(oi)
                    pts = [(o.CPoint(i).x, o.CPoint(i).y) for i in range(o.PointCount())]
                    if len(pts) >= 3:
                        polys.append(pts)
            except Exception:
                polys = []
            if not polys:
                bb = pad.GetBoundingBox()
                polys.append([(bb.GetX(), bb.GetY()), (bb.GetRight(), bb.GetY()),
                              (bb.GetRight(), bb.GetBottom()), (bb.GetX(), bb.GetBottom())])
            shapes = [('padpoly', pts) for pts in polys]
            hshape = None
            if drill > 0:
                hshape = self.hole_shape(pad)
                shapes.append(hshape)
                self.block_hole(hshape)
            # occupancy (actual copper) + keepouts
            for pts in polys:
                res = poly_dist_mask(self.grid, pts, 0.0)
                if res is None:
                    continue
                ax, ay, m, inside, best = res
                for li in lids:
                    sub = self.occ[li][ay:ay + m.shape[0], ax:ax + m.shape[1]]
                    sub[inside & (sub == 0)] = nid
            if hshape is not None:
                if hshape[0] == 'hole':
                    win = disk_mask(self.grid, hshape[1], hshape[2], hshape[3])
                else:
                    win = seg_mask(self.grid, hshape[1], hshape[2], hshape[3])
                if win:
                    ax, ay, m = win
                    for li in lids:
                        sub = self.occ[li][ay:ay + m.shape[0], ax:ax + m.shape[1]]
                        sub[m & (sub == 0)] = nid
            for sh in shapes:
                self.add_obstacle(lids, sh, nid)
            if nid != FAKE_NET:
                halfw = self.net_width[nid] / 2.0
                cells = set()
                res = poly_dist_mask(self.grid, polys[0], 0.0)
                if res is not None:
                    ax, ay, m, inside, best = res
                    core = inside & (best >= halfw * halfw)
                    if not core.any():
                        core = inside
                    ys, xs = np.nonzero(core)
                    for yy, xx in zip(ys, xs):
                        cells.add((int(ay + yy), int(ax + xx)))
                pp = pad.GetPosition()
                cx, cy = self.grid.to_cell(pp.x, pp.y)
                cx = min(max(cx, 1), self.W - 2)
                cy = min(max(cy, 1), self.H - 2)
                cells.add((cy, cx))
                self.pad_cells.setdefault(nid, []).append((cells, lids, pad))

    # ---- passability
    def passable_for(self, nid, halfw, is_via=False):
        bk = self.bucket_for(halfw, is_via)
        out = []
        for li in range(self.nlay):
            blocked = bk['ov'][li] | bk['outside']
            n1 = bk['n1'][li]
            n2 = bk['n2'][li]
            blocked |= (n1 != 0) & (n1 != nid)
            blocked |= (n2 != 0) & (n2 != nid)
            out.append(~blocked)
        return out

    # ---- A*
    def astar(self, passable, via_ok, srcs, tgts, tcell, max_pop=400000, via_cost=160):
        W, HW, nlay = self.W, self.HW, self.nlay
        tx, ty = tcell
        wgt = 13
        push = heapq.heappush
        pop = heapq.heappop
        closed = bytearray(nlay * HW)
        g = {}
        par = {}
        heap = []
        for s in srcs:
            if s in g:
                continue
            g[s] = 0
            li = s // HW
            rem = s - li * HW
            y, x = divmod(rem, W)
            dx = abs(x - tx)
            dy = abs(y - ty)
            h = (10 * (dx + dy) - 6 * min(dx, dy)) * wgt // 10
            push(heap, (h, 0, s))
        dirs = ((1, 10), (-1, 10), (W, 10), (-W, 10), (W + 1, 14), (W - 1, 14), (-W + 1, 14), (-W - 1, 14))
        pops = 0
        found = None
        while heap:
            f, gc, u = pop(heap)
            if closed[u]:
                continue
            closed[u] = 1
            pops += 1
            if u in tgts:
                found = u
                break
            if pops > max_pop:
                break
            li = u // HW
            base = li * HW
            rem = u - base
            y, x = divmod(rem, W)
            for d, c in dirs:
                v = u + d
                if closed[v] or not passable[v]:
                    continue
                ng = gc + c
                old = g.get(v)
                if old is not None and old <= ng:
                    continue
                g[v] = ng
                par[v] = u
                vy, vx = divmod(v - base, W)
                dx = abs(vx - tx)
                dy = abs(vy - ty)
                h = (10 * (dx + dy) - 6 * (dx if dx < dy else dy)) * wgt // 10
                push(heap, (ng + h, ng, v))
            if nlay > 1 and via_ok[rem]:
                v = rem if li else rem + HW
                if not closed[v] and passable[v]:
                    ng = gc + via_cost
                    old = g.get(v)
                    if old is None or old > ng:
                        g[v] = ng
                        par[v] = u
                        dx = abs(x - tx)
                        dy = abs(y - ty)
                        h = (10 * (dx + dy) - 6 * (dx if dx < dy else dy)) * wgt // 10
                        push(heap, (ng + h, ng, v))
        if found is None:
            return None
        path = [found]
        while found in par:
            found = par[found]
            path.append(found)
        path.reverse()
        return path

    # ---- main routing
    def route(self):
        self.buckets = {}
        self.bucket_order = []
        self.setup_nets()
        self.setup_grid()
        # create buckets for all widths up front (so incremental updates cover them)
        halfws = set()
        for nid, w in self.net_width.items():
            halfws.add(w // 2)
        for hw in sorted(halfws):
            self.bucket_for(hw)
        self.bucket_for(self.via_dia // 2, True)
        log("buckets", self.bucket_order)
        self.rasterize_pads()
        self.rasterize_zones()
        log("pads rasterized")
        self.tracks = []  # (layer_index, (x1,y1), (x2,y2), width, netid)
        self.vias = []    # (x, y, netid)
        self.net_len = {}

        order = self.net_order()
        for nid in order:
            if elapsed() > BUDGET:
                log("out of time, stopping")
                break
            self.route_net(nid)
        return self.tracks, self.vias

    def net_order(self):
        groups = []
        for nid, plist in self.pad_cells.items():
            if len(plist) < 2:
                continue
            pts = [p[2].GetPosition() for p in plist]
            xs = [p.x for p in pts]
            ys = [p.y for p in pts]
            span = (max(xs) - min(xs)) + (max(ys) - min(ys))
            groups.append((len(plist), span, nid))
        groups.sort()
        # diff pairs first
        pair_nets = set()
        for prefix in self.dru_skew:
            for name, nid in self.nets.items():
                if name.startswith(prefix) and len(name) == len(prefix) + 1:
                    pair_nets.add(nid)
        first = [g[2] for g in groups if g[2] in pair_nets]
        rest = [g[2] for g in groups if g[2] not in pair_nets]
        return first + rest

    def route_net(self, nid):
        name = self.net_names[nid]
        w = self.net_width[nid]
        halfw = w // 2
        plist = self.pad_cells[nid]
        if len(plist) < 2:
            return
        t_start = elapsed()
        pas = self.passable_for(nid, halfw)
        pasv = self.passable_for(nid, self.via_dia // 2, True)
        via_ok = pasv[0] & ~self.holeblock
        for li in range(1, self.nlay):
            via_ok = via_ok & pasv[li]
        via_ok_b = via_ok.tobytes()
        # pad interiors of this net are always usable (escape from the pad)
        for cells, lids, pad in plist:
            if not cells:
                continue
            ys = np.fromiter((c[0] for c in cells), dtype=np.int64, count=len(cells))
            xs = np.fromiter((c[1] for c in cells), dtype=np.int64, count=len(cells))
            for li in lids:
                pas[li][ys, xs] = True
        passable_b = bytearray()
        for li in range(self.nlay):
            passable_b += pas[li].tobytes()
        passable_b = bytes(passable_b)

        # components: start from pad 0
        comps = []
        for cells, lids, pad in plist:
            idxs = set()
            for (yy, xx) in cells:
                for li in lids:
                    idxs.add(li * self.HW + yy * self.W + xx)
            p = pad.GetPosition()
            cx, cy = self.grid.to_cell(p.x, p.y)
            for li in lids:
                idxs.add(li * self.HW + cy * self.W + cx)
            comps.append({'idx': idxs, 'pos': (p.x, p.y), 'cell': (cx, cy)})

        connected = comps[0]
        remaining = comps[1:]
        placed_any = False
        while remaining:
            if elapsed() > BUDGET:
                break
            # nearest remaining component to connected set (by pad position)
            bestk, bestd = 0, None
            for k, c in enumerate(remaining):
                d = min((abs(c['pos'][0] - q[0]) + abs(c['pos'][1] - q[1]))
                        for q in [connected['pos']]) if False else None
                d = min(abs(c['cell'][0] - cc) + abs(c['cell'][1] - rr)
                        for (cc, rr) in [(connected['cell'][0], connected['cell'][1])])
                if bestd is None or d < bestd:
                    bestd, bestk = d, k
            tgt = remaining.pop(bestk)
            srcs = [u for u in connected['idx'] if passable_b[u]]
            if not srcs:
                srcs = list(connected['idx'])
            tg = set(u for u in tgt['idx'] if passable_b[u])
            if not tg:
                tg = set(tgt['idx'])
            path = self.astar(passable_b, via_ok_b, srcs, tg, tgt['cell'],
                              max_pop=self.pop_limit(), via_cost=self.via_cost(w))
            if path is None and elapsed() < BUDGET - 20:
                path = self.astar(passable_b, via_ok_b, srcs, tg, tgt['cell'],
                                  max_pop=self.pop_limit() * 3, via_cost=60)
            if path is None:
                log("  fail %s" % name)
                connected['idx'] |= tgt['idx']
                connected['cell'] = tgt['cell']
                continue
            placed_any = True
            nv = len(self.vias)
            new_cells = self.emit_path(path, nid, w)
            if len(self.vias) != nv:
                vo = pasv[0] & ~self.holeblock
                for li in range(1, self.nlay):
                    vo = vo & pasv[li]
                via_ok_b = vo.tobytes()
            connected['idx'] |= tgt['idx']
            connected['idx'] |= new_cells
            connected['cell'] = tgt['cell']
        if placed_any:
            log("  net %s (%d pads) %.1fs" % (name, len(plist), elapsed() - t_start))

    def refresh(self, nid, halfw, pas):
        for li in range(self.nlay):
            pas[li] |= (self.occ[li] == nid)
        b = bytearray()
        for li in range(self.nlay):
            b += pas[li].tobytes()
        return bytes(b)

    def pop_limit(self):
        left = BUDGET - elapsed()
        if left < 30:
            return 20000
        return 300000

    def via_cost(self, w):
        return max(80, int(12 * self.pitch_nm // self.pitch_nm) * 10 + 120)

    def emit_path(self, path, nid, w):
        """Turn a node path into tracks + vias; update occupancy."""
        HW, W = self.HW, self.W
        pts = []
        for u in path:
            li = u // HW
            rem = u - li * HW
            y, x = divmod(rem, W)
            pts.append((li, x, y))
        # compress collinear runs
        segs = []
        vias = []
        i = 0
        while i < len(pts) - 1:
            li, x, y = pts[i]
            lj, x2, y2 = pts[i + 1]
            if lj != li:
                vias.append((x, y))
                i += 1
                continue
            dx = x2 - x
            dy = y2 - y
            j = i + 1
            while j < len(pts) - 1:
                lk, x3, y3 = pts[j + 1]
                if lk != li or (x3 - pts[j][1]) != dx or (y3 - pts[j][2]) != dy:
                    break
                j += 1
            a = pts[i]
            b2 = pts[j]
            segs.append((li, (a[1], a[2]), (b2[1], b2[2])))
            i = j
        new_cells = set()
        halfr = w / 2.0
        for li, (x1, y1), (x2, y2) in segs:
            p = self.grid.to_nm(x1, y1)
            q = self.grid.to_nm(x2, y2)
            self.tracks.append((li, p, q, w, nid))
            self.add_obstacle([li], ('seg', p, q, halfr), nid)
            win = seg_mask(self.grid, p, q, halfr)
            if win:
                ax, ay, m = win
                sub = self.occ[li][ay:ay + m.shape[0], ax:ax + m.shape[1]]
                sub[m & (sub == 0)] = nid
                ys, xs = np.nonzero(m)
                base = li * self.HW
                new_cells.update((base + (ay + int(yy)) * self.W + (ax + int(xx))) for yy, xx in zip(ys, xs))
        for (x, y) in vias:
            p = self.grid.to_nm(x, y)
            self.vias.append((p[0], p[1], nid))
            self.add_obstacle(list(range(self.nlay)), ('disk', p[0], p[1], self.via_dia / 2.0), nid)
            self.add_obstacle(list(range(self.nlay)), ('hole', p[0], p[1], self.via_drill / 2.0), nid)
            self.block_hole(('hole', p[0], p[1], self.via_drill / 2.0))
            win = disk_mask(self.grid, p[0], p[1], self.via_dia / 2.0)
            if win:
                ax, ay, m = win
                for li in range(self.nlay):
                    sub = self.occ[li][ay:ay + m.shape[0], ax:ax + m.shape[1]]
                    sub[m & (sub == 0)] = nid
                ys, xs = np.nonzero(m)
                for li in range(self.nlay):
                    base = li * self.HW
                    new_cells.update((base + (ay + int(yy)) * self.W + (ax + int(xx))) for yy, xx in zip(ys, xs))
        return new_cells

    # ---- output
    def write(self, out_path):
        b = self.board
        for li, p, q, w, nid in self.tracks:
            t = pcbnew.PCB_TRACK(b)
            t.SetStart(pcbnew.VECTOR2I(int(p[0]), int(p[1])))
            t.SetEnd(pcbnew.VECTOR2I(int(q[0]), int(q[1])))
            t.SetWidth(int(w))
            t.SetLayer(self.layers[li])
            net = b.FindNet(self.net_names[nid])
            if net:
                t.SetNet(net)
            b.Add(t)
        for (x, y, nid) in self.vias:
            v = pcbnew.PCB_VIA(b)
            v.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetLayerPair(self.layers[0], self.layers[-1])
            v.SetDrill(int(self.via_drill))
            try:
                v.SetWidth(self.layers[0], int(self.via_dia))
            except TypeError:
                v.SetWidth(int(self.via_dia))
            net = b.FindNet(self.net_names[nid])
            if net:
                v.SetNet(net)
            b.Add(v)
        pcbnew.SaveBoard(out_path, b)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    board = pcbnew.LoadBoard(src)
    # safety: always leave a valid file behind
    try:
        pcbnew.SaveBoard(dst, board)
    except Exception:
        pass
    r = Router(board, src)
    try:
        r.route()
    except Exception as exc:
        log("router error: %r" % (exc,))
        import traceback
        if os.environ.get("ROUTER_VERBOSE"):
            traceback.print_exc()
    r.write(dst)
    log("done: %d tracks %d vias" % (len(r.tracks), len(r.vias)))


if __name__ == "__main__":
    main()
