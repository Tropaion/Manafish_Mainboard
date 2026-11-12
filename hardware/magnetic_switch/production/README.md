# JLCPCB production data: magnetic switch

Generated 2026-11-12 from `Magnetic Switch.kicad_pcb` with KiCad 10.0.8 by `gerber_export.py`.
Bare PCB only: there is no BOM or CPL, the parts are soldered by hand.

| File | Use |
| --- | --- |
| `magnetic_switch_gerber_JLCPCB.zip` | Gerbers (2 copper layers, mask, paste, silkscreen, outline) + Excellon drill (PTH and NPTH separate) |
| `gerber_export.py` | Writes the zip above |

## Regenerating

Save the board in KiCad, then run `python3 gerber_export.py` in this folder. It needs `kicad-cli`
(KiCad 10) on the PATH or in `C:\Program Files\KiCad\10.0\bin`. It refills the zones while plotting;
the board file is not changed.

## PCB order settings

- 2 layers, 19.05 × 24.75 mm, 1.6 mm FR-4
- Vias 0.3 mm hole, tracks 0.2 mm minimum: JLCPCB's standard capabilities
- One 3.2 mm NPTH mounting hole
- The copper layers are named `Front` and `Back` in KiCad, so the Gerbers are `Magnetic Switch-Front.gtl`
  and `Magnetic Switch-Back.gbl`
