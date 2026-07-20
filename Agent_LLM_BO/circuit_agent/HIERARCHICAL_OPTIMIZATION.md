# Hierarchical Optimization

`hierarchical_flow.py` runs a staged flow for topologies that declare child
blocks: child BO → child PVT → frozen child artifact → parent BO → parent PVT.

## Generate a Parent Project

The topology writes `hierarchy.json` together with `requirements.json`.
For example, `bandgap_ptat` declares a frozen `two_stage_ota`
error-amplifier block.

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design

python -c "
from models import DesignTarget
from topologies import get_topology
get_topology('bandgap_ptat').write_project(
    'bandgap_project',
    targets=DesignTarget(power_w=1e-3, load_cap_f=1e-12),
)
"
```

## Run the Staged Flow

The default is dry-run mode. It exercises the command path but cannot pass the
PVT gate because no real Spectre measurements are produced.

```bash
python hierarchical_flow.py --project bandgap_project --max-iter 30
```

Run the actual child and parent BO/PVT sequence only on a Cadence/Spectre
machine:

```bash
python hierarchical_flow.py \
  --project bandgap_project \
  --max-iter 50 \
  --simulate
```

Each qualified child is copied to
`bandgap_project/child_blocks/<block_id>/artifact/`. The artifact contains the
final netlist, results, PDK snapshot, PVT summary, checksums, and interface
validation metadata. A valid artifact is reused on later runs; use
`--force-child` to optimize it again.

## Adding a New Parent Topology

Override `BaseTopology.get_hierarchical_blocks()` and return one or more
`HierarchicalBlockSpec` values. The spec declares the child topology, expected
subckt and port order, child targets, and the parent parameters that receive
the frozen `netlist` and `results` paths. Parent BO must keep the policy as
`frozen_macro`; do not add child W/L parameters to the parent search space.

Child and parent must use the same PDK profile and voltage domain. A child that
fails nominal targets, PVT, profile matching, checksum validation, or the
declared subckt interface is rejected before parent BO starts.
