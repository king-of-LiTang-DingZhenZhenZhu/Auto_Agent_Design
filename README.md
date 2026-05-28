# Auto Agent Design — 模拟电路自动设计优化系统

基于 LLM + 贝叶斯优化（BO）的模拟电路自动设计与优化闭环系统。用户输入电路需求，系统自动完成网表生成、Spectre 仿真、结果分析和参数优化迭代。

## 项目结构

```
Auto_Agent_Design/
├── CLAUDE.md                          # Claude Code 操作手册（AI 辅助设计流程）
├── GIT_GUIDE.md                       # Git 使用指南
├── README.md                          # 本文件
│
└── Agent_LLM_BO/
    ├── circuit_agent/                 # 核心优化引擎
    │   ├── main.py                    # 入口：交互模式 / 文件模式
    │   ├── config.py                  # 全局配置（PDK、LLM、优化参数）
    │   ├── models.py                  # 数据模型（参数空间、设计目标等）
    │   ├── optimizer.py               # HybridOptimizer：LLM + BO 协同优化
    │   ├── llm_client.py              # DeepSeek LLM 客户端
    │   ├── simulator.py               # Spectre 仿真调用与结果解析
    │   ├── utils.py                   # 工具函数
    │   ├── requirements.txt           # Python 依赖
    │   ├── .env.example               # 环境变量模板
    │   ├── knowledge_base/            # 设计知识库
    │   │   └── pdk_constraints.md     # TSMC N28 PDK 约束
    │   ├── outputs/                   # 优化结果输出
    │   └── workspace/                 # 运行时工作目录
    │
    ├── Spice_Scripts/                 # HSPICE 格式参考
    │   ├── spice_guide.md             # SPICE 网表编写规范
    │   └── Examples/                  # 示例网表（5T OTA 等）
    │
    ├── Scs_Scirpts/                   # Spectre 格式参考
    │   ├── Spectre.scs脚本编写规范.md   # SCS 脚本编写规范
    │   └── Examples/                  # SCS 格式示例
    │
    └── topology_examples/             # 拓扑参考网表
```

## 核心思想

```
用户需求 → Agent 生成网表 → Spectre 仿真 → 结果对比目标 → LLM+BO 优化 → 新参数 → 再仿真 → ... → 达标输出
                ↑______________________________________________________________________________↓
                                                闭环自动优化
```

## 两种工作模式

### 模式一：交互模式（LLM 生成网表）

用户在终端对话中输入需求，LLM 自动生成初始网表后进入优化循环：

```bash
cd Agent_LLM_BO/circuit_agent
python main.py
```

### 模式二：文件模式（外部 Agent 提供网表）

由外部 Agent（如 Claude Code）预先生成网表和参数空间，直接启动优化。这也是 CLAUDE.md 中描述的主要工作流程：

```bash
cd Agent_LLM_BO/circuit_agent

python main.py \
  --netlist /path/to/circuit.cir \
  --params /path/to/params.json \
  --requirements /path/to/requirements.json
```

**简化调用（不写 requirements.json）：**

```bash
python main.py \
  --netlist circuit.cir \
  --params params.json \
  --gain 40 --bw 500e6 --pm 60 --power 0.001 --load-cap 500e-15
```

**常用可选参数：**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--max-iter N` | 最大优化迭代次数 | 50 |
| `--dry-run` | 跳过 Spectre，使用启发式模拟 | 关闭 |
| `--verbose` | 输出 DEBUG 日志 | 关闭 |
| `--project <name>` | 指定项目名称 | 自动生成 |

## 优化算法：LLM + BO 协同

| 方法 | 角色 | 优势 |
|------|------|------|
| **BO（贝叶斯优化）** | 全局搜索，Optuna 代理模型 + 采集函数 | 样本效率高，适合仿真成本高的场景 |
| **LLM（大语言模型）** | 提供电路设计直觉，分析参数-性能关系 | 利用先验知识，避免无意义探索 |

**协同策略：** LLM 每 N 轮验证一次优化方向，提供参数调整建议作为 BO 搜索的约束或初始点；BO 在 LLM 建议的方向上进行精细化搜索。两者交替提供候选参数，选择更优者执行。

## 配置与环境

### 环境变量

```bash
cd Agent_LLM_BO/circuit_agent
cp .env.example .env
```

编辑 `.env`，填入：

```env
DEEPSEEK_API_KEY=sk-xxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
```

### PDK 配置

默认使用 TSMC N28 (N28HPC+) PDK：
- **VDD**: 0.9V
- **NMOS Model**: `nch_mac`，**PMOS Model**: `pch_mac`
- **Min L**: 30nm（模拟推荐 ≥ 60nm）
- **Max Width per Finger**: 3μm

详见 [pdk_constraints.md](Agent_LLM_BO/circuit_agent/knowledge_base/pdk_constraints.md)。

### Python 依赖

```bash
pip install -r Agent_LLM_BO/circuit_agent/requirements.txt
```

依赖项：`openai`, `optuna`, `python-dotenv`, `pydantic`, `pydantic-settings`, `rich`

## 输出结果

优化完成后，结果保存在 `outputs/<project_name>/`：

```
outputs/<project_name>/
├── netlist/
│   └── circuit.cir              # 最优参数渲染后的电路
├── simulation/
│   └── tb_circuit_ac.sp         # 仿真 testbench
├── data/
│   ├── sim.log                  # 最优迭代的仿真日志
│   └── raw/                     # Spectre PSF 数据
├── results.json                 # 结构化结果（指标、参数、达标状态）
├── summary_report.txt           # 人类可读报告
└── optimization_log.json        # 完整优化历史
```

### results.json 关键字段

```json
{
  "converged": true,
  "metrics": {
    "gain_db": 42.3,
    "bandwidth_hz": 520000000,
    "phase_margin_deg": 63.5,
    "power_w": 0.00085
  },
  "params": {"Wtail": 12e-6, "Ltail": 80e-9, "…": "…"},
  "target_status": {"gain_db": true, "bandwidth_hz": true, "…": "…"},
  "all_targets_met": true
}
```

## 工作流程（Claude Code 集成）

完整的 AI 驱动设计流程详见 [CLAUDE.md](CLAUDE.md)，摘要如下：

1. **解析需求** — 从自然语言提取指标（增益、带宽、相位裕度、功耗等）
2. **生成网表** — 生成 `circuit.cir`、testbench、`params.json`、`requirements.json`
3. **调用优化** — `python main.py --netlist ... --params ... --requirements ...`
4. **读取结果** — 解析 `outputs/<project>/results.json`，汇报达标情况

## 异常处理

- **仿真失败**：自动尝试 LLM 修复（最多 3 次）
- **未达标**：检查 `summary_report.txt` 中的 gap 分析，考虑扩大参数范围或放宽指标
- **排查问题**：使用 `--verbose` 和 `--dry-run` 定位问题
