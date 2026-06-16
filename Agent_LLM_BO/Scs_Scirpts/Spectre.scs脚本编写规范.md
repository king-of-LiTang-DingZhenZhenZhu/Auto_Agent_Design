# Spectre .scs 脚本编写规范

适用工艺：**TSMC 28nm (tsmcN28)**
适用仿真器：**Cadence Spectre**
晶体管模型：`nch_mac`（NMOS）/ `pch_mac`（PMOS）

---

## 1. 文件头（File Header）

每个脚本必须以注释块开头，说明电路名称、工艺、关键设计参数：

```spectre
// <电路名称>
// TSMC 28nm CLN28HPC+ | VDD=<电压> | <其他关键参数>
```

---

## 2. 仿真器声明

文件第一条非注释语句必须是：

```spectre
simulator lang=spectre insensitive=yes
```

- `insensitive=yes`：节点名与器件名大小写不敏感，避免歧义。

---

## 3. 工艺库 include

紧跟仿真器声明之后，include 工艺模型文件，并指定仿真角（section）：

```spectre
include "/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_tt
```

常用 section：

| Section    | 含义           |
|------------|----------------|
| `top_tt`   | 典型角（TT）   |
| `top_ff`   | 快角（FF）     |
| `top_ss`   | 慢角（SS）     |
| `top_fs`   | 快N慢P（FS）   |
| `top_sf`   | 慢N快P（SF）   |

> ⚠️ 不得省略 include 语句；若需多角仿真，改用 altergroup。

---

## 4. 参数定义（parameters）

### 4.1 基本规则

- 使用 `parameters` 关键字，每行定义一组相关参数。
- 尺寸参数统一使用 SI 单位后缀：`u`（μm）、`n`（nm）、`p`（pF）、`f`（fF）等。
- tsmc28工艺下单 finger 的尺寸有如下限制：宽度不超过 2.7um，长度不超过 1um，如果需要更大的总宽度，则考虑增大 nf 或 M.
- 参数名语义化，区分器件类型：

```spectre
parameters VDD=1.1 VBIAS=0.5    //VDD 取值在 0.8 - 1.1 都可以

parameters Wcm=2u   Lcm=100n    // Current mirror
parameters Wdp=3u   Ldp=100n    // Differential pair
parameters Wtail=3u Ltail=200n  // Tail current source
```

### 4.2 命名约定

| 前缀 | 含义 |
|------|------|
| `W`  | 晶体管宽度 |
| `L`  | 晶体管长度 |
| `I`  | 电流偏置值 |
| `C`  | 电容值 |
| `R`  | 电阻值 |

---

## 5. 电源与激励（Sources）

### 5.1 格式

```spectre
<InstanceName>  (<pos_node> <neg_node>)  vsource  type=<dc|sin|pulse|ac>  <params>
```

- 器件名以大写字母开头（`VDD`、`VIP` 等）。
- 负极通常为地节点 `0`。

### 5.2 常用电源写法

```spectre
// 直流电源
VDD   (vdd 0)   vsource type=dc dc=VDD

// 直流偏置
VBIAS (vbias 0) vsource type=dc dc=VBIAS

// 差分小信号（AC 分析用）
VIP   (vip 0)   vsource type=dc dc=0.3 mag=0.5
VIN   (vin 0)   vsource type=dc dc=0.3 mag=-0.5

// 正弦激励（Transient 用）
VIN   (vin 0)   vsource type=sin sinedc=0.55 ampl=0.1 freq=1Meg

// 脉冲激励
VPULSE (vin 0)  vsource type=pulse val0=0 val1=VDD delay=1n rise=100p fall=100p \
                width=5n period=10n
```

---

## 6. 晶体管实例（Transistors）

### 6.1 格式

```spectre
<Name>  (<drain> <gate> <source> <bulk>)  <model>  w=<W>  l=<L>  nf=<NF>
```

### 6.2 模型名称

| 类型 | 模型名    |
|------|-----------|
| NMOS | `nch_mac` |
| PMOS | `pch_mac` |

> ⚠️ 不得使用其他模型名（如 `nmos`、`pmos`、`nch`、`pch`）。

### 6.3 Bulk 连接规则

| 类型 | Bulk 连接 |
|------|-----------|
| NMOS | `0`（地）  |
| PMOS | `vdd`（电源） |

### 6.4 示例

```spectre
// PMOS：tail current source（source/bulk → vdd）
M5 (tail vbias vdd vdd)  pch_mac w=Wtail l=Ltail nf=1

// PMOS：differential pair
M1 (lout vip  tail tail) pch_mac w=Wdp l=Ldp nf=1
M2 (vout vin  tail tail) pch_mac w=Wdp l=Ldp nf=1

// NMOS：current mirror load（source/bulk → 0）
M3 (lout lout 0 0) nch_mac w=Wcm l=Lcm nf=1
M4 (vout lout 0 0) nch_mac w=Wcm l=Lcm nf=1
```

### 6.5 多 finger（nf）

当 `nf > 1` 时，总宽度 = `w × nf`，写法：

```spectre
M1 (out in vdd vdd) pch_mac w=1u l=100n nf=4
```

---

## 7. 无源器件

```spectre
// 电容
CL   (vout 0)    capacitor c=1p
CF   (out in)    capacitor c=100f

// 电阻
Rbias (a b)      resistor  r=10k

// 理想电感（慎用）
L1   (a b)       inductor  l=1n
```

---

## 8. 仿真语句（Analyses）

### 8.1 工作点（OP）

```spectre
op1 dc oppoint=rawfile
```

- `oppoint=rawfile`：将工作点数据写入 rawfile，便于后处理。
- OP 分析通常放在所有 Analysis 的**第一条**。

### 8.2 AC 分析

```spectre
ac1 ac start=1k stop=10G dec=100
```

| 参数  | 含义           |
|-------|----------------|
| `start` | 起始频率     |
| `stop`  | 终止频率     |
| `dec`   | 每十倍频点数 |

### 8.3 瞬态分析

```spectre
tran1 tran stop=100n step=10p
```

### 8.4 DC 扫描

```spectre
// 单变量扫描
dc1 dc dev=VIN param=dc start=0 stop=VDD step=10m

// 参数扫描（sweep）
sweep1 sweep param=Wcm start=1u stop=8u step=1u {
    op_sw dc oppoint=rawfile
}
```

### 8.5 噪声分析

```spectre
noise1 noise start=1k stop=10G dec=100 oprobe=Vout iprobe=VIN
```

---

## 9. 结果输出策略

### 9.1 核心机制说明

Spectre 原生不支持在 `.scs` 内直接输出格式化文本表格。结果的导出依赖**两个层次**的配合：

| 层次 | 负责内容 |
|------|----------|
| `.scs` 脚本 | 控制 **保存哪些量**（save / info）和**原始数据格式**（PSF binary 或 PSF ASCII） |
| 命令行 / 后处理脚本 | 控制**如何呈现**（读取 PSF 文件，格式化为可读文本） |

> 因此，`.scs` 脚本的职责是"存对数据"，读取与格式化在脚本外完成。

---

### 9.2 保存节点电压（所有仿真通用）

```spectre
save vout          // 单节点
save vout vin lout // 多节点
```

---

### 9.3 DC 工作点：保存晶体管内部变量

Spectre BSIM 模型在工作点分析后提供以下器件变量，可通过 `save` 语句直接指定：

| 变量       | 含义                        |
|------------|-----------------------------|
| `vgs`      | 栅源电压                    |
| `vds`      | 漏源电压                    |
| `vth`      | 阈值电压（含 body/drain 效应）|
| `vdsat`    | 饱和电压（速度饱和修正后）   |
| `ids`      | 漏极电流                    |
| `gm`       | 跨导                        |
| `gds`      | 输出电导                    |
| `vod`      | 过驱动电压 Vgs - Vth         |

**推荐写法**（逐管列出，便于 agent 后处理时按名索引）：

```spectre
// ------- 工作点变量保存 -------
save M1:vgs M1:vds M1:vth M1:vdsat M1:ids M1:gm M1:gds
save M2:vgs M2:vds M2:vth M2:vdsat M2:ids M2:gm M2:gds
save M3:vgs M3:vds M3:vth M3:vdsat M3:ids M3:gm M3:gds
save M4:vgs M4:vds M4:vth M4:vdsat M4:ids M4:gm M4:gds
save M5:vgs M5:vds M5:vth M5:vdsat M5:ids M5:gm M5:gds
```

**或者用 `info` 语句一次性导出所有器件的所有工作点参数**（推荐，更完整）：

```spectre
op1    dc  oppoint=rawfile
opinfo info what=oppoint where=rawfile
```

`info what=oppoint` 会把每个器件的完整工作点（vgs/vds/vth/vdsat/gm/gds/…）全部写入 rawfile，无需逐变量枚举。

---

### 9.4 AC 分析：保存增益与相位

```spectre
ac1 ac start=1k stop=10G dec=100
save vout          // 保存输出节点复数幅值（magnitude + phase 均包含在内）
```

Spectre AC 结果以**复数**形式存储，magnitude（dB）和 phase（°）在 PSF 文件中均可读取，无需额外声明。

---

### 9.5 输出格式：PSF ASCII（推荐）

默认输出为二进制 PSF，**运行时加 `-format psfascii` 参数**，结果即为可直接阅读的文本：

```bash
spectre -64 circuit.scs -format psfascii -raw ./psf
```

输出文件位置（以上面 5T-OTA 为例）：

| 文件              | 内容                              |
|-------------------|-----------------------------------|
| `psf/op1.dc`      | DC 节点电压                       |
| `psf/opinfo.info` | 所有晶体管工作点（vgs/vth/vdsat…） |
| `psf/ac1.ac`      | AC 复数结果（每频率点 vout 实虚部）|

PSF ASCII 文件结构示意（`opinfo.info`）：

```
...
VALUE
"M1" (
  vgs  -662.5m
  vds  -923.1m
  vth  -477.7m
  vdsat -144.0m
  ids   -97.7u
  gm     1.04m
  gds   39.8u
)
...
```

---

### 9.6 Save 语句补充规则

```spectre
save vout              // 节点电压
save M1:ids            // 单个器件电流
save M1:vgs M1:vds     // 器件内部电压（需 op 或 tran 分析）
```

> 若需保存所有节点：`save all`（大电路慎用，数据量大）。
> 若只需器件工作点，优先用 `info what=oppoint`，比逐行 save 更简洁完整。

---

## 10. 整体结构顺序

```
1. 文件头注释
2. simulator lang=spectre insensitive=yes
3. include（工艺库）
4. parameters（全局参数）
5. 电源与激励
6. 晶体管
7. 无源器件
8. 仿真语句（op → info → dc/ac/tran → noise）
9. save（节点电压 + 器件变量）
```

---

## 11. 注释规范

- 使用 `//` 单行注释，**不使用** `/* */`。
- 分区注释使用 `// ------- 标题 -------` 格式。
- 重要参数含义在行尾用注释说明。

```spectre
// ------- 差分对 -------
M1 (lout vip tail tail) pch_mac w=Wdp l=Ldp nf=1   // 正输入管
M2 (vout vin  tail tail) pch_mac w=Wdp l=Ldp nf=1  // 负输入管
```

---

## 12. 完整示例模板

```spectre
// Five-Transistor OTA
// TSMC 28nm CLN28HPC+ | VDD=1.1V | Vbias=500mV

simulator lang=spectre insensitive=yes

include "/mnt/hgfs/Share/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_tt

// ------- 参数 -------
parameters VDD=1.1 VBIAS=0.5
parameters Wcm=2u  Lcm=100n
parameters Wdp=4u  Ldp=100n
parameters Wtail=4u Ltail=200n

// ------- 电源 & 偏置 -------
VDD   (vdd 0)   vsource type=dc dc=VDD
VBIAS (vbias 0) vsource type=dc dc=VBIAS
VIP   (vip 0)   vsource type=dc dc=0.55 mag=0.5
VIN   (vin 0)   vsource type=dc dc=0.55 mag=-0.5

// ------- 晶体管 -------
M5 (tail vbias vdd  vdd)  pch_mac w=Wtail l=Ltail nf=1  // Tail
M1 (lout vip   tail tail) pch_mac w=Wdp   l=Ldp   nf=1  // Diff pair +
M2 (vout vin   tail tail) pch_mac w=Wdp   l=Ldp   nf=1  // Diff pair -
M3 (lout lout  0    0)    nch_mac w=Wcm   l=Lcm   nf=1  // Mirror diode
M4 (vout lout  0    0)    nch_mac w=Wcm   l=Lcm   nf=1  // Mirror output

// ------- 负载电容 -------
CL (vout 0) capacitor c=1p

// ------- 仿真 -------
op1    dc   oppoint=rawfile
opinfo info what=oppoint where=rawfile  // 导出所有晶体管 vgs/vds/vth/vdsat/gm/gds

ac1    ac   start=1k stop=10G dec=100

// ------- 保存节点 -------
save vout lout
```

**运行命令（输出 PSF ASCII 可读文本）：**

```bash
spectre -64 5T_OTA.scs -format psfascii -raw ./psf
```

**输出文件：**

| 文件 | 内容 |
|------|------|
| `psf/op1.dc` | DC 节点电压 |
| `psf/opinfo.info` | 每个晶体管的 vgs / vds / vth / vdsat / ids / gm / gds |
| `psf/ac1.ac` | 每个频率点的 vout 复数值（可提取 dB 增益和相位）|

---

## 13. 常见错误检查清单

| 检查项 | 正确 | 错误 |
|--------|------|------|
| 仿真器声明 | `simulator lang=spectre` | 缺省或写成 `lang=spice` |
| NMOS 模型 | `nch_mac` | `nch`、`nmos`、`n` |
| PMOS 模型 | `pch_mac` | `pch`、`pmos`、`p` |
| PMOS bulk | 连接 `vdd` | 连接 `0` |
| NMOS bulk | 连接 `0` | 连接 `vdd` |
| 端口顺序 | D G S B | G D S B（SPICE 顺序） |
| 注释符号 | `//` | `#`、`*`（SPICE 风格）|
| include 路径 | 绝对路径 | 相对路径（可移植性差）|
| 导出工作点 | `info what=oppoint where=rawfile` | 仅写 `op1 dc oppoint=rawfile`（无 info 则器件变量不输出）|
| 可读文本输出 | 命令行加 `-format psfascii` | 默认 psfbin，二进制不可直接阅读 |
