# Paper schematic review gate

Paper-derived topologies must pass a human schematic review before their
connectivity is treated as approved.  The review uses three files:

- `*_connectivity.json`: ordered subcircuit ports and ordered device pins;
- `*_schematic.svg`: deterministic drawing generated from the JSON;
- `*_schematic_review.md`: paper figure mapping, implementation deviations,
  polarity, and the human checklist.

The JSON is the machine-readable review source.  The SVG is not drawn
independently.  `schematic_review.py` generates it from the JSON and compares
the same ordered terminals with the topology-generated Spectre subcircuit.

## Required sequence

1. Render and inspect every paper page containing the selected schematic.
2. Record the selected figure, device roles, port polarity, pin order, parameter
   relations, and any ambiguity in the connectivity JSON.
3. Render the SVG and have a human confirm the circuit before implementing or
   correcting the topology.
4. Add a unit test that calls `validate_netlist_connectivity()` on the generated
   topology netlist.
5. Keep the committed SVG synchronized with `render_schematic_svg()`.

Do not use an AI-generated bitmap as connectivity evidence.  It may be useful
for presentation, but it is not an electrically checkable source.

## Render and validate

Run from `Agent_LLM_BO/circuit_agent` after activating the project environment:

```bash
python schematic_review.py \
  ../../knowledge_base/Comparator_knowledge_base/topologies/schematics/strongarm_latch_connectivity.json \
  ../../knowledge_base/Comparator_knowledge_base/topologies/schematics/strongarm_latch_schematic.svg \
  --topology strongarm_latch
```

The command exits nonzero when the subcircuit ports, instance set, ordered
device nets, or explicitly checked primitive/macro model differ.

## Review boundary

Passing the automated comparison means the topology netlist matches the
reviewed JSON.  It does not prove that the interpretation of the paper is
correct.  That decision remains the human SVG review gate.  Simulation, PVT,
noise, mismatch, and startup verification remain separate later gates.
