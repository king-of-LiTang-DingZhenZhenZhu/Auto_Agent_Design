# Circuit Design Agent - Claude 操作手册

## 角色与边界

- Claude：解析顶层需求、选择系统架构、派生 child targets、选择/修改 topology、运行测试/dry-run、分析 BO/Review/PVT 结果。
- `topologies/`：生成 Spectre DUT/testbench；不要手改 rendered netlist。
- `main.py`：单 topology 的 gm/Id、BO、Spectre 和结果保存。
- `system_decomposition.py`：系统架构、block graph、child 指标/预算与 `system_design.json`。
- `hierarchical_flow.py`：child-parent 依赖、资格调用、frozen artifact 与嵌入。
- `design_flow_graph.py`：单个 BO/Review 结果的 Design Audit、Review gate、PVT 和导出。
- `review_optimization.py`：Review context、patch plan 和 candidate 验证。


```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
```

## 工作流程

1. 识别叶子模块或系统级设计，将指标转换为 SI 单位，并区分硬约束与可选软目标。
2. 系统级设计通过 `system_decomposition.py` 生成架构、block graph、child targets/接口/预算。
3. 按知识库、topology registry 和 PDK 约束选择 child topology。
4. 用 `write_project()` 生成项目；层级项目同时生成 `hierarchy.json`。
5. 叶子模块运行 `main.py`；层级项目运行 `hierarchical_flow.py`。
6. 读取 `results.json`：达标则执行 Design Audit，未达标则进入 `failure_repair`；Audit blocker 进入 `audit_repair`。
7. nominal、Design Audit 和 PVT 合格后导出 Virtuoso。（待定，尚未完善）

`main.py` 不自动运行 Review/PVT。`design_flow_graph.py` 不自动填写 `patch_plan.json`。

## 指标策略

- 每项指标通过 `MetricGoal` 声明硬约束：`min`、`max`、`range` 或 `target`；可附加 `minimize`、`maximize` 或 `target` 软目标。
- BO 采用 feasibility-first：先满足全部硬约束，再在可行解中优化软目标；功耗默认是上限约束并同时最小化。
- 旧版 `DesignTarget` 字段会自动映射为 `MetricGoal`；显式 `metric_goals` 优先。格式见 `Agent_LLM_BO/circuit_agent/METRIC_GOALS.md`。

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
- 如果需要切换工艺库，必须先在仓库根目录的 `PDK_Info_Json/` 中完善对应的工艺信息文件，文件名统一为 `<厂商>_<工艺节点名称>_Information.json`；该文件未完善并通过校验前，不得开始新工艺下的设计、仿真或物理实现。
- 工艺专用初值/范围写入 `topology_presets`。
- 分析结果前检查 `outputs/<project>/pdk_profile_used.json`。

### 切换 PDK 前置准备

更换 PDK 不是只替换 model path。以下项目全部完成前，不得在新工艺下运行 BO、
无源映射、PVT 或版图导出：

1. 在 `PDK_Info_Json/<厂商>_<工艺节点名称>_Information.json` 登记并核对 model
   bundle、section、voltage domain、VDD 范围、gm/Id 表、PVT corners、Spectre
   options、Virtuoso tech library 和 PDK library 路径；路径不得硬编码到 topology。
2. 为实际使用的每个 R/C PCell 配置 `passive_devices` 和 `passive_role_map`，至少包含
   Spectre model、OA library/cell/view、端口顺序、CDF 到 Spectre 的 `parameter_map`、
   合法几何范围/grid、固定参数、串并联上限、容差和 `evaluator_key`。不得复用其他
   PDK 的 PCell 参数、LUT、方阻或 callback key。
3. 适配新 PCell 的 CDF callback evaluator。电容应从 callback 读取派生电容值和
   callback 最终接受的尺寸；电阻优先采用同类 callback。若电阻暂用方阻公式，必须
   重新提取该 PDK/器件的 sheet resistance，不能沿用旧工艺数值。
4. 对每种 R/C 在小值、中值、大值和几何边界选取黄金点：电阻用 Spectre DC `V/I`，
   电容用 AC `imag(Y)/(2*pi*f)`，比较 CDF/公式与 Spectre。记录 corner、温度、偏置、
   频率和误差；所有点满足配置容差后，才能将器件标记为可自动映射。无需制作密集
   LUT，但不得跳过该资格验证。
5. 在真实 Cadence 主机运行 profile 校验和 OA 文件检查。缺少 PCell、callback、模型、
   gm/Id 表或资格证据时，流程必须报 `configure_pdk_passives`/配置错误并停止，禁止
   静默回退到旧 PDK、旧 LUT、简化电容公式或 `analogLib`。
6. 新项目首次运行后检查 `outputs/<project>/pdk_profile_used.json`，确保 BO、R/C
   映射、Spectre、PVT 和 Virtuoso 导出使用同一 profile/voltage domain。映射网表
   必须保存 callback 回写后的参数并重新通过 nominal/PVT；导出后继续做 DRC/LVS，
   关键设计做 post-layout 仿真。

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

- 运放必须传 AC testbench；仅在指标包含 SR/ST 时追加对应 testbench。
- Bandgap 使用 startup、PSRR、temperature/nonlinearity、line-regulation 专用 testbench，不套用运放 AC/SR/ST 链路。
- 常用参数：`--max-iter`、`--dry-run`、`--verbose`、`--project`、`--gain`、`--gbw`、`--pm`、`--power`、`--load-cap`、`--sr`、`--settling-time`。
- 无软目标时，BO early-stop 要求全部硬约束达标、仿真收敛且 critical MOS 不在线性区。
- 存在软目标时，即使已可行也继续到 `max_iter`，并按软目标在可行解中排序。

## 结果与 Review

先读取 `outputs/<project>/results.json` 的 `all_targets_met`、`target_status`、`gap`、`metrics`、`params` 和 `operating_point_status`。

- `audit_repair`：BO 已达标但 Design Audit 有 blocker；针对 blocker 检查 critical OP、尺寸/倍乘数、支路电流和参数边界。
- `failure_repair`：检查主导 gap、DC OP、topology 知识、理论与参数影响；决定 `modify`、`restart_bo` 或 `change_topology`。
- Review 必须使用 topology/domain profile：运放关注 Gain/GBW/PM/SR/settling，Bandgap 关注 startup/Vref/tempco/非线性/PSRR/线性调整率/功耗；禁止共用同一套参数建议。
- Review 直接读取当前 topology 知识库和 `metric_goals`，不把通用说明文档当作电路证据。

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
- `design_flow_graph.py` 只在 BO 未达标或 Audit blocker 时提示 Review；Audit 无 blocker 的成功结果直接进入 PVT。
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
- 完整项目流程：`FILE_FLOW.md`
- 系统架构：`knowledge_base/System_knowledge_base/system_architecture_selection_guide.md`
- Bandgap：`knowledge_base/Bandgap_knowledge_base/topologies/bandgap_ptat_optimization.md`
- PDK：`knowledge_base/PDKs_info/pdk_profiles.md`
- 层级优化：`Agent_LLM_BO/circuit_agent/HIERARCHICAL_OPTIMIZATION.md`
- 系统分解：`Agent_LLM_BO/circuit_agent/SYSTEM_DECOMPOSITION.md`
- 指标策略：`Agent_LLM_BO/circuit_agent/METRIC_GOALS.md`
- Review：`Agent_LLM_BO/circuit_agent/AGENT_REVIEW.md`
- gm/Id：`Agent_LLM_BO/circuit_agent/SIZING_MODES.md`
