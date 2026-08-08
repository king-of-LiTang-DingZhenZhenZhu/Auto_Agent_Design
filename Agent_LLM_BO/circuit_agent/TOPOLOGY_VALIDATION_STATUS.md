# 拓扑可行性与验证状态

Last updated: 2026-08-08

This document records the electrical validation state of every topology in
`topologies.TOPOLOGY_REGISTRY`. A topology being registered, renderable, or
covered by unit tests does not by itself mean that the circuit is electrically
valid.

## 状态定义

- **已验证**: Real Spectre nominal simulation meets the documented targets,
  and the required PVT sweep passes. The status applies only to the PDK,
  voltage domain, targets, load, and testbenches cited in the evidence.
- **特定情况可行**: Real Spectre simulation demonstrates the intended
  circuit behavior under stated conditions, but the topology fails outside
  those conditions or does not yet have complete nominal/PVT qualification.
- **未验证**: Complete real-Spectre evidence is absent, or a real simulation
  has exposed a blocker. This does not mean that the underlying architecture is
  theoretically infeasible.

All status changes must cite a persistent `results.json` and, for **已验证**,
a real `pvt/pvt_results.json` with `pvt_pass=true`. Dry-run, generated-netlist,
unit-test, or schematic/layout evidence must not promote electrical status.

## Registry Summary

| Topology | Domain / architecture | Status | Current qualification or blocker |
|---|---|---|---|
| `5t_ota` | Single-stage 5-transistor OTA | **特定情况可行** | Default-parameter AC validation at TSMC28, 0.9 V, TT gives 24.38 dB gain, 135.61 MHz GBW, and 92.29 deg PM with critical OP passing. No PVT qualification. |
| `two_stage_ota` | Two-stage Miller OTA | **已验证** | TSMC28 evidence passes nominal targets and all 27 `tt/ss/ff x (vmin=0.9/vtyp=0.9/vmax=1.1 V) x -40/27/125 C` PVT corners for the cited smoke target. See evidence below. |
| `pmos_input_two_stage_ota` | PMOS-input two-stage Miller OTA | **特定情况可行** | After correcting the internal `vip`/`vin` mapping, default-parameter TSMC28, 0.9 V, TT AC/SR/ST runs give 67.01 dB gain, 42.76 MHz GBW, 81.07 deg PM, 50.36 V/us minimum SR, and 25.05 ns settling. `Mtail` remains linear at the default point and no PVT qualification exists. |
| `mzc_two_stage_ota` | Miller zero-cancellation/feedforward two-stage OTA | **特定情况可行** | Default-parameter AC validation at TSMC28, 0.9 V, TT gives 42.39 dB gain, 101.50 MHz GBW, and 55.52 deg PM with critical OP passing. No PVT qualification. |
| `pmos_input_mzc_two_stage_ota` | PMOS-input MZC two-stage OTA | **特定情况可行** | After correcting the main and feedforward input mappings, default-parameter TSMC28, 0.9 V, TT AC/SR/ST runs give 66.01 dB gain, 42.80 MHz GBW, 78.84 deg PM, 51.20 V/us minimum SR, and 23.18 ns settling. `Mtail` and `Mtailff` remain linear at the default point and no PVT qualification exists. |
| `folded_cascode` | Single-stage folded-cascode OTA | **特定情况可行** | Default-parameter AC validation at TSMC28, 0.9 V, TT gives 60.67 dB gain, 133.11 MHz GBW, and 80.89 deg PM. Critical OP passes, with several devices near the saturation boundary. No PVT qualification. |
| `folded_cascode_two_stage` | Folded-cascode plus second-stage OTA | **未验证** | Default point has 103.40 dB gain and 253.60 MHz GBW but only 12.30 deg PM; it is functionally unstable/marginal and needs compensation repair before qualification. |
| `nmcnr_three_stage` | Nested-Miller three-stage OTA with nulling resistor | **特定情况可行** | Default-parameter AC validation at TSMC28, 0.9 V, TT gives 88.06 dB gain, 27.18 MHz GBW, and 71.65 deg PM with critical OP passing. No PVT qualification. |
| `mnmc_three_stage` | Multipath nested-Miller three-stage OTA | **未验证** | Default point converges and has 69.76 dB gain and 170.42 MHz GBW, but PM is -94.52 deg. Compensation/stability is a blocker. |
| `nmcf_three_stage` | Nested-Miller/feedforward three-stage OTA | **未验证** | Default point converges and has 85.34 dB gain and 184.18 MHz GBW, but PM is -54.58 deg. Compensation/stability is a blocker. |
| `strongarm_latch` | Dynamic StrongARM latch comparator | **未验证** | Decision-polarity testbenches and physical adapter exist, but no retained real nominal/PVT electrical qualification. |
| `bandgap_ptat` | Hierarchical PNP PTAT reference | **特定情况可行** | At `VDD=1.1 V`, TT temperature simulation is monotonic PTAT from `0.3849 V @ -40 C` to `0.6113 V @ 125 C`. At `VDD=0.9 V`, low-temperature PNP voltage leaves insufficient PMOS-mirror headroom and the output approaches the supply. Full 1.1 V startup/PSRR/line/PVT sign-off is still absent. |
| `banba_sub1v_bandgap` | Banba current-summing sub-1-V bandgap | **未验证** | Known blocker: at `VDD=1.1 V`, the autonomous startup remains active but the circuit settles near the zero-current state (`Vref=0.342 mV`, `startup_success=false`). Startup injection must be repaired before nominal metrics are meaningful. |
| `leung_mok_sub1v_bandgap` | Leung-Mok 2002 sub-1-V bandgap | **未验证** | Paper-mapped topology and dedicated testbenches exist; no retained real nominal/PVT qualification. |
| `capless_ldo` | PMOS-pass capacitor-less LDO | **未验证** | Dedicated loop, load, PSR, and transient testbenches exist; no retained real nominal/PVT qualification. |
| `dfc_capless_ldo` | DFC capacitor-free LDO | **未验证** | Paper-mapped topology and dedicated testbenches exist; no retained real nominal/PVT qualification. |

## Op-Amp Single-Pass Validation

The retained 2026-08-08 validation uses one real Spectre AC/DC run per op-amp
topology with `get_default_params()` and the topology's primary AC testbench.
It does not run BO, gm/Id sizing, SR/ST, Review, or PVT. The evidence directory
is:

`outputs/opamp_topology_default_validation_20260808_111005/`

`main.py --max-iter 1` is not used for this purpose. In gm/Id mode, `main.py`
can run a `DEFAULT_PARAMS` baseline, a gm/Id initial point, and one BO trial.
The optimizer also enqueues the initial candidate, so the sole BO trial usually
repeats the initial point instead of exploring a repair candidate.

The single-pass classifications mean:

- `default_functional`: Spectre converges; Gain, GBW, and PM are finite and
  positive; gain is at least 20 dB; PM is at least 45 deg; and critical OP has
  no linear or unknown devices.
- `default_marginal`: basic AC metrics exist, but practical gain/PM or critical
  OP evidence is marginal.
- `default_invalid`: the default point lacks valid AC metrics, has non-positive
  PM, or otherwise fails basic function. This is a default-point result, not a
  proof that the architecture can never be sized successfully.

| Topology | Default result | Gain | GBW | PM | Critical OP |
|---|---|---:|---:|---:|---|
| `5t_ota` | functional | `24.38 dB` | `135.61 MHz` | `92.29 deg` | pass |
| `two_stage_ota` | functional | `47.14 dB` | `98.87 MHz` | `58.82 deg` | pass; `Mdiff2` near edge |
| `pmos_input_two_stage_ota` | marginal | `67.01 dB` | `42.76 MHz` | `81.07 deg` | fail; `Mtail` linear at default sizing |
| `mzc_two_stage_ota` | functional | `42.39 dB` | `101.50 MHz` | `55.52 deg` | pass; `Mdiff2` near edge |
| `pmos_input_mzc_two_stage_ota` | marginal | `66.01 dB` | `42.80 MHz` | `78.84 deg` | fail; `Mtail` and `Mtailff` linear at default sizing |
| `folded_cascode` | functional | `60.67 dB` | `133.11 MHz` | `80.89 deg` | pass; five critical devices near edge |
| `folded_cascode_two_stage` | marginal | `103.40 dB` | `253.60 MHz` | `12.30 deg` | pass; five critical devices near edge |
| `nmcnr_three_stage` | functional | `88.06 dB` | `27.18 MHz` | `71.65 deg` | pass |
| `mnmc_three_stage` | invalid | `69.76 dB` | `170.42 MHz` | `-94.52 deg` | pass; stability fails |
| `nmcf_three_stage` | invalid | `85.34 dB` | `184.18 MHz` | `-54.58 deg` | pass; stability fails |

The PMOS-input entries above were re-run after correcting their internal input
polarity while preserving the public `(vip vin vout ibias vdd vss)` contract.
The retained post-fix AC/SR/ST evidence is:

`outputs/pmos_input_polarity_fix_validation_20260808_111753/`

| Topology | Gain | GBW | PM | Min SR | 0.1% settling |
|---|---:|---:|---:|---:|---:|
| `pmos_input_two_stage_ota` | `67.01 dB` | `42.76 MHz` | `81.07 deg` | `50.36 V/us` | `25.05 ns` |
| `pmos_input_mzc_two_stage_ota` | `66.01 dB` | `42.80 MHz` | `78.84 deg` | `51.20 V/us` | `23.18 ns` |

All six Spectre analyses completed with zero errors. The remaining default-point
critical OP failures are sizing/headroom limitations, not feedback-polarity
failures, so these topologies remain conditionally feasible rather than fully
verified.

## Verified Evidence

### `two_stage_ota`

- PDK profile: `tsmc28`.
- Nominal result: `outputs/two_stage_ota_pvt_smoke/results.json`.
- Nominal metrics: gain `45.87 dB`, GBW `34.88 MHz`, phase margin
  `67.60 deg`, power `138.7 uW`, minimum slew rate `36.64 V/us`, and
  settling time `19.25 ns`; `all_targets_met=true`.
- PVT result: `outputs/two_stage_ota_pvt_smoke/pvt/pvt_results.json`.
- PVT coverage: 27/27 corners passed. Worst retained values are gain
  `39.12 dB`, GBW `27.4 MHz`, phase margin `62.88 deg`, power `0.14 mW`,
  slew rate `34.27 V/us`, and settling time `25.13 ns`.
- Scope: this evidence validates the cited target and load. It is not a claim
  that every target inside the topology metadata range is feasible.

## Conditional Evidence

### `bandgap_ptat`

The default `tsmc28` `VDD=0.9 V` run starts at room temperature, but it is not
valid across the full temperature range. At `-40 C`, the PNP emitter nodes and
`Vref` approach the supply, leaving only tens of millivolts across the PMOS
mirror devices. A DC operating-point transition occurs around `-4 C` to
`-3 C`.

Raising the supply to `1.1 V` restores mirror headroom and produces the intended
monotonic PTAT response:

| Temperature | Vref at 1.1 V |
|---:|---:|
| `-40 C` | `0.3849 V` |
| `0 C` | `0.4394 V` |
| `27 C` | `0.4767 V` |
| `60 C` | `0.5224 V` |
| `125 C` | `0.6113 V` |

Evidence directory:
`outputs/bandgap_ptat_validation_20260805_165934/`. The root `results.json`
records the 0.9 V full-testbench run; the 1.1 V temperature evidence is under
`temperature_diagnostic_vdd_1p1/`.

Required work before promotion to **已验证**:

1. Define explicit PTAT output, startup, power, PSRR, and line-regulation goals.
2. Run all dedicated nominal testbenches at the qualified supply.
3. Pass the required process, voltage, and temperature corners without mirror
   headroom or operating-point blockers.

## Known Blockers

### `banba_sub1v_bandgap`

At `VDD=1.1 V`, Spectre converges but the circuit does not escape the startup
state:

- `Vref = 0.342 mV`, `startup_success=false`;
- `VRS = 0.3277 V`, `SUP` remains near ground, so startup stays asserted;
- `Va = 0.788 V`, `Vb = 1.080 V`, and `Vg = 1.098 V`;
- the error amplifier therefore switches off the P1/P2/P3 core mirror.

The VREF detector and automatic shutoff polarity behave as designed. Replacing
the startup LVT devices with regular-Vt devices does not fix the failure.
Swapping the error-amplifier inputs drives `Vref` to the supply, which is also
an invalid state. The remaining repair area is the startup injection mechanism
and its interaction with the unequal Va/Vb branch impedances.

Evidence directory:
`outputs/banba_sub1v_bandgap_validation_vdd_1p1_20260805_173123/`.

## Updating This Document

When new evidence is produced:

1. Confirm `pdk_profile_used.json` matches the intended PDK and voltage domain.
2. Record exact targets, load, supply, temperature, and testbench set.
3. Check convergence, requested metrics, startup state, and critical operating
   points; convergence alone is not a pass.
4. Use **特定情况可行** when only a subset of the intended operating
   domain has passed.
5. Use **已验证** only after nominal targets and real PVT both pass.
6. Keep failed evidence in the notes until a newer retained result demonstrates
   that the blocker is resolved.
