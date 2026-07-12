# BO Optimization Review Guide

本文件是 BO 完成后的通用复盘协议。它只规定输入、输出、安全约束和判断顺序；具体电路结构、参数含义、DC OP 调整方向必须优先参考：

```text
knowledge_base/Opamp_knowledge_base/topologies/<topology>_optimization.md
```

## Review 目标

- 从 BO 历史中选择 reward 靠前的 run，生成少量保守候选。
- 候选只作为下一轮 Spectre 验证对象，不直接视为最终设计。
- 本地 Claude/Codex 可以填写 `patch_plan.json`；Python 只负责校验、clamp、渲染和仿真。

## 输入文件

- `outputs/<project>/optimization_metrics.csv`：每轮主要指标和 OP 统计。
- `outputs/<project>/optimization_log.json` 或 `workspace/history.json`：每轮参数、reward、原始指标。
- `workspace/run_xxx/circuit.cir`：Top run 已渲染 netlist。
- `workspace/run_xxx/diagnostics/diagnostics_summary.txt`：DC/AC 人类可读诊断。
- `workspace/run_xxx/diagnostics/dc_operating_points.csv`：用于判断 `|vds|-|vdsat|`。
- topology 专用 guide：当前拓扑的调参知识来源。

Top 样本选择规则：按 reward 降序选前 10%，至少 3 条，最多 10 条；总数少于 3 条时全部使用。

## Patch Plan 格式

Agent 不直接改 `.cir`。先由 Python 导出 `agent_context.md` 和 `patch_plan.json` 模板；本地 Agent 填写结构化 action；随后 Python 校验参数名、clamp 到 topology 参数空间，并重新渲染候选 netlist。

```json
{
  "summary": "本轮候选主要处理 DC OP margin 和 PM 缺口。",
  "candidates": [
    {
      "iteration": 3,
      "reason": "该 run reward 靠前，但 critical MOS near-edge。",
      "actions": [
        {
          "param": "Cc",
          "operation": "scale",
          "factor": 1.15,
          "reason": "PM 略低，保守增加补偿"
        }
      ]
    }
  ]
}
```

允许的 action：

- `operation="scale"` + `factor`
- `operation="set"` + `value`

## 判断顺序

1. 先看仿真是否收敛；未收敛时优先查 `sim.log` 和 testbench。
2. 再看 DC OP：critical MOS 若 `|vds|-|vdsat| < 0` 为 linear，`0~50mV` 为 near-edge。
3. 再看指标缺口：gain、GBW/UGF、PM、power、SR、settling。
4. 查 topology 专用 guide，选择与当前拓扑匹配的参数。
5. 每个 Top run 只生成一个综合候选，避免一次 review 产生过多组合。

## 安全约束

- 只修改候选 run 中已有的 `parameters`。
- 不新增参数，不修改器件连接、端口、模型、include、testbench。
- 优先使用保守倍率，通常 `0.8~1.3`。
- 如果按目标/实测缺口使用更大倍率，必须在 `reason` 中写明计算依据。
- 所有修改由 Python clamp 到 topology `get_param_space()` 的 low/high。
- 候选是否更好必须以重新仿真结果为准。

## 输出检查

Review 完成后查看：

- `outputs/<project>/agent_review/review_report.md`
- `outputs/<project>/agent_review/candidate_metrics.csv`
- `outputs/<project>/agent_review/candidates/*/metrics_summary.txt`
- candidate 的 `diagnostics/diagnostics_summary.txt`，确认 DC OP margin 是否改善。
