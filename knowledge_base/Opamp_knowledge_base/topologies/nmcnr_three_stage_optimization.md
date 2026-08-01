# NMCNR Three-Stage OTA Optimization Guide

## Topology

`nmcnr_three_stage` implements Leung/Mok Fig. 1(e). The main signal path is
`-Av1 -> +Av2 -> -Av3`, with a conventional NMOS common-source third stage and
PMOS current-source load.

- `Cc1`: `s1_out` to `vout`
- inner compensation: `s2_out -> Cc2 -> n_rm -> Rm -> vout`
- no feedforward transconductance stage

`n_rm` is a real series node. `Cc2` and `Rm` are not parallel, and `Rm` is not
in series with `Cc1`.

## Paper dimension conditions

Define `kg = gm2/gmL`. Equations (20)-(22) use:

```text
Rm = 1/gmL
kg < 1  (therefore gmL > gm2)
Cm1 = 4 (gm1/gmL) CL
Cm2 = [2/(1-kg)] (gm2/gmL) CL
GBW = gm1/Cm1 = 0.25 gmL/CL
```

At `Rm = 1/gmL`, the lower-frequency RHP zero is eliminated and the remaining
LHP zero improves phase margin. The paper notes that `Rm` need not be exact;
values near `1/gmL` move the RHP zero to high frequency.

These are small-signal initialization and review equations, not direct W/L
equalities. Verify them using the simulated operating-point transconductances.

## Optimization priorities

1. Check all critical MOS devices are saturated and `s1_out`, `s2_out`, and
   `vout` have adequate headroom.
2. Make `gmL > gm2`; if `kg` approaches or exceeds one, increasing compensation
   alone cannot repair the predicted RHP poles.
3. Initialize `Rm` from the measured `1/gm(Mgm3)`, then sweep around that value.
4. Set `Cc2` from `kg` and load, then tune `Cc1` for the GBW/PM tradeoff.
5. Recheck SR and settling time after reducing either compensation capacitor.

## Failure clues

- Low PM or peaking: inspect `gm2/gmL` first, then the realized `Rm*gmL`.
- RHP-zero signature: move `Rm` toward `1/gmL`; do not add an FTS.
- Large `Cc2`: `kg` may be too close to one; strengthen the output stage or
  reduce `gm2` before accepting excessive area and slow slew rate.
- Slow SR: verify bias currents and reduce capacitors only with adequate PM.

