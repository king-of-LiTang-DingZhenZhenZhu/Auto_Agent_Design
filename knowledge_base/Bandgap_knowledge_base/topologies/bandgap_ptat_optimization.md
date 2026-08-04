# Bandgap/PTAT Hierarchical Optimization Guide

## Scope

`bandgap_ptat` is a system-level topology. It should use a two-stage flow:

1. Derive two-stage OTA requirements from the bandgap/PTAT target.
2. Optimize and verify that two-stage OTA first.
3. Freeze the opamp as a macro/subckt inside `bandgap_ptat`.
4. Run bandgap-level BO on resistor ratios, PTAT/CTAT biasing, pass device size, compensation, and load parameters.

Do not expand child OTA W/L parameters into the bandgap BO search space unless the user explicitly requests joint optimization.

## Child Opamp Interface

The internal error amplifier uses the `two_stage_ota` port order:

```text
vip vin vout ibias vdd vss
```

The bandgap topology instantiates it as:

```text
Xopamp (vinp vinn vg opibias vdd vss) two_stage_ota
```

## First-Pass Opamp Targets

Use conservative derived targets unless the user specifies tighter values:

- Gain: 70 dB or higher.
- GBW/UGF: at least 10 MHz for slow reference loops; increase if startup or line-regulation settling is too slow.
- PM: at least 60 degrees.
- Load cap: use the pass-device gate and compensation estimate.
- Power: start from roughly half of the system budget if the user gave one.

## Bandgap-Level BO Parameters

Normal physical-parameter BO optimizes only:

- `R0_SEG_L`, `R1_SEG_L`
- `Lmirror_p`: 400-800 nm

In gm/Id mode, the PMOS mirror is sized with `gm/Id=12-18 V^-1` and
`Lmirror_p=400-800 nm`; BO derives `Wmirror_p` from the lookup table. Resistor
lengths remain pass-through BO parameters. Startup devices, ratios, resistor
widths, opamp bias, output load, and all child OTA parameters stay fixed.

## Startup Circuit III

The parent uses the autonomous startup strategy from Boni, JSSC 2002,
Fig. 3 circuit III:

- PMOS devices `MX` and `MY` inject current into the `vinn` (X, opamp minus)
  and `vinp` (Y, opamp plus) core branches, respectively.
- `Wstart_x > Wstart_y` enforces `IX > IY`, pushing the loop away from the
  zero-current equilibrium even in the presence of opamp offset.
- A resistor-self-biased PMOS mirror derives the detector currents from the
  raw supply, so no startup current exists at `VDD=0`; `QRS` and the
  `R / 0.9R` divider then generate a rough threshold `VRS`.
- The PMOS-input detector compares `VREF` with `VRS`; its inverter drives
  `SUP` low while `VREF < VRS` and drives `SUP` high after startup, switching
  both injection devices off.
- The frozen opamp is biased from the raw supply, so the optional paper device
  `MB` is intentionally omitted.

The startup transient saves `Xdut.vrs`, `Xdut.sup`, and `Xdut.cmp_out` so a
failed run can distinguish threshold-generation, comparator-polarity, and
injection-shutoff failures. Do not increase MX/MY blindly: excessive startup
current strengthens the secondary feedback loop and may cause oscillation or a
wrong stable operating point.

## First-Order Relations

- `Vref ~= VBE + K*DeltaVBE`，其中 `K` 由具体拓扑的电阻/电流比例决定。
- `DeltaVBE=(k*T/q)*ln(N)`，`N` 是 BJT current-density 或面积比例。
- 一阶温漂抵消条件为 `dVBE/dT + K*(k/q)*ln(N) ~= 0`。
- 室温 `Vref` 正确不代表 tempco 正确；必须分析 `Vref(T)`。
- 温漂偏负通常表示 PTAT 权重不足，偏正通常表示 PTAT 权重过大，但修改比例前必须确认电阻 tempco、运放 offset 和 BJT 工作区。
- 明显曲率不能通过无限微调一阶比例解决，应考虑 curvature compensation。
- 运放 offset 会转化为支路电流和 `Vref` 误差；运放 gain/GBW/PM 会影响 line regulation、startup 和 settling。

## Dedicated Simulations

`bandgap_ptat` generates four dedicated testbenches instead of opamp AC/SR/ST:

- `startup`: VDD ramps from 0 to nominal in 1 us; transient stop time is 10 us.
- `psrr`: inject a 1 V AC small signal at VDD and sweep 1 Hz to 100 MHz.
- `temperature`: sweep temperature from the PDK minimum to maximum at nominal VDD.
- `line`: sweep VDD across the active PDK voltage-domain range at 27 C.

Bandgap targets are stored under `requirements.json -> targets`:

```json
{
  "vref_v": 1.2,
  "vref_tolerance_v": 0.005,
  "tempco_ppm_per_c": 20,
  "vref_temp_nonlinearity_v": 0.001,
  "psrr_db": 50,
  "line_regulation_v_per_v": 0.001,
  "startup_time_s": 0.000005
}
```

Metric definitions used by BO:

- `vref_v`: final startup output at 27 C; temperature sweep also records the point nearest 27 C.
- `tempco_ppm_per_c`: peak-to-peak Vref variation divided by nominal Vref and temperature span.
- `vref_temp_nonlinearity_v`: maximum residual from the best linear fit of Vref versus temperature.
- `psrr_db`: minimum `-20log10(|Vref/VDD|)` across the PSRR sweep; VDD AC magnitude is 1 V.
- `line_regulation_v_per_v`: peak-to-peak Vref change divided by the VDD sweep span.
- `startup_time_s`: time from VDD reaching 1% of final supply until Vref enters and remains within 1% of its final value. Final Vref must also exceed 0.1 V and 10% of VDD to reject the zero-current state.

These metrics participate in target status, gap analysis, BO reward,
`results.json`, optimization history/CSV, summaries, and PVT rows.

## Failure Feedback

If bandgap nominal or PVT fails:

- Vref error/tempco dominated by PTAT/CTAT balance: adjust resistor ratio and BJT area ratio.
- Startup too slow: increase startup/bias current or reduce excessive compensation.
- Line regulation poor: increase opamp gain/GBW requirement and rerun the child opamp stage.
- PVT corner collapse caused by opamp headroom: inspect child two-stage OTA diagnostics and rerun opamp Review/BO before changing bandgap-level parameters.
