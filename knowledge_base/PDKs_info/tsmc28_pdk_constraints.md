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
| `io_1p8` | `1.62–1.98 V` | `nch_25ud18_mac` | `pch_25ud18_mac` | `300 nm` |

其他当前配置：

- PVT section：`tt=top_tt`、`ss=top_ss`、`ff=top_ff`。
- PVT 温度：`-40°C`、`27°C`、`125°C`。
- 单指宽度：`0.2–2.6 μm`。
- 特殊模型：PNP 使用 `pnp5`，poly resistor 模型角色使用 `rupolym`。
- Virtuoso technology library：`tsmcN28`。

## 当前限制

- `io_1p8` 尚无对应的 gm/Id lookup 数据，相关拓扑应使用物理 W/L 参数优化。
- `passive_devices` 和 `passive_role_map` 当前为空；真实 R/C PCell 名称、CDF 参数、
  合法尺寸和 evaluator/LUT 补齐前，不得宣称完成 PDK R/C 映射。
- `nf`、`m`、电流密度、可靠性和版图规则应从实际 PDK/CDF/deck 验证，不能使用
  本文档中未记录的经验值代替。

## 验证

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
PDK_PROFILE_FILE=../../PDK_Info_Json/TSMC_28nm_Information.json \
  python pdk_profiles.py --validate --require-gmid --require-virtuoso
```

在真实 Cadence 服务器上追加 `--check-files`，验证模型路径和 OA library 路径。
