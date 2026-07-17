# Two-Stage OTA Optimization Guide

## Circuit Summary

NMOS input 5T first stage drives a PMOS common-source second stage. Miller compensation uses `Cc` and `Rz`. `Mbias`、`Mtail` 和 `Mload` 共用 NMOS mirror unit size；`m_tail_unit` 与 `ratio_load_tail` 控制尾电流和第二级电流比例。

## First-Order Relations

- `GBW ~= gm_input/(2*pi*Cc)`，适用于 Miller 主极点补偿成立时。
- `gm_required ~= 2*pi*GBW_target*Cc`，用于判断输入级 gm 是否从一阶上足够。
- `PM ~= 90deg-atan(UGF/p2)`；若没有邻近零点，`p2/UGF ~= tan(PM)`。
- 增大 `Cc` 通常降低 GBW/SR、提高 PM；增大输入级 gm 通常提高 GBW，但会增加功耗并可能降低 PM。
- `Rz` 用于移动 Miller zero；PM 不能只通过无限增大 `Cc` 修复。

## Tunable Parameters

- `Wdiff/Ldiff`: first-stage gm, input VOD, and GBW through `gm1/Cc`.
- `Wmirr/Lmirr`: first-stage PMOS mirror load resistance and current matching.
- `Wbias/Lbias`: diode bias、尾管和第二级负载的 current-mirror unit size。
- `m_tail_unit`: 尾电流相对 `Mbias` 的整数倍数。
- `Wcs`: second-stage PMOS gain/current capability；其沟道长度使用 `Lbias`。
- `ratio_load_tail`: `Mload` 相对 `Mtail` 的整数电流比例。
- `Cc/Rz`: phase margin, settling, GBW, and slew-rate tradeoff.

## Metric-Guided Rules

- Gain low: increase `Lmirr`, `Lload`, and sometimes `Wcs`; avoid shrinking `Cc` blindly.
- GBW low: increase input-pair gm (`Wdiff` or `I_tail`) and reduce `Cc` only if PM margin allows.
- PM low: increase `Cc`; tune `Rz` upward conservatively.
- SR low: increase second-stage current capability (`ratio_load_tail` or `Wcs`) or reduce excessive `Cc`.
- Power high: reduce current ratios/current-source widths only if GBW/SR have margin.

## DC OP Rules

- `Mtail` linear: NMOS bias 或输入级 headroom 不足；检查 `Wbias/Lbias`、`m_tail_unit` 和输入共模。
- `Mload` linear: 第二级负载 headroom 或电流比例错误；检查 `ratio_load_tail`、`Wbias/Lbias`。
- `Mcs` linear: output common-mode or second-stage PMOS VOD is wrong; increase `Wcs` or reduce overdrive.
- `Mmirr1` is diode-connected; treat its OP with caution and focus on `Mmirr2` for output resistance.

## Avoid

- Do not let `Cc` grow enough to make PM large but GBW/SR unusable.
- Do not reintroduce global hidden `VBIAS` bounds in `main.py`; topology must own bias ranges.
