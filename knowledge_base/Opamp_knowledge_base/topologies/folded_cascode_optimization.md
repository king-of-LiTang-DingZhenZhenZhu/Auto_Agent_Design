# Single-Stage Folded Cascode Optimization Guide

## Circuit Summary

PMOS input pair feeds NMOS folded branches and NMOS/PMOS cascodes. The folded high-impedance node is exposed directly as `vout`; there is no common-source second stage or Miller compensation. Internal bias generator creates `VB1`, `VB2`, `VB3`, and `VB4`.

## Tunable Parameters

- `gm_id_diff_pair_pmos`, `L_diff_pair_pmos`: gm/Id sizing for `Wdiffp`.
- `m_half_unit`: base current-copy unit; sets tail/fold/cascode/mirror branch scale.
- `Lbias`: shared bias/reference length; bias widths scale with `Lbias/400n`.

## Metric-Guided Rules

- Gain low with OP healthy: increase `Lbias` or input/cascode effective output resistance; check that GBW does not collapse.
- GBW low: increase input gm through gm/Id or `m_half_unit`, then re-check output pole and load.
- PM low: reduce capacitive loading or adjust branch current; this topology has no Miller compensation knob.
- SR low: increase branch current through `m_half_unit` or reduce load capacitance.
- Power high: reduce `m_half_unit`, then re-check OP margins.

## DC OP Rules

Use `|vds|-|vdsat|`; below 0 means linear, below 50mV is near edge.

- `Mdiff1/2` problem: input-pair headroom/VOD issue; increase `Wdiffp` or change gm/Id.

## Avoid

- Do not reopen every bias generator W/L as independent BO parameters unless fixed bias ratios prove impossible.
- Do not judge a nominally passing design as final if critical cascode/load devices are linear.
- Do not change `nf` as a current multiplier; current replication is by `m`.
