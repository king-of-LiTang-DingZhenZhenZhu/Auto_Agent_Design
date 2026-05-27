# 适用于 spectre 仿真器的 spice 脚本编写规范
## 1. 总体原则
* **模块化设计**：将被测电路（DUT）与测试激励分离。电路定义为子电路（`.subckt`），测试台调用子电路，便于复用和不同仿真模式的切换。
* **高可读性**：善用注释、空行和统一的命名规范，让脚本像代码一样易读。
* **健壮性**：采用闭环等稳定直流工作点的测试方法，避免开环 OTA 仿真中常见的直流不收敛问题。
---
## 2. 文件与目录结构规范

### 2.1 Agent 输入文件（Claude Code 生成）
Agent 生成的原始网表和配置文件采用扁平结构，存放在同一目录下，便于一键提交给 Python 脚本：

```text
<working_dir>/
├── circuit.cir           # DUT 子电路网表（含 .param 可调参数）
├── tb_circuit_xxx.sp      # circuit 电路的 xxx 类仿真 testbench（含 .meas 测量语句）
├── params.json           # 参数搜索空间定义
└── requirements.json     # 设计指标
```

> 详细生成规则参见项目根目录的 `CLAUDE.md`。


---
## 3. 命名规范
* **文件命名**：小写字母，下划线分隔，带前缀表明用途。如 `tb_xxx_dc.sp`（xxx电路直流测试台）、`tb_xxx_ac.sp`（xxx 电路交流测试台）、` xxx.cir`（xxx电路网表）。
* **节点命名**：
  * 电源/地：`vdd`, `vss` 或 `gnd`。
  * 偏置：`ibias`, `vbp`, `vbn`。
  * 差分对：`vinp`, `vinn`。
  * 内部关键节点：带物理意义，如 `ntail`（尾电流源漏极）、`vout1`（第一级输出）、`vout`（最终输出）。
* **器件命名**：
  * MOS 管：`M` + 类型 + 编号/功能。如 `MP1`（PMOS1）, `MN3`（NMOS3）, `MPTAIL`（尾电流管）。
  * 电容/电阻：`Cc`（补偿电容）, `Rz`（调零电阻）, `CL`（负载电容）。
---
## 4. 电路网表编写规范
### 4.1 子电路(.cir)编写规范
**必须遵循的规则：**

```
- .lib 语句在顶部：.lib '/PDKS/TSMC28nm/models/spectre/toplevel.l' TOP_TT
- 最好在引用工艺库时添加 .options redefinedparams=ignore /* 忽略模型文件中存在重复定义的参数 */
- 所有可调参数用 .param 声明
- 核心电路封装在 .subckt ... .ends 中
- NMOS model = nch_mac，PMOS model = pch_mac
- NMOS bulk → gnd! (或 vss)，PMOS bulk → vdd!
- 每个晶体管必须写 nf=1（系统自动更新 finger 数量）
- W 参数代表总有效宽度（系统自动拆分为 W_finger × nf）
- 端口顺序：输入 → 输出 → 偏置 → 电源 → 地
```
**示例结构：**

```spice
* circuit.cir -- 5T OTA
.lib '/PDKS/TSMC28nm/models/spectre/toplevel.l' TOP_TT
.options redefinedparams=ignore
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

### 4.2 仿真 testbench 编写规范

一个标准的测试台脚本应遵循以下从上到下的顺序：
#### 4.2.1 头部注释
说明脚本名称、功能、特殊测试方法（如闭环法）。
```spice
* tb_xxx_ac.sp -- xxx AC Analysis (Closed-Loop Method C)
```
#### 4.2.2 电路网表引入
```spice
.include "../design/...cir" /* 引入被测电路网表 */
```
#### 4.2.3 电源与偏置定义
先定义全局电源，再定义偏置电流/电压。
```spice
VDD vdd 0 DC 1.2
VSS vss 0 DC 0
Ibias vdd ibias DC 50u
```
#### 4.2.4 输入激励设置
明确共模电平（Vcm），在此基础上加交流小信号。
```spice
Vcm vcm 0 DC 300mV
Vinp vinp vcm DC 0 AC 1   /* 交流正输入 */
Vinn vinn 0  DC 0          /* 交流接地（配合闭环网络） */
```
#### 4.2.5 闭环反馈网络（针对高增益 OTA 推荐）
使用超大电阻（1G）和超大电容（1F）构成低通网络，直流时强闭环稳定工作点，交流时开环测增益。
```spice
Rfb vout vinn 1G
Cfb vinn 0 1
```
#### 4.2.6 被测电路调用 (DUT) 与负载
```spice
Xdut vinp vinn vout ibias vdd vss two_stage_ota_se
CL vout 0 500f
```

#### 4.2.7 仿真控制指令

Spectre（SPICE 语法模式）**不支持 `.control/.endc` 块**，所有仿真类型以独立指令行书写：

```spice
.op                           /* 直流工作点分析（建议在 AC 前显式触发） */
.ac dec 20 1 1g              /* AC 扫描：decade，20点/decade，1Hz~1GHz */
.temp 27                      /* 仿真温度 */
```

> 仿真结果由 Spectre 自动保存为 PSF 格式（`.raw` 目录），可通过 Python（`psf_utils`、`libpsf`）或 MATLAB 后处理读取，无需在脚本中手动导出。

#### 4.2.8 测量语句（.measure）

Spectre 支持 SPICE 风格的 `.measure` 语句，用于自动提取关键指标。每条语句独立书写，**不支持** `let` 变量或语句间引用，派生量（如相位裕度）需由 Python 后处理计算：

```spice
* 提取 DC 增益（在低频 1kHz 处读取幅度，单位 dB）
.measure ac gain_dc find vdb(vout) at=1k

* 提取 DC 相位（在低频 1kHz 处读取，单位度，用于 PM 计算）
.measure ac phase_dc find vp(vout) at=1k

* 提取 GBW（增益过 0dB 时的频率，单位 Hz）
.measure ac gbw_hz when vdb(vout)=0 cross=1

* 提取 UGF 处相位（单位度，用于 PM 计算）
.measure ac phase_at_ugf find vp(vout) when vdb(vout)=0 cross=1

* 提取功耗（单位 W）
.measure dc power_total PARAM='-I(Vdd)*0.9'
```

> **相位裕度计算**（Python 后处理）：
> ```
> PM = 180 - (phase_dc - phase_at_ugf)
> ```
> Spectre `.measure` 不支持变量间运算，提取 `phase_dc` 和 `phase_at_ugf` 后交由 Python 计算 PM。


---