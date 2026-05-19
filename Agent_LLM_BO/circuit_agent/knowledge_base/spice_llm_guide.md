# SPICE 脚本编写指南（面向 LLM）

> 工艺节点：**TSMC N28**  
> 适用仿真器：Spectre / HSPICE / ngspice（语法以 SPICE 为主）

---

## 1. 工艺与器件命名规范

### 1.1 晶体管模型名

| 类型 | 模型名 | 说明 |
|------|--------|------|
| NMOS | `nch_mac` | tsmcN28 标准 NMOS |
| PMOS | `pch_mac` | tsmcN28 标准 PMOS |

> ⚠️ **严禁**使用通用名称（如 `nmos`、`pmos`、`NMOS4`），必须使用上表中的模型名。

### 1.2 端口顺序

SPICE 中 MOSFET 端口顺序为：

```
M<name> <drain> <gate> <source> <bulk> <model> [参数]
```

- **NMOS**：bulk 接 `gnd!`（或最低电位节点）
- **PMOS**：bulk 接 `vdd!`（或最高电位节点）

---

## 2. 文件结构模板

```spice
* ============================================================
* 电路名称：<circuit_name>
* 工艺：tsmcN28
* 作者：<author>
* 日期：<date>
* ============================================================

* --- 包含工艺库 ---
.lib '/path/to/tsmc28nm/models/spectre/tt.lib' tt

* --- 全局节点声明 ---
.global vdd! gnd!

* --- 顶层子电路定义 ---
.subckt <circuit_name> <port_list>
* ... 内部元件 ...
.ends <circuit_name>

* --- 顶层激励（TB） ---
Vdd vdd! gnd! DC 0.9      $ 28nm 典型电源电压
* ... 其余激励 ...

* --- 仿真控制 ---
.op
* 或 .tran / .ac / .dc
.end
```

---

## 3. 电源电压

| 电源类型 | 典型值 |
|----------|--------|
| 核心电源 VDD | **0.9 V** |
| IO 电源 | 1.8 V / 2.5 V（视具体 IO 库） |

---

## 4. 晶体管实例化规范

### 4.1 NMOS 示例

```spice
MN1  drain_n  gate_n  gnd!  gnd!  nch_mac  w=1u  l=30n  nf=4  m=1
```

### 4.2 PMOS 示例

```spice
MP1  drain_p  gate_p  vdd!  vdd!  pch_mac  w=2u  l=30n  nf=4  m=1
```

### 4.3 常用参数说明

| 参数 | 含义 | 典型范围（N28） |
|------|------|----------------|
| `w` | 单 finger 宽度 | 100n ~ 3 µm |
| `l` | 沟道长度 | **30n**（最小）~ 1u |
| `nf` | finger 数量 | 1, 2, 4, 8, ... |
| `m` | 并联倍数（multiplier） | 1, 2, 4, ... |

> 等效总宽度 = `w × nf × m`

---

## 5. 常用基础单元示例

### 5.1 CMOS 反相器

```spice
.subckt inv IN OUT VDD VSS
MP0  OUT  IN  VDD  VDD  pch_mac  w=200n  l=30n  nf=2  m=1
MN0  OUT  IN  VSS  VSS  nch_mac  w=100n  l=30n  nf=2  m=1
.ends inv
```

### 5.2 差分对（NMOS 输入）

```spice
.subckt diff_pair INP INN TAIL OUTP OUTN VDD VSS
* 输入差分对
MN_P  OUTP  INP  TAIL  VSS  nch_mac  w=500n  l=60n  nf=4  m=1
MN_N  OUTN  INN  TAIL  VSS  nch_mac  w=500n  l=60n  nf=4  m=1
* PMOS 电流镜负载
MP_P  OUTP  OUTP  VDD  VDD  pch_mac  w=1u  l=60n  nf=4  m=1
MP_N  OUTN  OUTP  VDD  VDD  pch_mac  w=1u  l=60n  nf=4  m=1
.ends diff_pair
```

### 5.3 电流镜

```spice
.subckt cmirror REF OUT VDD VSS
* 参考支路
MP_REF  REF  REF  VDD  VDD  pch_mac  w=500n  l=60n  nf=2  m=1
* 镜像支路（1:1）
MP_OUT  OUT  REF  VDD  VDD  pch_mac  w=500n  l=60n  nf=2  m=1
.ends cmirror
```

---

## 6. 激励源写法

```spice
* DC 电压源
Vdd   vdd!  gnd!  DC  0.9

* 脉冲信号（用于瞬态仿真）
Vin   IN    gnd!  PULSE(0 0.9 0 50p 50p 500p 1n)
*                       V0 V1 td tr  tf  pw   period

* 正弦信号（用于 AC 或瞬态）
Vin   IN    gnd!  SIN(0.45 0.1 1G)
*                      offset amp  freq

* AC 小信号源（用于 .ac 分析）
Vin   IN    gnd!  DC 0.45 AC 1
```

---

## 7. 仿真控制语句

```spice
* 静态工作点
.op

* 瞬态仿真：步长 1ps，总时长 10ns
.tran 10p 10n

* AC 仿真：10Hz ~ 100GHz，每十倍频 20 点
.ac dec 20 10 1G

* DC 扫描：VIN 从 0 到 0.9V，步长 1mV
.dc Vin 0 0.9 10m

* 蒙特卡洛（调用工艺库支持时）
.mc 100 ...

* 参数扫描
.param W_N = 100n
.step param W_N list 100n 200n 400n
```

---

## 8. 输出与测量

```spice
* 保存节点电压
.save V(OUT) V(IN) V(vdd!)

* 测量语句示例
.meas tran tphl TRIG V(IN) VAL=0.45 RISE=1
+                TARG V(OUT) VAL=0.45 FALL=1

.meas ac GBW WHEN VDB(OUT)=0
.meas ac PM  FIND VP(OUT) WHEN VDB(OUT)=0
```

---

## 9. LLM 输出清单（每次生成 SPICE 脚本前请确认）

- [ ] NMOS 使用 `nch_mac`，PMOS 使用 `pch_mac`
- [ ] 端口顺序：D G S B Model
- [ ] NMOS bulk 接 `gnd!`，PMOS bulk 接 `vdd!`
- [ ] VDD = 0.9 V（核心电路）
- [ ] 最小沟道长度 L ≥ 28 nm
- [ ] 已包含 `.lib` 工艺库引用（路径由用户指定）
- [ ] 子电路用 `.subckt` / `.ends` 封装
- [ ] 文件以 `.end` 结尾

---

## 10. 常见错误与避免方法

| 错误 | 正确做法 |
|------|----------|
| 使用 `nmos` / `pmos` 模型名 | 使用 `nch_mac` / `pch_mac` |
| PMOS bulk 接 `gnd!` | PMOS bulk 必须接 `vdd!` |
| L 小于 30 nm | L 最小设为 `30n` |
| VDD 设为 1.8 V（核心电路） | 核心电路 VDD = `0.9` |
| 忘记 `.global vdd! gnd!` | 文件头部声明全局节点 |
| 子电路未用 `.ends` 关闭 | 每个 `.subckt` 对应一个 `.ends` |

