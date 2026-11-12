#!/usr/bin/env python3
"""Grid maze router for 2-layer KiCad boards.

Usage: python3 route.py <unrouted.kicad_pcb> <routed.kicad_pcb>
"""
import os
import re
import sys
import math
import json
import time
import traceback

import numpy as np
import pcbnew

T0 = time.time()
TIME_LIMIT = float(os.environ.get("ROUTE_TIME_LIMIT", "400"))


def elapsed():
    return time.time() - T0


def left():
    return TIME_LIMIT - elapsed()


def log(*a):
    print("[%6.1fs]" % elapsed(), *a, file=sys.stderr)
    sys.stderr.flush()


MM = 1000000.0  # nm per mm


# --------------------------------------------------------------------------- rules


def parse_dru(path):
    """-> (min track width per net in mm, {pair prefix: max skew mm})"""
    widths, skews = {}, {}
    try:
        text = open(path).read()
    except OSError:
        return widths, skews
    # split into top level (rule ...) blocks
    blocks, depth, start = [], 0, None
    for i, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append(text[start:i + 1])
                start = None
    for b in blocks:
        if not b.startswith("(rule"):
            continue
        m = re.search(r"constraint\s+track_width\s*\(\s*min\s+([0-9.]+)mm", b)
        if m:
            wmm = float(m.group(1))
            for nm in re.findall(r"A\.NetName\s*==\s*'([^']*)'", b):
                widths[nm] = max(widths.get(nm, 0.0), wmm)
        m = re.search(r"constraint\s+skew\s*\(\s*max\s+([0-9.]+)mm", b)
        if m:
            smm = float(m.group(1))
            for nm in re.findall(r"inDiffPair\('([^']*)'\)", b):
                skews[nm] = min(skews.get(nm, 1e9), smm)
    return widths, skews


def parse_pro(path):
    out = {"clearance": 0.2, "min_track": 0.15, "via_dia": 0.6, "via_drill": 0.3,
           "min_via": 0.4, "min_hole": 0.25, "annular": 0.05, "edge": 0.0, "h2h": 0.25}
    try:
        d = json.load(open(path))
    except Exception:
        return out
    rules = d.get("board", {}).get("design_settings", {}).get("rules", {})
    classes = d.get("net_settings", {}).get("classes", [])
    clr = [c.get("clearance", 0.2) for c in classes] or [0.2]
    out["clearance"] = max(max(clr), rules.get("min_clearance", 0.0) or 0.0)
    out["min_track"] = rules.get("min_track_width", 0.15) or 0.15
    out["min_via"] = rules.get("min_via_diameter", 0.4) or 0.4
    out["min_hole"] = rules.get("min_through_hole_diameter", 0.25) or 0.25
    out["annular"] = rules.get("min_via_annular_width", 0.05) or 0.05
    out["edge"] = rules.get("min_copper_edge_clearance", 0.0) or 0.0
    out["h2h"] = rules.get("min_hole_to_hole", 0.25) or 0.25
    vd = [c.get("via_diameter", 0.6) for c in classes] or [0.6]
    out["via_dia"] = max(vd)
    out["via_drill"] = max([c.get("via_drill", 0.3) for c in classes] or [0.3])
    return out


# --------------------------------------------------------------------------- raster helpers


def dilate1(mask):
    """8-neighbour dilation by one cell."""
    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    out[1:, 1:] |= mask[:-1, :-1]
    out[:-1, :-1] |= mask[1:, 1:]
    out[1:, :-1] |= mask[:-1, 1:]
    out[:-1, 1:] |= mask[1:, :-1]
    return out


def hdilate(mask, k):
    """Horizontal dilation by k cells each side (via cumulative sums)."""
    if k <= 0:
        return mask
    h, w = mask.shape
    c = np.zeros((h, w + 1), np.int32)
    np.cumsum(mask, axis=1, out=c[:, 1:], dtype=np.int32)
    lo = np.clip(np.arange(w) - k, 0, w)
    hi = np.clip(np.arange(w) + k + 1, 0, w)
    return (c[:, hi] - c[:, lo]) > 0


def dilate_disk(mask, r):
    """Dilation by a disk of radius r cells."""
    if r <= 0:
        return mask
    if r == 1:
        return dilate1(mask)
    h, w = mask.shape
    out = np.zeros_like(mask)
    for dy in range(-r, r + 1):
        dx = int(math.floor(math.sqrt(max(0.0, r * r - dy * dy))))
        band = hdilate(mask, dx)
        if dy < 0:
            out[:h + dy] |= band[-dy:]
        elif dy > 0:
            out[dy:] |= band[:h - dy]
        else:
            out |= band
    return out


class Grid:
    def __init__(self, x0, y0, pitch, nx, ny):
        self.x0, self.y0, self.pitch, self.nx, self.ny = x0, y0, pitch, nx, ny

    def to_cell(self, x, y):
        return (int(round((y - self.y0) / self.pitch)), int(round((x - self.x0) / self.pitch)))

    def to_pt(self, iy, ix):
        return (int(round(self.x0 + ix * self.pitch)), int(round(self.y0 + iy * self.pitch)))


def poly_points(polyset):
    """SHAPE_POLY_SET -> list of (outline pts, [hole pts]) in nm."""
    res = []
    for i in range(polyset.OutlineCount()):
        o = polyset.Outline(i)
        pts = [(o.CPoint(k).x, o.CPoint(k).y) for k in range(o.PointCount())]
        holes = []
        for j in range(polyset.HoleCount(i)):
            hh = polyset.Hole(i, j)
            holes.append([(hh.CPoint(k).x, hh.CPoint(k).y) for k in range(hh.PointCount())])
        res.append((pts, holes))
    return res


def fill_polygon(grid, pts, out, value=True):
    """Scanline fill of a polygon (nm points) into bool/int array `out` (ny,nx)."""
    if len(pts) < 3:
        return
    ys = [p[1] for p in pts]
    xs = [p[0] for p in pts]
    iy0 = max(0, int(math.floor((min(ys) - grid.y0) / grid.pitch)))
    iy1 = min(grid.ny - 1, int(math.ceil((max(ys) - grid.y0) / grid.pitch)))
    if iy1 < iy0:
        return
    n = len(pts)
    px = np.array(xs, dtype=np.float64)
    py = np.array(ys, dtype=np.float64)
    qx = np.roll(px, -1)
    qy = np.roll(py, -1)
    for iy in range(iy0, iy1 + 1):
        yy = grid.y0 + iy * grid.pitch
        a = (py <= yy) & (qy > yy)
        b = (qy <= yy) & (py > yy)
        sel = a | b
        if not sel.any():
            continue
        t = (yy - py[sel]) / (qy[sel] - py[sel])
        xi = px[sel] + t * (qx[sel] - px[sel])
        xi = np.sort(xi)
        for k in range(0, len(xi) - 1, 2):
            ia = int(math.ceil((xi[k] - grid.x0) / grid.pitch))
            ib = int(math.floor((xi[k + 1] - grid.x0) / grid.pitch))
            ia = max(ia, 0)
            ib = min(ib, grid.nx - 1)
            if ib >= ia:
                out[iy, ia:ib + 1] = value


def poly_distance_field(grid, polys, margin_nm):
    """Distance (nm) from each cell centre in a local bbox to the polygon set.
    Cells inside get 0.  Returns (sy, sx, D) or None."""
    allpts = [p for pts, holes in polys for p in pts]
    if len(allpts) < 3:
        return None
    xs = [p[0] for p in allpts]
    ys = [p[1] for p in allpts]
    ia = max(0, int(math.floor((min(xs) - margin_nm - grid.x0) / grid.pitch)))
    ib = min(grid.nx - 1, int(math.ceil((max(xs) + margin_nm - grid.x0) / grid.pitch)))
    ja = max(0, int(math.floor((min(ys) - margin_nm - grid.y0) / grid.pitch)))
    jb = min(grid.ny - 1, int(math.ceil((max(ys) + margin_nm - grid.y0) / grid.pitch)))
    if ib < ia or jb < ja:
        return None
    gx = (grid.x0 + np.arange(ia, ib + 1) * grid.pitch).astype(np.float64)
    gy = (grid.y0 + np.arange(ja, jb + 1) * grid.pitch).astype(np.float64)
    X = gx[None, :]
    Y = gy[:, None]
    D = np.full((jb - ja + 1, ib - ia + 1), 1e18)
    for pts, holes in polys:
        for ring in [pts] + list(holes):
            n = len(ring)
            if n < 2:
                continue
            px = np.array([p[0] for p in ring], dtype=np.float64)
            py = np.array([p[1] for p in ring], dtype=np.float64)
            qx = np.roll(px, -1)
            qy = np.roll(py, -1)
            for k in range(n):
                dx, dy = qx[k] - px[k], qy[k] - py[k]
                L2 = dx * dx + dy * dy
                ux = X - px[k]
                uy = Y - py[k]
                if L2 <= 0:
                    d2 = ux * ux + uy * uy
                else:
                    t = np.clip((ux * dx + uy * dy) / L2, 0.0, 1.0)
                    d2 = (ux - t * dx) ** 2 + (uy - t * dy) ** 2
                np.minimum(D, d2, out=D)
    np.sqrt(D, out=D)
    # inside test
    ins = np.zeros(D.shape, bool)
    sub = Grid(grid.x0 + ia * grid.pitch, grid.y0 + ja * grid.pitch, grid.pitch,
               ib - ia + 1, jb - ja + 1)
    for pts, holes in polys:
        fill_polygon(sub, pts, ins, True)
    for pts, holes in polys:
        for hpts in holes:
            fill_polygon(sub, hpts, ins, False)
    D[ins] = 0.0
    return (slice(ja, jb + 1), slice(ia, ib + 1), D.astype(np.float32))


def seg_mask(grid, p0, p1, radius_nm):
    """(sy, sx, mask) for cells within radius_nm of segment p0-p1."""
    x0, y0 = p0
    x1, y1 = p1
    r = radius_nm + 0.5 * grid.pitch
    ax0 = min(x0, x1) - r
    ax1 = max(x0, x1) + r
    ay0 = min(y0, y1) - r
    ay1 = max(y0, y1) + r
    ia = max(0, int(math.floor((ax0 - grid.x0) / grid.pitch)))
    ib = min(grid.nx - 1, int(math.ceil((ax1 - grid.x0) / grid.pitch)))
    ja = max(0, int(math.floor((ay0 - grid.y0) / grid.pitch)))
    jb = min(grid.ny - 1, int(math.ceil((ay1 - grid.y0) / grid.pitch)))
    if ib < ia or jb < ja:
        return None
    gx = grid.x0 + np.arange(ia, ib + 1) * grid.pitch
    gy = grid.y0 + np.arange(ja, jb + 1) * grid.pitch
    X = gx[None, :] - x0
    Y = gy[:, None] - y0
    dx, dy = x1 - x0, y1 - y0
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        d2 = X * X + Y * Y
    else:
        t = np.clip((X * dx + Y * dy) / L2, 0.0, 1.0)
        d2 = (X - t * dx) ** 2 + (Y - t * dy) ** 2
    return (slice(ja, jb + 1), slice(ia, ib + 1), d2 <= r * r)


def stamp_net(arr, sm, val):
    if sm is None:
        return
    sy, sx, m = sm
    sub = arr[sy, sx]
    sub[m & (sub == 0)] = val


def stamp_bool(arr, sm):
    if sm is None:
        return
    sy, sx, m = sm
    arr[sy, sx] |= m


# --------------------------------------------------------------------------- BFS


NEI = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]


def spread(mask):
    return dilate1(mask)


def maze(blocked, via_ok, src, tgt, via_steps, max_steps):
    """blocked,(2,h,w) bool; via_ok (h,w) bool; src,tgt (2,h,w) bool.
    Returns (dist, (l,y,x)) or None."""
    nl, h, w = blocked.shape
    dist = np.full((nl, h, w), -1, np.int32)
    cur = src & ~blocked
    if not cur.any():
        return None
    dist[cur] = 0
    if (cur & tgt).any():
        idx = np.argwhere(cur & tgt)[0]
        return dist, (int(idx[0]), int(idx[1]), int(idx[2]))
    pending = {}
    vo = via_ok[None, :, :]
    if via_steps > 0:
        vs = cur & vo
        if vs.any():
            arr = np.zeros((nl, h, w), bool)
            arr[0] = vs[1]
            arr[1] = vs[0]
            pending[via_steps] = arr
    for t in range(1, max_steps + 1):
        new = np.empty((nl, h, w), bool)
        for l in range(nl):
            new[l] = spread(cur[l])
        p = pending.pop(t, None)
        if p is not None:
            new |= p
        new &= ~blocked
        new &= dist < 0
        if not new.any():
            if not pending:
                return None
            cur = new
            continue
        dist[new] = t
        if via_steps > 0:
            vsites = new & vo
            if vsites.any():
                tt = t + via_steps
                arr = pending.get(tt)
                if arr is None:
                    arr = np.zeros((nl, h, w), bool)
                    pending[tt] = arr
                arr[0] |= vsites[1]
                arr[1] |= vsites[0]
        hit = new & tgt
        if hit.any():
            idx = np.argwhere(hit)[0]
            return dist, (int(idx[0]), int(idx[1]), int(idx[2]))
        cur = new
    return None


def backtrace(dist, start, via_steps):
    nl, h, w = dist.shape
    l, y, x = start
    path = [(l, y, x)]
    d = int(dist[l, y, x])
    prev = None
    guard = 0
    while d > 0:
        guard += 1
        if guard > 100000:
            return None
        order = NEI if prev is None else ([prev] + [n for n in NEI if n != prev])
        nxt = None
        for (dy, dx) in order:
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and dist[l, ny, nx] == d - 1:
                nxt = (l, ny, nx, (dy, dx))
                break
        if nxt is None:
            if via_steps > 0 and d - via_steps >= 0 and dist[1 - l, y, x] == d - via_steps:
                nxt = (1 - l, y, x, None)
            else:
                return None
        l, y, x = nxt[0], nxt[1], nxt[2]
        prev = nxt[3]
        d = int(dist[l, y, x])
        path.append((l, y, x))
    path.reverse()
    return path


# --------------------------------------------------------------------------- router


class Router:
    def __init__(self, in_path, out_path):
        self.in_path = in_path
        self.out_path = out_path
        base = os.path.splitext(in_path)[0]
        self.pro = parse_pro(base + ".kicad_pro")
        self.dru_w, self.dru_skew = parse_dru(base + ".kicad_dru")
        self.board = pcbnew.LoadBoard(in_path)
        b = self.board
        self.layers = list(b.GetEnabledLayers().CuStack())
        if len(self.layers) < 2:
            self.layers = [pcbnew.F_Cu, pcbnew.B_Cu]
        self.layers = self.layers[:2]
        self.lidx = {lay: i for i, lay in enumerate(self.layers)}
        try:
            self.thickness = b.GetDesignSettings().GetBoardThickness()
        except Exception:
            self.thickness = int(1.6 * MM)

        # via geometry: smallest legal
        p = self.pro
        drill = max(p["min_hole"], 0.2)
        dia = max(p["min_via"], drill + 2 * p["annular"] + 0.001)
        self.via_drill = int(round(drill * MM))
        self.via_dia = int(round(dia * MM))
        self.clr = p["clearance"] * MM
        self.edge_clr = p["edge"] * MM

        # grid
        bb = b.GetBoardEdgesBoundingBox()
        if bb.GetWidth() <= 0:
            bb = b.GetBoundingBox()
        maxdim = max(bb.GetWidth(), bb.GetHeight()) / MM
        pitch_mm = min(0.2, max(0.1, maxdim / 1150.0))
        self.pitch = pitch_mm * MM
        pad_nm = int(2.0 * MM)
        x0 = bb.GetX() - pad_nm
        y0 = bb.GetY() - pad_nm
        nx = int((bb.GetWidth() + 2 * pad_nm) / self.pitch) + 2
        ny = int((bb.GetHeight() + 2 * pad_nm) / self.pitch) + 2
        self.g = Grid(x0, y0, self.pitch, nx, ny)
        log("grid %dx%d pitch %.3fmm clr %.3f via %.2f/%.2f" %
            (nx, ny, pitch_mm, p["clearance"], dia, drill))
        self.new_items = []
        self.net_items = {}
        self.fail_log = []
        self.unblock_pads = False

    # ---------------------------------------------------------------- setup
    def build(self):
        b = self.board
        g = self.g
        ny, nx = g.ny, g.nx
        try:
            pcbnew.ZONE_FILLER(b).Fill(b.Zones())
        except Exception:
            pass
        b.BuildConnectivity()

        # --- board outline -> inside mask
        inside = np.zeros((ny, nx), bool)
        poly = pcbnew.SHAPE_POLY_SET()
        try:
            b.GetBoardPolygonOutlines(poly)
        except Exception:
            pass
        if poly.OutlineCount() == 0:
            inside[:] = True
        else:
            for pts, holes in poly_points(poly):
                fill_polygon(g, pts, inside, True)
            for pts, holes in poly_points(poly):
                for hpts in holes:
                    fill_polygon(g, hpts, inside, False)
        self.outside = ~inside

        # --- width classes
        widths = set()
        for name, pads in self.net_pads_pre().items():
            widths.add(self.net_width(name))
        widths.add(int(round(self.pro["min_track"] * MM)))
        self.classes = sorted(widths)                     # track widths in nm
        self.cls_of = {w: i for i, w in enumerate(self.classes)}
        self.radii = [w / 2.0 + self.clr for w in self.classes]
        self.via_r = self.via_dia / 2.0
        self.via_halo = self.via_r + self.clr
        max_halo = max(self.radii + [self.via_halo])
        nc = len(self.classes)

        # --- pads
        self.pad_cnt = np.zeros((nc + 1, 2, ny, nx), np.uint16)   # last index = via class
        self.padnet = np.zeros((2, ny, nx), np.int32)
        self.holes = np.zeros((ny, nx), bool)
        self.pad_info = {}
        self.net_pads = {}
        radii = self.radii + [self.via_halo]
        for fp in b.GetFootprints():
            for pad in fp.Pads():
                code = pad.GetNetCode()
                name = pad.GetNetname()
                val = (code + 1) if name else -1
                lays = [i for i, lay in enumerate(self.layers) if pad.IsOnLayer(lay)]
                if pad.HasHole():
                    lays = [0, 1]
                if not lays:
                    continue
                polys = None
                try:
                    ps = pad.GetEffectivePolygon()
                    if ps is not None and ps.OutlineCount():
                        polys = poly_points(ps)
                except Exception:
                    polys = None
                if not polys:
                    bbp = pad.GetBoundingBox()
                    polys = [([(bbp.GetX(), bbp.GetY()), (bbp.GetRight(), bbp.GetY()),
                               (bbp.GetRight(), bbp.GetBottom()), (bbp.GetX(), bbp.GetBottom())], [])]
                df = poly_distance_field(g, polys, max_halo + g.pitch)
                if df is None:
                    continue
                sy, sx, D = df
                core = D <= 0.0
                for i in lays:
                    sub = self.padnet[i][sy, sx]
                    sub[core & (sub != 0) & (sub != val)] = -1
                    sub[core & (sub == 0)] = val
                for ci, r in enumerate(radii):
                    hm = (D < r)
                    for i in lays:
                        self.pad_cnt[ci, i][sy, sx] += hm
                if pad.HasHole():
                    ds = pad.GetDrillSize()
                    hr = max(ds.x, ds.y) / 2.0
                    sm = seg_mask(g, (pad.GetPosition().x, pad.GetPosition().y),
                                  (pad.GetPosition().x, pad.GetPosition().y), hr)
                    stamp_bool(self.holes, sm)
                if name:
                    self.net_pads.setdefault(name, []).append(pad)
                self.pad_info[pad.m_Uuid.AsString()] = (lays, sy, sx, D)

        # --- graphics on copper layers (text, shapes) block everything
        maxr = max(self.radii + [self.via_halo])
        drawables = list(b.GetDrawings())
        for fp in b.GetFootprints():
            drawables += list(fp.GraphicalItems())
        culay = set(self.layers)
        for it in drawables:
            try:
                lay = it.GetLayer()
            except Exception:
                continue
            if lay not in culay:
                continue
            li = self.lidx[lay]
            ps = pcbnew.SHAPE_POLY_SET()
            try:
                it.TransformShapeToPolygon(ps, lay, 0, int(0.01 * MM), pcbnew.ERROR_OUTSIDE)
            except Exception:
                try:
                    it.TransformShapeToPolygon(ps, lay, 0, int(0.01 * MM), pcbnew.ERROR_OUTSIDE, False)
                except Exception:
                    continue
            if ps.OutlineCount() == 0:
                continue
            for pts, holes in poly_points(ps):
                df = poly_distance_field(g, [(pts, holes)], maxr + g.pitch)
                if df is None:
                    continue
                sy, sx, D = df
                sub = self.padnet[li][sy, sx]
                sub[D <= 0.0] = -1
                for ci, r in enumerate(self.radii + [self.via_halo]):
                    self.pad_cnt[ci, li][sy, sx] += (D < r)

        # keepout rule areas
        self.keepout = np.zeros((2, ny, nx), bool)
        for z in b.Zones():
            try:
                if not z.GetIsRuleArea():
                    continue
            except Exception:
                continue
            block = True
            try:
                block = z.GetDoNotAllowTracks() or z.GetDoNotAllowVias()
            except Exception:
                pass
            if not block:
                continue
            m = np.zeros((ny, nx), bool)
            try:
                for pts, holes in poly_points(z.Outline()):
                    fill_polygon(g, pts, m, True)
            except Exception:
                continue
            for i, lay in enumerate(self.layers):
                if z.IsOnLayer(lay):
                    self.keepout[i] |= m

        # track copper we add
        self.trk = np.zeros((2, ny, nx), np.int32)
        self.trkcls = np.full((2, ny, nx), -1, np.int8)
        self.vianet = np.zeros((ny, nx), np.int32)

        # --- zone copper per net (own-net resource, not an obstacle)
        self.zone_mask = {}
        for z in b.Zones():
            try:
                if z.GetIsRuleArea():
                    continue
            except Exception:
                pass
            name = z.GetNetname()
            if not name:
                continue
            for i, lay in enumerate(self.layers):
                if not z.IsOnLayer(lay):
                    continue
                m = self.zone_mask.get(name)
                if m is None:
                    m = np.zeros((2, ny, nx), bool)
                    self.zone_mask[name] = m
                try:
                    ps = z.GetFilledPolysList(lay)
                except Exception:
                    ps = None
                if ps is None or ps.OutlineCount() == 0:
                    continue
                tmp = np.zeros((ny, nx), bool)
                for pts, holes in poly_points(ps):
                    fill_polygon(g, pts, tmp, True)
                for pts, holes in poly_points(ps):
                    for hpts in holes:
                        fill_polygon(g, hpts, tmp, False)
                m[i] |= tmp

        self.edge_block = [dilate_disk(self.outside, 1 + int(math.ceil((w / 2.0 + self.edge_clr) / g.pitch)))
                           for w in self.classes]
        self.edge_block_via = dilate_disk(self.outside, 1 + int(math.ceil((self.via_r + self.edge_clr) / g.pitch)))

    def net_pads_pre(self):
        d = {}
        for fp in self.board.GetFootprints():
            for pad in fp.Pads():
                n = pad.GetNetname()
                if n:
                    d.setdefault(n, []).append(pad)
        return d

    # ---------------------------------------------------------------- nets
    def net_width(self, name):
        w = max(self.dru_w.get(name, 0.0), self.pro["min_track"])
        return int(round(w * MM))

    def clusters_for(self, name):
        """Groups of pads already connected to each other (through zones)."""
        pads = self.net_pads.get(name, [])
        conn = self.board.GetConnectivity()
        idx = {p.m_Uuid.AsString(): i for i, p in enumerate(pads)}
        parent = list(range(len(pads)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def uni(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
        for i, p in enumerate(pads):
            try:
                for q in conn.GetConnectedPads(p):
                    j = idx.get(q.m_Uuid.AsString())
                    if j is not None:
                        uni(i, j)
            except Exception:
                pass
        groups = {}
        for i in range(len(pads)):
            groups.setdefault(find(i), []).append(pads[i])
        return list(groups.values())

    # ---------------------------------------------------------------- routing
    def net_masks(self, name, val, cls, sl, wh, ww, own_pad_cnt, own_via_cnt):
        g = self.g
        hw = self.classes[cls] / 2.0
        nc = len(self.classes)
        blocked = np.zeros((2, wh, ww), bool)
        for i in range(2):
            pc = self.pad_cnt[cls, i][sl[0], sl[1]].astype(np.int32)
            if own_pad_cnt is not None:
                pc -= own_pad_cnt[i]
            blocked[i] = pc > 0
        blocked |= self.keepout[:, sl[0], sl[1]]
        blocked |= self.edge_block[cls][None, sl[0], sl[1]]
        # other nets' tracks
        trkw = self.trk[:, sl[0], sl[1]]
        clsw = self.trkcls[:, sl[0], sl[1]]
        for c in range(nc):
            r = int(math.ceil((hw + self.classes[c] / 2.0 + self.clr) / g.pitch))
            for i in range(2):
                m = (clsw[i] == c) & (trkw[i] != val)
                if m.any():
                    blocked[i] |= dilate_disk(m, r)
        vw = self.vianet[sl[0], sl[1]]
        mv = (vw != 0) & (vw != val)
        if mv.any():
            d = dilate_disk(mv, int(math.ceil((hw + self.via_r + self.clr) / g.pitch)))
            blocked[0] |= d
            blocked[1] |= d
        # via sites
        pcv = self.pad_cnt[nc, 0][sl[0], sl[1]].astype(np.int32) + \
            self.pad_cnt[nc, 1][sl[0], sl[1]].astype(np.int32)
        if own_via_cnt is not None:
            pcv -= own_via_cnt
        via_ok = pcv <= 0
        via_ok &= ~self.edge_block_via[sl[0], sl[1]]
        via_ok &= ~(self.keepout[0][sl[0], sl[1]] | self.keepout[1][sl[0], sl[1]])
        for c in range(nc):
            r = int(math.ceil((self.via_r + self.classes[c] / 2.0 + self.clr) / g.pitch))
            for i in range(2):
                m = (clsw[i] == c) & (trkw[i] != val)
                if m.any():
                    via_ok &= ~dilate_disk(m, r)
        if mv.any():
            via_ok &= ~dilate_disk(mv, int(math.ceil((2 * self.via_r + self.clr) / g.pitch)))
        hw_mask = self.holes[sl[0], sl[1]]
        if hw_mask.any():
            via_ok &= ~dilate_disk(hw_mask, int(math.ceil(
                (self.via_drill / 2.0 + self.pro["h2h"] * MM) / g.pitch)))
        return blocked, via_ok

    def route_net(self, name, window_scale=1.0, via_steps=None):
        g = self.g
        ny, nx = g.ny, g.nx
        pads = self.net_pads.get(name, [])
        if len(pads) < 2:
            return True, 0
        clusters = self.clusters_for(name)
        if len(clusters) < 2:
            return True, 0
        zm = self.zone_mask.get(name)
        code = pads[0].GetNetCode()
        val = code + 1
        width = self.net_width(name)
        cls = self.cls_of[width]
        nc = len(self.classes)

        own = np.zeros((2, ny, nx), bool)
        cl_masks = []
        for cl in clusters:
            m = np.zeros((2, ny, nx), bool)
            for p in cl:
                info = self.pad_info.get(p.m_Uuid.AsString())
                if info is None:
                    continue
                lays, sy, sx, D = info
                core = D <= 0.0
                for i in lays:
                    m[i][sy, sx] |= core
            cl_masks.append(m)
            own |= m
        if zm is not None:
            own |= zm
        for i in range(2):
            own[i] |= (self.trk[i] == val)

        xs = [p.GetPosition().x for p in pads]
        ys = [p.GetPosition().y for p in pads]
        marg = max(5.0 * MM, 0.4 * max(max(xs) - min(xs), max(ys) - min(ys))) * window_scale
        wx0 = max(0, int((min(xs) - marg - g.x0) / g.pitch))
        wx1 = min(nx - 1, int((max(xs) + marg - g.x0) / g.pitch))
        wy0 = max(0, int((min(ys) - marg - g.y0) / g.pitch))
        wy1 = min(ny - 1, int((max(ys) + marg - g.y0) / g.pitch))
        sl = (slice(wy0, wy1 + 1), slice(wx0, wx1 + 1))
        wh, ww = wy1 - wy0 + 1, wx1 - wx0 + 1
        if via_steps is None:
            via_steps = max(1, int(round((1.2 * MM) / g.pitch)))

        # own pads' halo counts within the window (to subtract)
        own_pad_cnt = np.zeros((2, wh, ww), np.int32)
        own_via_cnt = np.zeros((wh, ww), np.int32)
        for p in pads:
            info = self.pad_info.get(p.m_Uuid.AsString())
            if info is None:
                continue
            lays, sy, sx, D = info
            ry0 = max(sy.start, wy0)
            ry1 = min(sy.stop, wy1 + 1)
            rx0 = max(sx.start, wx0)
            rx1 = min(sx.stop, wx1 + 1)
            if ry1 <= ry0 or rx1 <= rx0:
                continue
            sub = D[ry0 - sy.start:ry1 - sy.start, rx0 - sx.start:rx1 - sx.start]
            ht = sub < self.radii[cls]
            hv = sub < self.via_halo
            for i in lays:
                own_pad_cnt[i, ry0 - wy0:ry1 - wy0, rx0 - wx0:rx1 - wx0] += ht
                own_via_cnt[ry0 - wy0:ry1 - wy0, rx0 - wx0:rx1 - wx0] += hv

        done = 0
        order = sorted(range(len(clusters)), key=lambda i: -len(clusters[i]))
        joined = {order[0]}
        srcmask = cl_masks[order[0]].copy()
        if zm is not None:
            srcmask |= zm
        for i in range(2):
            srcmask[i] |= (self.trk[i] == val)
        remaining = set(order[1:])
        failed = 0
        while remaining:
            if left() < 20:
                return False, done
            blocked, via_ok = self.net_masks(name, val, cls, sl, wh, ww, own_pad_cnt, own_via_cnt)
            ownw = own[:, sl[0], sl[1]]
            srcw = srcmask[:, sl[0], sl[1]].copy()
            srcpts = [p.GetPosition() for i in joined for p in clusters[i]]
            best = None
            for j in remaining:
                for q in clusters[j]:
                    qp = q.GetPosition()
                    d = min((abs(sp.x - qp.x) + abs(sp.y - qp.y)) for sp in srcpts)
                    if best is None or d < best[0]:
                        best = (d, j)
            j = best[1]
            tgtw = cl_masks[j][:, sl[0], sl[1]].copy()
            if not srcw.any() or not tgtw.any():
                remaining.discard(j)
                failed += 1
                continue
            if self.unblock_pads:
                blocked &= ~(srcw | tgtw)
            maxsteps = int((wh + ww) * 1.8) + 60
            res = maze(blocked, via_ok, srcw, tgtw, via_steps, maxsteps)
            if res is None:
                remaining.discard(j)
                failed += 1
                self.fail_log.append(name)
                continue
            dist, hitcell = res
            path = backtrace(dist, hitcell, via_steps)
            if path is None:
                remaining.discard(j)
                failed += 1
                continue
            self.emit_path(path, wy0, wx0, code, name, width, cls, own, srcmask, val)
            joined.add(j)
            remaining.discard(j)
            done += 1
        return failed == 0, done

    def emit_path(self, path, wy0, wx0, code, name, width, cls, own, srcmask, val):
        g = self.g
        net = self.board.FindNet(code)
        cells = [(l, y + wy0, x + wx0) for (l, y, x) in path]
        segs = []
        vias = []
        i = 0
        while i < len(cells):
            l = cells[i][0]
            j = i
            while j + 1 < len(cells) and cells[j + 1][0] == l:
                j += 1
            run = cells[i:j + 1]
            pts = [g.to_pt(c[1], c[2]) for c in run]
            keep = [pts[0]]
            for k in range(1, len(pts) - 1):
                ax, ay = keep[-1]
                bx, by = pts[k]
                cx, cy = pts[k + 1]
                if (bx - ax) * (cy - by) == (by - ay) * (cx - bx):
                    continue
                keep.append(pts[k])
            if len(pts) > 1:
                keep.append(pts[-1])
            for k in range(len(keep) - 1):
                segs.append((l, keep[k], keep[k + 1]))
            if j + 1 < len(cells):
                vias.append(g.to_pt(run[-1][1], run[-1][2]))
            i = j + 1
        for (l, y, x) in (cells[0], cells[-1]):
            pt = g.to_pt(y, x)
            for p in self.net_pads.get(name, []):
                info = self.pad_info.get(p.m_Uuid.AsString())
                if info is None:
                    continue
                lays, sy, sx, D = info
                if l in lays and sy.start <= y < sy.stop and sx.start <= x < sx.stop \
                        and D[y - sy.start, x - sx.start] <= 0.0:
                    c = p.GetPosition()
                    if (c.x, c.y) != pt:
                        segs.append((l, pt, (c.x, c.y)))
                    break
        made = []
        for (l, p0, p1) in segs:
            if p0 == p1:
                continue
            t = pcbnew.PCB_TRACK(self.board)
            t.SetStart(pcbnew.VECTOR2I(int(p0[0]), int(p0[1])))
            t.SetEnd(pcbnew.VECTOR2I(int(p1[0]), int(p1[1])))
            t.SetWidth(int(width))
            t.SetLayer(self.layers[l])
            t.SetNet(net)
            self.board.Add(t)
            made.append(t)
            sm = seg_mask(g, p0, p1, 0.0)
            if sm is not None:
                sy, sx, m = sm
                sub = self.trk[l][sy, sx]
                sub[m] = val
                subc = self.trkcls[l][sy, sx]
                subc[m] = cls
                own[l][sy, sx] |= m
                srcmask[l][sy, sx] |= m
        for pt in vias:
            v = pcbnew.PCB_VIA(self.board)
            v.SetPosition(pcbnew.VECTOR2I(int(pt[0]), int(pt[1])))
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetLayerPair(self.layers[0], self.layers[1])
            v.SetDrill(self.via_drill)
            try:
                v.SetWidth(self.layers[0], self.via_dia)
            except Exception:
                try:
                    v.SetWidth(self.via_dia)
                except Exception:
                    pass
            v.SetNet(net)
            self.board.Add(v)
            made.append(v)
            sm = seg_mask(g, pt, pt, 0.0)
            if sm is not None:
                sy, sx, m = sm
                self.vianet[sy, sx][m] = val
                for l in (0, 1):
                    own[l][sy, sx] |= m
                    srcmask[l][sy, sx] |= m
            smh = seg_mask(g, pt, pt, self.via_drill / 2.0)
            stamp_bool(self.holes, smh)
        self.net_items.setdefault(name, []).extend(made)
        self.new_items.extend(made)

    # ---------------------------------------------------------------- driver
    def save(self):
        try:
            pcbnew.SaveBoard(self.out_path, self.board)
        except Exception:
            log("save failed")

    def ripup(self, name):
        for it in self.net_items.pop(name, []):
            try:
                self.board.Remove(it)
            except Exception:
                pass
        pads = self.net_pads.get(name, [])
        if not pads:
            return
        val = pads[0].GetNetCode() + 1
        for i in range(2):
            m = self.trk[i] == val
            self.trk[i][m] = 0
            self.trkcls[i][m] = -1
        self.vianet[self.vianet == val] = 0

    def run(self):
        self.build()
        self.save()
        log("setup done; classes", [c / 1e6 for c in self.classes])
        todo = []
        for name, pads in self.net_pads.items():
            if len(pads) < 2:
                continue
            cl = self.clusters_for(name)
            if len(cl) < 2:
                continue
            xs = [p.GetPosition().x for p in pads]
            ys = [p.GetPosition().y for p in pads]
            todo.append((max(max(xs) - min(xs), max(ys) - min(ys)), len(cl), name))
        todo.sort()
        pair_nets = set()
        for pfx in self.dru_skew:
            for name in self.net_pads:
                if name.startswith(pfx) and name[len(pfx):] in ("+", "-", "_P", "_N", "P", "N"):
                    pair_nets.add(name)
        todo.sort(key=lambda t: (0 if t[2] in pair_nets else 1, t[0]))
        log("nets to route:", len(todo))
        bad = []
        ok = 0
        for k, (span, ncl, name) in enumerate(todo):
            if left() < 45:
                log("out of time at net", k)
                bad += [t[2] for t in todo[k:]]
                break
            try:
                good, n = self.route_net(name)
            except Exception:
                log("error on", name, traceback.format_exc())
                good = False
            if good:
                ok += 1
            else:
                bad.append(name)
            if (k + 1) % 20 == 0:
                self.save()
                log("routed %d/%d (%d ok)" % (k + 1, len(todo), ok))
        log("pass 1: %d/%d complete" % (ok, len(todo)))
        self.save()
        # pass 2: rip up what failed and try again with a wider window
        for rnd, scale in ((2, 2.5), (3, 6.0)):
            if not bad or left() < 60:
                break
            again = []
            for name in bad:
                if left() < 40:
                    again.append(name)
                    continue
                self.ripup(name)
                try:
                    good, n = self.route_net(name, window_scale=scale)
                except Exception:
                    good = False
                if good:
                    ok += 1
                else:
                    again.append(name)
            bad = again
            log("pass %d: %d/%d complete" % (rnd, ok, len(todo)))
            self.save()
        # pass 3: last resort, allow escaping through tight pad halos
        if bad and left() > 50:
            self.unblock_pads = True
            for name in bad:
                if left() < 35:
                    break
                self.ripup(name)
                try:
                    self.route_net(name, window_scale=3.0)
                except Exception:
                    pass
            self.save()
        self.match_pairs(pair_nets)
        self.save()

    # ---------------------------------------------------------------- diff pairs
    def net_length(self, name):
        total = 0.0
        nv = 0
        for it in self.net_items.get(name, []):
            cls = it.GetClass()
            if cls == "PCB_VIA":
                nv += 1
            else:
                a, bq = it.GetStart(), it.GetEnd()
                total += math.hypot(bq.x - a.x, bq.y - a.y)
        return total + nv * self.thickness, nv

    def match_pairs(self, pair_nets):
        for pfx, tol in self.dru_skew.items():
            members = sorted(n for n in pair_nets if n.startswith(pfx))
            if len(members) != 2:
                continue
            la, _ = self.net_length(members[0])
            lb, _ = self.net_length(members[1])
            if la == 0 or lb == 0:
                continue
            delta = abs(la - lb)
            if delta <= tol * MM * 0.75:
                continue
            short = members[0] if la < lb else members[1]
            log("pair %s skew %.2fmm tol %.2f -> lengthen %s" % (pfx, delta / MM, tol, short))
            need = delta - tol * MM * 0.5
            if need <= 0:
                continue
            try:
                self.lengthen(short, need)
            except Exception:
                log("lengthen failed", short, traceback.format_exc().splitlines()[-1])

    def lengthen(self, name, need):
        """Add `need` nm of copper to this net by bumping one of its straight segments."""
        g = self.g
        pads = self.net_pads.get(name, [])
        if not pads:
            return
        val = pads[0].GetNetCode() + 1
        cls = self.cls_of[self.net_width(name)]
        hw = self.classes[cls] / 2.0
        items = [it for it in self.net_items.get(name, []) if it.GetClass() != "PCB_VIA"]
        items.sort(key=lambda it: -math.hypot(it.GetEnd().x - it.GetStart().x,
                                              it.GetEnd().y - it.GetStart().y))
        for it in items:
            a, b = it.GetStart(), it.GetEnd()
            L = math.hypot(b.x - a.x, b.y - a.y)
            if L < 0.4 * MM:
                continue
            ux, uy = (b.x - a.x) / L, (b.y - a.y) / L
            nxv, nyv = -uy, ux
            for nb in (1, 2, 3, 4, 6, 8):
                h = need / (2.0 * nb)
                if h < 0.1 * MM or h > 5.0 * MM:
                    continue
                seglen = L / nb
                if seglen < h + 0.25 * MM:
                    continue
                pts = []
                okk = True
                cur = (a.x, a.y)
                for q in range(nb):
                    s0 = a.x + ux * (seglen * q + 0.08 * MM), a.y + uy * (seglen * q + 0.08 * MM)
                    s1 = a.x + ux * (seglen * (q + 1) - 0.08 * MM), a.y + uy * (seglen * (q + 1) - 0.08 * MM)
                    sgn = 1 if q % 2 == 0 else -1
                    t0 = (s0[0] + nxv * h * sgn, s0[1] + nyv * h * sgn)
                    t1 = (s1[0] + nxv * h * sgn, s1[1] + nyv * h * sgn)
                    pts += [(cur, s0), (s0, t0), (t0, t1), (t1, s1)]
                    cur = s1
                pts.append((cur, (b.x, b.y)))
                for (p0, p1) in pts:
                    if not self.seg_free(p0, p1, val, cls, hw):
                        okk = False
                        break
                if not okk:
                    continue
                net = self.board.FindNet(pads[0].GetNetCode())
                try:
                    self.board.Remove(it)
                except Exception:
                    pass
                self.net_items[name].remove(it)
                for (p0, p1) in pts:
                    if abs(p0[0] - p1[0]) < 1 and abs(p0[1] - p1[1]) < 1:
                        continue
                    t = pcbnew.PCB_TRACK(self.board)
                    t.SetStart(pcbnew.VECTOR2I(int(p0[0]), int(p0[1])))
                    t.SetEnd(pcbnew.VECTOR2I(int(p1[0]), int(p1[1])))
                    t.SetWidth(self.classes[cls])
                    t.SetLayer(it.GetLayer())
                    t.SetNet(net)
                    self.board.Add(t)
                    self.net_items[name].append(t)
                    sm = seg_mask(g, p0, p1, 0.0)
                    if sm is not None:
                        sy, sx, m = sm
                        self.trk[self.lidx[it.GetLayer()]][sy, sx][m] = val
                        self.trkcls[self.lidx[it.GetLayer()]][sy, sx][m] = cls
                log("lengthened %s by %.2fmm" % (name, need / MM))
                return

    def seg_free(self, p0, p1, val, cls, hw):
        """Is a track of this net free of conflicts along p0-p1?"""
        g = self.g
        sm = seg_mask(g, p0, p1, 0.0)
        if sm is None:
            return False
        sy, sx, m = sm
        wh = sy.stop - sy.start
        ww = sx.stop - sx.start
        if wh <= 0 or ww <= 0:
            return False
        pc = self.pad_cnt[cls, 0][sy, sx].astype(np.int32)
        pc2 = self.pad_cnt[cls, 1][sy, sx].astype(np.int32)
        li = None
        own = np.zeros((wh, ww), np.int32)
        for p in self.net_pads.get(self.board.FindNet(val - 1).GetNetname(), []):
            info = self.pad_info.get(p.m_Uuid.AsString())
            if info is None:
                continue
            lays, psy, psx, D = info
            ry0 = max(psy.start, sy.start)
            ry1 = min(psy.stop, sy.stop)
            rx0 = max(psx.start, sx.start)
            rx1 = min(psx.stop, sx.stop)
            if ry1 <= ry0 or rx1 <= rx0:
                continue
            sub = D[ry0 - psy.start:ry1 - psy.start, rx0 - psx.start:rx1 - psx.start]
            own[ry0 - sy.start:ry1 - sy.start, rx0 - sx.start:rx1 - sx.start] += (sub < self.radii[cls])
        bad = ((pc + pc2 - 2 * own) > 0)
        if (bad & m).any():
            return False
        if (self.edge_block[cls][sy, sx] & m).any():
            return False
        if (self.keepout[:, sy, sx].any(axis=0) & m).any():
            return False
        for c in range(len(self.classes)):
            r = int(math.ceil((hw + self.classes[c] / 2.0 + self.clr) / g.pitch))
            big = (slice(max(0, sy.start - r), min(g.ny, sy.stop + r)),
                   slice(max(0, sx.start - r), min(g.nx, sx.stop + r)))
            for i in range(2):
                mm = (self.trkcls[i][big] == c) & (self.trk[i][big] != val)
                if mm.any():
                    d = dilate_disk(mm, r)
                    if (d[sy.start - big[0].start:sy.start - big[0].start + wh,
                          sx.start - big[1].start:sx.start - big[1].start + ww] & m).any():
                        return False
        return True


def main():
    inp, outp = sys.argv[1], sys.argv[2]
    # make sure a readable file exists no matter what happens next
    try:
        import shutil
        shutil.copyfile(inp, outp)
    except Exception:
        pass
    r = None
    try:
        r = Router(inp, outp)
        r.run()
    except Exception:
        log("fatal", traceback.format_exc())
    if r is not None:
        r.save()
    log("done")


if __name__ == "__main__":
    main()
