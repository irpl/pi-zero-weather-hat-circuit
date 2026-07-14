"""Generate a KiCad schematic whose connectivity is exactly the netlist extracted
from the fabricated board's copper (see extract_nets.py / netlist.py).

Nets are attached with local labels at each pin. Pins the board leaves unconnected
get a no-connect flag, so the schematic states plainly what is and isn't wired.
"""
import re, uuid, os

from pathlib import Path

# Run with plain CPython:  python make_sch.py
# Override the symbol library location with KICAD_SYMBOL_DIR if KiCad lives elsewhere.
HERE = Path(__file__).resolve().parent
LIB  = os.environ.get("KICAD_SYMBOL_DIR", r"C:\Program Files\KiCad\8.0\share\kicad\symbols")
OUT  = HERE / "RPi_Zero_pHat_recovered.kicad_sch"

def U(): return str(uuid.uuid4())

# ---------------------------------------------------------------- symbol loading
def load_symbol(libname, symname):
    """Pull one top-level (symbol "NAME" ...) block out of a .kicad_sym file.

    Symbols may be defined as (extends "PARENT"), which inherits the parent's pins
    and graphics; in that case return the parent's body under the child's name.
    """
    txt = open(os.path.join(LIB, libname + ".kicad_sym"), encoding="utf-8").read()
    start = txt.index(f'(symbol "{symname}"')
    depth, i = 0, start
    while True:
        if txt[i] == "(": depth += 1
        elif txt[i] == ")":
            depth -= 1
            if depth == 0: break
        i += 1
    block = txt[start:i + 1]
    ext = re.search(r'\(extends\s+"([^"]+)"\)', block)
    if ext:
        parent = load_symbol(libname, ext.group(1))
        # keep the parent's body (pins/graphics), present it under the child's name
        pname = ext.group(1)
        block = parent.replace(f'(symbol "{pname}"', f'(symbol "{symname}"', 1)
        block = block.replace(f'(symbol "{pname}_', f'(symbol "{symname}_')   # unit sub-symbols
    return block

def symbol_pins(block):
    """[(number, name, x, y)] - pin connection points in symbol coords."""
    pins = []
    for m in re.finditer(
        r"\(pin\s+\w+\s+\w+\s*\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\)"
        r".*?\(name\s+\"([^\"]*)\".*?\(number\s+\"([^\"]*)\"",
        block, re.S):
        x, y, ang, nm, num = m.groups()
        pins.append((num, nm, float(x), float(y), float(ang)))
    return pins

PARTS = {                       # ref -> (lib, symbol, value, x, y, ref_dy)
    "J1": ("Connector_Generic", "Conn_02x20_Odd_Even", "RPi_GPIO",  60,  105, 31.0),
    "U1": ("Analog_ADC",        "MCP3008",             "MCP3008",  145,  105, 17.0),
    "U2": ("Connector_Generic", "Conn_01x07",          "BME280",   215,   60, 12.0),
    "J2": ("Connector_Generic", "Conn_01x06",          "Wind",     215,  110, 11.0),
    "J3": ("Connector_Generic", "Conn_01x06",          "RainFall", 215,  155, 11.0),
    "R1": ("Device",            "R",                   "5k",       145,  160,  6.0),
}

# net -> [(ref, pin), ...]   (taken verbatim from the copper extraction)
NETS = {
    "GND":      [("J1","9"),("J1","39"),("J2","5"),("J3","5"),("R1","2"),
                 ("U1","9"),("U1","14"),("U2","3")],
    "+3V3":     [("J1","1"),("J1","17"),("J2","3"),
                 ("U1","15"),("U1","16"),("U2","1")],
    "SPI_SCLK": [("J1","23"),("U1","13")],
    "SPI_MISO": [("J1","21"),("U1","12")],
    "SPI_MOSI": [("J1","19"),("U1","11")],
    "SPI_CE0":  [("J1","24"),("U1","10")],
    "I2C_SDA":  [("J1","3"),("U2","6")],
    "I2C_SCL":  [("J1","5"),("U2","4")],
    "WIND_SPD": [("J1","29"),("J2","2")],
    "RAIN":     [("J1","31"),("J3","2")],
    "WIND_DIR": [("J2","4"),("R1","1"),("U1","1")],
}

# ---------------------------------------------------------------- build
lib_blocks, pin_map = {}, {}
for ref, (lib, sym, val, sx, sy, rdy) in PARTS.items():
    blk = load_symbol(lib, sym)
    lib_id = f"{lib}:{sym}"
    if lib_id not in lib_blocks:
        # rename the block to the fully-qualified lib_id for the lib_symbols section
        lib_blocks[lib_id] = blk.replace(f'(symbol "{sym}"', f'(symbol "{lib_id}"', 1)
    pin_map[ref] = {num: (x, y, ang, nm) for num, nm, x, y, ang in symbol_pins(blk)}

connected = {(r, p) for pins in NETS.values() for (r, p) in pins}

SHEET_UUID = U()
out = ['(kicad_sch (version 20231120) (generator "gerber_recovery") (generator_version "8.0")',
       f'  (uuid "{SHEET_UUID}")', '  (paper "A4")', '  (lib_symbols']
for lib_id, blk in lib_blocks.items():
    out.append("    " + blk)
out.append("  )")

def abs_pin(ref, num):
    sx, sy = PARTS[ref][3], PARTS[ref][4]
    px, py, ang, _ = pin_map[ref][num]
    return sx + px, sy - py          # symbol Y is up, sheet Y is down

for ref, (lib, sym, val, sx, sy, rdy) in PARTS.items():
    out.append(f'  (symbol (lib_id "{lib}:{sym}") (at {sx} {sy} 0) (unit 1)')
    out.append('    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)')
    out.append(f'    (uuid "{U()}")')
    out.append(f'    (property "Reference" "{ref}" (at {sx} {sy - rdy - 2.54} 0)'
               f' (effects (font (size 1.27 1.27))))')
    out.append(f'    (property "Value" "{val}" (at {sx} {sy - rdy} 0)'
               f' (effects (font (size 1.27 1.27))))')
    for num in pin_map[ref]:
        out.append(f'    (pin "{num}" (uuid "{U()}"))')
    out.append('    (instances (project "RPi_Zero_pHat_recovered"')
    out.append(f'      (path "/{SHEET_UUID}" (reference "{ref}") (unit 1))))')
    out.append("  )")

# labels + short wire stubs, and no-connects on unwired pins
for net, pins in NETS.items():
    for (ref, num) in pins:
        x, y = abs_pin(ref, num)
        px = pin_map[ref][num][0]
        dx = 5.08 if px >= 0 else -5.08          # stub away from the symbol body
        out.append(f'  (wire (pts (xy {x} {y}) (xy {x + dx} {y}))'
                   f' (stroke (width 0) (type default)) (uuid "{U()}"))')
        just = "left" if dx > 0 else "right"
        out.append(f'  (label "{net}" (at {x + dx} {y} 0) (fields_autoplaced yes)'
                   f' (effects (font (size 1.27 1.27)) (justify {just} bottom)) (uuid "{U()}"))')

for ref in PARTS:
    for num in pin_map[ref]:
        if (ref, num) not in connected:
            x, y = abs_pin(ref, num)
            out.append(f'  (no_connect (at {x} {y}) (uuid "{U()}"))')

out.append(')')
open(OUT, "w", encoding="utf-8").write("\n".join(out))
print("wrote", OUT)
print("parts:", len(PARTS), " nets:", len(NETS),
      " connected pins:", len(connected),
      " no-connects:", sum(1 for r in PARTS for n in pin_map[r] if (r, n) not in connected))
