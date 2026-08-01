# PMOS-Input MZC Two-Stage OTA Optimization Guide

## Circuit Summary

`pmos_input_mzc_two_stage_ota` 保留 `pmos_input_two_stage_ota` 的 PMOS 输入第一级和 NMOS 共源第二级，并增加从差分输入直接驱动 `vout` 的 PMOS-input FTS。FTS 输入顺序相对原第一级互换，以生成反相的高频补偿电流。`Cc` 直接连接 `n_s1` 与 `vout`，不使用 `Rz`。

## First-Order Relations

- Leung/Mok 式 (8) 给出的理想抵消条件为 `gm_fts = gm_1`。
- `fts_ratio` 同比例缩放 FTS 的 PMOS 输入对、NMOS 镜像负载和 PMOS 尾管宽度；默认值 1 对应匹配起点。
- FTS 不增加串联电压增益级，但会增加输入电容、输出寄生和静态电流。

## Review Rules

- PM/settling 异常：围绕 `fts_ratio=1` 做保守局部扰动，并结合 AC zero 特征判断欠抵消或过抵消。
- `Mffdiff*` 或 `Mtailff` linear：检查 PMOS 输入级共模和顶部 headroom。
- `Mffmirr2` linear：检查输出共模、NMOS 镜像 overdrive 和 FTS 电流密度。
- 功耗超标：在 PM 有余量时降低 `fts_ratio`；不要假设 FTS 是无功耗的理想受控源。
- GBW 不足：同时检查 `gm_1/Cc` 与 FTS 增加的输入/输出寄生。

## Avoid

- 不要使用 `Rz` patch；该参数不属于此拓扑。
- 不要在没有 PVT 证据时宣称 `fts_ratio=1` 已实现精确零点抵消。
