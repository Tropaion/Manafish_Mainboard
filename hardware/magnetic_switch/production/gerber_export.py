#!/usr/bin/env python3
"""JLCPCB Gerber zip for "Magnetic Switch.kicad_pcb" (bare PCB, no assembly).

Needs kicad-cli from KiCad 10 on the PATH (or in the default install folder):

    python3 gerber_export.py

Zones are refilled while plotting (--check-zones); the board file itself is not changed.
"""
import os, shutil, subprocess, tempfile, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BOARD = os.path.join(HERE, '..', 'Magnetic Switch.kicad_pcb')
NAME = 'magnetic_switch'
LAYERS = 'F.Cu,B.Cu,F.Mask,B.Mask,F.Paste,F.Silkscreen,B.Silkscreen,Edge.Cuts'


def kicad_cli():
    found = shutil.which('kicad-cli')
    if found:
        return found
    default = r'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe'
    if os.path.exists(default):
        return default
    raise SystemExit('kicad-cli not found: add KiCad\'s bin folder to PATH')


def main():
    cli = kicad_cli()
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([cli, 'pcb', 'export', 'gerbers', '-o', tmp + os.sep, '-l', LAYERS,
                        '--subtract-soldermask', '--no-x2', '--no-netlist', '--check-zones', BOARD],
                       check=True, stdout=subprocess.DEVNULL)
        subprocess.run([cli, 'pcb', 'export', 'drill', '-o', tmp + os.sep, '--format', 'excellon',
                        '--excellon-units', 'mm', '--excellon-separate-th', '--excellon-zeros-format', 'decimal',
                        '--excellon-oval-format', 'alternate', BOARD], check=True, stdout=subprocess.DEVNULL)
        out = os.path.join(HERE, f'{NAME}_gerber_JLCPCB.zip')
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            for name in sorted(os.listdir(tmp)):
                z.write(os.path.join(tmp, name), name)
                print(name)
    print('Wrote', out)


if __name__ == '__main__':
    main()
