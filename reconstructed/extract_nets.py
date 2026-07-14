"""Extract real connectivity from the fabricated board's copper.

Pads come from Gerber flashes (D03), tracks from draws (D01). A plated hole ties
F.Cu and B.Cu together, so pads are keyed by position and shared across layers.
Union-find over pad<->track and track<->track contact yields the netlist that was
actually manufactured.
"""
import re, math, collections, json
from pathlib import Path

# Run with plain CPython:  python extract_nets.py   (writes nets.json alongside this file)
HERE = Path(__file__).resolve().parent
SRC  = HERE.parent / "gerbers"
BASE = "RPi_Zero_pHat_Template"

def parse(path):
    txt = open(path, errors="ignore").read()
    dec = int(re.search(r"%FSLAX(\d)(\d)", txt).group(2))
    ap = {}
    for m in re.finditer(r"%ADD(\d+)([CROP]),([^*]+)\*%", txt):
        ap[int(m.group(1))] = (m.group(2), [float(v) for v in m.group(3).split("X")])
    scale = 10 ** dec
    x = y = 0.0
    cur = None
    draws, flashes = [], []
    for line in txt.splitlines():
        line = line.strip()
        dm = re.fullmatch(r"D(\d+)\*", line)
        if dm and int(dm.group(1)) >= 10:
            cur = int(dm.group(1)); continue
        cm = re.search(r"(?:X(-?\d+))?(?:Y(-?\d+))?(?:I(-?\d+))?(?:J(-?\d+))?D0?([123])\*", line)
        if not cm: continue
        nx = int(cm.group(1))/scale if cm.group(1) else x
        ny = int(cm.group(2))/scale if cm.group(2) else y
        op = cm.group(5)
        if op == "1":   draws.append(((x, y), (nx, ny), ap[cur]))
        elif op == "3": flashes.append(((nx, ny), ap[cur]))
        x, y = nx, ny
    return draws, flashes

layers = {}
for L in ("F_Cu", "B_Cu"):
    layers[L] = parse(SRC / f"{BASE}-{L}.gbr")

# --- pads: keyed by position (a plated hole spans both layers) ---
pads = {}          # (x,y) rounded -> {"shape":..., "dims":..., "layers":set()}
def key(p): return (round(p[0], 2), round(p[1], 2))
for L, (draws, flashes) in layers.items():
    for pos, (shape, dims) in flashes:
        k = key(pos)
        e = pads.setdefault(k, {"pos": pos, "shape": shape, "dims": dims, "layers": set()})
        e["layers"].add(L)
        if shape == "R":                      # rectangle = pin 1 marker
            e["shape"], e["dims"] = shape, dims

# --- union-find ---
parent = {}
def find(a):
    parent.setdefault(a, a)
    while parent[a] != a:
        parent[a] = parent[parent[a]]; a = parent[a]
    return a
def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb: parent[ra] = rb

def dist_pt_seg(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx-ax, by-ay
    L2 = dx*dx + dy*dy
    t = 0.0 if L2 == 0 else max(0, min(1, ((px-ax)*dx + (py-ay)*dy)/L2))
    return math.hypot(px - (ax+t*dx), py - (ay+t*dy))

# each track segment is a node; connect to pads it touches and to other segments
segs = []
for L, (draws, flashes) in layers.items():
    for a, b, (shape, dims) in draws:
        segs.append((L, a, b, dims[0]))

for si, (L, a, b, w) in enumerate(segs):
    sid = ("seg", si)
    find(sid)
    # segment endpoints landing on a pad
    for k, e in pads.items():
        if L not in e["layers"]: continue
        px, py = e["pos"]
        rx = max(e["dims"]) / 2 + w / 2
        if dist_pt_seg((px, py), a, b) <= rx * 0.75:
            union(sid, ("pad", k))
    # segment-to-segment contact on the same layer
    for sj in range(si + 1, len(segs)):
        L2, c, d, w2 = segs[sj]
        if L2 != L: continue
        tol = (w + w2) / 2
        if min(math.dist(a, c), math.dist(a, d), math.dist(b, c), math.dist(b, d)) <= tol:
            union(sid, ("seg", sj))

nets = collections.defaultdict(list)
for k in pads:
    nets[find(("pad", k))].append(k)

# --- cluster pads into components by proximity ---
def cluster(keys, gap=3.2):
    keys = sorted(keys); groups = []
    for k in keys:
        placed = False
        for g in groups:
            if any(math.dist(k, m) <= gap for m in g):
                g.append(k); placed = True; break
        if placed: continue
        groups.append([k])
    # merge overlapping groups
    merged = True
    while merged:
        merged = False
        for i in range(len(groups)):
            for j in range(i+1, len(groups)):
                if any(math.dist(a, b) <= gap for a in groups[i] for b in groups[j]):
                    groups[i] += groups[j]; del groups[j]; merged = True; break
            if merged: break
    return groups

groups = cluster(list(pads.keys()))
groups.sort(key=len, reverse=True)
print("=== pad clusters (candidate components) ===")
for g in groups:
    xs = [p[0] for p in g]; ys = [p[1] for p in g]
    r = [k for k in g if pads[k]["shape"] == "R"]
    print(f"  {len(g):2d} pads   x:{min(xs):6.2f}..{max(xs):6.2f}  y:{min(ys):6.2f}..{max(ys):6.2f}"
          f"   pin1(rect)@{r[0] if r else 'none'}")

print(f"\ntotal pads={len(pads)}  nets={len(nets)}  segments={len(segs)}")
json.dump(
    {"pads": {f"{k[0]},{k[1]}": {"shape": v["shape"], "dims": v["dims"], "layers": sorted(v["layers"])}
              for k, v in pads.items()},
     "nets": [[f"{k[0]},{k[1]}" for k in v] for v in nets.values()],
     "groups": [[f"{k[0]},{k[1]}" for k in g] for g in groups]},
    open(HERE / "nets.json", "w"), indent=1)
print("wrote nets.json")
