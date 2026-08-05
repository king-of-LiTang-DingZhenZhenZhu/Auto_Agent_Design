# StrongARM latch schematic review

Status: **awaiting human approval**

Review artifacts:

- [Rendered schematic](strongarm_latch_schematic.svg)
- [Machine-readable connectivity](strongarm_latch_connectivity.json)

## Paper selection

The implementation follows Behzad Razavi, *The StrongARM Latch: A Circuit for
All Seasons*, Figure 1(b), the modified four-precharge-switch topology.  It does
not implement Figure 1(a) or the lower-kickback alternative in Figure 8.

## Node and polarity mapping

| Paper | Implementation | Meaning |
|---|---|---|
| `Vin1` | `vip` | M1 gate, positive comparator input |
| `Vin2` | `vin` | M2 gate, negative comparator input |
| `X` | `outn` | low when `vip > vin` |
| `Y` | `outp` | high when `vip > vin` |
| `P`, `Q` | `p`, `q` | input-pair drain / NMOS-latch source nodes |
| `CK` | `clk` | low reset, high evaluate |

The four precharge switches are `S1:P`, `S2:Q`, `S3:X/outn`, and
`S4:Y/outp`.  M3/M4 and M5/M6 are both cross-coupled; their gate connection is
to the opposite output.

## Automated evidence

The connectivity test compares all seven subcircuit ports and all eleven MOS
instances, including ordered Spectre MOS pins `D G S B`.  MOS body connections
are included in the JSON even though the SVG lists them as text instead of
drawing separate wires.

## Human checklist

- [ ] Confirm Figure 1(b), not Figure 1(a), is the desired paper variant.
- [ ] Confirm M3 gate is `Y/outp` and M4 gate is `X/outn`.
- [ ] Confirm M5 gate is `Y/outp` and M6 gate is `X/outn`.
- [ ] Confirm `vip > vin` produces `outp=high`, `outn=low`.
- [ ] Confirm all S1-S4 devices precharge when `clk=low`.
- [ ] Approve this diagram before treating the topology as paper-correct.
