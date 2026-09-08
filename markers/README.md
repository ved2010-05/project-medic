# markers/ — canonical ArUco source (reprint ONLY from here)

Never generate markers from a phone photo or a random web image. This folder is the single source; regenerate the PDFs from the script and reprint from the PDFs.

## Spec (frozen)
- Dictionary: `cv2.aruco.DICT_4X4_50` (small, robust, IDs 0–49).
- Printed size: **140 mm** black square (design doc allows 120–150). White quiet zone ≥ 1 marker cell is added automatically — the printed white block is **187 mm**.
- Print **matte** (glossy = glare = missed detections). Mount rigidly, flat, at roughly camera height.

## Paper
**A4 portrait, one marker per page, printed at 100%.**

| Marker edge | White block | Fits A4? |
|---|---|---|
| 140 mm (default) | 187 mm | yes, ~11 mm margin each side |
| 120 mm (`--mm 120`) | 160 mm | yes, comfortably |
| 200 mm | 267 mm | **no** — use `--paper a3` |

A4 is 210 × 297 mm. The script refuses to build a PDF that can't print at true scale and tells you the largest `--mm` that fits, so you find out here rather than at the print shop.

**In the print dialog: Scale = 100% / "Actual size".** Not "Fit to page", not "Shrink oversized pages". `nav.py` judges distance from apparent marker width, so a rescaled print makes the robot stop at the wrong place.

If you only have 120 mm markers, detection range drops roughly in proportion (~2.5 m instead of ~3 m on the Pi camera) — fine indoors, just put markers a bit closer together.

## Generating
```bash
python markers/generate_markers.py --check                    # the four defaults
python markers/generate_markers.py --check --ids "10:PHARMACY,11:CORNER-1,22:ROOM 4B"
python markers/generate_markers.py --paper a3 --mm 220        # big markers for a long hall
```
`--check` re-detects every marker from its own rendered image. **If it does not report all-valid, do not print.**

Output lands in `markers/out/`: `medic-markers.pdf` (print this) plus one PNG per marker for slides.

A bare ID with no name (`--ids "10,11,12"`) prints a placeholder label — the real name is assigned later on the dashboard `/map` page during the teach run.

## ID assignment
IDs are just labels; the *names* live in the learned map (dashboard `/map`), not in the code. This scheme keeps them readable:

| Range | Meaning |
|---|---|
| 10 | Pharmacy station (start/return point) |
| 11–14 | Corridor / corner waypoints |
| 15 | Mid-route waypoint (the original demo course) |
| 20 | Ward station |
| 21–29 | Room stations (one on the wall beside each door) |
| 30 | Escort tag — follow-mode stretch goal, not in the demo |

Only 10, 15 and 20 are referenced by the built-in `ROUTE_TABLE` fallback in the dashboard. Everything else is learned.

## Placement rules
- **Every station gets one marker** on the wall facing the approach, at camera height.
- **Every turn gets one marker**, angled toward the direction the robot arrives from.
- **Two markers must be visible from anywhere on the route.** That overlap is what the teach run records as a link, and what lets the robot chain hops instead of needing to see its destination from the start.

## nav dependency
`pi/nav.py` uses the calibration-free path (steer by horizontal pixel offset, stop by apparent marker width). `solvePnP` pose is a fallback only — it would need the real marker edge length and a camera calibration (`pi/calib.npz`).

**Measure a printed marker with a ruler and confirm it really is 140 mm.** If your printer is off, record the true number here; everything downstream trusts it.
