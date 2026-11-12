# JLCPCB production data

Generated 2026-11-12 from `manafish.kicad_pcb` with KiCad 10.0.8 by `jlc_export.py`.

| File | Use |
| --- | --- |
| `manafish_gerber_JLCPCB.zip` | Gerbers (4 copper layers, mask, paste, silkscreen, outline) + Excellon drill (PTH and NPTH separate) |
| `manafish_BOM_JLCPCB.csv` | Assembly BOM: Comment, Designator, Footprint, LCSC Part # |
| `manafish_CPL_JLCPCB.csv` | Pick-and-place: Designator, Mid X, Mid Y, Layer, Rotation |
| `jlc_export.py` | Writes the three files above |

## Regenerating

1. In KiCad, update the PCB from the schematic (F8) and save. Close KiCad.
2. Optional, after layout changes: update the teardrops and zone fills and save them into the board with
   `kicad-cli pcb drc --refill-zones --save-board -o drc.rpt ../manafish.kicad_pcb`. The refill rebuilds
   every teardrop, the same as Refill All Zones in KiCad.
3. Run `python3 jlc_export.py` in this folder.
4. Optional: regenerate the interactive BOM `../bom/ibom.html` with the InteractiveHtmlBom plugin, using
   KiCad's Python and the settings of the published file:

   ```text
   generate_interactive_bom.py --no-browser --dark-mode --show-pads --show-fabrication --show-silkscreen
     --highlight-pin1 selected --checkboxes "Sourced,Placed" --bom-view left-right --layer-view FB
     --show-fields "Value,Footprint,LCSC,LCSC Part #,Mouser,Mouser PN" --include-tracks --include-nets
     --dest-dir bom --name-format ibom ../manafish.kicad_pcb
   ``` It needs KiCad's Python module `pcbnew`. It refills the
   zones while plotting, so the Gerbers never carry a stale fill; the board file is not changed.

The BOM takes each part from its footprint's `LCSC Part #` field, the field name JLCPCB asks for.
Footprints without that field are left out, and the script lists them. If the field holds an LCSC
product URL instead of the bare `C…` number, the script uses the number from the URL and lists the part.

The CPL is corrected for JLCPCB's footprints. JLCPCB places each part with the footprint from its own
library (EasyEDA), whose zero rotation and origin often differ from KiCad's. `jlc_export.py` holds the
rotation and centre offset for every KiCad footprint that needs one. Each value comes from matching
every pad of the KiCad footprint against JLCPCB's footprint for the chosen part. The finished CPL was
checked by placing all JLCPCB footprints from it onto the board pads. If you change a part to one
with a different JLCPCB footprint (connectors, ICs), check its entry.

The CM5 footprint (Module1, Raspberry Pi's footprint from the CM5 IO Board) contains both 100-pin
connectors, so KiCad's own BOM and position files list it once. The BOM and CPL here list them as
Module1 (connector A, pins 1–100) and Module2 (connector B, pins 101–200): two Amphenol
10164227-1001A1RLF (C6782225). With letter suffixes (Module1A/Module1B), JLCPCB's BOM check warns that
the line may hold several part types.

## PCB order settings

- 4 layers, 70 × 109.2 mm, 1.6 mm, stackup JLC04161H-7628, impedance control
- 1 oz outer / 0.5 oz inner copper, ENIG
- Vias 0.45/0.2 mm (smallest hole 0.2 mm), tented
- Layer order: F.Cu, In1 (`GND_Cu.g1`), In2 (`VCC_Cu.g2`), B.Cu

## Assembly notes

- **Not in the BOM/CPL:**
  - IC5 (Ag59824-LPB, bottom side): solder it by hand.
  - IC3 (JXF0-1012NL MagJack): JLCPCB doesn't stock it, so solder it by hand or consign it.
- **Stock at JLCPCB on 2026-11-12:**
  - Out of stock: IC10 WSEN-PADS 2511020213301 (C7174514). Use JLCPCB's parts pre-order or global
    sourcing, or consign it.
  - Low: IC8 LTC4162EUFD (C3681528, 11 left) and D2–D5 V6K100DUHM3 (C5985855, 76 left, 4 per board).
  - IC4 RP2354A (C41378174) is back in stock. There is no substitute; the RP2354B is a different package.
- **Y1** is C5310728 (Yajingxin TX322512M4DBDE2T, 12 MHz 10 pF, ESR 60 Ω), because the ABM8-272-T3
  (C20625731) ran out. It keeps the load (C18/C19 15 pF), pinout and pads, and JLCPCB's footprint has the
  same pad numbering, so its rotation is unchanged. Its ESR is 60 Ω instead of 50 Ω: check on the first
  boards that the oscillator starts reliably, also cold; if it is slow, raise
  `PICO_XOSC_STARTUP_DELAY_MULTIPLIER` in the firmware.
- **No JLCPCB footprint:** JLCPCB's library has no footprint for D2–D5 (V6K100DUHM3/H), IC10, R1, R20 and C21/C24,
  so JLCPCB's engineers place them by hand. The CPL gives their pad centre and KiCad's rotation. Check
  D2–D5 and IC10 for pin 1 in the placement preview.
- **Part choices:** the BOM uses JLCPCB Basic parts where one does the job; otherwise it keeps the
  original part. Exceptions, checked 2026-11-09:
  - C26/C28/C30/C33/C34/C40 stay C432929 (10 µF 50 V X7R 1210). JLCPCB's Basic library has no 1210
    MLCC, and the Basic 1206 parts keep only about a quarter to a half of the capacitance at 24 V.
  - R25 is C25804 (10k 1 %, Basic) since 2026-11-12, against R24's 0.5 %. The +5V worst case widens
    from 5.00–5.23 V to about 4.98–5.25 V, at the CM5's 5.25 V maximum before temperature drift. The
    0.5 % part C706017 (RT0603DRE0710KL) restores the margin.
  - C21/C24 are C3037690 (Fenghua 1812B472K202AT, 4.7 nF 2 kV X7R, flexible termination like the
    original KEMET part). The original C2183044 is out of stock.
  - IC8 is C3681528 (LTC4162**E**UFD-LAD, an approved alternate per the schematic note). The original
    I-grade part C3682003 is out of stock.
  - R21 stays C4201041 (Vishay 63.4k 1 % 0402, Extended): JLCPCB has no Basic 0402 63.4k, and the
    value sets IC8's switching frequency. C38606 (UNI-ROYAL 0402WGF6342TCE) is an equivalent Extended part.
  - C47 is C15850 (10 µF 25 V **X5R** 0805, Basic). The X7R part C16194871 had only 4 in stock on
    2026-11-12. X5R is rated to 85 °C, like C42/C44/C45.
- **Through-hole parts:** C20, J6, J7, J9, J13 and J14 are in the BOM. Deselect them in JLCPCB's BOM
  step if you solder them yourself.
- Check the placement preview before ordering, especially the connectors and the polarized parts
  (D1, D6, D7, C20, C39).
