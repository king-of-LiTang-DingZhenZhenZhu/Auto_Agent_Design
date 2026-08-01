# Modified StrongARM Latch Optimization Guide

## Scope

`strongarm_latch` implements Figure 1(b) of Behzad Razavi's *The StrongARM
Latch*. It is a leaf-level dynamic comparator, not an opamp. It consumes nearly
zero static power, produces rail-to-rail differential outputs, and is evaluated
with clocked transient simulations.

Port order:

```text
vip vin clk outp outn vdd vss
```

Polarity is defined so that `vip > vin` resolves `outp` high and `outn` low.

## Paper Mapping

- `M1/M2`: matched NMOS input pair connected to internal nodes P/Q.
- `M7`: clocked NMOS tail switch.
- `M3/M4`: cross-coupled NMOS devices from X/Y to P/Q.
- `M5/M6`: cross-coupled PMOS output-restoration pair.
- `S1/S2`: precharge P/Q to VDD.
- `S3/S4`: precharge X/Y to VDD and keep M5/M6 initially off.

When `clk=0`, S1-S4 are on and P/Q/X/Y precharge to VDD. When `clk` rises,
the input pair first integrates the differential input at P/Q, M3/M4 begin
regeneration, and M5/M6 finally restore one output to VDD while the other falls.

## Metrics and Testbenches

Two testbenches apply equal-magnitude positive and negative input differences.
Each testbench includes multiple clock cycles so energy includes evaluation and
the following precharge.

- `decision_positive_margin_v` and `decision_negative_margin_v`: signed output
  difference near the end of the first evaluation phase.
- `propagation_delay_positive_s` and `propagation_delay_negative_s`: time from
  the rising clock midpoint until the expected output difference reaches VDD/2.
- `energy_per_decision_j`: absolute VDD-source energy over one complete cycle.
- `power_w`: energy divided by the measured clock period.

Do not apply Gain/GBW/PM or reset-state DC saturation rules to this topology.

## Parent Search Space

- `Winput_n`, `Linput_n`: amplification gm, input capacitance, matching, noise,
  and kickback.
- `Wtail_n`: evaluation current, amplification time, energy, and clock kickback.
- `Wlatch_n`, `Llatch_n`: regeneration strength and internal capacitance.
- `Wlatch_p`, `Llatch_p`: output restoration and switched capacitance.
- `Wpre_p`: reset completeness, precharge current, and dynamic offset.

The direct gm/Id flow is disabled because a reset-state DC operating point does
not represent clocked amplification and regeneration.

## Diagnosis Order

1. Confirm all four internal/output nodes precharge to VDD while clock is low.
2. Confirm both input polarities resolve with the documented output polarity.
3. If margin is low, inspect P/Q differential growth before resizing the latch.
4. If P/Q separate but outputs are slow, inspect M3-M6 regeneration and load.
5. If one polarity is consistently worse, investigate asymmetry and dynamic
   offset rather than globally increasing device widths.
6. Optimize energy only after both polarities and delay pass.
7. Use Monte Carlo for random offset and transient noise; nominal deterministic
   sweeps cannot sign off either metric.
