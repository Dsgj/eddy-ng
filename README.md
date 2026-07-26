# eddy-ng (Klipper + BTT Eddy fork)

A fork of [vvuk/eddy-ng](https://github.com/vvuk/eddy-ng), narrowed to **Klipper only** and the
**BTT Eddy / BTT Eddy Duo** sensors only, in exchange for a round of audited bug fixes,
better diagnostics, and field-validated tuning guidance.

**If you run Kalico, or a Cartographer / Mellow Fly / generic LDC1612 sensor, use
[upstream](https://github.com/vvuk/eddy-ng) — those code paths are deleted here.**

## What eddy-ng does

Eddy current probes are very accurate but drift, because both the target surface's conductivity
and the coil's own parameters change with temperature. Rather than modelling that drift, eddy-ng
measures around it:

1. Calibration is performed at any temperature (cold is fine).
2. Z-homing uses that calibration regardless of current temperature. This is a *coarse* home —
   accurate enough for gantry levelling and preparation, not for printing.
3. A precise Z-offset is taken by **tapping** the build surface just before printing, with the bed
   at print temperature and the nozzle warm but not oozing.
4. The same tap records the delta between true height and what the sensor reads at that height.
   That offset is then applied to bed mesh and scanning reads, so the mesh inherits the correction.

## This fork vs. upstream

### What you gain

| Area | Change |
| --- | --- |
| **Diagnostics** | Sensor error names were decoded with the wrong bit shift (`>> 12` instead of `>> 28`), so every `Sensor error (...)` message printed **frequency data bits as error names** — producing impossible combinations like simultaneous under-range and over-range. Fixed; errors now name the real fault. |
| **Tap offset persistence** | `SAVE_CONFIG` after a calibration silently wiped `tap_adjust_z`; separately, a cleared value could come back on restart. Both fixed. |
| **Height conversion** | The scalar and vectorized `freq_to_height` paths disagreed on the fit-domain boundary, so a single read and a batch read of the same frequency could return different heights. |
| **SETUP drive-current choice** | SETUP scored candidates on fit error alone and could pick a homing drive current that cannot see the bed from the homing macro's z-hop — breaking `G28` entirely with "Couldn't get any valid samples". It now requires homing candidates to cover the calibrated range, flags calibration floors that are censored at the sweep's zero anchor rather than reporting them as measured, and warns when the tap drive current has no verified range below z=0. |
| **Firmware** (`sensor_ldc1612_ng.c`) | SOS section index bounds check (out-of-range writes now shut down cleanly); WMA accumulator widened to 64-bit; the `is_tap` predicate fixed so amplitude-high errors during plain `G28` are ignored *as the original comment always intended* rather than aborting the home. |
| **Startup robustness** | A corrupt calibration blob logs a warning instead of crashing klippy; `tap_samples > tap_max_samples` is now a config error instead of silently failing every tap after wasting bed contacts; `max_errors` and `tap_threshold` are range-checked against their wire encodings. |
| **Crash fixes** | `EDDYNG_STOP_STREAM_EXPERIMENTAL` with no stream raised a printer shutdown; a sample predating trapq history crashed the tap path; bed-mesh scan setup crashed on several valid `[bed_mesh]` configurations. |
| **New options** | `scan_use_trimmed_mean` (trimmed mean instead of median for scan/static windows) and `tap_log_hinge` (shadow two-segment contact fit, diagnostic only). |
| **Housekeeping** | Debug CSVs go to the system temp dir instead of hardcoded `/tmp`; `debug` defaults to `False`; honest `Optional` annotations; dead code removed; pinned CI lint. |
| **Documentation** | [TESTING.md](TESTING.md) — deploy, reflash (including CAN/katapult), smoke test, and a baseline measurement protocol. [UPGRADE_PLAN.md](UPGRADE_PLAN.md) — twelve researched algorithm upgrades, several now closed with hardware evidence rather than left as speculation. |

### What you lose

| Limitation | Detail |
| --- | --- |
| **Kalico support removed** | The installer refuses to install into a Kalico tree. (`-u` uninstall still understands Kalico layouts so pre-fork installs can be cleaned up.) |
| **Only `sensor_type: btt_eddy`** | Cartographer, Mellow Fly, `ldc1612_internal_clk` and generic LDC1612 branches are deleted from host and firmware. The firmware shuts down with "unsupported product" for other product codes. |
| **Diverges from upstream** | You will not automatically receive upstream fixes, and merging them back will conflict — expect conflicts in the import block, `LDC1612_ng.__init__`'s product setup, and the product `switch` in `command_config_ldc1612_ng`. |
| **Firmware reflash required** | The C changes need a rebuild and reflash to take effect. Host and firmware stay wire-compatible, so an unflashed sensor keeps working with the old behavior. |
| **Community support assumes upstream** | The Discord and upstream issue tracker are staffed by people running upstream. Report fork-specific problems [here](https://github.com/Dsgj/eddy-ng/issues) instead. |

### Which should you use?

- **This fork** — BTT Eddy on Klipper, and you want the fixes above (especially if you are
  *debugging* a probe: the error-decoder fix alone changes tap troubleshooting from guesswork
  to a readable fault name).
- **Upstream** — Kalico, a non-BTT sensor, or you would rather track the mainline and receive
  updates as they land.

## Field-validated notes for BTT Eddy

Measured on a Voron 2.4r2 with a BTT Eddy on CAN. Your machine may differ, but these are the
settings and failure modes that mattered most:

- **Coil height is part of the calibration contract.** The coil must sit **2.5–3.0 mm above the
  nozzle** (~2.75 ideal). A coil 0.44 mm too low made the sensor go blind *just above contact*, so
  every tap died on the samples the detector needs most. `SETUP` reporting a valid-height floor of
  `0.000` may mean "censored at the sweep anchor", not "reads to contact" — this fork annotates that.
- **Homing and tap need different drive currents.** `reg_drive_current: 15` and
  `tap_drive_current: 16` is the working BTT Eddy pairing. 16 reads close to the bed but saturates
  around 8–15 mm, so using it for homing breaks `G28` at the homing macro's z-hop.
- **`tap_time_position` is worth tuning.** The default `0.3` gave 18.6 µm run-to-run tap scatter;
  `0.7` gave **6.7 µm** — a 2.8× improvement from one config value. Changing it shifts absolute Z
  zero (~30 µm), so re-tune `tap_adjust_z` afterwards.
- **A *more* sensitive `tap_threshold` measured more repeatable, not less.** Raising it toward the
  `butter` default (150 → 250) doubled scatter, because a higher threshold triggers later, into the
  region where the signal reflects plate compliance rather than the contact transition.
- **Tap before bed mesh in `PRINT_START`.** The scanning probe captures `tap_offset` when its
  session begins, and only a tap produces that value — meshing first uses the previous print's offset.

## Installation

```bash
cd ~
git clone https://github.com/Dsgj/eddy-ng
cd ~/eddy-ng
./install.sh
```

If Klipper isn't in `~/klipper`, pass the path: `./install.sh ~/my-klipper`.

Symlink mode is the default and preferred: edits and `git pull`s take effect on the next Klipper
restart with no reinstall. Firmware changes still require a rebuild and reflash — see
[TESTING.md](TESTING.md) §3, which covers CAN/katapult flashing and the bootloader-offset trap.

Then follow the [upstream wiki](https://github.com/vvuk/eddy-ng/wiki) for setup, ignoring its
Kalico and non-BTT-sensor sections. Note the wiki's defaults have drifted from the code in places —
`ProbeEddyParams` in [probe_eddy_ng.py](probe_eddy_ng.py) is the source of truth.

## Updating

```bash
cd ~/eddy-ng
git pull
sudo systemctl restart klipper
```

A console `RESTART` is **not** enough for Python changes — klippy caches imported extras modules,
so `RESTART` and `FIRMWARE_RESTART` re-read the config but keep running the old code. Only a
service restart reloads modules from disk. (Re-run `./install.sh` only if the file list changed.)

## Support

Fork-specific issues: [github.com/Dsgj/eddy-ng/issues](https://github.com/Dsgj/eddy-ng/issues).

For general eddy-ng questions, the Sovol 3D Printers Discord `#eddy-ng` forum
(`https://discord.gg/Zg45rA52G7`) is upstream's preferred channel — nothing there is
Sovol-specific, it's just where the project started. Please don't file fork bugs upstream.
