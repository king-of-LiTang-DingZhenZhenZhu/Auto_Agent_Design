# PCell 单元库

`analogskills.pcell.unit_library` 把 PDK metadata 里的校准结果转换成可被 DSL/SMT 使用的 PCell 单元库。默认只导出 Calibre DRC clean 且 LVS clean 的候选。

## 当前 CRN28 clean 基元

- BJT：只承认 `npn5/M1` 单个 native PCell。`m=2/4/...` 会造成 LVS 面积属性不匹配，不能直接作为等效面积缩放。单元库会从这个 clean primitive 派生虚拟 array realization，例如 `npn_current_M1_array_M4_2x2`、`npn_current_M1_array_M8_2x4`。SMT 选择的是 array macro bbox；streamout 必须展开成多个 `npn5/M1` primitive。
- Resistor：`rnod/rnodl` 已校准 `w/l/sumW/sumL`，当前 clean primitive 候选为 W=2/3/4um、L=5/10um 六种。单元库会从这些 clean primitive 派生 M=2/4/8 的 `resistor_unit_array` macro candidates。
- Capacitor：`nmoscap` 已校准 `wr/lr/c/m/multi`，当前 clean primitive 候选为 `(wr, lr)=(1um,1um)` 和 `(2um,1um)`。更大 native 尺寸目前 LVS 可识别但 native DRC 不 clean，默认不作为 primitive；单元库会从 clean primitive 派生 M=2/4/8 的 `capacitor_unit_array` macro candidates。

## 必须保留的信息

每个候选必须同时包含：

- `layout_width_um/layout_height_um`：SMT 使用的真实 PCell bbox。
- `pcell_params`：streamout/实例化时要传给 native PCell 的真实 CDF 参数。
- `terminal_access`：端口访问坐标。CRN28 passive 的坐标依赖 CDF 参数表达式，例如 resistor 的 `PLUS=(l+0.08,w/2)`，capacitor 的 `PLUS=(lr/2,-0.025)`。
- `drc_clean/lvs_clean/pcell_calibre_usable_for_layout`：决定默认是否进入生产布局。

## 使用方式

```python
from pathlib import Path

from analogskills.pcell import build_pcell_unit_library
from analogskills.pdk import PdkConfig

pdk = PdkConfig.load_json(Path("analogskills/pdk_data/crn28hpcp.json"))
library = build_pcell_unit_library(pdk)

resistor_candidates = library.candidates_for("resistor")
smt_candidates = library.smt_candidates_for("resistor")
```

默认行为是 `clean_only=True`。如果要分析 DRC-dirty 的研究候选，可以显式使用：

```python
research_library = build_pcell_unit_library(pdk, clean_only=False)
```

生产版布局求解不应默认使用 research 候选。

## 后续扩展原则

1. 新增 PCell realization candidate 前，先跑 PCell calibration flow，确认 DRC/LVS 状态。
2. 只有 Calibre clean 的候选才能标记 `pcell_calibre_usable_for_layout=true`。
3. MOS/BJT/resistor/capacitor 的长宽比或阵列选择必须以校准后的候选进入主 SMT；不能只改估计 bbox。
4. 如果某个候选只能靠局部 ECO 修复，需要保留为 dirty/research 候选，等 ECO flow 稳定后再升级为 clean 候选。

## BJT array realization

BJT array candidate 不是新的 foundry PCell，而是由 clean primitive 派生的 macro realization：

- electrical `M=N` 保留在 sizing/SMT 层；
- native PCell 参数保持 `npn5/m=1`；
- `bjt_unit_array` 描述 `rows/cols/unit_count/unit_pcell_params`；
- `generate_pcell_layout_plan()` 在最终 PCell plan 中展开为 `Q_u0...Q_u{N-1}` 多个实例；
- 每个 unit 的 C/B/E 连接继承 parent BJT 的 C/B/E net。

这解决的是 SMT packing 自由度与 LVS correctness 的冲突：SMT 可以选择 1xN、Nx1、近方形等 bbox，但 signoff layout 不实例化 LVS 不可靠的 native `m=N` BJT。

## Resistor / capacitor array realization

R/C array candidate 和 BJT array candidate 的语义不同，不能混用：

- BJT 的并联 unit array 天然等价于 electrical `M=N`。
- Capacitor 的并联 unit array 等价于电容加和，要求 schematic/source 中也体现 N 个 unit 或等价 C 值。
- Resistor 的并联 unit array 等价于电阻减小；series array 等价于电阻增大。当前第一版只生成 `connection_mode=parallel` 的几何 macro，必须配套 schematic expansion 或已验证的 LVS 等效合并。

因此 R/C array candidate 会带：

- `passive_unit_array`
- `requires_schematic_expansion=true`
- `pcell_calibre_status=primitive_clean_array`

SMT 可以用它做紧凑布局探索；signoff flow 在没有同步 schematic/source expansion 前，不应把它声明为完整 block-level LVS clean。

## CRN28 R/C unit array scaffold status

2026-07-20 的 `crn28_passive_array_calibre_0720_v4` 小批量 Calibre 结果：

- `rnod_R1k_W2_L5_array_M4_2x2`：DRC/LVS clean。
- `nmoscap_current_C1f_array_M4_2x2`：DRC/LVS clean。

关键结论：

- R/C array 不能只依赖 OA instance terminal mapping；Calibre 需要真实金属把每个 unit terminal 接到公共 net。
- `analogskills.pcell.calibre_calibration.build_crn28_passive_unit_array_access_plan()` 会读取 `PCellLayoutPlan.metadata.passive_unit_arrays`，并生成已验证的 dogleg bus scaffold。
- CRN28 scaffold 参数从 `pdk_data/crn28hpcp.json` 的 `metadata.calibre.passive_array` 读取：
  - `rail_width_nm=80`
  - `external_bus_margin_nm=300`
  - `row_escape_margin_nm=170`
  - `minimum_array_spacing_nm_by_logical.resistor=500`
  - `minimum_array_spacing_nm_by_logical.capacitor=900`
  - resistor 默认 `MINUS->left`、`PLUS->right`
  - capacitor 默认 `PLUS->left`、`MINUS->right`

这部分不是硬编码到 solver 的 design rule；SMT/unit-library 读取配置产生 array candidate 的 bbox/pitch，版图生成阶段用同一份配置补内部 access scaffold。后续如果新增 R/C primitive 或改变 array 间距，必须重跑 passive array Calibre calibration，再把 clean 结果升级为可选候选。

## MOSFET realization policy

MOSFET 不采用“任意动态生成”策略，也不进入一个固定全局 PCellUnitLibrary 计数。原因是 MOS 的合法性不仅取决于 `W/L/nf/m`，还取决于 native PCell CDF 参数、dummy/DPO/pMetal、body/access scaffold 和 Calibre LVS 参数归一化。

当前 CRN28 策略是 config-constrained dynamic enumeration：

- 电气尺寸 `W/L` 由 sizing 阶段给定，MOS realization 不改变 `W/L`。
- SMT 只在配置允许的 `(nf, m, Wfg)` 候选中选择。
- 候选必须满足 `metadata.pcell_drc_sweep.strongarm_mos.mos_finger_constraints`：
  - `min_finger_width_nm=500`
  - `max_finger_width_nm_by_logical.nmos=2900`
  - `max_finger_width_nm_by_logical.pmos=2900`
  - `max_nf=128`
  - `max_m=64`
  - `prefer_even_nf=true`
- 如果用户/上游 sizing 给出的 current `(nf,m)` 不满足这些配置约束，它不会作为 SMT current candidate 保留；`optimize_crn28_mos_sizing_for_drc()` 会把它修正为配置合法的 split。
- CRN28 MOS PCell overrides 从配置读取，例如 `legal_dummy_geometry_matched_dpo` 里的 dummy/DPO 参数；PMOS 额外叠加 `pMetalOption/pMetalEnc*`。
- 已 Calibre 校准过的候选可以通过 cache/catalog 覆盖估算 bbox；未校准候选仍是 estimated，不应被视作 signoff-proven native realization。

因此 MOS 的正确边界是：

```text
fixed W/L
+ config-constrained nf/m enumeration
+ config-driven PCell overrides
+ optional Calibre cache/catalog promotion
```

不是：

```text
arbitrary CDF params directly generated by SMT/agent
```
