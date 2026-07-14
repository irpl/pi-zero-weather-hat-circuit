"""Rebuild a netlisted .kicad_pcb from the 2020 JLCPCB Gerber + Excellon set.

Gerbers store artwork, not a design. This script recovers both:

  * geometry  - tracks, pads, vias, board outline and silkscreen, straight from the
                Gerber apertures and the Excellon drill table;
  * electrics - pads are grouped into components (from their pitch and rect pin-1
                markers), and the netlist is recovered by union-find over the copper
                (a plated hole ties F.Cu to B.Cu), then applied to pads, tracks and vias.

The result opens in KiCad with a ratsnest and passes connectivity DRC, unlike a
plain artwork tracing. What is NOT recoverable from Gerbers: component values
(R1 = 5k is known independently) and real footprints - pads carry recovered
geometry, not library footprints.

Run with KiCad's bundled Python, which provides `pcbnew`:
    "C:/Program Files/KiCad/8.0/bin/python.exe" gerber2pcb.py
"""
import re, math, collections
from pathlib import Path
import pcbnew

HERE = Path(__file__).resolve().parent
SRC  = HERE.parent / "gerbers"
OUT  = HERE / "RPi_Zero_pHat_recovered.kicad_pcb"
BASE = "RPi_Zero_pHat_Template"

def nm(mm):  return int(round(mm * 1e6))
def V(x, y): return pcbnew.VECTOR2I(nm(x), nm(-y))   # Gerber Y is up, KiCad Y is down

OUTLINE_W = 0.1    # KiCad 5 plots Edge.Cuts onto the copper layers too; that stroke
                   # is a plot artifact, not copper, and is excluded from the tracks.
VIA_DRILL = 0.45   # 0.40 mm hole + small round pad on both layers = via, not a part

# ---------------------------------------------------------------- gerber / drill
class Gerber:
    """Minimal RS-274X reader: apertures, D01 draws, D02 moves, D03 flashes, G02/G03 arcs."""
    def __init__(self, path):
        self.apertures, self.draws, self.arcs, self.flashes = {}, [], [], []
        self._parse(open(path, errors="ignore").read())

    def _parse(self, text):
        dec = int(re.search(r"%FSLAX(\d)(\d)Y\d\d\*%", text).group(2))
        for am in re.finditer(r"%ADD(\d+)([CROP]),([^*]+)\*%", text):
            self.apertures[int(am.group(1))] = (am.group(2),
                                                [float(v) for v in am.group(3).split("X")])
        scale = 10 ** dec
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
            for g in ("G01", "G02", "G03"):
                if line.startswith(g): interp = g
            cm = re.search(r"(?:X(-?\d+))?(?:Y(-?\d+))?(?:I(-?\d+))?(?:J(-?\d+))?D0?([123])\*", line)
            if not cm:
                continue
            nx = int(cm.group(1)) / scale if cm.group(1) else x
            ny = int(cm.group(2)) / scale if cm.group(2) else y
            i  = int(cm.group(3)) / scale if cm.group(3) else 0.0
            j  = int(cm.group(4)) / scale if cm.group(4) else 0.0
            op = cm.group(5)
            if op == "1":
                if interp == "G01": self.draws.append((x, y, nx, ny, ap))
                else:               self.arcs.append((x, y, nx, ny, x + i, y + j, interp == "G03", ap))
            elif op == "3":
                self.flashes.append((nx, ny, ap))
            x, y = nx, ny

def read_drill(path):
    tools, holes, unit_in, cur = {}, [], False, None
    for line in open(path, errors="ignore"):
        line = line.strip()
        if line == "INCH":   unit_in = True
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

layers = {L: Gerber(SRC / f"{BASE}-{L}.gbr")
          for L in ("F_Cu", "B_Cu", "F_Mask", "B_Mask", "F_SilkS", "B_SilkS", "Edge_Cuts")}
holes = read_drill(SRC / f"{BASE}.drl")

# ---------------------------------------------------------------- pads & pin naming
# A plated hole spans both layers, so pads are keyed by position.
pads = {}
for L in ("F_Cu", "B_Cu"):
    g = layers[L]
    for (x, y, ap) in g.flashes:
        shape, dims = g.apertures[ap]
        k = (round(x, 2), round(y, 2))
        e = pads.setdefault(k, {"pos": (x, y), "shape": shape, "dims": dims})
        if shape == "R":                      # rectangular pad = pin 1
            e["shape"], e["dims"] = shape, dims

P = 2.54
def near(a, b, t=0.15): return abs(a - b) < t

RPI = {1:"3V3",2:"5V",3:"GPIO2_SDA",4:"5V",5:"GPIO3_SCL",6:"GND",7:"GPIO4",8:"GPIO14_TXD",
 9:"GND",10:"GPIO15_RXD",11:"GPIO17",12:"GPIO18",13:"GPIO27",14:"GND",15:"GPIO22",16:"GPIO23",
 17:"3V3",18:"GPIO24",19:"GPIO10_MOSI",20:"GND",21:"GPIO9_MISO",22:"GPIO25",23:"GPIO11_SCLK",
 24:"GPIO8_CE0",25:"GND",26:"GPIO7_CE1",27:"ID_SD",28:"ID_SC",29:"GPIO5",30:"GND",31:"GPIO6",
 32:"GPIO12",33:"GPIO13",34:"GND",35:"GPIO19",36:"GPIO16",37:"GPIO26",38:"GPIO20",39:"GND",40:"GPIO21"}

def classify(k):
    """pad key -> (refdes, pin number) using pitch and the rect pin-1 markers."""
    x, y = k
    if near(y, -29.27): return "J1", 2 * round((x - 20.87) / P) + 1
    if near(y, -26.73): return "J1", 2 * round((x - 20.87) / P) + 2
    if near(y, -46.99): return "U1", round((x - 20.32) / P) + 1          # MCP3008 pins 1-8
    if near(y, -39.37): return "U1", 9 + round((38.10 - x) / P)          # MCP3008 pins 9-16
    if near(y, -33.93): return "U2", round((36.17 - x) / P) + 1          # BME280, pin 1 at right
    # RJ11/RJ12 jacks: pads are staggered, not row-major. Pin number advances with x
    # (1.27 mm) while alternating rows - odd pins in pin-1's row, even pins in the other.
    # Contacts inside a modular jack sit side by side; their tails cannot cross.
    for ref, x0 in (("J2", 45.72), ("J3", 60.35)):
        if x0 - 0.2 <= x <= x0 + 6.6 and (near(y, -37.08) or near(y, -39.62)):
            return ref, round((x - x0) / 1.27) + 1
    if near(x, 16.51): return "R1", 1 if near(y, -45.72) else 2
    return None, None

# ---------------------------------------------------------------- recover the netlist
# union-find over pads and copper segments; a plated hole bridges the layers
parent = {}
def find(a):
    parent.setdefault(a, a)
    while parent[a] != a:
        parent[a] = parent[parent[a]]; a = parent[a]
    return a
def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb: parent[ra] = rb

def d_pt_seg(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

segs = []   # (layer, a, b, width) - real copper only, outline strokes excluded
for L in ("F_Cu", "B_Cu"):
    g = layers[L]
    for (x1, y1, x2, y2, ap) in g.draws:
        w = g.apertures[ap][1][0]
        if abs(w - OUTLINE_W) < 1e-6: continue
        segs.append((L, (x1, y1), (x2, y2), w))

for si, (L, a, b, w) in enumerate(segs):
    union(("seg", si), ("seg", si))
    for k, e in pads.items():
        r = max(e["dims"]) / 2 + w / 2
        if d_pt_seg(e["pos"], a, b) <= r * 0.75:
            union(("seg", si), ("pad", k))
    for sj in range(si + 1, len(segs)):
        L2, c, d, w2 = segs[sj]
        if L2 != L: continue
        if min(math.dist(a, c), math.dist(a, d), math.dist(b, c), math.dist(b, d)) <= (w + w2) / 2:
            union(("seg", si), ("seg", sj))

# name each electrical group from the pins it touches
NAMED = {                      # (ref,pin) -> net name
 **{("J1", p): "GND" for p in (9, 39)}, **{("J1", p): "+3V3" for p in (1, 17)},
 ("J1", 23): "SPI_SCLK", ("J1", 21): "SPI_MISO", ("J1", 19): "SPI_MOSI", ("J1", 24): "SPI_CE0",
 ("J1", 3): "I2C_SDA", ("J1", 5): "I2C_SCL", ("J1", 29): "WIND_SPD", ("J1", 31): "RAIN",
 ("J2", 2): "WIND_DIR",
}
group_net = {}
for k in pads:
    ref, pin = classify(k)
    if (ref, pin) in NAMED:
        group_net[find(("pad", k))] = NAMED[(ref, pin)]

def net_of(node):
    return group_net.get(find(node))

# ---------------------------------------------------------------- build the board
board = pcbnew.BOARD()
netmap = {}
for n in sorted(set(group_net.values())):
    ni = pcbnew.NETINFO_ITEM(board, n)
    board.Add(ni); netmap[n] = ni

VALUES = {"J1": "RPi_GPIO", "U1": "MCP3008", "U2": "BME280",
          "J2": "Wind", "J3": "RainFall", "R1": "5k"}

# component footprints, one per part, carrying its recovered pads
by_ref = collections.defaultdict(list)
for k in pads:
    ref, pin = classify(k)
    if ref: by_ref[ref].append((pin, k))

drill_at = {(round(x, 2), round(y, 2)): d for (x, y, d) in holes}
mask_at  = {}
for (x, y, ap) in layers["F_Mask"].flashes:
    shape, dims = layers["F_Mask"].apertures[ap]
    mask_at[(round(x, 2), round(y, 2))] = max(dims)

for ref, plist in by_ref.items():
    fp = pcbnew.FOOTPRINT(board)
    cx = sum(pads[k]["pos"][0] for _, k in plist) / len(plist)
    cy = sum(pads[k]["pos"][1] for _, k in plist) / len(plist)
    fp.SetPosition(V(cx, cy))
    fp.SetReference(ref)
    fp.SetValue(VALUES.get(ref, ""))
    fp.Reference().SetVisible(True)
    for pin, k in sorted(plist):
        e = pads[k]
        pad = pcbnew.PAD(fp)
        shape, dims = e["shape"], e["dims"]
        if shape == "C":   pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE); size = (dims[0], dims[0])
        elif shape == "R": pad.SetShape(pcbnew.PAD_SHAPE_RECT);   size = (dims[0], dims[1])
        else:              pad.SetShape(pcbnew.PAD_SHAPE_OVAL);   size = (dims[0], dims[1])
        pad.SetSize(pcbnew.VECTOR2I(nm(size[0]), nm(size[1])))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetLayerSet(pad.PTHMask())
        d = drill_at.get(k, 1.0)
        pad.SetDrillSize(pcbnew.VECTOR2I(nm(d), nm(d)))
        pad.SetPosition(V(*e["pos"]))
        pad.SetNumber(str(pin))
        n = net_of(("pad", k))
        if n: pad.SetNet(netmap[n])
        fp.Add(pad)
    board.Add(fp)

# unplated mounting / mechanical holes - mask opening comes from the mask gerber,
# which is where the original's 6.2 mm keep-out ring lives
mech = 0
for (hx, hy, hd) in holes:
    k = (round(hx, 2), round(hy, 2))
    if k in pads:                      # copper pad or via, handled elsewhere
        continue
    fp = pcbnew.FOOTPRINT(board)
    fp.SetPosition(V(hx, hy))
    fp.SetReference(f"H{mech + 1}")
    fp.Reference().SetVisible(False)
    pad = pcbnew.PAD(fp)
    opening = mask_at.get(k, hd)       # e.g. 6.2 mm around the 4 mounting holes
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(nm(opening), nm(opening)))
    pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
    pad.SetLayerSet(pad.UnplatedHoleMask())
    pad.SetDrillSize(pcbnew.VECTOR2I(nm(hd), nm(hd)))
    pad.SetPosition(V(hx, hy))
    fp.Add(pad)
    board.Add(fp)
    mech += 1

# vias
vias = 0
for (hx, hy, hd) in holes:
    k = (round(hx, 2), round(hy, 2))
    e = pads.get(k)
    if not e or hd > VIA_DRILL or e["shape"] != "C" or classify(k)[0]:
        continue
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(V(hx, hy))
    v.SetDrill(nm(hd)); v.SetWidth(nm(e["dims"][0]))
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    n = net_of(("pad", k))
    if n: v.SetNet(netmap[n])
    board.Add(v)
    vias += 1

# tracks (outline strokes excluded), each carrying its recovered net
LAYER = {"F_Cu": pcbnew.F_Cu, "B_Cu": pcbnew.B_Cu}
for si, (L, a, b, w) in enumerate(segs):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(V(*a)); t.SetEnd(V(*b))
    t.SetWidth(nm(w)); t.SetLayer(LAYER[L])
    n = net_of(("seg", si))
    if n: t.SetNet(netmap[n])
    board.Add(t)

# board outline + silkscreen
GFX = {"Edge_Cuts": pcbnew.Edge_Cuts, "F_SilkS": pcbnew.F_SilkS, "B_SilkS": pcbnew.B_SilkS}
for name, layer in GFX.items():
    g = layers[name]
    for (x1, y1, x2, y2, ap) in g.draws:
        w = g.apertures[ap][1][0]
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(V(x1, y1)); s.SetEnd(V(x2, y2))
        s.SetWidth(nm(w)); s.SetLayer(layer)
        board.Add(s)
    for (x1, y1, x2, y2, cx, cy, ccw, ap) in g.arcs:
        w = g.apertures[ap][1][0]
        a1 = math.atan2(y1 - cy, x1 - cx); a2 = math.atan2(y2 - cy, x2 - cx)
        r  = math.hypot(x1 - cx, y1 - cy)
        if ccw and a2 <= a1: a2 += 2 * math.pi
        if not ccw and a2 >= a1: a2 -= 2 * math.pi
        am = (a1 + a2) / 2
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_ARC)
        s.SetArcGeometry(V(x1, y1), V(cx + r * math.cos(am), cy + r * math.sin(am)), V(x2, y2))
        s.SetWidth(nm(w)); s.SetLayer(layer)
        board.Add(s)

pcbnew.SaveBoard(str(OUT), board)

netted = sum(1 for f in board.GetFootprints() for p in f.Pads() if p.GetNetname())
print(f"components={len(by_ref)}  pads={sum(len(v) for v in by_ref.values())} ({netted} netted)"
      f"  mech_holes={mech}  vias={vias}  tracks={len(segs)}  nets={len(netmap)}")
print("saved ->", OUT)
