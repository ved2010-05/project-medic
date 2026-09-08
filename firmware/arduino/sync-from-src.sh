#!/usr/bin/env bash
# Regenerate the Arduino IDE sketch from firmware/src/.
#
# firmware/src/ IS THE SOURCE OF TRUTH. This folder is a generated copy that
# exists only because the Arduino IDE insists a sketch folder contains a .ino
# with the same name as the folder, which PlatformIO does not require.
#
# So: edit firmware/src/*, then run this. Never edit arduino/medic_fw/* directly
# — the next run of this script silently overwrites it, and you will lose the
# change and then spend an hour wondering why your fix "didn't do anything".
#
#   bash firmware/arduino/sync-from-src.sh
#
# The only transformation is main.cpp -> medic_fw.ino. Nothing else changes;
# the Arduino IDE compiles the sibling .cpp/.h files in the sketch folder just
# like PlatformIO compiles src/.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/../src"
DEST="$HERE/medic_fw"

[ -d "$SRC" ] || { echo "ERROR: cannot find $SRC" >&2; exit 1; }

rm -rf "$DEST"
mkdir -p "$DEST"
cp "$SRC"/*.h "$DEST"/
for f in "$SRC"/*.cpp; do
    [ "$(basename "$f")" = "main.cpp" ] && continue
    cp "$f" "$DEST"/
done
cp "$SRC/main.cpp" "$DEST/medic_fw.ino"

echo "synced $(ls "$DEST" | wc -l) files into arduino/medic_fw/"
echo "open this in the Arduino IDE:"
echo "  $DEST/medic_fw.ino"
