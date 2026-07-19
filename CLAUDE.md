# Circuit Design Agent - Claude 操作手册

## 角色与边界

- Claude：解析顶层需求、选择系统架构、派生 child targets、选择/修改 topology、运行测试/dry-run、分析 BO/Review/PVT 结果。
- `topologies/`：生成 Spectre DUT/testbench；不要手改 rendered netlist。
- `main.py`：单 topology 的 gm/Id、BO、Spectre 和结果保存。
- `hierarchical_flow.py`：child BO/PVT → frozen artifact → parent BO/PVT。
- `review_optimization.py`：Review context、patch plan 和 candidate 验证。


```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
```

## 工作流程

1. 识别叶子模块或系统级设计，全部指标使用 SI 单位。
2. 系统级设计：选择架构 → block graph → child targets/接口/预算。
3. 按知识库、topology registry 和 PDK 约束选择 child topology。
4. 用 `write_project()` 生成项目；层级项目同时生成 `hierarchy.json`。
5. 叶子模块运行 `main.py`；层级项目运行 `hierarchical_flow.py`。
6. 读取 `results.json`，进入 `success_audit` 或 `failure_repair`。
7. nominal、Design Audit 和 PVT 合格后导出 Virtuoso。（待定，尚未完善）

`main.py` 不自动运行 Review/PVT。`design_flow_graph.py` 不自动填写 `patch_plan.json`。

## 架构与 Topology

- 固定顺序：`顶层指标 → 系统架构 → block graph → child targets → child topology → sizing/BO`。
- child targets 必须包含来源、裕量、PVT、负载/摆幅/共模和电源域。
- parent BO 不展开 child W/L；child/parent 必须匹配 PDK、voltage domain、subckt 和端口。
- 当前已接入 `bandgap_ptat`；ADC 架构、预算器和 topologies 尚未实现。
- 系统规则：`knowledge_base/System_knowledge_base/system_architecture_selection_guide.md`。
- 运放 topology：`knowledge_base/Opamp_knowledge_base/topology_selection_guide.md`。

查看 topology：

```bash
python -c "from topologies import list_topologies; [print(m.name) for m in list_topologies()]"
```

## PDK

- PDK 配置统一由 `pdk_profiles.py` 管理；topology 不得硬编码路径、model、VDD 或工艺初值。
- 工艺专用初值/范围写入 `topology_presets`。
- 分析结果前检查 `outputs/<project>/pdk_profile_used.json`。

```bash
python pdk_profiles.py --validate --require-gmid --require-virtuoso
# 真实 Cadence 机器可追加 --check-files
```

## 生成项目

```bash
python -c "
from models import DesignTarget
from topologies import get_topology

topo = get_topology('5t_ota')
targets = DesignTarget(gain_db=40, bandwidth_hz=500e6,
                       phase_margin_deg=60, power_w=1e-3)
topo.write_project('5t_ota', targets=targets,
                   original_requirement='5T OTA example')
"
```

层级项目：

```bash
python hierarchical_flow.py --project <top_project>
python hierarchical_flow.py --project <top_project> --simulate
```

## 运行 BO

```bash
python main.py \
  --netlist <project>/<circuit>.cir \
  --testbench <project>/tb_<circuit>_ac.scs \
  --requirements <project>/requirements.json
```

- AC testbench 必传；仅在指标包含 SR/ST 时传对应 testbench。
- 常用参数：`--max-iter`、`--dry-run`、`--verbose`、`--project`、`--gain`、`--gbw`、`--pm`、`--power`、`--load-cap`、`--sr`、`--settling-time`。
- BO early-stop 要求指标达标、仿真收敛且 critical MOS 不在线性区。

## 结果与 Review

先读取 `outputs/<project>/results.json` 的 `all_targets_met`、`target_status`、`gap`、`metrics`、`params` 和 `operating_point_status`。

- `success_audit`：检查 critical OP、尺寸/倍乘数、支路电流、参数贴边和过度设计；无证据支持修改时 `decision=accept`。
- `failure_repair`：检查主导 gap、DC OP、topology 知识、理论与参数影响；决定 `modify`、`restart_bo` 或 `change_topology`。

准备 Review：

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology <topology> \
  --prepare-agent-review
```

- `agent_context.md` 提供路线、任务、Top run、边界、gap、证据路径和 schema。
- `optimization_metrics.csv` 仅供人查看；`AGENT_REVIEW.md` 不作为 Agent evidence。
- `sim.log/raw` 仅在收敛、parser 或测量异常时读取。
- `decision` 可为 `accept`、`modify`、`restart_bo`、`change_topology`；当前不是执行器硬分支。
- Agent 只能对已有参数使用 `scale/set`；Python 负责校验和 clamp。

验证 candidate：

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology <topology> \
  --patch-plan outputs/<project>/agent_review/patch_plan.json \
  --simulate
```

- Design Audit blocker 阻止 PVT；warning 当前只记录。
- `design_flow_graph.py` 只在 BO 未达标或 audit blocker 时提示 Review；成功结果需显式准备完整 Review。
- candidate 进入 PVT 前必须检查 diagnostics。

## PVT

门槛：BO 达标且 Design Audit 无 blocker，或 Review candidate 达标且 diagnostics 可接受。

```bash
python pvt_simulation.py --results outputs/<project>/results.json --dry-run
python pvt_simulation.py --results outputs/<project>/results.json --simulate
```

默认 corners：`tt/ss/ff × VDD(min/typ/max) × temp(-40/27/125)`。

## Virtuoso 导出

```bash
python export_to_virtuoso.py \
  --results outputs/<project>/results.json \
  --lib BO_Designs \
  --tech-lib <tech_lib>
```

- 优先导出达标 Review candidate，否则导出 BO best。
- 默认只生成 SKILL/报告；仅在用户明确要求时使用 `--run-virtuoso`。

## 异常与验证

- 仿真失败：依次检查 PDK、`sim.log`、收敛、极端参数、testbench 和 parser。
- BO 未达标：区分局部 sizing、搜索空间、child target、child topology 和系统架构问题。
- 修改代码后先跑局部测试，再运行：

```bash
python -m unittest discover -s tests
```

## 文档入口

- 总规约：`AGENTS.md`
- 系统架构：`knowledge_base/System_knowledge_base/system_architecture_selection_guide.md`
- PDK：`knowledge_base/PDKs_info/pdk_profiles.md`
- 层级优化：`Agent_LLM_BO/circuit_agent/HIERARCHICAL_OPTIMIZATION.md`
- Review：`Agent_LLM_BO/circuit_agent/AGENT_REVIEW.md`
- gm/Id：`Agent_LLM_BO/circuit_agent/SIZING_MODES.md`
