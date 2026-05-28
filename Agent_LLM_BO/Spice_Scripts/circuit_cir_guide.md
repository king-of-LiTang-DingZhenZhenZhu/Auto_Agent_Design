# 电路网表 (.cir) 脚本编写规范

适用于 Spectre 仿真器的 `.cir` 子电路网表编写规范。
.cir 文件只描述电路的拓扑结构和参数，不进行仿真，描述的电路会被封装起来，在仿真脚本中调用
## 1. 总体原则

- **模块化设计**：被测电路（DUT）封装为子电路（`.subckt`），可调参数用 `.param` 声明，便于优化器自动修改。
- **高可读性**：善用注释、空行和统一的命名规范。
- **PDK 兼容**：严格遵循 TSMC N28 PDK 的模型名称和参数范围。

## 2. 命名规范

### 节点命名
| 节点类型 | 命名 | 说明 |
|---------|------|------|
| 电源/地 | `vdd`, `vss` 或 `gnd` | 全局电源 |
| 偏置 | `ibias`, `vbp`, `vbn` | 偏置电流/电压 |
| 差分输入 | `vinp`, `vinn` | 差分对输入 |
| 内部节点 | `ntail`（尾电流源漏极）、`vx1`（第一级输出）、`vout`（最终输出） | 带物理意义 |

### 器件命名
| 器件类型 | 命名规则 | 示例 |
|---------|---------|------|
| NMOS | `M` + `N` + 功能/编号 | `MN1`, `MNTAIL` |
| PMOS | `M` + `P` + 功能/编号 | `MP1`, `MPCM` |
| 电容 | `C` + 功能 | `Cc`（补偿电容）, `CL`（负载电容） |
| 电阻 | `R` + 功能 | `Rz`（调零电阻） |

## 3. 子电路编写规范

### 必须遵循的规则

```
- .lib 语句在顶部：.lib '/PDKS/TSMC28nm/models/hspice/toplevel.l' TOP_TT
- 建议添加 .options redefinedparams=ignore（忽略模型文件中重复定义的参数）
- 所有可调参数用 .param 声明
- 核心电路封装在 .subckt ... .ends 中
- NMOS model = nch_mac，PMOS model = pch_mac
- NMOS bulk → gnd! (或 vss)，PMOS bulk → vdd!
- 每个晶体管必须写 nf=1（系统自动更新 finger 数量）
- W 参数代表总有效宽度（系统自动拆分为 W_finger × nf）
- 端口顺序：输入 → 输出 → 偏置 → 电源 → 地
```

### 参数声明

`.param` 声明的参数将作为优化变量。命名建议：
- `W` + 功能（如 `Wtail`, `Wdp`, `Wcm`）：晶体管总宽度
- `L` + 功能（如 `Ltail`, `Ldp`, `Lcm`）：晶体管沟道长度
- 其他可调参数：`Cc`（补偿电容）, `Rz`（调零电阻）, `Ibias`（偏置电流）

### 示例：5T OTA

```spice
* <5T_OTA>.cir -- 5T OTA
.lib '/PDKS/TSMC28nm/models/hspice/toplevel.l' TOP_TT
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

## 4. PDK 约束

详见 [knowledge_base/pdk_constraints.md](../circuit_agent/knowledge_base/pdk_constraints.md)，关键参数范围：

| 参数 | 最小值 | 最大值 | 备注 |
|------|--------|--------|------|
| L (沟道长度) | 30nm | 1μm | 模拟推荐 ≥ 60nm |
| W (finger 宽度) | 100nm | 3μm | 每 finger |
| nf (finger 数) | 1 | 64 | 系统自动管理 |
| M (multiplier) | 1 | 32 | 系统自动管理 |

**有效宽度** = W × nf × M

> **注意**：不要在 `.cir` 文件中手动设置 `nf` 或 `M` 参数，系统会在优化过程中自动管理。只需声明晶体管时写 `nf=1`。
