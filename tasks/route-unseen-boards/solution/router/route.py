#!/usr/bin/env python3
"""A general router for two-layer KiCad boards.

    python3 route.py <board.kicad_pcb> <routed.kicad_pcb>

The board's rules are read from the project file (.kicad_pro) and the rules file (.kicad_dru) that
lie next to it. Nothing about any particular board is stored here: every number comes from the
board that is handed in.

How it works
  1. The board is laid over a fine grid (0.05 or 0.1 mm, chosen from the board's narrowest track
     and clearance). For every kind of track (width and clearance) a map says which grid points
     a track of that kind may be centred on, measured exactly against the pads, holes, board
     edge and keep-out areas.
  2. A pad that is too narrow or too close to its neighbours for any grid point gets a short
     straight "escape" track, centred exactly on the pad, out to the first grid point that is legal.
  3. Every net without a pour is routed with a shortest-path search over the grid (both layers,
     vias allowed). Nets may overlap at first. Being in another net's way is made more expensive
     round after round, most of all where nets have collided before, and the nets involved are
     routed again until no two are in each other's way (negotiated congestion, "PathFinder").
  4. A net that is still left out is laid across the others where that costs least, and the
     nets it crosses are routed again around it.
  5. Differential pairs with a length rule are evened out: the shorter track is sent through a
     point off its way, chosen so that the detour adds the missing length.
  6. Nets with a copper pour come last. KiCad itself fills the pours; the filled copper is read
     back; every group of pads the pour does not reach is joined to the rest with a via or a
     track. Where other nets leave no room for that, they are routed again around the join.
  7. KiCad's own design-rule check is run on the result. Anything it reports is repaired by
     routing the net again around the spot or, failing that, by taking the offending copper out.

Settings, all optional and all for testing: ROUTE_SECONDS (time allowed, default 480),
ROUTE_DEBUG=1 (more log lines), ROUTE_PICTURE=<folder> (PNG pictures of the copper while it
works), ROUTE_SHIFT=<mm> (moves the grid off the pads, to exercise the escape tracks).
"""
import sys, os, re, json, time, math, subprocess, tempfile, shutil, fnmatch

import numpy as np

T0 = time.time()
BUDGET = float(os.environ.get("ROUTE_SECONDS", "480"))   # wall-clock seconds the run may take
MM = 1e6
SAFE = 0.005     # mm added to clearances against copper that is not on the grid
EPS = 1e-6
BIG = 1 << 29
STEP, DIAG = 2, 3


def log(*a):
    print(f"[{time.time() - T0:6.1f}]", *a, flush=True)


def left():
    return BUDGET - (time.time() - T0)


def up(v, q=0.001):
    """Round a limit up to the next micrometre: 0.5499999999 -> 0.55."""
    return round(math.ceil(v / q - 1e-6) * q, 6)


ESCAPES = {"dblquote": '"', "quote": "'", "lt": "<", "gt": ">", "backslash": "\\", "slash": "/", "bar": "|",
           "comma": ",", "colon": ":", "space": " ", "dollar": "$", "tab": "\t", "return": "\n", "brace": "{"}


def unescape(name):
    return re.sub(r"\{([a-z]+)\}", lambda m: ESCAPES.get(m.group(1), m.group(0)), name)


# ------------------------------------------------------------------------------------------ rules
def load_rules(pcb_path, names):
    base = pcb_path[:-len(".kicad_pcb")]
    try:
        with open(base + ".kicad_pro") as f:
            pro = json.load(f)
    except Exception:
        pro = {}
    ns = pro.get("net_settings") or {}
    classes = {c.get("name"): c for c in (ns.get("classes") or [])}
    default = classes.get("Default", {})
    patterns = ns.get("netclass_patterns") or []
    assigns = ns.get("netclass_assignments") or {}
    ds = (pro.get("board") or {}).get("design_settings") or {}
    rules = ds.get("rules") or {}

    def g(k, d):
        v = rules.get(k)
        return d if v is None else v

    min_w = up(g("min_track_width", 0.2) or 0.2)
    min_clear = g("min_clearance", 0.0) or 0.0
    min_via = up(g("min_via_diameter", 0.4) or 0.4)
    min_drill = up(g("min_through_hole_diameter", 0.3) or 0.3)
    min_ann = up(g("min_via_annular_width", 0.05) or 0.05)
    net_minw, skews = {}, {}
    try:
        text = open(base + ".kicad_dru").read()
    except Exception:
        text = ""
    for m in re.finditer(r'\(rule\s+"[^"]*"(.*?)\n\s*\(constraint\s+(\w+)\s*\(([^)]*)\)\)', text, re.S):
        cond, kind, args = m.group(1), m.group(2), m.group(3)
        vm = re.search(r'(min|max)\s+([\d.]+)\s*mm', args)
        if not vm:
            continue
        val = float(vm.group(2))
        if kind == "track_width" and vm.group(1) == "min":
            for n in re.findall(r"NetName\s*==\s*'([^']*)'", cond):
                net_minw[n] = max(net_minw.get(n, 0), val)
        elif kind == "skew":
            for p in re.findall(r"inDiffPair\('([^']*)'\)", cond):
                skews[p] = val

    def netclass(name):
        a = assigns.get(name)
        if a:
            a = a[0] if isinstance(a, list) else a
            if a in classes:
                return classes[a]
        for p in patterns:
            pat = p.get("pattern", "")
            hit = fnmatch.fnmatchcase(name, pat)
            if not hit:
                try:
                    hit = bool(re.fullmatch(pat, name))
                except re.error:
                    hit = False
            if hit and p.get("netclass") in classes:
                return classes[p["netclass"]]
        return default

    def get(c, k, d):
        v = c.get(k)
        if v is None:
            v = default.get(k)
        return d if v is None else v

    # one via size for the board: the smallest of the sizes the board offers that its rules allow
    cands = [(v.get("diameter", 0), v.get("drill", 0)) for v in (ds.get("via_dimensions") or [])]
    cands += [(c.get("via_diameter", 0), c.get("via_drill", 0)) for c in classes.values()]
    legal = [(d, dr) for d, dr in cands if d and dr and d >= min_via - 1e-9 and dr >= min_drill - 1e-9
             and (d - dr) / 2 >= min_ann - 1e-9]
    if legal:
        vd, vdr = min(legal)
    else:
        vdr = min_drill
        vd = max(min_via, vdr + 2 * max(min_ann, 0.125))
    vd, vdr = up(vd), up(vdr)
    info = {}
    for name in names:
        c = netclass(name)
        clr = max(get(c, "clearance", 0.2), min_clear)
        w = up(max(net_minw.get(name, 0), net_minw.get(unescape(name), 0), min_w))
        info[name] = (w, round(clr, 4))
    return dict(net=info, vd=vd, vdr=vdr, skews=skews, default_clr=max(get(default, "clearance", 0.2), min_clear),
                edge=g("min_copper_edge_clearance", 0.0) or 0.0, hole_clr=g("min_hole_clearance", 0.0) or 0.0,
                h2h=g("min_hole_to_hole", 0.25) or 0.25)


# --------------------------------------------------------------------------------------- geometry
def rings_of(sps):
    rings = []
    for i in range(sps.OutlineCount()):
        o = sps.Outline(i)
        pts = np.array([[o.CPoint(j).x / MM, o.CPoint(j).y / MM] for j in range(o.PointCount())])
        if len(pts) >= 3:
            rings.append(pts)
        for h in range(sps.HoleCount(i)):
            hh = sps.Hole(i, h)
            pts = np.array([[hh.CPoint(j).x / MM, hh.CPoint(j).y / MM] for j in range(hh.PointCount())])
            if len(pts) >= 3:
                rings.append(pts)
    return rings


def poly_dist(X, Y, rings):
    """Distance from points to a polygon given as rings (even-odd); 0 inside."""
    X, Y = np.broadcast_arrays(X, Y)
    shp = X.shape
    x = X.ravel().astype(np.float64)
    y = Y.ravel().astype(np.float64)
    A = np.vstack(rings)
    Bp = np.vstack([np.roll(r, -1, axis=0) for r in rings])
    best = np.full(x.shape, np.inf)
    ins = np.zeros(x.shape, bool)
    ch = max(1, int(1.2e6 // max(1, x.size)))
    for s in range(0, len(A), ch):
        a, b = A[s:s + ch], Bp[s:s + ch]
        ax, ay = a[:, 0][:, None], a[:, 1][:, None]
        dx, dy = (b[:, 0] - a[:, 0])[:, None], (b[:, 1] - a[:, 1])[:, None]
        L = dx * dx + dy * dy
        L[L < 1e-18] = 1e-18
        px, py = x[None, :] - ax, y[None, :] - ay
        t = np.clip((px * dx + py * dy) / L, 0, 1)
        d2 = (px - t * dx) ** 2 + (py - t * dy) ** 2
        best = np.minimum(best, d2.min(axis=0))
        c = (ay > y[None, :]) != ((ay + dy) > y[None, :])
        xi = ax + (y[None, :] - ay) * dx / np.where(dy == 0, 1e-30, dy)
        ins ^= ((c & (x[None, :] < xi)).sum(axis=0) & 1).astype(bool)
    d = np.sqrt(best)
    d[ins] = 0.0
    return d.reshape(shp)


def seg_dist(X, Y, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L = dx * dx + dy * dy
    if L < 1e-18:
        return np.hypot(X - x1, Y - y1)
    t = np.clip(((X - x1) * dx + (Y - y1) * dy) / L, 0, 1)
    return np.hypot(X - x1 - t * dx, Y - y1 - t * dy)


def raster(rings, x0, y0, p, nx, ny):
    """Which grid points (x0 + i p, y0 + j p) lie inside the polygon (even-odd rule)."""
    delta = np.zeros((ny, nx + 1), np.int32)
    for r in rings:
        x1, y1 = r[:, 0], r[:, 1]
        x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
        ja = np.ceil((np.minimum(y1, y2) - y0) / p - 1e-9).astype(np.int64)
        jb = np.ceil((np.maximum(y1, y2) - y0) / p - 1e-9).astype(np.int64)
        cnt = np.maximum(jb - ja, 0)
        tot = int(cnt.sum())
        if tot == 0:
            continue
        e = np.repeat(np.arange(len(cnt)), cnt)
        j = ja[e] + (np.arange(tot) - np.repeat(np.cumsum(cnt) - cnt, cnt))
        yj = y0 + j * p
        den = y2[e] - y1[e]
        den[den == 0] = 1e-30
        xi = x1[e] + (yj - y1[e]) * (x2[e] - x1[e]) / den
        c = np.ceil((xi - x0) / p - 1e-9).astype(np.int64)
        ok = (j >= 0) & (j < ny)
        np.add.at(delta, (j[ok], np.clip(c[ok], 0, nx)), 1)
    return (np.cumsum(delta, axis=1)[:, :nx] & 1).astype(bool)


WORKER = r'''
import json, sys, pcbnew
base, items_path, out_path, dump_path = sys.argv[1:5]
b = pcbnew.LoadBoard(base)
it = json.load(open(items_path))
V = pcbnew.VECTOR2I
for (li, x1, y1, x2, y2, w, net) in it["segs"]:
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(V(int(x1), int(y1))); t.SetEnd(V(int(x2), int(y2))); t.SetWidth(int(w))
    t.SetLayer(pcbnew.F_Cu if li == 0 else pcbnew.B_Cu); t.SetNet(b.FindNet(int(net))); b.Add(t)
for (x, y, d, dr, net) in it["vias"]:
    v = pcbnew.PCB_VIA(b)
    v.SetPosition(V(int(x), int(y))); v.SetViaType(pcbnew.VIATYPE_THROUGH); v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetDrill(int(dr)); v.SetWidth(pcbnew.F_Cu, int(d)); v.SetNet(b.FindNet(int(net))); b.Add(v)
if dump_path != "-":
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    isl = []
    for z in b.Zones():
        if z.GetIsRuleArea():
            continue
        for layer in z.GetLayerSet().CuStack():
            li = 0 if layer == pcbnew.F_Cu else (1 if layer == pcbnew.B_Cu else -1)
            if li < 0:
                continue
            sps = z.GetFilledPolysList(layer)
            for i in range(sps.OutlineCount()):
                o = sps.Outline(i)
                rings = [[(o.CPoint(j).x, o.CPoint(j).y) for j in range(o.PointCount())]]
                for h in range(sps.HoleCount(i)):
                    hh = sps.Hole(i, h)
                    rings.append([(hh.CPoint(j).x, hh.CPoint(j).y) for j in range(hh.PointCount())])
                isl.append({"net": z.GetNetCode(), "li": li, "rings": rings})
    json.dump(isl, open(dump_path, "w"))
pcbnew.SaveBoard(out_path, b)
print("WORKER_OK")
'''

# The kinds of violation that count against a routed board (the errors about copper a router places).
COUNTED = {
    "clearance", "shorting_items", "tracks_crossing", "track_width", "via_diameter", "annular_width",
    "drill_out_of_range", "hole_clearance", "hole_to_hole", "holes_co_located", "copper_edge_clearance",
    "items_not_allowed", "microvia_drill_out_of_range", "padstack", "track_angle", "track_segment_length",
    "via_dangling", "creepage", "connection_width", "copper_sliver", "solder_mask_bridge",
    "skew_out_of_range", "length_out_of_range",
}


class Router:
    # ------------------------------------------------------------------------------------ set-up
    def __init__(self, pcb_path, out_path):
        import pcbnew
        self.pcbnew = pcbnew
        self.src, self.out = pcb_path, out_path
        self.tmp = tempfile.mkdtemp(prefix="router-")
        b = pcbnew.LoadBoard(pcb_path)
        self.board = b
        self.LAY = [pcbnew.F_Cu, pcbnew.B_Cu]
        self.netname = {net.GetNetCode(): str(name) for name, net in b.GetNetsByName().items()}
        self.plain = {unescape(v): k for k, v in self.netname.items()}
        self.R = load_rules(pcb_path, list(self.netname.values()))
        self.vd, self.vdr = self.R["vd"], self.R["vdr"]
        try:
            self.thick = b.GetDesignSettings().GetBoardThickness() / MM
        except Exception:
            self.thick = 1.6
        self.collect()
        self.make_grid()
        self.paint_static()
        self.make_access()
        self.items = {n: [] for n in self.nets}     # net -> painted items
        self.paths = {n: [] for n in self.nets}     # net -> list of node lists [(li, gx, gy), ...]
        self.stubs = {n: [] for n in self.nets}     # net -> list of (pad, access index) whose escape is in use
        self.failed = {n: list(self.nets[n]) for n in self.nets}
        self.block = {}                             # net -> [(li, gx, gy, radius in cells)] the net must avoid
        self.tries = {n: 0 for n in self.nets}
        self.seed = {}
        self.last_moved = {}
        self.npic = 0
        self.saved = False

    def ninfo(self, n):
        return self.R["net"].get(self.netname.get(n, ""), (up(0.25), self.R["default_clr"]))

    def collect(self):
        pcbnew, b = self.pcbnew, self.board
        self.pads, self.holes = [], []
        self.maskobs = []
        for fp in b.GetFootprints():
            for pad in fp.Pads():
                pos = pad.GetPosition()
                ent = dict(net=pad.GetNetCode(), rings={}, c=(pos.x / MM, pos.y / MM), ref=fp.GetReference() + "." + str(pad.GetNumber()))
                for li, L in enumerate(self.LAY):
                    if pad.IsOnLayer(L) and (not hasattr(pad, "FlashLayer") or pad.FlashLayer(L)):
                        try:
                            sps = pad.GetEffectivePolygon(L, pcbnew.ERROR_OUTSIDE)
                        except TypeError:
                            sps = pad.GetEffectivePolygon(pcbnew.ERROR_OUTSIDE)
                        r = rings_of(sps)
                        if r:
                            ent["rings"][li] = r
                for Lm, li in ((pcbnew.F_Mask, 0), (pcbnew.B_Mask, 1)):
                    if pad.IsOnLayer(Lm) and not pad.IsOnLayer(self.LAY[li]):
                        try:
                            r = rings_of(pad.GetEffectivePolygon(Lm, pcbnew.ERROR_OUTSIDE))
                            if r:
                                self.maskobs.append((r, li))
                        except Exception:
                            pass
                if pad.HasHole():
                    dsz = pad.GetDrillSize()
                    self.holes.append((pos.x / MM, pos.y / MM, max(dsz.x, dsz.y) / MM / 2, pad.GetNetCode(),
                                       pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH))
                self.pads.append(ent)
        nets = {}
        for i, pd in enumerate(self.pads):
            if pd["net"] > 0 and pd["rings"]:
                nets.setdefault(pd["net"], []).append(i)
        self.nets = {n: v for n, v in nets.items() if len(v) >= 2}
        self.zone_nets = sorted({z.GetNetCode() for z in b.Zones() if not z.GetIsRuleArea()} & set(self.nets))
        self.sig_nets = [n for n in self.nets if n not in self.zone_nets]
        self.w = {n: self.ninfo(n)[0] for n in self.nets}
        self.clr = {n: self.ninfo(n)[1] for n in self.nets}
        log(f"{len(self.pads)} pads, {len(self.sig_nets)} nets to route, {len(self.zone_nets)} nets with a pour; "
            f"via {self.vd}/{self.vdr}")

    def make_grid(self):
        b = self.board
        bb = b.GetBoardEdgesBoundingBox()
        x0, y0, w, h = bb.GetX() / MM, bb.GetY() / MM, bb.GetWidth() / MM, bb.GetHeight() / MM
        need = min(self.w[n] + self.clr[n] for n in self.nets) if self.nets else 0.5
        p = 0.05 if need < 0.42 else 0.1
        while (w / p) * (h / p) > 4.6e6:
            p *= 2
        self.p = p
        offs = []
        cs = np.array([pd["c"] for pd in self.pads]) if self.pads else np.zeros((1, 2))
        q = int(round(p / 0.001))
        for ax in range(2):
            m = np.round(cs[:, ax] / 0.001).astype(np.int64) % q
            vals, cnts = np.unique(m, return_counts=True)
            offs.append(vals[int(np.argmax(cnts))] * 0.001 + float(os.environ.get("ROUTE_SHIFT", "0")))   # the shift is for testing escapes
        self.x0 = round(math.floor((x0 - 0.6 - offs[0]) / p) * p + offs[0], 6)
        self.y0 = round(math.floor((y0 - 0.6 - offs[1]) / p) * p + offs[1], 6)
        self.nx = int((w + 1.2) / p) + 3
        self.ny = int((h + 1.2) / p) + 3
        keys = []
        self.tk, self.vk = {}, {}
        for n in self.nets:
            self.tk[n] = (round(self.w[n] / 2, 4), self.clr[n])
            self.vk[n] = (round(self.vd / 2, 4), self.clr[n])
            keys += [self.tk[n], self.vk[n]]
        self.keys = list(dict.fromkeys(keys))
        shape = (self.ny, self.nx)
        self.own = {k: [np.zeros(shape, np.int16), np.zeros(shape, np.int16)] for k in self.keys}
        self.dyn = {k: [np.zeros(shape, np.int16), np.zeros(shape, np.int16)] for k in self.keys}
        self.hist = [np.zeros(shape, np.int32), np.zeros(shape, np.int32)]
        self.holeb = np.zeros(shape, bool)
        self.via_cost = max(12, int(round(1.2 / p)) * STEP)
        log(f"board {w:.1f} x {h:.1f} mm, grid {p} mm, {self.nx} x {self.ny}, classes {self.keys}")

    def win(self, xa, ya, xb, yb):
        p = self.p
        return (max(0, int(math.floor((xa - self.x0) / p))), max(0, int(math.floor((ya - self.y0) / p))),
                min(self.nx, int(math.ceil((xb - self.x0) / p)) + 1), min(self.ny, int(math.ceil((yb - self.y0) / p)) + 1))

    def coords(self, i0, j0, i1, j1):
        return (self.x0 + np.arange(i0, i1) * self.p)[None, :], (self.y0 + np.arange(j0, j1) * self.p)[:, None]

    def xy(self, gx, gy):
        return (round(self.x0 + gx * self.p, 6), round(self.y0 + gy * self.p, 6))

    def spaint(self, fn, bbox, net, clr, layers, fixed=False, keep=True):
        """A fixed obstacle: mark every grid point that is too close to it, for every kind of track."""
        if keep:
            self.sitems.append((fn, bbox, net, clr, tuple(layers), fixed))
        rm = max(k[0] + (clr if fixed else max(k[1], clr)) for k in self.keys) + SAFE
        i0, j0, i1, j1 = self.win(bbox[0] - rm, bbox[1] - rm, bbox[2] + rm, bbox[3] + rm)
        if i1 <= i0 or j1 <= j0:
            return
        X, Y = self.coords(i0, j0, i1, j1)
        D = fn(X, Y)
        code = net if net > 0 else -1
        for k in self.keys:
            m = D < k[0] + (clr if fixed else max(k[1], clr)) + SAFE
            if not m.any():
                continue
            for li in layers:
                a = self.own[k][li][j0:j1, i0:i1]
                if code < 0:
                    a[m] = -1
                else:
                    cur = a[m]
                    a[m] = np.where((cur == 0) | (cur == code), code, -1)

    def paint_static(self):
        pcbnew, b = self.pcbnew, self.board
        self.sitems = []
        dclr = self.R["default_clr"]
        for pd in self.pads:
            for li, rings in pd["rings"].items():
                allp = np.vstack(rings)
                bbox = (allp[:, 0].min(), allp[:, 1].min(), allp[:, 0].max(), allp[:, 1].max())
                pd.setdefault("bbox", {})[li] = bbox
                clr = self.ninfo(pd["net"])[1] if pd["net"] > 0 else dclr
                self.spaint(lambda X, Y, r=rings: poly_dist(X, Y, r), bbox, pd["net"], clr, [li])
        for rings, li in self.maskobs:
            allp = np.vstack(rings)
            bbox = (allp[:, 0].min(), allp[:, 1].min(), allp[:, 0].max(), allp[:, 1].max())
            self.spaint(lambda X, Y, r=rings: poly_dist(X, Y, r), bbox, 0, 0.01, [li], fixed=True)
        hc = self.R["hole_clr"]
        for (x, y, r, net, npth) in self.holes:
            rad = r + self.vdr / 2 + self.R["h2h"] + 0.01
            i0, j0, i1, j1 = self.win(x - rad, y - rad, x + rad, y + rad)
            X, Y = self.coords(i0, j0, i1, j1)
            self.holeb[j0:j1, i0:i1] |= np.hypot(X - x, Y - y) < rad
            fn = lambda X, Y, x=x, y=y, r=r: np.maximum(np.hypot(X - x, Y - y) - r, 0)
            if npth:
                self.spaint(fn, (x - r, y - r, x + r, y + r), 0, max(hc, dclr), [0, 1], fixed=True)
            elif hc > 0:
                self.spaint(fn, (x - r, y - r, x + r, y + r), net, hc, [0, 1], fixed=True)
        # the board outline: nothing outside it, and nothing too close to it
        sps = pcbnew.SHAPE_POLY_SET()
        b.GetBoardPolygonOutlines(sps)
        outline = rings_of(sps)
        half = 0.0
        for d in b.GetDrawings():
            try:
                if d.GetLayer() == pcbnew.Edge_Cuts:
                    half = max(half, d.GetWidth() / MM / 2)
            except Exception:
                pass
        if outline:
            inside = raster(outline, self.x0, self.y0, self.p, self.nx, self.ny)
            for k in self.keys:
                for li in range(2):
                    self.own[k][li][~inside] = -1
            ec = max(self.R["edge"] + half, 0.15)
            for ring in outline:
                for a, bq in zip(ring, np.roll(ring, -1, axis=0)):
                    fn = lambda X, Y, a=a, bq=bq: seg_dist(X, Y, a[0], a[1], bq[0], bq[1])
                    self.spaint(fn, (min(a[0], bq[0]), min(a[1], bq[1]), max(a[0], bq[0]), max(a[1], bq[1])), 0, ec, [0, 1], fixed=True)
        # drawings and text on copper, and openings in the solder mask
        gitems = list(b.GetDrawings())
        for fp in b.GetFootprints():
            gitems += list(fp.GraphicalItems())
            gitems += [fp.Reference(), fp.Value()]
        maskmap = ((pcbnew.F_Mask, 0, False), (pcbnew.B_Mask, 1, False), (pcbnew.F_Cu, 0, True), (pcbnew.B_Cu, 1, True))
        ng = 0
        for gi in gitems:
            try:
                if gi.GetClass() == "PCB_FIELD" and not gi.IsVisible():
                    continue
                for Ls, li, copper in maskmap:
                    if not gi.IsOnLayer(Ls):
                        continue
                    sps = pcbnew.SHAPE_POLY_SET()
                    gi.TransformShapeToPolygon(sps, Ls, 0, int(0.005 * MM), pcbnew.ERROR_OUTSIDE)
                    rings = rings_of(sps)
                    if not rings:
                        continue
                    allp = np.vstack(rings)
                    bbox = (allp[:, 0].min(), allp[:, 1].min(), allp[:, 0].max(), allp[:, 1].max())
                    nc = gi.GetNetCode() if (copper and hasattr(gi, "GetNetCode")) else 0
                    self.spaint(lambda X, Y, r=rings: poly_dist(X, Y, r), bbox, nc, dclr if copper else 0.0, [li], fixed=not copper)
                    ng += 1
            except Exception:
                pass
        # keep-out areas
        for z in b.Zones():
            if not z.GetIsRuleArea():
                continue
            try:
                rings = rings_of(z.Outline())
                if not rings:
                    continue
                lays = [li for li, L in enumerate(self.LAY) if z.IsOnLayer(L)]
                notrk, novia = z.GetDoNotAllowTracks(), z.GetDoNotAllowVias()
                if not (notrk or novia) or not lays:
                    continue
                x0, y0 = np.vstack(rings).min(axis=0)
                x1, y1 = np.vstack(rings).max(axis=0)
                i0, j0, i1, j1 = self.win(x0, y0, x1, y1)
                m = raster(rings, self.x0 + i0 * self.p, self.y0 + j0 * self.p, self.p, i1 - i0, j1 - j0)
                tkeys, vkeys = set(self.tk.values()), set(self.vk.values())
                for k in self.keys:
                    if (notrk and k in tkeys) or (novia and k in vkeys):
                        for li in (lays if k in tkeys else [0, 1]):
                            self.own[k][li][j0:j1, i0:i1][m] = -1
            except Exception:
                pass
        log(f"fixed obstacles painted ({len(self.sitems)} shapes, {ng} drawings)")

    # ------------------------------------------------------------------------- pad access points
    def make_access(self):
        """For every pad of a net: the grid points a track of that net may end on. Where none lies
        inside the pad, a short straight escape from the pad's centre to the nearest legal point."""
        self.acc = {}       # pad -> list of (li, gx, gy, escape or None); escape = [(x1, y1, x2, y2), ...] in mm
        self.accnode = {}   # (li, gx, gy) of an escape's end -> (pad, index into acc[pad])
        nstub = nnone = 0
        for n, pids in self.nets.items():
            tk = self.tk[n]
            for pi in pids:
                pd = self.pads[pi]
                out = []
                for li, rings in pd["rings"].items():
                    bb = pd["bbox"][li]
                    i0, j0, i1, j1 = self.win(bb[0], bb[1], bb[2], bb[3])
                    if i1 <= i0 or j1 <= j0:
                        continue
                    X, Y = self.coords(i0, j0, i1, j1)
                    inside = poly_dist(X, Y, rings) <= 0
                    o = self.own[tk][li][j0:j1, i0:i1]
                    jj, ii = np.nonzero(inside & ((o == 0) | (o == n)))
                    for j, i in zip(jj.tolist(), ii.tolist()):
                        out.append((li, i + i0, j + j0, None))
                if not out:
                    for li, rings in pd["rings"].items():
                        out += self.escapes(n, pi, li, rings)
                    nstub += 1 if out else 0
                    nnone += 0 if out else 1
                    for ai, a in enumerate(out):
                        self.accnode[(a[0], a[1], a[2])] = (pi, ai)
                self.acc[pi] = out
        self.nodepad, self.accarr, self.overlap = {}, {}, {}
        for zn in self.zone_nets:
            rows, pairs = [], set()
            for pi in self.nets[zn]:
                for a in self.acc[pi]:
                    key = (a[0], a[1], a[2])
                    other = self.nodepad.get(key)
                    if other is not None and other != pi and a[3] is None:
                        pairs.add((other, pi))      # two pads of the net lie on top of each other
                    self.nodepad[key] = pi
                    rows.append((a[0], a[1], a[2], pi))
            self.accarr[zn] = np.array(rows, np.int64).reshape(-1, 4)
            self.overlap[zn] = sorted(pairs)
        log(f"pad access: {nstub} pads leave by an escape track, {nnone} pads cannot be reached")

    def escapes(self, n, pi, li, rings):
        """Escape tracks for a pad no grid point serves: from the pad's centre straight along each
        of its two axes, both ways, to the first point from which a legal grid point is one short
        step away. Every sample along the way is measured exactly against the fixed obstacles."""
        tk, hw, clr = self.tk[n], self.w[n] / 2, self.clr[n]
        allp = np.vstack(rings)
        c = (allp.max(axis=0) + allp.min(axis=0)) / 2
        q = allp - allp.mean(axis=0)
        try:
            ev, evec = np.linalg.eigh(q.T @ q)
            u = evec[:, 1]
            if abs(ev[1] - ev[0]) < 1e-9 * max(1.0, ev[1]):
                u = np.array([1.0, 0.0])
        except Exception:
            u = np.array([1.0, 0.0])
        v = np.array([-u[1], u[0]])
        hu, hv = np.abs(q @ u).max(), np.abs(q @ v).max()
        c = allp.mean(axis=0) if abs(hu - hv) > 1e-6 else c
        reach = max(hu, hv) + 2.0
        near = [it for it in self.sitems if it[2] != n and li in it[4]
                and it[1][0] < c[0] + reach and it[1][2] > c[0] - reach and it[1][1] < c[1] + reach and it[1][3] > c[1] - reach]

        def ok(xs, ys):
            good = np.ones(xs.shape, bool)
            for fn, bbox, net, iclr, lays, fixed in near:
                need = hw + (iclr if fixed else max(clr, iclr)) + 0.002
                good &= fn(xs, ys) >= need
            return good

        out = []
        o = self.own[tk][li]
        p = self.p
        for d, ext in ((u, hu), (-u, hu), (v, hv), (-v, hv)):
            ts = np.arange(0.0, ext + 1.8, 0.01)
            px, py = c[0] + ts * d[0], c[1] + ts * d[1]
            good = ok(px, py)
            bad = np.flatnonzero(~good)
            tmax = int(bad[0]) if bad.size else len(ts)
            found = None
            for i in range(0, tmax, 2):
                fx, fy = (px[i] - self.x0) / p, (py[i] - self.y0) / p
                cands = sorted(((gx - fx) ** 2 + (gy - fy) ** 2, gx, gy)
                               for gx in (int(math.floor(fx)), int(math.floor(fx)) + 1)
                               for gy in (int(math.floor(fy)), int(math.floor(fy)) + 1))
                for _, gx, gy in cands:
                    if not (0 < gx < self.nx - 1 and 0 < gy < self.ny - 1):
                        continue
                    if o[gy, gx] != 0 and o[gy, gx] != n:
                        continue
                    nxm, nym = self.xy(gx, gy)
                    s = np.linspace(0, 1, 6)
                    if ok(px[i] + s * (nxm - px[i]), py[i] + s * (nym - py[i])).all():
                        segs = [(float(c[0]), float(c[1]), float(px[i]), float(py[i]))]
                        if abs(nxm - px[i]) + abs(nym - py[i]) > 1e-6:
                            segs.append((float(px[i]), float(py[i]), nxm, nym))
                        found = (li, gx, gy, segs)
                        break
                if found:
                    break
            if found:
                out.append(found)
        return out

    # ------------------------------------------------------------------------------ moving copper
    def dpaint(self, n, item, sign):
        """Add (sign 1) or take away (sign -1) one piece of routed copper in the maps of every kind
        of track: the grid points it makes unusable for other nets."""
        kind = item[0]
        clr = self.clr[n]
        if kind == "v":
            _, gx, gy = item
            x, y = self.xy(gx, gy)
            r, extra, lays = self.vd / 2, -EPS, (0, 1)
            x1, y1, x2, y2 = x, y, x, y
        else:
            _, li, x1, y1, x2, y2, off = item
            r, extra, lays = self.w[n] / 2, (SAFE if off else -EPS), (li,)
        rm = max(k[0] + max(k[1], clr) for k in self.keys) + r + extra
        i0, j0, i1, j1 = self.win(min(x1, x2) - rm, min(y1, y2) - rm, max(x1, x2) + rm, max(y1, y2) + rm)
        if i1 <= i0 or j1 <= j0:
            return
        X, Y = self.coords(i0, j0, i1, j1)
        D = seg_dist(X, Y, x1, y1, x2, y2)
        for k in self.keys:
            m = D < k[0] + max(k[1], clr) + r + extra
            for li in lays:
                self.dyn[k][li][j0:j1, i0:i1][m] += sign

    def paint_net(self, n, sign):
        for it in self.items[n]:
            self.dpaint(n, it, sign)

    def build_items(self, n):
        """Turn the net's grid paths and the escapes it uses into straight pieces of track and vias."""
        items = []
        for path in self.paths[n]:
            start = prev = path[0]
            pdir = None
            for c in path[1:]:
                if c[0] != prev[0]:
                    if prev != start:
                        items.append(("s", prev[0]) + self.xy(start[1], start[2]) + self.xy(prev[1], prev[2]) + (False,))
                    items.append(("v", c[1], c[2]))
                    start, pdir = c, None
                else:
                    d = (c[1] - prev[1], c[2] - prev[2])
                    if pdir is not None and d != pdir:
                        items.append(("s", prev[0]) + self.xy(start[1], start[2]) + self.xy(prev[1], prev[2]) + (False,))
                        start = prev
                    pdir = d
                prev = c
            if prev != start:
                items.append(("s", prev[0]) + self.xy(start[1], start[2]) + self.xy(prev[1], prev[2]) + (False,))
        for (pi, ai) in self.stubs[n]:
            a = self.acc[pi][ai]
            for (x1, y1, x2, y2) in a[3]:
                items.append(("s", a[0], x1, y1, x2, y2, True))
        self.items[n] = items

    def stub_cells(self, pi, ai):
        a = self.acc[pi][ai]
        cells = set()
        for (x1, y1, x2, y2) in a[3]:
            k = max(2, int(math.hypot(x2 - x1, y2 - y1) / (self.p / 2)) + 1)
            for t in np.linspace(0, 1, k):
                cells.add((a[0], int(round((x1 + t * (x2 - x1) - self.x0) / self.p)), int(round((y1 + t * (y2 - y1) - self.y0) / self.p))))
        return cells

    def conflicts(self, n):
        """Grid points of the net's copper that are too close to another net's. The net's own copper
        must be off the maps when this is called."""
        tk, vk = self.tk[n], self.vk[n]
        bad = []
        dt, dv = self.dyn[tk], self.dyn[vk]
        self.first_bad = None
        for k, path in enumerate(self.paths[n]):
            prev = None
            before = len(bad)
            for c in path:
                if dt[c[0]][c[2], c[1]] > 0:
                    bad.append(c)
                if prev is not None and prev[0] != c[0]:
                    if dv[0][c[2], c[1]] > 0 or dv[1][c[2], c[1]] > 0:
                        bad.append(c)
                        bad.append(prev)
                prev = c
            if len(bad) > before and self.first_bad is None:
                self.first_bad = k
        for (pi, ai) in self.stubs[n]:
            for c in self.stub_cells(pi, ai):
                if dt[c[0]][c[2], c[1]] > 0:
                    bad.append(c)
                    self.first_bad = 0
        return bad

    # ------------------------------------------------------------------------------------ search
    def costs(self, n, i0, j0, i1, j1, pres, hard):
        """Cost of stepping on each grid point of a window for net n: BIG where it may not go."""
        tk, vk = self.tk[n], self.vk[n]
        H, W = j1 - j0 + 2, i1 - i0 + 2
        N = H * W
        cost = np.full((2, H, W), BIG, np.int32)
        extra = None
        if not hard:
            # In another net's way: a price for going in (enter), and a smaller one for every
            # further step inside. Both grow where nets have collided before (hist).
            extra = (np.zeros((2, H, W), np.int32), np.zeros((2, H, W), np.int32))
            fee = max(1, pres // 12)
        vfree = None
        for li in range(2):
            o = self.own[tk][li][j0:j1, i0:i1]
            free = (o == 0) | (o == n)
            cnt = self.dyn[tk][li][j0:j1, i0:i1]
            if hard:
                c = np.where(free & (cnt == 0), 0, BIG)
            else:
                h = self.hist[li][j0:j1, i0:i1]
                grow = 4 + np.minimum(h, 60)
                c3 = np.minimum(cnt, 3).astype(np.int32)
                c = np.where(free, h + c3 * fee * grow // 4, BIG)
                extra[0][li, 1:-1, 1:-1] = np.where(free, c3, 0)
                extra[1][li, 1:-1, 1:-1] = pres * grow // 4
            cost[li, 1:-1, 1:-1] = c
            ov = self.own[vk][li][j0:j1, i0:i1]
            f = (ov == 0) | (ov == n)
            vfree = f if vfree is None else (vfree & f)
        vcnt = self.dyn[vk][0][j0:j1, i0:i1] + self.dyn[vk][1][j0:j1, i0:i1]
        vfree &= ~self.holeb[j0:j1, i0:i1]
        if hard:
            vc = np.where(vfree & (vcnt == 0), self.via_cost, BIG)
        else:
            vc = np.where(vfree, self.via_cost + np.minimum(vcnt, 3).astype(np.int32) * pres * 2, BIG)
        vcost = np.full((H, W), BIG, np.int32)
        vcost[1:-1, 1:-1] = vc
        for (li, gx, gy, r) in self.block.get(n, ()):
            a0, b0, a1, b1 = max(gx - r, i0), max(gy - r, j0), min(gx + r + 1, i1), min(gy + r + 1, j1)
            if a1 > a0 and b1 > b0:
                cost[li, b0 - j0 + 1:b1 - j0 + 1, a0 - i0 + 1:a1 - i0 + 1] = BIG
                vcost[b0 - j0 + 1:b1 - j0 + 1, a0 - i0 + 1:a1 - i0 + 1] = BIG
        if extra is not None:
            extra = (extra[0].reshape(2 * N), extra[1].reshape(2 * N))
        return cost.reshape(2 * N), vcost.reshape(N), W, N, extra

    def search(self, src, tgt, cost, vcost, W, N, deadline, extra=None, dmax=None):
        """Cheapest path from any source point to any target point. Points are numbered
        layer * N + row * W + column inside the window. Returns the list of points, or None.
        With tgt None it returns instead the distance of every point up to dmax and the way back
        (dist, par), for use with walk_back."""
        dist = np.full(2 * N, BIG, np.int32)
        par = np.zeros(2 * N, np.int8)
        stamp = np.empty(2 * N, np.int32)
        dist[src] = 0
        buckets = {0: [src]}
        moves = ((1, STEP), (-1, STEP), (W, STEP), (-W, STEP), (W + 1, DIAG), (W - 1, DIAG), (-W + 1, DIAG), (-W - 1, DIAG))
        limit = dmax
        d, dmax, rounds = 0, 0, 0
        found = -1
        while d <= dmax:
            if limit is not None and d > limit:
                break
            lst = buckets.pop(d, None)
            if lst is None:
                d += 1
                continue
            idx = lst[0] if len(lst) == 1 else np.concatenate(lst)
            idx = idx[dist[idx] == d]
            if idx.size == 0:
                d += 1
                continue
            if idx.size > 1:
                ar = np.arange(idx.size, dtype=np.int32)
                stamp[idx] = ar
                idx = idx[stamp[idx] == ar]
            if tgt is not None:
                hit = tgt[idx]
                if hit.any():
                    found = int(idx[int(np.argmax(hit))])
                    break
            rounds += 1
            if not (rounds & 255) and time.time() > deadline:
                return None
            if extra is not None:
                here = extra[0][idx]
            for code, (o, s) in enumerate(moves):
                nidx = idx + o
                nd = cost[nidx] + (d + s)
                if extra is not None:
                    nd += np.maximum(extra[0][nidx] - here, 0) * extra[1][nidx]
                m = nd < dist[nidx]
                if m.any():
                    nn, ndm = nidx[m], nd[m]
                    dist[nn] = ndm
                    par[nn] = code
                    lo, hi = int(ndm.min()), int(ndm.max())
                    if lo == hi:
                        buckets.setdefault(lo, []).append(nn)
                    else:
                        order = np.argsort(ndm, kind="stable")
                        nn, ndm = nn[order], ndm[order]
                        vals, starts = np.unique(ndm, return_index=True)
                        ends = list(starts[1:]) + [len(ndm)]
                        for v, s0, e0 in zip(vals.tolist(), starts.tolist(), ends):
                            buckets.setdefault(v, []).append(nn[s0:e0])
                    if hi > dmax:
                        dmax = hi
            cell = np.where(idx >= N, idx - N, idx)
            vc = vcost[cell]
            mv = vc < BIG
            if mv.any():
                ii = idx[mv]
                nidx = np.where(ii >= N, ii - N, ii + N)
                nd = vc[mv] + cost[nidx] + d
                if extra is not None:
                    nd += extra[0][nidx] * extra[1][nidx]
                m = nd < dist[nidx]
                if m.any():
                    nn, ndm = nidx[m], nd[m]
                    dist[nn] = ndm
                    par[nn] = 8
                    for v in np.unique(ndm).tolist():
                        buckets.setdefault(v, []).append(nn[ndm == v])
                    hi = int(ndm.max())
                    if hi > dmax:
                        dmax = hi
            d += 1
        if tgt is None:
            return dist, par
        if found < 0:
            return None
        return self.walk_back(found, dist, par, W, N)

    def walk_back(self, s, dist, par, W, N):
        offs = (1, -1, W, -W, W + 1, W - 1, -W + 1, -W - 1)
        path = [s]
        while dist[s] != 0:
            code = int(par[s])
            s = (s - N if s >= N else s + N) if code == 8 else s - offs[code]
            path.append(s)
        return path[::-1]

    def route_net(self, n, pres, hard=False, full=False, margin=3.0, keep=()):
        """Join all pads of net n. Sets paths, stubs and failed for the net. keep: paths of the net's
        present routing to start from (the first ones it was built from), instead of starting afresh."""
        pids = self.nets[n]
        cs = [self.pads[i]["c"] for i in pids]
        xs, ys = [c[0] for c in cs], [c[1] for c in cs]
        keep = [list(pth) for pth in keep]
        if full:
            i0, j0, i1, j1 = 0, 0, self.nx, self.ny
        else:
            i0, j0, i1, j1 = self.win(min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
            for pth in keep:
                i0, j0 = min(i0, min(c[1] for c in pth)), min(j0, min(c[2] for c in pth))
                i1, j1 = max(i1, max(c[1] for c in pth) + 1), max(j1, max(c[2] for c in pth) + 1)
        cost, vcost, W, N, extra = self.costs(n, i0, j0, i1, j1, pres, hard)

        def loc(li, gx, gy):
            if i0 <= gx < i1 and j0 <= gy < j1:
                return li * N + (gy - j0 + 1) * W + (gx - i0 + 1)
            return -1

        def glob(s):
            li = 1 if s >= N else 0
            k = s - li * N
            return (li, k % W - 1 + i0, k // W - 1 + j0)

        padnodes = {}
        for pi in pids:
            ids = [loc(a[0], a[1], a[2]) for a in self.acc[pi]]
            ids = np.array([s for s in ids if s >= 0], np.int64)
            padnodes[pi] = ids[cost[ids] < BIG] if ids.size else ids
        # start from the pad nearest the middle of the net
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        order = sorted(pids, key=lambda i: (self.pads[i]["c"][0] - mx) ** 2 + (self.pads[i]["c"][1] - my) ** 2)
        seed = next((pi for pi in order if padnodes[pi].size), None)
        if keep and self.seed.get(n) is not None and padnodes[self.seed[n]].size:
            seed = self.seed[n]
        else:
            keep = []
        self.seed[n] = seed
        self.paths[n], self.stubs[n] = [], []
        if seed is None:
            self.failed[n] = list(pids)
            return
        remaining = [pi for pi in pids if pi != seed and padnodes[pi].size]
        failed = [pi for pi in pids if pi != seed and not padnodes[pi].size]
        intree = np.zeros(2 * N, bool)
        intree[padnodes[seed]] = True
        deadline = T0 + BUDGET - 25
        used = set()

        def absorb():
            """pads whose copper the tree already touches are joined; an escape whose end it touches is added"""
            again = True
            while again:
                again = False
                for pi in list(remaining):
                    hit = padnodes[pi][intree[padnodes[pi]]]
                    if not hit.size:
                        continue
                    if any(a[3] for a in self.acc[pi][:1]):       # this pad is reached by escapes only
                        g = glob(int(hit[0]))
                        for ai, a in enumerate(self.acc[pi]):
                            if (a[0], a[1], a[2]) == g and (pi, ai) not in used:
                                used.add((pi, ai))
                                self.stubs[n].append((pi, ai))
                                break
                    else:
                        intree[padnodes[pi]] = True
                    remaining.remove(pi)
                    again = True

        absorb()
        for gp in keep:
            self.paths[n].append(gp)
            st = self.accnode.get(gp[0])
            if st is not None and st[0] in pids and st not in used:
                used.add(st)
                self.stubs[n].append(st)
            intree[np.array([loc(*c) for c in gp], np.int64)] = True
            absorb()
        while remaining:
            tgt = np.zeros(2 * N, bool)
            for pi in remaining:
                tgt[padnodes[pi]] = True
            path = self.search(np.flatnonzero(intree), tgt, cost, vcost, W, N, deadline, extra)
            if path is None:
                failed += remaining
                break
            gp = [glob(s) for s in path]
            self.paths[n].append(gp)
            st = self.accnode.get(gp[0])
            if st is not None and st[0] in pids and st not in used:
                used.add(st)
                self.stubs[n].append(st)
            intree[np.array(path, np.int64)] = True
            absorb()
            # a via on this path closes the points around it to further vias of the same net
            for a, bq in zip(path, path[1:]):
                if abs(a - bq) == N:
                    k = a % N
                    r = int((self.vdr + self.R["h2h"]) / self.p) + 1
                    yy, xx = k // W, k % W
                    v2 = vcost.reshape(-1, W)
                    v2[max(0, yy - r):yy + r + 1, max(0, xx - r):xx + r + 1] = BIG
        self.failed[n] = failed
        if not full and failed and not hard:
            self.route_net(n, pres, hard, True, keep=keep)

    # --------------------------------------------------------------------- negotiated congestion
    def negotiate(self, until, pres=12):
        """Route every net without a pour, then route again, round after round, the nets that are in
        another net's way, at a rising price for being there. Stops when no net is, when the same
        few nets keep colliding, or when less than `until` seconds are left."""
        nets = list(self.sig_nets)

        def span(n):
            cs = [self.pads[i]["c"] for i in self.nets[n]]
            return (max(c[0] for c in cs) - min(c[0] for c in cs)) + (max(c[1] for c in cs) - min(c[1] for c in cs))

        nets.sort(key=span)
        it, stuck, dropped = 0, 0, 0
        while left() > until:
            it += 1
            moved = 0
            for n in nets:
                if left() < until:
                    break
                keep = ()
                if self.items[n] or self.paths[n]:
                    self.paint_net(n, -1)
                    bad = self.conflicts(n)
                    if not bad and not self.failed[n]:
                        self.paint_net(n, 1)
                        continue
                    for (li, gx, gy) in bad:
                        self.hist[li][max(0, gy - 1):gy + 2, max(0, gx - 1):gx + 2] += 1
                    if bad and self.first_bad and not self.failed[n]:
                        keep = self.paths[n][:self.first_bad]
                self.tries[n] += 1
                self.last_moved[n] = it
                self.route_net(n, pres, margin=3.0 + 2.0 * self.tries[n], keep=keep)
                self.build_items(n)
                self.paint_net(n, 1)
                moved += 1
            nfail = sum(1 for n in nets if self.failed[n])
            log(f"round {it}: {moved} nets routed again, {nfail} incomplete, overlap price {pres}")
            if nfail and it == 1:
                for n in nets:
                    if self.failed[n]:
                        log(f"   cannot reach on {self.netname[n]}: {[self.pads[pi]['ref'] for pi in self.failed[n]][:8]}")
            if moved == 0:
                break
            stuck = stuck + 1 if (moved <= 4 and it > 6) else 0
            if os.environ.get("ROUTE_PICTURE") and (it % 10 == 0):
                self.picture(os.path.join(os.environ["ROUTE_PICTURE"], f"round{it:03d}.png"))
                log("   colliding:", [self.netname[n] for n in nets if self.tries[n] and self.last_moved.get(n) == it][:20])
            if stuck >= 6 and os.environ.get("ROUTE_PICTURE"):
                k = 0
                for n in nets:
                    if self.last_moved.get(n) == it and self.items[n]:
                        self.paint_net(n, -1)
                        bad = self.conflicts(n)
                        self.paint_net(n, 1)
                        log(f"   stuck: {self.netname[n]} collides at {[(c[0],) + tuple(float(v) for v in self.xy(c[1], c[2])) for c in bad[:3]]} pads {[self.pads[pi]['ref'] for pi in self.nets[n]][:6]}")
                        if bad and k < 3:
                            k += 1
                            self.picture(os.path.join(os.environ["ROUTE_PICTURE"], f"stuck{self.npic}.png"), (bad[0][1], bad[0][2], 110), 4)
                            self.npic += 1
            if stuck >= 6:
                # the same few nets keep colliding: if a pour's join is in their way, take it out
                if dropped < 3 and self.drop_joins():
                    dropped += 1
                    stuck = 0
                    continue
                break
            pres = min(int(pres * 1.4) + 1, 2000)
        return it

    def drop_joins(self, skip=None):
        """Take out the joins of pour nets that other nets collide with. They are made again later."""
        n_out = 0
        for zn in self.zone_nets:
            if not self.paths[zn] or zn == skip:
                continue
            self.paint_net(zn, -1)
            dt, dv = self.dyn[self.tk[zn]], self.dyn[self.vk[zn]]
            keep = []
            for path in self.paths[zn]:
                hit = any(dt[c[0]][c[2], c[1]] > 0 for c in path) or any(
                    a[0] != b[0] and (dv[0][b[2], b[1]] > 0 or dv[1][b[2], b[1]] > 0) for a, b in zip(path, path[1:]))
                if hit:
                    n_out += 1
                    for (li, gx, gy) in path:
                        self.hist[li][gy, gx] += 8
                else:
                    keep.append(path)
            self.paths[zn] = keep
            self.build_items(zn)
            self.paint_net(zn, 1)
        if n_out:
            log(f"{n_out} join(s) of a pour taken out to let other nets through")
        return n_out

    def settle(self, until):
        """Make the result legal: take out nets until none is in another's way, then put the ones
        taken out back in, pushing other nets aside where there is no free way."""
        nets = list(self.sig_nets)
        self.drop_joins()        # a pour's join that is still in the way goes first: it can be made again
        while True:
            worst, wc = None, 0
            for n in nets:
                if not self.items[n]:
                    continue
                self.paint_net(n, -1)
                c = len(self.conflicts(n))
                self.paint_net(n, 1)
                if c > wc:
                    worst, wc = n, c
            if worst is None:
                break
            self.paint_net(worst, -1)
            self.items[worst], self.paths[worst], self.stubs[worst] = [], [], []
            self.failed[worst] = list(self.nets[worst])
            log("taken out:", self.netname[worst])
        self.retry_open(until)

    def retry_open(self, until, nets=None):
        for n in (nets if nets is not None else self.sig_nets):
            if not self.failed[n] or left() < until:
                continue
            old = (self.items[n], self.paths[n], self.stubs[n], self.failed[n])
            if self.items[n]:
                self.paint_net(n, -1)
            self.route_net(n, 0, hard=True, full=True)
            if self.failed[n] and len(self.failed[n]) >= len(old[3]):
                self.items[n], self.paths[n], self.stubs[n], self.failed[n] = old
            else:
                self.build_items(n)
            self.paint_net(n, 1)
            if self.failed[n]:
                self.push(n, until)
            log(f"second try for {self.netname[n]}: {'joined' if not self.failed[n] else 'still open'}")

    def audit(self, tag):
        """While developing: say which nets are in another net's way right now."""
        if not os.environ.get("ROUTE_DEBUG"):
            return
        for m in list(self.sig_nets) + list(self.zone_nets):
            if not self.items[m]:
                continue
            self.paint_net(m, -1)
            bad = self.conflicts(m)
            self.paint_net(m, 1)
            if bad:
                log(f"   AUDIT {tag}: {self.netname[m]} collides at {[(c[0],) + tuple(float(v) for v in self.xy(c[1], c[2])) for c in bad[:2]]} ({len(bad)} points)")

    def crossers(self, region):
        """Routed nets whose copper is now in another net's way, among those near region
        (gx0, gy0, gx1, gy1)."""
        out = []
        for m in self.sig_nets:
            if not self.items[m]:
                continue
            if self.paths[m]:
                xs = [c[1] for pth in self.paths[m] for c in pth]
                ys = [c[2] for pth in self.paths[m] for c in pth]
                if min(xs) > region[2] + 60 or max(xs) < region[0] - 60 or min(ys) > region[3] + 60 or max(ys) < region[1] - 60:
                    continue
            self.paint_net(m, -1)
            bad = self.conflicts(m)
            self.paint_net(m, 1)
            if bad:
                out.append(m)
        return out

    def make_room(self, n, region):
        """Net n's copper has just been laid across other nets. Route those nets again around it,
        with everything else as a hard obstacle. If one of them cannot be routed, put them all
        back as they were and return False."""
        old = {}
        ok = True
        for m in self.crossers(region):
            if m == n:
                continue
            old[m] = (self.items[m], self.paths[m], self.stubs[m], self.failed[m])
            self.paint_net(m, -1)
            self.route_net(m, 0, hard=True, margin=5.0)
            if self.failed[m]:
                self.route_net(m, 0, hard=True, full=True)
            self.build_items(m)
            self.paint_net(m, 1)
            if self.failed[m]:
                ok = False
                break
        if not ok:
            for m, st in old.items():
                self.paint_net(m, -1)
                self.items[m], self.paths[m], self.stubs[m], self.failed[m] = st
                self.paint_net(m, 1)
        elif old:
            log(f"   made room for {self.netname[n]}: routed again {[self.netname[m] for m in old]}")
        return ok

    def push(self, n, until):
        """Net n is open and no way is free for it. Lay it across other nets where that costs
        least, then route those nets again around it. Tries a few different crossings."""
        for attempt in range(4):
            if left() < until:
                return False
            old = (self.items[n], self.paths[n], self.stubs[n], self.failed[n])
            if self.items[n]:
                self.paint_net(n, -1)
            self.route_net(n, 60, margin=6.0)
            self.build_items(n)
            bad = self.conflicts(n)
            if self.failed[n]:
                self.items[n], self.paths[n], self.stubs[n], self.failed[n] = old
                self.paint_net(n, 1)
                return False
            self.paint_net(n, 1)
            if not bad:
                return True
            region = (min(c[1] for c in bad), min(c[2] for c in bad), max(c[1] for c in bad), max(c[2] for c in bad))
            if self.make_room(n, region):
                self.drop_joins()
                return True
            self.paint_net(n, -1)
            for (li, gx, gy) in bad:
                self.hist[li][max(0, gy - 2):gy + 3, max(0, gx - 2):gx + 3] += 6
            self.items[n], self.paths[n], self.stubs[n], self.failed[n] = old
            self.paint_net(n, 1)
        return False

    # ------------------------------------------------------------------------------ pair lengths
    def net_length(self, n):
        L = 0.0
        for it in self.items[n]:
            if it[0] == "v":
                L += self.thick
            else:
                L += math.hypot(it[4] - it[2], it[5] - it[3])
        return L

    def pairs(self):
        out = []
        byname = {v: k for k, v in self.netname.items()}
        for base, lim in self.R["skews"].items():
            for sp, sn in (("+", "-"), ("P", "N"), ("_P", "_N"), ("p", "n")):
                a, b = byname.get(base + sp), byname.get(base + sn)
                if a in self.nets and b in self.nets:
                    out.append((a, b, lim))
                    break
        return out

    def lengthen(self, n, need):
        """Add about `need` mm to net n with rectangular detours on its straight runs. The net's
        copper must be off the maps. Returns the length added."""
        tk = self.tk[n]
        p = self.p
        free = []
        for li in range(2):
            o = self.own[tk][li]
            free.append(((o == 0) | (o == n)) & (self.dyn[tk][li] == 0))
        wcells = max(3, int(math.ceil((self.w[n] + self.clr[n]) / p)) + 1)
        added = 0.0
        for _ in range(40):
            rest = need - added
            if rest <= p:
                break
            done = False
            for pi, path in enumerate(self.paths[n]):
                runs = []
                i = 0
                while i < len(path) - 1:
                    a0 = path[i]
                    d = (path[i + 1][1] - a0[1], path[i + 1][2] - a0[2])
                    if path[i + 1][0] != a0[0] or abs(d[0]) + abs(d[1]) != 1:
                        i += 1
                        continue
                    j = i + 1
                    while j < len(path) - 1 and path[j + 1][0] == a0[0] and (path[j + 1][1] - path[j][1], path[j + 1][2] - path[j][2]) == d:
                        j += 1
                    runs.append((j - i, i, j, d))
                    i = j
                runs.sort(reverse=True)
                for m, i, j, d in runs:
                    if m < wcells + 4:
                        continue
                    li = path[i][0]
                    fr = free[li]
                    H = max(1, int(round(rest / (2 * p))))
                    for start in range(i + 2, j - wcells - 1):
                        for q in ((-d[1], d[0]), (d[1], -d[0])):
                            for h in sorted({min(H, int(1.5 / p)), max(1, H * 2 // 3), max(1, H // 2), max(1, H // 4)}, reverse=True):
                                x0, y0 = path[start][1], path[start][2]
                                cells = [(li, x0 + q[0] * t, y0 + q[1] * t) for t in range(1, h + 1)]
                                xt, yt = x0 + q[0] * h, y0 + q[1] * h
                                cells += [(li, xt + d[0] * t, yt + d[1] * t) for t in range(1, wcells + 1)]
                                xe, ye = xt + d[0] * wcells, yt + d[1] * wcells
                                cells += [(li, xe - q[0] * t, ye - q[1] * t) for t in range(1, h)]
                                if all(0 < c[1] < self.nx - 1 and 0 < c[2] < self.ny - 1 and fr[c[2], c[1]] for c in cells):
                                    self.paths[n][pi] = path[:start + 1] + cells + path[start + wcells:]
                                    added += 2 * h * p
                                    done = True
                                    break
                            if done:
                                break
                        if done:
                            break
                    if done:
                        break
                if done:
                    break
            if not done:
                break
        return added

    def path_len(self, path):
        L = 0.0
        for a, b in zip(path, path[1:]):
            if a[0] != b[0]:
                L += self.thick
            else:
                L += self.p * (1.41421356 if (a[1] != b[1] and a[2] != b[2]) else 1.0)
        return L

    def stretch(self, n, need, tol):
        """Make net n about `need` mm longer by sending its longest piece through a point off its
        way: the piece becomes start -> point -> end, each half a shortest path, the two halves
        not touching. The net's copper must be off the maps. Returns the length added."""
        order = sorted(range(len(self.paths[n])), key=lambda k: -len(self.paths[n][k]))
        for pi in order[:2]:
            path = self.paths[n][pi]
            if len(path) < 2:
                continue
            cur = self.path_len(path)
            want = cur + need
            m = (need / 2 + 1.5) / self.p
            i0 = max(0, int(min(c[1] for c in path) - m))
            j0 = max(0, int(min(c[2] for c in path) - m))
            i1 = min(self.nx, int(max(c[1] for c in path) + m) + 1)
            j1 = min(self.ny, int(max(c[2] for c in path) + m) + 1)
            cost, vcost, W, N, _ = self.costs(n, i0, j0, i1, j1, 0, True)

            def loc(c):
                return c[0] * N + (c[2] - j0 + 1) * W + (c[1] - i0 + 1)

            def glob(s):
                li = 1 if s >= N else 0
                k = s - li * N
                return (li, k % W - 1 + i0, k // W - 1 + j0)

            la, lb = loc(path[0]), loc(path[-1])
            if cost[la] >= BIG or cost[lb] >= BIG:
                continue
            lim = int(want / self.p * 2.3) + 2 * self.via_cost
            deadline = T0 + BUDGET - 30
            ra = self.search(np.array([la], np.int64), None, cost, vcost, W, N, deadline, None, lim)
            rb = self.search(np.array([lb], np.int64), None, cost, vcost, W, N, deadline, None, lim)
            if ra is None or rb is None:
                continue
            (da, pa), (db, pb) = ra, rb
            total = da.astype(np.int64) + db
            lo, hi = int(want / self.p * 1.9), int(want / self.p * 2.25)
            cand = np.flatnonzero((total >= lo) & (total <= hi) & (da < BIG) & (db < BIG))
            if not cand.size:
                continue
            mid = want / self.p * 2.06
            cand = cand[np.argsort(np.abs(total[cand] - mid))][:4000:10]
            best = None
            for c in cand.tolist():
                wa = self.walk_back(c, da, pa, W, N)
                wb = self.walk_back(c, db, pb, W, N)
                if set(wa[:-1]) & set(wb[:-1]):
                    continue
                newp = [glob(x) for x in wa + wb[::-1][1:]]
                L = self.path_len(newp)
                if best is None or abs(L - want) < abs(best[0] - want):
                    best = (L, newp)
                    if abs(L - want) <= tol * 0.5:
                        break
            if best is not None and abs(best[0] - want) <= tol:
                self.paths[n][pi] = best[1]
                return best[0] - cur
        return 0.0

    def tune_pairs(self, seen=None):
        """Even out pairs with a length rule. seen: {net: (length KiCad wants, length KiCad measured)}
        from its check, where it reported a pair."""
        for a, b, lim in self.pairs():
            if self.failed[a] or self.failed[b] or not self.items[a] or not self.items[b]:
                continue
            La, Lb = self.net_length(a), self.net_length(b)
            diff = abs(La - Lb)
            s = a if La < Lb else b
            for x in (a, b):
                if seen and x in seen:
                    s, diff = x, max(diff, seen[x][0] - seen[x][1])
            if diff <= lim * 0.5 and not (seen and (a in seen or b in seen)):
                continue
            old = [list(x) for x in self.paths[s]]
            self.paint_net(s, -1)
            got = self.stretch(s, diff, lim * 0.3)
            if got <= 0:
                got = self.lengthen(s, diff - lim * 0.2)
            self.build_items(s)
            if self.conflicts(s):
                self.paths[s] = old
                self.build_items(s)
                got = 0
            self.paint_net(s, 1)
            log(f"pair {self.netname[a]} / {self.netname[b]}: lengths {La:.2f} and {Lb:.2f} mm, limit {lim}, added {got:.2f} mm to {self.netname[s]}")

    def picture(self, path, crop=None, scale=1):
        """A PNG of the copper for looking at while developing: front red, back blue, pads green,
        points where nets collide yellow. crop = (gx, gy, half width in grid points)."""
        import zlib, struct
        img = np.zeros((self.ny, self.nx, 3), np.uint8)
        for pd in self.pads:
            for li, rings in pd["rings"].items():
                bb = pd["bbox"][li]
                i0, j0, i1, j1 = self.win(bb[0], bb[1], bb[2], bb[3])
                X, Y = self.coords(i0, j0, i1, j1)
                m = poly_dist(X, Y, rings) <= 0
                img[j0:j1, i0:i1, 1][m] = np.maximum(img[j0:j1, i0:i1, 1][m], 110 if li == 0 else 60)
        for n, items in self.items.items():
            for it in items:
                if it[0] == "v":
                    x, y = self.xy(it[1], it[2])
                    r, lays, x1, y1, x2, y2 = self.vd / 2, (0, 1), x, y, x, y
                else:
                    r, lays, (x1, y1, x2, y2) = self.w[n] / 2, (it[1],), it[2:6]
                i0, j0, i1, j1 = self.win(min(x1, x2) - r, min(y1, y2) - r, max(x1, x2) + r, max(y1, y2) + r)
                X, Y = self.coords(i0, j0, i1, j1)
                m = seg_dist(X, Y, x1, y1, x2, y2) <= r
                for li in lays:
                    img[j0:j1, i0:i1, 0 if li == 0 else 2][m] = 255
        for n in self.nets:
            if self.items[n]:
                self.paint_net(n, -1)
                for (li, gx, gy) in self.conflicts(n):
                    img[max(0, gy - 1):gy + 2, max(0, gx - 1):gx + 2] = (255, 255, 0)
                self.paint_net(n, 1)
        if crop:
            gx, gy, half = crop
            img = img[max(0, gy - half):gy + half, max(0, gx - half):gx + half]
        if scale > 1:
            img = np.repeat(np.repeat(img, scale, axis=0), scale, axis=1)
        img = np.ascontiguousarray(img)
        h, w = img.shape[:2]
        raw = b"".join(b"\x00" + img[y].tobytes() for y in range(h))

        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)

        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))

    # ------------------------------------------------------------------------------------ output
    def dump_items(self, skip=()):
        segs, vias = [], []
        for n, items in self.items.items():
            if n in skip:
                continue
            w = int(round(self.w[n] * MM))
            for it in items:
                if it[0] == "v":
                    x, y = self.xy(it[1], it[2])
                    vias.append((int(round(x * MM)), int(round(y * MM)), int(round(self.vd * MM)), int(round(self.vdr * MM)), n))
                else:
                    x1, y1, x2, y2 = (int(round(v * MM)) for v in it[2:6])
                    if x1 != x2 or y1 != y2:
                        segs.append((it[1], x1, y1, x2, y2, w, n))
        return {"segs": segs, "vias": vias}

    def save(self, fill=True, skip=()):
        """Write the board with the current copper (KiCad does the writing). With fill=True the pours
        are filled first and the filled areas are returned."""
        d = tempfile.mkdtemp(prefix="save-", dir=self.tmp)
        ip, op, dp = os.path.join(d, "items.json"), os.path.join(d, "out.kicad_pcb"), os.path.join(d, "fill.json")
        with open(ip, "w") as f:
            json.dump(self.dump_items(skip), f)
        for attempt in range(3):
            done = subprocess.run([sys.executable, "-c", WORKER, self.src, ip, op, dp if fill else "-"], capture_output=True, text=True)
            if "WORKER_OK" in done.stdout and os.path.exists(op):
                break
        else:
            log("saving failed:", (done.stderr or "")[-300:])
            return None, None
        tmp = self.out + ".part"
        shutil.copy(op, tmp)
        os.replace(tmp, self.out)
        self.saved = True
        isl = None
        if fill:
            try:
                with open(dp) as f:
                    isl = json.load(f)
            except Exception:
                isl = []
        return op, isl

    def drc(self, board_path):
        d = tempfile.mkdtemp(prefix="drc-", dir=self.tmp)
        name = os.path.basename(self.src)
        tgt = os.path.join(d, name)
        shutil.copy(board_path, tgt)
        for ext in (".kicad_pro", ".kicad_dru"):
            s = self.src[:-len(".kicad_pcb")] + ext
            if os.path.exists(s):
                shutil.copy(s, os.path.join(d, name[:-len(".kicad_pcb")] + ext))
        out = tgt + ".json"
        for attempt in range(3):
            subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "--all-track-errors", "--output", out, tgt],
                           capture_output=True, text=True)
            try:
                with open(out) as f:
                    return json.load(f)
            except Exception:
                continue
        return None

    def read_report(self, rep):
        """-> ({net: [(kind, [(x, y), ...], text)]} for violations with this program's copper, set of open nets)"""
        bad, opens = {}, set()

        def net_of(desc):
            if "[" in desc and "]" in desc:
                return self.plain.get(desc[desc.index("[") + 1:desc.rindex("]")])
            return None

        matched = set()
        for a, b, lim in self.pairs():
            matched |= {a, b}
        for v in rep.get("violations", []):
            kind = v.get("type")
            mine = [net_of(i.get("description", "")) for i in v.get("items", [])
                    if i.get("description", "").split(" ")[0] in ("Track", "Via", "Arc")]
            mine = [n for n in mine if n is not None]
            if kind in ("track_dangling", "via_dangling") and v.get("severity") != "error":
                mine = [n for n in mine if n in matched]
            elif v.get("severity") != "error" or kind not in COUNTED:
                continue
            pos = [(i["pos"]["x"], i["pos"]["y"]) for i in v.get("items", []) if "pos" in i]
            for n in set(mine):
                bad.setdefault(n, []).append((kind, pos, v.get("description", "")))
        for e in rep.get("unconnected_items", []):
            for i in e.get("items", []):
                n = net_of(i.get("description", ""))
                if n is not None:
                    opens.add(n)
        return bad, opens

    # ------------------------------------------------------------------------------------- pours
    def join_pours(self, islands, until):
        """islands: the filled copper of every pour, as KiCad filled it. Join every group of pads of
        a pour's net that the pour does not reach to the rest of the net, with a via or a track:
        first through free space only; then across other nets, which are at once routed again
        around the join (make_room); as a last resort the join is left across them and they are
        negotiated again all together (self.renegotiate). After a join that moved other nets the
        pours have to be filled again, so the function returns early. Returns the joins made."""
        made = 0
        self.pushed = False
        for zn in self.zone_nets:
            if left() < until:
                break
            lab = [np.zeros((self.ny, self.nx), np.int32), np.zeros((self.ny, self.nx), np.int32)]
            nid = 0
            for isl in islands:
                if isl["net"] != zn:
                    continue
                rings = [np.array(r, np.float64) / MM for r in isl["rings"] if len(r) >= 3]
                if not rings:
                    continue
                allp = np.vstack(rings)
                i0, j0, i1, j1 = self.win(allp[:, 0].min(), allp[:, 1].min(), allp[:, 0].max(), allp[:, 1].max())
                if i1 <= i0 or j1 <= j0:
                    continue
                m = raster(rings, self.x0 + i0 * self.p, self.y0 + j0 * self.p, self.p, i1 - i0, j1 - j0)
                nid += 1
                lab[isl["li"]][j0:j1, i0:i1][m] = nid
            pids = self.nets[zn]
            parent = list(range(nid + 1 + len(pids)))

            def find(a):
                while parent[a] != a:
                    parent[a] = parent[parent[a]]
                    a = parent[a]
                return a

            def union(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

            padid = {pi: nid + 1 + k for k, pi in enumerate(pids)}
            for pi in pids:
                pd = self.pads[pi]
                for li, rings in pd["rings"].items():
                    bb = pd["bbox"][li]
                    i0, j0, i1, j1 = self.win(bb[0], bb[1], bb[2], bb[3])
                    if i1 <= i0 or j1 <= j0:
                        continue
                    X, Y = self.coords(i0, j0, i1, j1)
                    inside = poly_dist(X, Y, rings) <= 0
                    for v in np.unique(lab[li][j0:j1, i0:i1][inside]).tolist():
                        if v:
                            union(padid[pi], v)

            for (pa, pb) in self.overlap[zn]:
                union(padid[pa], padid[pb])

            def touch(path):
                """everything a piece of this net's copper touches: filled areas and pads"""
                ids = set()
                for (li, gx, gy) in path:
                    v = int(lab[li][gy, gx])
                    if v:
                        ids.add(v)
                    pi = self.nodepad.get((li, gx, gy))
                    if pi is not None and pi in padid:
                        ids.add(padid[pi])
                for end in (path[0], path[-1]):
                    st = self.accnode.get(end)
                    if st is not None and st[0] in padid:
                        ids.add(padid[st[0]])
                return list(ids)

            for path in self.paths[zn]:
                ids = touch(path)
                for v in ids[1:]:
                    union(ids[0], v)

            def try_join(g, members, margin, hard, force=False):
                cs = [self.pads[pi]["c"] for pi in members]
                if margin is None:
                    i0, j0, i1, j1 = 0, 0, self.nx, self.ny
                else:
                    i0, j0, i1, j1 = self.win(min(c[0] for c in cs) - margin, min(c[1] for c in cs) - margin,
                                              max(c[0] for c in cs) + margin, max(c[1] for c in cs) + margin)
                self.paint_net(zn, -1)
                cost, vcost, W, N, extra = self.costs(zn, i0, j0, i1, j1, 40, hard)
                freec = cost if hard else self.costs(zn, i0, j0, i1, j1, 0, True)[0]
                self.paint_net(zn, 1)
                H = N // W
                roots = np.array([find(i) for i in range(len(parent))])
                src_m = np.zeros((2, H, W), bool)
                tgt_m = np.zeros((2, H, W), bool)
                for li in range(2):
                    lw = lab[li][j0:j1, i0:i1]
                    r = roots[lw]
                    src_m[li, 1:-1, 1:-1] = (lw > 0) & (r == g)
                    tgt_m[li, 1:-1, 1:-1] = (lw > 0) & (r != g)
                src_m, tgt_m = src_m.reshape(2 * N), tgt_m.reshape(2 * N)
                A = self.accarr[zn]
                if len(A):
                    Aw = A[(A[:, 1] >= i0) & (A[:, 1] < i1) & (A[:, 2] >= j0) & (A[:, 2] < j1)]
                    if len(Aw):
                        ids = Aw[:, 0] * N + (Aw[:, 2] - j0 + 1) * W + (Aw[:, 1] - i0 + 1)
                        mine = np.array([find(padid[int(pi)]) == g for pi in Aw[:, 3]], bool)
                        src_m[ids[mine]] = True
                        tgt_m[ids[~mine]] = True
                # start and end on points that are free of other nets; a soft join crosses them in between
                src_m &= freec < BIG
                tgt_m &= freec < BIG
                src = np.flatnonzero(src_m)
                if os.environ.get("ROUTE_DEBUG"):
                    log(f"   join {[self.pads[pi]['ref'] for pi in members][:6]} margin {margin} hard {hard}: {src.size} start points, {int(tgt_m.sum())} end points")
                if not src.size or not tgt_m.any():
                    return False
                path = self.search(src, tgt_m, cost, vcost, W, N, T0 + BUDGET - until, extra)
                if path is None:
                    return False
                gp = []
                for s in path:
                    li = 1 if s >= N else 0
                    k = s - li * N
                    gp.append((li, k % W - 1 + i0, k // W - 1 + j0))
                self.paint_net(zn, -1)
                self.paths[zn].append(gp)
                new_stubs = []
                for end in (gp[0], gp[-1]):
                    st = self.accnode.get(end)
                    if st is not None and st[0] in padid and st not in self.stubs[zn]:
                        self.stubs[zn].append(st)
                        new_stubs.append(st)
                self.build_items(zn)
                self.paint_net(zn, 1)
                if not hard:
                    region = (min(c[1] for c in gp), min(c[2] for c in gp), max(c[1] for c in gp), max(c[2] for c in gp))
                    room = self.make_room(zn, region)
                    if not room and force:
                        # leave the join where it is: the nets in its way are routed again, all together
                        self.renegotiate = True
                        log(f"   a join for {[self.pads[pi]['ref'] for pi in members][:4]} is laid across other nets; they are routed again")
                    elif not room:
                        self.paint_net(zn, -1)
                        self.paths[zn].pop()
                        self.stubs[zn] = [st for st in self.stubs[zn] if st not in new_stubs]
                        self.build_items(zn)
                        self.paint_net(zn, 1)
                        for (li, gx, gy) in gp:
                            self.hist[li][max(0, gy - 1):gy + 2, max(0, gx - 1):gx + 2] += 6
                        return False
                    self.drop_joins(skip=zn)     # joins of other pours that this one crosses are made again later
                ids = touch(gp)
                for v in ids:
                    union(g, v)
                return True

            given_up = set()
            for round_ in range(4 * len(pids)):
                if left() < until:
                    break
                groups = {}
                for pi in pids:
                    groups.setdefault(find(padid[pi]), []).append(pi)
                if len(groups) <= 1:
                    break
                main = max(groups, key=lambda g: len(groups[g]))
                todo = sorted((g for g in groups if g != main and frozenset(groups[g]) not in given_up), key=lambda g: len(groups[g]))
                if not todo:
                    break
                g = todo[0]
                ok = False
                for margin in (4.0, 12.0, None):
                    if try_join(g, groups[g], margin, True):
                        ok = True
                        break
                if not ok:
                    for margin in (8.0, 8.0, None):
                        if try_join(g, groups[g], margin, False):
                            ok = True
                            self.pushed = True
                            break
                if not ok and self.force_budget > 0:
                    self.force_budget -= 1
                    if try_join(g, groups[g], 8.0, False, force=True):
                        ok = True
                        self.pushed = True
                after = len({find(padid[pi]) for pi in pids})
                if ok and after < len(groups):
                    made += 1
                    if self.pushed:
                        break       # other nets have moved: fill the pours again before going on
                else:
                    given_up.add(frozenset(groups[g]))
            groups = {}
            for pi in pids:
                groups.setdefault(find(padid[pi]), []).append(pi)
            self.failed[zn] = [] if len(groups) <= 1 else [pi for g, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))[1:] for pi in v]
            log(f"pour net {self.netname[zn]}: {nid} filled areas, {len(groups)} group(s) of pads left after {made} joins")
        return made

    # ------------------------------------------------------------------------------------- driver
    def run(self):
        self.pushed = self.renegotiate = False
        self.force_budget = 6
        # 1. route the nets without a pour
        self.negotiate(until=BUDGET * 0.40)
        self.audit("after negotiation")
        self.settle(until=BUDGET * 0.32)
        self.audit("after settling")
        self.tune_pairs()
        self.audit("after pairs")
        # 2. pours and KiCad's own check, until clean or out of time
        last, check = None, 0
        for cycle in range(60):
            if left() < 40:
                break
            path, islands = self.save(fill=True)
            if path is None:
                break
            if self.zone_nets and islands is not None and self.join_pours(islands, until=45):
                if self.renegotiate and left() > 100:
                    self.renegotiate = False
                    self.negotiate(until=max(70.0, left() * 0.5))
                    self.settle(until=60)
                self.audit("after joins")
                continue            # copper was added: fill again and look again
            rep = self.drc(path)
            if rep is None:
                break
            bad, opens = self.read_report(rep)
            state = (sorted((n, len(v)) for n, v in bad.items()), sorted(opens))
            check += 1
            log(f"check {check}: nets with violations {[self.netname[n] for n in bad]}, open nets {[self.netname[n] for n in opens]}")
            for n, vs in bad.items():
                for kind, pos, text in vs[:6]:
                    log("   ", kind, text, pos[:2])
            if not bad and not opens:
                log("clean")
                return
            if state == last or left() < 45:
                break
            last = state
            self.repair(bad, opens)
            self.audit("after repair")
        self.finish()

    def repair(self, bad, opens):
        """Act on KiCad's report: bad = {net: violations}, opens = nets with a missing connection."""
        skew = {}
        for n, vs in bad.items():
            if n not in self.nets:
                continue
            if all(k in ("skew_out_of_range",) for k, _, _ in vs):
                m = re.search(r"target net length\s*([\d.]+)\s*mm.*?actual\s*([\d.]+)\s*mm", " ".join(t for _, _, t in vs))
                if m:
                    skew[n] = (float(m.group(1)), float(m.group(2)))
                continue
            # route the net again, keeping clear of the spots KiCad named
            for kind, pos, text in vs:
                for (x, y) in pos:
                    gx, gy = int(round((x - self.x0) / self.p)), int(round((y - self.y0) / self.p))
                    for li in range(2):
                        self.block.setdefault(n, []).append((li, gx, gy, max(1, int(0.2 / self.p))))
            if n in self.zone_nets:
                self.paint_net(n, -1)
                self.items[n], self.paths[n], self.stubs[n] = [], [], []
                continue
            self.paint_net(n, -1)
            self.route_net(n, 0, hard=True, full=True)
            self.build_items(n)
            self.paint_net(n, 1)
            log(f"routed {self.netname[n]} again after a violation: {'joined' if not self.failed[n] else 'open'}")
        if skew:
            self.tune_pairs(skew)
        self.retry_open(until=45, nets=[n for n in opens if n in self.sig_nets])

    def finish(self):
        """Last resort: whatever still breaks a rule is taken out, so that it does not spoil other nets."""
        for cycle in range(4):
            if left() < 12:
                return
            path, islands = self.save(fill=True)
            if path is None:
                return
            rep = self.drc(path)
            if rep is None:
                return
            bad, opens = self.read_report(rep)
            bad = {n: v for n, v in bad.items() if n in self.nets and self.items[n]}
            if not bad:
                return
            for n in bad:
                self.paint_net(n, -1)
                self.items[n], self.paths[n], self.stubs[n] = [], [], []
                self.failed[n] = list(self.nets[n])
                log("taken out after the check:", self.netname[n])
        if left() > 6:
            self.save(fill=False)


def main():
    src, out = sys.argv[1], sys.argv[2]
    r = Router(src, out)
    try:
        r.run()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        log("stopped by an error:", exc)
    if not r.saved:
        try:
            r.save(fill=False)
        except Exception as exc:
            log("could not save:", exc)
    log(f"done: {sum(1 for n in r.nets if not r.failed[n])} of {len(r.nets)} nets joined")
    shutil.rmtree(r.tmp, ignore_errors=True)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
