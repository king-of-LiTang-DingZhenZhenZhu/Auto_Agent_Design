# 电路设计项目文件流说明

本文说明从用户提出设计要求开始，项目如何生成电路网表和仿真网表，`main.py` 如何读取并渲染这些文件，Spectre 实际仿真使用哪些文件，以及 BO 优化结束后如何生成最终网表和结果文件。

本文描述的是当前代码的实际行为。涉及的主要模块如下：

| 模块 | 作用 |
|------|------|
| `topologies/*.py` | 根据选定拓扑生成初始 DUT 网表、testbench 和需求文件 |
| `topologies/base.py` | 提供 `write_project()`，统一写出项目输入文件 |
| `main.py` | 读取输入文件，建立参数空间，启动初始仿真和 BO 优化，保存最终结果 |
| `netlist.py` | 解析参数声明并把参数模板渲染成实际器件尺寸 |
| `gmid_lookup.py` | 在 gm/Id 模式下把 gm/Id 设计变量换算成实际 W/L |
| `simulator.py` | 为每轮仿真建立运行目录、写入实际网表、调用 Spectre 并组织结果解析 |
| `psf_results.py` | 读取 PSF ASCII 数据并计算 AC、DC、TRAN 指标 |
| `optimizer.py` | 执行 BO 循环，提出参数、运行仿真、计算 reward 并记录历史 |

---

## 1. 三类网表文件

理解整个流程时，首先要区分三类文件。

### 1.1 项目输入模板

由 `topologies/` 中的 Python 拓扑脚本生成，例如：

```text
folded_cascode/
├── folded_cascode.cir
├── tb_folded_cascode_ac.scs
├── tb_folded_cascode_sr.scs
├── tb_folded_cascode_st.scs
└── requirements.json
```

这些文件是后续所有仿真的源文件。

- `folded_cascode.cir` 是 DUT 子电路模板。
- `tb_folded_cascode_ac.scs` 是 AC 仿真模板。
- `tb_folded_cascode_sr.scs` 是大信号压摆率仿真模板。
- `tb_folded_cascode_st.scs` 是 0.1% 建立时间仿真模板。
- `requirements.json` 保存用户要求和拓扑信息。

DUT 网表中的 W/L 通常仍然使用参数名，例如：

```spectre
parameters Wdiff=10u Ldiff=120n

Mdiff1 (nout vip ntail vdd) pch_lvt_mac w=Wdiff l=Ldiff nf=1
Mdiff2 (nfold vin ntail vdd) pch_lvt_mac w=Wdiff l=Ldiff nf=1
```

因此它是一个可渲染模板，不是某一轮 BO 最终送入 Spectre 的固定尺寸网表。

testbench 中包含激励、负载、反馈网络、分析语句等。当前 AC testbench 保留拓扑脚本原本定义的共模源、单端 AC 激励和反馈网络，例如：

```spectre
VCM (vcm 0) vsource dc=VCM
VIPsrc (vinp vcm) vsource dc=0 type=sine mag=1
VINsrc (vinn 0) vsource dc=VCM
Rfb (vout vinn) resistor r=1G
Cfb (vinn 0) capacitor c=1
```

### 1.2 单轮实际仿真网表

每次初始仿真或 BO 迭代都会在 `workspace/run_xxx/` 下生成一组实际文件：

```text
workspace/
└── run_003/
    ├── circuit.cir
    ├── tb.scs
    ├── tb_1.scs
    ├── sim.log
    └── raw/
```

其中：

- `circuit.cir` 已经写入该轮的实际 W/L/nf。
- `tb.scs` 是该轮调用的主 testbench。
- `tb_1.scs` 等是额外 testbench。
- `sim.log` 是 Spectre 标准输出和错误输出。
- `raw/` 是 Spectre PSF 原始数据目录。

Spectre 实际运行的是这里的 `tb.scs`，不是项目目录中的原始 testbench。

### 1.3 BO 最终输出网表

优化结束后，`main.py` 会把选择出的最佳参数重新渲染，并写入：

```text
outputs/<project_name>/
├── netlist/
│   └── circuit.cir
├── simulation/
│   ├── tb_circuit.scs
│   └── tb_circuit_1.scs
├── data/
│   ├── sim.log
│   └── raw/
├── results.json
├── summary_report.txt
├── optimization_log.json
└── virtuoso/
    ├── import_schematic.il
    └── export_report.json
```

`outputs/<project_name>/netlist/circuit.cir` 是供查看、复现和导入 Virtuoso 使用的最终 DUT 网表。

---

## 2. 总体文件流

```text
用户自然语言要求
        |
        v
Codex 解析指标并选择 topology
        |
        v
topologies/<name>.py
        |
        | topo.write_project(...)
        v
项目输入目录
├── <name>.cir
├── tb_<name>_ac.scs
├── tb_<name>_sr.scs
├── tb_<name>_st.scs
└── requirements.json
        |
        | python main.py --netlist ... --testbench ...
        v
main.py 读取输入文件和参数空间
        |
        +--> 普通模式：BO 直接优化 W/L 等物理参数
        |
        +--> gm/Id 模式：BO 优化 gm/Id、支路电流、L 等设计变量
                         GmidSizer 再换算成实际 W/L
        |
        v
NetlistTemplate.render(physical_params)
        |
        v
workspace/run_xxx/
├── circuit.cir
└── tb.scs
        |
        | spectre tb.scs +aps -raw raw
        v
sim.log + raw/ + 仿真指标
        |
        | optimizer 计算 reward，向 BO 回传结果
        v
下一组参数，重复渲染和仿真
        |
        v
选择最佳 iteration
        |
        v
outputs/<project_name>/
├── netlist/circuit.cir
├── simulation/*.scs
├── results.json
└── optimization_log.json
```

---

## 3. 从用户要求生成初始项目

### 3.1 解析用户要求

用户可能输入：

```text
设计一个 folded cascode，增益大于 65 dB，
GBW 大于 300 MHz，相位裕度大于 60 度，功耗小于 2 mW。
```

需要提取为结构化指标：

```python
DesignTarget(
    gain_db=65,
    bandwidth_hz=300e6,
    phase_margin_deg=60,
    power_w=2e-3,
)
```

随后根据知识库和能力范围选择拓扑，例如 `folded_cascode`。

### 3.2 调用拓扑库

典型调用方式：

```python
from topologies import get_topology
from models import DesignTarget

topo = get_topology("folded_cascode")
targets = DesignTarget(
    gain_db=65,
    bandwidth_hz=300e6,
    phase_margin_deg=60,
    power_w=2e-3,
)

topo.write_project(
    "folded_cascode",
    targets=targets,
    original_requirement="设计一个 folded cascode ...",
)
```

调用链为：

```text
get_topology("folded_cascode")
    -> FoldedCascodeTopology
    -> BaseTopology.write_project()
    -> topology.get_circuit_files()
    -> 写出 DUT、testbench 和 requirements.json
```

`write_project()` 只负责生成项目输入模板，不会在此时运行 Spectre，也不会运行 BO。

### 3.3 DUT 网表如何生成

每个拓扑脚本负责定义：

- 子电路端口。
- MOS、R、C 等器件及连接关系。
- 可调参数的默认值。
- 器件模型名。
- W/L 的拓扑级硬约束和默认范围。
- gm/Id 分组信息（如果该拓扑支持）。

生成的 `.cir` 使用 Spectre syntax，典型结构为：

```spectre
simulator lang=spectre

parameters Wdiff=10u Ldiff=120n
parameters Wtail=8u Ltail=200n

subckt folded_cascode vip vin vout ibias vdd vss
Mdiff1 (...) pch_lvt_mac w=Wdiff l=Ldiff nf=1
Mdiff2 (...) pch_lvt_mac w=Wdiff l=Ldiff nf=1
...
ends folded_cascode
```

这里的 `Wdiff`、`Ldiff` 等参数同时承担两项作用：

1. 给项目提供一组初始尺寸。
2. 让 `ParamSpace.from_netlist()` 自动发现可优化参数。

### 3.4 testbench 如何生成

拓扑类还会生成一到多个 `.scs`：

```text
tb_folded_cascode_ac.scs
tb_folded_cascode_sr.scs
tb_folded_cascode_st.scs
```

testbench 通常包含：

- `include "circuit.cir"`。
- DUT 实例化。
- 电源和输入源。
- 负载。
- 反馈或测试网络。
- `dc`、`ac`、`tran` 等分析声明。
- Spectre 保存选项。

当前 testbench 在分析语句前加入：

```spectre
outOpts options rawfmt=psfascii
```

因此 Spectre 的 `raw/` 结果使用 PSF ASCII，而不是默认的 PSF binary。
Python 使用 `psf_utils` 直接读取这些文件，不需要为每轮仿真启动
Virtuoso 或 OCEAN。

testbench 中的固定测试条件，例如 VDD、VCM、负载电容和扫频范围，是在项目生成时根据拓扑默认值或用户要求写入的。

当前渲染流程会把同一轮参数应用到 DUT 和 testbench 的 `parameters`
声明。只有进入 BO 参数空间的变量会变化，因此负载、电源和分析设置
在一次优化任务中通常保持不变。

5T OTA 的 `VBIAS` 是 testbench 所有的优化参数：DUT 只接收 `vbias`
端口，不在 `.cir` 中重复声明 `VBIAS`。每轮 BO 会同时渲染所有
testbench 的：

```spectre
parameters VBIAS=...
VBIASsrc (vbias 0) vsource type=dc dc=VBIAS
```

因此 `VBIAS` 会随 trial 改变，而 `VDD`、`VCM`、`CL` 和输入阶跃幅度
仍保持项目生成时的固定测试条件。

### 3.5 requirements.json 的作用

当 `write_project()` 收到 `targets` 时，会生成 `requirements.json`，其中包括：

```json
{
  "original_requirement": "用户原始要求",
  "targets": {
    "gain_db": 65,
    "bandwidth_hz": 300000000,
    "phase_margin_deg": 60,
    "power_w": 0.002
  },
  "topology_name": "folded_cascode",
  "topology_display_name": "Folded Cascode OTA"
}
```

`topology_name` 不只是说明信息。当前 `main.py` 会使用它判断是否能为该拓扑启用 gm/Id sizing。

---

## 4. main.py 如何读取项目

典型命令：

```bash
conda activate Auto_Agent_Design

cd Agent_LLM_BO/circuit_agent

python main.py \
  --netlist folded_cascode/folded_cascode.cir \
  --testbench folded_cascode/tb_folded_cascode_ac.scs \
              folded_cascode/tb_folded_cascode_sr.scs \
              folded_cascode/tb_folded_cascode_st.scs \
  --requirements folded_cascode/requirements.json
```

### 4.1 读取输入文件

`main.py` 会读取：

```text
--netlist
    -> DUT 模板文本

--testbench
    -> 一个或多个 testbench 模板文本

--requirements
    -> 指标、原始要求和 topology_name

--params（可选）
    -> 手工指定的参数空间
```

读取后会建立 `CircuitFiles`，在内存中保存：

- DUT 文件名和文本。
- 主 testbench 文件名和文本。
- 额外 testbench 的文件名和文本。

### 4.2 参数空间的来源

参数空间有两条来源。

#### 路径 A：显式 params.json

如果传入 `--params`：

```text
params.json
    -> ParamSpace.from_dict()
```

此时以文件中定义的上下界、步长和参数类型为准。

#### 路径 B：从 DUT 自动提取

如果没有传入 `--params`：

```text
<topology>.cir
    -> ParamSpace.from_netlist()
```

解析器扫描 Spectre `parameters` 声明，并根据参数名判断类型：

- `W...`：晶体管宽度。
- `L...`：晶体管长度。
- 其他参数：按通用连续参数处理。

然后为它们分配搜索边界和量化步长。

### 4.3 普通 W/L 模式与 gm/Id 模式

#### 普通模式

BO 的变量直接是物理参数：

```text
Wdiff, Ldiff, Wtail, Ltail, ...
```

BO 提出的参数可以直接交给 `NetlistTemplate.render()`。

#### gm/Id 模式

如果 `requirements.json` 中包含有效的 `topology_name`，并且该拓扑提供 `get_gmid_spec()`，`main.py` 会尝试创建：

```text
gm/Id lookup table
    + topology gm/Id spec
    -> GmidSizer
```

此时 BO 搜索的变量会变成类似：

```text
gmid_input
gmid_load
gmid_bias
id_input
id_bias
L_input
L_load
...
```

在每轮仿真前执行：

```text
BO gm/Id 参数
    -> GmidSizer.size()
    -> 实际 W/L 参数
    -> NetlistTemplate.render()
```

因此 gm/Id 参数不会直接写入 MOS 网表。Spectre 最终看到的仍然是实际 `w`、`l` 和 `nf`。

---

## 5. main.py 建立工作区模板

开始仿真前，`main.py` 会把原始输入文件复制到工作区：

```text
workspace/
├── circuit_template.cir
├── tb_template.scs
└── tb_template_1.scs
```

这些文件主要用于：

- 保留本次运行使用的原始模板快照。
- 方便排查本次优化究竟从什么输入开始。
- 避免直接修改用户生成的项目输入目录。

`main.py` 后续渲染的是内存中的 `NetlistTemplate`。项目目录下原始 `.cir` 和 `.scs` 不会随着每轮 BO 被覆盖。

---

## 6. DUT 网表如何被渲染

### 6.1 渲染入口

核心调用关系为：

```text
main.py / optimizer.py
    -> Simulator.render_circuit_and_testbench()
    -> NetlistTemplate.render()
    -> workspace/run_xxx/circuit.cir
```

输入包括：

- 原始 DUT 模板。
- 当前轮物理参数。
- `ParamSpace` 中的范围、finger 和网格信息。

### 6.2 参数替换

假设模板包含：

```spectre
parameters Wtail=8u Ltail=200n
Mtail (...) nch_lvt_mac w=Wtail l=Ltail nf=1
```

当前轮物理参数为：

```python
{
    "Wtail": 12e-6,
    "Ltail": 300e-9,
}
```

渲染器会：

1. 检查参数是否在允许范围内。
2. 把 W/L 量化到工艺网格。
3. 根据总宽度确定 finger 数。
4. 把总 W 转换成单 finger W。
5. 更新 `parameters` 声明。
6. 把 MOS 行中的符号参数展开成实际数值。
7. 更新 MOS 的 `nf`。

例如总宽度 `Wtail=12u` 被拆为 4 个 finger 时，实际文件可能成为：

```spectre
parameters Wtail=3u Ltail=300n
Mtail (...) nch_lvt_mac w=3u l=300n nf=4
```

这里需要注意：

- BO 或 gm/Id sizing 给出的 W 通常表示总有效宽度。
- 写入单个 MOS `w` 的值是每个 finger 的宽度。
- 总有效宽度约等于 `w * nf`。
- 最终网表中的 `w` 不能脱离 `nf` 单独解释。

### 6.3 testbench 的处理

`Simulator.render_circuit_and_testbench()` 会把 testbench 文本写入当前运行目录：

```text
项目输入 tb_folded_cascode_ac.scs
    -> workspace/run_003/tb.scs
```

testbench 本身当前不经过 `NetlistTemplate.render()` 的参数替换。

testbench 通过相对路径包含同一运行目录中的 DUT：

```spectre
include "circuit.cir"
```

所以当 Spectre 在 `workspace/run_003/` 中执行 `tb.scs` 时，会自然加载该轮对应的 `workspace/run_003/circuit.cir`。

---

## 7. 初始仿真

在正式 BO 之前，`main.py` 会进行一次初始仿真。

### 7.1 初始参数

普通模式：

```text
ParamSpace.get_initial_params()
    -> 初始物理 W/L
```

gm/Id 模式：

```text
gm/Id 参数默认值
    -> GmidSizer.size()
    -> 初始物理 W/L
```

### 7.2 初始运行目录

初始仿真写入：

```text
workspace/run_000/
├── circuit.cir
├── tb.scs
├── tb_1.scs
├── sim.log
└── raw/
```

初始仿真的目的包括：

- 验证当前模板能否运行。
- 得到一组基准指标。
- 在 BO 开始前尽早暴露模型路径、语法或收敛问题。

当前实现中，BO 的第 0 次迭代也使用 `run_000`。因此 BO 开始后，初始仿真的 `run_000` 文件可能被第 0 次 BO 仿真覆盖。详见本文末尾的“当前实现注意事项”。

---

## 8. Spectre 实际调用过程

对主 testbench，`Simulator.run_spectre()` 会在当前 run 目录中执行类似：

```bash
spectre tb.scs +aps -raw raw
```

同时把输出写入：

```text
sim.log
```

关键点如下：

1. Spectre 的入口是 `tb.scs`。
2. `tb.scs` 通过 `include "circuit.cir"` 加载同一轮 DUT。
3. `circuit.cir` 已经包含这一轮实际 W/L/nf。
4. PSF 数据写入该轮的 `raw/`。
5. 日志写入该轮的 `sim.log`。
6. `psf_results.py` 从 `raw/` 读取当前 testbench 的 analysis 数据。

如果存在多个 testbench，优化器会依次运行主 testbench和额外 testbench，再合并可用指标。

---

## 9. BO 每一轮发生什么

`HybridOptimizer` 的单轮流程可以概括为：

```text
Optuna study.ask()
        |
        v
得到一组搜索变量
        |
        +-- 普通模式：已经是物理 W/L
        |
        +-- gm/Id 模式：GmidSizer -> 物理 W/L
        |
        v
可选的 LLM 参数物理可行性检查
        |
        v
渲染 workspace/run_<iteration>/circuit.cir
        |
        v
写入该轮 tb.scs
        |
        v
运行 Spectre
        |
        v
从 PSF ASCII 计算 gain / GBW / PM / power 等指标
        |
        v
根据目标计算 reward
        |
        v
study.tell(trial, reward)
        |
        v
写入 IterationRecord
```

### 9.1 每轮目录

例如：

```text
workspace/
├── run_000/
├── run_001/
├── run_002/
└── run_003/
```

每个目录都应当能够反映该轮实际送入 Spectre 的 DUT 和 testbench。

### 9.2 指标如何从 raw 数据得到

AC testbench 保存 `vout`，且当前 AC 输入源幅度为 1。Python 从
`raw/ac1.ac` 读取频率轴和复数输出波形，并计算：

```text
gain_db(f) = 20 * log10(abs(vout(f)))
gain_dc    = 最低仿真频点的 gain_db
gbw_hz     = gain_db 第一次由正值穿过 0 dB 的频率
PM         = 180 + UGF 处的输出相位
```

DC analysis 保存 `VDDsrc:p`。Python 从 `raw/op1.dc` 读取电源功率并取
绝对值，得到 `power_w`。

Transient testbench 保存 `vinp` 和 `vout`。Python 从 `raw/tran1.tran`
读取时间、输入和输出波形，用输入的 50% 交越划分上升/下降响应窗口，
并只在输出的 10%-90% 电平区间计算：

```text
SR+ = max(dVout/dt)
SR- = abs(min(dVout/dt))
SR  = min(SR+, SR-)
```

BO 使用较差方向的 `SR` 判断是否达标，同时在结果中保留独立的
`slew_rate_positive_v_per_s` 和 `slew_rate_negative_v_per_s`。

建立时间 testbench 使用 10 mV 小信号阶跃，analysis 名为 `stTran`。
Python 用输入的 50% 交越作为响应起点，以每个响应窗口最后 10% 波形
的中位数作为稳态输出，并采用统一的 0.1% 误差带：

```text
error_band = 0.001 * input_step
```

输出最后一次离开误差带之后的第一个采样点定义为建立完成。上升沿和
下降沿分别计算，`settling_time_s` 保存二者较大值，BO 按
`settling_time_s <= target` 判断是否达标。

当前结果读取顺序为：

```text
PSF ASCII raw/
    -> 成功：直接形成 SimResult
    -> 失败或 psf_utils 不可用：退回 sim.log / *.measure 文本解析
```

### 9.3 仿真失败时

如果某轮 Spectre 出错或没有解析到有效结果：

- 该轮会记录失败状态和错误信息。
- optimizer 会给该 trial 一个失败或惩罚 reward。
- BO 使用其他成功和失败样本继续建立搜索模型。
- 后续 iteration 仍会提出新的参数。

因此单次仿真失败通常不会终止整个优化，但大量连续失败会显著降低 BO 的有效信息量。

### 9.4 优化历史

每轮记录会保存到工作区历史文件，并最终复制为：

```text
outputs/<project_name>/optimization_log.json
```

普通模式下，一轮记录的 `params` 就是实际物理参数。

gm/Id 模式下：

- `params` 保存 BO 使用的 gm/Id 搜索变量。
- `physical_params` 保存该轮实际渲染网表使用的 W/L。

这两个字段在 gm/Id 模式下不能混用。

---

## 10. 如何选出 BO 最佳结果

optimizer 会根据每轮 reward 和目标完成情况选择最佳记录。

最佳记录通常包含：

```text
iteration
params
physical_params（gm/Id 模式）
metrics
reward
success
error
```

指标可能包括：

```text
gain_db
bandwidth_hz
phase_margin_deg
power_w
```

`main.py` 使用最佳记录生成：

- 最终网表。
- 结构化结果。
- 人类可读报告。
- 完整优化历史。
- Virtuoso 导入脚本。

---

## 11. BO 结束后的最终文件

最终保存逻辑位于 `main.py` 的 `_save_final_output()`。

### 11.1 最终 DUT

```text
outputs/<project_name>/netlist/circuit.cir
```

该文件不是简单复制某个 `workspace/run_xxx/circuit.cir`，而是：

```text
原始 NetlistTemplate
    + 最佳参数
    -> 再次调用 render()
    -> outputs/.../netlist/circuit.cir
```

理想情况下，它应与最佳 iteration 的实际 DUT 尺寸一致。

### 11.2 最终 testbench

```text
outputs/<project_name>/simulation/tb_circuit.scs
outputs/<project_name>/simulation/tb_circuit_1.scs
```

这些文件由最初读取的 `CircuitFiles` testbench 文本写出。当前实现不是直接复制最佳 `run_xxx/` 中的 testbench。

由于 testbench 当前不会随 BO 参数变化，所以正常情况下二者内容应相同。

### 11.3 最佳仿真数据

```text
outputs/<project_name>/data/sim.log
outputs/<project_name>/data/raw/
```

保存逻辑会根据历史记录中的最佳 iteration，尝试从对应的：

```text
workspace/run_<best_iteration>/
```

复制日志和 PSF 数据。

这些文件用于：

- 排查最佳仿真的收敛情况。
- 查看 Spectre 输出。
- 后续从 PSF 中提取曲线。
- 复核最终结果。

### 11.4 results.json

`results.json` 是程序和其他工具读取的主要输出，内容包括：

```json
{
  "converged": true,
  "all_targets_met": true,
  "metrics": {
    "gain_db": 67.2,
    "bandwidth_hz": 340000000,
    "phase_margin_deg": 63.1,
    "power_w": 0.0018
  },
  "params": {},
  "target_status": {},
  "gap": {},
  "netlist_file": "outputs/.../netlist/circuit.cir",
  "project_name": "..."
}
```

其中：

- `metrics`：最佳仿真的指标。
- `params`：保存时传入的最佳参数。
- `target_status`：每项指标是否达标。
- `gap`：指标与目标的差值。
- `netlist_file`：最终 DUT 路径。

### 11.5 summary_report.txt

这是面向用户阅读的摘要，通常包含：

- 是否收敛。
- 是否全部达标。
- 最佳指标。
- 指标 gap。
- 最佳参数。
- 最终文件位置。

### 11.6 optimization_log.json

保存完整 BO 历史，可用于：

- 画优化曲线。
- 比较每轮参数和指标。
- 定位失败 iteration。
- 复查最佳 iteration。
- 分析 gm/Id 参数与物理 W/L 的关系。

### 11.7 Virtuoso 导出

如果最终网表能够被 Virtuoso exporter 解析，还会生成：

```text
outputs/<project_name>/virtuoso/import_schematic.il
outputs/<project_name>/virtuoso/export_report.json
```

导出器读取的是最终：

```text
outputs/<project_name>/netlist/circuit.cir
```

而不是最初带符号参数的 DUT 模板。这样 Virtuoso 中得到的器件尺寸应当对应 BO 最终选择的实际 W/L/nf。

---

## 12. 文件是否会被修改

| 文件位置 | 是否被 BO 覆盖 | 说明 |
|----------|----------------|------|
| `<project>/<topology>.cir` | 否 | 原始 DUT 模板 |
| `<project>/tb_*.scs` | 否 | 原始 testbench |
| `<project>/requirements.json` | 否 | 原始设计要求 |
| `workspace/circuit_template.cir` | 每次任务重建 | 本次任务输入快照 |
| `workspace/run_xxx/circuit.cir` | 对应 iteration 可重建 | 该轮实际 DUT |
| `workspace/run_xxx/tb.scs` | 对应 iteration 可重建 | 该轮 Spectre 入口 |
| `outputs/<project>/netlist/circuit.cir` | 每次最终保存时重建 | 最终最佳 DUT |
| `outputs/<project>/results.json` | 每次最终保存时重建 | 最终结构化结果 |

---

## 13. 推荐的排查顺序

当结果异常时，建议按文件流从前到后检查。

### 13.1 检查项目输入

```text
<project>/<topology>.cir
<project>/tb_<topology>_ac.scs
requirements.json
```

确认：

- 拓扑和端口正确。
- `parameters` 声明完整。
- testbench 包含正确的 DUT。
- 指标单位使用 SI 单位。

### 13.2 检查工作区模板

```text
workspace/circuit_template.cir
workspace/tb_template.scs
```

确认 `main.py` 实际读取的是预期文件，而不是旧项目或错误路径。

### 13.3 检查某轮实际网表

```text
workspace/run_005/circuit.cir
workspace/run_005/tb.scs
```

确认：

- W/L 是否处于约束范围。
- `w * nf` 是否等于期望的总有效宽度。
- gm/Id sizing 后的物理参数是否合理。
- `tb.scs` 是否 include 当前目录的 `circuit.cir`。

### 13.4 检查 Spectre 输出

```text
workspace/run_005/sim.log
workspace/run_005/raw/
```

确认：

- 模型库是否成功加载。
- 是否存在语法错误。
- 是否存在收敛错误。
- 分析是否真正执行。
- PSF 数据是否生成。

### 13.5 检查优化记录

```text
workspace/history.json
outputs/<project>/optimization_log.json
```

确认：

- 最佳 iteration 是哪一轮。
- 该轮是否成功。
- gm/Id `params` 和 `physical_params` 是否匹配。
- reward 是否与指标改善方向一致。

### 13.6 检查最终输出

对比：

```text
workspace/run_<best_iteration>/circuit.cir
outputs/<project>/netlist/circuit.cir
```

二者的器件尺寸应当一致。如果不一致，应优先检查最终保存时传入的是搜索参数还是物理参数。

---

## 14. 当前实现注意事项

以下内容是当前代码中需要特别留意的实际问题。

### 14.1 PSF ASCII 读取依赖

当前 Python 结果提取依赖：

```text
numpy
psf_utils
```

依赖已写入 `requirements.txt`。运行环境中必须安装 `psf_utils`，否则
程序会退回旧的 `sim.log / *.measure` 文本解析。由于新的纯 Spectre
testbench 不生成 HSPICE 风格 `.measure`，缺少 `psf_utils` 时通常无法
得到完整的 gain、GBW、PM 和 power。

不同 Spectre/PDK 环境可能使用不同的 PSF 信号名称。当前解析器兼容
`vout`、`V(vout)`、`/vout`，以及常见的 `VDDsrc:p` 功率名称。第一次在
真实 Cadence 环境运行时，应查看 `psf.all_signals()`，确认实际名称。

### 14.2 gm/Id 模式的最终网表参数需要使用 physical_params

当前 optimizer 在 gm/Id 模式下分别保存：

```text
best.params
    -> gm/Id 搜索变量

best.physical_params
    -> 实际用于 Spectre 的 W/L
```

最终网表渲染应使用 `best.physical_params`。

当前 `main.py` 的最终保存调用仍把 `best.params` 传入 `_save_final_output()`。这可能使：

- 最佳 iteration 实际运行网表使用正确的物理 W/L；
- 但 `outputs/<project>/netlist/circuit.cir` 没有使用同一组物理 W/L。

在修正前，gm/Id 模式必须对比：

```text
workspace/run_<best_iteration>/circuit.cir
outputs/<project>/netlist/circuit.cir
```

### 14.3 初始仿真和 BO iteration 0 使用相同目录

当前初始仿真使用：

```text
workspace/run_000/
```

BO 第 0 轮也使用：

```text
workspace/run_000/
```

因此初始仿真的文件会被 BO 第 0 轮覆盖。若需要长期保存初始基准，应把初始仿真改到独立目录，例如：

```text
workspace/initial/
```

### 14.4 最终 DUT 是重新渲染的，不是直接复制最佳 DUT

最终 `circuit.cir` 通过“模板 + 最佳参数”重新生成。这样设计便于形成统一输出，但要求最终保存参数与最佳仿真的物理参数完全一致。

如果未来渲染逻辑发生变化，或者 gm/Id 参数传递错误，最终网表可能与最佳 run 不一致。更稳妥的复现策略是同时：

- 复制最佳 run 的实际 `circuit.cir`；并且
- 保存用于生成它的参数和模板版本。

---

## 15. 一句话总结

整个文件流可以概括为：

```text
topologies 生成带参数的 DUT 和固定测试条件的 testbench
    -> main.py 读取模板和设计指标
    -> 普通 BO 或 gm/Id sizing 得到每轮实际 W/L
    -> netlist.py 展开参数并拆分 finger
    -> simulator.py 在 workspace/run_xxx 中生成实际网表并调用 Spectre
    -> optimizer.py 根据仿真指标更新 BO
    -> main.py 用最佳参数生成 outputs 下的最终网表、报告和 Virtuoso 导入文件
```
