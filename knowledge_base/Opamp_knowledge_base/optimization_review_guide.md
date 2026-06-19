# BO Optimization Review Guide

本文件用于 BO 完成后的 Agent 复盘。目标不是替代电路设计判断，而是把常见指标缺口映射到保守的候选 netlist 调整，用于下一轮验证。

## 复盘输入

- `outputs/<project>/optimization_metrics.csv`：每轮主要指标表。
- `outputs/<project>/optimization_log.json` 或 `workspace/history.json`：每轮 reward、参数和原始 SI 指标。
- `workspace/run_xxx/circuit.cir`：Top iteration 已渲染 netlist。
- `workspace/run_xxx/tb*.scs`：原 testbench 集合。

Top 样本选择规则：按 reward 降序选前 10%，至少 3 条，最多 10 条；总数少于 3 条时全部使用。

## 指标缺口到调整动作

### Gain 不足

常见原因是输出阻抗不足或增益级 gm 不够。候选调整：

- 增大现有 `L*` 约 20%，提升 ro，并且保持W/L不变。
- 对两级、折叠、三级结构，适当增大第二级/输出级宽度，例如 `Wcs`、`Wgm2`、`Wgm3` 约 10%。
- 若多轮候选仍无法改善，考虑沿拓扑升级路径更换 topology。

### GBW/UGF 不足

常见原因是输入级 gm 不足、补偿电容过大或负载太重。候选调整：

- 增大输入差分对宽度，例如 `Wdp`、`Wdiff`、`Wdiffp`、`Wdiff1` 约 15%。
- 若存在 `Cc*` 且相位裕度足够，可减小 `Cc*` 约 15%。
- 若目标 GBW 和 CL 推导出的电流下界过高，需要扩大电流空间或换拓扑。

### Phase Margin 不足

常见原因是非主极点过低、补偿不足或第二级太强。候选调整：

- 增大 `Cc*` 约 25%，同时适当增大第一级的电流以保持 GBW（如果 GBW 超过预设目标较多则可以不变）。
- 若有 `Rz*`，增大或重新调整 `Rz*` 约 20%。
- 如果 PM 改善但 GBW 明显降低，下一轮需在 Cc 和输入级 gm 之间折中。

### Power 超标

常见原因是支路电流、镜像 ratio 或输出级尺寸过大。候选调整：

- 减小偏置/负载相关宽度，例如 `Wtail*`、`Wload*` 约 10%。
- 减小第二级/输出级宽度，例如 `Wcs`、`Wgm2`、`Wgm3` 约 10%。
- 不能低于 topology 参数空间下限。

### Slew Rate 不足

常见原因是输出充放电电流不足或补偿/负载电容过大。候选调整：

- 增大第二级/输出级宽度，例如 `Wcs`、`Wgm2`、`Wgm3`、`Wload*` 约 15%。
- 若 `Cc*` 偏大，可减小 `Cc*` 约 10%。

### 0.1% Settling Time 过慢

先判断慢的来源：

- PM 不足时，优先按 PM 规则处理。
- GBW 不足时，优先按 GBW 规则处理。
- SR 不足时，优先按 SR 规则处理。
- 如果三者都不明显，需要查看 transient waveform 和工作点。

## 安全约束

- 第一版只修改已有 `parameters`，不新增参数。
- 所有修改 clamp 到 topology `get_param_space()` 的 low/high。
- 每个 Top run 只生成一个综合候选。
- 候选是否更好必须以重新仿真结果为准。
