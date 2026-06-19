# Auto Agent Design — 模拟电路自动设计优化系统

基于 **拓扑库 + gm/Id 查找表 + 贝叶斯优化** 的模拟电路自动设计闭环系统。用户描述需求，系统自动选择拓扑、生成 Spectre native 网表、调用 Spectre 仿真、运行 BO 优化迭代，最终输出达标的设计参数。

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
    │   ├── models.py                  # 数据模型
    │   ├── optimizer.py               # HybridOptimizer：LLM + BO 协同优化
    │   ├── review_optimization.py     # BO 后 Review：指标缺口分析 + 候选网表生成
    │   ├── simulator.py               # Spectre 仿真调用
    │   ├── llm_client.py              # DeepSeek LLM 客户端
    │   ├── psf_results.py             # PSF 结果解析（AC/瞬态）
    │   ├── gmid_lookup.py             # gm/Id 查找表与尺寸计算
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
    │   ├── Opamp_knowledge_base/      # 运放设计知识库
    │   │   └── topology_selection_guide.md
    │   ├── PDKs_info/                 # 工艺库信息
    │   │   └── tsmc28_pdk_constraints.md
    │   ├── outputs/                   # 优化结果输出
    │   └── workspace/                 # 运行时工作目录
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
⑥ BO 后 Review → 选取 Top 迭代，指标缺口规则生成候选网表，仿真验证
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

BO 优化完成后，可对 Top 迭代应用指标缺口规则生成候选网表：

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

## 可用拓扑

| 拓扑 | 增益范围 | GBW 范围 | 复杂度 |
|------|---------|----------|--------|
| 5T OTA | 25–55 dB | 1 MHz – 2 GHz | 1 |
| Two-Stage Miller OTA | 45–80 dB | 10 MHz – 500 MHz | 2 |
| Folded Cascode OTA | 60–85 dB | 1 MHz – 1 GHz | 3 |
| NMCF Three-Stage OTA | 75–115 dB | 500 kHz – 600 MHz | 4 |

## gm/Id 设计方法

系统使用 gm/Id 查找表将目标跨导和电流映射为器件尺寸：

1. **BO 搜索 gm/Id 空间** — 搜索 `gm_id`、`L`、支路电流等独立参数
2. **查找表映射** — `GmidSizer.size()` 将 gm/Id 参数转换为 W/L/nf
3. **电流镜比例** — 镜像输出管宽度由参考管 × 整数比自动推导
4. **偏置推导** — VBIAS 由尾管查表得到的 VGS 自动计算

无需手动处理 W/L 或 finger 数，系统自动满足 PDK 约束（W≤2.7μm/finger）。

## 优化算法

| 方法 | 角色 |
|------|------|
| **BO（贝叶斯优化）** | Optuna TPE 采样 + 高斯过程代理模型，全局搜索 |
| **LLM（大语言模型）** | 每 N 轮验证参数物理可行性，利用电路先验知识指导搜索方向 |

## PDK 约束（TSMC N28）

| 参数 | 范围 | 说明 |
|------|------|------|
| L | 30 nm – 1 μm | 模拟推荐 ≥ 60 nm |
| W_per_finger | 100 nm – 2.7 μm | 超出自动拆分 nf |
| nf | 1 – 64 | 建议 2 的幂次 |
| VDD | 0.9 V | 带 I/O 的 core 器件 |

## 输出结果

```
outputs/<project>/
├── netlist/
│   └── circuit.cir              # 最优参数渲染后的电路
├── simulation/
│   ├── tb_circuit.scs           # 第 1 个 testbench（通常 AC/DC）
│   ├── tb_circuit_1.scs         # 第 2 个（通常 Slew Rate）
│   └── tb_circuit_2.scs         # 第 3 个（通常 Settling Time）
├── data/
│   ├── sim.log                  # 最优迭代仿真日志
│   └── raw/                     # Spectre PSF ASCII 数据
├── results.json                 # 结构化结果
├── summary_report.txt           # 人类可读报告
├── optimization_log.json        # 完整优化历史
├── optimization_metrics.csv      # 每轮主要指标表
├── agent_review/ (可选)          # BO 后 Review 结果
│   ├── candidates/               # 候选网表
│   ├── candidate_metrics.csv     # 候选仿真汇总
│   └── review_report.md          # Review 报告
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
