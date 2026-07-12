# BO 与 gm/Id Sizing 模式说明

本文说明当前项目中两种优化参数空间的区别：普通物理参数 BO 和 gm/Id 模式。注意：gm/Id 模式中仍然使用 BO，只是 BO 搜索的变量从直接 W/L 变成 gm/Id、电流、比例等更高层参数。

## 1. 普通物理参数 BO

普通 BO 模式下，BO 直接搜索电路物理参数，例如：

```text
Wtail, Ltail, Wdiff, Ldiff, Wcs, Cc, Rz, VBIAS ...
```

流程：

```text
BO 建议物理 W/L/C/R/VBIAS
  -> NetlistTemplate.render()
  -> 生成 circuit.cir
  -> Spectre 仿真
  -> 解析指标
  -> reward
  -> BO 下一轮
```

特点：

- BO 直接控制晶体管尺寸。
- gm/Id lookup 不参与。
- 参数空间完全来自 topology 的 `get_param_space()`，或用户显式传入的 `params.json`。
- 如果参数空间设置太宽，BO 可能给出不合理工作点。

## 2. gm/Id 模式

gm/Id 模式下，BO 不直接搜索所有 W，而是搜索 gm/Id 层参数，例如：

```text
gm_id_diff_pair_pmos
L_diff_pair_pmos
gm_id_cs_pmos
I_tail
m_half_unit
m_load_ratio
Cc
Rz
```

随后 `GmidSizer` 根据 gm/Id lookup table 推导物理尺寸：

```text
BO 建议 gm/Id / L / 电流 / 电流比例
  -> GmidSizer.size()
  -> 查 gm/Id table
  -> 推导 W
  -> 得到 W/L/C/R 等物理参数
  -> NetlistTemplate.render()
  -> Spectre 仿真
  -> reward
  -> BO 下一轮
```

典型 W 计算关系：

```text
W = Id_target / id_w(gm_id, L, Vds, Vbs)
```

所以 gm/Id 模式仍然是 BO 优化，只是 BO 搜索的是更接近模拟设计意图的参数。

## 3. gm/Id 模式何时启动

当前 `main.py` 的逻辑是自动模式：

```text
如果 requirements.json 中有 topology_name
  -> get_topology(topology_name)
  -> 调 topology.get_gmid_spec(targets)
  -> 如果返回非 None
       启动 gm/Id 模式
     否则
       使用 topology.get_param_space() 普通模式
```

也就是说，只要满足以下条件，当前会自动进入 gm/Id 模式：

- 使用 `topo.write_project()` 生成项目，`requirements.json` 中包含 `topology_name`。
- 运行 `main.py` 时传入 `--requirements <project>/requirements.json`。
- 对应 topology 的 `get_gmid_spec(targets)` 返回有效 `GmidTopologySpec`。

当前以下 topology 都实现了 `get_gmid_spec()`：

```text
five_t_ota
two_stage_ota
folded_cascode
folded_cascode_two_stage
nmcf_three_stage
```

因此，用这些 topology 生成项目并带 `--requirements` 运行时，通常会自动启用 gm/Id 模式。

## 4. 什么时候不会使用 gm/Id

以下情况会回到普通物理参数 BO：

- 没有传 `--requirements`。
- `requirements.json` 中没有 `topology_name`。
- topology 没有实现 `get_gmid_spec()`。
- `get_gmid_spec(targets)` 或 gm/Id lookup 初始化失败。

目前 `--params` 不是明确的“关闭 gm/Id”开关。如果 topology 有有效 gm/Id spec，代码仍会把参数空间替换为 gm/Id 参数空间。

## 5. 两种模式对比

| 项目 | 普通物理参数 BO | gm/Id 模式 |
|---|---|---|
| 是否使用 BO | 是 | 是 |
| BO 搜索什么 | 物理 W/L/C/R/V | gm/Id、L、电流、比例、Cc/Rz |
| W 如何得到 | BO 直接给出 | lookup 根据 gm/Id 和电流推导 |
| 电流如何决定 | 由尺寸和偏置间接决定 | BO 直接或通过比例决定 |
| 物理约束 | 依赖 `get_param_space()` | 受 gm/Id lookup 和 topology spec 约束 |
| 优点 | 简单直接 | 更接近模拟设计流程，工作点更有物理意义 |
| 风险 | 容易探索坏工作点 | lookup 偏差、电流比例不合理时仍可能失败 |

## 6. gm/Id 表中离散 L 的处理

gm/Id lookup table 中的 L 是离散的。当前实现不会要求 BO 给出的 L 必须精确等于表中 L。

处理方式：

- `gm_id` 维度：对固定 `(model, L, Vds, Vbs)` sweep 做线性插值；超出范围时 clamp。
- `L` 维度：如果 BO 给出的 L 不在表中，会找相邻两个 L grid，并对 lookup 结果做线性插值。
- `Vds/Vbs` 维度：当前 snap 到最近的表中 grid。

例如：

```text
BO 给出 L = 350nm
表中有 L = 300nm 和 400nm
  -> 分别查 300nm / 400nm
  -> 对 id_w, vgs, gain, ft, gds, cgg, vth 做线性插值
  -> 得到 350nm 的估算结果
```

如果后续发现 gm/Id sizing 和 Spectre 偏差较大，可以考虑把 gm/Id 模式下的 L 改成离散/categorical 参数，只允许 BO 从 lookup table 已有 L 中选择。

## 7. 后续建议

当前 sizing 模式是自动选择。为了调试更清晰，后续可以给 `main.py` 增加显式选项：

```bash
--sizing-mode auto
--sizing-mode gmid
--sizing-mode physical
```

建议含义：

- `auto`：保持当前行为，有 gm/Id spec 就启用。
- `gmid`：强制 gm/Id，不可用就报错。
- `physical`：强制使用普通物理参数 BO，不调用 gm/Id lookup。
