"""Reconstruct a .kicad_pcb from the 2020 JLCPCB Gerber + Excellon set.

Recovers geometry only: copper tracks, pads, board outline, silkscreen.
There is no netlist or schematic in Gerber data, so nets/refdes cannot be recovered.
"""
import re, math, sys
from pathlib import Path
import pcbnew

# Needs KiCad's bundled Python, which provides `pcbnew`:
#   "C:/Program Files/KiCad/8.0/bin/python.exe" gerber2pcb.py
HERE = Path(__file__).resolve().parent
SRC  = HERE.parent / "gerbers"
OUT  = HERE / "RPi_Zero_pHat_recovered.kicad_pcb"
BASE = "RPi_Zero_pHat_Template"

def nm(mm):            # mm -> internal nanometres
    return int(round(mm * 1e6))
def V(x, y):           # gerber mm (Y up) -> KiCad VECTOR2I (Y down)
    return pcbnew.VECTOR2I(nm(x), nm(-y))

class Gerber:
    """Minimal RS-274X reader: apertures, D01 draws, D02 moves, D03 flashes, G02/G03 arcs."""
    def __init__(self, path):
        self.apertures = {}       # code -> (shape, params)
        self.draws = []           # (x1,y1,x2,y2,aperture)
        self.arcs = []            # (x1,y1,x2,y2,cx,cy,ccw,aperture)
        self.flashes = []         # (x,y,aperture)
        self._parse(open(path, errors="ignore").read())

    def _parse(self, text):
        # coordinate format, e.g. %FSLAX46Y46*%  -> 6 decimal places
        m = re.search(r"%FSLAX(\d)(\d)Y\d\d\*%", text)
        self.dec = int(m.group(2))
        for am in re.finditer(r"%ADD(\d+)([CROP]),([^*]+)\*%", text):
            code, shape, params = int(am.group(1)), am.group(2), am.group(3)
            self.apertures[code] = (shape, [float(v) for v in params.split("X")])

        scale = 10 ** self.dec
        x = y = 0.0
        ap = None
        interp = "G01"
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("%") or line.startswith("G04"):
                continue
            dm = re.fullmatch(r"D(\d+)\*", line)
            if dm and int(dm.group(1)) >= 10:
                ap = int(dm.group(1)); continue
            if line.startswith("G01"): interp = "G01"
            if line.startswith("G02"): interp = "G02"
            if line.startswith("G03"): interp = "G03"
            cm = re.search(r"(?:X(-?\d+))?(?:Y(-?\d+))?(?:I(-?\d+))?(?:J(-?\d+))?D0?([123])\*", line)
            if not cm:
                continue
            nx = int(cm.group(1)) / scale if cm.group(1) else x
            ny = int(cm.group(2)) / scale if cm.group(2) else y
            i  = int(cm.group(3)) / scale if cm.group(3) else 0.0
            j  = int(cm.group(4)) / scale if cm.group(4) else 0.0
            op = cm.group(5)
            if op == "1":                                  # draw
                if interp == "G01":
                    self.draws.append((x, y, nx, ny, ap))
                else:
                    self.arcs.append((x, y, nx, ny, x + i, y + j, interp == "G03", ap))
            elif op == "3":                                # flash
                self.flashes.append((nx, ny, ap))
            x, y = nx, ny

def read_drill(path):
    """Excellon: tool table + hole positions. Returns [(x_mm, y_mm, dia_mm)]."""
    tools, holes = {}, []
    unit_in = False
    cur = None
    for line in open(path, errors="ignore"):
        line = line.strip()
        if line == "INCH": unit_in = True
        if line == "METRIC": unit_in = False
        tm = re.fullmatch(r"T(\d+)C([\d.]+)", line)
        if tm:
            d = float(tm.group(2))
            tools[int(tm.group(1))] = d * 25.4 if unit_in else d
            continue
        sm = re.fullmatch(r"T(\d+)", line)
        if sm:
            cur = int(sm.group(1)); continue
        cm = re.fullmatch(r"X(-?[\d.]+)Y(-?[\d.]+)", line)
        if cm and cur in tools:
            x, y = float(cm.group(1)), float(cm.group(2))
            if unit_in: x, y = x * 25.4, y * 25.4
            holes.append((x, y, tools[cur]))
    return holes

board = pcbnew.BOARD()

# ---- copper: tracks + pads --------------------------------------------------
copper = {"F_Cu": pcbnew.F_Cu, "B_Cu": pcbnew.B_Cu}
pad_flashes = []   # (x, y, shape, dims, layer)
for name, layer in copper.items():
    g = Gerber(SRC / f"{BASE}-{name}.gbr")
    for (x1, y1, x2, y2, ap) in g.draws:
        shape, p = g.apertures[ap]
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(V(x1, y1)); t.SetEnd(V(x2, y2))
        t.SetWidth(nm(p[0])); t.SetLayer(layer)
        board.Add(t)
    for (x1, y1, x2, y2, cx, cy, ccw, ap) in g.arcs:
        shape, p = g.apertures[ap]
        a = pcbnew.PCB_ARC(board)
        a1 = math.atan2(y1 - cy, x1 - cx); a2 = math.atan2(y2 - cy, x2 - cx)
        r = math.hypot(x1 - cx, y1 - cy)
        if ccw and a2 <= a1: a2 += 2 * math.pi
        if not ccw and a2 >= a1: a2 -= 2 * math.pi
        am = (a1 + a2) / 2
        a.SetStart(V(x1, y1)); a.SetEnd(V(x2, y2))
        a.SetMid(V(cx + r * math.cos(am), cy + r * math.sin(am)))
        a.SetWidth(nm(p[0])); a.SetLayer(layer)
        board.Add(a)
    for (x, y, ap) in g.flashes:
        shape, p = g.apertures[ap]
        pad_flashes.append((x, y, shape, p, name))

# match each drill hole to the copper flash at the same spot -> through-hole pad
holes = read_drill(SRC / f"{BASE}.drl")
TOL = 0.06   # mm
made = 0
VIA_DRILL = 0.45   # a 0.40 mm hole with a small round pad on both layers is a via, not a part

for (hx, hy, hd) in holes:
    best = None
    for (x, y, shape, p, name) in pad_flashes:
        if name != "F_Cu":
            continue
        if abs(x - hx) < TOL and abs(y - hy) < TOL:
            best = (shape, p); break
    if hd <= VIA_DRILL and best and best[0] == "C":
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(V(hx, hy))
        v.SetDrill(nm(hd)); v.SetWidth(nm(best[1][0]))
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        board.Add(v)
        made += 1
        continue
    fp = pcbnew.FOOTPRINT(board)
    fp.SetPosition(V(hx, hy))
    pad = pcbnew.PAD(fp)
    if best:
        shape, p = best
        if shape == "C":
            pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE); pad.SetSize(pcbnew.VECTOR2I(nm(p[0]), nm(p[0])))
        elif shape == "R":
            pad.SetShape(pcbnew.PAD_SHAPE_RECT);   pad.SetSize(pcbnew.VECTOR2I(nm(p[0]), nm(p[1])))
        else:  # obround
            pad.SetShape(pcbnew.PAD_SHAPE_OVAL);   pad.SetSize(pcbnew.VECTOR2I(nm(p[0]), nm(p[1])))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetLayerSet(pad.PTHMask())
    else:
        # no copper flash -> mechanical/mounting hole
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE); pad.SetSize(pcbnew.VECTOR2I(nm(hd), nm(hd)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
        pad.SetLayerSet(pad.UnplatedHoleMask())
    pad.SetDrillSize(pcbnew.VECTOR2I(nm(hd), nm(hd)))
    pad.SetPosition(V(hx, hy))
    fp.Add(pad)
    board.Add(fp)
    made += 1

# ---- outline + silkscreen ---------------------------------------------------
graphics = {"Edge_Cuts": pcbnew.Edge_Cuts, "F_SilkS": pcbnew.F_SilkS, "B_SilkS": pcbnew.B_SilkS}
for name, layer in graphics.items():
    g = Gerber(SRC / f"{BASE}-{name}.gbr")
    for (x1, y1, x2, y2, ap) in g.draws:
        shape, p = g.apertures[ap]
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(V(x1, y1)); s.SetEnd(V(x2, y2))
        s.SetWidth(nm(p[0])); s.SetLayer(layer)
        board.Add(s)
    for (x1, y1, x2, y2, cx, cy, ccw, ap) in g.arcs:
        shape, p = g.apertures[ap]
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_ARC)
        # KiCad arc: start, mid, end. Compute mid point on the arc.
        a1 = math.atan2(y1 - cy, x1 - cx); a2 = math.atan2(y2 - cy, x2 - cx)
        r = math.hypot(x1 - cx, y1 - cy)
        if ccw:
            if a2 <= a1: a2 += 2 * math.pi
        else:
            if a2 >= a1: a2 -= 2 * math.pi
        am = (a1 + a2) / 2
        mx, my = cx + r * math.cos(am), cy + r * math.sin(am)
        s.SetArcGeometry(V(x1, y1), V(mx, my), V(x2, y2))
        s.SetWidth(nm(p[0])); s.SetLayer(layer)
        board.Add(s)

pcbnew.SaveBoard(str(OUT), board)
print(f"tracks={len(board.GetTracks())} footprints={len(board.GetFootprints())} drawings={len(board.GetDrawings())} holes={made}")
print("saved ->", OUT)
