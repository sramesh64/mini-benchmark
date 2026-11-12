#!/usr/bin/env python3
"""Grid maze router for 2-layer KiCad boards."""
import sys, os, re, math, time, heapq, json
import numpy as np
import pcbnew

START = time.time()
BUDGET = float(os.environ.get("ROUTER_BUDGET", "390"))
VERBOSE = bool(os.environ.get("ROUTER_VERBOSE"))


def tleft():
    return BUDGET - (time.time() - START)


def log(*a):
    if VERBOSE:
        print("[%6.1fs]" % (time.time() - START), *a, file=sys.stderr, flush=True)


MM = 1e6
PITCHES = [0.03175, 0.0397, 0.0508, 0.0635, 0.0794, 0.0907, 0.1016, 0.127, 0.1588,
           0.1814, 0.2117, 0.254]
CELL_BUDGET = 900000


# ---------------------------------------------------------------- geometry
def fill_poly(outlines, xs, ys, out=None):
    """even-odd fill of polygons (lists of (x,y)) on grid xs (asc) x ys (asc)"""
    if out is None:
        out = np.zeros((len(ys), len(xs)), dtype=bool)
    Xr = xs[None, :]
    for pts in outlines:
        n = len(pts)
        for k in range(n):
            x1, y1 = pts[k]
            x2, y2 = pts[(k + 1) % n]
            if y1 == y2:
                continue
            lo, hi = (y1, y2) if y1 < y2 else (y2, y1)
            i0 = int(np.searchsorted(ys, lo, 'left'))
            i1 = int(np.searchsorted(ys, hi, 'left'))
            if i1 <= i0:
                continue
            yy = ys[i0:i1][:, None]
            xint = x1 + (yy - y1) * (x2 - x1) / (y2 - y1)
            out[i0:i1] ^= (Xr < xint)
    return out


def seg_dist_field(outlines, xs, ys):
    """min distance from each grid point to the polygon boundary segments"""
    X = xs[None, :]
    Y = ys[:, None]
    best = None
    for pts in outlines:
        n = len(pts)
        for k in range(n):
            x1, y1 = pts[k]
            x2, y2 = pts[(k + 1) % n]
            d = point_seg_dist(X, Y, x1, y1, x2, y2)
            best = d if best is None else np.minimum(best, d)
    return best


def point_seg_dist(X, Y, x1, y1, x2, y2):
    dx = x2 - x1
    dy = y2 - y1
    L2 = dx * dx + dy * dy
    px = X - x1
    py = Y - y1
    if L2 <= 0:
        return np.hypot(px, py)
    t = (px * dx + py * dy) / L2
    np.clip(t, 0.0, 1.0, out=t)
    return np.hypot(px - t * dx, py - t * dy)


def polyset_outlines(ps):
    res = []
    for i in range(ps.OutlineCount()):
        o = ps.Outline(i)
        res.append([(o.CPoint(j).x / MM, o.CPoint(j).y / MM) for j in range(o.PointCount())])
        try:
            for h in range(ps.HoleCount(i)):
                ho = ps.Hole(i, h)
                res.append([(ho.CPoint(j).x / MM, ho.CPoint(j).y / MM) for j in range(ho.PointCount())])
        except Exception:
            pass
    return res


# ---------------------------------------------------------------- grid
class Grid:
    def __init__(self, x0, y0, x1, y1, pitch):
        self.p = pitch
        self.ox = x0
        self.oy = y0
        self.W = int(math.ceil((x1 - x0) / pitch)) + 1
        self.H = int(math.ceil((y1 - y0) / pitch)) + 1
        self.xs = x0 + np.arange(self.W) * pitch
        self.ys = y0 + np.arange(self.H) * pitch
        self.LS = self.W * self.H

    def gx(self, x):
        return min(self.W - 1, max(0, int(round((x - self.ox) / self.p))))

    def gy(self, y):
        return min(self.H - 1, max(0, int(round((y - self.oy) / self.p))))

    def xmm(self, i):
        return self.ox + i * self.p

    def ymm(self, j):
        return self.oy + j * self.p

    def win(self, xmin, ymin, xmax, ymax, pad=0.0):
        i0 = max(0, int(math.floor((xmin - pad - self.ox) / self.p)))
        i1 = min(self.W, int(math.ceil((xmax + pad - self.ox) / self.p)) + 1)
        j0 = max(0, int(math.floor((ymin - pad - self.oy) / self.p)))
        j1 = min(self.H, int(math.ceil((ymax + pad - self.oy) / self.p)) + 1)
        return i0, j0, i1, j1


# ---------------------------------------------------------------- main
def main():
    inp, outp = sys.argv[1], sys.argv[2]
    board = pcbnew.LoadBoard(inp)
    try:
        pcbnew.SaveBoard(outp, board)
    except Exception:
        pass

    base = inp[:-10] if inp.endswith(".kicad_pcb") else os.path.splitext(inp)[0]
    rules = {}
    classes = []
    try:
        pro = json.load(open(base + ".kicad_pro"))
        rules = pro.get("board", {}).get("design_settings", {}).get("rules", {})
        classes = pro.get("net_settings", {}).get("classes", [])
    except Exception:
        pass
    clearance = max([c.get("clearance", 0.2) for c in classes] + [rules.get("min_clearance", 0.0), 0.1])
    edge_clr = max(rules.get("min_copper_edge_clearance", 0.0), 0.0)
    min_tw = rules.get("min_track_width", 0.2) or 0.2
    via_drill = max(rules.get("min_through_hole_diameter", 0.3) or 0.3, 0.3)
    via_dia = max(rules.get("min_via_diameter", 0.6) or 0.6,
                  via_drill + 2 * (rules.get("min_via_annular_width", 0.05) or 0.05) + 0.02)

    # per-net minimum track width from the design rules file
    net_w = {}
    pairs = {}
    try:
        txt = open(base + ".kicad_dru").read()
        for chunk in txt.split("(rule ")[1:]:
            m = re.search(r'\(constraint\s+track_width\s+\(min\s+([0-9.]+)mm\)', chunk)
            cond = re.search(r'\(condition\s+"(.*?)"\)', chunk, re.S)
            if m and cond:
                w = float(m.group(1))
                for n in re.findall(r"A\.NetName\s*==\s*'([^']*)'", cond.group(1)):
                    net_w[n] = max(net_w.get(n, 0.0), w)
            m2 = re.search(r'\(constraint\s+skew\s+\(max\s+([0-9.]+)mm\)', chunk)
            if m2 and cond:
                for n in re.findall(r"A\.inDiffPair\('([^']*)'\)", cond.group(1)):
                    pairs[n] = float(m2.group(1))
    except Exception:
        pass

    cu = [l for l in board.GetEnabledLayers().CuStack()]
    if len(cu) > 2:
        cu = [pcbnew.F_Cu, pcbnew.B_Cu]
    layer_of = {0: cu[0], 1: cu[-1]}
    nlay = 2

    bb = board.GetBoardEdgesBoundingBox()
    x0, y0 = bb.GetX() / MM, bb.GetY() / MM
    x1, y1 = x0 + bb.GetWidth() / MM, y0 + bb.GetHeight() / MM
    area = (x1 - x0) * (y1 - y0)
    pitch = PITCHES[-1]
    for p in PITCHES:
        if area / (p * p) <= CELL_BUDGET:
            pitch = p
            break
    # align grid to pad coordinates (most common residue)
    pads = [p for fp in board.GetFootprints() for p in fp.Pads()]
    def align(vals, o):
        r = [((v - o) / pitch) % 1.0 for v in vals]
        if not r:
            return o
        r.sort()
        best, bn = 0.0, -1
        for c in r:
            n = sum(1 for v in r if abs(v - c) < 0.08 or abs(v - c) > 0.92)
            if n > bn:
                bn, best = n, c
        return o + best * pitch - pitch
    ox = align([p.GetPosition().x / MM for p in pads], x0)
    oy = align([p.GetPosition().y / MM for p in pads], y0)
    g = Grid(ox - pitch, oy - pitch, x1 + pitch, y1 + pitch, pitch)
    log("pitch %.4f grid %dx%d  clr %.3f via %.2f/%.2f" % (pitch, g.W, g.H, clearance, via_dia, via_drill))

    # ------------------------------------------------ radii / net widths
    nets = {}
    for code in range(board.GetNetCount()):
        n = board.FindNet(code)
        if n is not None and n.GetNetname():
            nets[code] = n.GetNetname()
    widths = {}
    for code, name in nets.items():
        widths[code] = max(min_tw, net_w.get(name, 0.0))
    uw = sorted(set(widths.values()))
    margin = 0.15 * pitch + 0.008
    RAD = [w / 2 + clearance + margin for w in uw] + [via_dia / 2 + clearance + margin]
    EDGE = [w / 2 + edge_clr + margin for w in uw] + [via_dia / 2 + edge_clr + margin]
    HALF = [w / 2 for w in uw] + [via_dia / 2]
    VIA_RI = len(uw)
    nri = len(RAD)
    ri_of = {code: uw.index(widths[code]) for code in nets}

    owner = np.zeros((nri, nlay, g.H, g.W), dtype=np.int16)

    def stamp(j0, j1, i0, i1, D, inside, basr, code, layers, thr=None):
        """mark cells blocked. D: distance field to shape core; inside: mask or None"""
        for ri in range(nri):
            t = (RAD[ri] + basr) if thr is None else thr[ri]
            if t is None:
                continue
            m = D < t
            if inside is not None:
                m |= inside
            if not m.any():
                continue
            for L in layers:
                sub = owner[ri, L, j0:j1, i0:i1]
                np.putmask(sub, m & (sub != 0) & (sub != code), -1)
                np.putmask(sub, m & (sub == 0), code)

    # board outline
    outl = pcbnew.SHAPE_POLY_SET()
    board.GetBoardPolygonOutlines(outl)
    outs = polyset_outlines(outl)
    if outs:
        ins = fill_poly(outs, g.xs, g.ys)
        for ri in range(nri):
            for L in range(nlay):
                np.putmask(owner[ri, L], ~ins, -1)
        for pts in outs:
            n = len(pts)
            for k in range(n):
                ax, ay = pts[k]
                bx, by = pts[(k + 1) % n]
                mx = max(EDGE)
                i0, j0, i1, j1 = g.win(min(ax, bx), min(ay, by), max(ax, bx), max(ay, by), mx + pitch)
                if i1 <= i0 or j1 <= j0:
                    continue
                D = point_seg_dist(g.xs[None, i0:i1], g.ys[j0:j1, None] if False else g.ys[j0:j1][:, None],
                                   ax, ay, bx, by)
                stamp(j0, j1, i0, i1, D, None, 0.0, -1, range(nlay), thr=EDGE)

    # graphics / text on copper layers: hard obstacles
    gitems = list(board.GetDrawings())
    for fp in board.GetFootprints():
        gitems += list(fp.GraphicalItems())
    for it in gitems:
        try:
            L = it.GetLayer()
        except Exception:
            continue
        if L not in (layer_of[0], layer_of[1]):
            continue
        gl = 0 if L == layer_of[0] else 1
        try:
            bbg = it.GetBoundingBox()
        except Exception:
            continue
        ax, ay = bbg.GetX() / MM, bbg.GetY() / MM
        bx, by = ax + bbg.GetWidth() / MM, ay + bbg.GetHeight() / MM
        if bx - ax > 60 or by - ay > 60:
            continue
        mx = max(RAD)
        i0, j0, i1, j1 = g.win(ax, ay, bx, by, mx + pitch)
        if i1 <= i0 or j1 <= j0:
            continue
        pts = [(ax, ay), (bx, ay), (bx, by), (ax, by)]
        inside = fill_poly([pts], g.xs[i0:i1], g.ys[j0:j1])
        D = seg_dist_field([pts], g.xs[i0:i1], g.ys[j0:j1])
        stamp(j0, j1, i0, i1, D, inside, 0.0, -1, [gl])

    # pads
    pad_cells = {}
    for pad in pads:
        code = pad.GetNetCode() if pad.GetNetname() else 0
        c = code if code in nets else -1
        pls = [L for L in range(nlay) if pad.IsOnLayer(layer_of[L])]
        if not pls:
            continue
        try:
            ps = pad.GetEffectivePolygon(layer_of[pls[0]])
            outs2 = polyset_outlines(ps)
        except Exception:
            outs2 = []
        if not outs2 or len(outs2[0]) < 3:
            bbp = pad.GetBoundingBox()
            a, b = bbp.GetX() / MM, bbp.GetY() / MM
            w, h = bbp.GetWidth() / MM, bbp.GetHeight() / MM
            outs2 = [[(a, b), (a + w, b), (a + w, b + h), (a, b + h)]]
        allp = [pt for o in outs2 for pt in o]
        xmn = min(p_[0] for p_ in allp); xmx = max(p_[0] for p_ in allp)
        ymn = min(p_[1] for p_ in allp); ymx = max(p_[1] for p_ in allp)
        mx = max(RAD)
        i0, j0, i1, j1 = g.win(xmn, ymn, xmx, ymx, mx + pitch)
        if i1 <= i0 or j1 <= j0:
            continue
        xs = g.xs[i0:i1]
        ys = g.ys[j0:j1]
        inside = fill_poly(outs2, xs, ys)
        D = seg_dist_field(outs2, xs, ys)
        stamp(j0, j1, i0, i1, D, inside, 0.0, c, pls)
        # hole: keep via drills away
        ds = pad.GetDrillSize()
        if ds.x > 0:
            hr = max(ds.x, ds.y) / 2 / MM
            hclr = max(rules.get("min_hole_clearance", 0.0) or 0.0, 0.0)
            thr = [hr + hclr + HALF[ri] + margin for ri in range(nri)]
            thr[VIA_RI] = max(thr[VIA_RI], hr + max(0.25, hclr) + via_drill / 2)
            px, py = pad.GetPosition().x / MM, pad.GetPosition().y / MM
            i0h, j0h, i1h, j1h = g.win(px, py, px, py, thr[VIA_RI] + pitch)
            if i1h > i0h and j1h > j0h:
                Dh = np.hypot(g.xs[None, i0h:i1h] - px, g.ys[j0h:j1h][:, None] - py)
                stamp(j0h, j1h, i0h, i1h, Dh, None, 0.0, c, range(nlay), thr=thr)
        if c > 0:
            # cells usable as terminals: strictly inside the pad
            Din = D.copy()
            ok = inside & (Din > 0.1 * pitch)
            jj, ii = np.nonzero(ok)
            cells = []
            for L in pls:
                for a_, b_ in zip(jj, ii):
                    cells.append(L * g.LS + (j0 + int(a_)) * g.W + (i0 + int(b_)))
            if not cells:
                ix = g.gx(pad.GetPosition().x / MM)
                iy = g.gy(pad.GetPosition().y / MM)
                cells = [L * g.LS + iy * g.W + ix for L in pls]
            pad_cells.setdefault(c, []).append(cells)

    # zones: pre-connected copper for their own net (not obstacles)
    zone_cells = {}
    for z in board.Zones():
        code = z.GetNetCode()
        if code not in nets:
            continue
        for L in range(nlay):
            if not z.IsOnLayer(layer_of[L]):
                continue
            try:
                fp = z.GetFilledPolysList(layer_of[L])
            except Exception:
                continue
            outs3 = polyset_outlines(fp)
            if not outs3:
                continue
            allp = [pt for o in outs3 for pt in o]
            xmn = min(p_[0] for p_ in allp); xmx = max(p_[0] for p_ in allp)
            ymn = min(p_[1] for p_ in allp); ymx = max(p_[1] for p_ in allp)
            i0, j0, i1, j1 = g.win(xmn, ymn, xmx, ymx, pitch)
            if i1 <= i0 or j1 <= j0:
                continue
            m = fill_poly(outs3, g.xs[i0:i1], g.ys[j0:j1])
            if not m.any():
                continue
            jj, ii = np.nonzero(m)
            s = zone_cells.setdefault(code, set())
            base_ = L * g.LS
            for a_, b_ in zip(jj.tolist(), ii.tolist()):
                s.add(base_ + (j0 + a_) * g.W + (i0 + b_))

    # border cells blocked (avoids wrap-around in the search)
    for ri in range(nri):
        for L in range(nlay):
            owner[ri, L, 0, :] = -1
            owner[ri, L, -1, :] = -1
            owner[ri, L, :, 0] = -1
            owner[ri, L, :, -1] = -1

    log("obstacles built")
    if os.environ.get("ROUTER_DEBUG_PT"):
        dx, dy = [float(v) for v in os.environ["ROUTER_DEBUG_PT"].split(",")]
        ix, iy = g.gx(dx), g.gy(dy)
        print("DEBUG", dx, dy, ix, iy, [owner[ri, L, iy, ix] for ri in range(nri) for L in range(nlay)],
              "uw", uw, file=sys.stderr)
        print("pads stamped:", len(pad_cells), "nets", sum(len(v) for v in pad_cells.values()),
              "ofwhich", file=sys.stderr)

    # ------------------------------------------------ nets to route
    tasks = []
    for code, name in nets.items():
        groups = pad_cells.get(code, [])
        if len(groups) < 2:
            continue
        comps = []
        zc = zone_cells.get(code)
        zonecomp = None
        for cells in groups:
            if zc is not None and any(c_ in zc for c_ in cells):
                if zonecomp is None:
                    zonecomp = set(zc)
                zonecomp.update(cells)
            else:
                comps.append(set(cells))
        if zonecomp is not None:
            comps.append(zonecomp)
        if len(comps) < 2:
            continue
        pts = []
        for cells in groups:
            c0 = cells[0] % g.LS
            pts.append((c0 % g.W, c0 // g.W))
        xs_ = [p_[0] for p_ in pts]; ys_ = [p_[1] for p_ in pts]
        span = (max(xs_) - min(xs_) + max(ys_) - min(ys_)) * pitch
        tasks.append((span * (1 + 0.3 * len(comps)), code, comps))
    tasks.sort()
    log("nets to route: %d" % len(tasks))

    router = Router(g, owner, nri, VIA_RI, RAD, HALF, nlay)
    router.freeze()
    results = {}
    tracks = []  # (code, layer, (x0,y0), (x1,y1)) grid idx ; vias (code, cell)

    def do_net(code, comps, boost=1.0, full=False):
        return router.route_net(code, comps, ri_of[code], widths[code], boost, full)

    order = [(t[1], t[2]) for t in tasks]
    failed = []
    n_ok = 0
    for k, (code, comps) in enumerate(order):
        if tleft() < 60:
            log("out of time at net %d" % k)
            failed.extend(order[k:])
            break
        ok, segs, vias = do_net(code, comps)
        if ok:
            n_ok += 1
            results[code] = (segs, vias)
        else:
            failed.append((code, comps))
    log("pass1: %d/%d ok, %d failed" % (n_ok, len(order), len(failed)))

    write_board(board, g, results, widths, via_dia, via_drill, layer_of, nets, outp)
    log("written")

    # ------------------------------------------------ retry failures
    rounds = 0
    best = dict(results)
    while failed and tleft() > 50 and rounds < 40:
        rounds += 1
        still = []
        for code, comps in failed:
            if tleft() < 40:
                still.append((code, comps))
                continue
            ok, segs, vias = do_net(code, comps, full=True)
            if ok:
                results[code] = (segs, vias)
            else:
                still.append((code, comps))
        if len(results) > len(best):
            best = dict(results)
            write_board(board, g, best, widths, via_dia, via_drill, layer_of, nets, outp)
        log("round %d: %d routed (%d failed)" % (rounds, len(results), len(still)))
        if not still or tleft() < 80:
            failed = still
            break
        if len(still) == len(failed):
            # rip up neighbours of failing nets and retry
            victims = set()
            for code, comps in still:
                victims |= router.blockers(code, comps, results)
            victims -= {c for c, _ in still}
            if not victims:
                break
            import random
            vl = sorted(victims)
            random.Random(rounds).shuffle(vl)
            vl = vl[:max(1, len(vl) // 2)]
            log("ripping up %d nets" % len(vl))
            for v in vl:
                del results[v]
            router.rebuild(results, widths)
            vlist = [(c, cm) for c, cm in order if c in set(vl)]
            failed = still + vlist
        else:
            failed = still
    if len(results) > len(best):
        best = dict(results)
    results = best
    log("final: %d nets routed" % len(results))
    write_board(board, g, results, widths, via_dia, via_drill, layer_of, nets, outp)
    log("done")


# ---------------------------------------------------------------- router
class Router:
    def __init__(self, g, owner, nri, via_ri, RAD, HALF, nlay):
        self.g = g
        self.owner = owner
        self.nri = nri
        self.via_ri = via_ri
        self.RAD = RAD
        self.HALF = HALF
        self.nlay = nlay
        self.cost_extra = np.zeros((nlay, g.H, g.W), dtype=np.float32)

    # -------------------------------------------------- stamping copper
    def stamp_seg(self, code, L, ax, ay, bx, by, w):
        g = self.g
        mx = max(self.RAD) + w / 2
        i0, j0, i1, j1 = g.win(min(ax, bx), min(ay, by), max(ax, bx), max(ay, by), mx + g.p)
        if i1 <= i0 or j1 <= j0:
            return
        D = point_seg_dist(g.xs[None, i0:i1], g.ys[j0:j1][:, None], ax, ay, bx, by)
        for ri in range(self.nri):
            m = D < (self.RAD[ri] + w / 2)
            if not m.any():
                continue
            lay = range(self.nlay) if L is None else [L]
            for LL in lay:
                sub = self.owner[ri, LL, j0:j1, i0:i1]
                np.putmask(sub, m & (sub != 0) & (sub != code), -1)
                np.putmask(sub, m & (sub == 0), code)

    def apply(self, code, segs, vias, w, viar):
        g = self.g
        for (L, a, b) in segs:
            self.stamp_seg(code, L, g.xmm(a % g.W), g.ymm(a // g.W), g.xmm(b % g.W), g.ymm(b // g.W), w)
        for c in vias:
            self.stamp_seg(code, None, g.xmm(c % g.W), g.ymm(c // g.W), g.xmm(c % g.W), g.ymm(c // g.W), 2 * viar)

    def freeze(self):
        self.static = self.owner.copy()

    def rebuild(self, results, widths):
        np.copyto(self.owner, self.static)
        for code, (segs, vias) in results.items():
            self.apply(code, segs, vias, widths[code], self.HALF[self.via_ri])

    def blockers(self, code, comps, results):
        """nets whose copper lies in the region between the components"""
        g = self.g
        pts = []
        for s in comps:
            c = next(iter(s)) % g.LS
            pts.append((c % g.W, c // g.W))
        xs_ = [p[0] for p in pts]; ys_ = [p[1] for p in pts]
        pad = int(6 / g.p)
        i0 = max(0, min(xs_) - pad); i1 = min(g.W, max(xs_) + pad)
        j0 = max(0, min(ys_) - pad); j1 = min(g.H, max(ys_) + pad)
        sub = self.owner[0, :, j0:j1, i0:i1]
        vals = set(np.unique(sub).tolist())
        return {v for v in vals if v > 0 and v in results}

    # -------------------------------------------------- A*
    def route_net(self, code, comps, ri, w, boost=1.0, full=False):
        g = self.g
        segs = []
        vias = []
        comps = [set(c) for c in comps]
        conn = max(comps, key=len)
        comps.remove(conn)
        conn = set(conn)
        while comps:
            r = self.search(code, ri, conn, comps, full)
            if r is None:
                return False, segs, vias
            path, hit = r
            if os.environ.get("ROUTER_CHECK"):
                g2 = self.g
                for c in path:
                    L = c // g2.LS; rr = c % g2.LS
                    o = self.owner[ri, L, rr // g2.W, rr % g2.W]
                    if o != 0 and o != code:
                        print("BADCELL net", code, "cell owner", o, g2.xmm(rr % g2.W), g2.ymm(rr // g2.W),
                              file=sys.stderr)
                        break
            # which component
            idx = None
            for i, c in enumerate(comps):
                if hit in c:
                    idx = i
                    break
            conn |= comps.pop(idx)
            conn.update(path)
            s, v = self.path_to_geom(path)
            segs += s
            vias += v
        viar = self.RAD[self.via_ri] - self.RAD[ri] + self.HALF[ri]
        self.apply(code, segs, vias, w, self.HALF[self.via_ri])
        return True, segs, vias

    def path_to_geom(self, path):
        g = self.g
        segs = []
        vias = []
        run = [path[0]]
        for i in range(1, len(path)):
            a, b = path[i - 1], path[i]
            if (a % g.LS) == (b % g.LS):
                # via
                if len(run) > 1:
                    segs += self.compress(run)
                vias.append(a % g.LS)
                run = [b]
            else:
                run.append(b)
        if len(run) > 1:
            segs += self.compress(run)
        return segs, vias

    def compress(self, run):
        g = self.g
        L = run[0] // g.LS
        out = []
        cells = [c % g.LS for c in run]
        s = 0
        for i in range(1, len(cells)):
            if i + 1 < len(cells):
                d1 = (cells[i] - cells[i - 1])
                d2 = (cells[i + 1] - cells[i])
                if d1 == d2:
                    continue
            out.append((L, cells[s], cells[i]))
            s = i
        return out

    def search(self, code, ri, sources, comps, full):
        g = self.g
        W, H, LS = g.W, g.H, g.LS
        # bounding box
        pts = []
        for s in (list(sources)[:4000],):
            pass
        allc = []
        for c in comps:
            allc.append(next(iter(c)))
        tx = [(c % LS) % W for c in allc]
        ty = [(c % LS) // W for c in allc]
        sxs = [(c % LS) % W for c in list(sources)[:400]]
        sys_ = [(c % LS) // W for c in list(sources)[:400]]
        if full:
            i0, j0, i1, j1 = 0, 0, W, H
        else:
            pad = max(int(8.0 / g.p), 20)
            i0 = max(0, min(tx + sxs) - pad); i1 = min(W, max(tx + sxs) + pad + 1)
            j0 = max(0, min(ty + sys_) - pad); j1 = min(H, max(ty + sys_) + pad + 1)
        w = i1 - i0
        h = j1 - j0
        ls = w * h
        own = self.owner[ri, :, j0:j1, i0:i1]
        free = ((own == 0) | (own == code))
        free[:, 0, :] = False
        free[:, -1, :] = False
        free[:, :, 0] = False
        free[:, :, -1] = False
        ownv = self.owner[self.via_ri, :, j0:j1, i0:i1]
        viaok = (((ownv == 0) | (ownv == code)).all(axis=0) & free.all(axis=0))
        freeb = free.reshape(-1).tobytes()
        viab = viaok.reshape(-1).tobytes()

        def loc(c):
            L = c // LS
            r = c % LS
            y = r // W - j0
            x = r % W - i0
            if x < 0 or y < 0 or x >= w or y >= h:
                return -1
            return L * ls + y * w + x

        def glob(c):
            L = c // ls
            r = c % ls
            return L * LS + (r // w + j0) * W + (r % w + i0)

        tgt = {}
        for ci, cs in enumerate(comps):
            for c in cs:
                l = loc(c)
                if l >= 0:
                    tgt[l] = ci
        if not tgt:
            return None
        srcs = [loc(c) for c in sources]
        srcs = [s for s in srcs if s >= 0 and freeb[s]]
        if not srcs:
            return None

        # heuristic field: octile distance to the nearest target anchor
        anchors = []
        for ci, cs in enumerate(comps):
            lst = [loc(c) for c in cs]
            lst = [x_ for x_ in lst if x_ >= 0]
            if lst:
                m = lst[len(lst) // 2]
                anchors.append(((m % ls) % w, (m % ls) // w))
        if not anchors:
            return None
        XX = np.arange(w, dtype=np.float32)[None, :]
        YY = np.arange(h, dtype=np.float32)[:, None]
        Hf = None
        for (ax, ay) in anchors[:6]:
            dx = np.abs(XX - ax)
            dy = np.abs(YY - ay)
            d = np.maximum(dx, dy) + 0.4142 * np.minimum(dx, dy)
            Hf = d if Hf is None else np.minimum(Hf, d)
        Hl = (Hf * (g.p * 0.999)).reshape(-1).tolist()

        p = g.p
        dia = p * 1.41421356
        VIA_C = 2.0 + 2 * p
        moves = ((-w - 1, dia), (-w, p), (-w + 1, dia), (-1, p), (1, p),
                 (w - 1, dia), (w, p), (w + 1, dia))
        gs = {}
        prev = {}
        heap = []
        push = heapq.heappush
        pop = heapq.heappop
        for s in srcs:
            if s in gs:
                continue
            gs[s] = 0.0
            prev[s] = -1
            push(heap, (Hl[s % ls], s))
        closed = set()
        limit = 900000 if full else 300000
        cnt = 0
        found = None
        while heap:
            f, i = pop(heap)
            if i in closed:
                continue
            closed.add(i)
            if i in tgt:
                found = i
                break
            cnt += 1
            if cnt > limit:
                break
            gi = gs[i]
            base = i - (i // ls) * ls
            for d, c in moves:
                ni = i + d
                if not freeb[ni] or ni in closed:
                    continue
                ng = gi + c
                o = gs.get(ni)
                if o is None or ng < o - 1e-9:
                    gs[ni] = ng
                    prev[ni] = i
                    push(heap, (ng + Hl[ni % ls], ni))
            # via
            ni = i + ls if i < ls else i - ls
            if viab[base] and freeb[ni] and ni not in closed:
                ng = gi + VIA_C
                o = gs.get(ni)
                if o is None or ng < o - 1e-9:
                    gs[ni] = ng
                    prev[ni] = i
                    push(heap, (ng + Hl[ni % ls], ni))
        if found is None:
            return None
        path = []
        i = found
        while i != -1:
            path.append(glob(i))
            i = prev[i]
        path.reverse()
        return path, glob(found)


# ---------------------------------------------------------------- output
def write_board(board, g, results, widths, via_dia, via_drill, layer_of, nets, outp):
    for t in list(board.GetTracks()):
        board.Remove(t)
    for code, (segs, vias) in results.items():
        net = board.FindNet(code)
        w = int(round(widths[code] * MM))
        for (L, a, b) in segs:
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(pcbnew.VECTOR2I(int(round(g.xmm(a % g.W) * MM)), int(round(g.ymm(a // g.W) * MM))))
            t.SetEnd(pcbnew.VECTOR2I(int(round(g.xmm(b % g.W) * MM)), int(round(g.ymm(b // g.W) * MM))))
            t.SetWidth(w)
            t.SetLayer(layer_of[L])
            t.SetNet(net)
            board.Add(t)
        for c in vias:
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(pcbnew.VECTOR2I(int(round(g.xmm(c % g.W) * MM)), int(round(g.ymm(c // g.W) * MM))))
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetLayerPair(layer_of[0], layer_of[1])
            v.SetDrill(int(round(via_drill * MM)))
            v.SetWidth(pcbnew.F_Cu, int(round(via_dia * MM)))
            v.SetNet(net)
            board.Add(v)
    pcbnew.SaveBoard(outp, board)


if __name__ == "__main__":
    main()
