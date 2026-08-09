# DFC Capacitor-Free LDO 优化知识

## 电路边界

- 目标结构来自论文 Fig. 4：PMOS 输入误差放大器、增益增强第二级、PMOS pass 管和 DFC 补偿网络。
- 使用 TSMC28 `io_1p8` voltage domain，器件模型为 `nch_18_mac` / `pch_18_mac`。
- `VIN=1.8 V`、`VOUT=0.9 V`、`VREF=0.1 V`，负载范围为论文采用的 `10–100 mA`。
- `VB1`、`VB2`、`VB4` 是外部偏置端口，由 testbench 理想电压源提供，并纳入 BO。

## 节点与器件角色

- `n_stage1`：第一级输出，对应论文小信号 `v1`。
- `n_stage1_inv`：M17/M18 产生的反相小信号副本，对应论文标记的 `-v1`；它不是负直流电压。
- M21/M22：尺寸为 M17/M18 的 `k_gm` 倍，与 M23/M24 电流镜组成增益增强第二级。
- `n_gate`：第二级输出和 PMOS pass 管栅极。
- MD1–MD7：交叉连接的 damping-factor-control 网络；MD1 gate 接第一级输出 `n_stage1 (v1)`，不是 `n_stage1_inv (-v1)`。
- `n_dfc_tail`：MD2 drain 与 MD3/MD4 source 的公共 DFC 尾节点；它不与 `n_stage1` 相连。
- `Cm1`：`n_stage1` 到 `vout` 的全局 Miller 补偿。
- `Cm2`：`n_stage1` 到 `n_dfc_out` 的 DFC 补偿。
- `Cf1`：输出到反馈节点的前馈电容；与反馈电阻一起位于 `Iloop` 探针外侧。
- 默认负载阶跃边沿为 `1 us`；`10 ns` 边沿不属于该论文型 DFC LDO 的默认瞬态合同。
- 不固定 `gm_MD4/gm_MD1=4`。该数值只对应论文的一组设计点；更一般的设计应由式 (10) 的阻尼条件反推：
  `gm4 = 2ζCm1·sqrt(gm2·gmp/(Cg·COUT))`。
- 初始设计可取 `ζ=1/sqrt(2)`，得到
  `gm4 = sqrt(2)Cm1·sqrt(gm2·gmp/(Cg·COUT))`；最终仍以最坏负载下的 STB、PM 和频响峰化为准。
- `Cm1+Cm2+Cf1<12 pF` 由 `Ccomp_total` 和两个分配比例参数硬保证，BO 不直接独立搜索三只电容。
- M12/M13、M14/M15、M17/M18、M23/M24、MD3/MD4 和 MD5/MD6 使用共享 W/L；M21/M22 与 M17/M18 共用 W/L，并只通过整数 `k_gm` 放大 multiplicity。

## Review 顺序

1. 先确认 `VOUT`、pass 管栅压范围和 `10–100 mA` 负载能力。
2. 检查 M11–M24、Mpass、MD1–MD7 的 DC 区域和电流；特别关注 Mpass、M21/M22、MD1/MD5 的余量。
3. 用最小负载 STB 结果检查 Gain、GBW 和 PM，再分析 `Cm1/Cm2/Cf1`。
4. 检查 `n_stage1_inv` 是否围绕合理直流点形成反相信号；不要把论文 `-v1` 当成负电源节点。
5. 分开判断 DC PSR、load regulation 和负载阶跃过冲/下冲，避免只靠增大 pass 管或补偿电容掩盖根因。
6. 检查 `VB1/VB2/VB4` 是否贴近 BO 边界；若偏置决定了错误工作区，优先调整偏置而非盲目扩大尺寸。

## 参数方向

- `Wpass` 增大通常改善满载压差与负载瞬态，但增加 gate pole、电容和面积。
- `k_gm` 增大可增强第二级等效跨导，但会提高电流需求并改变非主极点。
- `Cm1` 主要控制 Miller 主极点；过大会降低 GBW 和瞬态速度。
- `Cm2` 与 DFC 支路共同调节阻尼；必须结合 `n_dfc_out` 的 DC 点与 STB 曲线判断。
- `Cf1` 可改善高频反馈和负载瞬态，但会引入额外零极点；调整时确认 STB 探针截获完整路径。
- `feedback_ratio` 的一阶关系为 `VOUT ≈ VREF·(1+feedback_ratio)`；在 `0.1 V → 0.9 V` 时中心值为 8。
