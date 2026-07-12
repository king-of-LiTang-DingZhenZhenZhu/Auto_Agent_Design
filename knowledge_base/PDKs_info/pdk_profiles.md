# PDK Profiles

项目通过 `Agent_LLM_BO/circuit_agent/pdk_profiles.py` 集中管理工艺相关配置。拓扑脚本不应直接写死 PDK model include 路径或 MOS model 名称，而应从当前 profile 读取。

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

当前 `five_t_ota`、`two_stage_ota`、`nmcf_three_stage` 使用常规 MOS；`folded_cascode` 与 `folded_cascode_two_stage` 使用 LVT MOS。换 PDK 时，只需要改 profile 中这些 model 名称，topology 会把对应 model 写入生成的 Spectre netlist 和 gm/Id sizing spec。

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

1. 在 `pdk_profiles.py` 的 `PDK_PROFILES` 中新增一项，或准备外部 JSON 并设置 `PDK_PROFILE_FILE=/path/to/profile.json`。
2. 填写 Spectre/HSPICE model include、nominal section、PVT process section、VDD 范围、MOS model role、尺寸约束、gm/Id table path、Virtuoso tech lib、OA library path，以及必要 topology 的 `topology_presets`。
3. 确认 gm/Id 表包含 topology 需要的 model 名。常规拓扑需要 `nmos/pmos`；folded cascode 当前需要 `nmos_lvt/pmos_lvt`。
4. 运行 profile 验证：

```bash
cd Agent_LLM_BO/circuit_agent
conda activate Auto_Agent_Design
python pdk_profiles.py --validate --require-gmid --require-virtuoso
```

5. 在真实 Cadence/Spectre 机器上加 `--check-files`，确认模型文件和 Virtuoso OA library 路径可见。

优化完成后，`outputs/<project>/pdk_profile_used.json` 会保存当次使用的 profile 快照，方便之后复现实验或排查 PDK 切换问题。
