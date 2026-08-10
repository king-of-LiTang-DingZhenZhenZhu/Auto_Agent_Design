# Auto Agent Design — 模拟电路自动设计优化系统

基于 **拓扑库 + gm/Id 查找表 + 贝叶斯优化** 的模拟电路自动设计闭环系统。用户描述需求后，系统生成 Spectre native 网表、调用 Spectre 仿真、解析结果并运行 BO 优化迭代，最终输出最优设计参数、诊断文件和可复现实验目录。

当前完整的叶子电路、系统分解、层级优化、Review、PVT 和导出流程见
[`FILE_FLOW.md`](FILE_FLOW.md)。

将当前前端交接给物理后端、继续开发版图/DRC/LVS/PEX/post-layout 自动化时，见
[`FULL_FLOW_AUTOMATION_HANDOFF.md`](FULL_FLOW_AUTOMATION_HANDOFF.md)。

## 项目结构

```
Auto_Agent_Design/
├── AGENTS.md                          # AI 操作手册（Codex 工作流程）
├── CLAUDE.md                          # Claude Code 配置（已合并至 AGENTS.md）
├── README.md                          # 本文件
├── FILE_FLOW.md                       # 当前完整项目流程与文件流
├── Agent_LLM_BO/
│   └── circuit_agent/                 # 核心设计与优化引擎
│       ├── main.py                    # 固定 topology 的 BO 入口
│       ├── system_decomposition.py    # 系统架构、block graph、指标与预算分解
│       ├── hierarchical_flow.py       # child-parent 依赖、冻结与嵌入
│       ├── design_flow_graph.py       # Audit/Review/PVT/导出状态编排
│       ├── models.py                  # 指标、参数和仿真数据模型
│       ├── optimizer.py               # BO 与 reward
│       ├── simulator.py               # Spectre 调用
│       ├── system_architectures/      # 系统 block graph、接口和指标预算
│       ├── pdk_integration/           # PDK profile、校验、callback 和器件表征
│       ├── passive_devices/           # R/C 器件映射和网表实现
│       ├── topologies/                # 按 amplifiers/references/regulators/comparators 分类
│       ├── virtuoso_export/           # Virtuoso SKILL 导出
│       ├── tests/
│       ├── outputs/
│       └── workspace/
├── knowledge_base/                    # 系统/运放/Bandgap/PDK 知识库
│   ├── System_knowledge_base/
│   ├── Opamp_knowledge_base/
│   ├── Bandgap_knowledge_base/
│   └── PDKs_info/
├── gmid_lookup_table/                 # gm/Id 查找表
├── Spice_Scripts/                     # HSPICE 格式参考
└── Scs_Scirpts/                       # Spectre 格式参考
```

## 核心流程

```text
用户需求
  |
  +-- 叶子模块：OTA/运放
  |     -> DesignTarget / MetricGoal
  |     -> 选择 topology
  |     -> write_project()
  |     -> main.py: gm/Id + BO + Spectre
  |     -> Design Audit / audit_repair 或 failure_repair
  |     -> PVT
  |     -> Virtuoso 导出
  |
  +-- 系统级电路：Bandgap/LDO/ADC
        -> system_decomposition.py
        -> system_design.json
        -> parent project + hierarchy.json
        -> hierarchical_flow.py
        -> child BO → Audit/Review gate/PVT
        -> frozen child artifact
        -> parent BO → Audit/Review gate/PVT
        -> Review/Audit 与导出
```

`system_decomposition.py` 是设计决策层，决定系统架构、block graph、
child targets、PVT targets、预算和 topology；`hierarchical_flow.py`
是执行层，只消费已经确定的 `ExecutableChildSpec`。

当前系统级规则已实现 Bandgap 和 LDO；ADC 尚未实现系统规则和 parent
topology。

## 快速开始

```bash
# 1. 激活环境
conda activate Auto_Agent_Design

# 2. 准备本地配置（PDK/.env；LLM API 仅在显式启用时需要）
cd Agent_LLM_BO/circuit_agent
cp .env.example .env
# 按需编辑 .env；默认 BO 优化不需要 DEEPSEEK_API_KEY

# 3. 一行生成网表项目
python -c "
from topologies import get_topology
from models import DesignTarget
topo = get_topology('5t_ota')
targets = DesignTarget(gain_db=40, bandwidth_hz=5e8, phase_margin_deg=60, power_w=0.001)
topo.write_project('my_ota', targets=targets, original_requirement='5T OTA gain>40dB GBW>500MHz')
"

# 4. 运行优化（dry-run 快速验证）
python main.py \
  --netlist my_ota/my_ota.cir \
  --testbench my_ota/tb_my_ota_ac.scs my_ota/tb_my_ota_sr.scs my_ota/tb_my_ota_st.scs \
  --requirements my_ota/requirements.json \
  --dry-run

# 5. 查看结果
cat outputs/*/results.json
```

系统级项目先生成 decomposition 和 hierarchy：

```bash
python system_decomposition.py \
  --requirements bandgap_requirements.json \
  --project bandgap_project

python hierarchical_flow.py \
  --project bandgap_project \
  --simulate
```

## 命令行参数

```
python main.py \
  --netlist <circuit>.cir \
  --testbench <tb_ac.scs> <tb_sr.scs> <tb_st.scs> \
  --requirements requirements.json
```

| 参数 | 说明 | 示例 |
|------|------|------|
| `--netlist` | DUT 子电路网表（.cir） | 必填 |
| `--testbench` | testbench 文件（.scs），可多个 | 至少 1 个 |
| `--params` | 参数搜索空间 JSON（可选，默认从网表自动提取） | `--params params.json` |
| `--requirements` | 设计指标 JSON | `--requirements requirements.json` |
| `--max-iter N` | 最大迭代次数（默认 50） | `--max-iter 20` |
| `--dry-run` | 跳过 Spectre，使用启发式模拟 | 调试用 |
| `--project <name>` | 指定输出项目名 | `--project my_design` |
| `--gain / --gbw / --pm / --power / --load-cap` | 快捷指定指标 | `--gain 40 --gbw 500e6` |
| `--sr / --settling-time` | 快捷指定摆率/建立时间 | `--sr 100e6 --settling-time 20e-9` |

**简化调用（不用 requirements.json）：**

```bash
python main.py \
  --netlist my_ota/my_ota.cir \
  --testbench my_ota/tb_my_ota_ac.scs my_ota/tb_my_ota_sr.scs my_ota/tb_my_ota_st.scs \
  --gain 40 --gbw 500e6 --pm 60 --power 0.001 --load-cap 500e-15 \
  --sr 100e6 --settling-time 20e-9
```

## BO 后 Review

BO 优化完成后，可对 Top 迭代应用指标缺口规则生成候选网表；也可以先生成本地 Agent review 上下文，再由本地 Claude/Codex 根据知识库填写 `patch_plan.json`。

```bash
cd Agent_LLM_BO/circuit_agent

# 直接使用内置保守规则
python review_optimization.py \
  --project outputs/<project> \
  --workspace workspace \
  --topology two_stage_ota \
  --simulate
```

规则参考：[optimization_review_guide.md](knowledge_base/Opamp_knowledge_base/optimization_review_guide.md)

## PVT 验证

BO 最优或 Review candidate 在 nominal 条件下达标后，建议先做 PVT 验证，再导出最终 schematic。`pvt_simulation.py` 会复用最终 netlist 选择逻辑：若 Review candidate 达标则优先验证它，否则验证 BO best。

默认 PVT 矩阵为 `tt/ss/ff × VDD(min/typ/max) × temp(-40/27/125)`，共 27 个 corner。process section 来自 `pdk_integration/profiles.py` 的 `process_sections`，可用 `.env` 中的 `PDK_PROCESS_SECTIONS=tt:top_tt,ss:top_ss,ff:top_ff` 覆盖。

```bash
cd Agent_LLM_BO/circuit_agent

# 只生成 PVT 目录和 patched netlist/testbench，不跑真实 Spectre
python pvt_simulation.py \
  --results outputs/<project>/results.json \
  --dry-run

# 在本地 Cadence/Spectre 环境中执行真实 PVT
python pvt_simulation.py \
  --results outputs/<project>/results.json \
  --simulate
```

输出位于 `outputs/<project>/pvt/`，包括 `pvt_results.csv`、`pvt_results.json`、`pvt_report.md` 和每个 corner 的 `raw/diagnostics/metrics_summary.txt`。第一版 PVT 只报告 pass/fail 和最差 corner，不自动改电路。

## Design Flow Graph

`design_flow_graph.py` 是 BO → Design Audit → Review gate → PVT → Virtuoso
的资格与状态编排入口。它不替代 BO，也不自动填写 `patch_plan.json`。
`hierarchical_flow.py` 会对每个 child 和 parent 调用它，统一 PVT 前门槛。
状态写入 `outputs/<project>/flow/flow_state.json` 和 `flow_report.md`。

```bash
cd Agent_LLM_BO/circuit_agent

# 只检查当前项目状态，给出 next_action
python design_flow_graph.py \
  --project outputs/<project>

# nominal/review 达标后生成 PVT dry-run 文件
python design_flow_graph.py \
  --project outputs/<project> \
  --run-pvt

# 显式允许真实 Spectre PVT，并在 PVT 通过后导出 Virtuoso SKILL
python design_flow_graph.py \
  --project outputs/<project> \
  --run-pvt \
  --simulate \
  --export-virtuoso
```

安装 `langgraph` 后会使用真实 `StateGraph`；若当前环境暂时没有该依赖，脚本会用同样节点顺序的 fallback 执行，便于先验证流程。

## Virtuoso 导出

`export_to_virtuoso.py --results outputs/<project>/results.json` 会导出最终应采用的 netlist：若 `agent_review/candidate_metrics.csv` 中存在满足原始目标的 review candidate，则优先导出该 candidate；否则导出 BO 最优的 `outputs/<project>/netlist/circuit.cir`。建议在 PVT 也通过后再导出。也可以用 `--netlist` 显式指定要导出的 `.cir`。

默认行为只生成 SKILL，不启动 Cadence：

```bash
python export_to_virtuoso.py \
  --results outputs/<project>/results.json \
  --lib BO_Designs
```

如需自动创建 Virtuoso 工作目录、生成 `cds.lib` 和 wrapper SKILL，并用批处理加载原理图：

```bash
python export_to_virtuoso.py \
  --results outputs/<project>/results.json \
  --lib BO_Designs \
  --tech-lib tsmcN28 \
  --include-cds-lib /home/userone/cds.lib \
  --pdk-lib-path /PDKS/TSMC28nm/tsmcN28 \
  --run-virtuoso
```

自动导入工作目录默认在：

```text
Agent_LLM_BO/virtuoso_runs/<project>/
├── cds.lib
├── import_schematic.il
├── run_import.il
├── virtuoso_import.log
└── README_import.md
```

`--tech-lib` 是 Virtuoso technology library 名称，不是 Spectre model include 文件路径。batch Virtuoso 不一定会自动读取用户主目录的 `cds.lib`，因此建议用 `--include-cds-lib` 显式引入站点/用户 `cds.lib`，或用 `--pdk-lib-path` 显式写入 `DEFINE tsmcN28 /PDKS/TSMC28nm/tsmcN28`。自动运行时脚本会把 `CDS_LOG` 指到工作目录下的 `CDS.log`，避免和已打开的 Virtuoso GUI 争用 `~/CDS.log` 锁。

原理图导出使用 PDK PCell 的 `dbCreateParamInst` 创建 MOS 和已映射 R/C，按
CDF 参数类型传入尺寸与倍乘参数；连接从 symbol master 的真实 pin 坐标生成
wire/net label，顶层端口使用 `basic/ipin`、`opin`、`iopin` 创建。生成的
`cds.lib` 会显式加载 `basic` 和 `analogLib`，batch wrapper 也会处理
`display.drf` 退出对话框，避免无图形导入完成后阻塞。

## 可用拓扑

| 拓扑 | 增益范围 | GBW 范围 | 复杂度 |
|------|---------|----------|--------|
| 5T OTA | 25–55 dB | 1 MHz – 2 GHz | 1 |
| Two-Stage Miller OTA | 45–80 dB | 10 MHz – 500 MHz | 2 |
| Folded Cascode OTA | 60–85 dB | 1 MHz – 1 GHz | 3 |
| NMCF Three-Stage OTA | 75–115 dB | 500 kHz – 600 MHz | 4 |
| Bandgap/PTAT Reference | 系统级 | 系统级 | 5 |

`bandgap_ptat` 先由 `system_decomposition.py` 生成系统架构、block graph、
child nominal/PVT targets 和 `hierarchy.json`，再把内部运放单独优化、
通过 Design Audit、必要的 Review 和 PVT 后冻结为 macro/subckt，最后
对 bandgap parent 执行相同资格流程。bandgap
级 BO 不展开 child 运放内部 W/L。相关规则见
`knowledge_base/Bandgap_knowledge_base/topologies/bandgap_ptat_optimization.md`。

论文专用低压基准还包括 `banba_sub1v_bandgap` 和
`leung_mok_sub1v_bandgap`；后者实现 Leung/Mok 2002 Fig. 3 的完整
603 mV 架构，并使用独立的启动、温度、PSRR 和线性调整率测试平台。

## gm/Id 设计方法

系统使用 gm/Id 查找表将目标跨导和电流映射为器件尺寸：

1. **BO 搜索 gm/Id 空间** — 搜索 `gm_id`、`L`、支路电流或整数电流比例。
2. **查找表映射** — `GmidSizer.size()` 根据 gm/Id、L、电流和预估 VDS/VBS 推导 W/L/nf/m。
3. **电流镜比例** — 镜像输出管使用整数倍率复制参考电流，宽器件先拆 `nf`，`nf>32` 后再用 `m`。
4. **偏置推导** — 支持由 lookup 的 VGS/VSG 推导 VBIAS；folded cascode 当前固定 internal bias generator，主路径通过电流比例和 gm/Id 推导尺寸。

普通物理参数 BO 与 gm/Id 模式的详细区别见：[SIZING_MODES.md](SIZING_MODES.md)。

无需手动处理单指 W 或 finger 数，系统使用 `2.6μm/finger` guard-band 满足 PDK bin 约束。

## 优化算法

| 方法 | 角色 |
|------|------|
| **BO（贝叶斯优化）** | Optuna TPE 采样，在物理参数或 gm/Id 参数空间中搜索 |
| **Spectre + parser** | 执行 AC/SR/ST 仿真，解析 gain/GBW/PM/power/SR/ST 与诊断数据 |
| **Local Agent Review（可选）** | BO 后读取指标、DC OP 和拓扑知识库，生成候选 patch plan 与候选网表 |
| **LLM（可选）** | 仅用于自然语言需求解析或实验性参数校验；BO 迭代默认不调用外部 LLM |

## PDK Profile 与约束

工艺相关信息集中在 `Agent_LLM_BO/circuit_agent/pdk_integration/profiles.py`，拓扑脚本从当前 profile 读取 Spectre include 路径、section、NMOS/PMOS/LVT model 名称、默认 VDD、VDD 允许范围、尺寸边界、gm/Id 表路径、PVT 温度列表、Spectre options 和 Virtuoso tech library。默认 profile 是 `tsmc28`。

可通过环境变量切换或覆盖：

```bash
export CIRCUIT_AGENT_PDK=tsmc28
export PDK_SPECTRE_PATH=/PDKS/TSMC28nm/models/spectre/toplevel.scs
export NMOS_MODEL=nch_mac
export PMOS_MODEL=pch_mac
export NMOS_LVT_MODEL=nch_lvt_mac
export PMOS_LVT_MODEL=pch_lvt_mac
export VDD=1.1
export VIRTUOSO_TECH_LIB=tsmcN28
```

也可以用外部 JSON profile：

```bash
export PDK_PROFILE_FILE=/path/to/my_pdk_profile.json
python Agent_LLM_BO/circuit_agent/pdk_integration/profiles.py --validate --require-gmid --require-virtuoso
```

VDD 使用优先级：单次 `params["VDD"]` 最高，其次 `.env`/环境变量 `VDD`，最后才是 profile 默认值。profile 中的 `VDD_MIN/VDD_MAX` 记录该工艺允许范围，例如 TSMC28 当前为 `0.9~1.1V`；如果希望 BO 搜索 VDD，应在 topology 的 `get_param_space()` 或显式 `params.json` 中加入 `VDD`，范围不要超过 profile 允许值。

晶体管类型由 topology 选择 profile 字段：`five_t_ota`、`two_stage_ota` 使用 `nmos_model/pmos_model`；`nmcnr_three_stage`、`mnmc_three_stage`、`nmcf_three_stage`、`folded_cascode` 与 `folded_cascode_two_stage` 使用 `nmos_lvt_model/pmos_lvt_model`。换 PDK 时改 profile，不要在 topology 模板里硬编码 model 名。

添加新工艺时，优先新增一个 PDK profile，而不是修改 topology。profile 至少需要包含：Spectre/HSPICE model include、process section、PVT corner section、VDD 范围、MOS model role、W/L 限制、gm/Id table path、Virtuoso tech lib 和 OA library path。然后运行：

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
python -m pdk_integration.profiles --validate --require-gmid --require-virtuoso
```

真实 Cadence VM 中可以额外加 `--check-files`，确认 PDK 路径实际存在。每次优化输出目录会保存 `pdk_profile_used.json`，用于复现实验。

默认 TSMC N28 约束：

| 参数 | 范围 | 说明 |
|------|------|------|
| L | 30 nm – 1 μm | 模拟推荐 ≥ 60 nm |
| W_per_finger | 100 nm – 2.6 μm | guard-band，低于 PDK bin 上界 |
| nf/m | nf ≤ 32 | `nf` 只把 instance 总宽 `W` 分成多个 finger；有效宽度为 `W*m` |
| VDD | 默认 0.9 V，允许 0.9–1.1 V | 单次设计可用 `VDD` 参数覆盖 |

## 输出结果

```
outputs/<project>/
├── initial_default/              # DEFAULT_PARAMS 初始仿真结果
├── initial_gmid/                 # 默认 gm/Id 推导尺寸后的初始仿真结果
├── netlist/
│   └── circuit.cir              # 最优参数渲染后的电路
├── simulation/
│   ├── tb_circuit.scs           # 第 1 个 testbench（通常 AC/DC）
│   ├── tb_circuit_1.scs         # 第 2 个（通常 Slew Rate）
│   └── tb_circuit_2.scs         # 第 3 个（通常 Settling Time）
├── data/
│   ├── sim.log                  # 最优迭代仿真日志
│   └── raw/                     # Spectre PSF ASCII 数据
├── diagnostics/
│   ├── dc_operating_points.csv   # MOS DC 工作点
│   ├── ac_response.csv           # AC 幅相数据
│   └── diagnostics_summary.txt   # 人类可读 DC/AC 诊断摘要
├── results.json                 # 结构化结果
├── summary_report.txt           # 人类可读报告
├── optimization_log.json        # 完整优化历史
├── optimization_metrics.csv      # 每轮主要指标表
├── agent_review/ (可选)          # BO 后 Review 结果
│   ├── candidates/               # 候选网表
│   ├── candidate_metrics.csv     # 候选仿真汇总
│   └── review_report.md          # Review 报告
├── pvt/ (可选)                   # PVT 验证结果
│   ├── corners/                  # 每个 PVT corner 的网表/仿真/诊断
│   ├── pvt_results.csv
│   ├── pvt_results.json
│   └── pvt_report.md
└── virtuoso/ (可选)             # Virtuoso SKILL 导入脚本
```

### results.json 字段

```json
{
  "converged": true,
  "metrics": {
    "gain_db": 42.3,
    "gbw_hz": 520000000,
    "bandwidth_hz": 520000000,
    "unity_gain_freq_hz": 520000000,
    "phase_margin_deg": 63.5,
    "power_w": 0.00085,
    "slew_rate_v_per_s": 120000000,
    "settling_time_s": 15e-9
  },
  "params": { "Wtail": 12e-6, "Ltail": 80e-9 },
  "target_status": { "gain_db": true, "gbw_hz": true },
  "all_targets_met": true
}
```

## Python 依赖

```bash
pip install -r Agent_LLM_BO/circuit_agent/requirements.txt
```

依赖项：`openai`, `optuna`, `scipy`, `python-dotenv`, `pydantic`, `pydantic-settings`, `rich`
