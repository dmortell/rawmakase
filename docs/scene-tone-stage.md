# Scene tone stage (engine 5): design

Status: proposed, not implemented. Tracking issue: #354. Supersedes the per-control
approach of #359 (closed) and #366 (draft, kept for its measurements).

## Problem

On a Sony A7 IV set, Camera Raw 18.7's Shadows +100, Dehaze +40 and Whites +100 differ from
RAWmakase by photo brightness, by up to 39 ΔE00 on low-key photos (#354). The default
render is close (0.7–0.9 median ΔE00); the errors come from these sliders, not from
decoding.

Measured causes:

- **Stage order.** Camera Raw applies Whites, Shadows and Dehaze to scene values,
  before the camera profile's tone curve. With the same DNG and settings, swapping
  the profile's tone curve for a linear one changes Camera Raw's output exactly as
  applying that curve afterwards would: within 0.00006 MAE on gray ramps, against
  0.008–0.049 for the order RAWmakase uses. Engine 4 applies all three after
  `CameraProfile::finish` (`develop/pipeline/pixel.rs`): Whites and Dehaze in the
  composed basic curve, Shadows as the local-tone gain.
- **Scene adaptation.** Whites' strength follows the scene's brightness. A darker
  shot and the Exposure slider select different curves. Engine 4 measured the curves
  after the profile curve, with the slider, and only down to −2 EV, so every darker
  photo got the same, far too weak curve.
- **Surround.** Shadows and Dehaze depend on the surrounding area. Identical
  ramps in dark and bright surroundings differ by 0.072 MAE (Shadows +100) and
  0.014 (Dehaze +40) in Camera Raw, against 0.002 and 0 in RAWmakase. Whites does not.

Engine 4 has met each of these with another fitted model in the post-curve domain:
an averaged curve, then `Adaptive`, then (in #366) `Scene`, each with photo offsets
and trade-offs. Every step adds complexity, and some photos or the chart get worse.
The mismatch is structural, so the fix should be too.

## Goal and non-goals

Goal: new edits render these controls in the stage Camera Raw uses, with operators
measured in that stage, so they improve on held-out photos without regressions
beyond agreed limits.

Non-goals: an exact copy of Adobe's adaptive measures (they are proprietary and
will remain fitted); changing how saved edits render; a new UI.

## Proposal

### One process version

Add `engine: 5` for new edits. Engines 1–4 keep their pipelines and their
per-control operator fields (`whites_model`, `contrast_model`, …) unchanged. Engine 5
implies the scene-stage operators. It adds no per-control model field, so the
version replaces the growing list of operator flags for these controls.
`Recipe::validate` accepts 5, `with_profiles` sets it, and `update_process`
(Calibration's Update) stays at 4 until it is decided whether older edits may move
(see open questions).

### Pipeline (engine 5)

1. Camera RGB → white balance → camera profile matrix and HueSatMap → Exposure and
   the DNG exposure ramp (unchanged).
2. **Scene stage (new)**: scene-linear values before the profile's look table and
   tone curve. Holds the controls the stage-order test places here, in the order
   Camera Raw applies them.
   - Global curves: Whites, and Blacks if measured there, applied to the brightest
     and darkest channel, in scene-linear ProPhoto RGB.
   - Local gain: Shadows (and Highlights and Dehaze if measured here). The base map
     is built from scene log-luminance of the reduced copy instead of the toned image.
   - The photo measures these controls follow (the highlight key, the local base)
     are taken here, before the profile curve.
3. Profile look table and tone curve, look curve (`finish`, unchanged).
4. The remaining Basic curves (Contrast, and Blacks if measured after), point and
   parametric curves, color mixer, grading, output (unchanged).

Masks apply their Whites, Shadows and Dehaze deltas in the same stage.

The stage must be identical across the CPU reference (`pipeline/pixel.rs`
`scene_stage`), the GPU port (`gpu/develop.wgsl`: `local_gain` and the global
scene curves move before `profile_finish`; the `P_TONE_ONLY` map pass returns scene
values) and the stage cache. With every scene slider at 0, engine 5 must render
bit-identically to engine 4. That is a test, and the first milestone ships nothing
else.

### Measurement

All references are Camera Raw 18.7 renders of synthetic DNGs (no photos, no Adobe
files in the repository):

- Stage order per control: the same DNG with a standard and a linear
  `ProfileToneCurve`. A control that commutes with the curve belongs in the scene
  stage. Also test it against a profile look table (synthetic looks in
  `tests/corpus/looks`) to place it before or after the look.
- A linear profile tone curve makes Camera Raw's output equal to the scene stage's
  result, so curves and gains are read directly, without inverting a tone curve.
- Global controls: `synthetic-d65` shot at sensor exposures −7…+1 EV × Exposure
  slider −4…+4 (generator and 180 Whites references from #366).
- Local controls: neutral ramps and patches in controlled surroundings (dark/bright,
  near/far, sizes) at several sensor exposures, plus the chart.
- Fitting scripts live in `scripts/corpus/` and write data files under
  `develop/`, as today. The photo statistic each control follows is fitted on a fixed
  training split only.

### Acceptance gates

A control moves to the scene stage only when, against engine 4:

- **Held-out photos**: CC0 raw.pixls.us samples split into training and held-out
  sets before fitting, plus the reporter's A7 IV RAWs if they share them (kept
  private). Mean and median ΔE00 to Camera Raw improve at ±50 and ±100. No
  held-out photo gets worse by more than 1.0 ΔE00 at ±50 or 2.0 at ±100.
- **Corpus charts**: `camera_raw_parity_does_not_regress` holds or improves. Any
  re-bless names the cases and the reason.
- **Default render**: bit-identical to engine 4 at slider 0 (above).
- **GPU**: CPU/GPU equivalence tests cover the stage. Preview cost stays within 10% on
  the benchmark.

A control that fails its gate stays on its engine 4 operator, documented as such.

## Milestones (one PR each; none merged without its gate)

1. **Stage-order survey.** Measurement only: every Basic tone slider (Exposure,
   Contrast, Highlights, Shadows, Whites, Blacks, Clarity, Dehaze) against the linear
   profile curve and a look table. Result: which controls belong in the scene stage.
2. **Engine 5 skeleton.** The scene stage on CPU and GPU with engine 4's operators
   unchanged. Engine 5 renders identically to engine 4. Mechanical, no rendering
   change.
3. **Whites** in the scene stage (reusing #366's measurements), through the gates;
   closes #366.
4. **Shadows and Highlights** with a scene-domain local map.
5. **Dehaze.**
6. Docs (`tone-controls.md`, `parity-gaps.md`, `color-pipeline.md`, architecture
   and code map) and release notes.

## Risks

- How Camera Raw chooses a photo's curve or gain stays fitted. #366 found the
  synthetic chart and photos select Whites' curve 0.45 EV apart, and no simple
  statistic explained both. Expect some residual per-photo error.
- Older builds reject `engine: 5` (and unknown operator values) in a saved edit, so
  an edit made in a newer build is protected, not rendered, in an older one. That is
  the existing policy for new versions; release notes must say so.
- The local map is built from different values than engine 4's, so its cost and the
  stage cache's keys need checking.
- Stage-order findings are on neutral ramps. Color handling in the scene stage is
  fitted on the chart's colored patches and checked on photos.

## Open questions

- Should Calibration's Update move engine 4 edits to engine 5? Saved edits must
  not change silently; an explicit Update is the user's choice in Lightroom.
- Are the regression limits above right?
- Request the reporter's A7 IV RAWs and Camera Raw renders for the held-out set?
