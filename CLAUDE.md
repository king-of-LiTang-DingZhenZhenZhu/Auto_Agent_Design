# Circuit Design Agent - AI 操作手册

## 角色分工

| 角色 | 职责 |
|------|------|
| **你 (Claude Code)** | 理解顶层需求 → 选择系统架构 → 分解子模块并派生指标预算 → 为叶子模块选择拓扑 → 生成项目/调用优化 → 读取结果 → 基于知识与数据做层级回传和 BO 后 Review |
| **Python 拓扑库** (`topologies/`) | 硬约束生成 Spectre native syntax 的 `.cir` / `.scs` 网表文件，保证语法正确 |
| **Python 脚本** (`main.py`) | 执行 Spectre 仿真、解析结果、运行 BO 优化循环 |
| **Python 脚本** (`review_optimization.py`) | BO 完成后选取 Top 迭代，汇总指标、OP、参数影响和理论诊断，生成受约束候选并仿真验证 |
| **Python 脚本** (`parameter_effects.py`) | 从 BO 历史推断搜索参数/物理参数对各指标的经验影响、边界压力和收敛区间 |
| **Python 脚本** (`knowledge_review.py`) | 用结构化电路公式计算 top run 的一阶理论需求和派生诊断 |

**你不会直接手写 SPICE 网表或把参数硬改进网表** — 网表由拓扑库生成，仿真/优化交给 `main.py`。

---

## 完整工作流程

> **环境要求**：生成网表或运行本项目之前，必须先执行 `conda activate Auto_Agent_Design`。

1. **解析顶层需求** — 除叶子模块的增益、GBW、PM、功耗、负载、SR/ST 外，系统设计还要提取功能、精度/分辨率、采样率/带宽、噪声、动态范围、输入输出范围、延迟、时钟、电源域和 PVT 约束。
2. **选择系统架构** — 系统级需求先比较候选架构并建立 block graph；例如 ADC 先选 SAR/pipeline/sigma-delta，不能直接跳到“选一个运放”。
3. **派生 child targets** — 将系统误差、噪声、速度、功耗和摆幅预算分配到各 child，声明接口、负载、电源域、推导公式、设计裕量及 PVT 门槛。
4. **选择 child topology** — 对每个叶子 child 再查 topology registry、专用知识库和 PDK 能力，选择复杂度最低且满足局部指标的晶体管级拓扑。
5. **生成项目** — 叶子模块调用 `topo.write_project()`；层级 parent 还应生成 `hierarchy.json` 和 child block 声明。
6. **分阶段优化** — 叶子模块运行 `main.py`；层级系统运行 `hierarchical_flow.py`，执行 child BO/PVT → frozen artifact → parent BO/PVT。
7. **读取与 Review** — 检查 `results.json`、gap 和 DC OP；未达标时区分系统架构、预算、接口、child topology 和尺寸参数问题。
8. **系统回传** — parent 未达标时重新分配受影响 child 的指标预算，只重跑必要模块；v1 不把全部 child W/L 展开做 joint BO。
9. **签核与导出** — nominal、Design Audit 和 PVT 通过后，运行 `export_to_virtuoso.py` 生成 Virtuoso 导入脚本。

> **文件命名**：根据电路拓扑命名，例如 5T OTA → `5t_ota.cir` + `tb_5t_ota_ac.scs`。所有文件放在同名文件夹下。

---

## 第二步：系统架构、子模块与拓扑决策

决策顺序：

```text
顶层指标
  → 系统架构
  → block graph
  → child 指标预算与接口
  → child topology
  → gm/Id + BO 尺寸优化
```

只有需求本身就是 OTA、比较器等叶子模拟模块时，才直接执行 topology selection。运放子模块可参考：

```
gain ≥ 40 dB → two_stage_ota 或 folded_cascode
gain < 40 dB → 5t_ota
gain ≥ 100 dB → nmcf_three_stage      # 尚未完善
```

系统级例子：

- **SAR ADC**：根据分辨率、采样率、输入带宽、功耗和参考建立要求选择 CDAC/采样网络、比较器、参考驱动、时钟和数字控制。SAR 本身不必然需要运放；只有前端缓冲或参考驱动确有闭环精度/速度需求时才派生对应运放指标。
- **Pipeline ADC**：先进行级数、每级位数和误差预算，再由闭环增益误差、半周期建立时间、kT/C/噪声和输出摆幅推导 residue amplifier 的 DC gain、GBW、PM、SR、噪声、线性度和负载，最后选择 OTA topology。
- **Sigma-delta ADC**：先确定环路阶数、OSR、量化器和 NTF，再从积分器泄漏、噪声与摆幅预算派生 OTA 的 DC gain、UGF、SR、噪声和输出摆幅。
- **Bandgap/PTAT**：先确定 Vref、tempco、line/load regulation、启动时间和功耗预算，再派生 error amplifier、PTAT/CTAT 核心、startup 和 bias 指标；当前 `bandgap_ptat` 已支持冻结 child opamp 后优化 parent。

每个 child target 必须记录来源和假设。例如 pipeline residue amplifier 的建立时间不能直接等于 ADC 总周期，应扣除采样、开关非重叠和比较/数字延迟后再分配；GBW 与 SR 还要根据闭环增益、反馈因子、负载和允许误差推导。

查看可用拓扑及其指标范围：
```bash
cd Agent_LLM_BO/circuit_agent
python -c "from topologies import list_topologies; [print(f'{m.name}: {m.display_name} (gain {m.min_gain_db}-{m.max_gain_db} dB, GBW {m.min_gbw_hz}-{m.max_gbw_hz} Hz)') for m in list_topologies()]"
```

参考文档：
- `./knowledge_base/Opamp_knowledge_base/topology_selection_guide.md`
- `./knowledge_base/PDKs_info/pdk_profiles.md`
- `./knowledge_base/PDKs_info/tsmc28_pdk_constraints.md`

PDK 路径、MOS model、VDD、gm/Id 表、PVT 温度、Virtuoso tech library、拓扑初始参数 preset 统一入口在 `pdk_profiles.py`。不要在 topology 文件里硬编码 PDK 路径或 model 名称；换工艺时新增 `PDKProfile`，必要时在 `topology_presets` 覆盖默认值。换工艺前先验证：`python pdk_profiles.py --validate --require-gmid --require-virtuoso`。

VDD 默认来自 profile，单次覆盖用 `params={"VDD": 1.1}` 或 `--vdd` CLI。层级系统（`bandgap_ptat`）先独立优化子模块 opamp，再冻结 macro 做系统级 BO。当前通用 frozen-child 框架已经存在，但 ADC 架构、ADC 专用指标预算器和 ADC topologies 尚未实现，不要把上述 ADC 决策规则误认为可直接运行的代码能力。

---

## 第三步：生成网表

```bash
cd Agent_LLM_BO/circuit_agent
python -c "
from topologies import get_topology
from models import DesignTarget

topo = get_topology('5t_ota')
targets = DesignTarget(gain_db=40, bandwidth_hz=500e6, phase_margin_deg=60, power_w=0.001)
out = topo.write_project('5t_ota', targets=targets, original_requirement='5T OTA gain>40dB GBW>500MHz')
print(f'Created: {out}')
"
```

`write_project()` 一步生成：`<name>/` 目录 + `<name>.cir` + `tb_<name>_ac.scs` + `tb_<name>_sr.scs` + `tb_<name>_st.scs` + `requirements.json`。

> 所有值使用 SI 基本单位 — Hz 不是 MHz，W 不是 mW，F 不是 pF。

---

## 第四步：运行优化

```bash
cd Agent_LLM_BO/circuit_agent
python main.py \
  --netlist <circuit>/<circuit>.cir \
  --testbench <circuit>/tb_<circuit>_ac.scs \
              <circuit>/tb_<circuit>_sr.scs \
              <circuit>/tb_<circuit>_st.scs \
  --requirements <circuit>/requirements.json
```

AC testbench 必须传入；SR/ST testbench 仅当用户需求包含摆率或建立时间时传入。

**常用可选参数：**

| 参数 | 说明 | 示例 |
|------|------|------|
| `--max-iter 20` | 最大迭代次数（默认50） | 快速验证 |
| `--dry-run` | 跳过 Spectre，启发式模拟 | 无 Spectre 环境 |
| `--verbose` | DEBUG 日志 | 排查问题 |
| `--project <name>` | 指定项目名称 | 覆盖自动命名 |
| `--gain / --gbw / --pm / --power / --load-cap` | 快捷指定指标 | `--gain 40 --gbw 500e6` |
| `--sr / --settling-time` | 快捷指定 SR/ST | `--sr 100e6 --settling-time 20e-9` |

简化调用（不用 requirements.json）：
```bash
python main.py --netlist ... --testbench ... --gain 40 --gbw 500e6 --pm 60 --power 0.001
```

---

## 第五步：读取结果

主要输出：`outputs/<project_name>/results.json`

关键字段：`all_targets_met`（是否全部达标）、`target_status`（逐项达标状态）、`gap`（与目标的差距，正=超额，负=不足）、`metrics`（实际仿真值）、`params`（最优参数）。

其他重要输出：`optimization_metrics.csv`（每轮指标表格）、`optimization_log.json`（完整优化历史）、`diagnostics/diagnostics_summary.txt`（最优 run DC/AC 诊断）。

BO 保存项目时还会自动生成 `parameter_analysis/parameter_effects.json|csv|md`。它分别分析 BO 搜索参数与实际物理尺寸的 Spearman 趋势，并提示边界聚集和收敛率异常；这些结果是经验关联，不是因果证明，也不会自动修改参数空间。

---

## 第六步：BO 后本地 Agent Review

对 BO 结果中 Top 10%（3~10 条）迭代做复盘，生成候选网表并仿真验证。推荐两阶段流程：Python 准备上下文与理论派生诊断 → 本地 Agent 填写 `patch_plan.json` → Python 执行。

`main.py` 不会自动调用 Agent Review 或 PVT；`design_flow_graph.py` 会对达标结果自动生成 `design_audit/` 报告，在 BO 未达标或审计存在 blocker 时写出 `next_action=prepare_agent_review`，不会自动填写 patch plan 或运行候选。

```bash
cd Agent_LLM_BO/circuit_agent

# 1. 准备 Agent 复盘上下文
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology two_stage_ota \
  --prepare-agent-review
```

准备阶段会生成：

```text
outputs/<project>/
├── parameter_analysis/
│   ├── parameter_effects.json
│   ├── parameter_effects.csv
│   └── parameter_effects.md
└── agent_review/
    ├── agent_context.md
    ├── patch_plan.json
    ├── knowledge_analysis.json
    └── knowledge_analysis.md
```

`agent_context.md` 直接给出本轮 Review 路线、任务、Top run 指标/参数/边界、target gaps、证据路径和 patch schema。Agent 按其中的路径读取当前拓扑知识、参数影响、理论诊断和必要的 run diagnostics；`Agent_LLM_BO/circuit_agent/AGENT_REVIEW.md` 只供开发者/操作者理解流程，不作为 Agent evidence。

知识驱动诊断从 `knowledge_base/circuit_design_relations.json` 读取结构化关系，当前包括：

- 单级运放：`GBW≈gm_input/(2πCL)`。
- Miller 运放：`GBW≈gm_input/(2πCc)`，并计算目标 GBW 所需输入级 gm。
- 两极点 PM：`p2/UGF≈tan(PM)`，用于判断非主极点是否过近；必须检查附近零点等假设。
- 一阶 bandgap：`Vref≈VBE+KΔVBE`、`ΔVBE=(kT/q)ln(N)` 和一阶 tempco 抵消关系。

Agent 应对照“理论预测 × BO 参数影响 × Spectre 实测”。三者冲突时，提出局部扰动实验，不要直接把相关性或一阶公式视为因果结论。随后填写 `patch_plan.json`：仅允许 `scale`/`set` 已有参数，数值由 Python clamp 到拓扑搜索空间。然后执行：

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology two_stage_ota \
  --patch-plan outputs/<project>/agent_review/patch_plan.json \
  --simulate
```

或不填 patch plan，直接使用内置保守规则快速验证（加 `--dry-run` 跳过 Spectre）：

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology two_stage_ota \
  --simulate
```

当前边界：

- 尚未自动执行局部扰动实验、自动调整参数空间、warm-start 重启 BO 或自动切换拓扑；这些动作由 Agent 根据 Review 证据决定。
- Review candidate 目前主要按性能指标判断是否达标，尚未像 BO early-stop 一样统一把 critical MOS 工作区作为硬门槛；进入 PVT 前必须检查 candidate diagnostics。
- `bandgap_ptat` 当前仍是理想 PTAT/CTAT source scaffold，缺少真实 PDK BJT、专用温扫/tempco 和 line-regulation parser；Bandgap 公式目前只能用于一阶推理，不能用于温漂签核。

---

## 第七步：达标后 PVT 验证

`all_targets_met=true` 或 Review candidate 达标后，做 27-corner PVT 检查（`tt/ss/ff × VDD × temp`）。

```bash
cd Agent_LLM_BO/circuit_agent

python pvt_simulation.py --results outputs/<project>/results.json --dry-run   # 先检查
python pvt_simulation.py --results outputs/<project>/results.json --simulate  # 真实仿真
```

输出：`outputs/<project>/pvt/pvt_results.csv`、`pvt_report.md`。第一版 PVT 只报告 pass/fail，不自动改电路。

---

## 第八步：PVT 达标后导出 Virtuoso

```bash
cd Agent_LLM_BO/circuit_agent

# 默认只生成 SKILL 脚本
python export_to_virtuoso.py \
  --results outputs/<project>/results.json \
  --lib BO_Designs \
  --tech-lib tsmcN28

# 需要自动启动 Virtuoso 批量导入时
python export_to_virtuoso.py \
  --results outputs/<project>/results.json \
  --lib BO_Designs --tech-lib tsmcN28 \
  --include-cds-lib /home/userone/cds.lib \
  --pdk-lib-path /PDKS/TSMC28nm/tsmcN28 \
  --run-virtuoso
```

导出选择：Review candidate 达标优先导出 candidate，否则导出 BO 最优。输出在 `outputs/<project>/virtuoso/`。`--run-virtuoso` 模式额外在 `virtuoso_runs/<project>/` 生成 `cds.lib` + `run_import.il`。

> 注意：导出脚本通过 SKILL 画 schematic，不做 layout。

---

## 异常处理

### 仿真失败
1. 读取 `workspace/run_XXX/sim.log`
2. 收敛问题 → 调整偏置；模型未找到 → 检查 PDK 路径

### 优化结束仍未达标
1. 检查 `summary_report.txt` 的 gap 分析
2. 考虑：扩大参数搜索范围 / 建议放宽指标 / **换拓扑**（如 5t_ota → two_stage_ota）
3. Review 时先区分：局部参数可修复、搜索空间边界不合理、或当前拓扑能力不足；不要无限微调同一候选

---

## 架构说明

```
Agent (Claude Code)                      Python 脚本
───────────────────                      ────────────
• 解析顶层需求与应用场景                    • main.py: 单个给定拓扑的 BO 优化
• 选择系统架构并建立 block graph             • topologies/: 硬约束网表生成
• 派生 child targets、接口和预算              • hierarchical_flow.py: child→parent 编排
• 为每个叶子 child 选择 topology              • simulator.py: Spectre 仿真
• 调 topologies/ 生成 parent/child 项目        • review_optimization.py: BO 后 Review
• 分析 parent gap 并回传受影响 child           • pvt_simulation.py: PVT 验证
• 用理论/数据判断改尺寸、改预算或改架构          • parameter_effects.py: BO 经验趋势
• nominal/PVT 通过后组织交付                  • knowledge_review.py: 理论派生诊断
                                          • export_to_virtuoso.py: Virtuoso 导出
```

## 参考资源

- **拓扑库**：[topologies/](Agent_LLM_BO/circuit_agent/topologies/) — `base.py`, `five_t_ota.py`, `__init__.py`
- **运放子模块拓扑选择**：`topologies/__init__.py:get_topology_for_targets()`
- **层级优化**：[HIERARCHICAL_OPTIMIZATION.md](Agent_LLM_BO/circuit_agent/HIERARCHICAL_OPTIMIZATION.md)
- **代码入口**：[main.py](Agent_LLM_BO/circuit_agent/main.py)
- **Review 脚本**：[review_optimization.py](Agent_LLM_BO/circuit_agent/review_optimization.py)
- **Review 工作流**：[AGENT_REVIEW.md](Agent_LLM_BO/circuit_agent/AGENT_REVIEW.md)
- **参数影响分析**：[parameter_effects.py](Agent_LLM_BO/circuit_agent/parameter_effects.py)
- **知识驱动诊断**：[knowledge_review.py](Agent_LLM_BO/circuit_agent/knowledge_review.py)
- **结构化电路关系**：[circuit_design_relations.json](knowledge_base/circuit_design_relations.json)
- **Virtuoso 导出**：[export_to_virtuoso.py](Agent_LLM_BO/circuit_agent/export_to_virtuoso.py)
- **文件流说明**：[FILE_FLOW.md](Agent_LLM_BO/circuit_agent/FILE_FLOW.md)
- **Sizing 模式说明**：[SIZING_MODES.md](Agent_LLM_BO/circuit_agent/SIZING_MODES.md)
- **PDK 约束**：[tsmc28_pdk_constraints.md](knowledge_base/PDKs_info/tsmc28_pdk_constraints.md)
- **PDK Profile**：[pdk_profiles.py](Agent_LLM_BO/circuit_agent/pdk_profiles.py)
- **拓扑 Review 知识库**：[topologies/](knowledge_base/Opamp_knowledge_base/topologies/)
- **配置**：[config.py](Agent_LLM_BO/circuit_agent/config.py)
