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

- `io_1p8` 使用独立的 `tsmc28_1p8v_nch18_pch18_gmid_tables.json`；相关拓扑不得使用 core-device gm/Id 数据。
- `io_1p8` 的 `nch_18_mac/pch_18_mac` 使用主 1.8 V model bundle，OA CDF 默认
  最小 `L=150 nm`、单指 `W=320 nm`；当前 gm/Id 表征的最小沟道长度为
  `180 nm`，自动 gm/Id sizing 以 `180 nm` 为下限。
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
| `m` | `multi` | `1–16`；仅在单 PCell 容值范围不足时使用 |

金属堆叠必须至少包含 3 层，即 `spm-stm+1 >= 3`。当前 callback 自动映射固定
`w=s=0.05 um, stm=1, spm=6`，并搜索 `nr/lr`。单 PCell 可覆盖目标时固定
`multi=1`；超出单 PCell 范围时允许 `multi=2–16`。这些是项目选择的映射
族，不是 PCell 的唯一合法几何。该 PCell 的 CDF 显示参数 `c` 不包含 `multi`；
映射器以 `c × multi` 作为 Spectre 中的最终电容值。

characterization 条件与结果：

- 方法：`imag(I)/(2*pi*f)`，`1 MHz`、`0 V` DC bias。
- PVT：`tt/ss/ff/fs/sf × -40/27/125°C`。
- LUT：`PDK_Info_Json/characterization/tsmc28_cfmom_2t_lut.json`，1368 点。
- TT/27°C 单元覆盖：`9.471 fF–8.363 pF`；最大相邻中点误差约 `1.19%`。
- 相对 TT/27°C 的 PVT 范围约为 `-25.35%–+25.92%`。
- `max_parallel_units=16`，nominal 目标可覆盖到约 `133.8 pF`；单 PCell 满足
  2% 容差时优先使用单元，只有超出单元范围时才使用并联阵列。

### OA CDF 正向映射

`cfmom_2t` 的 base CDF 包含派生参数 `c`（`CapValue@0V_(F)`）。项目可以在
临时 OA library 中设置 `Wfinger/Sfinger/Lfinger/Nfinger/StartMn/StopMn/m`，执行
CDF 登记的 callback，并读取 `c`，无需为每个候选尺寸运行 Spectre。实现见
`Agent_LLM_BO/circuit_agent/pdk_integration/cdf_evaluator.py`：

```bash
python -m pdk_integration.cdf_evaluator \
  --target 241.291f \
  --work-dir /share/tmp/cfmom_cdf_mapping
```

默认 `mapping_mode=callback`。mapper 缓存合法 `Nfinger` 在最小长度处的 CDF
校准值，先评估一个预测尺寸，再用两点线性插值校正长度，最终批量评估附近的
`Lfinger` 网格。最终值和参数都来自 callback，不用预测值替代。真实 OA
实验对 `241.291 fF` 目标得到 `nr=280, lr=1.5 um, w=s=0.05 um, stm=1,
spm=6, multi=1`：CDF 为 `241.389 fF`（`+0.0406%`），独立 Spectre TT/27°C
验证为 `241.424 fF`（`+0.0550%`）。另外三组尺寸中，CDF 与 Spectre 的差异约
为 `0.01%`，因此 CDF 可作为快速 nominal 正向 evaluator，最终结果仍需 Spectre
和 PVT 验证。

当前 OA callback 会把请求的 `StopMn=8` 合法化为 `StopMn=6`，因此生产映射使用
callback 回写的 `spm=6`。现有 `spm=8` LUT 仅保留为旧版 characterization 和 PVT
范围参考，不再作为默认 nominal 几何来源。对 `500 fF` 的 flow 集成验证得到 CDF
`499.758 fF`、Spectre TT/27°C `499.831 fF`，两者相差 `0.0146%`。

## 验证

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
PDK_PROFILE_FILE=../../PDK_Info_Json/TSMC_28nm_Information.json \
  python -m pdk_integration.profiles --validate --require-gmid --require-virtuoso
```

在真实 Cadence 服务器上追加 `--check-files`，验证模型路径和 OA library 路径。
