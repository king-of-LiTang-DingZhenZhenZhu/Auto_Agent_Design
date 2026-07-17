# Circuit Design Agent 操作规约

## 角色与边界

- Codex 负责理解需求、选择/修改拓扑代码、运行 Python 测试与 dry-run、分析结果并给出真实仿真命令。
- `topologies/` 负责程序化生成 Spectre DUT/testbench；不要手写最终网表或直接改 rendered `.cir`。
- `main.py` 负责给定拓扑下的 gm/Id 初始化、BO、Spectre 调用、解析和结果保存。
- `review_optimization.py` 负责 BO 后知识/数据驱动 Review；`parameter_effects.py` 与 `knowledge_review.py` 只提供分析，不直接修改参数空间或网表。
- 默认不替用户运行真实 Spectre/BO/PVT/Virtuoso。除非用户明确要求且环境可用，只运行单元测试、静态检查和 dry-run，并给出用户本地命令。
- 运行项目命令前先执行：

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
```

## 标准流程

1. 从需求提取 SI 单位指标：`gain_db`、`bandwidth_hz`（实际表示 GBW/UGF）、`phase_margin_deg`、`power_w`、`load_cap_f`、`slew_rate_v_per_s`、`settling_time_s`。
2. 用户未指定架构时，通过 `topologies.list_topologies()`、拓扑选择指南和 PDK 约束选择复杂度最低且可满足指标的拓扑。
3. 调 `topology.write_project()` 生成 `.cir`、AC/SR/ST testbench 和 `requirements.json`。
4. 用 `main.py` 运行 BO。AC testbench 必须传入；只有需求包含 SR/ST 时才传对应 testbench。
5. 读取 `results.json` 和诊断：
   - 达标 → Design Audit；无 blocker 后再进入 PVT。
   - 未达标 → 本地 Agent Review。
6. Review candidate 达标后再做 PVT；nominal 与 PVT 都通过后才作为可交付设计。
7. 最终用 `export_to_virtuoso.py --results` 生成 Virtuoso SKILL；仅在用户明确要求时加 `--run-virtuoso`。

`main.py` 不会自动运行 Agent Review 或 PVT。`design_flow_graph.py` 在 nominal 达标后自动生成 Design Audit，运行显式请求的 PVT/Virtuoso 并写 `flow_report.md`；BO 未达标或审计存在 blocker 时提示 Agent Review。

## 拓扑与层级设计

- 在满足指标的前提下优先简单拓扑：低/中增益优先 5T OTA，高增益或复杂动态指标再考虑 two-stage/folded cascode。
- 自动拓扑升级默认关闭；需要换拓扑时由 Agent 根据结果和知识库重新生成项目。
- `bandgap_ptat` 等系统级拓扑采用层级流程：先优化并 PVT 验证 child opamp，再冻结为 macro，最后优化 parent。
- parent BO 不展开 child 内部 W/L；child 与 parent 必须匹配 PDK profile、voltage domain、subckt 名和端口。
- 层级入口：

```bash
python hierarchical_flow.py --project <top_project>          # 默认 dry-run
python hierarchical_flow.py --project <top_project> --simulate
```

## PDK 规则

- PDK 路径、section、器件 model、VDD/允许范围、gm/Id 表、PVT 温度、Spectre options、Virtuoso tech library 和 topology preset 的唯一代码入口是 `pdk_profiles.py`。
- 不要在 topology 中硬编码 PDK 路径、model 名、电源默认值或某工艺专用初始 W/L。
- 晶体管类型通过 profile 字段选择，例如 `nmos_model/pmos_model` 或 `nmos_lvt_model/pmos_lvt_model`。
- profile `vdd` 是默认值，`vdd_min/vdd_max` 是允许范围；若 BO 搜索 VDD，必须显式加入参数空间并限制在该范围内。
- 新工艺/器件导致初值不合适时，优先修改 profile 的 `topology_presets`，不要直接污染通用 topology 默认值。
- 换工艺前验证：

```bash
python pdk_profiles.py --validate --require-gmid --require-virtuoso
# 真实 Cadence 机器可追加 --check-files
```

分析结果时优先检查 `outputs/<project>/pdk_profile_used.json`。

## 生成项目与运行 BO

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

常用参数：`--max-iter`、`--dry-run`、`--verbose`、`--project`、`--gain`、`--gbw`、`--pm`、`--power`、`--load-cap`、`--sr`、`--settling-time`。

gm/Id 初始化可用一阶关系建立电流下界：

- 单级：`gm ≈ 2π·GBW·CL`。
- Miller 多级：`gm1 ≈ 2π·GBW·Cc`。
- 这些公式只用于初始化/诊断，最终性能以 Spectre 为准。

BO early-stop 同时要求目标满足且 critical MOS 不在线性区；critical OP 用 `|vds|-|vdsat|` 判断，`0~50mV` 视为 near-edge。

## 结果读取

按以下顺序分析：

1. `outputs/<project>/results.json`
2. `outputs/<project>/optimization_metrics.csv`
3. `outputs/<project>/optimization_log.json`
4. `outputs/<project>/diagnostics/diagnostics_summary.txt`
5. `workspace/run_xxx/diagnostics/dc_operating_points.csv`
6. `workspace/run_xxx/sim.log`

关键结果：`all_targets_met`、`target_status`、`gap`、`metrics`、`params`、`operating_point_status`。

主要附加输出：

```text
outputs/<project>/
├── parameter_analysis/parameter_effects.{json,csv,md}
├── agent_review/
│   ├── agent_context.md
│   ├── patch_plan.json
│   ├── knowledge_analysis.{json,md}
│   ├── candidate_metrics.csv
│   └── candidates/
└── pvt/
```

## Agent Review

BO 达标后先由 `design_audit.py` 检查 critical OP、异常 MOS 尺寸/W/L、搜索边界贴边和功耗下降机会。blocker 阻止 PVT，warning 记录但允许继续。

BO 未达标时先准备上下文：

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology <topology> \
  --prepare-agent-review
```

Agent 读取紧凑的 `agent_context.md`，其中内嵌 Top run 指标/参数/边界，并按索引继续读取 `AGENT_REVIEW.md`、拓扑知识、BO 参数影响和理论派生诊断，然后填写 `patch_plan.json`。

- `parameter_effects.py` 给出搜索参数和物理参数对指标的 Spearman 经验趋势、边界聚集和收敛区间；相关性不是因果。
- `knowledge_review.py` 使用 `knowledge_base/circuit_design_relations.json`：单级/Miller GBW、两极点 PM、一阶 bandgap 等关系都必须检查适用假设。
- 理论、BO 趋势和 Spectre 不一致时，建议局部扰动实验，不要直接认定任一来源正确。
- Agent 只能对已有参数提出 `scale`/`set`；Python 会忽略未知参数并 clamp 到 topology 参数范围。

执行 Review candidate：

```bash
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology <topology> \
  --patch-plan outputs/<project>/agent_review/patch_plan.json \
  --simulate
```

当前限制：不会自动做局部扰动、自动调整参数空间、warm-start 重启 BO 或自动换拓扑；Review candidate 的 critical OP 尚未作为统一硬门槛，进入 PVT 前必须检查 candidate diagnostics。

## PVT 与 Virtuoso

nominal BO/Review candidate 达标后：

```bash
python pvt_simulation.py --results outputs/<project>/results.json --simulate
```

默认 PVT 为 `tt/ss/ff × VDD(min/typ/max) × temp(-40/27/125)`。PVT 失败时先看 `pvt_report.md` 和失败 corner diagnostics，不要直接导出最终设计。

PVT 通过后：

```bash
python export_to_virtuoso.py \
  --results outputs/<project>/results.json \
  --lib BO_Designs \
  --tech-lib <tech_lib>
```

若存在达标 Review candidate，导出器优先选择 candidate；否则选择 BO best。默认只生成 SKILL/报告，不启动 Cadence。

## 修改与验证

- 修复根因，保持改动最小；不要顺手修复无关问题或覆盖用户已有改动。
- topology 负责结构和参数空间；parser/simulator 负责测量，不在 `main.py` 加拓扑专用硬编码。
- 修改代码后先跑局部测试，再跑：

```bash
python -m unittest discover -s tests
```

- 默认不跑真实 Spectre。真实失败时依次检查 PDK、`sim.log`、收敛、极端参数、testbench 和 parser。

## 关键文档

- 拓扑选择：`knowledge_base/Opamp_knowledge_base/topology_selection_guide.md`
- 拓扑 Review：`knowledge_base/Opamp_knowledge_base/topologies/*_optimization.md`
- 结构化关系：`knowledge_base/circuit_design_relations.json`
- PDK：`knowledge_base/PDKs_info/pdk_profiles.md`、`Agent_LLM_BO/circuit_agent/pdk_profiles.py`
- 文件流：`Agent_LLM_BO/circuit_agent/FILE_FLOW.md`
- gm/Id：`Agent_LLM_BO/circuit_agent/SIZING_MODES.md`
- 层级优化：`Agent_LLM_BO/circuit_agent/HIERARCHICAL_OPTIMIZATION.md`
- Agent Review 细节：`Agent_LLM_BO/circuit_agent/AGENT_REVIEW.md`
