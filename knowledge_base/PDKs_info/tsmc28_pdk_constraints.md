# TSMC28 项目约束快照

本文档只说明当前项目采用的 TSMC28 约束，不重复完整的 profile 字段、加载规则和
R/C 映射接口。机器可读配置以
`PDK_Info_Json/TSMC_28nm_Information.json` 为准；通用说明见
`knowledge_base/PDKs_info/pdk_profiles.md`。

这些数值是当前项目的设计边界，不应解读为 foundry 手册中全部可用器件或全部
sign-off 规则。

## 电压域与模型

| 电压域 | 电源范围 | NMOS | PMOS | 最小沟道长度 |
|---|---:|---|---|---:|
| `core_0p9` | `0.9–1.1 V` | `nch_mac` / `nch_lvt_mac` | `pch_mac` / `pch_lvt_mac` | `30 nm`（profile 全局边界） |
| `io_1p8` | `1.62–1.98 V` | `nch_18_mac` | `pch_18_mac` | `150 nm` |

其他当前配置：

- PVT section：`tt=top_tt`、`ss=top_ss`、`ff=top_ff`、`fs=top_fs`、`sf=top_sf`。
- PVT 温度：`-40°C`、`27°C`、`125°C`。
- core 最小沟道长度：`30 nm`；单指宽度：`0.1–2.7 μm`。
- 特殊模型：PNP 使用 `pnp5`，poly resistor 模型角色使用 `rupolym`。
- Virtuoso technology library：`tsmcN28`。

## 当前限制

- `io_1p8` 尚无对应的 gm/Id lookup 数据，相关拓扑应使用物理 W/L 参数优化。
- `io_1p8` 的 `nch_18_mac/pch_18_mac` 使用主 1.8 V model bundle，OA CDF 默认
  `L=150 nm`、单指 `W=320 nm`；当前没有对应 gm/Id lookup 数据。
- `rupolym` 已按 OA CDF 接入解析映射，但它只提供几何搜索初值，必须经过映射后
  nominal 仿真。
- `nf`、`m`、电流密度、可靠性和版图规则应从实际 PDK/CDF/deck 验证，不能使用
  本文档中未记录的经验值代替。

## `cfmom_2t` 电容映射

`tsmcN28/cfmom_2t` 已完成 CDF、Spectre 和 PVT characterization，并以
`finger_mom_2t` 登记到 `passive_devices`。端口顺序为 `PLUS, MINUS`。

| CDF 参数 | Spectre 参数 | 合法范围 |
|---|---|---|
| `Wfinger` | `w` | `0.05–0.075 um`，步进 `0.005 um` |
| `Sfinger` | `s` | `0.05–0.24 um`，步进 `0.005 um` |
| `Lfinger` | `lr` | `1–40 um`，步进 `0.01 um` |
| `Nfinger` | `nr` | `6–288`，偶数，步进 `2` |
| `StartMn` | `stm` | `1–6` |
| `StopMn` | `spm` | `3–8` |
| `m` | `multi` | 当前 LUT 固定为 `1` |

金属堆叠必须至少包含 3 层，即 `spm-stm+1 >= 3`。自动映射使用高密度采样族
`w=s=0.05 um, stm=1, spm=8`；完整合法范围保留在 LUT 元数据中，不能把采样族
误解为 PCell 的唯一合法几何。

characterization 条件与结果：

- 方法：`imag(I)/(2*pi*f)`，`1 MHz`、`0 V` DC bias。
- PVT：`tt/ss/ff/fs/sf × -40/27/125°C`。
- LUT：`PDK_Info_Json/characterization/tsmc28_cfmom_2t_lut.json`，1368 点。
- TT/27°C 单元覆盖：`9.471 fF–8.363 pF`；最大相邻中点误差约 `1.19%`。
- 相对 TT/27°C 的 PVT 范围约为 `-25.35%–+25.92%`。
- `max_parallel_units=16`，nominal 目标可覆盖到约 `133.8 pF`；单 PCell 满足
  2% 容差时优先使用单元，只有超出单元范围时才使用并联阵列。

## 验证

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
PDK_PROFILE_FILE=../../PDK_Info_Json/TSMC_28nm_Information.json \
  python pdk_profiles.py --validate --require-gmid --require-virtuoso
```

在真实 Cadence 服务器上追加 `--check-files`，验证模型路径和 OA library 路径。
