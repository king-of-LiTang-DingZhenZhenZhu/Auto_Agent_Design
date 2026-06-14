---
paths:
  - "**/*.cir"
---

# Spectre 电路网表 (.cir) 脚本编写规范

适用于 Spectre 仿真器的 `.cir` 子电路网表编写规范。
`.cir` 文件只描述电路的拓扑结构和可调参数，不进行仿真。电路封装为子电路后，在仿真 testbench（`.scs`）中通过 `include` 调用。

## 1. 总体原则

- **Spectre native syntax**：文件头必须声明 `simulator lang=spectre insensitive=yes`，全程使用 Spectre 原生语法。
- **模块化设计**：被测电路（DUT）封装为子电路（`subckt ... ends`），可调参数用 `parameters` 声明，便于优化器自动修改。
- **高可读性**：使用 `//` 注释、空行和统一命名规范。
- **PDK 兼容**：严格遵循 TSMC N28 PDK 的模型名称和参数范围。

## 2. 命名规范

### 节点命名

| 节点类型 | 命名 | 说明 |
|---------|------|------|
| 电源/地 | `vdd`, `vss` 或 `0` | 全局电源，地节点固定为 `0` |
| 偏置 | `vbias`, `vbp`, `vbn` | 偏置电流/电压 |
| 差分输入 | `vip`, `vin` | 差分对输入 |
| 内部节点 | `ntail`（尾电流源漏极）、`vx1`（第一级输出）、`vout`（最终输出） | 带物理意义 |

### 器件命名

| 器件类型 | 命名规则 | 示例 |
|---------|---------|------|
| NMOS | `M` + `N` + 功能/编号 | `MN1`, `MNTAIL` |
| PMOS | `M` + `P` + 功能/编号 | `MP1`, `MPCM` |
| 电容 | `C` + 功能 | `Cc`（补偿电容）, `CL`（负载电容） |
| 电阻 | `R` + 功能 | `Rz`（调零电阻） |

## 3. 子电路编写规范

### 3.1 文件结构（从上到下）

```
1. 文件头注释（// 电路名称、工艺、关键参数）
2. simulator lang=spectre insensitive=yes
3. include PDK 模型文件（section=top_tt）
4. parameters 可调参数声明
5. subckt ... ends 子电路定义
```

### 3.2 必须遵循的规则

```
- simulator lang=spectre insensitive=yes 必须是第一条非注释语句
- include PDK 模型文件，使用 section= 指定工艺角
- 所有可调参数用 parameters 声明（不加引号，直接写值）
- 核心电路封装在 subckt ... ends 中
- NMOS model = nch_mac，PMOS model = pch_mac
- NMOS bulk → 0，PMOS bulk → vdd
- 每个晶体管必须写 nf=1（系统自动更新 finger 数量）
- W 参数代表总有效宽度（系统自动拆分为 W_finger × nf）
- 端口顺序：(d g s b)，与 HSPICE 不同（HSPICE 也是 D G S B，但需确认一致）
- 使用 // 注释，禁止使用 *（HSPICE）或 # 注释
- W/L 最小单位 10nm，最大单 finger 宽度 3μm，最大沟长 1μm
```

### 3.3 参数声明

`parameters` 声明的参数将作为优化变量。命名建议：

| 参数前缀 | 含义 | 示例 |
|---------|------|------|
| `W` + 功能 | 晶体管总宽度 | `Wtail`, `Wdp`, `Wcm` |
| `L` + 功能 | 晶体管沟道长度 | `Ltail`, `Ldp`, `Lcm` |
| `I` + 功能 | 偏置电流 | `Ibias` |
| `C` + 功能 | 电容值 | `Cc`（补偿电容） |
| `R` + 功能 | 电阻值 | `Rz`（调零电阻） |

尺寸参数使用 SI 单位后缀：`u`（μm）、`n`（nm）、`p`（pF）、`f`（fF）、`k`（kΩ）。

```spectre
parameters VDD=1.1 VBIAS=0.5
parameters Wtail=10u Ltail=60n Wdp=5u Ldp=60n Wcm=8u Lcm=100n
```

### 3.4 示例：5T OTA

```spectre
// <5T_OTA>.cir — 5T OTA 子电路
// TSMC 28nm CLN28HPC+ | VDD=1.1V

simulator lang=spectre insensitive=yes

include "/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_tt

parameters Wtail=10u Ltail=60n Wdp=5u Ldp=60n Wcm=8u Lcm=100n

subckt ota_5t (vip vin vout vdd vss)
// --- Tail current source ---
Mtail (ntail vbias vss vss) nch_mac w=Wtail l=Ltail nf=1
// --- Differential pair ---
Mdp1 (vx1 vip ntail vss) nch_mac w=Wdp l=Ldp nf=1
Mdp2 (vout vin ntail vss) nch_mac w=Wdp l=Ldp nf=1
// --- Active load (current mirror) ---
Mcm1 (vx1 vx1 vdd vdd) pch_mac w=Wcm l=Lcm nf=1
Mcm2 (vout vx1 vdd vdd) pch_mac w=Wcm l=Lcm nf=1
ends ota_5t
```

> **注意**：参数值不加引号（`w=Wtail` 而非 `W='Wtail'`），这是 Spectre native syntax 与 HSPICE 的关键区别。

## 4. 与 HSPICE 语法的关键区别

| 项目 | HSPICE（旧） | Spectre（新） |
|------|-------------|--------------|
| 仿真器声明 | 无（默认 SPICE） | `simulator lang=spectre insensitive=yes` |
| 工艺库引入 | `.lib '/path/to/model' TOP_TT` | `include "/path/to/model" section=top_tt` |
| 可选参数 | `.options redefinedparams=ignore` | 不需要 |
| 参数声明 | `.param Wtail=10u` | `parameters Wtail=10u` |
| 子电路 | `.subckt ... .ends` | `subckt ... ends` |
| 注释 | `*` 开头 | `//` 开头 |
| 参数引用 | `W='Wtail'`（引号） | `w=Wtail`（无引号） |
| 地节点 | `gnd` 或 `0` | `0`（推荐） |

## 5. PDK 约束

详见 [Agent_LLM_BO/circuit_agent/knowledge_base/pdk_constraints.md]，关键参数范围：

| 参数 | 最小值 | 最大值 | 备注 |
|------|--------|--------|------|
| L (沟道长度) | 30nm | 1μm | 模拟推荐 ≥ 60nm |
| W (finger 宽度) | 100nm | 3μm | 每 finger |
| nf (finger 数) | 1 | 64 | 系统自动管理 |
| M (multiplier) | 1 | 32 | 系统自动管理 |

**有效宽度** = W × nf × M

> **注意**：不要在 `.cir` 文件中手动设置 `nf` 或 `M` 参数，系统会在优化过程中自动管理。声明晶体管时固定写 `nf=1`。
