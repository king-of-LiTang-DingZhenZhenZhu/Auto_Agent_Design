# NMCF Three-Stage OTA Optimization Guide

## Circuit Summary

基于 Leung/Mok Fig. 1(h) 的三级 NMCF OTA。第一级为 PMOS 输入跨导级；第二级由 PMOS `Mgm2`、NMOS mirror `Mmirror2a/b` 和 PMOS source load `Msource2` 构成；输出由 NMOS `Mgm3` 与从一级输出前馈的 PMOS `Mgmf2` 组成 push-pull 级。`Cc1` 从 `s1_out` 接到 `vout`，`Cc2` 从 `s2_out` 接到 `vout`，没有 nulling resistor。

## Paper Relations

- 定义 `m = gm_f2/gm_2 > 1`、`kg = gm_2/gm_L`。
- 论文式 (32)-(33) 给出 `Cm1/Cm2` 与 `gm1/gmL`、`gm2/gmL`、`m`、`CL` 的关系；实际优化应先用这些关系判断数量级，再由 AC/PVT 校正。
- 论文实验电路令 `gm_f2 ~= gm_L`，式 (35) 表明该 push-pull 前馈路径可将 GBW 近似提高一倍。
- 式 (34) 要求 `gm_L >= (sqrt(2)-1)*(gm_f2-gm_2)+4*gm_1`，用于约束 LHP zero 与复极点的相对位置。

## Tunable Parameters

- Stage 1: `Wtail1/Ltail1`, `Wdiff1/Ldiff1`, `Wload1/Lload1`.
- Stage 2: `Wgm2/Lgm2`, `Wmirror2/Lmirror2`, `Wsource2/Lsource2`.
- Output: `Wgm3/Lgm3` controls `gm_L`; `Wgmf2/Lgmf2` controls feedforward `gm_f2`.
- Bias: `Wbiasn/Lbiasn`, `Wbiasp/Lbiasp`.
- Compensation: `Cc1/Cc2`.

## Metric-Guided Rules

- Gain low: increase gain-device/load lengths stage by stage; avoid only increasing final stage size.
- GBW low: identify dominant stage; increase earlier-stage gm before changing output stage aggressively.
- PM low or ringing: compare the measured pole/zero pattern with equations (32)-(36); tune `Cc1/Cc2` together and inspect `gmf2/gm2`, rather than applying an `Rz1` rule.
- SR low: inspect the push-pull pair `Mgm3/Mgmf2`, then reduce excessive compensation only with PM margin.
- Power high: reduce stage currents from output stage backward, then verify GBW and settling.

## DC OP Rules

- `Mtail1` or `Mload1a/b` linear: first-stage bias/current mirror issue.
- `Mgm2/Mmirror2a/b/Msource2` linear: intermediate-stage bias, mirror compliance, or compensation loading issue.
- `Mgm3/Mgmf2` linear: push-pull current balance or output common-mode/headroom issue.
- Bias devices linear: check generated `vbiasp`/`ibias` assumptions before changing signal-path devices.

## Avoid

- Do not use NMCF as the first choice for moderate-gain specs.
- Do not reconnect `Cc1` to `s2_out`; Fig. 1(h) requires `Cc1` from `s1_out` to `vout`.
- Do not add `Rz1`; it is not part of the NMCF structure analyzed by equations (31)-(36).
- Do not assume `gmf2=gmL` from width equality alone; verify DC currents and gm in operating-point data.
