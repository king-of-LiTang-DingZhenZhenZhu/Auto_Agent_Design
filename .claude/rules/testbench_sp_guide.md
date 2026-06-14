---
paths:
  - "**/*.scs"
---

# Spectre 仿真 Testbench (.scs) 脚本编写规范

适用于 Spectre 仿真器的 `.scs` testbench 脚本编写规范。

## 1. 总体原则

- **激励与 DUT 分离**：testbench 负责提供电源、偏置、输入激励和负载，通过 `include` 引入 DUT 子电路（`.cir` 文件）。
- **Spectre native syntax**：全程使用 Spectre 原生语法（`vsource`、`capacitor`、`resistor`、`ac`、`tran`、`save` 等）。
- **数据导出优先**：指标提取不在 `.scs` 中完成（Spectre 没有 `.measure`），而是通过 `save`/`info` 将数据写入 PSF 文件，由 Python 后处理脚本读取。
- **健壮性**：高增益 OTA 推荐使用闭环法稳定直流工作点，避免开环仿真中的收敛问题。

## 2. 文件结构（从上到下）

```
1.  文件头注释（// 脚本名称、电路、分析方法）
2.  simulator lang=spectre insensitive=yes
3.  include PDK 模型文件（section=top_tt）
4.  include DUT 电路网表（.cir 文件）
5.  parameters 全局参数（电源电压、偏置等）
6.  电源与偏置激励（vsource）
7.  输入激励设置（共模电平 + 交流小信号）
8.  DUT 实例化与负载
9.  仿真控制（op → ac / tran）
10. save 语句（保存节点电压和器件变量）
11. opinfo info 语句（导出晶体管工作点）
```

## 3. 命名规范

### 电源与激励命名

| 器件 | 实例名 | 说明 |
|------|--------|------|
| 正电源 | `VDD` | 全局直流电源 |
| 地 | `0` | Spectre 全局地节点 |
| 偏置电压 | `VBIAS` | 偏置电压源 |
| 共模电压 | `VCM` | 输入共模电平 |
| 差分正输入 | `VIP` | 交流小信号正端 |
| 差分负输入 | `VIN` | 交流小信号负端 |

## 4. 编写规范

### 4.1 头部注释

第一行必须是用 `//` 注释说明脚本名称、电路和分析方法：

```spectre
// tb_ota_ac.scs — 5T OTA AC 分析 (闭环法)
```

### 4.2 仿真器声明与 PDK 引入

```spectre
simulator lang=spectre insensitive=yes

include "/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_tt
```

### 4.3 引入 DUT 网表

```spectre
include "./5t_ota/5t_ota.cir"
```

### 4.4 参数声明

```spectre
parameters VDD=1.1 VBIAS=0.5 VCM=0.55 CL=500f
```

### 4.5 电源与偏置

Spectre 使用 `vsource` 器件，格式：`<Name> (<pos> <neg>) vsource type=<type> <params>`

```spectre
// --- 电源 & 偏置 ---
VDD   (vdd 0)   vsource type=dc dc=VDD
VBIAS (vbias 0) vsource type=dc dc=VBIAS
```

### 4.6 输入激励

```spectre
// --- 输入激励（闭环 AC 分析）---
VCM (vcm 0) vsource type=dc dc=VCM
VIP (vip vcm) vsource type=dc dc=0 mag=0.5      // ac magnitude +0.5V
VIN (vin 0) vsource type=dc dc=0                 // AC 接地

// --- 闭环反馈网络（稳定 DC 工作点）---
Rfb (vout vin) resistor r=1G
Cfb (vin 0) capacitor c=1
```

> **说明**：`mag=0.5` 在 `VIP` 上设置 AC 幅度为 +0.5V。使用 `mag=-0.5` 在 `VIN` 上可获得差分激励。`mag` 参数只在 AC 分析中生效。

### 4.7 DUT 实例化与负载

```spectre
// --- DUT ---
X1 (vip vin vout vbias vdd 0) ota_5t

// --- 负载电容 ---
CL (vout 0) capacitor c=CL
```

> **注意**：DUT 实例端口顺序必须与 `.cir` 中 `subckt` 定义的端口顺序完全一致。

### 4.8 仿真控制

```spectre
// --- 仿真分析 ---
op1    dc   oppoint=rawfile                          // DC 工作点
opinfo info what=oppoint where=rawfile               // 导出所有晶体管工作点
ac1    ac   start=1k stop=10G dec=20                 // AC 扫描：1kHz~10GHz，20点/decade
```

常用 AC 参数：

| 参数 | 含义 |
|------|------|
| `start` | 起始频率 (Hz) |
| `stop` | 终止频率 (Hz) |
| `dec` | 每十倍频点数 |

可选瞬态分析：

```spectre
tran1  tran stop=100n step=10p                       // 瞬态：0~100ns，步长10ps
```

> **注意**：Spectre 没有 `.temp` 语句（默认 27°C），如需设置温度使用 `tran1 tran stop=100n step=10p temp=27`。

### 4.9 结果保存（save / info）

Spectre **不支持** `.measure` 语句。指标提取分两层：
- `.scs` 脚本：通过 `save` / `info` 控制保存哪些数据
- Python 后处理：读取 PSF 文件，提取指标

#### save 节点电压

```spectre
save vout vip vin vdd vbias
```

#### info 导出晶体管工作点（推荐）

```spectre
opinfo info what=oppoint where=rawfile
```

`info what=oppoint` 会将每个器件的完整工作点（vgs、vds、vth、vdsat、ids、gm、gds 等）全部写入 PSF rawfile，无需逐变量枚举。

#### 逐管 save（备选方案）

```spectre
save M1:vgs M1:vds M1:vth M1:vdsat M1:ids M1:gm M1:gds
save M2:vgs M2:vds M2:vth M2:vdsat M2:ids M2:gm M2:gds
save Mtail:vgs Mtail:vds Mtail:vth Mtail:vdsat Mtail:ids Mtail:gm Mtail:gds
```

### 4.10 规范输出指标名称

以下为 Python 后处理脚本从 PSF 数据中提取的标准指标名称（与 `simulator.py` 兼容）：

| 指标名称 | 分析类型 | 单位 | 说明 |
|---------|---------|------|------|
| `gain_dc` | AC | dB | 低频增益（从 vout 频率响应提取） |
| `phase_dc` | AC | 度 | 低频相位 |
| `gbw_hz` | AC | Hz | 单位增益带宽（增益过 0dB 的频率） |
| `phase_at_ugf` | AC | 度 | UGF 处相位（用于计算 PM） |
| `power_total` | DC | W | 总功耗（= VDD × I_VDD） |
| `slew_rate` | TRAN | V/s | 压摆率（取绝对值） |
| `slew_rate_neg` | TRAN | V/s | 负向压摆率 |
| `settling_time` | TRAN | s | 建立时间 |

> **兼容性**：`gain_db`、`ugf`、`phase_margin`、`pm`、`power` 等旧名称仍被 `simulator.py` 支持，但推荐使用规范名称。

## 5. 完整示例

### 5.1 AC 分析 Testbench

```spectre
// tb_ota_ac.scs — 5T OTA AC 分析 (闭环法)
// TSMC 28nm CLN28HPC+ | VDD=1.1V

simulator lang=spectre insensitive=yes

include "/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_tt
include "./5t_ota/5t_ota.cir"

parameters VDD=1.1 VBIAS=0.5 VCM=0.55

// --- 电源 & 偏置 ---
VDD   (vdd 0)   vsource type=dc dc=VDD
VBIAS (vbias 0) vsource type=dc dc=VBIAS

// --- 输入激励 ---
VCM (vcm 0) vsource type=dc dc=VCM
VIP (vip vcm) vsource type=dc dc=0 mag=0.5
VIN (vin 0) vsource type=dc dc=0

// --- 闭环反馈（稳定 DC 工作点，AC 分析中通过大电阻隔离）---
Rfb (vout vin) resistor r=1G
Cfb (vin 0) capacitor c=1

// --- DUT + 负载 ---
X1 (vip vin vout vbias vdd 0) ota_5t
CL (vout 0) capacitor c=500f

// --- 仿真 ---
op1    dc   oppoint=rawfile
opinfo info what=oppoint where=rawfile
ac1    ac   start=1k stop=10G dec=20

// --- 保存关键节点 ---
save vout vip vin vdd vbias
```

### 5.2 瞬态分析 Testbench（可选）

```spectre
// tb_ota_tran.scs — 5T OTA 瞬态分析 (单位增益缓冲器)
// TSMC 28nm CLN28HPC+ | VDD=1.1V

simulator lang=spectre insensitive=yes

include "/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_tt
include "./5t_ota/5t_ota.cir"

parameters VDD=1.1 VBIAS=0.5 VCM=0.55

// --- 电源 & 偏置 ---
VDD   (vdd 0)   vsource type=dc dc=VDD
VBIAS (vbias 0) vsource type=dc dc=VBIAS

// --- 脉冲输入（单位增益缓冲器配置）---
VPULSE (vin 0) vsource type=pulse val0=0.45 val1=0.65 delay=1n \
    rise=100p fall=100p width=5n period=20n

// --- DUT（连接为缓冲器：vout → vin 反馈）---
X1 (vin vout vout vbias vdd 0) ota_5t
CL (vout 0) capacitor c=500f

// --- 仿真 ---
op1    dc   oppoint=rawfile
opinfo info what=oppoint where=rawfile
tran1  tran stop=50n step=10p

// --- 保存关键节点 ---
save vout vin
```

## 6. 与 HSPICE Testbench 的关键区别

| 项目 | HSPICE（旧） | Spectre（新） |
|------|-------------|--------------|
| 文件扩展名 | `.sp` | `.scs` |
| 仿真器声明 | 无 | `simulator lang=spectre insensitive=yes` |
| 引入 DUT | `.include "./5T_OTA.cir"` | `include "./5t_ota/5t_ota.cir"` |
| 电压源 | `VDD vdd 0 DC 0.9` | `VDD (vdd 0) vsource type=dc dc=0.9` |
| 电容 | `CL vout 0 500f` | `CL (vout 0) capacitor c=500f` |
| 电阻 | `Rfb vout vinn 1G` | `Rfb (vout vin) resistor r=1G` |
| 工作点 | `.op` | `op1 dc oppoint=rawfile` |
| AC 扫描 | `.ac dec 20 1 1g` | `ac1 ac start=1 stop=1G dec=20` |
| 瞬态 | `.tran 10p 100n` | `tran1 tran stop=100n step=10p` |
| 温度 | `.temp 27` | 默认 27°C；在分析语句中加 `temp=27` |
| 测量 | `.measure ac gain_dc find vdb(vout) at=1k` | 不支持；由 Python 从 PSF 数据提取 |
| 注释 | `*` 开头 | `//` 开头 |
| 结束 | `.end` | 不需要 |

## 7. 运行命令

```bash
# 输出 PSF ASCII 可读文本（推荐用于调试）
spectre -64 tb_ota_ac.scs -format psfascii -raw ./psf

# 输出 PSF 二进制（默认，用于生产环境）
spectre -64 tb_ota_ac.scs -raw ./psf
```

**输出文件：**

| PSF 文件 | 内容 |
|---------|------|
| `psf/op1.dc` | DC 节点电压 |
| `psf/opinfo.info` | 所有晶体管工作点（vgs/vds/vth/vdsat/ids/gm/gds） |
| `psf/ac1.ac` | AC 复数结果（每频率点的 vout 实部/虚部 → 可提取增益 dB 和相位） |
| `psf/tran1.tran` | 瞬态波形数据 |
