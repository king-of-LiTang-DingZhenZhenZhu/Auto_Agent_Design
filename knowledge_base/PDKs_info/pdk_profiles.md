# PDK Profiles

项目通过 `Agent_LLM_BO/circuit_agent/pdk_profiles.py` 集中管理工艺相关配置。拓扑脚本不应直接写死 PDK model include 路径或 MOS model 名称，而应从当前 profile 读取。

工艺信息文件统一存放在仓库根目录的 `PDK_Info_Json/`，并使用
`<厂商>_<工艺节点名称>_Information.json` 命名。当前 TSMC28 配置位于
`PDK_Info_Json/TSMC_28nm_Information.json`；`get_pdk_profile("tsmc28")` 会优先读取
该文件，`pdk_profiles.py` 中的同名内置项仅作为文件缺失时的兼容回退。

## 当前默认 profile

`tsmc28`:

| 字段 | 默认值 |
|------|--------|
| Spectre model | `/PDKS/TSMC28nm/models/spectre/toplevel.scs` |
| Spectre section | `top_tt` |
| PVT process sections | `tt:top_tt, ss:top_ss, ff:top_ff` |
| HSPICE model | `/PDKS/TSMC28nm/models/hspice/toplevel.l` |
| HSPICE section | `TOP_TT` |
| NMOS / PMOS | `nch_mac` / `pch_mac` |
| LVT NMOS / PMOS | `nch_lvt_mac` / `pch_lvt_mac` |
| Special models | `pnp:pnp5`, `resistor_poly:rupolym` |
| VDD default/range | default `0.9 V`, allowed `0.9 V ~ 1.1 V` |
| W per finger | `0.2um ~ 2.6um` |
| gm/Id table | `gmid_lookup_table/gm_id_tables_tsmc28.json` |
| PVT temperatures | `-40, 27, 125 °C` |
| Spectre options | `rawfmt=psfascii`, `soft_bin=allmodels` |
| Virtuoso tech lib | `tsmcN28` |
| Virtuoso OA lib path | `/PDKS/TSMC28nm/tsmcN28` |

## VDD 使用逻辑

`PDKProfile.vdd` 是默认电源电压，不代表该工艺只能使用一个 VDD。对于 TSMC28，profile 记录 `vdd_min=0.9`、`vdd_max=1.1`，表示当前项目允许在这个范围内做电路级选择。

实际某次生成网表时，优先级如下：

1. `params["VDD"]`：单次设计显式指定，优先级最高。
2. 环境变量 `VDD`：本机/本项目默认值。
3. `PDKProfile.vdd`：profile 默认值。

示例：

```python
from topologies import get_topology

topo = get_topology("folded_cascode")
topo.write_project(
    "folded_1v1",
    params={"VDD": 1.1, "VCM": 0.3, "CL": 1e-12},
)
```

命令行环境覆盖：

```bash
export VDD=1.1
```

如果需要对 VDD 做 BO 搜索，应在 topology 的 `get_param_space()` 或显式 `params.json` 中加入 `VDD` 参数，并把范围限制在 `vdd_min~vdd_max` 内；不要在 topology 模板里写死电源值。

## 晶体管类型使用逻辑

profile 同时提供常规 MOS 和 LVT MOS model 名称：

| topology 使用场景 | profile 字段 | 默认 TSMC28 model |
|------------------|--------------|-------------------|
| 常规 NMOS | `nmos_model` | `nch_mac` |
| 常规 PMOS | `pmos_model` | `pch_mac` |
| LVT NMOS | `nmos_lvt_model` | `nch_lvt_mac` |
| LVT PMOS | `pmos_lvt_model` | `pch_lvt_mac` |

当前 `five_t_ota`、`two_stage_ota` 使用常规 MOS；`nmcnr_three_stage`、`mnmc_three_stage`、`nmcf_three_stage`、`folded_cascode` 与 `folded_cascode_two_stage` 使用 LVT MOS。换 PDK 时，只需要改 profile 中这些 model 名称，topology 会把对应 model 写入生成的 Spectre netlist 和 gm/Id sizing spec。

`leung_mok_sub1v_bandgap` 按论文“不需要低阈值器件”的条件使用常规
`nmos_model/pmos_model` 和 `special_models.pnp`。其 PMOS 体端前向偏置必须
在目标 PDK 的结电流和可靠性规则下重新验证。

PNP、poly resistor 等非 MOS 器件写入 `special_models`，通过
`profile.resolve_model(<role>)` 获取。当前 `bandgap_ptat` 需要：

```json
"special_models": {
  "pnp": "pnp5",
  "resistor_poly": "rupolym"
}
```

## Topology 初始参数 preset

不同 PDK 或同一 PDK 的不同器件型号，可能需要不同初始 W/L、bias、VCM 和搜索范围。项目通过 `PDKProfile.topology_presets` 表达这些差异，而不是把工艺专用初值写回 topology 源码。

`topology_presets` 是**可选校准层**，不是“每个拓扑 × 每个工艺”都必须填写的矩阵。新增 PDK 时，先只配置模型路径、model 名、VDD、尺寸限制和 gm/Id 表；如果某个 topology 的默认初始仿真明显不工作，或者某个型号需要特定 VCM/bias/search range，再只为这个 topology 增加 preset。简单拓扑可以长期只使用通用 `DEFAULT_PARAMS`。

每个 topology preset 支持三类字段：

| 字段 | 作用 |
|------|------|
| `default_params` | 覆盖 topology 的通用 `DEFAULT_PARAMS`，影响初始网表、`initial_default/`、普通 BO 初始点和 gm/Id pass-through/fixed 参数 |
| `testbench_defaults` | 覆盖 testbench 默认值，例如 `VCM`、`IBIAS`、`VBIAS`、`CL`；`VDD` 仍优先使用 profile 顶层 `vdd` |
| `param_space_overrides` | 覆盖指定 BO 参数的 `low/high/log_scale/unit/max_per_finger/value_type`；`default` 可写在 JSON 中作记录，但当前初始值主要由 `default_params` 决定 |

外部 JSON profile 示例：

```json
{
  "name": "my28_lvt",
  "spectre_model_path": "/PDKS/MY28/models/spectre/top.scs",
  "spectre_section": "tt",
  "...": "...",
  "topology_presets": {
    "folded_cascode_two_stage": {
      "default_params": {
        "Lbias": 5e-7,
        "Wbp_big": 6e-6,
        "m_half_unit": 4,
        "m_load_ratio": 3
      },
      "testbench_defaults": {
        "VCM": 0.35,
        "IBIAS": 2e-5,
        "CL": 1e-12
      },
      "param_space_overrides": {
        "m_half_unit": {"low": 3, "high": 6}
      }
    }
  }
}
```

## 电压域与器件 Flavor

一个 PDK profile 可声明多个 `voltage_domains`。选择 domain 会同时切换
VDD 范围、Spectre/HSPICE section、gm/Id table（如有覆盖）以及 MOS model；不要只
切换 model 而保留原 VDD。`model_flavors` 以 `nmos/pmos → svt/lvt/hvt/...` 组织，
可用 `profile.resolve_model("pmos:hvt")` 解析。

```json
{
  "name": "my28",
  "...": "base core fields retained for backward compatibility",
  "voltage_domains": {
    "core_1p0": {
      "vdd": 1.0,
      "vdd_min": 0.9,
      "vdd_max": 1.1,
      "max_device_voltage": 1.1,
      "model_flavors": {
        "nmos": {"svt": "n_core_svt", "lvt": "n_core_lvt"},
        "pmos": {"svt": "p_core_svt", "lvt": "p_core_lvt"}
      }
    },
    "io_1p8": {
      "vdd": 1.8,
      "vdd_min": 1.62,
      "vdd_max": 1.98,
      "max_device_voltage": 1.98,
      "spectre_section": "io_tt",
      "gmid_table_path": "/PDKS/MY28/gmid_io_1p8.json",
      "model_flavors": {
        "nmos": {"svt": "n_io18_svt", "hvt": "n_io18_hvt"},
        "pmos": {"svt": "p_io18_svt", "hvt": "p_io18_hvt"}
      }
    }
  }
}
```

选择方式：设环境变量 `PDK_VOLTAGE_DOMAIN=io_1p8`，或在生成项目时传入
`params={"VOLTAGE_DOMAIN": "io_1p8"}`。后者会记录到 `requirements.json`，
`main.py` 在 gm/Id sizing 前自动恢复同一 domain。

如果某个 profile 没有给 topology preset，系统会回退到 topology 自带的 `DEFAULT_PARAMS` 和默认搜索空间，保证旧 profile 兼容。推荐维护策略是：

1. 新 PDK 先跑无 preset 的 topology dry-run/初始仿真。
2. 只有出现初始工作点明显不可用、偏置节点不合理、搜索范围不适合该型号时，才补 `topology_presets.<topology>`。
3. folded cascode、NMCF 这类偏置复杂拓扑优先准备 preset；5T OTA、two-stage OTA 可以先不写或只覆盖少量 `VCM/VBIAS/CL`。

## 切换或覆盖

优先推荐用 profile 分组：

```bash
export CIRCUIT_AGENT_PDK=tsmc28
```

本地机器路径不同但工艺相同时，可以只覆盖某些字段：

```bash
export PDK_SPECTRE_PATH=/my/pdk/models/spectre/toplevel.scs
export GMID_TABLE_PATH=/my/pdk/gmid/gm_id_tables.json
export VIRTUOSO_PDK_LIB_PATH=/my/pdk/tsmcN28
```

常用覆盖变量：

| 环境变量 | 作用 |
|----------|------|
| `CIRCUIT_AGENT_PDK` / `PDK_PROFILE` | 选择 profile 名称 |
| `PDK_SPECTRE_PATH`, `PDK_SPECTRE_SECTION` | Spectre include |
| `PDK_PROCESS_SECTIONS` | PVT process 到 Spectre section 的映射，例如 `tt:top_tt,ss:top_ss,ff:top_ff` |
| `PDK_HSPICE_PATH`, `PDK_HSPICE_SECTION` | HSPICE include |
| `NMOS_MODEL`, `PMOS_MODEL` | 常规 MOS model |
| `NMOS_LVT_MODEL`, `PMOS_LVT_MODEL` | LVT MOS model |
| `VDD`, `VDD_MIN`, `VDD_MAX` | 默认电源电压和允许范围 |
| `PDK_MIN_L`, `PDK_MAX_WIDTH_PER_FINGER`, `PDK_MIN_WIDTH_PER_FINGER` | 尺寸边界 |
| `GMID_TABLE_PATH` | 当前 PDK 的 gm/Id lookup JSON |
| `PDK_PVT_TEMPERATURES` | PVT 温度列表，例如 `-40,27,125` |
| `PDK_SPECTRE_OPTIONS` | testbench options，例如 `rawfmt=psfascii,soft_bin=allmodels` |
| `VIRTUOSO_TECH_LIB`, `VIRTUOSO_PDK_LIB_PATH` | Virtuoso library 绑定 |

## 添加新工艺

推荐新增一个 profile，而不是改 topology：

1. 在仓库根目录 `PDK_Info_Json/` 下新增完整的 `<厂商>_<工艺节点名称>_Information.json`；不要把新工艺数据散落到 topology 中。
2. 填写 Spectre/HSPICE model include、nominal section、PVT process section、VDD 范围、MOS/special model role、尺寸约束、gm/Id table path、Virtuoso tech lib、OA library path，以及必要 topology 的 `topology_presets`。
3. 如果希望通过短名称选择该工艺，在 `pdk_profiles.py` 的 `PDK_INFORMATION_FILES` 中登记名称与 JSON 路径；否则可通过 `PDK_PROFILE_FILE` 或把 JSON 路径直接传给 `get_pdk_profile()` 加载。
4. 确认 gm/Id 表包含 topology 需要的 model 名。常规拓扑需要 `nmos/pmos`；folded cascode 当前需要 `nmos_lvt/pmos_lvt`。
5. 运行 profile 验证：

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
python pdk_profiles.py --validate --require-gmid --require-virtuoso
```

6. 在真实 Cadence/Spectre 机器上加 `--check-files`，确认模型文件和 Virtuoso OA library 路径可见。

优化完成后，`outputs/<project>/pdk_profile_used.json` 会保存当次使用的 profile 快照，方便之后复现实验或排查 PDK 切换问题。

## 片上电阻与电容

`passive_devices` 是无源器件实现的机器可读数据源；本文档中的器件名称只作说明。
不要根据 Virtuoso GUI 显示值猜测 PCell 参数，也不要把受 NDA 约束的数据提交到仓库。
这类数据必须写入对应的 `PDK_Info_Json/<厂商>_<工艺节点名称>_Information.json`；
可通过已登记的短名称、显式 JSON 路径或 `PDK_PROFILE_FILE` 加载。

```json
{
  "passive_devices": {
    "example_rpoly": {
      "kind": "resistor",
      "spectre_model": "<model-from-pdk>",
      "virtuoso_lib": "<pdk-library>",
      "virtuoso_cell": "<pcell-name>",
      "mapping_mode": "callback",
      "evaluator_key": "my_pdk_rpoly_callback",
      "term_order": ["PLUS", "MINUS"],
      "parameter_map": {"W": "w", "L": "l"},
      "min_width_m": 5e-7,
      "max_width_m": 5e-6,
      "min_length_m": 5e-7,
      "max_length_m": 1e-4,
      "geometry_grid_m": 1e-8,
      "default_width_m": 1e-6,
      "max_aspect_ratio": 100,
      "sheet_resistance_ohm_per_square": 100,
      "max_series_units": 8,
      "max_parallel_units": 4,
      "value_tolerance": 0.02
    },
    "example_mim": {
      "kind": "capacitor",
      "spectre_model": "<model-from-pdk>",
      "virtuoso_lib": "<pdk-library>",
      "virtuoso_cell": "<pcell-name>",
      "mapping_mode": "lookup",
      "lookup_table_path": "characterization/example_mim.json",
      "min_width_m": 1e-6,
      "max_width_m": 50e-6,
      "min_length_m": 1e-6,
      "max_length_m": 50e-6,
      "geometry_grid_m": 1e-7,
      "default_aspect_ratio": 1,
      "max_aspect_ratio": 4,
      "capacitance_per_area_f_per_m2": 0.001,
      "max_parallel_units": 16,
      "term_order": ["PLUS", "MINUS"]
    }
  },
  "passive_role_map": {
    "compensation_resistor": "example_rpoly",
    "feedback_resistor": "example_rpoly",
    "compensation_capacitor": "example_mim",
    "feedforward_capacitor": "example_mim"
  }
}
```

映射模式：

- `value`：Spectre/PCell 有经过验证的可写容阻值参数。必须填写 `value_parameter`。
- `callback`：推荐用于可调用 CDF/PCell/Spectre 探针的真实 PDK。必须填写
  `evaluator_key`，并在运行进程中注册对应 evaluator。
- `lookup`：使用预 characterization 数据。JSON 包含 `version`、提取条件和
  `points`；每个 point 至少有 `value` 与实际 Spectre `params`，可附 `area_m2`。
- `formula`：仅作为离线开发/测试 fallback。方阻或电容密度只用于初始点；映射器
  仍通过统一 evaluator 获得 `actual_R/C`，生产 signoff 不应把简化公式当 PDK 真值。

几何字段集中在 `PassiveDeviceProfile`，包括 W/L min/max、manufacturing grid、默认
W/长宽比、最大 aspect ratio、单位面积上限，以及 `m/nseg/finger/array row/column`
参数名和上限。外部 series/parallel decomposition 只在单个合法 PCell 无法达到容差
时启用。

Python callback 的接入方式：

```python
from passive_mapping import (
    CallablePassiveEvaluator,
    DeviceEvaluation,
    IllegalDeviceGeometry,
    register_passive_evaluator,
)

def evaluate_rpoly(device, params):
    # 在这里复用站点已有的 SKILL server、CDF/PCell callback 或 Spectre probe。
    # params 中 W/L 为 SI 数值；回调必须返回 PDK 计算的真实值。
    response = site_pdk_bridge.evaluate(device.virtuoso_cell, params)
    return DeviceEvaluation(
        actual_value=response["actual_value"],
        area_m2=response.get("area_m2"),
        resolved_params=response.get("resolved_params", params),
        metadata={"run_id": response.get("run_id")},
    )

register_passive_evaluator(
    "my_pdk_rpoly_callback",
    CallablePassiveEvaluator(evaluate_rpoly, backend_name="site_skill_server"),
)
```

`resolved_params` 用于回传 CDF callback 最终接受的合法 W/L/m/nseg 等参数；如果
callback 未改参数可原样返回。单个尺寸被 PCell 拒绝时 callback 应抛出
`IllegalDeviceGeometry`，搜索器会继续其他 grid 点；工具/许可证/通信失败应抛出其他
异常并立即终止，不能伪装成“无合法尺寸”。callback 未注册、返回非正/非有限值或
找不到满足容差的合法尺寸时，流程会明确 blocked，不会退回硬编码 foundry 公式。
报告用 `mapping_no_solution_or_configuration`、`mapping_input_error` 和
`pdk_evaluator_error` 区分无解/配置、输入错误与外部 PDK 工具故障。

```json
{
  "version": "pdk-release-and-extraction-revision",
  "corner": "tt",
  "temperature_c": 27,
  "method": "DC V/I or AC imag(Y)/(2*pi*f)",
  "points": [
    {"value": 10000.0, "params": {"w": "1u", "l": "100u"}, "area_m2": 1e-10}
  ]
}
```

先通过 PDK 文档、CDF 和生成的 Spectre netlist 确认可写参数与单位；电阻用 DC
`V/I`，电容用 AC `imag(Y)/(2*pi*f)` 建表。CDF 派生显示值只用于交叉检查。
已知 PCell 的 library/cell 后，可以生成只读 CDF/端口探针，再在 Cadence 工作目录中运行：

```bash
python pdk_passive_probe.py --device <passive_devices-key> --out passive_cdf_probe.il
virtuoso -nograph -replay passive_cdf_probe.il
```

探针不创建或修改 cell，只把 base CDF 参数和 symbol terminals 写到
`passive_cdf_report.txt`。随后仍需检查一次 PCell 生成的 Spectre netlist，确认 CDF
属性到 simulator 参数的真实映射。

BO 达标后运行设计流时，无源器件阶段会：

1. 只转换 topology 标记为 `on_chip` 的 DUT 器件；`external/testbench` 保持理想。
2. 固定合理 W，以解析近似或合法几何生成 L 初值，然后在 grid 上调用 PDK evaluator
   做 bracket/bisection 和邻点离散搜索；电容还会搜索接近方形的多个 W 候选。
3. 单 PCell 无解时尝试合法的 m/nseg/finger/array 和外部 series/parallel 组合。
4. 按误差、面积、unit 数量选择解，输出
   `passive_realization/passive_realization.json` 和 PDK 网表；报告同时保留 target、
   actual、relative error、area、evaluator backend 和 decomposition。
5. 使用 PDK 模型重跑 nominal；只有映射后 nominal 验证通过，才允许 Design Audit、
   PVT 和 Virtuoso 导出。

```bash
python design_flow_graph.py --project outputs/<project> --run-pvt --simulate
```

默认 `tsmc28` profile 目前没有填写 MIM PCell、CDF 参数或无源特性表。代码会明确
报 `configure_pdk_passives`，不会猜测器件名、换算公式或回退到 `analogLib`。

# TSMC28 IO 1.8 V 域

内置 `tsmc28` profile 提供 `io_1p8` voltage domain：

- 默认/允许电源：`1.8 V`，范围 `1.62–1.98 V`
- NMOS：`nch_25ud18_mac`
- PMOS：`pch_25ud18_mac`
- 最小沟道长度：`300 nm`

项目参数或环境变量使用 `VOLTAGE_DOMAIN=io_1p8` 选择该域。当前 gm/Id lookup table 不包含这两个 IO model，因此使用它们的拓扑采用物理 W/L 参数 BO，而不调用 core-device gm/Id 表。
