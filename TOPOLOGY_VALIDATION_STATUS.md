# 拓扑可行性与验证状态

最后更新：2026-08-09

本文档记录 `topologies.TOPOLOGY_REGISTRY` 中每个拓扑的电气验证状态。拓扑已注册、能够生成网表或已被单元测试覆盖，并不等同于该电路在电气上有效。

## 状态定义

- **PASS**：真实 Spectre 标称仿真表明核心功能正常，并满足当前单次验证所列的主要功能指标。晶体管在线性区或接近工作区边界作为设计风险备注，不单独否定功能 PASS。
- **PARTIAL PASS**：电路已表现出部分预期功能，但当前参数下仍有可通过进一步调参、补偿或尺寸优化修正的指标问题。例如运放已经具有增益和带宽，但相位裕度不足或为负。
- **FAIL**：真实仿真表明核心功能不成立，不能作为当前用途使用。例如基准无法启动、锁定在错误工作点或输出行为与拓扑目标相反。
- **UNVERIFIED**：尚无足够的真实仿真证据判断核心功能。没有测试证据不等同于 FAIL。

功能状态与资格签核分开记录。`PASS` 表示指定标称条件下功能可用，不表示已完成 PVT；只有真实 `pvt/pvt_results.json` 中 `pvt_pass=true` 才能注明“PVT 已签核”。所有状态变更都必须引用持久仿真证据；dry-run、生成网表和单元测试不能提升功能状态。

## 注册表汇总

| 拓扑 | 领域/架构 | 状态 | 仿真证据与备注 |
|---|---|---|---|
| `5t_ota` | 单级五管 OTA | **PASS** | TSMC28、0.9 V、TT 条件下，默认参数 AC 验证得到增益 24.38 dB、GBW 135.61 MHz、相位裕度 92.29°，核心功能正常；尚未完成 PVT。 |
| `two_stage_ota` | 两级 Miller OTA | **PASS** | TSMC28 标称功能达标，并通过全部 27 个 `tt/ss/ff ×（vmin=0.9/vtyp=0.9/vmax=1.1 V）× -40/27/125°C` PVT 角。 |
| `pmos_input_two_stage_ota` | PMOS 输入两级 Miller OTA | **PASS** | 修正输入映射后，增益 67.01 dB、GBW 42.76 MHz、相位裕度 81.07°，SR/ST 正常。备注：默认尺寸下 `Mtail` 在线性区，尚未完成 PVT。 |
| `mzc_two_stage_ota` | Miller 零点消除/前馈两级 OTA | **PASS** | TSMC28、0.9 V、TT 条件下，增益 42.39 dB、GBW 101.50 MHz、相位裕度 55.52°，核心功能正常；尚未完成 PVT。 |
| `pmos_input_mzc_two_stage_ota` | PMOS 输入 MZC 两级 OTA | **PASS** | 修正输入映射后，增益 66.01 dB、GBW 42.80 MHz、相位裕度 78.84°，SR/ST 正常。备注：`Mtail` 和 `Mtailff` 在线性区，尚未完成 PVT。 |
| `folded_cascode` | 单级折叠共源共栅 OTA | **PASS** | TSMC28、0.9 V、TT 条件下，增益 60.67 dB、GBW 133.11 MHz、相位裕度 80.89°。备注：若干器件接近饱和区边界，尚未完成 PVT。 |
| `folded_cascode_two_stage` | 折叠共源共栅加第二级 OTA | **PASS** | 新默认值在 TSMC28、0.9 V、TT、1 pF 下得到增益 79.46 dB、GBW 218.47 MHz、相位裕度 66.33°，主要 AC 指标达标。备注：默认点有 `Mtailp`、`Mmirr1`、`Mmirr2` 在线性区；60 轮 BO 候选可减少到仅 `Mtailp` 在线性区。 |
| `nmcnr_three_stage` | 带消零电阻的嵌套 Miller 三级 OTA | **PASS** | TSMC28、0.9 V、TT 条件下，增益 88.06 dB、GBW 27.18 MHz、相位裕度 71.65°，核心功能正常；尚未完成 PVT。 |
| `mnmc_three_stage` | 多通路嵌套 Miller 三级 OTA | **PARTIAL PASS** | 默认点已有 69.76 dB 增益和 170.42 MHz GBW，但相位裕度为 -94.52°；当前不可稳定使用，预计可通过补偿和参数迭代修正。 |
| `nmcf_three_stage` | 嵌套 Miller/前馈三级 OTA | **PARTIAL PASS** | 默认点已有 85.34 dB 增益和 184.18 MHz GBW，但相位裕度为 -54.58°；当前不可稳定使用，预计可通过补偿和参数迭代修正。 |
| `strongarm_latch` | 动态 StrongARM 锁存比较器 | **PASS** | 正负判决均通过标称目标，并通过全部 27 个 PVT 角。 |
| `bandgap_ptat` | 层次化 PNP PTAT 基准 | **PASS** | 在 `VDD=1.1 V`、TT 温度仿真中，PTAT 输出从 `-40°C 时 0.3849 V` 单调上升至 `125°C 时 0.6113 V`，功能正常。适用条件：`VDD=0.9 V` 时低温裕量不足；尚未完成 1.1 V 下完整签核。 |
| `banba_sub1v_bandgap` | Banba 电流求和型亚 1 V 带隙基准 | **FAIL** | `VDD=1.1 V` 时核心收敛在接近零电流的错误状态（`Vref=0.342 mV`、`startup_success=false`）；启动电路持续导通且未能建立核心电流，当前功能不正常。 |
| `leung_mok_sub1v_bandgap` | Leung-Mok 2002 亚 1 V 带隙基准 | **UNVERIFIED** | 已实现拓扑及专用测试平台，但没有保留真实标称仿真证据。 |
| `capless_ldo` | PMOS 调整管无片外电容 LDO | **UNVERIFIED** | 已有专用测试平台，但没有保留真实标称仿真证据。 |
| `dfc_capless_ldo` | DFC 无片外电容 LDO | **UNVERIFIED** | 已实现拓扑及专用测试平台，但没有保留真实标称仿真证据。 |

## 运放单次验证

2026-08-08 保留的验证，对每个运放拓扑使用 `get_default_params()` 和该拓扑的主要 AC 测试平台，各执行一次真实 Spectre AC/DC 仿真。该验证不运行 BO、gm/Id 尺寸设计、SR/ST、Review 或 PVT。证据目录为：

`outputs/opamp_topology_default_validation_20260808_111005/`

本验证不使用 `main.py --max-iter 1`。在 gm/Id 模式下，`main.py` 可能依次运行 `DEFAULT_PARAMS` 基线、gm/Id 初始点和一次 BO 试验。优化器还会将初始候选加入队列，因此唯一一次 BO 试验通常只会重复初始点，而不会探索修复候选。

单次验证使用与汇总表相同的功能口径：主要 AC 指标达到当前验证目标为
`PASS`；已有增益/带宽但稳定性等指标尚待调参修正为 `PARTIAL PASS`；器件
工作区异常单独备注，不因这一项自动降级。

| 拓扑 | 默认结果 | 增益 | GBW | 相位裕度 | 关键工作点 |
|---|---|---:|---:|---:|---|
| `5t_ota` | PASS | `24.38 dB` | `135.61 MHz` | `92.29°` | 通过 |
| `two_stage_ota` | PASS | `47.14 dB` | `98.87 MHz` | `58.82°` | 通过；`Mdiff2` 接近边界 |
| `pmos_input_two_stage_ota` | PASS | `67.01 dB` | `42.76 MHz` | `81.07°` | 备注：默认尺寸下 `Mtail` 在线性区 |
| `mzc_two_stage_ota` | PASS | `42.39 dB` | `101.50 MHz` | `55.52°` | 通过；`Mdiff2` 接近边界 |
| `pmos_input_mzc_two_stage_ota` | PASS | `66.01 dB` | `42.80 MHz` | `78.84°` | 备注：默认尺寸下 `Mtail` 和 `Mtailff` 在线性区 |
| `folded_cascode` | PASS | `60.67 dB` | `133.11 MHz` | `80.89°` | 通过；五个关键器件接近边界 |
| `folded_cascode_two_stage` | PASS | `79.46 dB` | `218.47 MHz` | `66.33°` | 备注：`Mtailp`、`Mmirr1`、`Mmirr2` 在线性区，另有关键器件接近边界 |
| `nmcnr_three_stage` | PASS | `88.06 dB` | `27.18 MHz` | `71.65°` | 通过 |
| `mnmc_three_stage` | PARTIAL PASS | `69.76 dB` | `170.42 MHz` | `-94.52°` | 当前稳定性不合格，可继续调参修正 |
| `nmcf_three_stage` | PARTIAL PASS | `85.34 dB` | `184.18 MHz` | `-54.58°` | 当前稳定性不合格，可继续调参修正 |

### `folded_cascode_two_stage` BO 复验

修复 Spectre 大写 `NaN` 导致的 DC OP 解析缺失后，在 TSMC28、0.9 V、
TT、1 pF 负载下，以增益不低于 50 dB、GBW 不低于 10 MHz、相位裕度
不低于 60°为硬约束，以此前迭代 35 的参数作为新默认值重新运行了 60 次
BO。持久证据为：

`Agent_LLM_BO/circuit_agent/outputs/folded_cascode_two_stage_iter35_default_bo60_20260809/results.json`

- 新默认基线为增益 79.46 dB、GBW 218.47 MHz、相位裕度 66.33°、
  功耗 330.63 μW；AC 硬指标通过，但 `Mtailp`、`Mmirr1`、`Mmirr2`
  在线性区。
- 60 次搜索中没有任何候选通过关键工作区检查；多个后期候选已将关键
  线性区器件减少到仅 `Mtailp`。
- 最终保存的候选来自迭代 43：增益 77.30 dB、GBW 252.55 MHz、相位
  裕度 65.06°、功耗 459.19 μW；仅 `Mtailp` 在线性区，另有 5 个关键
  MOS 接近饱和边界，`all_targets_met=true`、
  `operating_point_status.passed=false`。

因此，该拓扑的主要 AC 功能指标已经达标，功能状态记为 **PASS**。关键器件
工作区问题保留为设计风险备注；在正式 PVT 签核前仍建议继续处理该风险，
但它不再否定本次功能验证结论。

上述 PMOS 输入拓扑在修正内部输入极性后重新运行，同时保留公开的 `(vip vin vout ibias vdd vss)` 端口约定。修正后的 AC/SR/ST 证据保存在：

`outputs/pmos_input_polarity_fix_validation_20260808_111753/`

| 拓扑 | 增益 | GBW | 相位裕度 | 最小转换速率 | 0.1% 稳定时间 |
|---|---:|---:|---:|---:|---:|
| `pmos_input_two_stage_ota` | `67.01 dB` | `42.76 MHz` | `81.07°` | `50.36 V/μs` | `25.05 ns` |
| `pmos_input_mzc_two_stage_ota` | `66.01 dB` | `42.80 MHz` | `78.84°` | `51.20 V/μs` | `23.18 ns` |

全部六项 Spectre 分析均以零错误完成。默认工作点剩余的线性区器件源于尺寸/电压裕量限制，而非反馈极性错误；按当前功能判定口径，两种 PMOS 输入拓扑均记为 **PASS**，并保留工作区风险备注。

## PVT 已签核证据

### `two_stage_ota`

- PDK profile：`tsmc28`。
- 标称结果：`outputs/two_stage_ota_pvt_smoke/results.json`。
- 标称指标：增益 `45.87 dB`、GBW `34.88 MHz`、相位裕度 `67.60°`、功耗 `138.7 μW`、最小转换速率 `36.64 V/μs`、稳定时间 `19.25 ns`；`all_targets_met=true`。
- PVT 结果：`outputs/two_stage_ota_pvt_smoke/pvt/pvt_results.json`。
- PVT 覆盖：27/27 个角全部通过。保留的最差值为增益 `39.12 dB`、GBW `27.4 MHz`、相位裕度 `62.88°`、功耗 `0.14 mW`、转换速率 `34.27 V/μs`、稳定时间 `25.13 ns`。
- 适用范围：该证据验证的是所引用的设计指标和负载，并不表示拓扑元数据范围内的每组指标都可实现。

### `strongarm_latch`

- PDK profile：`tsmc28`。
- 标称结果：`Agent_LLM_BO/circuit_agent/outputs/strongarm_latch_validation_20260808_codex/results.json`；`all_targets_met=true`。
- 标称条件：`VDD=0.9 V`、`TT`、`27°C`、输入共模 `0.45 V`、差分输入 `10 mV`、每端负载 `5 fF`、时钟周期 `4 ns`。
- 标称指标：正/负判决裕量分别为 `0.899998 V` 和 `0.899998 V`，正/负传播延迟均为 `349.54 ps`，每次判决能量为 `39.19 fJ`，平均功耗为 `9.80 μW`。对应目标为裕量不低于 `0.45 V`、延迟不高于 `1 ns`、能量不高于 `200 fJ`、功耗不高于 `100 μW`。
- PVT 结果：`Agent_LLM_BO/circuit_agent/outputs/strongarm_latch_validation_20260808_codex/pvt/pvt_results.json`；标准 `pvt_simulation.py --results ... --simulate` 入口运行得到 `pvt_pass=true`。
- PVT 覆盖：27/27 个声明角全部收敛并通过；该 profile 的 `vmin` 和 `vtyp` 均为 `0.9 V`，`vmax` 为 `1.1 V`。最差正/负判决裕量均为 `0.887505 V`，最差正/负传播延迟均为 `970.54 ps`，最大判决能量为 `62.61 fJ`，最大功耗为 `15.65 μW`。
- 适用范围：该证据验证确定性模型下指定输入差分、共模、负载和时钟条件的功能及 PVT 鲁棒性；尚未覆盖 Monte Carlo mismatch、输入失调分布、极小差分输入或 metastability 统计验证。

## 条件性 PASS 证据

### `bandgap_ptat`

默认 `tsmc28`、`VDD=0.9 V` 仿真能够在室温下启动，但在完整温度范围内无效。在 `-40°C` 时，PNP 发射极节点和 `Vref` 接近电源电压，使 PMOS 镜像器件两端只剩几十毫伏压降。在约 `-4°C` 到 `-3°C` 之间会发生直流工作点跃迁。

将电源提高到 `1.1 V` 后，镜像器件的电压裕量恢复，并产生预期的单调 PTAT 响应：

| 温度 | 1.1 V 电源下的 Vref |
|---:|---:|
| `-40°C` | `0.3849 V` |
| `0°C` | `0.4394 V` |
| `27°C` | `0.4767 V` |
| `60°C` | `0.5224 V` |
| `125°C` | `0.6113 V` |

证据目录：`outputs/bandgap_ptat_validation_20260805_165934/`。根目录中的 `results.json` 记录 0.9 V 完整测试平台仿真；1.1 V 温度证据位于 `temperature_diagnostic_vdd_1p1/`。

完成 PVT 签核前必须完成以下工作：

1. 明确定义 PTAT 输出、启动、功耗、PSRR 和线性调整率指标。
2. 在确定合格的电源电压下运行全部专用标称测试平台。
3. 在要求的工艺、电压和温度角下全部通过，且不存在镜像器件电压裕量或工作点阻塞问题。

## FAIL 证据

### `banba_sub1v_bandgap`

在 `VDD=1.1 V` 时，Spectre 能够收敛，但电路无法脱离启动状态：

- `Vref = 0.342 mV`、`startup_success=false`；
- `VRS = 0.3277 V`，`SUP` 保持在接近地电位，因此启动电路持续有效；
- `Va = 0.788 V`、`Vb = 1.080 V`、`Vg = 1.098 V`；
- 因此误差放大器会关断 P1/P2/P3 核心电流镜。

VREF 检测器和自动关断极性符合设计。将启动 LVT 器件替换为普通阈值器件并不能修复该问题。交换误差放大器输入会使 `Vref` 上升到电源电压，同样是无效状态。剩余的修复范围是启动注入机制，以及它与 Va/Vb 两支路不等阻抗之间的相互作用。

证据目录：`outputs/banba_sub1v_bandgap_validation_vdd_1p1_20260805_173123/`。

## 更新本文档

产生新证据时：

1. 确认 `pdk_profile_used.json` 与预期 PDK 和电压域一致。
2. 记录准确的设计指标、负载、电源、温度和测试平台集合。
3. 检查收敛情况、要求的指标、启动状态和关键工作点；仅仅收敛不能视为通过。
4. 主要功能指标达标时使用 **PASS**，工作区问题作为风险备注。
5. 当前参数下存在预计可通过迭代修正的指标问题时使用 **PARTIAL PASS**。
6. 核心功能不成立时使用 **FAIL**；没有足够仿真证据时使用 **UNVERIFIED**。
7. PVT 是否签核单独记录，不改变标称功能状态的定义。
8. 在更新且持久保存的结果证明问题已经解决之前，继续在说明中保留失败或风险证据。
