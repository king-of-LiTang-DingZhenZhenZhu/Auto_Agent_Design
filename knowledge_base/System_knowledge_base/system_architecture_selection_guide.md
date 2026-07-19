# 系统级电路架构选择与模块分解指南

本文档用于系统级模拟/混合信号设计的架构选择、模块分解和指标预算。
`AGENTS.md` 与 `CLAUDE.md` 只定义 Agent 的工作流程；具体电路类别的设计知识应维护在本知识库中。

## 通用决策顺序

```text
顶层功能与指标
  → 候选系统架构
  → block graph 与接口
  → 误差/噪声/速度/功耗预算
  → child targets 与 PVT 裕量
  → child topology
  → child BO/PVT
  → frozen artifact
  → parent BO/PVT
```

系统架构选择和晶体管级拓扑选择是两个不同层次的决策：

- 系统架构决定需要哪些功能模块，以及各模块之间如何连接和分配预算。
- child topology selection 在局部指标确定后进行，例如为 residue amplifier 选择 5T、两级 Miller 或 folded-cascode OTA。
- parent 验证失败时，先检查接口、测试平台和预算假设，再检查 child 裕量，最后才更换 child topology 或系统架构。
- v1 使用分阶段冻结策略，不把所有 child 的 W/L 展开到 parent 做 joint BO。

## Child Target 要求

每个 child target 至少记录：

- 指标来源、推导公式和适用假设；
- nominal target、设计裕量和 PVT target；
- 输入输出范围、共模范围、负载和驱动条件；
- 电源域、时钟条件、接口端口和 subckt 名；
- 分配的功耗、噪声、误差、面积和延迟预算；
- parent gap 应回传到哪个 child，以及何时需要重新分配预算。

不能把顶层指标直接复制给所有 child。系统周期、总噪声或总功耗通常需要扣除公共开销并按模块贡献重新分配。

## ADC 架构示例

### SAR ADC

根据分辨率、采样率、输入带宽、功耗和参考建立要求选择采样网络、CDAC、比较器、参考驱动、时钟和 SAR logic。

- SAR ADC 不必然需要运放。
- 只有前端缓冲或参考驱动存在闭环精度、驱动能力或建立速度需求时，才派生运放 child target。
- 比较器、CDAC、参考驱动和采样开关需要分别分配噪声、失调、建立误差和功耗预算。

### Pipeline ADC

先确定级数、每级位数、冗余和级间误差预算，再派生 residue amplifier 指标：

- 由允许的闭环增益误差派生有限 DC gain 要求；
- 由有效放大时间、反馈因子和允许建立误差派生 GBW 与 PM；
- 由最大输出步进和有效放大时间派生 SR；
- 由 kT/C、运放噪声和量化噪声预算派生输入等效噪声；
- 同时约束输出摆幅、线性度、负载和功耗。

ADC 总周期不能直接作为运放建立时间，必须扣除采样、非重叠时钟、比较和数字延迟。

### Sigma-Delta ADC

先确定环路阶数、OSR、量化器、NTF 和积分器实现，再从积分器泄漏、噪声和内部状态摆幅派生 OTA 的 DC gain、UGF、SR、噪声、线性度和输出摆幅。

## Bandgap/PTAT 示例

先确定 `Vref`、tempco、line/load regulation、启动时间、噪声和功耗预算，再分解为：

- PTAT/CTAT 核心；
- error amplifier；
- startup；
- bias/current reference；
- 必要的输出缓冲或 trim/calibration。

error amplifier 的增益、输入共模、失调、输出摆幅、GBW、负载和功耗应由 bandgap 环路和误差预算派生，而不是直接套用通用运放指标。

当前代码中的 `bandgap_ptat` 已接入 frozen child opamp 流程。ADC 架构、ADC 专用指标预算器和 ADC topologies 尚未实现。

## 扩展约定

内容增长后按电路类别拆分到本目录的子目录，例如：

```text
System_knowledge_base/
├── system_architecture_selection_guide.md
├── ADC/
├── References/
├── Power_Management/
└── Clocking/
```

通用流程保留在本文档；公式、架构比较表、模块预算方法和 Review 规则放入对应类别文件，避免继续扩充 Agent 操作手册。
