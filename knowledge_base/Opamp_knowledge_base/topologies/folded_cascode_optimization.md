# Folded Cascode Optimization Guide

## Circuit Summary

PMOS input pair feeds NMOS folded branches and NMOS/PMOS cascodes, then drives a PMOS common-source second stage. Internal bias generator creates `VB1`, `VB2`, `VB3`, and `VB4`. Main current-source/load devices copy fixed bias unit devices by integer `m` ratios.

## Tunable Parameters

- `gm_id_diff_pair_pmos`, `L_diff_pair_pmos`: gm/Id sizing for `Wdiffp`.
- `gm_id_cs_pmos`: gm/Id sizing for second-stage `Wcs`.
- `m_half_unit`: base current-copy unit; sets tail/fold/cascode/mirror branch scale.
- `m_load_ratio`: second-stage load current ratio; `m_load_unit=m_half_unit*m_load_ratio`.
- `Lbias`: shared bias/reference length; bias widths scale with `Lbias/400n`.
- `bias_p_scale`, `bias_n_scale`: PMOS/NMOS bias unit strength knobs.
- `bias_p_small_scale`, `bias_n_small_scale`: small-device bias strength knobs.
- `Cc/Rz`: Miller compensation.

## Metric-Guided Rules

- Gain low with OP healthy: increase `Lbias` or input/cascode effective output resistance; check that GBW does not collapse.
- GBW low: increase `m_half_unit` or input gm through gm/Id; reduce `Cc` only if PM is safe.
- PM low: increase `Cc` and tune `Rz`; do not fix PM by starving second-stage current.
- SR low: increase `m_load_ratio` or second-stage gm/current; reduce excessive `Cc`.
- Power high: reduce `m_half_unit` or `m_load_ratio`, then re-check OP margins.

## DC OP Rules

Use `|vds|-|vdsat|`; below 0 means linear, below 50mV is near edge.

- `Mtailp`, `Mmirr1/2`, `Mcasp1/2` problem: PMOS bias/headroom issue; try increasing `bias_p_scale` conservatively.
- `Mfold1/2`, `Mcasn1/2`, `Mload` problem: NMOS bias/headroom issue; try increasing `bias_n_scale` conservatively.
- `Mcs` problem: second-stage PMOS VOD/current mismatch; increase `Wcs` or adjust `m_load_ratio`.
- `Mdiff1/2` problem: input-pair headroom/VOD issue; increase `Wdiffp` or change gm/Id.

## Avoid

- Do not reopen every bias generator W/L as independent BO parameters unless fixed bias ratios prove impossible.
- Do not judge a nominally passing design as final if critical cascode/load devices are linear.
- Do not change `nf` as a current multiplier; current replication is by `m`.
