# Deploying and testing eddy-ng changes

This repo has no test suite and cannot run outside a Klipper host — the printer *is* the
test rig. This document is the standard procedure for getting working-tree changes onto
the printer, verifying them, and recording the baseline measurements that
[UPGRADE_PLAN.md](UPGRADE_PLAN.md) uses as acceptance criteria.

Changes fall into three categories with different deployment costs:

| Category | Files | What deployment needs |
| --- | --- | --- |
| Host Python | `probe_eddy_ng.py`, `ldc1612_ng.py` | sync + `sudo systemctl restart klipper` (console `RESTART` does NOT reload code) |
| MCU firmware | `eddy-ng/sensor_ldc1612_ng.c` | sync + rebuild + **reflash the Eddy's RP2040** |
| Installer/infra | `install.py`, `install.sh`, docs | nothing on the printer (used at install time only) |

## 1. Get the changes onto the printer host

The working tree on this machine is the source of truth; nothing is committed until you
decide to. When ready:

```bash
# on this machine (review first: git diff)
git add -A
git commit -m "Audit fix batch: 29 verified bugs + improvements"
git push origin leo-voron-2.4r2-fork
```

```bash
# on the printer host (Pi)
cd ~/eddy-ng
git pull
```

For a quick unreviewed experiment you can instead `scp` the two `.py` files straight into
place, but prefer the git route — it keeps the Pi's checkout identical to what was audited.

If eddy-ng has never been installed on this Klipper tree:

```bash
cd ~/eddy-ng
./install.sh ~/klipper        # symlinks + patches src/Makefile and bed_mesh.py
```

Symlink mode means future `git pull`s take effect on the next Klipper restart with no
reinstall. (The installer now refuses Kalico trees by design — this fork is Klipper-only.)

## 2. Restart and first checks (host-side changes)

1. `sudo systemctl restart klipper` on the Pi. **A console `RESTART` is NOT enough** — klippy
   caches imported extras modules in-process, so `RESTART`/`FIRMWARE_RESTART` re-read the config
   but keep running the old Python code. Only a service restart reloads modules from disk.
2. Watch the console for startup warnings. Two new behaviors to know about:
   - A **corrupt calibration blob no longer crashes startup** — it logs
     `calibration data ... is corrupt or unreadable, please recalibrate` instead. If you see
     that unexpectedly, don't ignore it; recalibrate.
   - `tap_samples > tap_max_samples` in config is now a **startup config error** (it
     previously "worked" and then failed every TAP after 5 wasted bed contacts).
3. `PROBE_EDDY_NG_STATUS` — should report sensor frequency and a plausible height.

## 3. Firmware rebuild and reflash (only when `sensor_ldc1612_ng.c` changed)

The current batch **does** change firmware (SOS bounds check, 64-bit WMA, `is_tap`
predicate fix, descriptive shutdowns, debug wiring), so a reflash is required for those
fixes to be live. Host and firmware stay wire-compatible either way — no constants or
message formats changed — so an unflashed Eddy keeps working with old behavior. None of
the firmware fixes affect tap or homing accuracy, so a reflash is never urgent — do it as
its own step, not in the middle of debugging something else.

**There is no `WANT_EDDY_NG` menuconfig option on Klipper.** `eddy-ng/Kconfig` is the
Kalico plugin path. On Klipper the installer patches `src/Makefile` so
`sensor_ldc1612_ng.c` builds under `CONFIG_WANT_LDC1612` (default enabled). Confirm the
patch survived any Klipper update before building:

```bash
grep sensor_ldc1612_ng ~/klipper/src/Makefile   # must print the patched src-$(CONFIG_WANT_LDC1612) line
```

Build into a separate config file so you don't clobber the `.config` for your main board:

```bash
cd ~/klipper
make menuconfig KCONFIG_CONFIG=eddy.config
#   Micro-controller Architecture: Raspberry Pi RP2040
#   Communication interface: CAN bus (BTT Eddy CAN: RX 4, TX 5, 1000000) or USB (USB Eddy)
#   Bootloader offset: MUST match the device — see below
make clean KCONFIG_CONFIG=eddy.config
make KCONFIG_CONFIG=eddy.config
```

### Bootloader offset must match what is on the device

Getting this wrong produces firmware that will not boot and needs physical BOOT-button
recovery. Check for katapult first:

```bash
python3 ~/katapult/scripts/flashtool.py -i can0 -q     # CAN boards
ls /dev/serial/by-id/                                   # USB boards
```

| Device state | Bootloader offset | Flash with |
| --- | --- | --- |
| katapult installed (CAN) | 16KiB | `flashtool.py -i can0 -u <uuid> -f out/klipper.bin` |
| katapult installed (USB) | 16KiB | `make flash KCONFIG_CONFIG=eddy.config FLASH_DEVICE=<by-id path>` |
| no bootloader | No bootloader | BOOT button + copy `out/klipper.uf2` to the `RPI-RP2` drive |

Stop Klipper before a CAN flash (`sudo service klipper stop`), start it after. The UF2
route always works regardless of offset, but flashing a "No bootloader" build over an
existing katapult erases katapult — after that every future update needs the BOOT button.

Then `FIRMWARE_RESTART` and confirm the MCU version updated (`STATUS` or the MCU section
in Mainsail/Fluidd).

### Behavior change to verify after this reflash

The `is_tap` fix means **amplitude-high sensor errors during plain `G28 Z` homing are now
ignored** (as the original code comment always intended) instead of aborting on the first
one with `max_errors=0`. Watch the first few homes: they should be *more* tolerant of
transient errors near the bed, never less. If homing triggers at a visibly wrong height
(it shouldn't — trigger logic is untouched), stop and reflash the previous firmware.

## 4. Smoke test sequence

Run in order; each step gates the next. Bed and hotend cold unless noted.

| # | Command | Pass criteria |
| --- | --- | --- |
| 1 | `PROBE_EDDY_NG_STATUS` | frequency present, height plausible for current Z |
| 2 | `G28` | homes normally; no `Sensor error` abort from a single transient |
| 3 | `PROBE_EDDY_NG_PROBE_ACCURACY SAMPLES=10` | stddev in the low-µm range (record it) |
| 4 | `PROBE_EDDY_NG_TAP` (bed/nozzle at print temp) | completes; cluster stddev ≤ 0.020 gate |
| 5 | `QUAD_GANTRY_LEVEL` | converges in normal number of iterations |
| 6 | `BED_MESH_CALIBRATE METHOD=rapid_scan` | completes; mesh looks like your usual mesh |

New-behavior spot checks (each was a fixed bug — 2 minutes total):

- `PROBE_EDDY_NG_TAP AUTO_LOWER=0` — a "too close to target z" result must now fail the
  sample instead of descending. Also note: auto-lower now honors an explicit `TARGET_Z=` as
  its floor reference and logs accurate messages.
- `SET_TAP_ADJUST_Z VALUE=0.05` → `PROBE_EDDY_NG_CALIBRATE ...` (or `CLEAR_CALIBRATION`) →
  `SAVE_CONFIG` → restart → confirm `tap_adjust_z` is **still 0.05** (previously the
  calibration save silently wiped it).
- `SET_TAP_ADJUST_Z VALUE=0.05` then `SET_TAP_ADJUST_Z VALUE=0` → `SAVE_CONFIG` → restart →
  confirm it is **0** (previously the stale 0.05 came back).
- `EDDYNG_STOP_STREAM_EXPERIMENTAL` with no stream running → friendly "No stream is active"
  error, **not** a printer shutdown.
- Debug CSVs (`SAVE=1`, `debug: true`) now land in the system temp dir (`/tmp` on the Pi,
  same as before there — the change matters on non-Linux hosts).

## 5. Baseline measurement protocol

Do this **once, before starting UPGRADE_PLAN.md**, and again after every phase. Keep the
numbers in a table (a `baselines.md` or spreadsheet). Every upgrade's acceptance criterion
is "no metric regresses; the targeted metric improves."

1. **Static noise, cold:** bed/hotend cold, toolhead at bed center, homed.
   `PROBE_EDDY_NG_PROBE_ACCURACY SAMPLES=20` → record mean, stddev, range.
2. **Static noise, hot:** bed at print temp, heat-soaked ≥ 15 min, hotend at print temp.
   Same command → record. (The cold-vs-hot mean delta is your current thermal drift number.)
3. **Tap repeatability, hot:** run `PROBE_EDDY_NG_TAP` 5 times in a row (let it re-home
   between runs as it normally does). Record each accepted tap Z and the spread of the 5.
4. **Calibration quality:** run `PROBE_EDDY_NG_CALIBRATE DRIVE_CURRENT=<your homing dc>`
   and record the reported RMS fit error; same for your tap drive current.
5. **Mesh stability:** `BED_MESH_CALIBRATE METHOD=rapid_scan` twice back-to-back, save both
   profiles, note the max point-to-point delta between the two runs (this is scan noise,
   independent of true bed shape).
6. Record ambient conditions and firmware/host git SHAs alongside the numbers.

## 6. Rollback

- **Host Python:** on the Pi, `git -C ~/eddy-ng checkout <previous-sha>` (or `git revert`),
  then `sudo systemctl restart klipper`. Symlinks make this instant.
- **Firmware:** reflash the previously built `klipper.uf2` (keep the old `out/klipper.uf2`
  renamed, e.g. `klipper-<sha>.uf2`, before each rebuild).
- **Calibration:** upgrades that bump `calibration_version` invalidate stored calibrations
  on purpose; rolling back the code restores the old version check, and your old
  `calibration_*` blobs still load (they're only replaced when you SAVE_CONFIG after a
  recalibration — so don't SAVE_CONFIG until a new calibration has proven itself).
