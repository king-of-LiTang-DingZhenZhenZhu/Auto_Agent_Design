# Circuit Design Agent - AI 操作手册

## 角色分工

| 角色 | 职责 |
|------|------|
| **你 (Codex)** | 理解需求 → 查阅知识库选择拓扑 → 调 Python 拓扑库生成网表文件 → 调 main.py 运行优化 → 读取结果 |
| **Python 拓扑库** (`topologies/`) | 硬约束生成 .cir / .sp 网表文件，保证语法正确 |
| **Python 脚本** (`main.py`) | 执行 Spectre 仿真、解析结果、运行 BO 优化循环 |

**你不会直接写 SPICE 网表、运行 Spectre、或修改参数** — 网表由拓扑库生成，仿真/优化交给 main.py。

---

## 完整工作流程

```
用户描述电路需求
      │
      ▼
① 解析需求，提取指标
      │
      ▼
② 查阅知识库，选择拓扑
   ├── knowledge_base/topology_selection_guide.md   ← 拓扑选择决策指南
   └── knowledge_base/pdk_constraints.md            ← TSMC N28 约束
      │
      ▼
③ 调 Python 拓扑库生成网表文件（硬约束，语法保证正确）
   在 <circuit_name>/ 文件夹下生成:
   ├── <circuit_name>.cir          # DUT 子电路（含 .param，系统自动提取搜索空间）
   ├── tb_<circuit_name>_ac.sp     # AC 仿真 testbench（含 .meas）
   ├── tb_<circuit_name>_tran.sp   # （可选）Transient 仿真 testbench
   ├── params.json                 # （可选）参数搜索空间，省略时自动从网表提取
   └── requirements.json           # 设计指标
      │
      ▼
④ 调用 python main.py --netlist <circuit_name>/<circuit_name>.cir --testbench <circuit_name>/tb_<circuit_name>_ac.sp <circuit_name>/tb_<circuit_name>_tran.sp --requirements <circuit_name>/requirements.json
   （--params 可省略，系统自动从网表 .param 声明中提取搜索空间并分配合理边界）
      │
      ▼
⑤ 读取 outputs/<project_name>/results.json，向用户汇报结果
```

> **文件命名**：根据电路拓扑命名，例如：5T OTA → `5t_ota.cir` + `tb_5t_ota_ac.sp`；两级运放 → `two_stage_ota.cir` + `tb_two_stage_ota_ac.sp`。所有生成的输入文件放在同名文件夹下。

---

## 第一步：解析用户需求

用户可能说：
- "设计一个5T OTA，增益>40dB，带宽>500MHz，PM>60°，功耗<1mW，负载500fF"
- "两级运放，gain>60dB，BW>100MHz，power<2mW"

提取为结构化指标：`gain_db`, `bandwidth_hz`, `phase_margin_deg`, `power_w`, `load_cap_f`, `topology_hint`

---

## 第二步：查阅知识库，选择拓扑

### 2.1 阅读知识库

按顺序阅读：
1. **[knowledge_base/topology_selection_guide.md](Agent_LLM_BO/circuit_agent/knowledge_base/topology_selection_guide.md)** — 有哪些可用拓扑、各自指标能力范围、选择决策树
2. **[knowledge_base/pdk_constraints.md](Agent_LLM_BO/circuit_agent/knowledge_base/pdk_constraints.md)** — TSMC N28 工艺约束（器件模型、W/L 范围、电流密度）

### 2.2 匹配拓扑

根据用户需求指标，从知识库中的决策树选择最合适的拓扑：

```
gain ≥ 60 dB → two_stage_ota 或 folded_cascode
gain < 60 dB → 5t_ota（最简单）
power < 100uW → 5t_ota + 亚阈值偏置
```

> **原则**：在满足指标的前提下，优先选择复杂度最低的拓扑。

### 2.3 查看可用拓扑列表

```bash
cd Agent_LLM_BO/circuit_agent
conda run -n circuit_agent python -c "
from topologies import list_topologies
for m in list_topologies():
    print(f'{m.name}: {m.display_name} (gain {m.min_gain_db}-{m.max_gain_db} dB)')
"
```

---

## 第三步：调 Python 拓扑库生成网表文件

### 3.1 使用拓扑库生成文件（一行完成）

```bash
cd Agent_LLM_BO/circuit_agent

# 一行生成整个项目目录
conda run -n circuit_agent python -c "
from topologies import get_topology
from models import DesignTarget

topo = get_topology('5t_ota')  # 根据第二步的决策选择
targets = DesignTarget(gain_db=40, bandwidth_hz=500e6, phase_margin_deg=60, power_w=0.001)

out = topo.write_project(
    '5t_ota',                    # 项目目录名
    targets=targets,
    original_requirement='设计一个5T OTA，增益>40dB，带宽>500MHz'
)
print(f'Project created: {out}')
"

# 结果：
#   5t_ota/
#   ├── 5t_ota.cir              # DUT 子电路网表
#   ├── tb_5t_ota_ac.sp         # AC testbench（含 .meas）
#   ├── tb_5t_ota_tran.sp       # （如果拓扑支持）Transient testbench
#   └── requirements.json       # 设计指标（自动生成，含拓扑名、默认参数）
```

> `write_project()` 一步完成：创建目录 → 写 .cir → 写所有 testbench → 写 requirements.json。Agent 无需手动处理文件。

### 3.2 网表文件规范

> Python 拓扑库生成的网表已经保证语法正确，无需手动检查。关键特性：
> - 每个晶体管 `nf=1`，W 为总有效宽度（系统自动拆 finger）
> - `.param` 声明所有可调参数
> - NMOS bulk → gnd! (vss), PMOS bulk → vdd!
> - `.meas` 名称匹配标准：`gain_dc`, `phase_dc`, `gbw_hz`, `phase_at_ugf`, `power_total`
> - W/L 最小单位 10nm

如需了解底层细节，见 [.Codex/rules/circuit_cir_guide.md](.Codex/rules/circuit_cir_guide.md) 和 [.Codex/rules/testbench_sp_guide.md](.Codex/rules/testbench_sp_guide.md)。

### 3.3 requirements.json 格式

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

## 第四步：调用 Python 脚本运行优化

```bash
cd Agent_LLM_BO/circuit_agent

python main.py \
  --netlist <circuit_name>/<circuit_name>.cir \
  --testbench <circuit_name>/tb_<circuit_name>_ac.sp \
  --requirements <circuit_name>/requirements.json
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
  --gain 40 --bw 500e6 --pm 60 --power 0.001 --load-cap 500e-15
```

---

## 第五步：读取结果

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
  "gap": {"gain_db": 2.3, ...},
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
│   └── tb_circuit.sp            # 仿真 testbench
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

Python 脚本不再调用 LLM 修复网表（网表由拓扑库生成，语法正确）。仿真失败通常是收敛问题或参数极端值导致：

1. 读取失败日志 `workspace/run_000/sim.log`
2. 分析错误类型（收敛问题 → 调整偏置参数；模型未找到 → 检查 PDK 路径）
3. 修改 `.cir` 或 `.sp` 文件（如有必要）后重新运行

### 优化结束仍未达标

1. 检查 `summary_report.txt` 中的 gap 分析
2. 看哪个指标差距最大
3. 考虑：
   - 扩大参数搜索范围（通过手动 params.json）
   - 建议用户放宽指标
   - **换拓扑**：查阅拓扑指南中的升级路径（如 5t_ota → two_stage_ota），用新拓扑重新生成网表再优化

---

## 快速开始示例

```bash
# 1. 配置环境（仅首次）
cd Agent_LLM_BO/circuit_agent
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

# 2. 生成网表（一行完成）
conda run -n circuit_agent python -c "
from topologies import get_topology
from models import DesignTarget

topo = get_topology('5t_ota')
targets = DesignTarget(gain_db=40, bandwidth_hz=5e8, phase_margin_deg=60, power_w=0.001)
topo.write_project('5t_ota', targets=targets, original_requirement='5T OTA gain>40dB BW>500MHz')
print('Project created: 5t_ota/')
"

# 3. 运行优化（dry-run 快速验证）
python main.py \
  --netlist 5t_ota/5t_ota.cir \
  --testbench 5t_ota/tb_5t_ota_ac.sp \
  --requirements 5t_ota/requirements.json \
  --dry-run

# 4. 查看结果
cat outputs/*/results.json
```

---

## 架构说明

```
Agent (Codex)                    Python 脚本
───────────────────                    ────────────
• 解析用户需求                          • main.py: 接收文件路径
• 查阅知识库选择拓扑                     • BO 优化循环
• 调 topologies/ 库生成网表文件           • 调用 Spectre 仿真
• 调用 main.py --netlist ...            • 解析结果，返回给 Agent
• 读 results.json，汇报用户
```

> **LLM 的角色已收敛**：仅用于 (1) `parse_user_requirements()` 解析自然语言需求，(2) `validate_and_adjust_params()` 在 BO 迭代中检查参数物理可行性。网表生成、修复、拓扑变更全部由 Python 硬约束代码处理。

## 参考资源

- **拓扑库代码**：[Agent_LLM_BO/circuit_agent/topologies/](Agent_LLM_BO/circuit_agent/topologies/)
  - `base.py` — 抽象基类
  - `five_t_ota.py` — 5T OTA 实现
  - `__init__.py` — 拓扑注册表 + 选择器
- **知识库**：[Agent_LLM_BO/circuit_agent/knowledge_base/](Agent_LLM_BO/circuit_agent/knowledge_base/)
  - `topology_selection_guide.md` — 拓扑选择决策指南
  - `pdk_constraints.md` — TSMC N28 PDK 约束
- **SPICE 编写规范**：[.Codex/rules/circuit_cir_guide.md](.Codex/rules/circuit_cir_guide.md) / [.Codex/rules/testbench_sp_guide.md](.Codex/rules/testbench_sp_guide.md)
- **代码入口**：[Agent_LLM_BO/circuit_agent/main.py](Agent_LLM_BO/circuit_agent/main.py)
- **配置文件**：[Agent_LLM_BO/circuit_agent/config.py](Agent_LLM_BO/circuit_agent/config.py)
