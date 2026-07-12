# Bandgap/PTAT Hierarchical Optimization Guide

## Scope

`bandgap_ptat` is a system-level topology. It should use a two-stage flow:

1. Derive folded-cascode opamp requirements from the bandgap/PTAT target.
2. Optimize and verify that folded-cascode opamp first.
3. Freeze the opamp as a macro/subckt inside `bandgap_ptat`.
4. Run bandgap-level BO on resistor ratios, PTAT/CTAT biasing, pass device size, compensation, and load parameters.

Do not expand folded-cascode W/L parameters into the bandgap BO search space unless the user explicitly requests joint optimization.

## Child Opamp Interface

The internal error amplifier uses the folded-cascode port order:

```text
vip vin vout ibias vdd vss
```

The bandgap topology instantiates it as:

```text
Xopamp (nsense nfb vctrl opibias vdd vss) folded_cascode
```

## First-Pass Opamp Targets

Use conservative derived targets unless the user specifies tighter values:

- Gain: 70 dB or higher.
- GBW/UGF: at least 10 MHz for slow reference loops; increase if startup or line-regulation settling is too slow.
- PM: at least 60 degrees.
- Load cap: use the pass-device gate and compensation estimate.
- Power: start from roughly half of the system budget if the user gave one.

## Bandgap-Level BO Parameters

Optimize only system parameters in the first version:

- `Rptat`, `Rctat`, `Rtop`, `Rbot`
- `Ibias`, `Iopbias`
- `BJT_AREA_RATIO`
- `Wpass`, `Lpass`
- `Ccomp`, `Cload`

Folded-cascode internal parameters such as `Wdiffp`, `Lbias`, `m_half_unit`, `bias_p_scale`, `Wcs`, `Cc`, and `Rz` belong to the child opamp optimization stage, not the bandgap stage.

## Failure Feedback

If bandgap nominal or PVT fails:

- Vref error/tempco dominated by PTAT/CTAT balance: adjust resistor ratio and BJT area ratio.
- Startup too slow: increase startup/bias current or reduce excessive compensation.
- Line regulation poor: increase opamp gain/GBW requirement and rerun the child opamp stage.
- PVT corner collapse caused by opamp headroom: inspect child folded-cascode diagnostics and rerun opamp Review/BO before changing bandgap-level parameters.

