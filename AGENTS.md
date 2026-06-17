# Circuit Design Agent - AI 操作手册

## 角色分工

| 角色 | 职责 |
|------|------|
| **你 (Codex)** | 理解需求 → 查阅知识库/代码 → 修改 Python 拓扑库、优化器、解析器和文档 → 运行单元测试/静态检查 → 给出用户可手动执行的真实仿真命令 |
| **Python 拓扑库** (`topologies/`) | 硬约束生成 Spectre native syntax 的 `.cir` / `.scs` 网表文件，保证语法正确 |
| **Python 脚本** (`main.py`) | 在用户本地 Cadence/Spectre 环境中执行仿真、解析结果、运行 BO 优化循环 |

**当前协作边界**：Codex 主要负责改代码、改文档、跑 Python 单元测试和 dry-run；默认不在本机替用户跑真实 Spectre/BO 仿真。需要真实仿真时，Codex 应提供明确命令、说明预期输出位置，并根据用户贴回的日志/结果继续分析。

**你不会直接手写最终 SPICE/Spectre 网表或手动改优化参数** — 网表由拓扑库生成，仿真/优化由用户在本地环境调用 `main.py` 完成。

---

## 完整工作流程

> **环境要求**：生成网表或运行本项目之前，必须先执行 `conda activate Auto_Agent_Design` 激活项目环境。

```
用户描述电路需求
      │
      ▼
① 解析需求，提取指标
      │
      ▼
② 如果用户没有指定拓扑架构，通过拓扑库程序化匹配，选择拓扑
   ├── ./knowledge_base/Opamp_knowledge_base/topology_selection_guide.md
   └── ./knowledge_base/PDKs_info/tsmc28_pdk_constraints.md        ← TSMC N28 约束
      │
      ▼
③ 调 Python 拓扑库生成网表文件（硬约束，语法保证正确）
   在 <circuit_name>/ 文件夹下生成:
   ├── <circuit_name>.cir          # DUT 子电路（Spectre native syntax，扩展名保持 .cir）
   ├── tb_<circuit_name>_ac.scs    # Spectre AC testbench
   ├── tb_<circuit_name>_sr.scs    # Slew Rate transient testbench
   ├── tb_<circuit_name>_st.scs    # 0.1% Settling Time transient testbench
   ├── params.json                 # （可选）参数搜索空间，省略时自动从网表提取
   └── requirements.json           # 设计指标
      │
      ▼
④ 给出或检查 python main.py --netlist <circuit_name>/<circuit_name>.cir --testbench <circuit_name>/tb_<circuit_name>_ac.scs [tb_sr.scs] [tb_st.scs] --requirements <circuit_name>/requirements.json
   （--params 可省略，系统自动从网表 parameters 声明中提取搜索空间并分配合理边界）
   **只传 SR/ST testbench：仅当用户需求中包含摆率或建立时间指标时**
      │
      ▼
⑤ 用户完成真实仿真后，Codex 根据 outputs/<project_name>/results.json、summary_report.txt、optimization_log.json、workspace/run_xxx/diagnostics/*.csv 等文件分析结果
```

> **文件命名**：根据电路拓扑命名，例如：5T OTA → `5t_ota.cir` + `tb_5t_ota_ac.scs`；两级运放 → `two_stage_ota.cir` + `tb_two_stage_ota_ac.scs`。所有生成的输入文件放在同名文件夹下。

---

## 第一步：解析用户需求

用户可能说：
- "设计一个5T OTA，增益>40dB，GBW>500MHz，PM>60°，功耗<1mW，负载500fF"
- "两级运放，gain>60dB，GBW>100MHz，SR>100V/us，0.1%建立时间<20ns"

提取为结构化指标：`gain_db`, `bandwidth_hz`, `phase_margin_deg`, `power_w`, `load_cap_f`, `slew_rate_v_per_s`, `settling_time_s`, `topology_hint`。其中 `bandwidth_hz` 是兼容旧接口的字段名，当前实际表示 GBW/UGF。

---

## 第二步：查阅知识库，选择拓扑

### 2.1 阅读知识库

按以下方式获取拓扑信息和工艺约束：
1. 调用 `python -c "from topologies import list_topologies; [print(f'{m.name}: {m.display_name} (gain {m.min_gain_db}-{m.max_gain_db} dB, GBW {m.min_gbw_hz}-{m.max_gbw_hz} Hz)') for m in list_topologies()]"` — 查看可用拓扑和各指标能力范围
2. **[PDKs_info/tsmc28_pdk_constraints.md](Agent_LLM_BO/circuit_agent/PDKs_info/tsmc28_pdk_constraints.md)** — TSMC N28 工艺约束（器件模型、W/L 范围、电流密度）

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
conda activate Auto_Agent_Design
python -c "
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
conda activate Auto_Agent_Design

# 一行生成整个项目目录
python -c "
from topologies import get_topology
from models import DesignTarget

topo = get_topology('5t_ota')  # 根据第二步的决策选择
targets = DesignTarget(gain_db=40, bandwidth_hz=500e6, phase_margin_deg=60, power_w=0.001)

out = topo.write_project(
    '5t_ota',                    # 项目目录名
    targets=targets,
    original_requirement='设计一个5T OTA，增益>40dB，GBW>500MHz'
)
print(f'Project created: {out}')
"

# 结果：
#   5t_ota/
#   ├── 5t_ota.cir              # DUT 子电路网表
#   ├── tb_5t_ota_ac.scs        # Spectre AC testbench
#   ├── tb_5t_ota_sr.scs        # Slew Rate testbench
#   ├── tb_5t_ota_st.scs        # 0.1% Settling Time testbench
#   └── requirements.json       # 设计指标（自动生成，含拓扑名、默认参数）
```

> `write_project()` 一步完成：创建目录 → 写 .cir → 写所有 testbench → 写 requirements.json。Agent 无需手动处理文件。



### 3.3 requirements.json 格式

```json
{
  "original_requirement": "用户原始输入文本",
  "targets": {
    "gain_db": 40,
    "bandwidth_hz": 500000000,
    "phase_margin_deg": 60,
    "power_w": 0.001,
    "load_cap_f": 500e-15,
    "slew_rate_v_per_s": 100000000,
    "settling_time_s": 20e-9
  },
  "topology_hint": "5T OTA"
}
```

> **注意：所有值使用 SI 基本单位** — Hz 不是 MHz，W 不是 mW，F 不是 pF。

---

## 第四步：给出真实优化命令，或仅运行 dry-run/测试

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design

python main.py \
  --netlist <circuit_name>/<circuit_name>.cir \
  --testbench <circuit_name>/tb_<circuit_name>_ac.scs \
              <circuit_name>/tb_<circuit_name>_sr.scs \
              <circuit_name>/tb_<circuit_name>_st.scs \
  --requirements <circuit_name>/requirements.json
```

其中 AC testbench 必须传入；SR/ST testbench 仅当用户要求摆率或 0.1% 建立时间时传入。

> **重要**：除非用户明确要求并确认当前环境可用，Codex 不默认执行上面的真实 Spectre 优化命令。通常只负责生成/修改代码，并用 `python -m unittest discover -s tests`、`--dry-run` 或局部 parser 测试验证代码逻辑。真实仿真由用户在本地 Cadence/Spectre 环境中执行。

**常用可选参数：**

| 参数 | 说明 | 示例 |
|------|------|------|
| `--max-iter 20` | 最大优化迭代次数（默认50） | 快速验证时减少 |
| `--dry-run` | 跳过 Spectre，用启发式模拟 | 无 Spectre 环境测试 |
| `--verbose` | 输出 DEBUG 日志 | 排查问题时 |
| `--project <name>` | 指定项目名称 | 覆盖自动生成的名字 |
| `--gbw 500e6` | 设置 GBW/UGF 目标 | 推荐使用；`--bw` 保留为兼容别名 |
| `--sr 100e6` | 设置 Slew Rate 下限 | 单位 V/s |
| `--settling-time 20e-9` | 设置 0.1% 建立时间上限 | 单位 s |

**简化调用（不用 requirements.json）：**
```bash
python main.py \
  --netlist <circuit_name>/<circuit_name>.cir \
  --testbench <circuit_name>/tb_<circuit_name>_ac.scs \
              <circuit_name>/tb_<circuit_name>_sr.scs \
              <circuit_name>/tb_<circuit_name>_st.scs \
  --gain 40 --gbw 500e6 --pm 60 --power 0.001 --load-cap 500e-15 \
  --sr 100e6 --settling-time 20e-9
```

---

## 第五步：根据用户提供或本地已有结果分析

用户运行真实优化脚本结束后，Codex 可读取或让用户提供以下文件：

### 主要输出：`outputs/<project_name>/results.json`

```json
{
  "converged": true,
  "metrics": {
    "gain_db": 42.3,
    "bandwidth_hz": 520000000,
    "gbw_hz": 520000000,
    "phase_margin_deg": 63.5,
    "power_w": 0.00085,
    "slew_rate_positive_v_per_s": 130000000,
    "slew_rate_negative_v_per_s": 120000000,
    "slew_rate_v_per_s": 120000000,
    "settling_time_s": 15e-9
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
│   ├── tb_circuit.scs           # 第 1 个 testbench，通常为 AC/DC
│   ├── tb_circuit_1.scs         # 第 2 个 testbench，通常为 Slew Rate
│   └── tb_circuit_2.scs         # 第 3 个 testbench，通常为 Settling Time
├── data/
│   ├── sim.log                  # 最优迭代的仿真日志
│   └── raw/                     # Spectre PSF ASCII 数据
├── results.json                 # 结构化结果
├── summary_report.txt           # 人类可读报告
└── optimization_log.json        # 完整优化历史
```

### 仿真指标提取

真实运行时，`main.py` 通过 `simulator.py` 调用 Spectre，各 testbench 的结果写入 `raw/`。`psf_results.py` 使用 `psf_utils` 读取 PSF ASCII：

- AC：读取输出幅相，DC 增益取低频值；首次 0 dB 交越频率作为 GBW/UGF，并计算该处相位裕度
- 功耗：读取 DC 结果中的 `VDDsrc:p`
- SR：分别定位输入上升沿和下降沿，在输出各自 10% 到 90% 的区间计算 `dVout/dt`；`SR+=max(dVout/dt)`，`SR-=abs(min(dVout/dt))`，最终 `SR=min(SR+, SR-)`
- 建立时间：使用小信号阶跃，在上升、下降响应中分别寻找进入并持续保持在最终值 `±0.1%` 误差带内的时间，最终取较差者

### gm/Id 初始化与参数下界

gm/Id 模式由 lookup table 把目标 gm/Id、支路电流、预估 VDS 映射为器件 W/L；所有尾电流管的 `VDS` 预估统一为 `0.2V`。5T OTA 的 `VBIAS` 不再属于 BO 参数空间，而由尾管 lookup 的 `VGS/VSG` 自动推导。

当用户给定 GBW 和 CL 时，会先估算实现该 GBW 所需的跨导，再用允许的最大 gm/Id 得到理论最小支路电流，并收紧 BO 电流参数下界：

- 5T OTA：`gm=2π·GBW·CL`，单输入管电流 `x=gm/(gm/Id)_max`，尾电流下界 `2x`
- Two-stage OTA：先取 `Cc=0.5CL`，`gm1=2π·GBW·Cc`；尾电流下界 `2x`，第二级电流下界 `4x`
- Folded cascode 二级运放：同样用 `Cc=0.5CL`；尾支路 `2x`、两侧折叠支路各 `2x`、第二级 `4x`，整机最小电流估算为 `10x`

这些公式只用于建立物理合理的搜索下界，最终尺寸和性能仍由 Spectre + BO 决定。

### 拓扑升级策略

停滞检测代码仍然保留，但自动升级拓扑默认关闭（`enable_topology_escalation=False`）。当前优化固定在用户选定的 topology 内；需要换拓扑时，由 Agent 根据结果和拓扑选择指南重新生成项目。

---

## 异常处理

### 仿真失败怎么办

Python 脚本不再调用 LLM 修复网表（网表由拓扑库生成，语法正确）。真实仿真失败通常是收敛问题、参数极端值、PDK 路径或 Cadence 环境导致：

1. 用户运行真实仿真后提供或保留 `workspace/run_000/sim.log`
2. Codex 分析错误类型（收敛问题 → 调整参数空间/初始值；模型未找到 → 检查 PDK 路径；测量缺失 → 检查 testbench/解析器）
3. Codex 修改拓扑库、testbench 模板、参数空间或解析器代码
4. Codex 运行单元测试/dry-run 验证代码路径
5. 用户重新执行真实 Spectre 优化命令

### 优化结束仍未达标

1. 用户提供或保留 `summary_report.txt`、`results.json`、`optimization_log.json`
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
conda activate Auto_Agent_Design
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

# 2. 生成网表（一行完成）
python -c "
from topologies import get_topology
from models import DesignTarget

topo = get_topology('5t_ota')
targets = DesignTarget(gain_db=40, bandwidth_hz=5e8, phase_margin_deg=60, power_w=0.001)
topo.write_project('5t_ota', targets=targets, original_requirement='5T OTA gain>40dB GBW>500MHz')
print('Project created: 5t_ota/')
"

# 3. Codex 可运行 dry-run 快速验证代码路径；真实 Spectre 优化由用户手动去掉 --dry-run 后执行
python main.py \
  --netlist 5t_ota/5t_ota.cir \
  --testbench 5t_ota/tb_5t_ota_ac.scs \
              5t_ota/tb_5t_ota_sr.scs \
              5t_ota/tb_5t_ota_st.scs \
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
• 修改/调用 topologies 生成网表           • 调用 Spectre 仿真
• 运行单元测试和 dry-run                 • 解析结果，写入 outputs/
• 给出真实仿真命令                       • 用户在本地环境执行真实仿真
• 根据用户提供的结果继续分析
```

> **LLM 的角色已收敛**：仅用于 (1) `parse_user_requirements()` 解析自然语言需求，(2) `validate_and_adjust_params()` 在 BO 迭代中检查参数物理可行性。网表生成、修复、拓扑变更全部由 Python 硬约束代码处理。

## 参考资源

- **拓扑库代码**：[Agent_LLM_BO/circuit_agent/topologies/](Agent_LLM_BO/circuit_agent/topologies/)
  - `base.py` — 抽象基类
  - `five_t_ota.py` — 5T OTA 实现
  - `__init__.py` — 拓扑注册表 + 选择器
- **PDK 约束**：[PDKs_info/tsmc28_pdk_constraints.md](Agent_LLM_BO/circuit_agent/PDKs_info/tsmc28_pdk_constraints.md)
- **拓扑选择**：`topologies/__init__.py:get_topology_for_targets()` 程序化匹配
- **Spectre 编写规范**：[Agent_LLM_BO/Scs_Scirpts/Spectre.scs脚本编写规范.md](Agent_LLM_BO/Scs_Scirpts/Spectre.scs脚本编写规范.md)
- **Spectre 示例**：[Agent_LLM_BO/Scs_Scirpts/Examples/5T_OTA.scs](Agent_LLM_BO/Scs_Scirpts/Examples/5T_OTA.scs)
- **代码入口**：[Agent_LLM_BO/circuit_agent/main.py](Agent_LLM_BO/circuit_agent/main.py)
- **文件流说明**：[Agent_LLM_BO/circuit_agent/FILE_FLOW.md](Agent_LLM_BO/circuit_agent/FILE_FLOW.md)
- **配置文件**：[Agent_LLM_BO/circuit_agent/config.py](Agent_LLM_BO/circuit_agent/config.py)
