# 项目流程与文件流

本文是当前项目的总流程说明，覆盖叶子电路、系统级电路、BO、Review、
PVT、层级工件和 Virtuoso 导出。操作约束分别见 `AGENTS.md` 和
`CLAUDE.md`；具体模块细节见文末链接。

## 1. 分层架构

```text
设计决策层
  system_decomposition.py
  决定系统架构、block graph、预算、child targets 和 child topology

执行编排层
  hierarchical_flow.py / design_flow_graph.py
  前者组织 child-parent 依赖，后者统一执行 Audit-Review gate-PVT-Export

电路优化层
  main.py + optimizer.py + simulator.py
  在固定 topology 内执行 gm/Id sizing、BO、Spectre 和结果解析

硬约束生成层
  topologies/
  程序化生成 DUT 和 testbench，不让 Agent 直接手写最终网表
```

`system_decomposition.py` 与 `hierarchical_flow.py` 不重复：

- 前者回答“系统怎么拆、指标怎么分、选择什么 child”。
- 后者回答“已确定的 child 以什么顺序完成 BO/PVT、冻结和嵌入”。
- `SystemBlockSpec.to_executable_child()` 把设计决策转换为
  `ExecutableChildSpec` 执行合约。

## 2. 总入口判断

```text
用户需求
  |
  +-- 叶子模块，例如 OTA/运放
  |     -> 选择 topology
  |     -> write_project()
  |     -> main.py
  |
  +-- 系统级电路，例如 Bandgap/LDO/ADC
        -> system_decomposition.py
        -> system_design.json
        -> parent project + hierarchy.json
        -> hierarchical_flow.py
```

当前已经接入系统分解层的系统是：

- `bandgap`：Bandgap core + frozen `two_stage_ota`；
- `ldo`：PMOS-pass cap-less LDO + frozen `two_stage_ota`。

ADC 尚未注册系统规则或 parent topology。

## 3. 叶子模块流程

### 3.1 需求与指标

Agent 将用户要求转换为 SI 单位的 `DesignTarget` 和 `MetricGoal`：

- 硬约束：`min`、`max`、`range`、`target`。
- 可行域软目标：`minimize`、`maximize`、`target`。
- BO 使用 feasibility-first：先满足全部硬约束，再比较软目标。
- 例如功耗默认同时是“不得超过上限”和“在可行域内尽量降低”。

### 3.2 选择 topology

Agent 结合：

- topology registry 的能力范围；
- 对应电路知识库；
- PDK profile、电压域和器件类型；
- 复杂度最低优先原则；

选择固定 topology。BO 默认不会在运行中自动升级 topology。

### 3.3 生成项目

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
    original_requirement="two-stage OTA example",
)
```

典型输出：

```text
two_stage_project/
├── two_stage_ota.cir
├── tb_two_stage_ota_ac.scs
├── tb_two_stage_ota_sr.scs
├── tb_two_stage_ota_st.scs
└── requirements.json
```

运放使用 AC，并按需求追加 SR/ST testbench。Bandgap 使用 startup、PSRR、
temperature 和 line-regulation。Cap-less LDO 使用 zero-load STB、DC load
regulation、near-DC PSR 和 10 ns load transient 四套专用 testbench。

### 3.4 运行 BO

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design

python main.py \
  --netlist two_stage_project/two_stage_ota.cir \
  --testbench two_stage_project/tb_two_stage_ota_ac.scs \
              two_stage_project/tb_two_stage_ota_sr.scs \
              two_stage_project/tb_two_stage_ota_st.scs \
  --requirements two_stage_project/requirements.json
```

默认协作中，真实 Spectre 命令由用户在 Cadence 环境执行；Codex 主要运行
单元测试和 `--dry-run`。

## 4. 单次 BO 内部流程

```text
requirements.json + DUT/testbench templates
  -> 构造 DesignTarget / MetricGoal
  -> 读取 topology 参数空间
  -> 可选 gm/Id sizing spec
  -> 初始参数仿真
  -> Optuna 提出 trial
  -> gm/Id/电流/L 映射为物理 W/L/nf/m
  -> 渲染 workspace/run_xxx/
  -> Spectre
  -> PSF 解析
  -> 指标 + DC OP 状态
  -> feasibility-first reward
  -> 下一 trial
  -> 保存最佳结果
```

### 4.1 每轮工作目录

```text
workspace/run_003/
├── circuit.cir
├── tb.scs
├── tb_1.scs
├── sim.log
├── raw/
└── diagnostics/
    ├── dc_operating_points.csv
    ├── ac_response.csv
    └── diagnostics_summary.txt
```

Spectre 在该 run 目录执行 testbench；实际入口是本目录内的 `tb.scs`，
它包含同目录的 `circuit.cir`。

### 4.2 gm/Id 模式

BO 搜索 gm/Id、支路电流、L、整数镜像倍率和少量 pass-through 参数；
`GmidSizer` 再通过 PDK lookup table 得到实际 W/L。最终 Spectre 网表只
包含物理尺寸，不包含 gm/Id 变量。

### 4.3 DC 工作点约束

每轮解析 critical MOS 的 `|vds|-|vdsat|`：

- critical MOS 在线性区会进入 reward 强惩罚；
- critical MOS 仍在线性区时，即使性能指标达标也不会成功早停；
- noncritical/bias MOS 主要记录 warning。

### 4.4 早停

- 没有软目标：硬约束全部达标、仿真收敛且 critical OP 合格时可早停。
- 存在软目标：达到可行域后继续优化到 `max_iter`，再在可行结果中按
  软目标选择。

## 5. BO 输出

```text
outputs/<project>/
├── initial_default/
├── initial_gmid/
├── netlist/circuit.cir
├── simulation/tb_circuit*.scs
├── data/
│   ├── sim.log
│   └── raw/
├── diagnostics/
├── results.json
├── summary_report.txt
├── optimization_log.json
├── optimization_metrics.csv
├── parameter_analysis/
├── agent_review/
├── design_audit/
├── pvt/
└── virtuoso/
```

优先读取：

```text
results.json
  -> optimization_log.json
  -> diagnostics/diagnostics_summary.txt
  -> workspace/run_xxx/sim.log
  -> raw/
```

`results.json` 的关键字段：

- `all_targets_met`
- `target_status`
- `gap`
- `metrics`
- `params`
- `metric_goals`
- `operating_point_status`

## 6. BO 后 Review

`main.py` 不自动运行 Review 或 PVT。

### 6.1 BO 达标：Design Audit

```text
BO nominal 达标
  -> Design Audit
  -> 检查 critical OP、尺寸、倍乘数、支路电流、参数贴边和过度设计
  |
  +-- 无 blocker -> 进入 PVT
  +-- 有 blocker -> audit_repair Agent Review
```

### 6.2 BO 未达标：failure_repair

```text
BO nominal 未达标
  -> 分析主导 gap
  -> 检查 DC OP
  -> 读取 topology/domain 知识
  -> 对照参数影响和一阶关系
  -> modify / restart_bo / change_topology
```

Review 按 domain 分流：

- 运放关注 Gain/GBW/PM/SR/settling/power。
- Bandgap 关注 startup/Vref/tempco/曲率/PSRR/line regulation/power。

Agent 只填写结构化 `patch_plan.json`；Python 校验参数、clamp、渲染和
仿真 candidate。`AGENT_REVIEW.md` 是人类操作说明，不作为 Agent evidence。

## 7. PVT 与导出

推荐门槛：

```text
BO best 或 Review candidate nominal 达标
  -> Design Audit 无 blocker
  -> PVT
  -> Virtuoso 导出
```

PVT 默认矩阵：

```text
tt/ss/ff × VDD(min/typ/max) × temp(-40/27/125)
```

```bash
python pvt_simulation.py \
  --results outputs/<project>/results.json \
  --simulate
```

PVT 通过后：

```bash
python export_to_virtuoso.py \
  --results outputs/<project>/results.json \
  --lib BO_Designs \
  --tech-lib <tech_lib>
```

默认只生成 SKILL 和报告；只有用户显式要求时才使用 `--run-virtuoso`。

`design_flow_graph.py` 可以读取 BO/Review/Audit/PVT 状态并给出下一步，
但不替代 `main.py`，也不会自动填写 Agent patch plan。

## 8. 系统级分解流程

### 8.1 输入

```json
{
  "system_type": "bandgap",
  "original_requirement": "1.2 V bandgap reference",
  "targets": {
    "vref_v": 1.2,
    "tempco_ppm_per_c": 20,
    "psrr_db": 50,
    "line_regulation_v_per_v": 0.001,
    "startup_time_s": 0.000005,
    "power_w": 0.0002
  }
}
```

### 8.2 设计决策

```bash
python system_decomposition.py \
  --requirements bandgap_requirements.json \
  --project bandgap_project
```

生成：

```text
bandgap_project/
├── system_design.json
├── requirements.json
├── hierarchy.json
├── bandgap_ptat.cir
└── tb_bandgap_ptat_*.scs
```

`system_design.json` 记录：

- 系统架构及选择理由；
- block graph 和依赖关系；
- `parent_internal` 与 `hierarchical_child`；
- child topology 候选和当前选择；
- 功耗/误差等预算；
- nominal/PVT targets；
- 指标来源、推导规则、假设和裕量；
- 未明确的顶层需求。

`hierarchy.json` 只保留执行所需的 `ExecutableChildSpec`：

- child topology；
- subckt/端口；
- nominal/PVT targets；
- frozen artifact 策略；
- parent netlist/results 注入参数。

## 9. 层级执行流程

```bash
python hierarchical_flow.py \
  --project bandgap_project \
  --max-iter 50 \
  --simulate
```

执行顺序：

```text
读取 hierarchy.json
  -> child nominal BO
  -> design_flow_graph child qualification
       -> nominal/review candidate 检查
       -> Design Audit
       -> blocker 时停止并要求 Agent Review
       -> Audit 合格后执行 child PVT targets
  -> 校验 PDK/voltage domain/subckt/ports
  -> 冻结 child artifact
  -> 注入 parent
  -> parent BO
  -> design_flow_graph parent qualification
       -> Design Audit / Review gate
       -> parent PVT
```

child artifact：

```text
bandgap_project/child_blocks/<id>/artifact/
├── circuit.cir
├── results.json
├── pdk_profile_used.json
├── artifact.json
└── pvt/
```

`artifact.json` 保存文件哈希、接口、PDK、nominal/Audit/PVT 状态和
qualification target contract。目标、接口、PDK 或文件哈希变化时，
旧 artifact 不会复用；`--force-child` 可强制重新优化。

### Review 后恢复

`design_flow_graph.py` 负责判断是否需要 Review，但 Agent Review 仍是
需要 Agent 填写 patch plan 并验证 candidate 的显式阶段。层级流程遇到
nominal failure 或 Audit blocker 时会停止并返回 `next_action`。

Review candidate 验证完成后使用：

```bash
python hierarchical_flow.py \
  --project bandgap_project \
  --resume-qualification \
  --simulate
```

该模式复用已有 BO/Review 输出，从 Audit/PVT/freeze 继续，不重新运行 BO
覆盖 candidate。若希望放弃 candidate 并重跑 child BO，使用
`--force-child` 且不要使用 `--resume-qualification`。

## 10. Bandgap 当前实例

```text
Bandgap system targets
  -> architecture: opamp_assisted_pnp_bandgap
  |
  +-- core: parent_internal
  +-- bias: parent_internal
  +-- startup: parent_internal
  +-- opamp: hierarchical_child
        -> two_stage_ota
        -> nominal BO
        -> independent PVT targets
        -> frozen artifact
  -> bandgap parent BO
  -> startup/PSRR/temperature/line simulations
  -> bandgap PVT
```

当前 opamp 默认目标是保守的系统规则，并非由完整小信号环路自动提取；
用户可通过 `custom_specs` 覆盖。缺失的顶层指标写入
`unresolved_requirements`，不会静默伪造。

## 11. 排错顺序

### BO 或单轮 Spectre 失败

```text
pdk_profile_used.json
  -> workspace/run_xxx/sim.log
  -> workspace/run_xxx/diagnostics/
  -> workspace/run_xxx/circuit.cir 与 tb*.scs
  -> raw/
```

### BO 未达标

```text
results.json gap
  -> operating_point_status
  -> optimization_log.json 参数与指标
  -> parameter_effects.md
  -> topology/domain 知识库
```

### 层级项目失败

```text
接口/testbench
  -> child target 推导与预算假设
  -> child nominal/PVT 裕量
  -> child topology
  -> parent architecture
```

## 12. 当前自动化边界

- Agent 负责需求理解、架构/拓扑决策、代码和 Review 判断。
- topology Python 代码负责网表结构硬约束。
- BO 只在固定 topology 和参数空间内优化。
- `system_decomposition.py` 当前只有 Bandgap 规则。
- LDO/ADC 的系统规则、预算器和 parent topology 尚未实现。
- 默认不由 Codex 直接运行真实 Spectre、PVT 或 Virtuoso。

## 13. 相关文档

- 系统分解：`Agent_LLM_BO/circuit_agent/SYSTEM_DECOMPOSITION.md`
- 层级优化：`Agent_LLM_BO/circuit_agent/HIERARCHICAL_OPTIMIZATION.md`
- BO 指标策略：`Agent_LLM_BO/circuit_agent/METRIC_GOALS.md`
- gm/Id：`Agent_LLM_BO/circuit_agent/SIZING_MODES.md`
- Review：`Agent_LLM_BO/circuit_agent/AGENT_REVIEW.md`
- 系统架构知识：`knowledge_base/System_knowledge_base/system_architecture_selection_guide.md`
- Bandgap 知识：`knowledge_base/Bandgap_knowledge_base/topologies/bandgap_ptat_optimization.md`
