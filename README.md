# Auto Agent Design — 模拟电路自动设计优化系统

基于 **拓扑库 + gm/Id 查找表 + 贝叶斯优化** 的模拟电路自动设计闭环系统。用户描述需求后，系统生成 Spectre native 网表、调用 Spectre 仿真、解析结果并运行 BO 优化迭代，最终输出最优设计参数、诊断文件和可复现实验目录。

## 项目结构

```
Auto_Agent_Design/
├── AGENTS.md                          # AI 操作手册（Codex 工作流程）
├── CLAUDE.md                          # Claude Code 配置（已合并至 AGENTS.md）
├── README.md                          # 本文件
│
└── Agent_LLM_BO/
    ├── circuit_agent/                 # 核心优化引擎
    │   ├── main.py                    # 入口：BO 优化循环
    │   ├── config.py                  # 全局配置
    │   ├── pdk_profiles.py            # PDK profile：模型路径、器件名、VDD、尺寸约束
    │   ├── models.py                  # 数据模型
    │   ├── optimizer.py               # HybridOptimizer：LLM + BO 协同优化
    │   ├── review_optimization.py     # BO 后 Review：指标缺口分析 + 候选网表生成
    │   ├── simulator.py               # Spectre 仿真调用
    │   ├── llm_client.py              # DeepSeek LLM 客户端
    │   ├── psf_results.py             # PSF 结果解析（AC/瞬态）
    │   ├── diagnostics_export.py       # DC/AC 诊断 CSV 与可读摘要
    │   ├── gmid_lookup.py             # gm/Id 查找表与尺寸计算
    │   ├── SIZING_MODES.md            # 普通 BO 与 gm/Id BO 参数空间说明
    │   ├── utils.py                   # 工具函数
    │   ├── requirements.txt           # Python 依赖
    │   ├── .env.example               # 环境变量模板
    │   │
    │   ├── topologies/                # 拓扑库（硬约束网表生成）
    │   │   ├── __init__.py            # 拓扑注册与选择器
    │   │   ├── base.py                # 抽象基类
    │   │   ├── five_t_ota.py          # 5T OTA
    │   │   ├── two_stage_ota.py       # 两级 Miller OTA
    │   │   ├── folded_cascode.py      # 折叠 Cascode OTA
    │   │   └── nmcf_three_stage.py    # NMCF 三级 OTA
    │   │
    │   ├── virtuoso_export/           # Virtuoso SKILL 导出
    │   │   ├── exporter.py
    │   │   ├── parser.py
    │   │   ├── placement.py
    │   │   └── skill_writer.py
    │   │
    │   ├── tests/                     # 单元测试
    │   │
    │   ├── outputs/                   # 优化结果输出
    │   └── workspace/                 # 运行时工作目录
    │
    ├── knowledge_base/                # 设计知识库与 PDK 说明
    │   ├── Opamp_knowledge_base/
    │   └── PDKs_info/
    │       ├── pdk_profiles.md
    │       └── tsmc28_pdk_constraints.md
    │
    ├── Spice_Scripts/                 # HSPICE 格式参考
    ├── Scs_Scirpts/                   # Spectre 格式参考
    └── topology_examples/             # 拓扑参考网表
```

## 核心流程

```
用户需求
    │
    ▼
① 解析需求 → 提取指标 (gain, GBW, PM, power, SR, settling time...)
    │
    ▼
② 查阅知识库 → 选择拓扑 (复杂度最低优先)
    │
    ▼
③ 拓扑库生成网表 → <circuit_name>/
    │   ├── <circuit_name>.cir          # DUT 子电路（Spectre native）
    │   ├── tb_*_ac.scs                 # AC testbench
    │   ├── tb_*_sr.scs                 # Slew Rate testbench
    │   ├── tb_*_st.scs                 # 0.1% 建立时间 testbench
    │   ├── params.json                 # 参数搜索空间（可选）
    │   └── requirements.json           # 设计指标
    │
    ▼
④ python main.py --netlist ... --testbench ... --requirements ...
    │
    ▼
⑤ 读取 outputs/<project>/results.json → 汇报结果
    │
    ▼
⑥ BO 后 Review → 选取 Top 迭代，指标缺口规则或本地 Agent patch plan 生成候选网表，仿真验证
```

## 快速开始

```bash
# 1. 激活环境
conda activate Auto_Agent_Design

# 2. 配置 LLM API
cd Agent_LLM_BO/circuit_agent
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

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

默认 PVT 矩阵为 `tt/ss/ff × VDD(min/typ/max) × temp(-40/27/125)`，共 27 个 corner。process section 来自 `pdk_profiles.py` 的 `process_sections`，可用 `.env` 中的 `PDK_PROCESS_SECTIONS=tt:top_tt,ss:top_ss,ff:top_ff` 覆盖。

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

`design_flow_graph.py` 是 BO → Review → PVT → Virtuoso 的上层编排入口。它不替代底层脚本，只读取当前项目状态并决定下一步，输出统一的 `outputs/<project>/flow/flow_state.json` 和 `flow_report.md`。

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

## 可用拓扑

| 拓扑 | 增益范围 | GBW 范围 | 复杂度 |
|------|---------|----------|--------|
| 5T OTA | 25–55 dB | 1 MHz – 2 GHz | 1 |
| Two-Stage Miller OTA | 45–80 dB | 10 MHz – 500 MHz | 2 |
| Folded Cascode OTA | 60–85 dB | 1 MHz – 1 GHz | 3 |
| NMCF Three-Stage OTA | 75–115 dB | 500 kHz – 600 MHz | 4 |

## gm/Id 设计方法

系统使用 gm/Id 查找表将目标跨导和电流映射为器件尺寸：

1. **BO 搜索 gm/Id 空间** — 搜索 `gm_id`、`L`、支路电流或整数电流比例。
2. **查找表映射** — `GmidSizer.size()` 根据 gm/Id、L、电流和预估 VDS/VBS 推导 W/L/nf/m。
3. **电流镜比例** — 镜像输出管使用整数倍率复制参考电流，宽器件先拆 `nf`，`nf>32` 后再用 `m`。
4. **偏置推导** — 支持由 lookup 的 VGS/VSG 推导 VBIAS；folded cascode 当前固定 internal bias generator，主路径通过电流比例和 gm/Id 推导尺寸。

普通物理参数 BO 与 gm/Id 模式的详细区别见：[SIZING_MODES.md](Agent_LLM_BO/circuit_agent/SIZING_MODES.md)。

无需手动处理单指 W 或 finger 数，系统使用 `2.6μm/finger` guard-band 满足 PDK bin 约束。

## 优化算法

| 方法 | 角色 |
|------|------|
| **BO（贝叶斯优化）** | Optuna TPE 采样，在物理参数或 gm/Id 参数空间中搜索 |
| **Spectre + parser** | 执行 AC/SR/ST 仿真，解析 gain/GBW/PM/power/SR/ST 与诊断数据 |
| **LLM（可选）** | 解析自然语言需求、周期性检查参数物理可行性；不负责修改拓扑网表 |

## PDK Profile 与约束

工艺相关信息集中在 `Agent_LLM_BO/circuit_agent/pdk_profiles.py`，拓扑脚本从当前 profile 读取 Spectre include 路径、section、NMOS/PMOS/LVT model 名称、默认 VDD、VDD 允许范围、尺寸边界、gm/Id 表路径、PVT 温度列表、Spectre options 和 Virtuoso tech library。默认 profile 是 `tsmc28`。

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
python Agent_LLM_BO/circuit_agent/pdk_profiles.py --validate --require-gmid --require-virtuoso
```

VDD 使用优先级：单次 `params["VDD"]` 最高，其次 `.env`/环境变量 `VDD`，最后才是 profile 默认值。profile 中的 `VDD_MIN/VDD_MAX` 记录该工艺允许范围，例如 TSMC28 当前为 `0.9~1.1V`；如果希望 BO 搜索 VDD，应在 topology 的 `get_param_space()` 或显式 `params.json` 中加入 `VDD`，范围不要超过 profile 允许值。

晶体管类型由 topology 选择 profile 字段：`five_t_ota`、`two_stage_ota`、`nmcf_three_stage` 使用 `nmos_model/pmos_model`；`folded_cascode` 使用 `nmos_lvt_model/pmos_lvt_model`。换 PDK 时改 profile，不要在 topology 模板里硬编码 model 名。

添加新工艺时，优先新增一个 PDK profile，而不是修改 topology。profile 至少需要包含：Spectre/HSPICE model include、process section、PVT corner section、VDD 范围、MOS model role、W/L 限制、gm/Id table path、Virtuoso tech lib 和 OA library path。然后运行：

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
python pdk_profiles.py --validate --require-gmid --require-virtuoso
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
