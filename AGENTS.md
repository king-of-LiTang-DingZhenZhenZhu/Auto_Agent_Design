# Circuit Design Agent 操作规约

## 角色与边界

- Codex：解析顶层需求、选择系统架构、分解 child 与指标预算、选择/修改 topology、运行测试/dry-run、分析结果并给出真实仿真命令。
- `topologies/`：程序化生成 Spectre DUT/testbench；不要手改 rendered `.cir/.scs`。
- `main.py`：给定 topology 下的 gm/Id、BO、Spectre、解析和结果保存。
- `hierarchical_flow.py`：child BO/PVT → frozen artifact → parent BO/PVT。
- `review_optimization.py`：生成 Review context、校验 patch plan、生成并验证 candidate。
- 默认不运行真实 Spectre/BO/PVT/Virtuoso；用户明确要求且环境可用时除外。

运行项目命令前：

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
```

## 标准流程

1. 识别设计层级并将指标转换为 SI 单位。
2. 系统级需求：选择系统架构 → block graph → child targets/接口/预算；叶子模块可直接选择 topology。
3. 根据知识库、topology registry 和 PDK 约束选择 child topology。
4. 用 `write_project()` 生成网表、testbench、`requirements.json`；层级项目同时生成 `hierarchy.json`。
5. 叶子模块运行 `main.py`；层级项目运行 `hierarchical_flow.py`。
6. 读取 `results.json`，进入 `success_audit` 或 `failure_repair`。
7. nominal 与 Design Audit 合格后运行 PVT；parent gap 必要时回传并重分配 child targets。
8. nominal/PVT 合格后用 `export_to_virtuoso.py` 导出。

`main.py` 不自动运行 Review/PVT。`design_flow_graph.py` 负责状态编排，不替代 BO，也不自动填写 `patch_plan.json`。

## 系统与层级规则

- 固定决策顺序：`顶层指标 → 系统架构 → block graph → child targets/接口 → child topology → sizing/BO`。
- child targets 必须包含来源、裕量、PVT target、负载/摆幅/共模和电源域；不得直接复制顶层指标。
- parent BO 不展开 child W/L；child 与 parent 必须匹配 PDK profile、voltage domain、subckt 和端口。
- parent 失败时依次检查接口/testbench、预算假设、child PVT 裕量、child topology、系统架构。
- 自动拓扑升级默认关闭。
- 当前已接入 `bandgap_ptat`；ADC 架构、预算器和 topologies 尚未实现。
- 具体架构规则读取 `knowledge_base/System_knowledge_base/system_architecture_selection_guide.md`。

```bash
python hierarchical_flow.py --project <top_project>
python hierarchical_flow.py --project <top_project> --simulate
```

## PDK 规则

- PDK 路径、section、model、VDD、gm/Id 表、PVT、Spectre options、Virtuoso tech library 和 topology preset 统一由 `pdk_profiles.py` 管理。
- topology 中不得硬编码 PDK 路径、model、电源默认值或工艺专用初始 W/L。
- 晶体管类型使用 profile 的 `nmos_model/pmos_model` 或 LVT 等对应字段。
- `vdd` 是默认值，`vdd_min/vdd_max` 是允许范围；搜索 VDD 时必须显式加入参数空间。
- 工艺专用初值/范围优先写入 `topology_presets`。
- 分析结果前检查 `outputs/<project>/pdk_profile_used.json`。

```bash
python pdk_profiles.py --validate --require-gmid --require-virtuoso
# 真实 Cadence 机器可追加 --check-files
```

## 生成与优化

```bash
python -c "
from topologies import get_topology
from models import DesignTarget

topo = get_topology('5t_ota')
targets = DesignTarget(gain_db=40, bandwidth_hz=500e6,
                       phase_margin_deg=60, power_w=1e-3)
topo.write_project('5t_ota', targets=targets,
                   original_requirement='5T OTA example')
"
```

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

- 先读 `outputs/<project>/results.json`：`all_targets_met`、`target_status`、`gap`、`metrics`、`params`、`operating_point_status`。
- `agent_context.md` 按路线索引 topology 知识、`parameter_effects.md`、`knowledge_analysis.md`、`optimization_log.json` 和必要 diagnostics。
- `optimization_metrics.csv` 仅供人查看；`sim.log/raw` 仅在收敛或解析异常时读取。
- `AGENT_REVIEW.md` 是人类说明，不作为 Agent evidence。

Review 路线：

- `success_audit`：检查 critical OP、尺寸/倍乘数、支路电流、参数贴边和过度设计；无改进证据时 `decision=accept`。
- `failure_repair`：检查主导 gap、DC OP、topology 知识、理论与参数影响；决定 `modify`、`restart_bo` 或 `change_topology`。

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology <topology> \
  --prepare-agent-review
```

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology <topology> \
  --patch-plan outputs/<project>/agent_review/patch_plan.json \
  --simulate
```

- Agent 只能对已有参数使用 `scale/set`；Python 负责校验和 clamp。
- `decision` 当前不是执行器硬分支；`restart_bo/change_topology` 不会自动执行。
- Design Audit blocker 阻止 PVT；warning 当前只记录。
- `design_flow_graph.py` 只在 BO 未达标或 audit blocker 时提示 Review；成功结果需显式执行 `--prepare-agent-review` 才进入完整 Agent Review。
- Review candidate 进入 PVT 前必须检查 diagnostics。

## PVT 与导出

```bash
python pvt_simulation.py --results outputs/<project>/results.json --simulate
```

```bash
python export_to_virtuoso.py \
  --results outputs/<project>/results.json \
  --lib BO_Designs \
  --tech-lib <tech_lib>
```

- 默认 PVT：`tt/ss/ff × VDD(min/typ/max) × temp(-40/27/125)`。
- PVT 失败先读 `pvt_report.md` 和失败 corner diagnostics。
- 导出器优先选择达标 Review candidate，否则选择 BO best。
- 仅在用户明确要求时使用 `--run-virtuoso`。

## 修改与验证

- 修复根因，保持改动最小；不要覆盖用户已有改动或修复无关问题。
- topology 管结构/参数空间；parser/simulator 管测量；不要在 `main.py` 增加 topology 专用硬编码。
- 修改后先跑局部测试，再运行：

```bash
python -m unittest discover -s tests
```

## 文档入口

- 系统架构：`knowledge_base/System_knowledge_base/system_architecture_selection_guide.md`
- 运放 topology：`knowledge_base/Opamp_knowledge_base/topology_selection_guide.md`
- topology Review：`knowledge_base/Opamp_knowledge_base/topologies/*_optimization.md`
- 结构化关系：`knowledge_base/circuit_design_relations.json`
- PDK：`knowledge_base/PDKs_info/pdk_profiles.md`、`Agent_LLM_BO/circuit_agent/pdk_profiles.py`
- 层级优化：`Agent_LLM_BO/circuit_agent/HIERARCHICAL_OPTIMIZATION.md`
- Review：`Agent_LLM_BO/circuit_agent/AGENT_REVIEW.md`
- gm/Id：`Agent_LLM_BO/circuit_agent/SIZING_MODES.md`
