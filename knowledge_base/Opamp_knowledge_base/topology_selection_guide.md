# Topology Selection Guide

本文档为 LLM 辅助拓扑选择提供参考。每个拓扑有明确的指标能力范围，选择时按以下决策树匹配合适的拓扑。

---

## 可用拓扑

### 5T OTA (Five-Transistor OTA)

- **结构**: PMOS 差分对 + NMOS 电流镜负载（单级）
- **增益**: 25-55 dB
- **带宽**: 最高 ~2 GHz（最小 L 时）
- **相位裕度**: >60°（单极点系统，天然稳定）
- **功耗**: ~100uW - 2mW
- **适用场景**: 中等增益、高速应用；对功耗有要求的场合
- **复杂度**: 1（最简单）
- **升级路径**: two_stage_ota（当增益不足时）

### Two-Stage Miller OTA

- **结构**: 第一级差分输入 + 第二级共源放大 + Miller 补偿
- **增益**: 60-90 dB
- **带宽**: 受 Miller 补偿限制，通常 10-500 MHz
- **相位裕度**: 依赖 Cc/Rz 补偿网络
- **功耗**: 高于 5T OTA（两级偏置电流）
- **适用场景**: 高增益、中等带宽
- **复杂度**: 2
- **升级路径**: folded_cascode（当带宽不足时）
- **偏置设计**: 第一级 NMOS 尾电流管（M5）与第二级 NMOS 负载管（M7）**共用同一偏置 Vb**，减少偏置电路开销。实际设计中只需一个偏置产生电路即可同时偏置两级。`two_stage_ota` 拓扑的端口顺序为 `vip vin vout vb vdd vss`（6 引脚，非 7 引脚）。

### Folded-Cascode Two-Stage Miller OTA

- **结构**: 折叠 Cascode 第一级 + 第二级共源放大 + Miller 补偿
- **增益**: 60-85 dB
- **带宽**: 通常高于五管第一级的 Two-Stage Miller OTA；仍受 Cc/Rz 补偿与负载限制
- **相位裕度**: 依赖 Cc/Rz 补偿网络；折叠节点会引入额外高频非主极点
- **功耗**: 高于普通 two_stage_ota（折叠支路额外消耗电流）
- **适用场景**: 高增益 + 高带宽；普通 two_stage_ota 增益或带宽不足；需要更高第一级输出阻抗/更大第一级增益
- **复杂度**: 3
- **升级路径**: `folded_cascode_two_stage`（需要更高增益或负载驱动时）
- **偏置设计**: `folded_cascode` 是单级结构，端口顺序为 `vip vin vout ibias vdd vss`，折叠高阻节点直接作为 `vout`。

### Folded-Cascode Two-Stage OTA

- **结构**: 单级 folded cascode 后接 PMOS common-source 第二级，并使用 Cc/Rz Miller 补偿
- **拓扑名**: `folded_cascode_two_stage`
- **适用场景**: 单级 folded cascode 增益或负载驱动不足，但尚不需要三级 NMCF
- **升级路径**: `nmcf_three_stage`

### NMCF Three-Stage OTA

- **结构**: PMOS 输入第一级 + NMOS 共源第二级 + PMOS 共源输出级 + Nested Miller 补偿
- **增益**: 75-115 dB
- **带宽**: 通常低于单级/两级高速 OTA，依赖 Cc1/Cc2/Rz1 与负载；适合高增益、较大负载场景
- **相位裕度**: 强依赖 nested Miller 补偿网络，优化时需要同时搜索 Cc1、Cc2、Rz1
- **功耗**: 高于 folded_cascode（三个增益级 + 偏置网络）
- **适用场景**: 极高增益、大负载、两级/折叠 Cascode 优化后仍不达标
- **复杂度**: 4
- **升级路径**: 无（当前最高复杂度）
- **偏置设计**: `nmcf_three_stage` 拓扑端口顺序为 `vip vin vout ibias vdd vss`。外部输入参考电流 `ibias`，内部偏置网络生成 PMOS tail/load 偏置和 NMOS load 偏置。

---

## 选择决策树

```
需求分析 → 
├─ gain ≥ 85 dB 或大负载高增益 → nmcf_three_stage
│
├─ gain ≥ 60 dB → 两级架构
│  ├─ BW > 500 MHz → folded_cascode
│  ├─ 普通 two_stage_ota 优化后带宽不足/第一级增益不足 → folded_cascode
│  ├─ folded_cascode 增益/驱动不足 → folded_cascode_two_stage
│  ├─ folded_cascode_two_stage 仍不足 → nmcf_three_stage
│  └─ BW ≤ 500 MHz 且功耗敏感 → two_stage_ota
│
├─ gain < 60 dB → 5t_ota
│
├─ power < 100 uW → 5t_ota + 亚阈值偏置
│
└─ 默认 → 5t_ota
```

## 选择原则

1. **简单优先**: 在满足指标的前提下，优先选择复杂度最低的拓扑
2. **升级路径**: 如果当前拓扑经过 BO 优化仍不达标，按升级路径自动切换
3. **指标平衡**: `folded_cascode` 是高速单级结构；`folded_cascode_two_stage` 用额外 CS 级和 Miller 补偿换取更高增益与驱动；`nmcf_three_stage` 面向极高增益/负载能力。
4. **PDK 约束**: 所有拓扑严格遵循 TSMC N28 PDK 约束（L≥30nm, W/nf≤2.6um；有效宽度为 W*m）

## 什么时候使用 folded_cascode

优先选择 `folded_cascode` 的情况：

- 目标同时要求 **高增益和高带宽**，例如 gain ≥ 60 dB 且 BW > 500 MHz。
- `two_stage_ota` 已经经过 BO 优化但带宽不足，或 gap 显示第一级增益/输出阻抗成为瓶颈。
- 负载较大但仍需要较高 GBW，需要通过更强第一级和更小/更可控的补偿电容改善速度。
- 用户明确要求折叠 Cascode、低 1/f 噪声 PMOS 输入对，或输入/输出共模需要更灵活的电平分配。

不优先选择 `folded_cascode` 的情况：

- gain < 60 dB：优先 `5t_ota`。
- gain ≥ 60 dB 但 BW ≤ 500 MHz，且功耗/设计复杂度更敏感：优先 `two_stage_ota`。
- power < 100 uW：折叠支路功耗开销较大，通常不合适。

## 什么时候使用 nmcf_three_stage

优先选择 `nmcf_three_stage` 的情况：

- 目标增益非常高，例如 gain ≥ 85 dB，普通 two-stage 或 folded_cascode 余量不足。
- 负载电容较大，同时仍需要较高闭环精度或较强输出驱动。
- `folded_cascode` 优化后主要 gap 仍在 gain、settling 或大负载驱动能力。
- 用户明确要求三级运放、NMCF、Nested Miller compensation 或参考 Leung NMCF 结构。

不优先选择 `nmcf_three_stage` 的情况：

- gain < 75 dB：优先选择复杂度更低的拓扑。
- 极高带宽优先且负载较轻：优先 `5t_ota` 或 `folded_cascode`。
- 功耗预算严格：三级结构偏置支路更多，通常不适合超低功耗目标。
