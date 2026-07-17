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
Xopamp (nsense nfb vctrl opibias vdd vss) folded_cascode_two_stage
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

Folded-cascode internal parameters such as `Wdiffp`, `Lbias`, `m_half_unit`, `Wcs`, `Cc`, and `Rz` belong to the child opamp optimization stage, not the bandgap stage.

## First-Order Relations

- `Vref ~= VBE + K*DeltaVBE`，其中 `K` 由具体拓扑的电阻/电流比例决定。
- `DeltaVBE=(k*T/q)*ln(N)`，`N` 是 BJT current-density 或面积比例。
- 一阶温漂抵消条件为 `dVBE/dT + K*(k/q)*ln(N) ~= 0`。
- 室温 `Vref` 正确不代表 tempco 正确；必须分析 `Vref(T)`。
- 温漂偏负通常表示 PTAT 权重不足，偏正通常表示 PTAT 权重过大，但修改比例前必须确认电阻 tempco、运放 offset 和 BJT 工作区。
- 明显曲率不能通过无限微调一阶比例解决，应考虑 curvature compensation。
- 运放 offset 会转化为支路电流和 `Vref` 误差；运放 gain/GBW/PM 会影响 line regulation、startup 和 settling。

当前 `bandgap_ptat` 仍使用理想 PTAT/CTAT source scaffold，尚未实现真实 PDK BJT、专用温度扫描 parser 和 line-regulation parser。因此上述关系当前用于架构和 Review 推理，不能视为真实 bandgap 温漂签核。

## Failure Feedback

If bandgap nominal or PVT fails:

- Vref error/tempco dominated by PTAT/CTAT balance: adjust resistor ratio and BJT area ratio.
- Startup too slow: increase startup/bias current or reduce excessive compensation.
- Line regulation poor: increase opamp gain/GBW requirement and rerun the child opamp stage.
- PVT corner collapse caused by opamp headroom: inspect child folded-cascode diagnostics and rerun opamp Review/BO before changing bandgap-level parameters.
