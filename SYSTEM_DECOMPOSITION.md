# System-Level Decomposition

`system_decomposition.py` converts a system-level circuit request into a
validated, machine-readable architecture and block graph before topology
generation or BO starts.

## Flow Position

```text
system request
  -> architecture rule
  -> system_design.json
  -> parent topology + hierarchy.json
  -> child BO + design-flow qualification
  -> frozen artifacts
  -> parent BO + design-flow qualification
```

`system_design.json` explains why blocks exist and how their targets were
derived. `hierarchy.json` remains the executable child-artifact contract used
by `hierarchical_flow.py`.

## Input

The input JSON uses SI units and must identify `system_type`:

```json
{
  "system_type": "bandgap",
  "original_requirement": "1.2 V bandgap reference",
  "targets": {
    "vref_v": 1.2,
    "vref_tolerance_v": 0.005,
    "tempco_ppm_per_c": 20,
    "psrr_db": 50,
    "line_regulation_v_per_v": 0.001,
    "startup_time_s": 0.000005,
    "power_w": 0.0002
  },
  "custom_specs": {
    "opamp_gain_db": 75,
    "opamp_pvt_gain_db": 65
  }
}
```

## Commands

Generate only the decomposition:

```bash
python system_decomposition.py \
  --requirements bandgap_requirements.json \
  --output bandgap_system_design.json
```

Generate the executable parent project as well:

```bash
python system_decomposition.py \
  --requirements bandgap_requirements.json \
  --project bandgap_project
```

The project contains:

```text
bandgap_project/
├── system_design.json
├── requirements.json
├── hierarchy.json
├── bandgap_ptat.cir
└── tb_bandgap_ptat_*.scs
```

Then run the existing staged flow:

```bash
python hierarchical_flow.py --project bandgap_project --simulate
```

## Executable Data Model

- `SystemDesignRequest`: top-level system type, targets, voltage domain,
  architecture hint and constraints.
- `SystemDesignSpec`: selected architecture, parent topology, blocks,
  connections, rationale, assumptions and unresolved requirements.
- `SystemBlockSpec`: block function, implementation policy, topology
  candidates, selected topology, interface, dependencies, operating
  conditions, budget, nominal targets and PVT targets.
- `TargetDerivation`: source, rule, assumptions, nominal value, PVT value and
  design margin for each derived child metric.

`parent_internal` blocks remain inside the parent topology.
`hierarchical_child` blocks become `ExecutableChildSpec` entries and must pass
nominal or validated Review results, Design Audit, and their independently
declared PVT targets before freezing.

## Current Rule

The first registered rule is `bandgap`:

- architecture: `opamp_assisted_pnp_bandgap`;
- parent topology: `bandgap_ptat`;
- internal blocks: core, bias and startup;
- hierarchical child: `two_stage_ota` error amplifier;
- child Gain/GBW/PM/power/load targets include derivation records and separate
  PVT thresholds.

The current opamp defaults are conservative rules, not extracted small-signal
loop requirements. Missing top-level metrics are listed under
`unresolved_requirements` instead of being silently invented.

## Adding LDO

Add one registered LDO decomposition rule before adding its parent topology:

1. Select an LDO architecture from load current, dropout, stability, transient,
   PSRR, noise and quiescent-current targets.
2. Emit reference, error amplifier, pass device, feedback, compensation and
   protection blocks.
3. Derive child nominal/PVT targets with explicit assumptions and margins.
4. Mark reusable optimized blocks as `hierarchical_child` and embedded devices
   as `parent_internal`.
5. Implement the parent topology so its `hierarchy.json` matches the planned
   child blocks.

Architecture and budget equations belong in the domain knowledge base. Python
rules compute and validate values; the flow does not evaluate arbitrary formula
strings from Markdown or JSON.
