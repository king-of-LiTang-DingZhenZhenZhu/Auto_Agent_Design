# Circuit Design Agent - AI 操作手册

## 角色分工

| 角色 | 职责 |
|------|------|
| **你 (Claude Code)** | 理解需求 → 查阅知识库选择拓扑 → 调 Python 拓扑库生成网表文件 → 给出/调用优化命令 → 读取结果 → BO 后本地 Agent Review |
| **Python 拓扑库** (`topologies/`) | 硬约束生成 Spectre native syntax 的 `.cir` / `.scs` 网表文件，保证语法正确 |
| **Python 脚本** (`main.py`) | 执行 Spectre 仿真、解析结果、运行 BO 优化循环 |
| **Python 脚本** (`review_optimization.py`) | BO 完成后选取 Top 迭代，应用指标缺口规则生成候选网表并仿真验证 |

**你不会直接手写 SPICE 网表或把参数硬改进网表** — 网表由拓扑库生成，仿真/优化交给 `main.py`。BO 后 Review 时，你可以读取结果和知识库，填写结构化 `patch_plan.json`；再由 `review_optimization.py` 校验、clamp 并重新渲染候选网表。

---

## 完整工作流程

> **环境要求**：生成网表或运行本项目之前，必须先执行 `conda activate Auto_Agent_Design`。

1. **解析需求** — 提取 `gain_db`, `bandwidth_hz`(即 GBW/UGF), `phase_margin_deg`, `power_w`, `load_cap_f`, `slew_rate_v_per_s`, `settling_time_s`, `topology_hint`
2. **选择拓扑** — 用户指定则跳过；否则按决策树匹配（见下方），优先选复杂度最低的
3. **生成网表** — 调 `topo.write_project()` 一行生成 `.cir` + testbench + `requirements.json`
4. **运行优化** — `python main.py --netlist ... --testbench ... --requirements ...`
5. **读取结果** — 查看 `outputs/<project>/results.json`，重点关注 `all_targets_met`、`target_status`、`gap`
6. **BO 后 Review** — 若结果未完全达标，运行 `review_optimization.py` 分析 Top 迭代，生成候选网表并仿真验证
7. **PVT 验证** — 若 BO 最优或 Review candidate 已达标，运行 `pvt_simulation.py` 做 27-corner PVT 检查
8. **导出 Virtuoso 原理图** — nominal 和 PVT 都满足后，运行 `export_to_virtuoso.py` 选择最终 netlist 并生成 Virtuoso 导入脚本/工作区

可选：使用 `design_flow_graph.py --project outputs/<project>` 作为上层编排器，让 LangGraph/fallback 自动读取当前状态并输出 `flow/flow_report.md` 与下一步 `next_action`。它只调度现有 BO/Review/PVT/Virtuoso 脚本，不替代底层实现。

> **文件命名**：根据电路拓扑命名，例如 5T OTA → `5t_ota.cir` + `tb_5t_ota_ac.scs`。所有文件放在同名文件夹下。

---

## 第二步：拓扑选择（决策树）

```
gain ≥ 40 dB → two_stage_ota 或 folded_cascode，folded_cascode 
gain < 40 dB → 5t_ota
gain ≥ 100 dB → nmcf_three_stage      # 尚未完善
```

查看可用拓扑及其指标范围：
```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
python -c "from topologies import list_topologies; [print(f'{m.name}: {m.display_name} (gain {m.min_gain_db}-{m.max_gain_db} dB, GBW {m.min_gbw_hz}-{m.max_gbw_hz} Hz)') for m in list_topologies()]"
```

参考文档：
- `./knowledge_base/Opamp_knowledge_base/topology_selection_guide.md`
- `./knowledge_base/PDKs_info/pdk_profiles.md`
- `./knowledge_base/PDKs_info/tsmc28_pdk_constraints.md`

PDK 路径、Spectre section、NMOS/PMOS/LVT model 名称、默认 VDD、VDD 允许范围、gm/Id 表路径、PVT 温度、Spectre options、Virtuoso tech library 的代码入口统一在 `Agent_LLM_BO/circuit_agent/pdk_profiles.py`。不要在 topology 文件里新增硬编码 PDK 路径、MOS model 或电源默认值；换工艺时新增/选择 `PDKProfile`，或用 `.env` / `PDK_PROFILE_FILE` 覆盖对应字段。

换工艺前先做 profile 验证：

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
python pdk_profiles.py --validate --require-gmid --require-virtuoso
```

如果在真实 Cadence/Spectre 机器上，再加 `--check-files` 检查模型和 OA library 路径是否存在。优化输出会保存 `pdk_profile_used.json`，分析结果时优先确认该文件与当前环境一致。

VDD 使用规则：profile 的 `vdd` 是默认值，`vdd_min/vdd_max` 是允许范围；单次设计需要 1.0V 或 1.1V 时，通过 `params={"VDD": 1.1}`、requirements/CLI 或 `.env` 的 `VDD=1.1` 覆盖。若要让 BO 搜索 VDD，必须在 topology `get_param_space()` 或显式 `params.json` 中加入 `VDD`，并限制在 profile 范围内。

晶体管类型规则：常规拓扑使用 profile 的 `nmos_model/pmos_model`；folded cascode 当前使用 `nmos_lvt_model/pmos_lvt_model`。不要在 netlist template 中直接写死 `nch_mac/pch_mac/nch_lvt_mac/pch_lvt_mac`。

---

## 第三步：生成网表

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
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
conda activate Auto_Agent_Design
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

其他重要输出：

```text
outputs/<project_name>/
├── initial_default/              # topology DEFAULT_PARAMS 初始仿真
├── initial_gmid/                 # 默认 gm/Id 推导尺寸后的初始仿真
├── diagnostics/
│   └── diagnostics_summary.txt   # 最优 run 的 DC/AC 人类可读诊断
├── optimization_metrics.csv      # 每轮 gain/GBW/PM/power/SR/ST 表格
└── optimization_log.json         # 完整优化历史
```


---

## 第六步：BO 后本地 Agent Review

对 BO 结果中 Top 10%（3~10条）迭代做复盘，生成候选网表并仿真验证。推荐优先使用两阶段本地 Agent 流程：Python 只准备上下文，本地 Claude/Codex 根据知识库填写 `patch_plan.json`，Python 再安全执行。

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design

# 1. 准备本地 Agent 复盘上下文，不调用外部 LLM
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology two_stage_ota \
  --prepare-agent-review
```

此时查看并填写：

```text
outputs/<project>/agent_review/
├── agent_context.md              # 给本地 Agent 读取的 BO 结果、Top run 参数和知识库规则
├── patch_plan_template.json      # 空模板
└── patch_plan.json               # 本地 Agent 填写 scale/set action
```

本地 Agent 填写 `patch_plan.json` 前，必须先阅读：

- `outputs/<project>/agent_review/agent_context.md`
- `knowledge_base/Opamp_knowledge_base/optimization_review_guide.md`

其中 `optimization_review_guide.md` 是调参规则来源；如果 Agent 的判断和指南冲突，需要在 `patch_plan.json` 的 `reason` 中说明原因。

本地 Agent 填写 `patch_plan.json` 后执行：

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology two_stage_ota \
  --patch-plan outputs/<project>/agent_review/patch_plan.json \
  --simulate
```

如果只是快速验证，也可以直接使用内置保守规则：

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology two_stage_ota \
  --simulate
```

无 Spectre 环境加 `--dry-run`。`patch_plan.json` 只允许对已有参数做 `scale` 或 `set`，未知参数会被忽略，所有数值会 clamp 到 topology 的 `get_param_space()` 范围；不要让 Agent 直接改 `.cir` 连接、模型、端口或 testbench。

---

## 第七步：达标后 PVT 验证

优化完成后，如果 `outputs/<project>/results.json` 中 `all_targets_met=true`，或 BO 后 Review 的 `candidate_metrics.csv` 中存在达标 candidate，应先做 PVT 验证。

默认 PVT 矩阵：`tt/ss/ff × VDD(min/typ/max) × temp(-40/27/125)`，共 27 个 corner。process section 来自 `pdk_profiles.py`，必要时用 `.env` 的 `PDK_PROCESS_SECTIONS=tt:top_tt,ss:top_ss,ff:top_ff` 覆盖。

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design

# Codex 默认只建议/检查 dry-run；真实 PVT 由用户在 Cadence/Spectre 环境执行
python pvt_simulation.py \
  --results outputs/<project>/results.json \
  --dry-run

python pvt_simulation.py \
  --results outputs/<project>/results.json \
  --simulate
```

输出在 `outputs/<project>/pvt/`：`pvt_results.csv`、`pvt_results.json`、`pvt_report.md` 和每个 corner 的 `diagnostics/metrics_summary.txt`。第一版 PVT 只报告 pass/fail 和最差 corner，不自动改电路。

---

## 第八步：PVT 达标后生成 Virtuoso 原理图

优化完成后，如果 nominal 与 PVT 都满足指标，可以导出最终 netlist 到 Virtuoso。

导出选择规则由 `export_to_virtuoso.py --results` 自动处理：

- 若 Review candidate 达标：导出 `outputs/<project>/agent_review/candidates/.../circuit.cir`
- 否则导出 BO 最优：`outputs/<project>/netlist/circuit.cir`

默认只生成 SKILL 和导出报告，不启动 Cadence：

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design

python export_to_virtuoso.py \
  --results outputs/<project>/results.json \
  --lib BO_Designs \
  --tech-lib tsmcN28
```

输出通常位于：

```text
outputs/<project>/virtuoso/
├── import_schematic.il
└── export_report.json
```

如果需要创建独立 Cadence 工作目录并尝试自动导入，显式加 `--run-virtuoso`：

```bash
python export_to_virtuoso.py \
  --results outputs/<project>/results.json \
  --lib BO_Designs \
  --tech-lib tsmcN28 \
  --include-cds-lib /home/userone/cds.lib \
  --pdk-lib-path /PDKS/TSMC28nm/tsmcN28 \
  --run-virtuoso
```

这会在 `Agent_LLM_BO/virtuoso_runs/<project>/` 下生成：

```text
cds.lib
import_schematic.il
run_import.il
virtuoso_import.log
README_import.md
```

`run_import.il` 会创建/打开目标 library，尝试绑定 `--tech-lib` 指定的工艺库，然后加载 `import_schematic.il` 生成 `BO_Designs/<cell>/schematic`。如果不使用 `--run-virtuoso`，也可以手动在 Virtuoso CIW 中 `load(".../import_schematic.il")`。

batch Virtuoso 常见问题：

- 如果报 `CDS.log File is already locked`，说明已有 GUI 进程占用默认 log；导出器默认把 `CDS_LOG` 指到 `virtuoso_runs/<project>/CDS.log`，也可以用 `--cds-log <path>` 覆盖。
- 如果报 `Tech library tsmcN28 is not visible`，说明 batch 进程没看到 PDK library；使用 `--include-cds-lib /home/userone/cds.lib` 引入用户/站点 `cds.lib`，或使用 `--pdk-lib-path /PDKS/TSMC28nm/tsmcN28` 显式写入 `DEFINE tsmcN28 ...`。

> 注意：导出脚本第一版是通过 SKILL 画 schematic，不做 layout，也不自动运行 ADE。

---

## 异常处理

### 仿真失败
1. 读取 `workspace/run_XXX/sim.log`
2. 收敛问题 → 调整偏置；模型未找到 → 检查 PDK 路径

### 优化结束仍未达标
1. 检查 `summary_report.txt` 的 gap 分析
2. 考虑：扩大参数搜索范围 / 建议放宽指标 / **换拓扑**（如 5t_ota → two_stage_ota）

---

## 架构说明

```
Agent (Claude Code)                      Python 脚本
───────────────────                      ────────────
• 解析用户需求                            • main.py: BO 优化循环
• 查阅知识库选择拓扑                       • topologies/: 硬约束网表生成
• 调 topologies/ 生成网表                 • simulator.py: Spectre 仿真
• 调 main.py 运行优化                     • review_optimization.py: BO 后 Review
• 读 results.json，汇报用户
• 读 agent_context.md 和知识库，填写 patch_plan.json
• 调 review_optimization.py 执行 patch plan
• 达标后调 export_to_virtuoso.py 导出 Virtuoso SKILL/工作区
```

## 参考资源

- **拓扑库**：[topologies/](Agent_LLM_BO/circuit_agent/topologies/) — `base.py`, `five_t_ota.py`, `__init__.py`
- **拓扑选择**：`topologies/__init__.py:get_topology_for_targets()`
- **代码入口**：[main.py](Agent_LLM_BO/circuit_agent/main.py)
- **Review 脚本**：[review_optimization.py](Agent_LLM_BO/circuit_agent/review_optimization.py)
- **Virtuoso 导出**：[export_to_virtuoso.py](Agent_LLM_BO/circuit_agent/export_to_virtuoso.py)
- **文件流说明**：[FILE_FLOW.md](Agent_LLM_BO/circuit_agent/FILE_FLOW.md)
- **Sizing 模式说明**：[SIZING_MODES.md](Agent_LLM_BO/circuit_agent/SIZING_MODES.md)
- **PDK 约束**：[tsmc28_pdk_constraints.md](knowledge_base/PDKs_info/tsmc28_pdk_constraints.md)
- **PDK Profile**：[pdk_profiles.py](Agent_LLM_BO/circuit_agent/pdk_profiles.py)
- **Review 指南**：[optimization_review_guide.md](knowledge_base/Opamp_knowledge_base/optimization_review_guide.md)
- **配置**：[config.py](Agent_LLM_BO/circuit_agent/config.py)
