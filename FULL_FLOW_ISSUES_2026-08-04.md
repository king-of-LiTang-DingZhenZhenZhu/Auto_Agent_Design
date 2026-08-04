# Unified Physical Flow Issue Log (2026-08-04)

## Scope and current state

- Branch: `feature/unified-physical-flow`
- Inspected commit: `657a3b2 feat: complete requirements-to-signoff orchestration`
- Host: `mn01`
- EDA: Spectre 18.1, Virtuoso IC6.1.8, Calibre 2024.2
- PDK root: `/share/home/chenhaonan/PDKS/TSMC28nm`
- The electrical flow is operational through real BO, Design Audit, and PVT.
- A two-stage OTA result has passed all 27 PVT corners when nominal and PVT
  budgets are supplied separately.
- The physical flow currently stops in the first Virtuoso OA replay step.
  GDS, DRC, and LVS have therefore not completed.

## 1. PDK installation was incomplete after the initial copy

Status: resolved locally.

The copied PDK initially did not contain the root `tsmcN28` Virtuoso library.
It was restored from the existing PDK archive, after which the installer was
run for Skill PCell, LO, 0.9/1.8 V, and `1P10M_5X2Y2R`.

The installer still printed `PDK installation failed`, apparently in optional
QCI/RCX steps, but the assets required by this flow are present:

- `tsmcN28/tech.db`
- `nch_mac/layout/layout.oa`
- `pch_mac/layout/layout.oa`
- `nmoscap/layout/layout.oa`
- `rnod/layout/layout.oa`
- Calibre DRC/LVS decks for `1P10M_5X2Y2R`

Strict project validation subsequently passed.

## 2. Physical Python dependencies selected incompatible source builds

Status: resolved locally, dependency constraints should be tightened.

Installing the unbounded physical requirements selected source builds that
were incompatible with GCC 4.8. Compatible wheels were installed instead:

- `gdstk==0.9.62`
- `z3-solver==4.13.4.0`

The physical requirements should pin versions that have wheels for the
supported Python/platform combination.

## 3. `models/spectre/toplevel.scs` only supports TT

Status: fixed in the project PDK profile and local ignored `.env`.

The PDK `models/spectre/toplevel.scs` is a valid direct include for nominal
simulation, but it defines only:

```spectre
section top_tt
```

It does not define `top_ss` or `top_ff`. The original profile nevertheless
mapped PVT to `top_tt/top_ss/top_ff`. This caused all SS/FF Spectre runs to
terminate with `No section found`.

The real process-corner entry points are in:

`cln28hpcp_1d8_elk_v1d0_2p2_shrink0d9_embedded_usage.scs`

using these sections:

- TT: `ttmacro_mos_moscap`
- SS: `ssmacro_mos_moscap`
- FF: `ffmacro_mos_moscap`

Directly selecting the lower-level `tt/ss/ff` sections in the large model
file is also insufficient: those sections define BSIM models but do not make
the `nch_mac/pch_mac` wrapper subcircuits available. The `*macro_mos_moscap`
sections are required for the generated topology netlists.

`pdk_profiles.py --check-files` previously checked only that the model file
existed. It now also checks that the configured nominal and process sections
are actually defined in that file.

## 4. `.env` cannot contain every PDK override understood by `pdk_profiles.py`

Status: open configuration consistency issue.

`pdk_profiles.py` reads `PDK_PROCESS_SECTIONS` directly from the environment,
but the current `pydantic-settings` model rejects that key as an extra field
when it is placed in `.env`. Exporting it in the shell works, while persisting
it in `.env` fails application startup.

The Settings schema and the independent PDK environment parser should have a
single source of truth. The local `.env` currently avoids this key.

## 5. Structured `metric_goals` crashed PVT/export source selection

Status: fixed with regression coverage.

`optimization_log.json.targets` can contain a nested `metric_goals` object.
`virtuoso_export/exporter.py::_load_targets()` previously ran `float(value)`
on every non-null target, producing:

```text
TypeError: float() argument must be a string or a real number, not 'dict'
```

The loader now accepts only scalar numeric targets and ignores structured or
invalid values. A focused export regression test covers a nested
`metric_goals` object.

## 6. Failed simulations were reported as failed metrics with empty errors

Status: open reporting issue.

When Spectre failed because `top_ss/top_ff` did not exist, the PVT CSV showed
all metrics as blank and listed every metric under `failed_metrics`, while
`error_message` was empty. This made an infrastructure/model configuration
failure look like an electrical design failure.

PVT result generation should propagate the simulator read-in error and should
not classify unavailable metrics as ordinary spec misses.

## 7. Nominal and PVT targets need separate budgets

Status: supported by the API, not exposed clearly by the unified CLI/input.

Setting nominal gain to 45 dB also made every PVT corner require 45 dB. A
nominal solution at 45.9 dB then failed six 0.9 V/125 C corners even though
the worst gain was still 39.1 dB.

The correct budget used for the successful PVT run was:

- nominal design target: gain >= 45 dB
- system/PVT acceptance target: gain >= 20 dB

`run_design_flow(..., pvt_targets=DesignTarget(...))` supports this, and the
result passed all 27 real corners. `run_full_flow.py` and the structured input
format should expose the same distinction so users do not need a Python API
call.

## 8. Default soft objectives can select a less PVT-robust feasible point

Status: open optimization-policy issue.

An attempted input with hard gain >= 20 dB and a soft gain target of 45 dB
still retained the default PM target and power minimization objectives. The
optimizer selected a 59.5 uW solution with 38.9 dB nominal gain instead of the
more robust 45.9 dB solution. That low-power solution failed 15 PVT corners
because high-voltage PM fell below 30 degrees and several transient responses
could not produce a settling measurement.

For PVT-margin optimization, all competing default objectives must be made
explicit, or candidate ranking must include corner margin rather than nominal
metrics alone.

## 9. Physical bridge overwrote qualification actions

Status: observed, not yet fixed.

In the first strict 60 dB example, nominal BO and Review did not pass, but the
physical bridge returned `next_action=run_pvt`. Physical preparation must not
overwrite a more specific failure/Review action when qualification has not
passed.

## 10. Configured Calibre binary path was wrong

Status: corrected for runtime commands; example configuration should be
checked.

The initially configured path omitted the Calibre build suffix:

```text
.../aoj_cal_2024.2_36/bin/calibre
```

The installed executable is:

```text
.../aoj_cal_2024.2_36.24/bin/calibre
```

After correction, physical preflight passed for Virtuoso, Calibre, the PDK
library, and both rule decks.

## 11. Relative physical paths were evaluated from the physical workdir

Status: fixed in `physical_bridge.py`.

The bridge accepted a relative project path and generated relative artifact
paths. It then ran Virtuoso with `cwd=<project>/physical`, causing a replay
path such as:

```text
outputs/proj/physical/outputs/proj/physical/oa/schematic.il
```

The bridge now resolves the project directory before preparing the physical
run, so generated OA/GDS paths and replay commands are absolute.

## 12. Virtuoso `-nograph` cannot start its internal Xvnc on this host

Status: current physical blocker.

With correct absolute paths, Virtuoso reaches its display startup. In
`-nograph` mode it ignores the existing `DISPLAY` and tries internal displays
`:80` through `:89`. Every internal Xvnc exits with:

```text
Fatal server error:
could not open default font 'fixed'
```

The Cadence `cdsVncserver` chooses `/usr/share/X11/fonts`, but that font tree
is unusable on this host. An existing Cadence Xvnc process demonstrates that
`/tmp/xfonts_misc` is usable, but the supplied `cdsVncserver` does not expose
a supported font-path override.

Using the current user's existing VNC display `:18` requires omitting
`-nograph`. A process-local test changed the command to `virtuoso -replay`,
which connected without the previous display/font errors but did not exit on
its own. The generated replay SKILL or batch wrapper likely needs an explicit
successful exit call after saving/closing the OA cellview.

No GDS has been produced yet, so Calibre DRC/LVS has not run.

## 13. Physical failure state loses the blocker detail

Status: open state-reporting issue.

`physical_state.json` correctly contains `errors: ["schematic_oa failed"]`,
but the parent `flow_state.json` can show:

```json
{
  "physical_status": "physical_blocked",
  "physical_blocker": null,
  "next_action": "fix_physical_blocker"
}
```

The bridge should copy the physical result error into `physical_blocker` so
the top-level report is actionable.

## 14. Full test suite has pre-existing environment/version failures

Status: open, unrelated to the PDK/export fixes.

The complete circuit-agent suite ran 197 tests and reported these additional
failures:

- Two StrongARM tests fail because NumPy 1.26 does not provide
  `np.trapezoid`; the project requirements allow versions where only
  `np.trapz` is available.
- One external PDK profile test is contaminated by the local `.env`
  `GMID_TABLE_PATH`, causing its temporary unit-profile gm/Id data to be
  ignored.

Focused tests for PDK section validation, PVT patching, physical preparation,
and structured export targets passed.

## Run evidence

### `two_stage_ota_real_smoke`

- Strict example, 5 BO iterations
- Best: gain 33.0 dB, GBW 154 MHz, PM 56.7 degrees, power 437 uW
- Did not meet gain 60 dB and PM 60 degrees
- Review produced no passing candidate

### `two_stage_ota_physical_smoke`

- Initial relaxed nominal result: gain 29.2 dB, GBW 63.7 MHz, PM 58.2
  degrees, power 98.9 uW, SR 56.1 MV/s, settling 25.1 ns
- Initial PVT with correct model sections: 21/27 corners passed; six
  0.9 V/125 C corners missed gain >= 20 dB

### `two_stage_ota_pvt_smoke`

- 30-iteration nominal design with gain margin
- Nominal: gain 45.9 dB, GBW 34.9 MHz, PM 67.6 degrees, power 139 uW,
  SR 36.6 MV/s, settling 19.3 ns
- With separate PVT acceptance target gain >= 20 dB: 27/27 corners passed
- Worst PVT values included gain 39.1 dB, GBW 27.4 MHz, PM 62.9 degrees,
  power 140 uW, SR 34.3 MV/s, and settling 25.1 ns
- Physical handoff, OA plans/SKILL, instance mapping, CDS library, and LVS CDL
  were generated
- Physical sign-off currently stops at `schematic_oa` due to the Virtuoso
  batch/display/exit issue described above

## Recommended next actions

1. Add an explicit Virtuoso runtime mode that uses an existing `DISPLAY` and
   omits `-nograph`, while keeping `-nograph` as the default.
2. Ensure every generated OA/stream-out replay script exits Virtuoso with a
   success/failure status after closing databases.
3. Add an integration test for relative `--project` paths and replay command
   paths.
4. Expose separate nominal budgets and `pvt_targets` in `run_full_flow.py` and
   structured requirements.
5. Propagate simulator errors and physical blockers into top-level reports.
6. After Virtuoso batch completion, verify a non-empty GDS, then inspect real
   Calibre DRC/LVS reports and bounded ECO behavior.
