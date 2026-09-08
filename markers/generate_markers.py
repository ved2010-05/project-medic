"""generate_markers.py — THE canonical source for Project MEDIC's ArUco markers.

markers/ is the single source of truth (see README.md in this folder). Reprint
ONLY from the PDFs this produces. Never regenerate a marker from a phone photo,
a screenshot, or a random web image: a rescaled or slightly-cropped marker still
"looks like" an ArUco tag but detects badly at range and at angle, which then
gets blamed on nav.py for a whole afternoon.

WHAT IT MAKES
    markers/out/medic-markers.pdf      print this — one marker per page, exact scale
    markers/out/marker-<id>-<name>.png at the chosen DPI, for slides/docs

Each page is deliberately bare: the human-readable NAME and ID across the top,
the marker below it, nothing else. Every extra mark near a marker is another
thing the detector can trip on. Pass --print-notes if you want the scale and
paper reminders printed at the foot of each page as a check for whoever prints
them; the same warnings are on stdout either way.

USAGE
    python markers/generate_markers.py --check
    python markers/generate_markers.py --ids 10:PHARMACY,15:CORNER-A,22:ROOM-4B
    python markers/generate_markers.py --paper letter --mm 120

PRINTING RULES THAT ACTUALLY MATTER
    * PRINT AT 100% / "Actual size". Any "fit to page" or "shrink to margins"
      silently rescales the marker and makes the printed edge length a lie —
      and nav.py's standoff is measured in apparent marker WIDTH, so a wrong
      physical size means the robot stops at the wrong distance.
    * PRINT MATTE. Glossy paper + overhead lighting = specular glare across the
      marker = missed detections. This is the single biggest ArUco killer
      indoors (docs/design-doc-v0.3.md §4.1).
    * Mount FLAT and RIGID on board/card. A curled or flapping marker changes
      its apparent width frame to frame, which reads as the robot moving.
    * Measure the printed edge with a ruler afterwards and write the real number
      into README.md. Everything downstream depends on it being true.

BENCH TEST:
    python markers/generate_markers.py --check
  Generates, then re-detects every marker from its own rendered image and
  reports the decoded ID. If --check does not report all-valid, do not print.
"""

import argparse
import os
import re
import sys

import cv2
import numpy as np

# markers/README.md — FROZEN. Do not change these without changing that file,
# pi/nav.py's ARUCO_DICT, and reprinting every marker on the course.
DICT_NAME = "DICT_4X4_50"
DICT_MAX_ID = 50  # DICT_4X4_50 holds IDs 0..49

# Default course set. Override with --ids for extra rooms/corners once the
# teach run (dashboard /map) tells you how many you actually need.
DEFAULT_MARKERS = [
    (10, "PHARMACY"),
    (20, "WARD"),
    (15, "WAYPOINT"),
    (30, "ESCORT"),
]

DEFAULT_MM = 140.0   # design doc says 120-150 mm; 140 is a good middle
DEFAULT_DPI = 300.0
QUIET_CELLS = 1      # white border, in marker cells (README: >= 1 cell)
GRID_CELLS = 4       # DICT_4X4_50 -> 4x4 data cells...
BORDER_CELLS = 1     # ...plus ArUco's own 1-cell black border on each side
TOTAL_CELLS = GRID_CELLS + 2 * BORDER_CELLS  # = 6 cells across the black square

# Paper sizes in inches, portrait. The printable area is smaller than the sheet;
# PRINT_MARGIN_IN is a conservative allowance for the printer's own dead border.
PAPERS = {
    "a4": (8.27, 11.69),
    "letter": (8.5, 11.0),
    "a3": (11.69, 16.54),
}
PRINT_MARGIN_IN = 0.25


def get_dictionary():
    """Works on both the modern and legacy cv2.aruco APIs."""
    d = getattr(cv2.aruco, DICT_NAME)
    if hasattr(cv2.aruco, "getPredefinedDictionary"):
        return cv2.aruco.getPredefinedDictionary(d)
    return cv2.aruco.Dictionary_get(d)  # OpenCV < 4.7


def draw_marker(dictionary, marker_id, px):
    """Render one marker as a px-by-px greyscale image."""
    if hasattr(cv2.aruco, "generateImageMarker"):
        return cv2.aruco.generateImageMarker(dictionary, marker_id, px)
    return cv2.aruco.drawMarker(dictionary, marker_id, px)  # legacy name


def make_detector(dictionary):
    if hasattr(cv2.aruco, "ArucoDetector"):
        det = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
        return lambda img: det.detectMarkers(img)
    params = cv2.aruco.DetectorParameters_create()
    return lambda img: cv2.aruco.detectMarkers(img, dictionary, parameters=params)


def mm_to_px(mm, dpi):
    return int(round(mm / 25.4 * dpi))


def slug(name):
    """Filename-safe version of a human label."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return s or "marker"


def parse_ids(spec):
    """--ids "10:PHARMACY,15,22:ROOM 4B" -> [(10,'PHARMACY'),(15,'ID-15'),...]

    A bare number gets a placeholder name; you can still tell the sheets apart
    by the big ID, and the real name is assigned on the dashboard /map page.
    """
    out, seen = [], set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            raw_id, name = chunk.split(":", 1)
        else:
            raw_id, name = chunk, ""
        raw_id = raw_id.strip()
        if not raw_id.isdigit():
            raise ValueError("bad marker id %r (expected a number, or ID:NAME)" % chunk)
        mid = int(raw_id)
        if not 0 <= mid < DICT_MAX_ID:
            raise ValueError("marker id %d is outside %s (valid 0-%d)"
                             % (mid, DICT_NAME, DICT_MAX_ID - 1))
        if mid in seen:
            raise ValueError("marker id %d listed twice" % mid)
        seen.add(mid)
        name = name.strip().upper() or ("ID-%d" % mid)
        out.append((mid, name))
    if not out:
        raise ValueError("--ids was empty")
    return out


def build_png(dictionary, marker_id, mm, dpi):
    """Marker at exact physical scale, with its white quiet zone.

    Returns (canvas, black_square_mm, canvas_mm). The two measurements matter
    separately: the DETECTOR sees the black square, the PRINTER lays out the
    whole canvas. Confusing them is how a marker ends up 3/4 of its stated size.
    """
    # The printed edge length refers to the BLACK SQUARE (what a detector sees),
    # so the quiet zone is added OUTSIDE it. Getting this backwards makes every
    # printed marker ~33% smaller than intended.
    marker_px = mm_to_px(mm, dpi)
    # Snap to a whole number of cells so no cell is a fractional pixel — a
    # half-pixel cell edge is exactly what makes a marker fail at an angle.
    cell_px = max(1, int(round(marker_px / float(TOTAL_CELLS))))
    marker_px = cell_px * TOTAL_CELLS
    quiet_px = cell_px * QUIET_CELLS

    marker = draw_marker(dictionary, marker_id, marker_px)
    canvas_px = marker_px + 2 * quiet_px
    canvas = np.full((canvas_px, canvas_px), 255, dtype=np.uint8)
    canvas[quiet_px:quiet_px + marker_px, quiet_px:quiet_px + marker_px] = marker

    black_mm = marker_px / dpi * 25.4   # after cell snapping
    canvas_mm = canvas_px / dpi * 25.4
    return canvas, black_mm, canvas_mm


def main():
    ap = argparse.ArgumentParser(description="Generate Project MEDIC ArUco markers")
    ap.add_argument("--mm", type=float, default=DEFAULT_MM,
                    help="printed edge of the BLACK SQUARE in mm (default %.0f)"
                         % DEFAULT_MM)
    ap.add_argument("--dpi", type=float, default=DEFAULT_DPI)
    ap.add_argument("--paper", default="a4", choices=sorted(PAPERS),
                    help="page size for the PDF (default a4)")
    ap.add_argument("--ids", default=None,
                    help='markers to make, e.g. "10:PHARMACY,15:CORNER-A,22". '
                         "Default is the four course markers.")
    ap.add_argument("--print-notes", action="store_true",
                    help="add the scale/matte reminder line to the foot of each "
                         "page (off by default — the pages are kept bare)")
    ap.add_argument("--out", default=None, help="output dir (default markers/out)")
    ap.add_argument("--check", action="store_true",
                    help="re-detect every generated marker to prove it is valid")
    args = ap.parse_args()

    try:
        markers = parse_ids(args.ids) if args.ids else list(DEFAULT_MARKERS)
    except ValueError as exc:
        print("error: %s" % exc)
        return 2

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = args.out or os.path.join(here, "out")
    os.makedirs(outdir, exist_ok=True)

    dictionary = get_dictionary()
    print("dictionary : %s" % DICT_NAME)
    print("target edge: %.1f mm at %.0f DPI" % (args.mm, args.dpi))
    print("paper      : %s" % args.paper.upper())
    print()

    images, black_mm, canvas_mm = {}, None, None
    for mid, name in markers:
        img, black_mm, canvas_mm = build_png(dictionary, mid, args.mm, args.dpi)
        path = os.path.join(outdir, "marker-%02d-%s.png" % (mid, slug(name)))
        cv2.imwrite(path, img)
        images[mid] = img
        print("  ID %-3d %-14s %s" % (mid, name, os.path.basename(path)))

    print()
    print("black square prints at : %.2f mm" % black_mm)
    print("whole sheet block      : %.2f mm (marker + quiet zone)" % canvas_mm)
    if abs(black_mm - args.mm) > 0.5:
        print("  (snapped from %.1f mm so every cell is a whole number of pixels)"
              % args.mm)

    # ---- fits-on-the-paper check ------------------------------------------
    # Worth doing here rather than discovering it at the print shop: if the
    # block is wider than the printable area, the driver will "helpfully"
    # shrink it and the physical edge length silently stops being true.
    page_w_in, page_h_in = PAPERS[args.paper]
    block_in = canvas_mm / 25.4
    usable_w = page_w_in - 2 * PRINT_MARGIN_IN
    if block_in > usable_w:
        need_mm = usable_w * 25.4 * TOTAL_CELLS / (TOTAL_CELLS + 2 * QUIET_CELLS)
        print()
        print("*** TOO BIG FOR %s ***" % args.paper.upper())
        print("    block is %.0f mm wide, printable width is about %.0f mm."
              % (canvas_mm, usable_w * 25.4))
        print("    Use --mm %.0f, or --paper a3." % (need_mm - 1))
        return 1
    print("fits %s with %.0f mm to spare each side."
          % (args.paper.upper(), (usable_w - block_in) * 25.4 / 2))

    # ---- print-ready PDF, one marker per page, at true scale ---------------
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages
    except ImportError:
        print("\nmatplotlib not installed — PNGs written, PDF skipped.")
        print("  pip install matplotlib   (or print the PNGs at %.0f DPI)" % args.dpi)
        return 0

    pdf_path = os.path.join(outdir, "medic-markers.pdf")
    with PdfPages(pdf_path) as pdf:
        for mid, name in markers:
            fig = plt.figure(figsize=(page_w_in, page_h_in))

            # Lay out the WHOLE CANVAS (marker + quiet zone), because that is
            # what imshow fills. Sizing the axes to the black square instead
            # would print the marker at 6/8 of its stated edge.
            side_in = canvas_mm / 25.4
            left = (page_w_in - side_in) / 2.0 / page_w_in
            w = side_in / page_w_in
            h = side_in / page_h_in
            bottom = (1.0 - h) / 2.0 - 0.04   # nudged down; the label sits above
            axm = fig.add_axes([left, bottom, w, h])
            axm.imshow(images[mid], cmap="gray", interpolation="nearest",
                       vmin=0, vmax=255)
            axm.axis("off")

            # Name big, ID big — readable from across the room so a human can
            # place the right sheet on the right wall without squinting.
            fig.text(0.5, bottom + h + 0.055, name,
                     ha="center", size=44, weight="bold")
            fig.text(0.5, bottom + h + 0.022, "ID %d" % mid,
                     ha="center", size=26, color="#333333")

            # Off by default: the sheets go on a wall and read better bare.
            if args.print_notes:
                fig.text(0.5, 0.035,
                         "%s  ·  black square = %.0f mm  ·  PRINT AT 100%% "
                         "(never 'fit to page')  ·  matte paper"
                         % (DICT_NAME, black_mm),
                         ha="center", size=9, color="#666666")

            pdf.savefig(fig)
            plt.close(fig)

    print("PDF        : %s  (%d page%s)"
          % (pdf_path, len(markers), "" if len(markers) == 1 else "s"))

    # ---- verify ------------------------------------------------------------
    if args.check:
        print()
        print("--check: re-detecting each generated marker")
        detect = make_detector(dictionary)
        ok = 0
        for mid, name in markers:
            corners, ids, _ = detect(images[mid])
            found = [] if ids is None else [int(i) for i in ids.flatten()]
            good = found == [mid]
            ok += 1 if good else 0
            print("  ID %-3d %-14s -> decoded %s   %s"
                  % (mid, name, found or "NOTHING", "OK" if good else "*** FAIL ***"))
        print("  %d/%d valid" % (ok, len(markers)))
        if ok != len(markers):
            print("  DO NOT PRINT — regenerate first.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
