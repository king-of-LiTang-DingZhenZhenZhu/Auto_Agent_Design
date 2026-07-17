# Agent Review 工作流

> 本文件面向人类开发者和操作人员，用于说明 Review 的两条路线、文件流和执行命令。它不会作为 Agent Review 的证据文件加载；Agent 所需任务、证据路径和输出约束由每次生成的 `agent_context.md` 直接给出。

## 定位与两条路线

Review 根据 BO 是否满足 nominal targets 分成两条路线。Agent 不直接编辑 rendered netlist，也不替代 Spectre：Agent 负责解释证据和填写结构化 `patch_plan.json`，Python 负责校验、clamp、渲染和仿真。

### 路线 A：`success_audit`

BO 已达标时，不再分析“怎么补指标”，而是审计设计质量：

- critical MOS 的 DC 工作点和饱和裕量是否合理；
- W/L、`nf`、`m`、总有效宽度是否异常；
- branch current、current ratio 和功耗分配是否合理；
- 是否存在参数贴边、过度设计、过大面积/电流；
- 是否有安全的降功耗、降面积或增加鲁棒性的空间。

没有明确改进证据时应输出 `decision=accept`，不要为了“优化”而强行修改已经达标的设计。

### 路线 B：`failure_repair`

BO 未达标时，先诊断再修改：

- 找出主导 target gap 和互相冲突的次要 gap；
- 检查 DC OP，区分工作点错误与纯 sizing 问题；
- 结合 topology 知识、一阶理论和带参数的 BO 经验趋势；
- 输出局部修改候选，或判断应调整参数空间、重启 BO、升级 topology。

`main.py` 不会自动启动本地 Agent。`review_optimization.py --prepare-agent-review` 会根据 `results.json.all_targets_met` 自动选择上述模式并生成 Agent context。

## 达标后的 Design Audit

`design_flow_graph.py` 在 PVT 前调用 `design_audit.py`，输出 `design_audit/design_audit.json` 和 `design_audit.md`。当前检查：

- critical MOS 是否进入线性区或接近饱和边缘；
- 单管 `W>100um`、异常偏小 W/L，以及按 `Weff=W×m` 统计的总有效栅宽；
- 已知 topology 参数是否贴近 BO 搜索边界；
- 功耗接近上限、但增益/GBW/PM/SR/建立时间存在较大裕量时，提示降功耗机会。

`blocker` 会在 PVT 前停止并转入 Agent Review；`warning` 只记录面积、寄生、边界或功耗优化机会，不阻止 PVT。尺寸阈值属于保守启发式检查，不替代 PDK DRC、版图寄生或可靠性规则。

## 数据流

```text
BO history
  ├── 指标与 reward
  ├── Top run 参数/边界
  ├── DC operating point
  ├── 参数影响分析
  └── 电路理论派生诊断
          ↓
     agent_context.md
          ↓
     patch_plan.json
          ↓
  candidate netlist + Spectre
```

## 准备 Review

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology <topology> \
  --prepare-agent-review
```

按 reward 选择前 10% 的 run，最少 3 个、最多 10 个。准备阶段生成：

```text
outputs/<project>/
├── parameter_analysis/
│   └── parameter_effects.{json,csv,md}
└── agent_review/
    ├── agent_context.md
    ├── patch_plan.json
    ├── patch_plan_template.json
    └── knowledge_analysis.{json,md}
```

`agent_context.md` 只保存本轮摘要、Top run 参数和证据文件索引，不重复嵌入完整知识库或分析报告。

## Agent Context 证据

生成的 `agent_context.md` 会直接要求 Agent 按模式读取以下证据：

1. `agent_context.md`：本轮 targets、Top run 结果、参数当前值和边界。
2. 当前 topology 的 `*_optimization.md`：器件角色、公式适用条件、调参方向和拓扑特有限制。
3. `parameter_effects.md`：带参数的 BO 历史所推导出的 Spearman 经验趋势、边界聚集和收敛区间。
4. `knowledge_analysis.md`：对 Top run 计算的一阶理论量和一致性检查。
5. 必要时读取对应 `workspace/run_xxx/diagnostics/`、`sim.log`、完整 `optimization_log.json` 和 raw 数据。
6. 若由达标后审计触发 Review，优先读取 `design_audit/design_audit.md`。

`optimization_metrics.csv` 仅保留为面向人的 BO 指标汇总，不作为 Agent Review 的必读证据，因为它没有对应参数。`AGENT_REVIEW.md` 和 `optimization_review_guide.md` 都不加载给 Agent；模式任务、判断顺序、Patch Plan schema 和安全规则直接写入本轮 `agent_context.md`。

## `circuit_design_relations.json`

该文件是机器可读的电路关系注册表，由 `knowledge_review.py` 加载，不直接整份写入 Agent prompt。每条关系包含：

- 公式；
- 适用 topology/domain；
- 假设；
- 主要 tradeoff。

当前关系包括：

- 单级运放：`GBW≈gm_input/(2πCL)`；
- Miller 运放：`GBW≈gm_input/(2πCc)`；
- 两极点近似：`p2/UGF≈tan(PM)`；
- 一阶 bandgap：`Vref≈VBE+KΔVBE`、`ΔVBE=(kT/q)ln(N)`。

公式是物理先验，不是精确仿真模型。若理论、BO 趋势和 Spectre 实测冲突，优先提出局部扰动实验，不要把任一来源直接视为因果结论。

## 判断顺序

1. 仿真是否收敛。
2. critical MOS 是否 linear/near-edge。
3. 哪个指标缺口主导 reward。
4. 一阶理论需求与实测 OP 是否一致。
5. BO 参数影响是否支持该方向。
6. topology 专用知识是否允许该修改。
7. 决定局部修复、调整参数空间、重启 BO 或升级拓扑。

## Patch Plan

```json
{
  "review_mode": "failure_repair",
  "decision": "modify",
  "summary": "本轮策略",
  "findings": [
    {
      "type": "target_gap",
      "evidence": "PM gap=-8deg",
      "conclusion": "stability is the dominant failure"
    }
  ],
  "candidates": [
    {
      "iteration": 3,
      "reason": "选择该 run 的原因和诊断证据",
      "actions": [
        {
          "param": "Cc",
          "operation": "scale",
          "factor": 1.15,
          "reason": "PM 略低，局部证据支持保守增加补偿"
        }
      ]
    }
  ]
}
```

约束：

- 只允许已有参数；
- action 仅允许 `scale` 或 `set`；
- 通常使用 `0.8~1.3` 的保守修改；
- Python 会忽略未知参数并 clamp 到 topology 参数空间；
- 不修改连接、端口、model、include 或 testbench。

## 执行候选

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology <topology> \
  --patch-plan outputs/<project>/agent_review/patch_plan.json \
  --simulate
```

比较 `candidate_metrics.csv`、candidate `metrics_summary.txt` 和 diagnostics。候选性能达标后仍需检查 critical OP，再进入 PVT。

## 当前边界

- 不自动执行局部扰动实验；
- 不自动调整参数空间或 warm-start 重启 BO；
- 不自动切换 topology；
- Review candidate 的 critical OP 尚未纳入统一硬门槛；
- Bandgap 目前缺少真实 PDK BJT、tempco 和 line-regulation parser，相关公式只用于一阶推理。
