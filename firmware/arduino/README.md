# Flashing with the Arduino IDE

`firmware/src/` is the source of truth. `medic_fw/` is a **generated copy** —
the Arduino IDE requires a sketch folder containing a `.ino` named after the
folder, which PlatformIO doesn't. The only difference is `main.cpp` renamed to
`medic_fw.ino`.

**Edit `firmware/src/`, then regenerate:**

```bash
bash firmware/arduino/sync-from-src.sh
```

Never edit `medic_fw/` directly — the next sync overwrites it.

---

## One-time IDE setup

**Tools → Board → esp32 → "ESP32 Dev Module"**
(Board package `esp32` **3.3.10** is already installed. Don't pick a variant
board like "DOIT ESP32 DEVKIT V1" unless that's literally your board — Dev
Module is the safe generic choice.)

Then set:

| Setting | Value | Why |
|---|---|---|
| **Port** | COM5 | your CH9102 USB-serial bridge |
| **Upload Speed** | **115200** | ← change this from the 921600 default |
| Flash Frequency | 80 MHz | default |
| Partition Scheme | Default 4MB | sketch is only 23% of it |

**No libraries to install.** The servo is driven straight from the ESP32's LEDC
peripheral in `payload.cpp`, so there is no ESP32Servo dependency. If the IDE
ever asks you to install a library for this sketch, something is wrong —
check you opened `medic_fw.ino` and not a stray copy.

## Open and flash

1. **File → Open…** →
   `firmware\arduino\medic_fw\medic_fw.ino`
   (all the `.cpp`/`.h` files appear as tabs — that's correct)
2. **Close the Serial Monitor if it's open.** It holds the port and is the most
   common cause of a failed upload.
3. ✓ **Verify** first. It should report roughly:
   `Sketch uses 309551 bytes (23%) … Global variables use 23368 bytes (7%)`
4. → **Upload**.

## If upload fails

The exact error seen on this machine was:

```
A fatal error occurred: Failed to connect to ESP32: Download mode successfully
detected, but getting no sync reply: The serial TX path seems to be down.
```

That means the ESP32 *did* enter bootloader mode (so the board and cable are
fine and the PC can hear it) but it isn't hearing the PC. In order of likelihood:

1. **Upload Speed still at 921600.** Set it to 115200. CH9102 bridges are
   frequently unreliable at the top speed. This is the first thing to try.
2. **Something else owns the port** — Serial Monitor, a second IDE window, a
   PlatformIO monitor, or a `screen`/PuTTY session. Close them all.
3. **Hold the BOOT button.** Press and hold BOOT, tap EN/RST, release EN, keep
   BOOT held until "Connecting…" turns into "Writing…", then let go.
4. **Try a different USB cable and port.** Charge-only cables enumerate power
   but not data reliably. Prefer a rear/motherboard USB port over a hub.

## Before the first flash — safety

Put the **wheels off the ground**. The firmware is designed to boot safe: it
starts in `SAFEHOLD_COMMS` (nothing has talked to it yet) and `Drive::begin()`
zeroes both motor outputs, so it should not move. Verify that rather than
trust it.

The **magazine servo will sweep to `SERVO_HOME_DEG` on boot**, which is
intended — it parks the disk closed. If the magazine is loaded and your HOME
angle is wrong, that first sweep can drop a unit. Flash with the magazine empty
the first time.

## First thing after flashing

Serial Monitor, **115200 baud**, **line ending: Newline** (the protocol is
newline-delimited JSON — with "No line ending" nothing you type will ever parse).

Within ~2 s of boot you should see repeating state frames:

```json
{"t":"state","s":"IDLE_AT_PHARMACY","ovr":"SAFEHOLD_COMMS","since_ms":1000}
```

`SAFEHOLD_COMMS` is **correct** here — no Pi is talking to it yet, so the robot
refuses to move. That is the comms watchdog doing its job, not a fault.

Then type:

```json
{"t":"ping"}
```

You should get exactly `{"t":"pong"}` back, and the override should clear.
That round trip proves the whole serial link end to end.

See the `BENCH TEST:` block at the top of each `.cpp` for what to exercise next.
