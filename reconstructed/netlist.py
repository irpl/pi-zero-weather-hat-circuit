"""Name the pins of each component and print the netlist the board actually implements."""
import json, math, collections

from pathlib import Path

# Reads nets.json written by extract_nets.py:  python netlist.py
HERE = Path(__file__).resolve().parent
d = json.load(open(HERE / "nets.json"))
pads = {tuple(float(v) for v in k.split(",")): v for k, v in d["pads"].items()}
nets = [[tuple(float(v) for v in k.split(",")) for k in n] for n in d["nets"]]

P = 2.54
def near(a, b, t=0.15): return abs(a - b) < t

name = {}   # pad -> "REF.PIN"

# --- J1: Raspberry Pi 40-pin GPIO (pin1 rect at x=20.87, y=-29.27) ---
RPI = {1:"3V3",2:"5V",3:"GPIO2_SDA",4:"5V",5:"GPIO3_SCL",6:"GND",7:"GPIO4",8:"GPIO14_TXD",
 9:"GND",10:"GPIO15_RXD",11:"GPIO17",12:"GPIO18",13:"GPIO27",14:"GND",15:"GPIO22",16:"GPIO23",
 17:"3V3",18:"GPIO24",19:"GPIO10_MOSI",20:"GND",21:"GPIO9_MISO",22:"GPIO25",23:"GPIO11_SCLK",
 24:"GPIO8_CE0",25:"GND",26:"GPIO7_CE1",27:"ID_SD",28:"ID_SC",29:"GPIO5",30:"GND",31:"GPIO6",
 32:"GPIO12",33:"GPIO13",34:"GND",35:"GPIO19",36:"GPIO16",37:"GPIO26",38:"GPIO20",39:"GND",40:"GPIO21"}
for p in pads:
    if near(p[1], -29.27) or near(p[1], -26.73):
        i = round((p[0] - 20.87) / P)
        pin = 2*i + 1 if near(p[1], -29.27) else 2*i + 2
        name[p] = ("J1", pin, RPI[pin])

# --- U1: MCP3008, DIP-16 (pin1 rect at x=20.32, y=-46.99) ---
MCP = {1:"CH0",2:"CH1",3:"CH2",4:"CH3",5:"CH4",6:"CH5",7:"CH6",8:"CH7",
 9:"DGND",10:"CS/SHDN",11:"DIN",12:"DOUT",13:"CLK",14:"AGND",15:"VREF",16:"VDD"}
for p in pads:
    if near(p[1], -46.99):                       # pins 1-8, left to right
        pin = round((p[0] - 20.32) / P) + 1
        name[p] = ("U1", pin, MCP[pin])
    elif near(p[1], -39.37):                     # pins 9-16, right to left
        pin = 9 + round((38.10 - p[0]) / P)
        name[p] = ("U1", pin, MCP[pin])

# --- U2: BME280 breakout, 7-pin (pin1 rect at x=36.17 -> numbering right to left) ---
BME = {1:"VIN",2:"3Vo",3:"GND",4:"SCK",5:"SDO",6:"SDI",7:"CS"}
for p in pads:
    if near(p[1], -33.93):
        pin = round((36.17 - p[0]) / P) + 1
        name[p] = ("U2", pin, BME.get(pin, f"P{pin}"))

# --- J2 Wind / J3 RainFall: 6-pin RJ jacks ---
# RJ11/RJ12 jacks: pads are staggered, so the pin number advances with x (1.27 mm)
# while alternating rows - odd pins in pin-1's row, even pins in the other. The
# contacts inside a modular jack sit side by side and their tails cannot cross.
for ref, x0 in (("J2_Wind", 45.72), ("J3_RainFall", 60.35)):
    for p in pads:
        if x0 - 0.2 <= p[0] <= x0 + 6.6 and (near(p[1], -37.08) or near(p[1], -39.62)):
            pin = round((p[0] - x0) / 1.27) + 1
            name[p] = (ref, pin, f"pin{pin}")

# --- R1, plus two vias ---
# The remaining pair (0.40 mm drill, 0.60 mm round pad, copper on both layers, no
# silkscreen refdes) are vias on the +3V3 net where the route changes layer - not a
# component, so they carry no pin name and do not appear on the schematic.
for p in pads:
    if p not in name:
        if near(p[0], 16.51): name[p] = ("R1", 1 if near(p[1], -45.72) else 2, "")
        else:                 name[p] = ("VIA", 1 if p[0] < 43 else 2, "3V3 layer change")

def lbl(p):
    r, pin, fn = name[p]
    return f"{r}.{pin}" + (f" ({fn})" if fn else "")

print("=== NETS (pads electrically joined by copper) ===")
multi = [n for n in nets if len(n) > 1]
for n in sorted(multi, key=lambda n: -len(n)):
    print("  " + "  ==  ".join(sorted(lbl(p) for p in n)))

print("\n=== UNCONNECTED pads (no copper leaves them) ===")
single = [n[0] for n in nets if len(n) == 1]
by_ref = collections.defaultdict(list)
for p in single: by_ref[name[p][0]].append(name[p][1])
for r, pins in sorted(by_ref.items()):
    print(f"  {r}: pins {sorted(pins)}")
