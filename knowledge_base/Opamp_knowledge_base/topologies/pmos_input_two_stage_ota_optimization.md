# PMOS-Input Two-Stage OTA Optimization Guide

## Circuit Summary

PMOS 差分输入对驱动 NMOS common-source 第二级。`Mbias` 是 PMOS diode，外部 `IBIAS` 从 bias node 拉向 VSS；`Mtail/Mload` 与 `Mbias` 使用同一 PMOS mirror unit size，通过整数 `m` 控制电流比例。Miller compensation 使用 `Cc+Rz`。

## First-Order Relations

- `GBW ~= gm_input/(2*pi*Cc)`，适用于 Miller 主极点补偿成立时。
- `gm_required ~= 2*pi*GBW_target*Cc`。
- `PM ~= 90deg-atan(UGF/p2)`；无邻近零点时 `p2/UGF ~= tan(PM)`。
- 增大 `Cc` 通常提高 PM，但降低 GBW 与 SR。
- 提高输入级或第二级电流可提高 gm/速度，但增加功耗和 headroom 压力。

## Tunable Parameters

- `Wbias/Lbias`：PMOS mirror unit size。
- `m_tail_unit`：输入级尾电流相对 `Mbias` 的整数倍数。
- `ratio_load_tail`：第二级 PMOS load 相对尾电流的比例。
- `Wdiff/Ldiff`：输入级 gm、噪声和输入共模范围。
- `Wmirr/Lmirr`：NMOS mirror load 的输出阻抗和匹配。
- `Wcs`：NMOS common-source 第二级能力。
- `Cc/Rz`：GBW、PM、SR 和 settling tradeoff。

## Review Rules

- GBW 不足：先比较实测输入 `gm` 与 `2*pi*GBW_target*Cc`，再决定增加 gm 还是减小 `Cc`。
- PM 不足：比较推导的 `p2/UGF` 与目标值，检查第二级 gm、负载和 zero，不要只增大 `Cc`。
- 功耗过高：检查 `m_tail_unit`、`ratio_load_tail`，同时确认 GBW/SR 余量。
- `Mtail/Mload` linear：检查 PMOS mirror bias、输出共模和电流比例。
- `Mcs` linear：检查第二级 NMOS overdrive、输出共模和 `Wcs`。
