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
② 生成 4 个文件:
   ├── circuit.cir          # DUT 子电路（含 .param 可调参数）
   ├── tb_circuit_ac.sp     # AC 仿真 testbench（含 .meas）如果必要则生成其他的仿真脚本
   ├── params.json          # 参数搜索空间
   └── requirements.json    # 设计指标
      │
      ▼
③ 调用 python main.py --netlist circuit.cir --params params.json --requirements requirements.json
      │
      ▼
④ 读取 outputs/results.json，向用户汇报结果
```

---

## 第一步：解析用户需求

用户可能说：
- "设计一个5T OTA，增益>40dB，带宽>500MHz，PM>60°，功耗<1mW，负载500fF"
- "两级运放，gain>60dB，BW>100MHz，power<2mW"

提取为结构化指标：`gain_db`, `bandwidth_hz`, `phase_margin_deg`, `power_w`, `load_cap_f`, `topology_hint`

---

## 第二步：生成文件

### 2.1 circuit.cir — DUT 子电路网表

**必须遵循的规则：**

```
- .lib 语句在顶部：.lib '/PDKS/TSMC28nm/models/spectre/toplevel.l' TOP_TT
- 所有可调参数用 .param 声明
- 核心电路封装在 .subckt ... .ends 中
- NMOS model = nch_mac，PMOS model = pch_mac
- NMOS bulk → gnd! (或 vss)，PMOS bulk → vdd!
- 每个晶体管必须写 nf=1（系统自动更新 finger 数量）
- W 参数代表总有效宽度（系统自动拆分为 W_finger × nf）
- 端口顺序：输入 → 输出 → 偏置 → 电源 → 地
```

**PDK 约束：**

| 参数 | 最小值 | 最大值 | 说明 |
|------|--------|--------|------|
| L | 30nm | 1um | channel length |
| W (per finger) | 100nm | 3um | 超过3um 自动拆 finger |
| nf | 1 | 64 | finger 数量 |
| VDD | 0.9 | 1.1V | 核心电压 |
| Vth (NMOS) | ~0.4V | — | 典型阈值电压 |
| Vth (PMOS) | ~-0.4V | — | 典型阈值电压 |

**示例结构：**

```spice
* circuit.cir -- 5T OTA
.lib '/PDKS/TSMC28nm/models/spectre/toplevel.scs' top_tt

.param Wtail=10u Ltail=60n Wdp=5u Ldp=60n Wcm=8u Lcm=100n

.subckt ota_5t vip vin vout vdd vss
* --- Tail current source ---
Mtail ntail vbias vss vss nch_mac W='Wtail' L='Ltail' nf=1
* --- Differential pair ---
Mdp1 vx1 vip ntail vss nch_mac W='Wdp' L='Ldp' nf=1
Mdp2 vout vin ntail vss nch_mac W='Wdp' L='Ldp' nf=1
* --- Active load (current mirror) ---
Mcm1 vx1 vx1 vdd vdd pch_mac W='Wcm' L='Lcm' nf=1
Mcm2 vout vx1 vdd vdd pch_mac W='Wcm' L='Lcm' nf=1
.ends ota_5t
```

### 2.2 tb_circuit_ac.sp — AC 仿真 testbench

**必须遵循的规则：**

```
- .include "circuit.cir" 引入 DUT（相对路径，同目录）
- 定义电源 VDD、偏置 VBIAS、输入激励
- 实例化 DUT：Xdut ... <subckt_name>
- AC 分析：.ac dec 20 1 10G
- .meas 语句名称必须精确（解析器依赖这些名称）
- 末尾 .end
```

**必须的 .meas 语句（名称不能改）：**

```spice
.meas ac gain_db MAX VDB(vout)
.meas ac ugf WHEN VDB(vout)=0 CROSS=1
.meas ac phase_margin FIND VP(vout) WHEN VDB(vout)=0 CROSS=1
.meas dc power_total PARAM='-I(Vdd)*0.9'
```

**示例结构：**

```spice
* tb_circuit_ac.sp -- AC Analysis Testbench
.include "circuit.cir"

* --- Power Supplies ---
VDD vdd 0 DC 0.9
VSS vss 0 DC 0
VBIAS vbias 0 DC 0.5

* --- Input Stimulus (AC) ---
Vcm vcm 0 DC 0.45
Vinp vip vcm DC 0 AC 1
Vinn vin 0 DC 0.45 AC 0

* --- DUT ---
Xdut vip vin vout vdd vss ota_5t

* --- Load ---
CL vout 0 500f

* --- Analysis ---
.op
.ac dec 20 1 10G

* --- Measurements (names MUST match exactly) ---
.meas ac gain_db MAX VDB(vout)
.meas ac ugf WHEN VDB(vout)=0 CROSS=1
.meas ac phase_margin FIND VP(vout) WHEN VDB(vout)=0 CROSS=1
.meas dc power_total PARAM='-I(Vdd)*0.9'

.end
```

### 2.3 params.json — 参数搜索空间

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

### 2.4 requirements.json — 设计指标

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
  --netlist /path/to/circuit.cir \
  --params /path/to/params.json \
  --requirements /path/to/requirements.json
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
  --netlist circuit.cir \
  --params params.json \
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

# 2. 生成网表后运行优化（带 dry-run 测试）
python main.py \
  --netlist circuit.cir \
  --params params.json \
  --gain 40 --bw 500e6 --pm 60 --power 0.001 \
  --dry-run

# 3. 查看结果
cat outputs/*/results.json
```

---

## 参考资源

- 知识库：[Agent_LLM_BO/circuit_agent/knowledge_base/](Agent_LLM_BO/circuit_agent/knowledge_base/)
  - `pdk_constraints.md` — TSMC N28 PDK 约束
  - `spice_scripts_guide.md` — SPICE 编写规范
- 代码入口：[Agent_LLM_BO/circuit_agent/main.py](Agent_LLM_BO/circuit_agent/main.py)
- 配置文件：[Agent_LLM_BO/circuit_agent/config.py](Agent_LLM_BO/circuit_agent/config.py)
