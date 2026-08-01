# Banba Sub-1-V Bandgap Optimization Guide

## Scope

`banba_sub1v_bandgap` implements the current-summing CMOS bandgap proposed by
Banba et al. in *IEEE JSSC*, May 1999. Unlike a conventional 1.25 V bandgap,
the topology adds a CTAT current and a PTAT current, then converts their sum to
an independently scalable output voltage. The reference can therefore remain
below the silicon bandgap voltage.

The implementation uses a frozen NMOS-input `two_stage_ota` child. The feedback
inputs are the diode-voltage nodes Va/Vb, not the approximately 0.515 V output;
their expected temperature range is 0.65-0.75 V with a 0.70 V nominal child
testbench common mode. This also follows the paper's native-NMOS input stage.
Parent BO must not expand child transistor dimensions.

## Paper Mapping

- `P1`, `P2`, `P3` are equal-size PMOS devices, enforcing `I1=I2=I3`.
- `R1dev` and `R2dev` share the single parameter `R12`, enforcing `R1=R2`.
- `Q1` has unit area and `QN` has `DIODE_AREA_RATIO=N`.
- `R3dev` converts `DeltaVf=VT*ln(N)` to PTAT current.
- `R4dev` converts the sum of CTAT and PTAT currents to `Vref`.
- `C1dev` and `C2dev` reproduce the two stabilization capacitors in Fig. 5.
- The paper's external `PONRST` is represented by `CstartDev`, `RstartDev`, and
  `Mstart`, which create an internal power-on pulse and then turn fully off.

First-order equations:

```text
Va = Vb
I1 = I2 = I3
DeltaVf = VT*ln(N)
I2 = Vf1/R12 + DeltaVf/R3
Vref = R4*(Vf1/R12 + DeltaVf/R3)
```

The published test-chip starting values are `R12=2.063 Mohm`, `R3=393 kohm`,
`R4=884 kohm`, and `N=100`. They target approximately 0.515 V. These values are
electrical resistances, not portable resistor geometries.

## Parent Search Space

The first parent-level BO pass changes only:

- `R12`: CTAT-current scale while preserving `R1=R2`;
- `R3`: PTAT-current scale;
- `R4`: output-voltage scale;
- `Lmirror_p`: current-source output resistance and headroom tradeoff.

Keep `DIODE_AREA_RATIO=100` fixed for the first pass. Change it only when the
available PNP model, area, or matching budget makes the published ratio
impractical. Mirror widths, startup devices, compensation, and child-opamp
parameters remain fixed until diagnostics identify them as the limiting factor.

## Diagnosis Order

1. Confirm startup reaches the nonzero branch and `Mstart` is off at steady state.
2. Confirm `P1/P2/P3` remain saturated at the minimum supply and hot corner.
3. Confirm the NMOS-input child opamp supports 0.65-0.75 V common mode across
   temperature; child nominal qualification uses 0.70 V and parent PVT checks
   the actual diode-node movement.
4. Correct room-temperature `Vref` primarily with `R4`.
5. Correct temperature slope with `R3/R12`; smaller `R3` increases PTAT weight.
6. Treat residual curvature separately from first-order slope.
7. Re-run PSRR and line regulation after any mirror-length or child-opamp change.

The topology reuses the standard bandgap `startup`, `psrr`, `temperature`, and
`line` testbenches. PDK resistor mapping, mismatch, diode statistics, and the
paper's native-device threshold assumptions require dedicated signoff.
