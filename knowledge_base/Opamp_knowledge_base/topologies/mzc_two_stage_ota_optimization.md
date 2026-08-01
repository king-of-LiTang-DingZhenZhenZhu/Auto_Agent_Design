# MZC Two-Stage OTA Optimization Guide

## Circuit Summary

`mzc_two_stage_ota` 保留 `two_stage_ota` 的 NMOS 输入第一级和 PMOS 共源第二级，并按 Leung/Mok 图 1(c) 增加从差分输入直接到 `vout` 的 feedforward transconductance stage (FTS)。FTS 的差分输入顺序与第一级相反，因此其高频输出电流抵消 Miller 电容的前馈电流。补偿支路是 `n_s1` 到 `vout` 的直接 `Cc`，没有 `Rz`。

## First-Order Relations

- 论文式 (8) 的 RHP-zero cancellation 条件是 `gm_fts = gm_1`，与第二级 `gm_L` 无关。
- `fts_ratio` 同比例缩放 FTS 输入对、镜像负载和尾管宽度；一阶近似下 `gm_fts/gm_1 ~= fts_ratio`。
- `fts_ratio=1` 是理论起点，不代表 PVT 下必然精确匹配；寄生、有限输出阻抗和器件工作区会改变最佳值。
- FTS 理论上不移动原有两极点，但晶体管级寄生和输出负载会改变实际 pole/zero，因此必须以 AC/PVT 结果确认。

## Review Rules

- PM 低且出现 RHP-zero 特征：先在 `fts_ratio=1` 附近做小范围扰动，不能套用 `Rz` 调零规则。
- `Mffdiff*`、`Mffmirr*` 或 `Mtailff` 不饱和：先修复输入共模、输出共模和 FTS headroom，再解释 `fts_ratio` 对 PM 的影响。
- 功耗高：FTS 增加一条与第一级同量级的尾电流；降低 `fts_ratio` 会同时降低抵消能力，必须检查 PM/settling。
- GBW 低：仍检查 `gm_1/(2*pi*Cc)`，并确认 FTS 输出寄生没有形成新的低频非主极点。

## Avoid

- 不要同时用 `Rz` 调零公式解释此拓扑；这里的补偿电容是直接连接。
- 不要仅凭 nominal PM 将 `fts_ratio` 固定为 1；PVT mismatch 必须验证。
