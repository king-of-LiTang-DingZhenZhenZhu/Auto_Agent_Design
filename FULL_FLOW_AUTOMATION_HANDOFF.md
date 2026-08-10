# 模拟电路全流程自动化项目交接说明

本文用于把 `main` 分支现有的前端电路设计自动化能力，交接给负责继续实现
“原理图 → 版图 → DRC/LVS → PEX → post-layout 验证”全流程自动化的开发者。

本文描述的是编写时的代码基线，而不是未来能力承诺：

- 分支：`main`
- 基线提交：`ebcbdff`
- 前端单元测试：共运行 195 个，结果通过，其中 1 个跳过
- 当前边界：前端 BO、Review、Design Audit、PVT 和 Virtuoso 原理图导出已接入；
  自动布局布线、DRC、LVS、PEX 和 post-layout 仿真尚未在 `main` 中实现

接手者应先完整复现第 4 节的前端流程，再按第 9～14 节定义的接口开发物理后端。

## 1. 项目目标与设计原则

本项目面向模拟集成电路自动设计。输入是自然语言或结构化电路指标，输出应最终
成为通过前仿真、PVT、版图验证和后仿真的可追溯设计。

当前 `main` 已完成的核心闭环是：

```text
需求
  → 系统架构/模块分解
  → 固定 topology 选择
  → gm/Id 初始 sizing
  → 贝叶斯优化（BO）
  → Spectre 仿真与结果解析
  → Design Audit / Agent Review
  → PVT
  → Virtuoso schematic SKILL 导出
```

目标中的完整闭环应扩展为：

```text
前端合格设计
  → 原理图导入与连通性确认
  → 版图约束生成
  → 器件生成、布局、布线
  → DRC 修复闭环
  → LVS 修复闭环
  → PEX
  → post-layout nominal/PVT
  → 指标偏差分析
  → 物理 ECO 或返回前端重新优化
  → 最终 signoff 工件
```

实现时必须坚持以下原则：

1. topology Python 代码是电路结构的源头，不直接手改生成后的 `.cir/.scs`。
2. BO 在固定 topology 内调整参数，不在优化过程中隐式改变系统架构。
3. 所有阶段通过结构化 JSON 和不可变工件交接，禁止依赖人工记忆或临时目录。
4. 每个阶段必须有明确的输入、输出、通过条件、失败原因和恢复入口。
5. PDK 路径、模型、corner、器件映射和技术库均来自 PDK profile，不散落硬编码。
6. 物理后端不能静默改变电气等效关系；任何器件合并、拆分或参数变化都要可追踪。
7. DRC clean、LVS match 与 post-layout 指标达标是三个独立门槛，不能互相替代。

## 2. 仓库结构与模块职责

```text
Auto_Agent_Design/
├── Agent_LLM_BO/circuit_agent/
│   ├── main.py                    # 单个固定 topology 的 BO 主入口
│   ├── models.py                  # 指标、参数、仿真结果和 gm/Id 数据模型
│   ├── optimizer.py               # Optuna BO、reward、可行域优先排序
│   ├── simulator.py               # Spectre 调用、工作目录和多 testbench 执行
│   ├── psf_results.py             # PSF 结果读取
│   ├── operating_point.py         # MOS 工作区和裕量检查
│   ├── diagnostics_export.py      # DC/AC 诊断文件导出
│   ├── gmid_lookup.py             # gm/Id lookup 与 W/L 映射
│   ├── pdk_integration/           # PDK profile、校验、callback 和器件表征
│   ├── passive_devices/           # R/C 器件映射与网表实现
│   ├── system_decomposition.py    # 系统架构、block graph 和 child 指标预算
│   ├── hierarchical_flow.py       # child 优化、资格检查、冻结和 parent 嵌入
│   ├── design_flow_graph.py       # Audit/Review/PVT/Export 状态编排
│   ├── design_audit.py            # 前端设计审计
│   ├── review_optimization.py     # Review context、patch plan 和 candidate 验证
│   ├── pvt_simulation.py          # 前仿 PVT 矩阵
│   ├── virtuoso_schematic_generation/ # Virtuoso 原理图生成及命令行入口
│   ├── topologies/                # 固定结构的 DUT/testbench 生成器
│   └── tests/                     # 前端单元测试
├── knowledge_base/                # 系统、拓扑、Review 和 PDK 知识
├── gmid_lookup_table/             # gm/Id 查找表
├── FILE_FLOW.md                   # 当前前端文件流
├── AGENTS.md                      # 项目运行约束
└── FULL_FLOW_AUTOMATION_HANDOFF.md# 本交接文档
```

关键边界如下：

| 层 | 负责回答的问题 | 当前主要实现 |
|---|---|---|
| 需求/架构层 | 系统如何拆、child 指标怎么来 | `system_decomposition.py` |
| 层级执行层 | child 以什么顺序优化、何时冻结 | `hierarchical_flow.py` |
| 资格编排层 | 是否可进入 Review/PVT/导出 | `design_flow_graph.py` |
| 电路优化层 | 固定 topology 的参数如何优化 | `main.py`、`optimizer.py` |
| 仿真解析层 | 如何执行 Spectre 并得到统一指标 | `simulator.py`、`psf_results.py` |
| 结构生成层 | DUT/testbench 的合法结构是什么 | `topologies/` |
| 原理图生成层 | 如何把最终网表变成 Virtuoso schematic | `virtuoso_schematic_generation/` |
| 物理实现层 | placement/routing/DRC/LVS/PEX/post-layout | **待实现** |

## 3. 当前已实现能力

### 3.1 topology 注册表

当前 `topologies/__init__.py` 注册了 16 个 topology：

- 运放/OTA：`5t_ota`、`two_stage_ota`、`pmos_input_two_stage_ota`、
  `mzc_two_stage_ota`、`pmos_input_mzc_two_stage_ota`、`folded_cascode`、
  `folded_cascode_two_stage`、`nmcnr_three_stage`、`mnmc_three_stage`、
  `nmcf_three_stage`。
- 比较器：`strongarm_latch`。
- Bandgap：`bandgap_ptat`、`banba_sub1v_bandgap`、
  `leung_mok_sub1v_bandgap`。
- LDO：`capless_ldo`、`dfc_capless_ldo`。

每个 topology 至少负责：

- 固定器件连接关系；
- 默认参数和搜索范围；
- PDK model 选择；
- DUT 子电路生成；
- 对应 domain 的 testbench 生成；
- 可选 gm/Id sizing 规格；
- 可选层级 child 合约。

### 3.2 指标模型

指标由 `DesignTarget` 和 `MetricGoal` 表达。硬约束支持：

- `min`：不低于阈值；
- `max`：不高于阈值；
- `range`：必须落在区间内；
- `target`：围绕目标值和容差判断。

软目标支持 `minimize`、`maximize` 和 `target`。优化器采用
feasibility-first：先比较硬约束可行性，再在可行解中比较软目标。

不同电路域使用不同指标：

- 运放：Gain、GBW、PM、功耗、SR、settling 等；
- Bandgap：VREF、温漂/非线性、startup、PSRR、line regulation、功耗等；
- LDO：输出精度、line/load regulation、环路稳定性、PSR、瞬态、功耗等；
- 比较器：正负输入判决、延迟、功耗等。

### 3.3 gm/Id 与 BO

支持 gm/Id 的 topology 不直接搜索所有原始 W，而是搜索：

- 支路电流；
- 每类晶体管的 gm/Id；
- 沟道长度 L；
- 电流镜整数倍率；
- 补偿电容、电阻等少量直通参数。

`GmidSizer` 根据 PDK lookup table 映射到物理 W/L/nf/m。Spectre 最终看到的
是物理尺寸。critical MOS 若落入线性区，会受到 reward 强惩罚，并阻止成功早停。

### 3.4 系统分解与层级优化

当前代码实际注册了两类系统分解规则：

- Bandgap：`opamp_assisted_pnp_bandgap`，parent 为 `bandgap_ptat`，
  `two_stage_ota` 作为独立优化并冻结的误差放大器。
- LDO：`pmos_pass_capless_ldo`，parent 为 `capless_ldo`，
  `two_stage_ota` 作为独立优化并冻结的误差放大器。

ADC 尚未注册系统分解规则和 parent topology。

层级 child 在进入 parent 前必须通过：

- nominal 或已验证的 Review candidate；
- Design Audit；
- child 自己的 PVT targets；
- PDK profile 与 voltage domain 一致性；
- subckt 名称和端口顺序检查；
- artifact 文件哈希检查。

### 3.5 Review、Audit 和 PVT

`main.py` 完成 BO，但不会自动替 Agent 做 Review，也不会自动执行 PVT。

资格顺序是：

```text
nominal 未达标 → failure_repair Review
nominal 达标   → Design Audit
Audit blocker  → audit_repair Review
Audit 无 blocker → PVT
PVT 通过 → 允许导出
```

默认 PVT 为 27 个 corner：

```text
tt/ss/ff × VDD(min/typ/max) × temp(-40/27/125)
```

### 3.6 Virtuoso 原理图导出

前端会把最终 netlist 解析为 topology-neutral `SchematicIR`，再根据器件映射生成
Virtuoso SKILL。导出源选择优先级是：

1. 满足原始指标的 Review candidate；
2. BO best netlist。

默认只生成 `import_schematic.il` 和报告，不启动 Virtuoso。只有显式传入
`--run-virtuoso` 时才批处理创建 schematic。

当前导出层的本质是“建立原理图视图”，不是版图生成器，也不意味着已通过 LVS。

## 4. 前端环境与复现步骤

### 4.1 获取相同代码基线

```bash
git clone <repository-url> Auto_Agent_Design
cd Auto_Agent_Design
git switch main
git pull --ff-only origin main
git rev-parse HEAD
```

如果要求严格复现本文基线，`git rev-parse HEAD` 应为 `ebcbdff`；如果 main 已继续
更新，则以新的 HEAD 为准，同时记录提交号，不要只记录“main”。

### 4.2 Python 环境

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
pip install -r requirements.txt
```

`.env`、PDK 路径和访问凭据不应提交到 Git。可从 `.env.example` 复制本地配置：

```bash
cp .env.example .env
```

### 4.3 仿真机与 PDK 预检

真实 Spectre、PVT 和 Virtuoso 只能在有 Cadence/PDK 的机器执行。登录方法和敏感
信息只保存在 Git ignored 的 `LOCAL_SIMULATION_ACCESS.md`，不得写入脚本或日志。

登录后先确认环境：

```bash
hostname
pwd
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
python -m pdk_integration.profiles --validate --require-gmid --require-virtuoso --check-files
```

### 4.4 前端测试基线

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
python -m unittest discover -s tests
```

接手后第一次修改前，应先保存测试数量、跳过项和当前失败项。不要在存在未知基线
失败时直接开发全流程编排。

## 5. 前端标准运行流程

### 5.1 叶子电路

先由 topology 生成项目：

```python
from models import DesignTarget
from topologies import get_topology

targets = DesignTarget(
    gain_db=60,
    bandwidth_hz=100e6,
    phase_margin_deg=60,
    power_w=1e-3,
)

get_topology("two_stage_ota").write_project(
    "two_stage_project",
    targets=targets,
    original_requirement="Two-stage OTA example",
)
```

再运行 BO：

```bash
python main.py \
  --netlist two_stage_project/two_stage_ota.cir \
  --testbench two_stage_project/tb_two_stage_ota_ac.scs \
              two_stage_project/tb_two_stage_ota_sr.scs \
              two_stage_project/tb_two_stage_ota_st.scs \
  --requirements two_stage_project/requirements.json \
  --max-iter 50
```

`--dry-run` 只用于检查流程和文件生成，不能作为性能通过证据。

### 5.2 系统级电路

结构化输入示例：

```json
{
  "system_type": "bandgap",
  "original_requirement": "1.2 V bandgap reference",
  "targets": {
    "vref_v": 1.2,
    "vref_tolerance_v": 0.005,
    "tempco_ppm_per_c": 20,
    "psrr_db": 50,
    "line_regulation_v_per_v": 0.001,
    "startup_time_s": 0.000005,
    "power_w": 0.0002
  }
}
```

生成分解结果和 parent project：

```bash
python system_decomposition.py \
  --requirements bandgap_requirements.json \
  --project bandgap_project
```

执行 child → freeze → parent：

```bash
python hierarchical_flow.py \
  --project bandgap_project \
  --max-iter 50 \
  --simulate
```

### 5.3 Review、PVT 和导出

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology <topology> \
  --prepare-agent-review
```

Agent 填写并审核 `patch_plan.json` 后：

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology <topology> \
  --patch-plan outputs/<project>/agent_review/patch_plan.json \
  --simulate
```

PVT：

```bash
python pvt_simulation.py \
  --results outputs/<project>/results.json \
  --simulate
```

导出 Virtuoso schematic：

```bash
python -m virtuoso_schematic_generation \
  --results outputs/<project>/results.json \
  --lib BO_Designs \
  --tech-lib <tech_lib>
```

## 6. 前端输入、输出与可信来源

### 6.1 输入

一个可执行设计至少需要：

- topology 名称；
- `requirements.json` 或等价 CLI 指标；
- DUT `.cir`；
- 一套或多套 `.scs` testbench；
- PDK profile；
- 可选的 `hierarchy.json` 和 `system_design.json`。

### 6.2 BO 输出目录

```text
outputs/<project>/
├── initial_default/
├── initial_gmid/
├── netlist/circuit.cir
├── simulation/tb_circuit*.scs
├── data/sim.log
├── data/raw/
├── diagnostics/
├── results.json
├── pdk_profile_used.json
├── summary_report.txt
├── optimization_log.json
├── optimization_metrics.csv
├── parameter_analysis/
├── agent_review/
├── design_audit/
├── pvt/
└── virtuoso/
```

读取结果时的优先顺序：

```text
results.json
  → pdk_profile_used.json
  → design_audit/design_audit.json
  → pvt/pvt_results.json
  → diagnostics/diagnostics_summary.txt
  → optimization_log.json
  → sim.log/raw（只在解析或收敛异常时深入）
```

`results.json` 中物理后端必须读取并保留的字段至少包括：

- `project_name`、`topology_name`、`original_requirement`；
- `all_targets_met`、`target_status`、`gap`；
- `metrics`、`targets`、`metric_goals`；
- `params`、`operating_point_status`；
- `netlist_file`；
- `pdk_profile`、`pdk_profile_file`；
- `diagnostics`；
- `virtuoso_schematic_generation`（若生成成功）。

### 6.3 层级 child artifact

```text
<parent_project>/child_blocks/<block_id>/artifact/
├── circuit.cir
├── results.json
├── pdk_profile_used.json
├── artifact.json
└── pvt/
    ├── pvt_results.json
    ├── pvt_results.csv
    └── pvt_report.md
```

`artifact.json` 是 child 的资格证明，包含接口、目标合约、PDK、PVT 状态和文件
SHA-256。物理后端不得绕过该合约直接从某个临时 BO run 拿网表。

## 7. 前端交给物理后端的唯一入口

建议新增一个显式的 `physical_handoff.json`，由前端资格通过后生成。物理后端只能
消费这个 manifest，不自行猜测“哪个网表最好”。建议 schema 如下：

```json
{
  "schema_version": 1,
  "design_id": "bandgap_project",
  "frontend_commit": "<git-sha>",
  "topology": "bandgap_ptat",
  "source": "bo_best_or_review_candidate",
  "qualification": {
    "nominal_pass": true,
    "design_audit_pass": true,
    "pvt_pass": true
  },
  "inputs": {
    "results_json": ".../results.json",
    "netlist": ".../circuit.cir",
    "pdk_profile": ".../pdk_profile_used.json",
    "schematic_skill": ".../virtuoso/import_schematic.il"
  },
  "identity": {
    "subckt": "bandgap_ptat",
    "ports": ["vdd", "vss", "vref"],
    "voltage_domain": "core_0p9"
  },
  "checksums": {
    "netlist": "<sha256>",
    "results_json": "<sha256>",
    "pdk_profile": "<sha256>"
  }
}
```

生成 handoff 前必须验证：

1. final source 的选择与 `select_export_netlist()` 一致；
2. nominal、Audit 和 PVT 都通过；
3. netlist、results 和 PDK snapshot 存在；
4. subckt、ports、device models 都可被 Virtuoso device map 解析；
5. manifest 中的路径最好同时提供 artifact 内相对路径，避免换机器后失效；
6. 所有输入写入哈希，后续任一文件变化都使旧物理结果失效。

## 8. 物理后端建议的软件分层

不要把所有 Cadence 命令堆进一个脚本。建议新增独立 package，例如：

```text
Agent_LLM_BO/circuit_agent/physical_flow/
├── models.py               # manifest、stage result、violation、ECO 数据模型
├── handoff.py              # 前端结果选择、冻结、哈希和资格检查
├── schematic.py            # Virtuoso schematic 导入和 connectivity 验证
├── constraints.py          # 匹配、对称、邻近、guard ring、pin 等约束
├── layout.py               # PCell、placement、routing 的编排
├── drc.py                  # DRC deck 调用、解析和分类
├── lvs.py                  # LVS deck 调用、解析和差异归一化
├── pex.py                  # 提取 deck、view/netlist 产出和完整性校验
├── postlayout.py           # nominal/PVT 后仿真和前后仿指标对比
├── eco.py                  # 物理 ECO 与返回前端的决策策略
├── orchestrator.py         # 全流程状态机、恢复和幂等执行
└── adapters/
    ├── virtuoso.py         # Cadence/Virtuoso 命令适配
    └── signoff.py          # PVS/Assura/Calibre 等工具适配
```

原则上：

- `models.py` 不依赖具体 EDA 工具；
- adapter 只负责命令拼装和原始结果采集；
- parser 把工具专用输出转换成统一模型；
- policy 根据统一模型决定下一步；
- orchestrator 只负责状态跳转，不隐藏电路决策。

## 9. 全流程状态机

建议状态定义：

```text
FRONTEND_QUALIFIED
  → HANDOFF_FROZEN
  → SCHEMATIC_IMPORTED
  → CONNECTIVITY_VERIFIED
  → CONSTRAINTS_READY
  → LAYOUT_GENERATED
  → DRC_RUNNING / DRC_CLEAN
  → LVS_RUNNING / LVS_MATCH
  → PEX_RUNNING / PEX_READY
  → POSTLAYOUT_NOMINAL_RUNNING / POSTLAYOUT_NOMINAL_PASS
  → POSTLAYOUT_PVT_RUNNING / POSTLAYOUT_PVT_PASS
  → SIGNOFF_COMPLETE
```

失败状态不应只有一个 `FAILED`，至少区分：

- `TOOL_ERROR`：许可证、命令、环境或 deck 无法运行；
- `PARSE_ERROR`：工具已运行但结果无法可靠解析；
- `DRC_VIOLATIONS`：有可定位的 DRC 违规；
- `LVS_MISMATCH`：网络或器件不匹配；
- `PEX_INVALID`：提取网表缺失、空网表或端口不一致；
- `POSTLAYOUT_SPEC_FAIL`：后仿指标不达标；
- `FRONTEND_REOPT_REQUIRED`：物理 ECO 无法安全恢复指标。

每次执行都写：

- `stage`、`status`、开始/结束时间；
- 输入 artifact 哈希；
- 实际工具命令（不含密码）；
- 工具版本、PDK/deck 版本；
- 原始日志路径和结构化结果路径；
- `next_action`；
- retry 次数和失败分类。

同样输入重复运行时，应复用已验证工件；输入哈希变化时，应从第一个受影响阶段
重新运行，而不是复用旧 signoff 结果。

## 10. 版图生成阶段

### 10.1 输入

- 已冻结的 `physical_handoff.json`；
- 已成功导入的 schematic；
- PDK tech library 和器件 PCell 映射；
- topology/domain 的版图约束；
- die/core/region、pin、供电和层使用约束。

### 10.2 约束模型

不能只传器件坐标。模拟版图至少需要表达：

- differential pair / current mirror 的匹配组；
- common-centroid、interdigitation、dummy、方向和 finger 约束；
- 对称轴与等长/等环境布线；
- 敏感节点、屏蔽、允许层和最大寄生预算；
- 高电流网络的宽度、并联 via 和 EM 约束；
- guard ring、well、substrate contact、隔离和 latch-up 规则；
- 电阻/电容阵列的匹配和 dummy；
- Bandgap 中 BJT 比例、热耦合和热梯度要求；
- block boundary、keepout、pin access 和层级宏接口。

建议约束文件使用稳定的 instance role，而不是依赖 M0/M1 等可能变化的实例编号。
topology generator 应提供 `role → instance(s)` 映射，物理后端再生成具体约束。

### 10.3 输出

```text
physical/layout/
├── layout_manifest.json
├── constraints_resolved.json
├── placement.json
├── routing_summary.json
├── virtuoso_layout.log
└── snapshots/
```

`layout_manifest.json` 至少记录 library/cell/view、输入 schematic 哈希、PDK、器件
数量、网络数量、版图 bbox、生成器版本和关键约束满足状态。

## 11. DRC 与 LVS 闭环

### 11.1 DRC

DRC runner 应输出统一违规对象：

```json
{
  "rule_id": "M2.SPACE.1",
  "severity": "error",
  "layer": "M2",
  "bbox": [0.0, 0.0, 1.2, 0.4],
  "message": "minimum spacing violation",
  "source": "<raw-result-reference>",
  "repair_class": "routing_spacing"
}
```

自动修复只能处理有明确几何语义、可局部回滚的违规，例如 spacing、短路、缺 via
或 enclosure。影响匹配、器件参数、敏感节点寄生或拓扑的修复必须升级为人工/Agent
审查。每轮 DRC 都保存 before/after、违规数量和修改集，设置最大迭代次数。

### 11.2 LVS

LVS 通过条件应是工具明确报告 match，且解析器成功识别：

- top cell 一致；
- ports 一致；
- nets 一致；
- devices/subckts 一致；
- 器件参数在规定容差内一致。

LVS mismatch 至少分类为：

- missing/extra device；
- missing/extra net；
- short/open；
- pin/port mismatch；
- model mismatch；
- W/L/nf/m 或电阻、电容值 mismatch；
- hierarchy/black-box mismatch。

不要用“命令返回码为 0”代替 LVS match。返回码只证明工具进程结束，必须解析
signoff summary。

## 12. PEX 与 post-layout 验证

### 12.1 PEX 产物

PEX 阶段至少输出：

```text
physical/pex/
├── extracted_netlist.scs
├── pex_manifest.json
├── extraction_summary.json
├── coupling_summary.csv
└── raw/
```

`pex_manifest.json` 应包含：

- layout/schematic/LVS result 的输入哈希；
- extraction deck 和版本；
- RC、C-only 或 R-only 模式；
- coupling-cap 处理策略；
- top subckt 和端口；
- extracted netlist 哈希；
- 总 R/C、关键网络寄生和异常检查结果。

进入后仿前必须检查：提取网表非空、top subckt 存在、端口顺序可映射、LVS 已通过、
没有未解析器件或 floating parasitic section。

### 12.2 复用前端 testbench

post-layout 应尽量复用前端同一套 stimulus、measure 和 parser，只替换 DUT：

```text
pre-layout testbench + schematic DUT
post-layout testbench + extracted DUT
```

不要复制一套独立指标解析逻辑。建议扩展 `Simulator` 支持 `design_view`：

- `schematic`：当前 BO best / Review candidate；
- `extracted`：PEX netlist；
- 后续可扩展 `extracted_rc`、`extracted_c`。

### 12.3 后仿门槛

建议先跑 post-layout nominal，通过后再跑 post-layout PVT：

```text
PEX ready
  → post-layout nominal
  → 与原始 MetricGoal 判断
  → 前后仿 delta 分析
  → post-layout PVT
  → signoff
```

`postlayout_results.json` 至少保存：

- 与 `results.json` 相同的统一指标名；
- `all_targets_met`、`target_status`、`gap`；
- schematic 与 extracted 的指标绝对差和百分比；
- 失败 corner；
- PEX manifest/hash；
- 使用的 testbench/hash；
- 仿真日志和 raw data 路径。

## 13. ECO 与回退策略

post-layout 失败不能一律重新跑 BO。建议按影响范围分三类：

| 类型 | 示例 | 处理方式 |
|---|---|---|
| 纯几何 ECO | spacing、via、局部绕线 | 保持 schematic 不变，重跑受影响的 DRC→LVS→PEX→后仿 |
| 约束/版图 ECO | 敏感线过长、耦合过大、匹配布局差 | 修改布局约束并重新布局，之后完整物理验证 |
| 电气 ECO | 后仿 GBW/PM/VREF 等无法靠版图恢复 | 输出寄生预算和 gap，返回前端 Review/BO，再生成新 handoff |

返回前端的 `frontend_feedback.json` 建议包含：

- 失败指标与最坏 corner；
- schematic/post-layout delta；
- 关键网络 R/C 和主要耦合来源；
- 建议的寄生预算；
- 是否允许只修改现有参数；
- 是否需要 topology/architecture 变化；
- 对应物理 artifact 哈希。

前端重新优化后必须产生新的 handoff ID，旧版图结果不能继续标记为有效。

## 14. 建议开发里程碑与验收标准

### M1：前端 handoff 冻结

交付：`physical_handoff.json`、schema 校验、相对路径打包、SHA-256、资格 gate。

验收：

- nominal/Audit/PVT 任一失败时拒绝 handoff；
- Review candidate 与 BO best 的选择规则与现有导出器一致；
- 文件被修改后 checksum 校验失败；
- 同一输入重复生成结果稳定。

### M2：schematic 导入与连通性确认

交付：批处理导入、器件映射、端口/网络/实例统计和明确失败状态。

验收：

- 能在目标 PDK 建立 library/cell/schematic；
- 导入后统计与 `SchematicIR` 一致；
- unsupported model、端口顺序错误和 PDK 缺失能被可靠拒绝。

### M3：最小可用版图生成

建议先选择一个小型 topology（如 `5t_ota`）作为 vertical slice，不要一开始覆盖
全部 16 个 topology。

验收：

- 可重复生成同一版图；
- 关键匹配/对称约束有机器可读证据；
- layout manifest 可追溯到 handoff 和代码提交。

### M4：DRC/LVS 自动执行与解析

验收：

- 工具错误、解析错误、真实 violation/mismatch 可区分；
- DRC 以明确的 zero violations 为通过条件；
- LVS 以明确的 schematic-layout match 为通过条件；
- 原始报告和结构化结果都保留。

### M5：PEX 与 post-layout nominal

验收：

- PEX 输入必须来自 LVS match 的版图；
- post-layout 复用前端 testbench 和 MetricGoal；
- 输出前后仿 delta 与关键寄生摘要；
- 用受控 testcase 验证寄生增大能引起可预期的性能变化。

### M6：post-layout PVT 与恢复

验收：

- 支持中断后从最后一个有效 stage 恢复；
- 输入哈希变化时自动失效下游缓存；
- post-layout nominal/PVT 均通过后才生成 `SIGNOFF_COMPLETE`；
- 失败时能生成物理 ECO 或前端反馈，而不是无限重试。

### M7：扩展 topology

扩展顺序建议：

```text
5t_ota
  → two_stage_ota
  → 其他 OTA
  → bandgap_ptat / Banba / Leung-Mok
  → capless_ldo / DFC LDO
  → strongarm_latch
```

每增加一个 topology，都要补充器件角色映射、版图约束、最小 DRC/LVS/PEX fixture
和 post-layout 指标回归。

## 15. 测试策略

测试分四层：

1. 纯单元测试：schema、状态机、路径、哈希、report parser、ECO policy。
2. 伪工具集成测试：用固定日志/报告 fixture 验证 DRC/LVS/PEX parser。
3. 小型真实 EDA smoke test：单个 topology、单个 corner、固定 PDK。
4. 全流程回归：代表性 OTA + Bandgap/LDO，nominal/PVT/post-layout 全部执行。

所有真实 EDA 测试都应可通过 marker 或环境变量跳过，使没有 Cadence 的开发机仍能
运行纯 Python 测试。禁止把许可证、绝对用户目录或敏感凭据写入 fixture。

每次改动最低验证：

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
python -m unittest discover -s tests
```

物理后端建立后，再增加独立测试入口，例如：

```bash
python -m unittest discover -s tests/physical_flow
```

## 16. PDK 和 EDA 适配要求

当前 `PDKProfile` 已包含 Spectre model、process section、voltage domain、gm/Id table、
器件模型和 Virtuoso technology library 信息，但还不包含完整物理 signoff 配置。

物理后端应扩展或新增 profile，至少表达：

- tech lib、display/tech file；
- MOS/BJT/R/C PCell 和 terminal/parameter map；
- routing layer、via、pin layer 和合法方向；
- DRC/LVS/PEX 工具类型、deck 路径、runset 和版本；
- LVS source/layout view 和 black-box 策略；
- PEX 模式、输出格式和 model include；
- layer-purpose pair、guard ring 和 well contact 规则入口；
- 工具可执行文件和许可证预检。

这些字段必须允许站点本地覆盖，并在每次运行中冻结 snapshot。代码仓库只保存模板
和非敏感默认值，不保存许可证或登录信息。

## 17. 常见风险

- 把 SKILL 导出成功误认为 schematic 电气正确或 LVS 已通过。
- 只看进程返回码，不解析 DRC/LVS/PEX 的真实总结。
- 用绝对路径绑定 artifact，换机器后全部失效。
- 版图中改变 nf/m、并联结构或器件参数，却未回写等效关系。
- 后仿另写一套测量脚本，造成前后仿指标定义漂移。
- PEX 后只跑 typical nominal，没有检查 PVT。
- 自动修复 DRC 时破坏匹配、对称或敏感节点寄生。
- 物理失败后无限循环，不设置最大 ECO 次数和升级策略。
- 复用旧版图/PEX 结果时没有校验前端网表、PDK 和 deck 哈希。
- 为追求“一键运行”隐藏中间状态，导致失败无法诊断和恢复。

## 18. 接手者首周建议任务

1. 在自己的环境复现 `Ran 195 tests`、`OK (skipped=1)` 的前端测试基线。
2. dry-run 生成一个 `5t_ota` 项目，理解 `requirements.json` 和 `results.json`。
3. 在 Cadence 环境完成一次真实 BO 或读取一份已存在的合格 outputs 工件。
4. 用现有导出器创建 Virtuoso schematic，人工核对器件、端口和网络。
5. 实现并测试 `physical_handoff.json`，先不做自动版图。
6. 选定目标 signoff 工具及 DRC/LVS/PEX deck，建立 adapter 和 fixture。
7. 以 `5t_ota` 完成第一条 schematic→layout→DRC→LVS→PEX→post-layout vertical slice。
8. vertical slice 稳定后再扩展层级电路和其他 topology。

## 19. 完成定义

本项目的“全流程自动化完成”至少应满足：

- 输入需求到前端合格设计可追溯；
- 物理后端只接受通过资格 gate 的冻结 handoff；
- schematic、layout、DRC、LVS、PEX、post-layout 各阶段都有机器可读状态；
- DRC zero violations、LVS match、PEX 完整性检查均通过；
- post-layout nominal 和规定 PVT corners 满足同一套 MetricGoal；
- 全部输入、输出、工具/PDK/deck 版本和哈希可审计；
- 流程可中断恢复、可重复执行、不会错误复用失效工件；
- 失败能定位到具体阶段，并能选择物理 ECO 或返回前端；
- 至少一个 OTA 和一个系统级电路完成真实 EDA 的端到端回归。

在达到以上条件前，应使用“前端自动化完成、物理后端开发中”描述项目状态，不能
将“已生成 Virtuoso SKILL”表述为“已完成版图或 signoff”。
