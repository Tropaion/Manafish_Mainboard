#!/usr/bin/env python3
"""JLCPCB production files for manafish.kicad_pcb: Gerber zip, BOM and CPL.

Run with the Python that ships with KiCad 10 (it needs the pcbnew module), with KiCad closed:

    python3 jlc_export.py

Zones are refilled while plotting (kicad-cli --check-zones); the board file itself is not changed.

Parts come from the "LCSC Part #" field on the board footprints, so update the PCB from the schematic (F8)
after changing that field. Footprints without that field, excluded from BOM or position files, or DNP
are left out of BOM and CPL, and the script lists them.

CPL positions and rotations follow JLCPCB's own footprints (from the EasyEDA library), not KiCad's.
CORRECTIONS holds, per KiCad footprint, the rotation to add and the centre offset in footprint coordinates,
found by matching every pad of the KiCad footprint against JLCPCB's footprint of the listed LCSC part.
A footprint not in the table gets no correction, which is right for KiCad's own SMD passives.
JLCPCB's footprint belongs to the LCSC part, so the script warns when a corrected footprint carries a
different LCSC number: check that part's rotation in JLCPCB's placement preview.
"""
import csv, math, os, re, shutil, subprocess, sys, tempfile, zipfile
from collections import OrderedDict

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
BOARD = os.path.join(HERE, '..', 'manafish.kicad_pcb')
NAME = 'manafish'
FIELD = 'LCSC Part #'

# KiCad footprint name: (rotation to add in degrees, centre offset x, y in mm in footprint coordinates
# with KiCad's y pointing down, LCSC part the correction was measured for)
CORRECTIONS = {
    'CP_Radial_D10.0mm_P5.00mm':                                 (0,   2.5,    0.0,    'C136284'),   # C20
    'USON-10_2.5x1.0mm_P0.5mm':                                  (270, 0.0,    0.0,    'C90627'),    # IC1, IC2
    'BMI270':                                                    (180, 0.0,    0.0,    'C2836813'),  # IC11
    'Vishay_SOP-4_P2.54mm_VOM1271T':                             (270, 0.0,    0.0,    'C146286'),   # IC16
    'SMDIP-4_W9.53mm_Clearance8mm':                              (270, 0.0,    0.0,    'C109227'),   # IC6, IC7
    'QFN-28-1EP_4x5mm_P0.5mm_EP2.65x3.65mm_ThermalVias0.3mm':    (270, 0.0,    0.0,    'C3681528'),  # IC8
    'ONSemi_SO-8FL_488AA':                                       (270, -0.962, 0.0,    'C604432'),   # Q3
    'FDMC86139P':                                                (0,   -0.061, 0.005,  'C890957'),   # Q1, Q2
    'JST_GH_SM04B-GHS-TB_1x04-1MP_P1.25mm_Horizontal':           (0,   0.0,    -0.25,  'C189895'),   # J1
    'JST_GH_SM03B-GHS-TB_1x03-1MP_P1.25mm_Horizontal':           (0,   0.0,    -0.175, 'C514175'),   # J12
    'jst_sm03b-srss-tb-lf--sn-':                                 (0,   0.0,    -2.637, 'C160403'),   # J2
    'JST_GH_BM05B-GHS-TBT_1x05-1MP_P1.25mm_Vertical':            (180, 0.0,    0.275,  'C189891'),   # J3, J4
    'Molex_501951-2230_1x22-1MP_P0.50mm_Vertical_RPi-CSI':       (180, 0.0,    0.0,    'C6819364'),  # J5, J11
    'USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal':   (0,   0.0,    -1.308, 'C3020560'),  # J8
    'PinHeader_2x06_P1.27mm_Vertical':                           (270, 0.635,  3.175,  'C22438121'), # J13
    'PinHeader_2x07_P1.27mm_Vertical':                           (270, 0.635,  3.81,   'C22438122'), # J7
    'PinHeader_2x10_P1.27mm_Vertical':                           (270, 0.635,  5.715,  'C22438125'), # J9
    'AMASS_XT60PW-M_1x02_P7.20mm_Horizontal':                    (270, 3.6,    -3.0,   'C98732'),    # J6
    'AMASS_XT60PW-F_1x02_P7.20mm_Horizontal':                    (0,   -3.6,   -3.0,   'C428722'),   # J14
}

# One KiCad footprint, several JLCPCB parts: the CM5 footprint holds both 100-pin connectors.
# KiCad footprint name: (comment, LCSC part, [(designator suffix, rotation to add, centre x, y in footprint coordinates)])
SPLIT = {
    'Raspberry-Pi-5-Compute-Module': ('10164227-1001A1RLF', 'C6782225',
                                      [('A', 270, -0.5, -21.5), ('B', 270, 33.5, -21.5)]),
}

GERBER_LAYERS = 'F.Cu,In1.Cu,In2.Cu,B.Cu,F.Mask,B.Mask,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,Edge.Cuts'


def natural(s):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', s)]


def field(fp, name):
    return fp.GetFieldText(name).strip() if fp.HasField(name) else ''


def place(fp, rot_add, ox, oy):
    """Board position (mm, KiCad y down) of footprint-local point (ox, oy) and the JLCPCB rotation."""
    pos = fp.GetPosition()
    a = math.radians(fp.GetOrientationDegrees())
    # KiCad rotates counter-clockwise on screen; with y pointing down that is (x, y) -> (x cos + y sin, -x sin + y cos)
    x = pcbnew.ToMM(pos.x) + ox * math.cos(a) + oy * math.sin(a)
    y = pcbnew.ToMM(pos.y) - ox * math.sin(a) + oy * math.cos(a)
    return x, y, round(fp.GetOrientationDegrees() + rot_add, 1) % 360


def plot(tmp):
    subprocess.run(['kicad-cli', 'pcb', 'export', 'gerbers', '-o', tmp + os.sep, '-l', GERBER_LAYERS,
                    '--subtract-soldermask', '--no-x2', '--no-netlist', '--check-zones', BOARD],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['kicad-cli', 'pcb', 'export', 'drill', '-o', tmp + os.sep, '--format', 'excellon',
                    '--excellon-units', 'mm', '--excellon-separate-th', '--excellon-zeros-format', 'decimal',
                    '--excellon-oval-format', 'alternate', BOARD], check=True, stdout=subprocess.DEVNULL)
    zpath = os.path.join(tmp, 'gerber.zip')
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(tmp)):
            if name != 'gerber.zip':
                z.write(os.path.join(tmp, name), name)
    return zpath


def main():
    board = pcbnew.LoadBoard(BOARD)
    bom = OrderedDict()
    cpl = []
    skipped = []
    warnings = []
    for fp in sorted(board.GetFootprints(), key=lambda f: natural(f.GetReference())):
        ref = fp.GetReference()
        attrs = fp.GetAttributes()
        if attrs & pcbnew.FP_BOARD_ONLY:
            continue
        code = field(fp, FIELD)
        if not code or fp.IsDNP() or attrs & (pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES):
            skipped.append(ref)
            continue
        if fp.IsFlipped():
            sys.exit(f'{ref} is on the bottom: the corrections here are for top-side parts only')
        fpname = str(fp.GetFPID().GetUniStringLibItemName())
        if fpname in SPLIT:
            comment, split_code, parts = SPLIT[fpname]
            if code != split_code:
                warnings.append(f'{ref}: {code}, but the split was measured for {split_code}')
            refs = []
            for suffix, rot_add, ox, oy in parts:
                cpl.append((ref + suffix,) + place(fp, rot_add, ox, oy))
                refs.append(ref + suffix)
        else:
            rot_add, ox, oy, measured = CORRECTIONS.get(fpname, (0, 0.0, 0.0, None))
            if measured and code != measured:
                warnings.append(f'{ref}: {code}, but the {fpname} correction was measured for {measured}')
            cpl.append((ref,) + place(fp, rot_add, ox, oy))
            refs = [ref]
            comment = fp.GetValue()
        line = bom.setdefault((code, fpname), {'refs': [], 'values': set(), 'mpns': set()})
        line['refs'] += refs
        line['values'].add(comment)
        line['mpns'].add(field(fp, 'MPN'))

    with tempfile.TemporaryDirectory() as tmp:
        zpath = plot(tmp)
        shutil.copyfile(zpath, os.path.join(HERE, f'{NAME}_gerber_JLCPCB.zip'))

    with open(os.path.join(HERE, f'{NAME}_BOM_JLCPCB.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['Comment', 'Designator', 'Footprint', FIELD])
        for (code, fpname), line in sorted(bom.items(), key=lambda kv: natural(kv[1]['refs'][0])):
            values, mpns = sorted(line['values']), sorted(line['mpns'])
            if len(values) == 1:
                comment = values[0]
            elif len(mpns) == 1 and mpns[0]:
                comment = mpns[0]   # same part under different values, e.g. J3 "AM32_1" and J4 "AM32_2"
            else:
                comment = ' / '.join(values)
            w.writerow([comment, ','.join(sorted(line['refs'], key=natural)), fpname, code])

    with open(os.path.join(HERE, f'{NAME}_CPL_JLCPCB.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['Designator', 'Mid X', 'Mid Y', 'Layer', 'Rotation'])
        for ref, x, y, r in sorted(cpl, key=lambda c: natural(c[0])):
            # Same origin as the Gerbers (KiCad absolute), y up as in the Gerbers
            w.writerow([ref, f'{x:.4f}mm', f'{-y:.4f}mm', 'Top', f'{r:.1f}'])

    print(f'BOM: {len(bom)} lines, CPL: {len(cpl)} placements')
    print('Not in BOM/CPL (no LCSC Part #, DNP or excluded):', ', '.join(skipped) or 'none')
    for w_ in warnings:
        print('Check rotation:', w_)


if __name__ == '__main__':
    main()
