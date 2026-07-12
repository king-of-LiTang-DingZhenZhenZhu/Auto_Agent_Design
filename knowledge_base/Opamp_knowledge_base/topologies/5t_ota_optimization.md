# 5T OTA Optimization Guide

## Circuit Summary

PMOS input differential pair (`Mdp1/Mdp2`) steers the PMOS tail current (`Mtail`) into an NMOS current mirror load (`Mcm1/Mcm2`). It is a single-stage OTA, so gain, GBW, output swing, and power are tightly coupled.

## Tunable Parameters

- `Wtail/Ltail`: tail current-source strength and output resistance.
- `Wdp/Ldp`: input pair transconductance, input headroom, and noise.
- `Wcm/Lcm`: NMOS mirror load current density and output resistance.
- `VBIAS`: physical mode only; gm/Id mode derives it from tail lookup.

## Metric-Guided Rules

- Gain low: increase `Ldp` and `Lcm` first; increase `Wdp` only if GBW/noise also needs help.
- GBW low: increase `Wdp` or tail current; avoid only reducing load capacitance assumptions.
- Power high: reduce tail current or `Wtail`, then re-check GBW and slew rate.
- PM/settling issues are usually load/testbench dominated because this is single-stage.

## DC OP Rules

- `Mtail` linear: tail headroom or `VBIAS` is wrong; in physical mode adjust `VBIAS`, in gm/Id mode inspect derived bias.
- `Mdp1/Mdp2` near linear: input common-mode or tail headroom is too tight.
- `Mcm1/Mcm2` linear: output common-mode/swing or mirror sizing is not viable.

## Avoid

- Do not add extra stages inside this topology; switch to `two_stage_ota` if gain target is too high.
- Do not manually edit device connections in rendered netlists; update topology code instead.
