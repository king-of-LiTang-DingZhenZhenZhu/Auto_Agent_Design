# PDK Profiles

项目通过 `Agent_LLM_BO/circuit_agent/pdk_profiles.py` 集中管理工艺相关配置。拓扑脚本不应直接写死 PDK model include 路径或 MOS model 名称，而应从当前 profile 读取。

## 当前默认 profile

`tsmc28`:

| 字段 | 默认值 |
|------|--------|
| Spectre model | `/PDKS/TSMC28nm/models/spectre/toplevel.scs` |
| Spectre section | `top_tt` |
| HSPICE model | `/PDKS/TSMC28nm/models/hspice/toplevel.l` |
| HSPICE section | `TOP_TT` |
| NMOS / PMOS | `nch_mac` / `pch_mac` |
| LVT NMOS / PMOS | `nch_lvt_mac` / `pch_lvt_mac` |
| VDD default/range | default `0.9 V`, allowed `0.9 V ~ 1.1 V` |
| W per finger | `0.2um ~ 2.6um` |
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

当前 `five_t_ota`、`two_stage_ota`、`nmcf_three_stage` 使用常规 MOS；`folded_cascode` 使用 LVT MOS。换 PDK 时，只需要改 profile 中这些 model 名称，topology 会把对应 model 写入生成的 Spectre netlist 和 gm/Id sizing spec。

## 切换或覆盖

优先推荐用 profile 分组：

```bash
export CIRCUIT_AGENT_PDK=tsmc28
```

本地机器路径不同但工艺相同时，可以只覆盖某些字段：

```bash
export PDK_SPECTRE_PATH=/my/pdk/models/spectre/toplevel.scs
export VIRTUOSO_PDK_LIB_PATH=/my/pdk/tsmcN28
```

常用覆盖变量：

| 环境变量 | 作用 |
|----------|------|
| `CIRCUIT_AGENT_PDK` / `PDK_PROFILE` | 选择 profile 名称 |
| `PDK_SPECTRE_PATH`, `PDK_SPECTRE_SECTION` | Spectre include |
| `PDK_HSPICE_PATH`, `PDK_HSPICE_SECTION` | HSPICE include |
| `NMOS_MODEL`, `PMOS_MODEL` | 常规 MOS model |
| `NMOS_LVT_MODEL`, `PMOS_LVT_MODEL` | LVT MOS model |
| `VDD`, `VDD_MIN`, `VDD_MAX` | 默认电源电压和允许范围 |
| `PDK_MIN_L`, `PDK_MAX_WIDTH_PER_FINGER`, `PDK_MIN_WIDTH_PER_FINGER` | 尺寸边界 |
| `VIRTUOSO_TECH_LIB`, `VIRTUOSO_PDK_LIB_PATH` | Virtuoso library 绑定 |

## 添加新工艺

在 `pdk_profiles.py` 的 `PDK_PROFILES` 中新增一项，例如 `gf22` 或 `sky130`，填写 model include、section、NMOS/PMOS model 名称、VDD、尺寸约束和 Virtuoso tech library。拓扑代码会自动读取这些字段。
