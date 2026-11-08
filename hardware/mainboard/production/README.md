# JLCPCB production data

Generated 2026-11-08 from `manafish.kicad_pcb` (commit e48865c) with KiCad 10.0.8. Exported from the saved board
with filled zones. Re-export after any change: refill zones (B), save, then plot.

| File | Use |
| --- | --- |
| `manafish_gerber_JLCPCB.zip` | Gerbers (4 copper layers, mask, paste, silkscreen, outline) + Excellon drill (PTH and NPTH separate) |
| `manafish_BOM_JLCPCB.csv` | Assembly BOM: Comment, Designator, Footprint, LCSC Part # |
| `manafish_CPL_JLCPCB.csv` | Pick-and-place: Designator, Mid X, Mid Y, Layer, Rotation |

## PCB order settings

- 4 layers, 70 × 109.2 mm, 1.6 mm, stackup JLC04161H-7628, impedance control
- 1 oz outer / 0.5 oz inner copper, ENIG
- Vias 0.45/0.2 mm (smallest hole 0.2 mm), tented
- Layer order: F.Cu, In1 (`GND_Cu.g1`), In2 (`VCC_Cu.g2`), B.Cu

## Assembly notes

- **Not in the BOM/CPL:** IC5 (Ag59824-LPB, bottom side, hand-soldered).
- **No JLCPCB part:** IC3 (JXF0-1012NL MagJack), so supply it yourself or solder it by hand.
- **Out of stock at JLCPCB on 2026-11-08:**
  - IC4 RP2354A (C41378174). There is no substitute; the RP2354B is a different package.
  - IC10 WSEN-PADS 2511020213301 (C7174514).

  Use JLCPCB's parts pre-order or global sourcing, or consign the parts.
- **Changed in the BOM only (the schematic still lists the originals):**
  - IC8 is LTC4162EUFD-LAD#PBF (C3681528) instead of the out-of-stock LTC4162IUFD-LAD#PBF. Same chip and package, E grade instead of I grade.
  - C21/C24 are Yageo CC1812KKX7RDBB472 (C309511), 4.7 nF 2 kV X7R 1812, instead of the out-of-stock KEMET C1812X472KGRACTU. Unlike the original, it has no flexible terminations.
- **Through-hole parts:** C20, J6, J14, J7, J9 and J13 are in the BOM. Deselect them in JLCPCB's BOM step if you solder them yourself.
- **Rotations:** check every part in JLCPCB's placement preview. Their library orientation differs from KiCad's for some packages (QFN, SOT, connectors), and you correct rotations there.
