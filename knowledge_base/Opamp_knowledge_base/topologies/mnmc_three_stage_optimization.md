# MNMC Three-Stage OTA Optimization Guide

## Topology

`mnmc_three_stage` implements Leung/Mok Fig. 1(f). The main signal path is
`-Av1 -> +Av2 -> -Av3`. A separate PMOS-input differential FTS (`-Avf1`)
senses `vip/vin` and injects its single-ended current into `s2_out`, which is
the input of the third stage. The FTS output is **not** connected directly to
`vout`.

- `Cc1`: `s1_out` to `vout`
- `Cc2`: `s2_out` to `vout`
- `Mgm3/Mload3`: conventional NMOS common-source third stage with PMOS load
- `Mtailf1`, `Mgmf1a/b`, `Mloadf1a/b`: feedforward differential stage
- no nulling resistor

## Paper dimension conditions

Under the paper's assumptions `gmL >> gm1, gm2` and negligible interstage
coupling capacitances, equations (24)-(27) give:

```text
Cm2 = 10 (gm2/gmL) CL
Cm1 = 2.25 (gm1/gmL) CL
gmf1 = 4.45 gm2
GBW = gm1/Cm1 = 0.445 gmL/CL
```

`gmf1 = 4.45 gm2` places the multipath LHP zero on the lower nondominant pole.
Treat these equations as initialization/review guidance, then verify the actual
MOS operating points and AC response; they are not exact W/L ratios.

## Optimization priorities

1. Check every critical MOS is saturated and neither `s1_out` nor `s2_out` is
   pinned near a rail.
2. Establish `gmL` comfortably above `gm1` and `gm2` using `Wgm3`, `I_s3`, and
   the third-stage gm/Id.
3. Tune `Cc2` with the measured `gm2/gmL`; MNMC generally needs a larger `Cc2`
   than ordinary NMC and can become slew-rate limited.
4. Tune `I_f1`, `Wgmf1`, and the FTS gm/Id so `gmf1/gm2` approaches 4.45, then
   inspect whether the pole-zero doublet is acceptably placed.
5. Tune `Cc1` for the target GBW/PM and recheck SR and settling time.

## Failure clues

- Low PM or peaking: verify `gmL >> gm2`, then retune `Cc2` and `gmf1/gm2`.
- Slow SR: `Cc2` may be too large; increase the available stage-2/FTS current
  before reducing compensation blindly.
- Poor pole-zero cancellation: inspect the realized `gmf1/gm2`, not only W/L.
- Excess power: reduce `I_f1` only while preserving the required feedforward
  transconductance ratio.

