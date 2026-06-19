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

> **文件命名**：根据电路拓扑命名，例如 5T OTA → `5t_ota.cir` + `tb_5t_ota_ac.scs`。所有文件放在同名文件夹下。

---

## 第二步：拓扑选择（决策树）

```
gain ≥ 40 dB → two_stage_ota 或 folded_cascode
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
- `./knowledge_base/PDKs_info/tsmc28_pdk_constraints.md`

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

其他输出详见 [README.md](README.md#输出结果)。

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
```

## 参考资源

- **拓扑库**：[topologies/](Agent_LLM_BO/circuit_agent/topologies/) — `base.py`, `five_t_ota.py`, `__init__.py`
- **拓扑选择**：`topologies/__init__.py:get_topology_for_targets()`
- **代码入口**：[main.py](Agent_LLM_BO/circuit_agent/main.py)
- **Review 脚本**：[review_optimization.py](Agent_LLM_BO/circuit_agent/review_optimization.py)
- **文件流说明**：[FILE_FLOW.md](Agent_LLM_BO/circuit_agent/FILE_FLOW.md)
- **PDK 约束**：[tsmc28_pdk_constraints.md](knowledge_base/PDKs_info/tsmc28_pdk_constraints.md)
- **Review 指南**：[optimization_review_guide.md](knowledge_base/Opamp_knowledge_base/optimization_review_guide.md)
- **配置**：[config.py](Agent_LLM_BO/circuit_agent/config.py)
