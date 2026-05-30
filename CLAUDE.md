# Circuit Design Agent - AI 操作手册

## 角色分工

| 角色 | 职责 |
|------|------|
| **你 (Claude Code)** | 理解用户需求、生成网表文件、调用 Python 脚本、读取结果展示给用户 |
| **Python 脚本** | 执行 Spectre 仿真、解析结果、运行 LLM+BO 优化循环（50轮迭代） |

**你不会直接运行 Spectre 或修改参数** — 这些都交给 Python 脚本。

---

## 完整工作流程

```
用户描述电路需求
      │
      ▼
① 解析需求，提取指标
      │
      ▼
② 在 <circuit_name>/ 文件夹下生成文件（名称由LLM根据电路决定）:
   ├── <circuit_name>.cir          # DUT 子电路（含 .param 可调参数）
   ├── tb_<circuit_name>_ac.sp     # AC 仿真 testbench（含 .meas）
   ├── tb_<circuit_name>_tran.sp   # （可选）Transient 仿真 testbench
   ├── params.json                 # 参数搜索空间
   └── requirements.json           # 设计指标
      │
      ▼
③ 调用 python main.py --netlist <circuit_name>/<circuit_name>.cir --testbench <circuit_name>/tb_<circuit_name>_ac.sp <circuit_name>/tb_<circuit_name>_tran.sp --params <circuit_name>/params.json --requirements <circuit_name>/requirements.json
      │
      ▼
④ 读取 outputs/<project_name>/results.json，向用户汇报结果
```

> **文件命名**：不要硬编码为 `circuit.cir`。根据电路拓扑命名，例如：5T OTA → `5t_ota.cir` + `tb_5t_ota_ac.sp`；两级运放 → `two_stage_ota.cir` + `tb_two_stage_ota_ac.sp`。所有生成的输入文件放在同名文件夹下，避免散落在根目录。

---

## 第一步：解析用户需求

用户可能说：
- "设计一个5T OTA，增益>40dB，带宽>500MHz，PM>60°，功耗<1mW，负载500fF"
- "两级运放，gain>60dB，BW>100MHz，power<2mW"

提取为结构化指标：`gain_db`, `bandwidth_hz`, `phase_margin_deg`, `power_w`, `load_cap_f`, `topology_hint`

---

## 第二步：生成的各类文件的要求


### 2.1 网表文件 (.cir) 与 testbench (.sp)

- 编写规范参考存放到了 `.claude/rules` 下
- **文件命名**：不要硬编码为 `circuit.cir`，由 LLM 根据电路拓扑决定，如 `5t_ota.cir`、`two_stage_ota.cir`

**几个重要的点**
- 无论 .cir 还是 .sp 文件，开头第一行必须是注释，不允许写有效代码
- W 和 L 的最小单位是 10n，只能是 10n 的倍数增减

### 2.2 params.json — 参数搜索空间

**格式规范：**
- Width 参数：必有 `max_per_finger: 3e-6`
- Length 参数：不要 `max_per_finger`
- `log_scale: true` 适用于 W/L/C/R
- **绝对不要包含 nf 或 M 参数**（系统自动管理）

```json
[
  {"name": "Wtail", "low": 0.5e-6, "high": 20e-6, "log_scale": true, "unit": "m", "max_per_finger": 3e-6},
  {"name": "Ltail", "low": 30e-9,  "high": 500e-9, "log_scale": true, "unit": "m"},
  {"name": "Wdp",   "low": 0.5e-6, "high": 20e-6, "log_scale": true, "unit": "m", "max_per_finger": 3e-6},
  {"name": "Ldp",   "low": 30e-9,  "high": 500e-9, "log_scale": true, "unit": "m"},
  {"name": "Wcm",   "low": 0.5e-6, "high": 20e-6, "log_scale": true, "unit": "m", "max_per_finger": 3e-6},
  {"name": "Lcm",   "low": 30e-9,  "high": 1e-6,   "log_scale": true, "unit": "m"}
]
```

### 2.3 requirements.json — 设计指标

```json
{
  "original_requirement": "用户原始输入文本",
  "targets": {
    "gain_db": 40,
    "bandwidth_hz": 500000000,
    "phase_margin_deg": 60,
    "power_w": 0.001,
    "load_cap_f": 500e-15
  },
  "topology_hint": "5T OTA"
}
```

> **注意：所有值使用 SI 基本单位** — Hz 不是 MHz，W 不是 mW，F 不是 pF。

---

## 第三步：调用 Python 脚本

```bash
cd Agent_LLM_BO/circuit_agent

python main.py \
  --netlist /path/to/<circuit_name>/<circuit_name>.cir \
  --testbench /path/to/<circuit_name>/tb_<circuit_name>_ac.sp \
  --params /path/to/<circuit_name>/params.json \
  --requirements /path/to/<circuit_name>/requirements.json
```

**常用可选参数：**

| 参数 | 说明 | 示例 |
|------|------|------|
| `--max-iter 20` | 最大优化迭代次数（默认50） | 快速验证时减少 |
| `--dry-run` | 跳过 Spectre，用启发式模拟 | 无 Spectre 环境测试 |
| `--verbose` | 输出 DEBUG 日志 | 排查问题时 |
| `--project <name>` | 指定项目名称 | 覆盖自动生成的名字 |

**简化调用（不用 requirements.json）：**
```bash
python main.py \
  --netlist <circuit_name>/<circuit_name>.cir \
  --testbench <circuit_name>/tb_<circuit_name>_ac.sp \
  --params <circuit_name>/params.json \
  --gain 40 --bw 500e6 --pm 60 --power 0.001 --load-cap 500e-15
```

---

## 第四步：读取结果

脚本结束后，读取以下文件：

### 主要输出：`outputs/<project_name>/results.json`

```json
{
  "converged": true,
  "metrics": {
    "gain_db": 42.3,
    "bandwidth_hz": 520000000,
    "phase_margin_deg": 63.5,
    "power_w": 0.00085
  },
  "params": {"Wtail": 12e-6, "Ltail": 80e-9, ...},
  "target_status": {"gain_db": true, "bandwidth_hz": true, ...},
  "all_targets_met": true
}
```

关键字段：
- `all_targets_met` — 是否全部达标
- `target_status` — 每个指标是否达标
- `gap` — 每个指标与目标的差距（正=超额，负=不足）

### 其他输出文件

```
outputs/<project_name>/
├── netlist/
│   └── circuit.cir              # 最优参数渲染后的电路
├── simulation/
│   └── tb_circuit_ac.sp         # 仿真 testbench
├── data/
│   ├── sim.log                  # 最优迭代的仿真日志
│   └── raw/                     # Spectre PSF 数据
├── results.json                 # 结构化结果
├── summary_report.txt           # 人类可读报告
└── optimization_log.json        # 完整优化历史
```

---

## 异常处理

### 仿真失败怎么办

Python 脚本会自动尝试 LLM 修复（最多3次）。如果仍然失败：

1. 读取失败日志 `workspace/run_000/sim.log`
2. 分析错误类型（语法错误 / 收敛问题 / 浮空节点 / 模型未找到）
3. 修改 `.cir` 或 `.sp` 文件后重新运行

### 优化结束仍未达标

1. 检查 `summary_report.txt` 中的 gap 分析
2. 看哪个指标差距最大
3. 考虑：扩大参数搜索范围、建议用户放宽指标、或更换拓扑

---

## 快速开始示例

```bash
# 1. 配置环境（仅首次）
cd Agent_LLM_BO/circuit_agent
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
# 或者直接在终端export DEEPSEEK_API_KEY=
# 2. 生成网表后运行优化（带 dry-run 测试）
python main.py \
  --netlist <circuit_name>/<circuit_name>.cir \
  --params <circuit_name>/params.json \
  --gain 40 --bw 500e6 --pm 60 --power 0.001 \
  --dry-run

# 3. 查看结果
cat outputs/*/results.json
```

---

## 参考资源

- **SPICE 脚本编写规范**：[.claude/rules/circuit_cir_guide.md](.claude/rules/circuit_cir_guide.md) / [.claude/rules/testbench_sp_guide.md](.claude/rules/testbench_sp_guide.md)
  - `.cir` 子电路网表 → `.claude/rules/circuit_cir_guide.md`
  - `.sp` 仿真 testbench → `.claude/rules/testbench_sp_guide.md`
- **Spectre SCS 脚本编写规范**：[Agent_LLM_BO/Scs_Scirpts/Spectre.scs脚本编写规范.md](Agent_LLM_BO/Scs_Scirpts/Spectre.scs脚本编写规范.md)
  - 写 `.scs` 网表时参考：Spectre 原生语法、参数定义、仿真控制
- 知识库：[Agent_LLM_BO/circuit_agent/knowledge_base/](Agent_LLM_BO/circuit_agent/knowledge_base/)
  - `pdk_constraints.md` — TSMC N28 PDK 约束
- 代码入口：[Agent_LLM_BO/circuit_agent/main.py](Agent_LLM_BO/circuit_agent/main.py)
- 配置文件：[Agent_LLM_BO/circuit_agent/config.py](Agent_LLM_BO/circuit_agent/config.py)
