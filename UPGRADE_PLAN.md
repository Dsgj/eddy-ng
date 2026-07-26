# eddy-ng upgrade plan

Implementation roadmap for the 12 algorithm/technique upgrades identified by the
2026-07-24 research pass (5 web researchers + ground-truth code analysis + synthesis
judge, ranked by expected accuracy/reliability gain ÷ implementation risk). This plan
re-orders that ranking into **dependency-and-risk order**: foundations before things
built on them, one behavior change at a time, each validated on the printer before the
next begins.

## Ground rules

1. **One item per branch, validated before the next starts.** Every item changes
   measurement behavior; bundling two makes a regression undiagnosable.
2. **Baseline before, measure after.** Use the protocol in [TESTING.md](TESTING.md) §5.
   Acceptance = targeted metric improves, nothing else regresses.
3. **New behavior ships behind a flag with the old path as fallback** wherever feasible.
   Flip defaults only after the new path has survived real prints.
4. **Runtime constraints** (from the research constraints, non-negotiable):
   - Real-time trigger paths run in C on an FPU-less RP2040 at ~250 SPS — integer/fixed
     point, tiny per-sample budget.
   - Host side is Python in klippy: numpy yes, scipy import-guarded only, never block the
     reactor for long.
   - Everything must be calibratable by an end user without lab equipment.
5. **Calibration compatibility:** anything that changes the stored map format bumps
   `calibration_version` (currently 5) and forces recalibration. Batch such items
   (Phase 3) so users recalibrate once, not three times.

## Phase overview

| Phase | Items | Theme | Recal? | Reflash? | Risk |
| --- | --- | --- | --- | --- | --- |
| 1 | Freq-domain offset · trimmed mean · mesh NaN-fill · tap ergonomics | Independent quick wins, host-only | no | no | low |
| 2 | Timestamp centroid + true fs · per-phase sample rates | Sampling ground truth | yes (once) | no | low-med |
| 3 | Monotone calibration map · drive-current sweep rework | Calibration model (version 6) | yes (once) | no | medium |
| 4 | Contact fit (log → active) · auto-derived threshold | Tap accuracy | no | no | medium |
| 5 | NTC watchdog · parametric temp model (gated) | Thermal | no | no | med-high |
| 6 | Fixed-point SOS + high-rate tap (only if needed) | Firmware bandwidth | no | yes | high |

Phases 1 and 2 are independent of each other and could be swapped; everything else
depends on what precedes it.

---

## Field evidence (2026-07-26 hardware session) — read before picking an item

A full deploy + validation session on the Voron 2.4r2 changed several premises below.

- **Baselines are now real.** The pre-session numbers were measured against a sensor coil
  mounted 0.44 mm too low and should be discarded. Post-shim baseline: `PROBE_ACCURACY`
  scale factor 0.995 over 0.5–5.0 mm with ±10 µm residual nonlinearity and a constant
  −0.07 mm reference offset; stddev 0.006; tap 11 µm run-to-run, 4–5 µm within-run.
- **3.1 got direct supporting evidence.** With near-bed samples dropped as amplitude
  errors, the degree-9 fit had no data below 0.793 mm and rang across its *entire*
  domain — an 8.4 % scale error at 5 mm, far from the missing region. High-order
  polynomial fits distort globally when they lose an endpoint. A monotone PCHIP map
  cannot do this, which raises 3.1's value above its original ranking.
- **2.1's headline justification weakened.** That 8.4 % error was initially attributed to
  timestamp lag; it was the starved fit, and the error vanished on recalibration with
  clean data. The ~2 ms centroid bias is still real and still worth fixing before any
  recalibration-heavy work, but do not expect it to move accuracy much on a healthy mount.
- **3.2 is partially delivered** (commit `77f34ad`) — see that item for what remains.
- **Phase 4 has less headroom than assumed.** Tap already reaches 4–5 µm within-run on a
  correctly mounted sensor. 4.1 is passive logging and still free to start; 4.2/4.3 should
  be justified against the post-shim baseline, not the old one.
- **Phase 5 is unmeasured.** A tap-series drift initially read as thermal turned out to be
  a QGL step. Run the §5 cold-vs-soaked experiment (no code) before committing to 5.1.

---

## Phase 1 — Quick wins (independent, host-only, individually revertible)

### 1.1 Apply the tap drift offset in the frequency domain

- **Why:** `tap_offset` is a constant *mm* shift measured once at 2.0 mm and added after
  freq→height conversion. df/dh varies ~10× over 0–5 mm, so a constant mm offset is exact
  only at exactly 2 mm, while TI's drift decomposition (and Beacon/Cartographer practice)
  says the dominant drift term is a *frequency* shift — a constant Δf applied *before*
  conversion is exact at every height. Same measurement, same workflow.
- **Where:** the post-tap reference read in `cmd_TAP_next` (the block that computes
  `self._tap_offset` from `probe_static_height` at `home_trigger_height`); every consumer
  of `_tap_offset` (`ProbeEddyScanningProbe`, `BedMeshScanHelper.scan`,
  `probe_static_height` callers); `ProbeEddySampler`'s freq→height conversion.
- **How:**
  1. At the reference read, compute `delta_f = fmap.height_to_freq(home_trigger_height) -
     measured_freq` (careful with sign: freq **rises** as height falls). Store per drive
     current alongside the existing mm value.
  2. Apply `freq + delta_f` inside the sampler's conversion path instead of
     `height + tap_offset` in each consumer. Keep a derived mm-equivalent in `get_status()`
     for dashboards/macros that read it.
  3. Config flag `tap_offset_domain: freq|height` (default `height` initially) so both
     paths coexist; flip default after validation.
- **Validate:** hot-bed `PROBE_ACCURACY` at 1 mm, 2 mm, 3 mm (`Z=` variants): mm-domain
  and freq-domain must agree at 2 mm; freq-domain should be measurably closer to truth
  away from 2 mm (compare against a fresh tap at each height). Expected gain ~5–15 µm at
  non-reference heights.
- **Effort:** ~20 line diff + flag plumbing. **Rollback:** flag.

### 1.2 Trimmed mean instead of median for scan/static windows

- **Why:** per-window `np.median` (in `ProbeEddySampler.find_height_at_time`,
  `find_heights_at_times`, `probe_static_height`) forfeits ~36 % statistical efficiency vs
  the mean on Gaussian noise; contiguous frequency-count samples also average better than
  √N for the quantization component. A 10–20 % trimmed mean keeps nearly all of the mean's
  efficiency while retaining the spike robustness that motivated median.
- **How:** replace the median with `np.mean(np.sort(v)[k:-k])`, `k = ceil(0.1*N)` (guard
  N < 3 → plain mean/median). Keep `USE_MEDIAN`/`tap_use_median` semantics for tap
  clustering untouched — this item is only the *within-window* estimator. Report per-point
  stddev alongside mesh values while validating.
- **Validate (corrected 2026-07-26):** *not* the bracketed stddev `PROBE_ACCURACY` prints —
  that is the spread of samples about the centre and barely moves when the centre changes.
  Measure estimator noise directly: run `PROBE_EDDY_NG_PROBE_STATIC` ~10× at a fixed
  height, take the spread of the reported values, flag off vs on. Monte-Carlo at N=25
  Gaussian: median sd 0.2477 → trimmed 0.2070 → mean 0.2006, i.e. 16 % tighter than median
  and within 3 % of the unattainable plain mean, while a 50 σ spike moves the mean by
  +1.96 σ and the trimmed mean by −0.08 σ. Secondary: §5.5 mesh-repeat delta should shrink
  by a similar fraction. No change to means expected.
- **Effort:** a few lines in three functions. **Rollback:** trivial revert.
- **Implemented 2026-07-26** behind `scan_use_trimmed_mean` (default `False`).
- **Field result (18× `PROBE_STATIC` at ~5 mm, no motion between):** sd(median) 4.51 µm vs
  sd(mean) 3.56 µm, ratio **1.266** against the Gaussian prediction of 1.253 — the
  efficiency argument holds exactly. But the larger effect is *quantization*: the median
  returned only **3 distinct values across 18 trials** (5.003 / 5.008 / 5.016, steps of
  5 and 8 µm) where the mean returned 12. An order statistic always lands on an actual
  sample, so the median inherits the sensor's height quantum in full; averaging over
  dithered samples resolves below it. See 2.2 for where that quantum comes from.

### 1.3 Mesh robustness: NaN windows + capped neighbor fill

- **Why:** the experimental scan path hard-fails an entire mesh on any empty sample
  window, and windows *can* silently starve because `_process_batch` drops error samples
  upstream — a transient I²C hiccup or amplitude-error burst at a bed edge kills a
  5-minute mesh at its last step.
- **How:**
  1. `find_heights_at_times` returns NaN for an empty window instead of raising.
  2. `BedMeshScanHelper` fills NaN cells from valid neighbors with hard caps: error out if
     > 5 % of cells are NaN or any NaN cell has no valid neighbor (a failing sensor must
     not hide behind interpolation). Log every filled cell.
  3. Port upstream's neighbor-fill (vvuk/eddy-ng commit `c9a5c0d`) **but fix its indexing
     bug**: the heights stride must be `x_points` and the loop variable is `i`.
- **Validate:** normal meshes unchanged; artificially induce a dropout (briefly shade the
  sensor / lower drive current) and confirm the mesh completes with a logged fill instead
  of aborting.
- **Effort:** small. **Rollback:** trivial.

### 1.4 Tap retry ergonomics (Cartographer touch-loop ideas)

- **Why:** two documented biases: (a) repeated same-spot taps mark PEI and read
  progressively deeper — and the min-stddev-over-C(n,3) cluster selection will confidently
  pick exactly such a tight-but-progressively-wrong cluster; (b) full machine accel during
  the approach excites the 5–25 Hz band the butter detector listens to (SV08-class
  vibration reports).
- **How:** in `cmd_TAP_next`:
  1. `tap_fuzz_radius` (default ~0.5–1 mm, clamped to bed bounds): random XY jitter
     applied per attempt around the requested tap point.
  2. `tap_accel`: save/apply/restore via `SET_VELOCITY_LIMIT` around the homing move
     (verify overshoot stays within the existing `finish_z` checks).
  3. Optional: O(n) sequential max|z−median| gate behind the existing `tap_use_median`.
- **Validate:** 5-tap sessions on a soft sheet should stop trending deeper
  (compare tap 1 vs tap 5 across sessions); fewer retries on vibration-prone setups.
- **Effort:** ~50 lines. **Rollback:** defaults off (radius 0 / accel unset) = old behavior.

---

## Phase 2 — Sampling ground truth (do before any recalibration-heavy work)

### 2.1 Conversion-window centroid timestamp correction + honest sample rate

- **Why (two related lies):** every LDC1612 sample is the average of the preceding
  ~4.065 ms conversion window (16·RCOUNT/f_ref) but is timestamped at readout — nothing
  subtracts the ~2 ms centroid, so all moving reads are biased ~10–20 µm at 5 mm/s in a
  direction fixed by the sweep direction. This skew is baked into *all* calibration
  training data — no fit can remove it. Separately, code assumes fs = `samples_per_second`
  (250) while the chip actually converts at f_ref/(16·RCOUNT+4) ≈ 246 Hz — the hardcoded
  Butterworth coefficients carry a ~1.6 % passband error.
- **Where:** the single decode point feeding `ProbeEddySampler` (timestamp assignment in
  `ldc1612_ng.py` `_process_batch`); every consumer of `samples_per_second` as a
  frequency (butter design in `cmd_TAP_next`, window sizing).
- **How:**
  1. Compute `t_conv = 16*RCOUNT/f_ref` once at `_init_chip`; expose it and the true
     conversion rate from `LDC1612_ng`.
  2. Subtract `t_conv/2` from sample timestamps at the decode point (one place only).
  3. Replace `fs = samples_per_second` with the true rate everywhere it's used as a
     frequency; keep the hardcoded SOS tables but select them by *nearest true fs* and log
     the residual mismatch (regenerating exact tables needs scipy — do it where available).
  4. **Then recalibrate** (training data changes meaning) — fold into the Phase 3 recal.
- **Validate:** one-off experiment: calibrate, then run a slow *upward* sweep and compare
  freq-at-height against the downward calibration — the direction-dependent offset should
  shrink to < 1 ms equivalent. Homing trigger height (measure with a tap right after G28)
  should move by roughly the predicted ~10 µm.
- **Effort:** small code, medium validation. **Rollback:** revert; recalibrate again.
- **Revised expectation (2026-07-26):** a field gain error once attributed to this turned
  out to be a starved polynomial fit, and it disappeared on recalibration with clean data.
  The centroid bias is still physically real and still belongs before the Phase 3 recal,
  but treat it as correctness hygiene rather than an accuracy win. Do 3.1 first if you
  only have appetite for one calibration-touching change.

### 2.2 Per-phase sample rates (host-only stage)

- **Why:** one global `samples_per_second` fixes RCOUNT for every phase, but calibration
  and scanning need no bandwidth — and TI's own data (SNOA944 fig. 7: σ ∝ RCOUNT^-0.8)
  says one 4×-longer conversion beats averaging 4 short ones. 2–3× lower noise for
  calibration sweeps and mesh windows at zero time cost, and the DATA scale contains no
  RCOUNT term, so **stored calibrations, thresholds, and firmware are untouched**.
- **How:**
  1. `set_data_rate(sps)` in `ldc1612_ng.py`: sleep-mode-wrapped RCOUNT0 rewrite per
     datasheet §7.4.2 (config must only change in sleep mode), recompute `t_conv` (ties
     into 2.1).
  2. New config keys `calibration_samples_per_second` and `scan_samples_per_second`
     (default 125); switch at calibration entry and `start_probe_session` when no sampler
     is active (the one-global-sampler rule already serializes phase boundaries), restore
     to `samples_per_second` on session end. Homing and tap stay at 250 untouched.
- **Validate:** §5.1 static stddev at scan rate should drop visibly; scan windows get
  fewer-but-quieter samples (12 vs 25) — mesh repeat delta (§5.5) must improve or hold.
- **Effort:** medium-small. **Rollback:** set both new keys to 250.
- **Field evidence (2026-07-26) — promoted, this is a bigger lever than assumed.** The
  LDC1612's resolution is **RCOUNT-limited, not 28-bit-limited**: at `samples_per_second`
  250 the driver writes `RCOUNT0 = 3049`, giving a frequency quantum of
  `f_sensor/(16·RCOUNT)` ≈ **64.5 Hz** — while the 28-bit `freqval` LSB is 0.045 Hz, some
  1400× finer and therefore irrelevant. At df/dh in the 6–15 kHz/mm range that quantum is
  a height grid whose size depends strongly on where you measure. **Scope correction:** the
  grid is ~5 µm at 5 mm but only **~1.05 µm at scanning height** (measured from gaps in a
  real bed mesh: df/dh ≈ 61.5 kHz/mm near the bed vs ≈ 12.9 kHz/mm at 5 mm). Since nothing
  operates at 5 mm, the *grid* half of this item is worth well under a micron — do not
  justify 2.2 on quantization. The **noise** half still stands on its own: σ ∝ RCOUNT^-0.8
  applied to a ~13 µm per-sample spread is what actually moves the estimator, and it is one
  register write with no calibration or firmware impact. The same computation confirms
  2.1's other claim outright: the true conversion rate is **246.0 Hz**, not the 250 assumed
  in the filter design.
- **Methodological warning for anyone validating this area:** do not compare estimators by
  the sd of repeated static reads. A quantized estimator (the median) snaps to the grid and
  reports *lower* sd when the true value happens to sit on a level — observed swinging
  between 1.81 µm and 4.51 µm across two runs minutes apart, purely from grid alignment,
  while the mean held 3.56–3.83 µm. That is resolution loss impersonating precision.

---

## Phase 3 — Calibration model (one recalibration event, `calibration_version = 6`)

### 3.1 Monotone calibration map: binned medians + PAVA + PCHIP, derived inverse

- **Why (three birds):** the current pair of *independent* degree-9 polynomials
  (`calibrate_from_values`) can disagree round-trip — `ftoh(htof(h)) ≠ h` by O(RMSE),
  a bias that lands directly in the homed Z origin because homing thresholds come from
  `htof` while heights are read via `ftoh`. Degree-9 fits can also go locally
  non-monotonic at low freq spread (physics guarantees monotonicity), and tap descends
  into raw x⁹ extrapolation below the calibrated minimum on every run. A monotone spline
  with a *derived* inverse makes all three failure modes structurally impossible.
- **How (numpy-only, ~100 lines):**
  1. Quantile-bin the sweep samples in height, K = min(80, n/8); take median (h, 1/f) per
     bin (robustness comes free — replaces any need for Huber/RANSAC).
  2. PAVA (pool-adjacent-violators) to enforce monotonicity on the knots.
  3. Fritsch–Carlson tangent limiting → monotone cubic Hermite (PCHIP) evaluation via
     `np.searchsorted`.
  4. Inverse = bracketed Newton on the same knots (tolerance ~1e-8 mm), *not* a separate
     fit. Below the lowest knot: endpoint-tangent linear extension (sane tap-region
     extrapolation).
  5. Ship **alongside** the polynomial path first: every calibration fits both, logs
     RMSE / round-trip max error / monotonicity for each. Flip the default and bump
     `calibration_version` to 6 only when the logs show the spline winning on your data.
  6. Storage: new dict keys in the existing pickle blob (knots + tangents); keep loaders
     for v5 refusing gracefully (existing behavior).
- **Validate:** round-trip error drops from O(5–30 µm) to solver tolerance; G28 trigger
  height (tap immediately after home) shifts accordingly and becomes more repeatable;
  §5.3 tap spread should not regress.
- **Effort:** the largest single host-side item. **Rollback:** default flag back to
  polynomial; v5 calibrations still load.
- **Field evidence (2026-07-26):** observed in the wild — a degree-9 fit starved of data
  below 0.793 mm produced an 8.4 % scale error at 5 mm, i.e. the distortion appeared far
  from the missing data, not near it. Binned medians + PAVA + PCHIP are structurally
  immune: a missing endpoint degrades only the endpoint interval. This makes 3.1 the
  highest-value item in the plan, ahead of its original ranking, and it is the one change
  that would keep a marginal mount from silently corrupting the whole height map.

### 3.2 Drive-current sweep rework (SETUP)

- **Why:** the WIP sweep scores candidates by *in-sample* RMS of a degree-9 fit — a dc
  that overfits noise outranks a genuinely quieter one — and ignores amplitude margins,
  the mechanism behind the "tap needs dc 16 but homing 15" folklore and mid-print
  `SAMPLE_ERR_AE` aborts. The chip exposes both fixes already: per-sample error bits
  (streamed, currently only counted) and the auto-amplitude `INIT_IDRIVE` readback
  (`cmd_LDC_CALIBRATE`, currently a separate manual command).
- **How:**
  1. Extend `_process_batch` to tally error kinds (the `val >> 28` nibble) per batch, not
     just a single count.
  2. In the sweep: hard-disqualify any dc with amplitude errors anywhere in its sweep
     (degrade to warn+score if *no* dc passes, so marginal mounts still set up).
  3. Score survivors by noise-in-mm: binned MAD of freq residuals × |dz/df| from the
     (Phase 3.1) map, averaged over bins below 1 mm plus at 2 mm — separate scores for
     tap and homing roles instead of one conflated scalar.
  4. Seed the sweep range from the chip's `INIT_IDRIVE` estimate read at max working
     height (reuses `cmd_LDC_CALIBRATE` logic inline) — typically 5 sweeps instead of 7.
- **Validate:** SETUP on your machine picks the same or better currents than today's
  sweep; deliberately mis-mounted test (sensor a bit high) must *reject* amplitude-margin
  currents rather than pick them.
- **Effort:** medium; touches `cmd_SETUP_next` + `ldc1612_ng.py` batch path.
  **Rollback:** keep the RMS scorer behind `SETUP FAST=1`-style flag.
- **Partially delivered in `77f34ad` (2026-07-26)**, after SETUP twice picked a drive
  current that could not read at the homing macro's z-hop and broke `G28` outright
  ("Couldn't get any valid samples from sensor"): homing candidates must now cover
  `calibration_z_max - 2.0` rather than a flat 5.0 mm, with a warned fallback; calibration
  floors that bottom out at the sweep's zero anchor are annotated `(censored: sweep
  bottom)` instead of masquerading as measured; and a tap drive current with no verified
  range below z=0 now warns and names the likely cause. **Still to do:** per-kind error
  tallies in `_process_batch`, amplitude-error disqualification, noise-in-mm scoring
  (needs 3.1's map for |dz/df|), and `INIT_IDRIVE` seeding of the sweep range.

---

## Phase 4 — Tap accuracy (the headline items)

### 4.1 Contact fit, stage A: log-only hinge fit

- **Why:** the single biggest fudge in the Z=0 path is `tap_time_position = 0.3`: contact
  time is guessed as 30 % of the way between two *filter-delayed* timestamps (both carry
  15–30 ms of 5–25 Hz bandpass group delay). Klipper mainline solved the identical
  problem on the identical sensor geometrically (`TapBestFit`): fit the freq-vs-Z curve as
  two segments — bed-depress line + free-air curve — the breakpoint *is* contact,
  invariant to filter delay, tap speed, drive current, and threshold. The infrastructure
  is 90 % present: `home_wait` already collects samples up to `trigger_time` and discards
  them.
- **How (stage A = zero risk):** in `ProbeEddyEndstopWrapper.home_wait` after
  `wait_for_sample_at_time`, run a two-segment changepoint fit over the ~60–125 samples in
  `[trigger − W, trigger]` (cumulative-sums make the per-split RSS O(1); scan all splits).
  Gates: post/pre slope ratio < 0.6 and breakpoint τ within `[tap_start − margin,
  trigger]`. **Do not use the result** — log τ's fractional position in the
  `[tap_start, trigger]` interval alongside the 0.3 constant, every tap. After a few weeks
  of prints you have per-printer data for what 0.3 "really is" and how stable τ is.
- **Validate:** logging only — confirm no added latency (fit is µs-scale numpy on ≤128
  points) and that τ is produced on > 90 % of taps with sane gate pass rates.
- **Effort:** small-medium. **Rollback:** delete the log line.

### 4.2 Contact fit, stage B: active, with fallback

- **How:** once stage-A logs show τ stable and plausible:
  1. Use τ as the tap time when gates pass; fall back to the 0.3 interpolation (and log
     the disagreement) when they don't. Config `tap_time_method: fit|fixed`.
  2. Optional stage C (port of full `TapBestFit`): fit freq-vs-*Z* over a ≥ 0.4 mm
     *retract* window with Klipper's validity gates — better still, because the pullback
     is quasi-static. Only pursue if stage-B residual scatter justifies it.
- **Validate:** §5.3 five-tap spread — research estimate: stddev plausibly halves
  (0.02 gate → typical 0.005–0.010), and first-layer consistency across speed/threshold
  changes improves (the 0.3 constant's error scales with both; τ's doesn't).
- **Rollback:** `tap_time_method: fixed`.

### 4.3 Auto-derived, speed-normalized tap threshold

- **Why:** `tap_threshold` defaults (1000 wma / 250 butter) are raw filtered-signal units
  whose physical meaning scales with drive current, tap speed, sample rate, and coil
  temperature — the root cause of manual THRESHOLD bisection. Klipper normalizes to Hz/mm
  and converts per move (`threshold * speed / sps`); the calibration map's own slope gives
  the starting value for free. Depends on 3.1 (analytic slope) and pairs with 4.1/4.2
  (with geometric timing, the threshold only needs to *fire reliably*, not fire
  *precisely* — it can be biased safe).
- **How:**
  1. `slope_hz_per_mm` from the map's derivative over h ∈ [0.05, 0.5].
  2. New `PROBE_EDDY_NG_TAP_CALIBRATE`: GUESS (0.1 × slope × speed-normalization,
     constant derived once for the 5–25 Hz bandpass gain) → REFINE (0.2 × the
     depress-vs-freeair slope contrast from the 4.1 fit) → VERIFY (5 taps against the
     stddev gate, then `configfile.set`).
  3. Noise-margin diagnostic first (pure host): MAD of the bandpassed pre-contact signal
     per tap; warn when threshold < ~6×noise — turns "mystery early trigger" into an
     actionable number.
- **Validate:** fresh-setup experience: TAP works first try at 2 and 4 mm/s and both
  drive currents without touching THRESHOLD; noise-margin warning fires when you
  deliberately set THRESHOLD=50.
- **Effort:** medium. **Rollback:** explicit `tap_threshold` config wins over auto.

---

## Phase 5 — Thermal drift

### 5.1 Coil NTC capture + drift watchdog (prerequisite plumbing)

- **Why:** BTT Eddy carries an onboard NTC (`temperature_sensor` on the Eddy MCU,
  typically gpio26) that this codebase entirely ignores, while the architecture's core
  assumption — tap_offset measured once, valid until the next tap — fails silently
  whenever thermal state moves mid-print (~200 Hz/°C ≈ 10–40 µm/°C through the map).
- **How:**
  1. Support/lookup a configured `[temperature_sensor]` on the Eddy MCU from `ProbeEddy`.
  2. Record coil temp into the calibration blob (tolerant optional key, **no** version
     bump) and alongside every tap and tap_offset measurement.
  3. Watchdog: at `start_probe_session`/homing, warn when |T_now − T_tap| > ~5 °C, with
     optional auto-refresh of the (Phase 1.1) frequency-domain offset at 2 mm — no nozzle
     contact needed.
  4. Log (T, freq@2mm) pairs opportunistically — this is the dataset that justifies or
     kills 5.2.
- **Validate:** heat-soak test: cold tap → heat bed → watchdog warns at the threshold →
  auto-refresh restores §5.2-style hot accuracy without a new tap.
- **Effort:** small-medium. **Rollback:** unconfigured sensor = feature off.

### 5.2 Parametric temperature compensation (gated on 5.1 data)

- **Why:** the only technique that fixes what tap_offset structurally cannot: the homing
  trigger freqval (computed from a possibly-cold calibration, never corrected today) and
  continuous drift *between* taps. Klipper `temperature_probe`, Beacon, and Cartographer
  all converged on frequency-domain f(T) models — strong convergent evidence. Field
  reports show 0.1–0.4 mm uncompensated swing.
- **How:** only if 5.1's logged (T, freq) data shows a stable per-unit relationship:
  Cartographer's compact model form (parameters linear in f − f_min, closed-form
  inversion), fitted by an unattended tap-anchored heat-soak G-code (eddy-ng's unique
  advantage: tap gives ground-truth Z=0 at each temperature station automatically).
  Apply to raw freq before map lookup; apply inverse to the homing threshold freqval in
  `setup_home`. Firmware untouched.
- **Validate:** repeat the cold→hot §5 protocol: residual drift should shrink ~5–10×;
  G28-then-tap delta should stop tracking bed temperature.
- **Effort:** the largest item on this list (model + soak workflow + validation).
  **Rollback:** flag off → 5.1 watchdog behavior.

---

## Phase 6 — Deferred firmware work (only if Phase 4 leaves tap SNR-limited)

### 6.1 Fixed-point SOS filter + higher tap sample rate

- **Why deferred:** float32 biquads on the FPU-less RP2040 are soft-float — hundreds of
  cycles per sample — which is the ceiling blocking a 500–1000 SPS tap rate (finer
  trigger quantization, more samples in the depress region for the 4.x fits). Klipper
  mainline has an integer `sos_filter.c` to port, and OFFSET0 band-recentering keeps the
  values in integer range. But it's firmware surgery with wire-format and threshold-
  semantics implications — do it only if Phase 4 data shows tap accuracy is still
  sample-rate-limited.
- **Prereqs:** 2.1 (true fs plumbing), 4.x (so the payoff is measurable).

---

## Explicitly rejected (do not re-litigate without new evidence)

From the research synthesis — each sounded good and was killed for cause:

- **Bayesian online changepoint / PELT for tap:** infeasible in the MCU trigger path;
  host-side its MAP estimate equals the 4.1 hinge fit's argmin-RSS at far higher cost.
- **Dodd–Deeds physics-model fits:** magnetic PEI-on-spring-steel sheets violate the
  model's assumptions for a huge fraction of the installed base; 3.1 delivers the same
  benefits (monotonicity, sane extrapolation) robustly.
- **Rational/Padé fits:** dominated by 3.1; spurious-pole guards not worth it.
- **Dual-coil ratiometric drift compensation:** no BTT Eddy variant has a second coil.
- **CUSUM integer trigger:** mooted once 4.1/4.3 land; revisit only if missed-tap
  reliability persists.
- **Teager–Kaiser energy operator:** noise cross-term swamps the signal at these count
  magnitudes.
- **Matched-filter tap templates:** per-printer learned state with staleness/cold-start
  failure modes; 4.1's physical gates do the job interpretably.
- **FIN_DIVIDER=2 (datasheet f_IN spec):** legitimate experiment, but rescales the
  freqval domain and invalidates every stored calibration fleet-wide — offline A/B on one
  machine first, promote only on positive data.
- **Auto-deglitch selection:** the 10 MHz default is already correct for essentially all
  BTT Eddy units; at most a warn-only check in calibration output.

## Suggested sequencing with effort estimates

| Order | Item | Est. effort | Printer time |
| --- | --- | --- | --- |
| 1 | 1.1 freq-domain offset | 1–2 h | 30 min |
| 2 | 1.2 trimmed mean | < 1 h | 30 min |
| 3 | 1.3 mesh NaN-fill | 1–2 h | 30 min |
| 4 | 1.4 tap ergonomics | 2–3 h | 1 h |
| 5 | 2.1 timestamp centroid + true fs | 2–3 h | 1–2 h (incl. recal experiment) |
| 6 | 2.2 per-phase rates | 2–3 h | 1 h |
| 7 | 3.1 monotone map | 1–2 days | 2 h + weeks of shadow logging |
| 8 | 3.2 sweep rework | 0.5–1 day | 1–2 h |
| 9 | 4.1 hinge fit (log-only) | 0.5 day | passive (weeks of logs) |
| 10 | 4.2 contact fit active | 0.5 day | 2 h + prints |
| 11 | 4.3 auto threshold | 1 day | 2 h |
| 12 | 5.1 NTC watchdog | 0.5 day | heat-soak session |
| 13 | 5.2 temp model | 2–4 days | multiple soak sessions |
| 14 | 6.1 fixed-point SOS | 2–3 days | reflash + full retest |

Items 1–4 are a comfortable single weekend including printer validation. The
shadow-logging items (3.1, 4.1) are designed to run passively during normal printing, so
calendar time ≫ effort time — start them early.
