# 仿真 Testbench (.sp) 脚本编写规范

适用于 Spectre 仿真器的 `.sp` testbench 脚本编写规范。

## 1. 总体原则

- **激励与 DUT 分离**：testbench 负责提供电源、偏置、输入激励和负载，通过 `.include` 引入 DUT 子电路。
- **健壮性**：高增益 OTA 推荐使用闭环法稳定直流工作点，避免开环仿真中的收敛问题。
- **可测量性**：使用 `.measure` 语句自动提取关键性能指标，便于 Python 后处理。

## 2. Testbench 编写规范

一个标准的 testbench 脚本应遵循以下从上到下的顺序：

### 2.1 头部注释

说明脚本名称、功能、特殊测试方法。

```spice
* tb_ota_ac.sp -- 5T OTA AC Analysis (Closed-Loop Method)
```

### 2.2 引入 DUT 网表

```spice
.include "./circuit.cir"
```

### 2.3 电源与偏置定义

先定义全局电源，再定义偏置电流/电压。

```spice
VDD vdd 0 DC 0.9
VSS vss 0 DC 0
Vbias vbias 0 DC 0.5
```

### 2.4 输入激励设置

明确共模电平（Vcm），在此基础上加交流小信号。

```spice
Vcm vcm 0 DC 0.45
Vinp vinp vcm DC 0 AC 1    * 交流正输入
Vinn vinn 0  DC 0            * 交流接地（配合闭环网络）
```

### 2.5 DUT 调用与负载

```spice
Xdut vinp vinn vout vbias vdd vss ota_5t
CL vout 0 500f
```

### 2.6 仿真控制指令

Spectre（SPICE 语法模式）**不支持 `.control/.endc` 块**，所有仿真类型以独立指令行书写：

```spice
.op                           * 直流工作点分析（建议在 AC 前显式触发）
.ac dec 20 1 1g              * AC 扫描：decade，20点/decade，1Hz~1GHz
.temp 27                      * 仿真温度
```

### 2.7 测量语句（.measure）

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

#### 规范测量名称

以下为系统自动解析的标准测量名称（须严格匹配）：

| 测量名称 | 仿真类型 | 单位 | 说明 |
|---------|---------|------|------|
| `gain_dc` | AC | dB | 低频增益 |
| `phase_dc` | AC | 度 | 低频相位 |
| `gbw_hz` | AC | Hz | 单位增益带宽 |
| `phase_at_ugf` | AC | 度 | UGF 处相位 |
| `power_total` | DC | W | 总功耗 |

## 3. 完整示例

```spice
* tb_ota_ac.sp -- 5T OTA AC Analysis (Closed-Loop Method)
.include "./circuit.cir"

* --- Power supply ---
VDD vdd 0 DC 0.9
VSS vss 0 DC 0
Vbias vbias 0 DC 0.5

* --- Input stimulus ---
Vcm vcm 0 DC 0.45
Vinp vinp vcm DC 0 AC 1
Vinn vinn 0  DC 0

* --- Closed-loop feedback for DC stability ---
Rfb vout vinn 1G
Cfb vinn 0 1

* --- DUT ---
Xdut vinp vinn vout vbias vdd vss ota_5t
CL vout 0 500f

* --- Analysis ---
.op
.ac dec 20 1 1g
.temp 27

* --- Measurements ---
.measure ac gain_dc find vdb(vout) at=1k
.measure ac phase_dc find vp(vout) at=1k
.measure ac gbw_hz when vdb(vout)=0 cross=1
.measure ac phase_at_ugf find vp(vout) when vdb(vout)=0 cross=1
.measure dc power_total PARAM='-I(Vdd)*0.9'

.end
```
