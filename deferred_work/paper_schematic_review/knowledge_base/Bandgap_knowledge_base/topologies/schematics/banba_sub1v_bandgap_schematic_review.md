# Banba sub-1-V bandgap schematic review

Status: **awaiting human approval**

Review artifacts:

- [Rendered schematic](banba_sub1v_bandgap_schematic.svg)
- [Machine-readable connectivity](banba_sub1v_bandgap_connectivity.json)

## Paper selection

The current implementation uses Banba et al., *A CMOS Bandgap Reference
Circuit with Sub-1-V Operation*, Figure 2 as the functional current-summing
core.  C1/C2 follow the stabilization intent shown in Figure 5.

The SVG intentionally distinguishes the paper-aligned core from implementation
adaptations.  It must not be reviewed as a transistor-exact reproduction of
the complete Figure 5 test chip.

## Paper-aligned core

- The error amplifier forces `Va = Vb` with `Vb` on its non-inverting input and
  `Va` on its inverting input.
- Equal P1/P2/P3 devices share `VG`, giving `I1 = I2 = I3` ideally.
- `R1 = R2 = R12`.
- The Vb branch sums `VBE/R2` and `delta-VBE/R3` currents.
- P3 mirrors the summed current into R4 to generate VREF.
- Spectre PNP order is `C B E`: Q1 is `(vss vss va)` and QN is
  `(vss vss vdn)`.

## Explicit deviations from Figure 5

| Item | Paper test chip | Current implementation |
|---|---|---|
| Error amplifier | transistor-level amplifier in Figure 5 | frozen NMOS-input `two_stage_ota` macro |
| Startup | external `PONRST` | autonomous Cstart/Rstart/Mstart pulse |
| Diode ratio | `N=100` | `N=8` |
| Resistors | R1=R2=2063 kohm, R3=393 kohm, R4=884 kohm | R12=2.063 Mohm, R3=R12/11.1612, R4=0.412*R12 |
| Output load | implicit/test environment | explicit Cload |

The changed N and resistor ratios follow the requested first-order zero-TC
starting point.  They are not values copied from Figure 5 and still require
temperature simulation and process-specific optimization.

## Automated evidence

The connectivity test compares the three parent ports and all top-level core,
compensation, startup, load, bias, and amplifier-macro instances.  The frozen
child OTA's internal transistors are outside this parent-level review and must
be reviewed through its own topology evidence.

## Human checklist

- [ ] Confirm Figure 2 is the intended functional core.
- [ ] Confirm the op-amp signs: `vip=Vb`, `vin=Va`, `vout=VG`.
- [ ] Confirm Q1/QN use PNP `C B E` order and area ratio 1:8.
- [ ] Confirm the N=8 resistor ratios are intentional rather than Figure 5 values.
- [ ] Confirm replacing PONRST with autonomous RC startup is acceptable.
- [ ] Confirm using the frozen `two_stage_ota` instead of the Figure 5 amplifier is acceptable.
- [ ] Approve the diagram before treating the parent topology as paper-correct.
